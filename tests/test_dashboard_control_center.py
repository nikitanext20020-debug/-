from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from utils.database import Database


ROOT = Path(__file__).resolve().parents[1]


class DashboardControlCenterTests(unittest.TestCase):
    def setUp(self):
        Database.reset_instance()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmpdir.name, "dashboard.db"))
        self.account_id = self.db.add_account(
            "+79990000001",
            "dashboard-account",
            1001,
            "hash",
        )

    def tearDown(self):
        Database.reset_instance()
        self._tmpdir.cleanup()

    def test_account_mode_override_defaults_to_inherit_and_roundtrips(self):
        account = self.db.get_account(self.account_id)
        self.assertIn("work_mode_override", account)
        self.assertIsNone(self.db.get_account_mode_override(self.account_id))

        self.db.set_account_mode_override(self.account_id, "chill")
        self.assertEqual(
            self.db.get_account_mode_override(self.account_id),
            "chill",
        )

        self.db.set_account_mode_override(self.account_id, None)
        self.assertIsNone(self.db.get_account_mode_override(self.account_id))

    def test_account_mode_route_validates_and_persists_mode(self):
        import backend.main as backend_main

        old_db = backend_main.db
        try:
            backend_main.db = self.db
            result = asyncio.run(
                backend_main.update_account_mode(
                    self.account_id,
                    SimpleNamespace(mode="powerful"),
                )
            )
            self.assertEqual(result["mode"], "powerful")
            self.assertEqual(
                self.db.get_account_mode_override(self.account_id),
                "powerful",
            )

            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    backend_main.update_account_mode(
                        self.account_id,
                        SimpleNamespace(mode="unsafe"),
                    )
                )
            self.assertEqual(ctx.exception.status_code, 422)
        finally:
            backend_main.db = old_db

    def test_worker_prefers_override_and_falls_back_to_global_mode(self):
        import backend.worker as worker_module

        self.db.set_setting("work_mode", "neutral")
        self.db.set_setting("auto_night_mode", False)
        worker = object.__new__(worker_module.BotWorker)
        worker.account_id = self.account_id
        worker.db = self.db

        self.assertEqual(worker._get_current_mode(), "neutral")
        self.db.set_account_mode_override(self.account_id, "powerful")
        self.assertEqual(worker._get_current_mode(), "powerful")

    def test_dashboard_contains_control_center_blocks_and_mode_hook(self):
        html = (ROOT / "static" / "index.html").read_text()
        js = (ROOT / "static" / "app.js").read_text()
        self.assertIn('id="dashboard-quick-actions"', html)
        self.assertIn('id="btn-dashboard-start-all"', html)
        self.assertIn('id="btn-dashboard-stop-all"', html)
        self.assertIn('id="dash-comments-list"', html)
        self.assertIn('id="dash-errors-list"', html)
        self.assertRegex(js, r'data-dash-mode')

    def test_dashboard_js_loads_comments_errors_and_mode_endpoint(self):
        js = (ROOT / "static" / "app.js").read_text()
        self.assertIn("/comments?limit=10", js)
        self.assertIn("/logs?limit=20&level=error,critical", js)
        self.assertIn("/accounts/${id}/mode", js)
        self.assertIn("btn-dashboard-start-all", js)
        self.assertIn("btn-dashboard-stop-all", js)

    def test_dashboard_css_has_responsive_control_center_styles(self):
        css = (ROOT / "static" / "style.css").read_text()
        self.assertIn(".dash-quick-actions", css)
        self.assertIn(".dash-feed", css)
        self.assertIn("@media (max-width: 900px)", css)
