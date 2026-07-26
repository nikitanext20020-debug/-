from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from utils.database import Database


ROOT = Path(__file__).resolve().parents[1]


class AccountJoinModeDatabaseTests(unittest.TestCase):
    def setUp(self):
        Database.reset_instance()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "join-modes.db")

    def tearDown(self):
        Database.reset_instance()
        self._tmpdir.cleanup()

    def test_legacy_accounts_default_to_normal_and_new_accounts_to_new(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE,
                    session_name TEXT NOT NULL,
                    api_id INTEGER,
                    api_hash TEXT,
                    proxy_id INTEGER,
                    status TEXT DEFAULT 'stopped',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO accounts (phone, session_name) VALUES (?, ?)",
                ("+70000000001", "legacy"),
            )

        db = Database(self.db_path)
        legacy = db.get_account(1)
        self.assertEqual(legacy["join_mode"], "normal")
        self.assertIsNone(legacy["next_join_at"])

        new_id = db.add_account("+70000000002", "new-account", 1, "hash")
        new_account = db.get_account(new_id)
        self.assertEqual(new_account["join_mode"], "new")

    def test_join_mode_state_validates_and_schedules_seconds(self):
        db = Database(self.db_path)
        account_id = db.add_account("+70000000003", "scheduled", 1, "hash")

        state = db.set_account_join_mode(
            account_id,
            "new",
            now=1_000.0,
            delay_seconds=150,
        )
        self.assertEqual(state["mode"], "new")
        self.assertEqual(state["min_delay"], 120)
        self.assertEqual(state["max_delay"], 180)
        self.assertEqual(state["remaining_seconds"], 150)

        due = db.activate_account_join_slot(account_id, now=1_000.0)
        self.assertEqual(due["remaining_seconds"], 0)
        self.assertTrue(db.is_account_join_due(account_id, now=1_000.0))

        off = db.set_account_join_mode(account_id, "off", now=1_000.0)
        self.assertEqual(off["mode"], "off")
        self.assertIsNone(off["next_join_at"])
        self.assertFalse(db.is_account_join_due(account_id, now=1_000.0))

        with self.assertRaises(ValueError):
            db.set_account_join_mode(account_id, "unsafe")


class AccountJoinModeAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Database.reset_instance()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmpdir.name, "join-api.db"))
        self.account_id = self.db.add_account(
            "+70000000004", "api-account", 1, "hash"
        )
        import backend.main as backend_main

        self.backend_main = backend_main
        self.old_db = backend_main.db
        self.old_workers = backend_main.workers
        backend_main.db = self.db
        backend_main.workers = {}
        backend_main._set_global_pause(False)

    async def asyncTearDown(self):
        self.backend_main._set_global_pause(False)
        self.backend_main.db = self.old_db
        self.backend_main.workers = self.old_workers
        Database.reset_instance()
        self._tmpdir.cleanup()

    async def test_join_mode_endpoint_persists_valid_mode_and_rejects_invalid(self):
        result = await self.backend_main.update_account_join_mode(
            self.account_id,
            SimpleNamespace(mode="careful"),
        )
        self.assertEqual(result["mode"], "careful")
        self.assertGreaterEqual(result["remaining_seconds"], 60)
        self.assertLessEqual(result["remaining_seconds"], 120)

        with self.assertRaises(HTTPException) as ctx:
            await self.backend_main.update_account_join_mode(
                self.account_id,
                SimpleNamespace(mode="unsafe"),
            )
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_private_join_button_activates_slots_without_global_pause(self):
        self.db.add_found_channel(
            "+privateInvite",
            "Private",
            "manual",
            can_comment=True,
            min_subs=0,
        )
        worker = SimpleNamespace(is_running=True)
        self.backend_main.workers = {self.account_id: worker}
        self.db.set_account_join_mode(
            self.account_id,
            "new",
            now=time.time(),
            delay_seconds=150,
        )

        result = await self.backend_main.join_all_private_channels()

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["queued_accounts"], [self.account_id])
        self.assertFalse(self.backend_main.global_pause)
        from backend.worker import is_global_paused
        self.assertFalse(is_global_paused())
        self.assertTrue(
            self.db.is_account_join_due(self.account_id, now=time.time())
        )


class AccountJoinModeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Database.reset_instance()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmpdir.name, "join-worker.db"))
        self.account_id = self.db.add_account(
            "+70000000005", "worker-account", 1, "hash"
        )
        from backend.worker import BotWorker

        self.worker = object.__new__(BotWorker)
        self.worker.account_id = self.account_id
        self.worker.db = self.db
        self.worker.is_running = True
        self.worker._shutdown_flag = False
        self.worker._join_in_progress = False
        self.worker.health_monitor = SimpleNamespace(should_pause=lambda _id: False)
        self.worker._join_next_channel = mock.AsyncMock(return_value=True)

    async def asyncTearDown(self):
        Database.reset_instance()
        self._tmpdir.cleanup()

    async def test_scheduler_step_joins_once_and_schedules_next_slot(self):
        self.db.set_account_join_mode(
            self.account_id, "new", now=1_000.0, delay_seconds=150
        )
        self.db.activate_account_join_slot(self.account_id, now=1_000.0)

        with mock.patch("backend.worker.random.randint", return_value=150):
            result = await self.worker._join_scheduler_step(now=1_000.0)

        self.worker._join_next_channel.assert_awaited_once()
        self.assertEqual(result["action"], "joined")
        state = self.db.get_account_join_state(self.account_id, now=1_000.0)
        self.assertEqual(state["remaining_seconds"], 150)

    async def test_scheduler_step_skips_off_and_frozen_join_accounts(self):
        self.db.set_account_join_mode(self.account_id, "off", now=1_000.0)
        result = await self.worker._join_scheduler_step(now=1_000.0)
        self.assertEqual(result["action"], "off")
        self.worker._join_next_channel.assert_not_awaited()

        self.db.set_account_join_mode(
            self.account_id, "normal", now=1_000.0, delay_seconds=30
        )
        self.db.activate_account_join_slot(self.account_id, now=1_000.0)
        self.db.update_account_status(self.account_id, "frozen_join")
        result = await self.worker._join_scheduler_step(now=1_000.0)
        self.assertEqual(result["action"], "blocked")
        self.worker._join_next_channel.assert_not_awaited()


class AccountJoinModeSourceContractTests(unittest.TestCase):
    def test_dashboard_renders_join_mode_and_countdown(self):
        js = (ROOT / "static" / "app.js").read_text()
        self.assertIn("data-dash-join-mode", js)
        self.assertIn("/accounts/${id}/join-mode", js)
        self.assertIn("data-join-remaining", js)

    def test_old_unused_join_constants_and_blocking_sleeps_are_removed(self):
        config = (ROOT / "config.py").read_text()
        worker = (ROOT / "backend" / "worker.py").read_text()
        main = (ROOT / "backend" / "main.py").read_text()
        self.assertNotIn("JOIN_CHANNEL_DELAY_MIN", config)
        self.assertNotIn("JOIN_CHANNEL_DELAY_MAX", config)
        self.assertNotIn("_gradual_join_from_database", worker)
        self.assertNotIn("await self._join_pending_channels()", worker)
        self.assertNotIn("_set_global_pause(True)", main[main.index('@app.post("/discovery/channels/join-private")'):main.index('@app.post("/discovery/channels/fix-private-titles")')])
