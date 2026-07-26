from __future__ import annotations

import asyncio
import os
import re
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import HTTPException

from modules.channel_health_watcher import ChannelHealthWatcher
from utils.database import Database
from utils.health_monitor import HealthMonitor


class FiveAccountSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        Database.reset_instance()
        self.db = Database(os.path.join(self._tmpdir.name, "audit.db"))
        self.account_ids = [
            self.db.add_account(
                f"+7000000000{i}",
                f"account-{i}",
                1000 + i,
                f"hash-{i}",
            )
            for i in range(5)
        ]

    async def asyncTearDown(self):
        Database.reset_instance()
        self._tmpdir.cleanup()

    async def test_health_monitor_flood_wait_records_shared_history(self):
        monitor = HealthMonitor(self.db)

        monitor.record_flood_wait(self.account_ids[0], 7200)

        history = self.db.get_rate_limit_history(self.account_ids[0])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["action"], "flood_wait")

    async def test_stale_health_success_cannot_clear_database_pause(self):
        monitor = HealthMonitor(self.db)
        monitor.get_status(self.account_ids[0])
        self.db.record_flood_wait(self.account_ids[0], 7200)

        monitor.record_success(self.account_ids[0])

        health = self.db.get_account_health(self.account_ids[0])
        self.assertEqual(health["health_status"], "rate_limited")
        self.assertGreater(self.db.get_account_pause_seconds(self.account_ids[0]), 0)

    async def test_stats_full_uses_stored_comment_and_invite_counts(self):
        import backend.main as backend_main

        old_db = backend_main.db
        try:
            backend_main.db = self.db
            self.db.increment_daily_stat(
                self.account_ids[0], "channel", success=True
            )
            self.db.add_invite_result(
                self.account_ids[0], 123, 456, "success"
            )

            result = await backend_main.get_account_stats_full(self.account_ids[0])

            self.assertEqual(result["comments"], 1)
            self.assertEqual(result["invites"], 1)
        finally:
            backend_main.db = old_db

    async def test_statistics_reset_preserves_operational_state(self):
        import backend.main as backend_main

        old_db = backend_main.db
        try:
            backend_main.db = self.db
            account_id = self.account_ids[0]
            self.db.add_found_channel("channel", "Channel", "test")
            self.db.mark_banned(account_id, "channel")
            self.db.mark_post_processed(account_id, "channel", 1)
            self.db.increment_daily_stat(account_id, "channel", success=True)
            self.db.increment_stat(account_id, "likes")
            self.db.add_invite_result(account_id, 123, 456, "success")
            campaign_id = self.db.add_mass_send_campaign(
                account_id, "campaign", "hello", total_targets=1
            )
            self.db.add_mass_send_result(
                campaign_id, account_id, "789", "dm", "sent"
            )
            self.db.record_rate_limit_event(account_id, "audit", 60)

            await backend_main.clear_all_stats()

            with self.db.get_connection() as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM account_stats").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM invite_stats").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM mass_send_campaigns"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM rate_limit_history"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM channel_bans"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM processed_posts"
                    ).fetchone()[0],
                    1,
                )
        finally:
            backend_main.db = old_db

    async def test_profile_sync_runs_five_workers_concurrently(self):
        import backend.main as backend_main

        class FakeWorker:
            def __init__(self, account_id):
                self.account_id = account_id
                self.is_running = True
                self.loop = asyncio.new_event_loop()
                self.client = self
                self.thread = threading.Thread(
                    target=self._run_loop,
                    daemon=True,
                )
                self.thread.start()
                while not self.loop.is_running():
                    time.sleep(0.001)

            def _run_loop(self):
                asyncio.set_event_loop(self.loop)
                self.loop.run_forever()

            async def get_me(self):
                await asyncio.sleep(0.2)
                return SimpleNamespace(
                    first_name=f"Account {self.account_id}",
                    username=f"user{self.account_id}",
                )

            def close(self):
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.thread.join(timeout=2)
                self.loop.close()

        old_db = backend_main.db
        old_workers = backend_main.workers
        workers = {}
        try:
            backend_main.db = self.db
            workers = {
                account_id: FakeWorker(account_id)
                for account_id in self.account_ids
            }
            backend_main.workers = workers

            started = time.perf_counter()
            result = await backend_main.sync_account_profiles()
            elapsed = time.perf_counter() - started

            self.assertEqual(result["synced"], 5)
            self.assertLess(elapsed, 0.75)
        finally:
            for worker in workers.values():
                worker.close()
            backend_main.db = old_db
            backend_main.workers = old_workers

    async def test_watcher_does_not_delete_channel_for_account_local_lookup_error(
        self,
    ):
        self.db.add_found_channel("channel", "Channel", "test")
        watcher = ChannelHealthWatcher(self.db, workers_registry={})

        class LocalLookupFailure:
            async def get_entity(self, _name):
                raise ValueError("Cannot find any entity")

        worker = SimpleNamespace(client=LocalLookupFailure())

        result = await watcher._validate_channel(
            worker,
            {
                "channel": "channel",
                "can_comment": 1,
            },
        )

        self.assertEqual(result["action"], "no_change")
        with self.db.get_connection() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM found_channels WHERE channel='channel'"
                ).fetchone()[0],
                1,
            )

    async def test_start_does_not_overwrite_authoritative_banned_status(self):
        import backend.main as backend_main

        account_id = self.account_ids[0]
        self.db.update_account_status(account_id, "banned")
        old_db = backend_main.db
        old_workers = backend_main.workers
        old_start = backend_main._start_worker_for_account
        try:
            backend_main.db = self.db
            backend_main.workers = {}
            backend_main._start_worker_for_account = lambda _account: None

            with self.assertRaises(HTTPException) as raised:
                await backend_main.start_bot(account_id)

            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(
                self.db.get_account(account_id)["status"],
                "banned",
            )
        finally:
            backend_main.db = old_db
            backend_main.workers = old_workers
            backend_main._start_worker_for_account = old_start

    async def test_delete_waits_for_worker_shutdown_helper(self):
        import backend.main as backend_main

        account_id = self.account_ids[0]
        old_db = backend_main.db
        old_workers = backend_main.workers
        old_stop = backend_main._stop_worker_for_account
        try:
            backend_main.db = self.db
            backend_main.workers = {account_id: SimpleNamespace(is_running=True)}
            stopped = []

            def stop_worker(target_id, **_kwargs):
                stopped.append(target_id)
                backend_main.workers.pop(target_id, None)
                return True

            backend_main._stop_worker_for_account = stop_worker

            await backend_main.delete_account(account_id)

            self.assertEqual(stopped, [account_id])
            self.assertIsNone(self.db.get_account(account_id))
        finally:
            backend_main.db = old_db
            backend_main.workers = old_workers
            backend_main._stop_worker_for_account = old_stop

    async def test_rate_limit_polling_targets_account_cards(self):
        with open(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "static",
                "app.js",
            ),
            encoding="utf-8",
        ) as source_file:
            source = source_file.read()

        self.assertRegex(source, r'<div class="account-card[^>]*data-account-id=')

    async def test_operational_reset_restarts_only_eligible_running_accounts(self):
        import backend.main as backend_main

        old_db = backend_main.db
        old_workers = backend_main.workers
        old_stop = backend_main._stop_worker_for_account
        old_start = backend_main._start_worker_for_account
        try:
            backend_main.db = self.db
            self.db.update_account_status(self.account_ids[0], "active")
            self.db.update_account_status(self.account_ids[1], "banned")
            self.db.set_setting("work_mode", "safe")
            self.db.add_found_channel("channel", "Channel", "test")
            self.db.mark_banned(self.account_ids[0], "channel")
            self.db.mark_post_processed(self.account_ids[0], "channel", 1)

            class FakeWorker:
                is_running = True

            backend_main.workers = {self.account_ids[0]: FakeWorker()}
            stopped = []
            started = []

            def stop_worker(account_id, **_kwargs):
                stopped.append(account_id)
                backend_main.workers.pop(account_id, None)
                return True

            def start_worker(account):
                started.append(account["id"])
                return FakeWorker()

            backend_main._stop_worker_for_account = stop_worker
            backend_main._start_worker_for_account = start_worker

            result = await backend_main.reset_operational_state()

            self.assertEqual(stopped, [self.account_ids[0]])
            self.assertEqual(started, [self.account_ids[0]])
            self.assertEqual(result["restarted"], [self.account_ids[0]])
            self.assertEqual(
                self.db.get_account(self.account_ids[1])["status"],
                "banned",
            )
            self.assertEqual(self.db.get_setting("work_mode"), "safe")
            with self.db.get_connection() as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM channel_bans").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM processed_posts"
                    ).fetchone()[0],
                    0,
                )
        finally:
            backend_main.db = old_db
            backend_main.workers = old_workers
            backend_main._stop_worker_for_account = old_stop
            backend_main._start_worker_for_account = old_start

    async def test_five_account_daily_stats_writes_are_exact(self):
        def write_for(account_id):
            for index in range(50):
                self.db.increment_daily_stat(
                    account_id,
                    f"channel-{index % 5}",
                    success=True,
                )

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(write_for, self.account_ids))

        self.assertEqual(
            [
                self.db.get_stats_summary(account_id)["comments"]
                for account_id in self.account_ids
            ],
            [50] * 5,
        )


if __name__ == "__main__":
    unittest.main()
