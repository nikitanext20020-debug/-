"""
Модуль массовой рассылки сообщений.
Отправка в личные сообщения и группы с поддержкой спинтакса.

Безопасность:
- явный список целей (никаких «всех диалогов»)
- resolve каждого target в момент отправки
- один retry той же цели после разумного FloodWait
- hard-stop на PeerFlood + persist cooldown через Database.record_flood_wait
- lifecycle кампании (status / processed / finished_at)
- лимиты только clamp вниз до Config caps
"""
import asyncio
import random
import re
import os
import time
from typing import Dict, List, Optional, Tuple, Union

from telethon import TelegramClient
from telethon.tl.types import User, Chat, Channel
from telethon.errors import (
    FloodWaitError, PeerFloodError, ChatWriteForbiddenError,
    ChannelPrivateError
)

try:
    from telethon.errors import UserIsBlockedError
except ImportError:
    UserIsBlockedError = None

try:
    from telethon.errors import InputUserDeactivatedError
except ImportError:
    InputUserDeactivatedError = None

from config import Config
from utils.database import Database
from utils.telegram_targets import (
    ParsedTarget,
    ensure_sendable_targets,
    parse_telegram_target,
    target_entity_ref,
)


class MassSender:
    """Менеджер массовой рассылки сообщений"""

    def __init__(self, client: TelegramClient, db: Database, account_id: int):
        self.client = client
        self.db = db
        self.account_id = account_id

    def _log(self, message: str, level: str = "info"):
        """Логирует сообщение"""
        if self.db and self.account_id:
            try:
                self.db.add_log(self.account_id, level, message)
            except Exception:
                pass
        print(f"[MassSender] {message}")

    def _process_spintax(self, text: str) -> str:
        """Обрабатывает спинтакс паттерны вида {word1|word2|word3}."""
        def _replace(match):
            options = match.group(1).split('|')
            return random.choice(options)

        return re.sub(r'\{([^{}]+)\}', _replace, text)

    def _clamp_hourly_limit(self, hourly_limit: Optional[int]) -> int:
        cap = int(getattr(Config, "MASS_SEND_HOURLY_LIMIT", 20) or 20)
        if hourly_limit is None:
            return cap
        try:
            val = int(hourly_limit)
        except (TypeError, ValueError):
            return cap
        if val <= 0:
            return cap
        return min(val, cap)

    def _prepare_targets(self, raw_list: list) -> List[ParsedTarget]:
        return ensure_sendable_targets(raw_list)

    async def _resolve_target(self, parsed: ParsedTarget):
        """Resolve target at send-time via Telethon."""
        ref = target_entity_ref(parsed)
        return await self.client.get_entity(ref)

    async def send_to_user(self, user_id_or_username, text: str, media_path: str = None, media_type: str = None) -> Tuple[bool, Optional[str]]:
        """
        Отправляет сообщение пользователю в ЛС.

        Returns:
            (success, error) - кортеж с результатом
        """
        try:
            entity = user_id_or_username
            if isinstance(user_id_or_username, ParsedTarget):
                entity = await self._resolve_target(user_id_or_username)
            elif isinstance(user_id_or_username, (str, int)):
                parsed = parse_telegram_target(user_id_or_username)
                if not parsed.resolvable:
                    return (False, parsed.error or "invalid_target")
                entity = await self._resolve_target(parsed)

            if not isinstance(entity, User):
                return (False, "wrong_target_type:not_user")

            async with self.client.action(entity, 'typing'):
                await asyncio.sleep(random.uniform(2, 5))

            if media_path and os.path.exists(media_path):
                await self.client.send_file(entity, media_path, caption=text)
            else:
                await self.client.send_message(entity, text)

            return (True, None)

        except FloodWaitError as e:
            return (False, f"flood_wait:{e.seconds}")
        except PeerFloodError:
            return (False, "peer_flood")
        except Exception as e:
            if UserIsBlockedError and isinstance(e, UserIsBlockedError):
                return (False, "user_blocked")
            if InputUserDeactivatedError and isinstance(e, InputUserDeactivatedError):
                return (False, "user_deactivated")
            err_str = str(e).lower()
            if "blocked" in err_str:
                return (False, "user_blocked")
            if "deactivated" in err_str:
                return (False, "user_deactivated")
            return (False, str(e))

    async def send_to_group(self, chat_id, text: str, media_path: str = None, media_type: str = None) -> Tuple[bool, Optional[str]]:
        """Отправляет сообщение в группу/чат."""
        try:
            entity = chat_id
            if isinstance(chat_id, ParsedTarget):
                entity = await self._resolve_target(chat_id)
            elif isinstance(chat_id, (str, int)):
                parsed = parse_telegram_target(chat_id)
                if not parsed.resolvable:
                    return (False, parsed.error or "invalid_target")
                entity = await self._resolve_target(parsed)

            if not isinstance(entity, (Chat, Channel)):
                return (False, "wrong_target_type:not_group")

            if media_path and os.path.exists(media_path):
                await self.client.send_file(entity, media_path, caption=text)
            else:
                await self.client.send_message(entity, text)

            return (True, None)

        except ChatWriteForbiddenError:
            return (False, "write_forbidden")
        except FloodWaitError as e:
            return (False, f"flood_wait:{e.seconds}")
        except PeerFloodError:
            return (False, "peer_flood")
        except ChannelPrivateError:
            return (False, "channel_private")
        except Exception as e:
            return (False, str(e))

    def _handle_peer_flood(self, campaign_id: int, target_label: str, target_type: str) -> None:
        cooldown = int(getattr(Config, "PEER_FLOOD_COOLDOWN_SECONDS", 43200) or 43200)
        self._log(
            f"PeerFloodError: hard-stop кампании, cooldown {cooldown}s",
            "error",
        )
        try:
            self.db.record_flood_wait(self.account_id, cooldown)
        except Exception as e:
            self._log(f"Не удалось записать flood wait: {e}", "error")
        try:
            self.db.add_mass_send_result(
                campaign_id, self.account_id, str(target_label), target_type, "error", "peer_flood"
            )
        except Exception:
            pass
        try:
            self.db.update_mass_send_campaign(
                campaign_id,
                status="peer_flood",
                error_message="peer_flood",
                finished=True,
            )
        except Exception:
            pass

    async def _send_with_flood_retry(self, send_coro_factory, max_flood_retries: int = 1):
        """
        Call send once; on FloodWait wait and retry the SAME target once
        (reasonable waits only — > 1h aborts without raising limits).
        Returns (success, error, flood_waited_seconds).
        """
        success, error = await send_coro_factory()
        if success:
            return True, None, 0
        if error and error.startswith("flood_wait:"):
            try:
                seconds = int(error.split(":", 1)[1])
            except (IndexError, ValueError):
                seconds = 60
            if seconds > 3600:
                self._log(f"FloodWait слишком большой ({seconds}s), пропускаем цель", "warning")
                return False, error, seconds
            self._log(f"FloodWait: ожидание {seconds}s, один retry", "warning")
            await asyncio.sleep(seconds + 5)
            if max_flood_retries > 0:
                success2, error2 = await send_coro_factory()
                return success2, error2, seconds
            return False, error, seconds
        return False, error, 0

    # Длительность скользящего окна лимита (1 час)
    HOURLY_WINDOW_SECONDS = 3600

    async def _throttle_hourly_window(self, sent_in_window: int, window_start: float,
                                      hourly_limit: int) -> Tuple[int, float]:
        """
        Соблюдает лимит успешных отправок в скользящем часовом окне.

        Вызывается ПЕРЕД каждой отправкой. Если в текущем окне уже сделано
        >= hourly_limit успешных отправок — досыпает до конца окна (чанками по
        30 сек, чтобы asyncio.CancelledError прилетал быстро) и сбрасывает окно.

        Возвращает актуальные (sent_in_window, window_start).
        Тестируется без Telegram; asyncio.sleep мокается.
        """
        if hourly_limit <= 0 or sent_in_window < hourly_limit:
            return sent_in_window, window_start

        now = time.monotonic()
        elapsed = now - window_start
        remaining = self.HOURLY_WINDOW_SECONDS - elapsed

        if remaining > 0:
            minutes = max(1, int(round(remaining / 60)))
            self._log(
                f"Лимит {hourly_limit}/час исчерпан, пауза {minutes} мин до следующего окна",
                "info",
            )
            # спим чанками по 30 сек до конца окна
            slept = 0.0
            while slept < remaining:
                chunk = min(30.0, remaining - slept)
                await asyncio.sleep(chunk)
                slept += chunk

        # сброс окна
        return 0, time.monotonic()

    async def run_dm_campaign(self, campaign_id: int, user_list: list, message_template: str,
                              media_path: str = None, hourly_limit: int = None) -> Dict:
        """Запускает кампанию рассылки в ЛС по явному списку целей."""
        hourly_limit = self._clamp_hourly_limit(hourly_limit)

        try:
            targets = self._prepare_targets(user_list)
        except ValueError as e:
            self._log(f"DM кампания: невалидные цели ({e})", "error")
            try:
                self.db.update_mass_send_campaign(
                    campaign_id, status="failed", error_message=str(e), finished=True
                )
            except Exception:
                pass
            return {'sent': 0, 'blocked': 0, 'errors': 0, 'total': 0, 'status': 'failed'}

        try:
            self.db.update_mass_send_campaign(
                campaign_id, status="running", total_targets=len(targets), processed_count=0
            )
        except Exception:
            pass

        stats = {'sent': 0, 'blocked': 0, 'errors': 0, 'total': 0, 'status': 'running'}
        consecutive_errors = 0
        processed = 0
        # Скользящее часовое окно лимита успешных отправок
        window_start = time.monotonic()
        sent_in_window = 0

        try:
            for parsed in targets:
                label = parsed.original or str(parsed.value)
                stats['total'] += 1
                text = self._process_spintax(message_template)

                # Соблюдаем темп: не более hourly_limit успешных отправок в час
                sent_in_window, window_start = await self._throttle_hourly_window(
                    sent_in_window, window_start, hourly_limit
                )

                async def _do_send(p=parsed, t=text):
                    return await self.send_to_user(p, t, media_path)

                success, error, _fw = await self._send_with_flood_retry(_do_send, max_flood_retries=1)
                processed += 1

                if success:
                    stats['sent'] += 1
                    sent_in_window += 1
                    consecutive_errors = 0
                    self.db.add_mass_send_result(campaign_id, self.account_id, label, 'user', 'sent')
                    self._log(f"Отправлено пользователю {label}")

                elif error and ("blocked" in error or "deactivated" in error):
                    stats['blocked'] += 1
                    consecutive_errors = 0
                    self.db.add_mass_send_result(
                        campaign_id, self.account_id, label, 'user', 'blocked', error
                    )
                    self._log(f"Пользователь {label} заблокирован/деактивирован", "warning")

                elif error and "peer_flood" in error:
                    stats['errors'] += 1
                    self._handle_peer_flood(campaign_id, label, 'user')
                    stats['status'] = 'peer_flood'
                    try:
                        self.db.update_mass_send_campaign(campaign_id, processed_count=processed)
                    except Exception:
                        pass
                    return stats

                elif error and error.startswith("flood_wait:"):
                    stats['errors'] += 1
                    consecutive_errors += 1
                    self.db.add_mass_send_result(
                        campaign_id, self.account_id, label, 'user', 'error', error
                    )
                else:
                    stats['errors'] += 1
                    consecutive_errors += 1
                    self.db.add_mass_send_result(
                        campaign_id, self.account_id, label, 'user', 'error', error
                    )
                    self._log(f"Ошибка отправки пользователю {label}: {error}", "error")

                # Периодическое обновление прогресса (~каждые 10 обработанных)
                if processed % 10 == 0:
                    try:
                        self.db.update_mass_send_campaign(campaign_id, processed_count=processed)
                    except Exception:
                        pass

                if consecutive_errors >= Config.MASS_SEND_ERROR_THRESHOLD:
                    self._log(
                        f"Превышен порог ошибок ({consecutive_errors}), останавливаем кампанию",
                        "error",
                    )
                    stats['status'] = 'error_threshold'
                    try:
                        self.db.update_mass_send_campaign(
                            campaign_id,
                            status="error_threshold",
                            processed_count=processed,
                            error_message=f"consecutive_errors={consecutive_errors}",
                            finished=True,
                        )
                    except Exception:
                        pass
                    return stats

                await asyncio.sleep(
                    random.randint(Config.MASS_SEND_DELAY_MIN, Config.MASS_SEND_DELAY_MAX)
                )
        except asyncio.CancelledError:
            stats['status'] = 'stopped'
            try:
                self.db.update_mass_send_campaign(
                    campaign_id, status="stopped", processed_count=processed, finished=True
                )
            except Exception:
                pass
            self._log("Кампания остановлена", "warning")
            raise

        stats['status'] = 'completed'
        try:
            self.db.update_mass_send_campaign(
                campaign_id, status="completed", processed_count=processed, finished=True
            )
        except Exception:
            pass
        self._log(
            f"DM кампания завершена: {stats['sent']} отправлено, "
            f"{stats['blocked']} заблокировано, {stats['errors']} ошибок"
        )
        return stats

    async def run_group_campaign(self, campaign_id: int, chat_list: list, message_template: str,
                                 media_path: str = None, hourly_limit: int = None) -> Dict:
        """Запускает кампанию рассылки в группы по явному списку целей."""
        hourly_limit = self._clamp_hourly_limit(hourly_limit)

        try:
            targets = self._prepare_targets(chat_list)
        except ValueError as e:
            self._log(f"Group кампания: невалидные цели ({e})", "error")
            try:
                self.db.update_mass_send_campaign(
                    campaign_id, status="failed", error_message=str(e), finished=True
                )
            except Exception:
                pass
            return {'sent': 0, 'blocked': 0, 'errors': 0, 'total': 0, 'status': 'failed'}

        try:
            self.db.update_mass_send_campaign(
                campaign_id, status="running", total_targets=len(targets), processed_count=0
            )
        except Exception:
            pass

        stats = {'sent': 0, 'blocked': 0, 'errors': 0, 'total': 0, 'status': 'running'}
        consecutive_errors = 0
        processed = 0
        # Скользящее часовое окно лимита успешных отправок
        window_start = time.monotonic()
        sent_in_window = 0

        try:
            for parsed in targets:
                label = parsed.original or str(parsed.value)
                stats['total'] += 1
                text = self._process_spintax(message_template)

                # Соблюдаем темп: не более hourly_limit успешных отправок в час
                sent_in_window, window_start = await self._throttle_hourly_window(
                    sent_in_window, window_start, hourly_limit
                )

                async def _do_send(p=parsed, t=text):
                    return await self.send_to_group(p, t, media_path)

                success, error, _fw = await self._send_with_flood_retry(_do_send, max_flood_retries=1)
                processed += 1

                if success:
                    stats['sent'] += 1
                    sent_in_window += 1
                    consecutive_errors = 0
                    self.db.add_mass_send_result(campaign_id, self.account_id, label, 'group', 'sent')
                    self._log(f"Отправлено в группу {label}")

                elif error and "peer_flood" in error:
                    stats['errors'] += 1
                    self._handle_peer_flood(campaign_id, label, 'group')
                    stats['status'] = 'peer_flood'
                    try:
                        self.db.update_mass_send_campaign(campaign_id, processed_count=processed)
                    except Exception:
                        pass
                    return stats

                elif error and error.startswith("flood_wait:"):
                    stats['errors'] += 1
                    consecutive_errors += 1
                    self.db.add_mass_send_result(
                        campaign_id, self.account_id, label, 'group', 'error', error
                    )

                elif error and ("write_forbidden" in error or "channel_private" in error):
                    stats['blocked'] += 1
                    consecutive_errors = 0
                    self.db.add_mass_send_result(
                        campaign_id, self.account_id, label, 'group', 'blocked', error
                    )
                    self._log(f"Нет доступа к группе {label}: {error}", "warning")

                else:
                    stats['errors'] += 1
                    consecutive_errors += 1
                    self.db.add_mass_send_result(
                        campaign_id, self.account_id, label, 'group', 'error', error
                    )
                    self._log(f"Ошибка отправки в группу {label}: {error}", "error")

                # Периодическое обновление прогресса (~каждые 10 обработанных)
                if processed % 10 == 0:
                    try:
                        self.db.update_mass_send_campaign(campaign_id, processed_count=processed)
                    except Exception:
                        pass

                if consecutive_errors >= Config.MASS_SEND_ERROR_THRESHOLD:
                    self._log(
                        f"Превышен порог ошибок ({consecutive_errors}), останавливаем кампанию",
                        "error",
                    )
                    stats['status'] = 'error_threshold'
                    try:
                        self.db.update_mass_send_campaign(
                            campaign_id,
                            status="error_threshold",
                            processed_count=processed,
                            error_message=f"consecutive_errors={consecutive_errors}",
                            finished=True,
                        )
                    except Exception:
                        pass
                    return stats

                await asyncio.sleep(random.randint(30, 60))
        except asyncio.CancelledError:
            stats['status'] = 'stopped'
            try:
                self.db.update_mass_send_campaign(
                    campaign_id, status="stopped", processed_count=processed, finished=True
                )
            except Exception:
                pass
            self._log("Кампания остановлена", "warning")
            raise

        stats['status'] = 'completed'
        try:
            self.db.update_mass_send_campaign(
                campaign_id, status="completed", processed_count=processed, finished=True
            )
        except Exception:
            pass
        self._log(
            f"Групповая кампания завершена: {stats['sent']} отправлено, "
            f"{stats['blocked']} заблокировано, {stats['errors']} ошибок"
        )
        return stats

    async def get_campaign_stats(self, campaign_id: int) -> Dict:
        """Получает статистику кампании (включая lifecycle)."""
        return self.db.get_campaign_stats(campaign_id)

    async def get_available_targets(self) -> Dict:
        """
        Получает доступные цели для рассылки (справочно).
        Сами кампании принимают только явный список targets.
        """
        users = self.db.get_parsed_users(self.account_id, limit=500)

        groups = []
        try:
            async for dialog in self.client.iter_dialogs(limit=200):
                if dialog.is_group or (
                    dialog.is_channel and not getattr(dialog.entity, 'broadcast', False)
                ):
                    groups.append({
                        "id": dialog.entity.id,
                        "title": dialog.entity.title,
                        "members": getattr(dialog.entity, 'participants_count', 0)
                    })
        except Exception as e:
            self._log(f"Ошибка получения списка групп: {e}", "error")

        return {"users": users, "groups": groups}
