from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.tl.types import Channel, Chat
from telethon.utils import get_peer_id

from modules.keyword_search import KeywordSearch
from utils.database import Database


class _SearchClient:
    def __init__(self, chats):
        self.chats = chats
        self.queries = []

    def is_connected(self):
        return True

    async def __call__(self, request):
        self.queries.append(request.q)
        return SimpleNamespace(chats=self.chats)


class KeywordGroupSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Database.reset_instance()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmpdir.name, "test.db"))

    async def asyncTearDown(self):
        Database.reset_instance()
        self._tmpdir.cleanup()

    async def test_search_persists_groups_and_ignores_broadcast_channels(self):
        basic_group = Chat(
            id=101,
            title="Basic group",
            photo=None,
            participants_count=15,
            date=datetime.now(timezone.utc),
            version=1,
        )
        supergroup = Channel(
            id=202,
            title="Supergroup",
            photo=None,
            date=datetime.now(timezone.utc),
            broadcast=False,
            megagroup=True,
            participants_count=200,
        )
        broadcast = Channel(
            id=303,
            title="Broadcast",
            photo=None,
            date=datetime.now(timezone.utc),
            broadcast=True,
            megagroup=False,
            participants_count=300,
        )
        client = _SearchClient([basic_group, supergroup, broadcast])
        search = KeywordSearch(client, self.db)

        found = await search.search_groups_by_keywords(
            [" anime ", "ANIME", "manga"]
        )

        self.assertEqual(client.queries, ["anime", "manga"])
        self.assertEqual(
            {item["chat"] for item in found},
            {abs(get_peer_id(basic_group)), abs(get_peer_id(supergroup))},
        )
        self.assertEqual({item["title"] for item in found}, {
            "Basic group",
            "Supergroup",
        })
        stored = self.db.get_found_chats()
        self.assertEqual(
            {item["chat"] for item in stored},
            {abs(get_peer_id(basic_group)), abs(get_peer_id(supergroup))},
        )


if __name__ == "__main__":
    unittest.main()
