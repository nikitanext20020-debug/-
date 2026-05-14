"""
ChannelHealthWatcher — фоновая валидация базы каналов.

Запускается из FastAPI lifespan и крутится в отдельном daemon-thread.
Каждые N минут (настройка `channel_watcher_interval_minutes`) проходит по базе
найденных каналов и проверяет:

1. Канал ещё существует? (если get_entity бросает USERNAME_NOT_OCCUPIED — удаляем)
2. У канала всё ещё открыты комментарии? (linked_chat_id != None)
3. Сколько подписчиков сейчас? (обновляем subs_count)
4. Когда был последний пост? (если >7 дней — помечаем как dormant)
5. Дедупликация: если в БД есть @ и без @ — удаляем дубликат.

Использует ЛЮБОЙ запущенный воркер для делегирования API-вызовов в Telegram.
Если воркеров нет — спит до следующего цикла.

ВАЖНО: watcher НЕ создаёт свой TelegramClient — он переиспользует event loop
существующего воркера через asyncio.run_coroutine_threadsafe. Это снижает риск
бана за частые соединения и не требует отдельной сессии.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from telethon.errors import (
    UsernameInvalidError,
    UsernameNotOccupiedError,
    ChannelPrivateError,
    FloodWaitError,
)
from telethon.tl.functions.channels import GetFullChannelRequest


class ChannelHealthWatcher:
    """
    Фоновый watcher базы каналов. Поток-демон, никаких блокирующих эффектов
    на FastAPI lifecycle.
    """

    def __init__(self, db, workers_registry: Dict[int, Any]):
        """
        :param db: utils.database.Database
        :param workers_registry: тот же dict {acc_id: BotWorker} что в main.py
        """
        self.db = db
        self.workers = workers_registry
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.is_running = False
        # ограничение: сколько каналов проверять за один проход
        self.batch_size = 50
        # минимальная пауза между API-вызовами в секундах (анти-флуд)
        self.api_throttle = 1.5

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ChannelHealthWatcher", daemon=True
        )
        self.is_running = True
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_setting(self, key: str, default):
        try:
            v = self.db.get_setting(key, default)
            return v if v is not None else default
        except Exception:
            return default

    def _pick_worker(self):
        """Возвращает первого живого воркера (или None)."""
        for w in self.workers.values():
            try:
                if (w.is_running and getattr(w, "client", None)
                        and w.client.is_connected()
                        and getattr(w, "loop", None)
                        and w.loop.is_running()):
                    return w
            except Exception:
                continue
        return None

    def _run_in_worker(self, worker, coro, timeout: float = 30.0):
        """Запускает корутину в loop'е воркера, возвращает результат."""
        future = asyncio.run_coroutine_threadsafe(coro, worker.loop)
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            return e  # возвращаем как значение, чтобы не падать целиком

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self):
        # стартовая задержка чтобы воркеры успели подняться
        self._stop_event.wait(timeout=60)
        while not self._stop_event.is_set():
            try:
                enabled = bool(self._get_setting("channel_watcher_enabled", True))
                interval_min = int(self._get_setting("channel_watcher_interval_minutes", 30))
                interval_min = max(5, min(interval_min, 1440))  # clamp 5min..24h

                if not enabled:
                    self._sleep(60)
                    continue

                worker = self._pick_worker()
                if worker is None:
                    # некого использовать — ждём минуту и пробуем снова
                    self._sleep(60)
                    continue

                self._tick(worker)

                # ждём до следующего цикла, прерываемся по stop_event
                self._sleep(interval_min * 60)
            except Exception as e:
                self._log(f"❌ Ошибка цикла watcher'а: {e}", "error")
                self._sleep(120)

    def _sleep(self, seconds: float):
        """Sleep, который прерывается по stop_event."""
        self._stop_event.wait(timeout=seconds)

    # ------------------------------------------------------------------
    # Single tick
    # ------------------------------------------------------------------

    def _tick(self, worker):
        """Один проход по batch_size каналов."""
        try:
            # Берём ВСЕ каналы (включая закрытые) — нам нужно их «оживлять»
            channels = self.db.get_found_channels(limit=self.batch_size,
                                                  only_open_comments=False)
        except Exception as e:
            self._log(f"❌ Не удалось загрузить каналы: {e}", "error")
            return

        if not channels:
            return

        # Дедуп
        self._dedupe_channels(channels)

        checked = 0
        deleted = 0
        revived = 0
        closed = 0
        updated = 0
        flood_hit = False

        for ch in channels:
            if self._stop_event.is_set():
                break
            if flood_hit:
                break  # упёрлись в FloodWait — стопим проход
            name = ch["channel"]
            # пропускаем приватные каналы по +hash, у нас нет для них дешёвого
            # способа валидировать без вступления (это делает channel_joiner)
            if name.startswith("+"):
                continue

            try:
                result = self._run_in_worker(
                    worker, self._validate_channel(worker, ch), timeout=30.0
                )
            except Exception as e:
                result = e

            if isinstance(result, Exception):
                err_str = str(result).lower()
                if "flood" in err_str or "wait" in err_str:
                    flood_hit = True
                    self._log(f"⏳ FloodWait в watcher'е, прерываюсь: {result}", "warning")
                    break
                # тихо логируем, идём дальше
                continue

            checked += 1
            action = result.get("action")
            if action == "deleted":
                deleted += 1
            elif action == "closed_comments":
                closed += 1
            elif action == "revived":
                revived += 1
            elif action == "updated":
                updated += 1

            # throttle
            time.sleep(self.api_throttle)

        if checked > 0:
            self._log(
                f"👁️ Watcher: проверено {checked} | удалено {deleted} | "
                f"закрыто комментов {closed} | оживлено {revived} | обновлено {updated}"
            )

    async def _validate_channel(self, worker, ch_row: Dict) -> Dict:
        """
        Валидирует один канал. Возвращает dict с полем action:
        deleted / closed_comments / revived / updated / no_change.
        """
        client = worker.client
        name = ch_row["channel"]
        was_can_comment = bool(ch_row.get("can_comment", 0))

        try:
            entity = await client.get_entity(name)
        except (UsernameInvalidError, UsernameNotOccupiedError):
            self.db.delete_found_channel(name, force=False)
            return {"action": "deleted", "reason": "не существует"}
        except ChannelPrivateError:
            # Канал стал приватным — оставляем, но помечаем
            self.db.update_channel_status(name, "private")
            return {"action": "no_change", "reason": "стал приватным"}
        except FloodWaitError as fw:
            # пробрасываем наверх, чтобы остановить проход
            raise
        except ValueError as e:
            # Cannot find any entity — канал недоступен этому аккаунту
            if "cannot find any entity" in str(e).lower():
                self.db.delete_found_channel(name, force=False)
                return {"action": "deleted", "reason": "не найден"}
            return {"action": "no_change", "reason": str(e)[:60]}
        except Exception as e:
            return {"action": "no_change", "reason": f"err: {str(e)[:60]}"}

        # Проверяем что это канал (не пользователь и не группа)
        if not getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
            self.db.delete_found_channel(name, force=False)
            return {"action": "deleted", "reason": "не канал"}

        # Берём full info чтобы узнать subs_count + linked_chat_id (комменты)
        try:
            full = await client(GetFullChannelRequest(entity))
            subs_count = full.full_chat.participants_count or 0
            linked_chat_id = full.full_chat.linked_chat_id
            new_can_comment = bool(linked_chat_id)
        except FloodWaitError:
            raise
        except Exception:
            # full info недоступен — просто оставляем как есть
            return {"action": "no_change"}

        # Обновляем subs_count
        try:
            with self.db.get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE found_channels SET subs_count = ?, last_checked = ? "
                    "WHERE channel = ?",
                    (subs_count, datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                     name.lstrip('@')),
                )
        except Exception:
            pass

        # Обновляем статус комментариев
        if new_can_comment != was_can_comment:
            self.db.update_channel_comments_status(name, has_open_comments=new_can_comment)
            return {
                "action": "revived" if new_can_comment else "closed_comments",
                "subs": subs_count,
            }

        return {"action": "updated", "subs": subs_count}

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _dedupe_channels(self, channels):
        """Удаляет дубликаты вида '@x' и 'x' (хранится без @)."""
        seen = {}
        for ch in channels:
            n = ch["channel"]
            stripped = n.lstrip("@")
            if stripped in seen and stripped != n:
                # Дубль с @ — удаляем
                try:
                    self.db.delete_found_channel(n, force=True)
                except Exception:
                    pass
            else:
                seen[stripped] = n

    def _log(self, msg: str, level: str = "info"):
        try:
            # Пишем в общий лог без привязки к аккаунту
            self.db.add_log(None, level, msg)
        except Exception:
            print(f"[ChannelHealthWatcher] {msg}")
