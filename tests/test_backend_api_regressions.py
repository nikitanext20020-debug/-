from __future__ import annotations

import asyncio
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
_IMPORT_DATA = tempfile.TemporaryDirectory()
_OLD_DATA_DIR = os.environ.get("DATA_DIR")
_OLD_SESSIONS_DIR = os.environ.get("SESSIONS_DIR")
os.environ["DATA_DIR"] = os.path.join(_IMPORT_DATA.name, "data")
os.environ["SESSIONS_DIR"] = os.path.join(_IMPORT_DATA.name, "sessions")

from backend import main as backend_main  # noqa: E402
from backend import worker as worker_module  # noqa: E402

if _OLD_DATA_DIR is None:
    os.environ.pop("DATA_DIR", None)
else:
    os.environ["DATA_DIR"] = _OLD_DATA_DIR
if _OLD_SESSIONS_DIR is None:
    os.environ.pop("SESSIONS_DIR", None)
else:
    os.environ["SESSIONS_DIR"] = _OLD_SESSIONS_DIR


class _RateLimitDB:
    def __init__(self, health_status: str):
        self.health_status = health_status
        self.reset_calls = []

    def get_account(self, account_id):
        return {
            "id": account_id,
            "phone": "+100",
            "session_name": "session_100",
            "api_id": 1,
            "api_hash": "hash",
            "status": "stopped",
            "rate_limited_until": None,
        }

    def get_account_health(self, account_id):
        return {
            "health_status": self.health_status,
            "rate_limited_until": None,
        }

    def get_account_pause_seconds(self, account_id):
        return 0

    def reset_account_health(self, account_id):
        self.reset_calls.append(account_id)
        self.health_status = "healthy"


class _ImmediateFuture:
    def __init__(self, value):
        self.value = value

    def result(self, timeout=None):
        return self.value


class BackendAPIRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_db = backend_main.db
        self._old_workers = backend_main.workers
        backend_main.workers = {}
        backend_main._set_global_pause(False)

    async def asyncTearDown(self):
        backend_main._set_global_pause(False)
        backend_main.db = self._old_db
        backend_main.workers = self._old_workers

    async def test_expired_rate_limit_restarts_through_worker_registry(self):
        fake_db = _RateLimitDB("rate_limited")
        backend_main.db = fake_db

        def start_worker(account):
            worker = SimpleNamespace(is_running=True, thread=None)
            backend_main.workers[account["id"]] = worker
            return worker

        with mock.patch.object(
            backend_main,
            "_start_worker_for_account",
            side_effect=start_worker,
        ):
            payload = await backend_main.check_rate_limit_status(7)

        self.assertIn(7, backend_main.workers)
        self.assertEqual(fake_db.reset_calls, [7])
        self.assertTrue(payload["auto_restarted"])
        self.assertFalse(payload["blocked"])

    async def test_ready_account_does_not_claim_automatic_restart(self):
        backend_main.db = _RateLimitDB("healthy")

        payload = await backend_main.check_rate_limit_status(7)

        self.assertFalse(payload["auto_restarted"])
        self.assertNotIn(7, backend_main.workers)

    async def test_group_search_route_schedules_existing_dashboard_contract(self):
        route = next(
            (
                route
                for route in backend_main.app.routes
                if getattr(route, "path", None) == "/discovery/chats/search"
                and "POST" in (getattr(route, "methods", set()) or set())
            ),
            None,
        )
        self.assertIsNotNone(route, "POST /discovery/chats/search is not registered")

        class FakeKeywordSearch:
            def __init__(self):
                self.received = None

            def search_groups_by_keywords(self, keywords):
                self.received = keywords

                async def task():
                    return []

                return task()

        class FakeWorker:
            is_running = True

            def __init__(self):
                self.keyword_search = FakeKeywordSearch()

            def run_task(self, coro):
                coro.close()
                return object()

        worker = FakeWorker()
        backend_main.workers = {1: worker}

        payload = await route.endpoint(
            SimpleNamespace(keywords=[" anime ", "ANIME", "manga"])
        )

        self.assertEqual(worker.keyword_search.received, ["anime", "manga"])
        self.assertEqual(payload["status"], "started")
        self.assertEqual(payload["keywords"], ["anime", "manga"])

    async def test_join_private_queues_accounts_without_global_pause(self):
        class FakeDB:
            def __init__(self):
                self.activated = []

            def get_found_channels(self, **kwargs):
                return [{"channel": "+invite", "title": "Private"}]

            def get_account(self, account_id):
                return {"id": account_id, "status": "active"}

            def get_account_join_state(self, account_id, now=None):
                return {"mode": "new"}

            def activate_account_join_slot(self, account_id, now=None):
                self.activated.append(account_id)
                return {"mode": "new", "remaining_seconds": 0}

        class FakeWorker:
            is_running = True

        worker = FakeWorker()
        fake_db = FakeDB()
        backend_main.db = fake_db
        backend_main.workers = {1: worker}

        payload = await backend_main.join_all_private_channels()

        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["queued_accounts"], [1])
        self.assertEqual(fake_db.activated, [1])
        self.assertFalse(worker_module.is_global_paused())
        self.assertFalse(backend_main.global_pause)


class DirectEntrypointRegressionTests(unittest.TestCase):
    def test_direct_entrypoint_registers_dashboard_before_uvicorn_starts(self):
        captured_paths = set()

        def capture_run(app, **kwargs):
            captured_paths.update(
                getattr(route, "path", "") for route in app.routes
            )

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "uvicorn.run",
            side_effect=capture_run,
        ), mock.patch.dict(
            os.environ,
            {
                "DATA_DIR": os.path.join(tmpdir, "data"),
                "SESSIONS_DIR": os.path.join(tmpdir, "sessions"),
            },
        ):
            runpy.run_path(str(ROOT / "backend" / "main.py"), run_name="__main__")

        self.assertIn("/", captured_paths)
        self.assertIn("/auth-status", captured_paths)


def tearDownModule():
    _IMPORT_DATA.cleanup()


if __name__ == "__main__":
    unittest.main()
