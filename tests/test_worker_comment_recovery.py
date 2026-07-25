import asyncio
import types
import unittest

from telethon.errors import ChatGuestSendForbiddenError

from backend.worker import BotWorker


class FakeDB:
    def __init__(self):
        self.banned = []
        self.excluded = []
        self.comment_updates = []

    def mark_banned(self, account_id, channel):
        self.banned.append((account_id, channel))

    def exclude_channel_globally(self, channel, reason, **kwargs):
        self.excluded.append((channel, reason, kwargs))
        return True

    def update_channel_comments_status(self, channel, has_open_comments, **kwargs):
        self.comment_updates.append((channel, has_open_comments, kwargs))
        return True


class FakeClient:
    def __init__(self, linked_chat_id=777, fail_first_send=True):
        self.linked_chat_id = linked_chat_id
        self.fail_first_send = fail_first_send
        self.send_calls = 0

    async def send_message(self, **kwargs):
        self.send_calls += 1
        if self.fail_first_send and self.send_calls == 1:
            raise ChatGuestSendForbiddenError(None)
        return types.SimpleNamespace(id=123, chat_id=self.linked_chat_id)

    async def __call__(self, request):
        return types.SimpleNamespace(
            full_chat=types.SimpleNamespace(linked_chat_id=self.linked_chat_id)
        )


class WorkerCommentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_worker(self, linked_chat_id=777):
        db = FakeDB()
        worker = BotWorker.__new__(BotWorker)
        worker.account_id = 1
        worker.db = db
        worker.client = FakeClient(linked_chat_id=linked_chat_id)
        worker._send_as_cache = {}
        worker.log = lambda *args, **kwargs: None

        async def no_send_as():
            return None

        worker._resolve_send_as = no_send_as
        return worker, db

    async def test_guest_forbidden_joins_and_retries_exactly_once(self):
        worker, db = self.make_worker(linked_chat_id=777)
        join_calls = []

        async def join_once(linked_chat_id, channel_name):
            join_calls.append((linked_chat_id, channel_name))
            return True

        worker._join_discussion_group_if_needed = join_once
        result = await worker._send_comment_message(
            types.SimpleNamespace(id=55, username="testingcatalog"),
            "_",
            10,
            channel_name="testingcatalog",
        )

        self.assertEqual(result.id, 123)
        self.assertEqual(worker.client.send_calls, 2)
        self.assertEqual(join_calls, [(777, "testingcatalog")])
        self.assertEqual(db.banned, [])
        self.assertEqual(db.excluded, [])

    async def test_pending_join_is_not_banned_and_not_retried(self):
        worker, db = self.make_worker(linked_chat_id=888)

        async def pending(*_args):
            return "pending"

        worker._join_discussion_group_if_needed = pending
        result = await worker._send_comment_message(
            types.SimpleNamespace(id=56, username="pendingchan"),
            "_",
            11,
            channel_name="pendingchan",
        )

        self.assertIsNone(result)
        self.assertEqual(worker.client.send_calls, 1)
        self.assertEqual(db.banned, [])
        self.assertEqual(db.excluded, [])

    async def test_no_linked_chat_is_structurally_excluded(self):
        worker, db = self.make_worker(linked_chat_id=None)

        async def should_not_join(*_args):
            self.fail("join should not be attempted without linked_chat_id")

        worker._join_discussion_group_if_needed = should_not_join
        result = await worker._send_comment_message(
            types.SimpleNamespace(id=57, username="closedchan"),
            "_",
            12,
            channel_name="closedchan",
        )

        self.assertIsNone(result)
        self.assertEqual(worker.client.send_calls, 1)
        self.assertEqual(db.banned, [])
        self.assertTrue(db.excluded)
        self.assertEqual(db.excluded[0][0], "closedchan")


if __name__ == "__main__":
    unittest.main()
