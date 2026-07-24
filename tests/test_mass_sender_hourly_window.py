"""
Юнит-тесты логики скользящего часового окна лимита рассылки
(MassSender._throttle_hourly_window). Telegram/сеть не задействованы,
asyncio.sleep замокан. Корутина «прокручивается» вручную, чтобы не
завязываться на внутренности asyncio.run.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

# Project root on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.mass_sender import MassSender  # noqa: E402


def _make_sender() -> MassSender:
    """Создаёт MassSender без реального клиента/БД (для тестирования хелпера)."""
    sender = MassSender.__new__(MassSender)
    sender.client = None
    sender.db = None
    sender.account_id = 1
    return sender


def _drive(coro):
    """
    Прокручивает корутину вручную, отвечая на каждый await (asyncio.sleep
    заранее замокан как обычная функция, поэтому awaits тут не возникает).
    Возвращает результат корутины.
    """
    try:
        coro.send(None)
        raise AssertionError("coroutine did not complete synchronously")
    except StopIteration as stop:
        return stop.value


class HourlyWindowTests(unittest.TestCase):
    def test_no_throttle_below_limit(self):
        """Пока успешных отправок меньше лимита — паузы нет, окно не сбрасывается."""
        sender = _make_sender()
        start = 1000.0
        with mock.patch("modules.mass_sender.time.monotonic", return_value=1010.0), \
             mock.patch("modules.mass_sender.asyncio.sleep") as sleep_mock:
            new_sent, new_start = _drive(
                sender._throttle_hourly_window(sent_in_window=3, window_start=start, hourly_limit=5)
            )
        sleep_mock.assert_not_called()
        self.assertEqual(new_sent, 3)
        self.assertEqual(new_start, start)

    def test_throttle_when_limit_reached_sleeps_and_resets(self):
        """
        При достижении лимита в середине окна — спим до конца окна чанками по 30с,
        затем окно сбрасывается (sent=0, новый window_start).
        """
        sender = _make_sender()
        start = 1000.0
        logs = []
        sender._log = lambda msg, level="info": logs.append((msg, level))

        async def _fake_sleep(_secs):
            return None

        # elapsed = 4000-1000 = 3000; remaining = 600 -> 20 чанков по 30с
        with mock.patch("modules.mass_sender.time.monotonic", side_effect=[4000.0, 5000.0]), \
             mock.patch("modules.mass_sender.asyncio.sleep", side_effect=_fake_sleep) as sleep_mock:
            new_sent, new_start = _drive(
                sender._throttle_hourly_window(sent_in_window=5, window_start=start, hourly_limit=5)
            )

        self.assertEqual(sleep_mock.call_count, 20)
        for call in sleep_mock.call_args_list:
            self.assertLessEqual(call.args[0], 30.0)
        self.assertEqual(new_sent, 0)
        self.assertEqual(new_start, 5000.0)
        self.assertTrue(any("Лимит" in m and "исчерпан" in m for m, _ in logs))

    def test_throttle_window_already_expired(self):
        """Если окно уже истекло (remaining<=0) — не спим, но окно сбрасываем."""
        sender = _make_sender()
        start = 1000.0
        sender._log = lambda *a, **k: None

        async def _fake_sleep(_secs):
            return None

        # elapsed = 5000-1000 = 4000 > 3600 -> remaining < 0
        with mock.patch("modules.mass_sender.time.monotonic", side_effect=[5000.0, 5001.0]), \
             mock.patch("modules.mass_sender.asyncio.sleep", side_effect=_fake_sleep) as sleep_mock:
            new_sent, new_start = _drive(
                sender._throttle_hourly_window(sent_in_window=5, window_start=start, hourly_limit=5)
            )
        sleep_mock.assert_not_called()
        self.assertEqual(new_sent, 0)
        self.assertEqual(new_start, 5001.0)


if __name__ == "__main__":
    unittest.main()
