"""
JunkChatClassifier — AI-эвристический классификатор мусорных чатов.

Идея: бот может вступать (или его добавляют) в множество чатов. Часть из них —
обычные тематические сообщества, часть — мусорные «дроп-чаты», NSFW-помойки,
крипто-скам, спам-ботнеты и т.п. Этот модуль решает: выйти ли из чата.

Используется в worker._maybe_leave_junk_chats(), вызываемом раз в N циклов.

Стратегия:
1. Быстрая офлайн-эвристика по названию/описанию/количеству участников.
2. Если эвристика неоднозначна — спрашиваем у LLM (роутер AI/Gemini, тот же
   эндпойнт, что и для генерации комментариев). Лимитируем расход токенов:
   максимум 10 LLM-проверок за один проход.

Возвращаем не только bool, но и причину — это идёт в лог.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Optional, Tuple, List

from utils.async_http import AsyncHTTPClient
from config import Config


# Жёсткий blacklist — если хоть одно слово найдено в названии/описании,
# чат считается мусорным без обращения к LLM.
HARD_JUNK_KEYWORDS = [
    # русский крипта-скам
    "криптоарбитраж", "арбитраж крипто", "p2p заработок", "арбитраж денег",
    "доход от 100$", "доход без вложен", "пассивный доход",
    "инвестиции крипт", "сигналы крипт", "крипто памп",
    # инвайты в дроп-схемы
    "drop", "дроп", "обнал", "обнал.", "карты дроп", "теневой", "грязн",
    # NSFW / 18+ flooding
    "18+ slivы", "сливы 18", "приваты сливы", "интим знак", "слив 18",
    "anal", "porn", "порн", "sex", "слив бывших",
    # казино / bookies
    "казино", "casino", "ставки спорт", "1xbet", "stake.com",
    # рассылки / самобан
    "массовая рассылка", "рассылка телега", "автоинвайт",
    # ботнет / pump-чаты
    "memepump", "rugpull", "shitcoin", "shitcoins",
]

# «Мягкие» сигналы — не приговор, но триггерят LLM-проверку
SOFT_JUNK_SIGNALS = [
    "💰", "🤑", "💸", "💎",  # денежные эмодзи в названии
    "$$$", "💵", "🎰", "🔞",
    "join now", "приглашаем", "добавьтесь",
]


@dataclass
class JunkVerdict:
    is_junk: bool
    reason: str
    confidence: float  # 0..1
    used_llm: bool = False


class JunkChatClassifier:
    """
    Классификатор мусорных чатов.

    Использует комбинацию:
    - blacklist по названию/описанию;
    - LLM-classifier (тот же AI-провайдер, что и для комментариев).
    """

    def __init__(self, db=None, http_client: Optional[AsyncHTTPClient] = None):
        self.db = db
        self._http = http_client
        self._llm_calls_remaining = 10  # лимит LLM-вызовов на проход (сбрасывается извне)

    def reset_llm_budget(self, max_calls: int = 10):
        """Сбрасывает лимит LLM-вызовов (вызывается воркером в начале прохода)."""
        self._llm_calls_remaining = max_calls

    # ----------- offline heuristic ----------------------------------------

    @staticmethod
    def _norm(s: Optional[str]) -> str:
        return (s or "").lower().strip()

    def heuristic_check(
        self,
        title: str = "",
        about: str = "",
        members_count: int = 0,
        username: str = "",
    ) -> Optional[JunkVerdict]:
        """
        Быстрая эвристика без LLM. Возвращает JunkVerdict, если есть однозначный
        ответ, иначе None — нужно идти в LLM.
        """
        t = self._norm(title)
        a = self._norm(about)
        u = self._norm(username)
        haystack = f"{t}\n{a}\n{u}"

        # 1. Жёсткий blacklist — мусор
        for kw in HARD_JUNK_KEYWORDS:
            if kw in haystack:
                return JunkVerdict(
                    is_junk=True,
                    reason=f"hard-blacklist: '{kw}' в названии/описании",
                    confidence=0.95,
                )

        # 2. «Мёртвый» чат — < 5 участников и нет описания
        if 0 < members_count < 5 and not a:
            return JunkVerdict(
                is_junk=True,
                reason=f"мёртвый чат ({members_count} участников, без описания)",
                confidence=0.7,
            )

        # 3. Слишком много денежных триггеров — нужна LLM-проверка
        soft_hits = sum(1 for s in SOFT_JUNK_SIGNALS if s in haystack)
        if soft_hits >= 3:
            return None  # → LLM

        # 4. Если ничего подозрительного — точно НЕ мусор
        if soft_hits == 0:
            return JunkVerdict(
                is_junk=False,
                reason="чистая эвристика (нет триггеров)",
                confidence=0.85,
            )

        # 5. 1-2 сигнала — пограничный случай → LLM
        return None

    # ----------- LLM-classifier --------------------------------------------

    def _get_api_key(self) -> str:
        if self.db:
            return (self.db.get_setting("gemini_api_key", Config.GEMINI_API_KEY)
                    or Config.GEMINI_API_KEY)
        return Config.GEMINI_API_KEY

    def _get_model(self) -> str:
        if self.db:
            return (self.db.get_setting("gemini_model", Config.GEMINI_MODEL)
                    or Config.GEMINI_MODEL)
        return Config.GEMINI_MODEL

    def _get_endpoint(self) -> str:
        return f"{Config.GEMINI_BASE_URL.rstrip('/')}/chat/completions"

    async def _llm_check(self, title: str, about: str, members_count: int) -> JunkVerdict:
        """Спрашивает у LLM, мусорный ли чат."""
        if self._http is None:
            self._http = AsyncHTTPClient(max_retries=2, base_delay=1.0, timeout=15.0)

        system_prompt = (
            "Ты модератор Telegram-аккаунта. Получаешь карточку чата (название, "
            "описание, число участников). Твоя задача — решить, является ли это "
            "мусорным/спамным/скам-чатом, в котором не стоит оставаться. "
            "Мусором считай: P2P-арбитраж/крипто-сигналы/инвестиционный скам, "
            "дроп-схемы, обнал, казино/ставки, NSFW-сливы, спам-рассылки, "
            "пирамиды, мёртвые чаты с 1-2 участниками, чисто рекламные стены.\n"
            "Тематические сообщества (аниме, IT, новости, мемы, игры, музыка, "
            "обычные региональные чаты) — НЕ мусор.\n"
            "Ответь ТОЛЬКО валидным JSON формата: "
            '{"is_junk": true|false, "reason": "коротко, до 60 символов", "confidence": 0.0-1.0}'
        )
        user_prompt = (
            f"Название: {title or '(нет)'}\n"
            f"Описание: {about[:500] if about else '(нет)'}\n"
            f"Участников: {members_count}"
        )

        payload = {
            "model": self._get_model(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 80,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self._get_api_key()}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http.post(self._get_endpoint(), json_data=payload, headers=headers)
            if not resp.success:
                return JunkVerdict(False, f"LLM error {resp.status}", 0.0, used_llm=True)

            content = (resp.data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "") or "")
            content = content.strip()
            # Изредка модель оборачивает в ```json ... ```
            if content.startswith("```"):
                content = content.strip("`")
                if content.lower().startswith("json"):
                    content = content[4:].strip()
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                # Эвристический парсинг fallback
                is_junk = "true" in content.lower() and "is_junk" in content.lower()
                return JunkVerdict(is_junk, "LLM ответил невалидным JSON", 0.4, used_llm=True)

            return JunkVerdict(
                is_junk=bool(data.get("is_junk", False)),
                reason=str(data.get("reason", "no-reason"))[:80],
                confidence=float(data.get("confidence", 0.5)),
                used_llm=True,
            )
        except Exception as e:
            return JunkVerdict(False, f"LLM exception: {e}", 0.0, used_llm=True)

    # ----------- public API ------------------------------------------------

    async def classify(
        self,
        title: str = "",
        about: str = "",
        members_count: int = 0,
        username: str = "",
    ) -> JunkVerdict:
        """Главный метод: эвристика + (опционально) LLM."""
        verdict = self.heuristic_check(title, about, members_count, username)
        if verdict is not None:
            return verdict
        # пограничный случай — спрашиваем LLM, если бюджет не исчерпан
        if self._llm_calls_remaining <= 0:
            return JunkVerdict(False, "LLM-бюджет исчерпан, не мусор по умолчанию", 0.3)
        self._llm_calls_remaining -= 1
        return await self._llm_check(title, about, members_count)

    async def close(self):
        if self._http:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None
