"""
Regression tests:
- telegram target normalization / ordered dedupe / invite rejection
- global channel exclusion gate
- source-specific parsed users (junction)
- mass-send campaign lifecycle columns
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Project root on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.telegram_targets import (  # noqa: E402
    ensure_sendable_targets,
    normalize_targets,
    parse_telegram_target,
    preview_targets,
)
from utils.database import Database  # noqa: E402
from config import Config  # noqa: E402


class TargetNormalizationTests(unittest.TestCase):
    def test_numeric_ids(self):
        p = parse_telegram_target(123456)
        self.assertTrue(p.resolvable)
        self.assertEqual(p.kind, "user_id")
        self.assertEqual(p.value, 123456)

        p2 = parse_telegram_target("-1001234567890")
        self.assertTrue(p2.resolvable)
        self.assertEqual(p2.kind, "chat_id")
        self.assertEqual(p2.value, -1001234567890)

    def test_usernames(self):
        for raw in ("@durov", "durov", "https://t.me/durov", "t.me/durov/12"):
            p = parse_telegram_target(raw)
            self.assertTrue(p.resolvable, msg=raw)
            self.assertEqual(p.kind, "username")
            self.assertEqual(p.value, "durov")

    def test_tme_c_internal(self):
        p = parse_telegram_target("https://t.me/c/1234567890/42")
        self.assertTrue(p.resolvable)
        self.assertEqual(p.kind, "chat_id")
        self.assertEqual(p.value, -1001234567890)

    def test_tg_user_link(self):
        p = parse_telegram_target("tg://user?id=42")
        self.assertTrue(p.resolvable)
        self.assertEqual(p.value, 42)
        self.assertEqual(p.kind, "user_id")

    def test_reject_invite_hash(self):
        for raw in (
            "https://t.me/+AbCdEfGhIjK",
            "t.me/joinchat/AAAAAEexample",
            "+AbCdEfGhIjK",
        ):
            p = parse_telegram_target(raw)
            self.assertFalse(p.resolvable, msg=raw)
            self.assertEqual(p.kind, "invite")
            self.assertEqual(p.error, "invite_hash_not_allowed")

    def test_ordered_dedupe(self):
        raw = [
            "@Alice",
            "https://t.me/alice",
            "alice",
            111,
            "111",
            "@bobby",
            "https://t.me/+InviteHashXX",
            "not a target!!!",
        ]
        valid, rejected = normalize_targets(raw)
        values = [(v.kind, str(v.value).lower() if v.kind == "username" else v.value) for v in valid]
        self.assertEqual(values, [("username", "alice"), ("user_id", 111), ("username", "bobby")])
        self.assertGreaterEqual(len(rejected), 2)

    def test_preview_and_ensure(self):
        prev = preview_targets(["@a_user", "t.me/+bad", 5, 5, "@a_user"])
        self.assertEqual(prev["valid_count"], 2)
        self.assertGreaterEqual(prev["rejected_count"], 1)
        self.assertEqual(prev["invalid_count"], prev["rejected_count"])
        self.assertEqual(prev["duplicate_count"], 2)
        self.assertEqual(len(prev["valid"]), 2)
        typed = preview_targets([42, -100123], target_type="user")
        self.assertEqual(typed["valid_count"], 1)
        self.assertEqual(typed["invalid"][0]["error"], "wrong_target_type")

        with self.assertRaises(ValueError):
            ensure_sendable_targets([])
        with self.assertRaises(ValueError):
            ensure_sendable_targets(["https://t.me/+OnlyInvite"])


class DatabaseRegressionTests(unittest.TestCase):
    def setUp(self):
        Database.reset_instance()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test.db")
        self.db = Database(self.db_path)
        # seed account
        with self.db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO accounts (phone, session_name, status) VALUES (?,?,?)",
                ("+100", "s1", "active"),
            )
            self.account_id = cur.lastrowid

    def tearDown(self):
        Database.reset_instance()
        self._tmpdir.cleanup()

    def test_global_exclusion_gates_found_channels(self):
        self.assertTrue(self.db.add_found_channel("goodchan", "Good", subs=10000, can_comment=True))
        self.assertTrue(self.db.exclude_channel_globally(
            "BadChan",
            reason="structural_no_comments",
            channel_id=-100123,
            evidence={"linked_chat_id": None},
            source_module="test",
        ))
        # cannot add excluded
        self.assertFalse(self.db.add_found_channel("badchan", "Bad", subs=10000, can_comment=True))
        # direct insert then list should hide it
        with self.db.get_connection() as conn:
            conn.cursor().execute(
                "INSERT OR IGNORE INTO found_channels (channel, title, can_comment, status) "
                "VALUES ('badchan', 'Bad', 1, 'new')"
            )
        rows = self.db.get_found_channels(only_open_comments=False, limit=50)
        channels = {r["channel"] for r in rows}
        self.assertIn("goodchan", channels)
        self.assertNotIn("badchan", channels)
        self.assertTrue(self.db.is_channel_globally_excluded("badchan"))
        listed = self.db.list_global_exclusions()
        item = next(x for x in listed if x["channel"] == "badchan")
        self.assertEqual(item["channel_id"], -100123)
        self.assertEqual(item["source_module"], "test")
        self.assertEqual(item["evidence"]["linked_chat_id"], None)

    def test_structural_comments_status_excludes_globally(self):
        self.db.add_found_channel("structchan", "S", subs=5000, can_comment=True)
        # account-local style update without structural — no global exclude
        self.db.update_channel_comments_status("structchan", False, structural=False)
        self.assertFalse(self.db.is_channel_globally_excluded("structchan"))
        # structural closed → global exclude
        self.db.update_channel_comments_status("structchan", False, structural=True)
        self.assertTrue(self.db.is_channel_globally_excluded("structchan"))

    def test_parsed_users_source_filter_junction(self):
        # user 1 from source A only, user 2 from A and B, user 3 from B only
        self.assertTrue(self.db.add_parsed_user(self.account_id, 1, "u1", source_chat_id=100, source_chat_title="A"))
        self.assertTrue(self.db.add_parsed_user(self.account_id, 2, "u2", source_chat_id=100, source_chat_title="A"))
        # same user 2 also seen in B — not "new" but junction row added
        self.assertFalse(self.db.add_parsed_user(self.account_id, 2, "u2", source_chat_id=200, source_chat_title="B"))
        self.assertTrue(self.db.add_parsed_user(self.account_id, 3, "u3", source_chat_id=200, source_chat_title="B"))

        all_users = self.db.get_parsed_users(self.account_id, limit=50)
        self.assertEqual({u["user_id"] for u in all_users}, {1, 2, 3})

        from_a = self.db.get_parsed_users(self.account_id, limit=50, source_chat_id=100)
        self.assertEqual({u["user_id"] for u in from_a}, {1, 2})

        from_b = self.db.get_parsed_users(self.account_id, limit=50, source_chat_id=200)
        self.assertEqual({u["user_id"] for u in from_b}, {2, 3})

        from_c = self.db.get_parsed_users(self.account_id, limit=50, source_chat_id=999)
        self.assertEqual(from_c, [])

    def test_invite_today_count_success_only_and_get_invited(self):
        self.db.add_invite_result(self.account_id, -1001, 10, "success")
        self.db.add_invite_result(self.account_id, -1001, 11, "error", "x")
        self.db.add_invite_result(self.account_id, -1001, 12, "skipped", "privacy")
        stats = self.db.get_invite_stats(self.account_id)
        self.assertEqual(stats["today_count"], 1)
        self.assertEqual(stats["success"], 1)
        self.assertEqual(stats["errors"], 1)

        all_attempted = self.db.get_invited_user_ids(self.account_id, -1001)
        self.assertEqual(all_attempted, {10, 11, 12})
        only_ok = self.db.get_invited_user_ids(self.account_id, -1001, only_success=True)
        self.assertEqual(only_ok, {10})

    def test_campaign_lifecycle(self):
        cid = self.db.add_mass_send_campaign(
            self.account_id, "t", "hello {a|b}", target_type="dm", total_targets=3
        )
        st = self.db.get_campaign_stats(cid)
        self.assertEqual(st["status"], "running")
        self.assertEqual(st["total_targets"], 3)
        self.assertEqual(st["processed_count"], 0)

        self.db.add_mass_send_result(cid, self.account_id, "1", "user", "sent")
        self.db.add_mass_send_result(cid, self.account_id, "2", "user", "error", "x")
        self.db.update_mass_send_campaign(
            cid, status="completed", processed_count=2, finished=True
        )
        st2 = self.db.get_campaign_stats(cid)
        self.assertEqual(st2["sent"], 1)
        self.assertEqual(st2["errors"], 1)
        self.assertEqual(st2["status"], "completed")
        self.assertEqual(st2["processed_count"], 2)
        self.assertIsNotNone(st2["finished_at"])

    def test_peer_flood_cooldown_constant(self):
        self.assertEqual(Config.PEER_FLOOD_COOLDOWN_SECONDS, 43200)
        self.db.record_flood_wait(self.account_id, Config.PEER_FLOOD_COOLDOWN_SECONDS)
        secs = self.db.get_account_pause_seconds(self.account_id)
        self.assertGreater(secs, 40000)
        self.assertTrue(self.db.should_pause_account(self.account_id))


class ExistingDatabaseMigrationTests(unittest.TestCase):
    def test_old_exclusion_and_campaign_tables_are_upgraded(self):
        Database.reset_instance()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "legacy.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE channel_global_exclusions ("
                "channel TEXT PRIMARY KEY, reason TEXT, "
                "excluded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.execute(
                "CREATE TABLE mass_send_campaigns ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, "
                "name TEXT, message_template TEXT NOT NULL, media_path TEXT, "
                "media_type TEXT, target_type TEXT DEFAULT 'dm', "
                "status TEXT DEFAULT 'active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.commit()
            conn.close()

            db = Database(path)
            with db.get_connection() as upgraded:
                exclusion_cols = {
                    row[1] for row in upgraded.execute(
                        "PRAGMA table_info(channel_global_exclusions)"
                    ).fetchall()
                }
                campaign_cols = {
                    row[1] for row in upgraded.execute(
                        "PRAGMA table_info(mass_send_campaigns)"
                    ).fetchall()
                }
            self.assertTrue(
                {"channel_id", "evidence", "source_module", "updated_at"}
                <= exclusion_cols
            )
            self.assertTrue(
                {"total_targets", "processed_count", "error_message", "finished_at"}
                <= campaign_cols
            )
        Database.reset_instance()


if __name__ == "__main__":
    unittest.main()
