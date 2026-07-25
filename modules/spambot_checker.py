"""
SpamBotChecker — взаимодействие с официальным @SpamBot при PeerFloodError.

Когда Telethon выбрасывает PeerFloodError, это означает, что Telegram временно
блокирует исходящие сообщения с аккаунта (анти-спам). У Telegram есть
официальный бот @SpamBot (a.k.a @SpambotBot), которому можно написать /start
и узнать статус: «спам-блок активен» / «всё в порядке» / «бот сдвинул блок».

Этот модуль:
1. Открывает диалог с @SpamBot и отправляет /start.
2. Парсит ответ (RU/EN), определяет статус.
3. При наличии inline-кнопки «Just to be safe...» / «It's a mistake» — нажимает
   её (это часто снимает мягкий блок).
4. Возвращает структурированный результат: status, message, will_unblock_at.

Используется воркером в обработчике PeerFloodError, если включён тоггл
`spambot_unblock`.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import KeyboardButtonCallback
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest


SPAMBOT_USERNAMES = ["SpamBot", "spambot"]  # запасные варианты


@dataclass
class SpamBotResult:
    status: str  # "ok" | "limited" | "blocked" | "unblocked" | "error" | "unknown"
    message: str
    raw_response: str = ""
    will_unblock_at: Optional[datetime] = None
    pressed_button: bool = False


# Паттерны ответов SpamBot (русский + английский)
PATTERNS_OK = [
    "no limits", "are no limits", "ограничений нет", "нет ограничений",
    "your account is free", "ваш аккаунт свободен",
]
PATTERNS_LIMITED = [
    "limited until", "ограничен до", "limited in your account",
    "вы временно ограничены",
]
PATTERNS_BLOCKED = [
    "you're banned", "your account has been blocked",
    "ваш аккаунт заблокирован", "the spam ban",
]
PATTERNS_UNBLOCKED = [
    "good news", "хорошие новости", "the limits have been removed",
    "ограничения сняты", "we've lifted",
]


class SpamBotChecker:
    """Класс взаимодействия с @SpamBot (мини state-machine)."""

    def __init__(self, client: TelegramClient):
        self.client = client

    async def _find_spambot(self):
        """Пытается найти @SpamBot среди возможных username."""
        last_err = None
        for uname in SPAMBOT_USERNAMES:
            try:
                ent = await self.client.get_entity(uname)
                return ent
            except Exception as e:
                last_err = e
        raise RuntimeError(f"@SpamBot не найден: {last_err}")

    @staticmethod
    def _parse_status(text: str) -> str:
        t = (text or "").lower()
        for p in PATTERNS_UNBLOCKED:
            if p in t:
                return "unblocked"
        for p in PATTERNS_OK:
            if p in t:
                return "ok"
        for p in PATTERNS_BLOCKED:
            if p in t:
                return "blocked"
        for p in PATTERNS_LIMITED:
            if p in t:
                return "limited"
        return "unknown"

    @staticmethod
    def _parse_unblock_time(text: str) -> Optional[datetime]:
        """
        Пытается выдрать дату снятия лимита из текста ответа.
        Примеры:
        - "Your account will be unrestricted on 14 May 2026, 18:00 UTC"
        - "Ограничение снимется 14 мая 2026 в 18:00 UTC"
        """
        t = text or ""
        m = re.search(
            r"(\d{1,2})\s+(\w+)\s+(\d{4})\D+(\d{1,2}):(\d{2})",
            t,
        )
        if not m:
            return None
        try:
            day = int(m.group(1))
            month_str = m.group(2).lower()[:3]
            year = int(m.group(3))
            hour = int(m.group(4))
            minute = int(m.group(5))
            months = {
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
                "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5,
                "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11,
                "дек": 12,
            }
            month = months.get(month_str, None)
            if month is None:
                return None
            return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except Exception:
            return None

    async def check(self, press_buttons: bool = True, wait_seconds: float = 3.0) -> SpamBotResult:
        """
        Отправляет /start в @SpamBot, дожидается ответа, парсит.

        :param press_buttons: жать ли кнопки «Просто на всякий случай / Это ошибка»
        :param wait_seconds: сколько ждать ответ от бота
        """
        try:
            bot = await self._find_spambot()
        except Exception as e:
            return SpamBotResult("error", f"@SpamBot не найден: {e}")

        # Отправляем /start (или просто "/")
        try:
            await self.client.send_message(bot, "/start")
        except Exception as e:
            return SpamBotResult("error", f"Не удалось написать @SpamBot: {e}")

        # Ждём ответа
        await asyncio.sleep(wait_seconds)

        # Берём последние сообщения от бота
        try:
            messages = await self.client.get_messages(bot, limit=3)
        except Exception as e:
            return SpamBotResult("error", f"Не удалось получить ответ: {e}")

        # Ищем самое свежее НЕ исходящее сообщение
        bot_msg = None
        for m in messages:
            if not m.out and m.text:
                bot_msg = m
                break

        if bot_msg is None:
            return SpamBotResult("unknown", "Бот не ответил")

        text = bot_msg.text or bot_msg.message or ""
        status = self._parse_status(text)
        unblock_at = self._parse_unblock_time(text) if status in ("limited", "blocked") else None

        # Жмём первую попавшуюся кнопку, если бот предлагает её для снятия блока.
        pressed = False
        if press_buttons and bot_msg.reply_markup:
            try:
                for row in bot_msg.reply_markup.rows:
                    for btn in row.buttons:
                        if not isinstance(btn, KeyboardButtonCallback):
                            continue
                        btn_text = (btn.text or "").lower()
                        # Жмём кнопки уведомления / снятия блока
                        if any(kw in btn_text for kw in [
                            "no", "yes", "ok", "mistake", "ошибка", "никог", "never",
                            "i won", "не буду",
                        ]):
                            await self.client(GetBotCallbackAnswerRequest(
                                peer=bot, msg_id=bot_msg.id, data=btn.data
                            ))
                            pressed = True
                            await asyncio.sleep(2.0)
                            # Перечитываем последнее сообщение, бот мог обновить статус
                            try:
                                follow_up = await self.client.get_messages(bot, limit=1)
                                if follow_up:
                                    follow_text = follow_up[0].text or ""
                                    new_status = self._parse_status(follow_text)
                                    if new_status != "unknown":
                                        status = new_status
                                        text = follow_text
                            except Exception:
                                pass
                            break
                    if pressed:
                        break
            except Exception:
                pass

        # Финальный мессадж
        short_msg = text.strip().replace("\n", " ")
        if len(short_msg) > 200:
            short_msg = short_msg[:200] + "…"

        return SpamBotResult(
            status=status,
            message=short_msg,
            raw_response=text,
            will_unblock_at=unblock_at,
            pressed_button=pressed,
        )
