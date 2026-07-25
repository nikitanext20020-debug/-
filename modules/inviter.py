"""
Модуль инвайтинга пользователей в каналы/группы.
Парсинг участников чатов и приглашение в целевые каналы.

Безопасность:
- resolve строковых channel/user targets
- stats пишутся с resolved numeric channel id
- pool пользователей строго фильтруется по source chat (parsed_user_sources)
- skip prior attempts
- in-memory progress
- clamp daily limit вниз до Config.INVITER_DAILY_LIMIT
- PeerFlood → hard-stop + Database.record_flood_wait(Config.PEER_FLOOD_COOLDOWN_SECONDS)
- never broaden the requested pool
"""
import asyncio
import random
from typing import Dict, List, Optional, Union

from telethon import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest, InviteToChannelRequest
from telethon.tl.types import ChannelParticipantsSearch, PeerChannel, User
from telethon.errors import (
    FloodWaitError, PeerFloodError, ChannelPrivateError,
    UserPrivacyRestrictedError, UserNotMutualContactError,
    ChatAdminRequiredError, UserChannelsTooMuchError
)

from config import Config
from utils.database import Database
from utils.telegram_targets import parse_telegram_target, target_entity_ref


class Inviter:
    """Менеджер инвайтинга пользователей"""

    def __init__(self, client: TelegramClient, db: Database, account_id: int):
        self.client = client
        self.db = db
        self.account_id = account_id
        # In-memory progress for the current invite session (API-readable)
        self.progress: Dict = {
            "status": "idle",
            "channel_id": None,
            "source_chat_id": None,
            "total": 0,
            "processed": 0,
            "success": 0,
            "errors": 0,
            "skipped": 0,
            "last_error": None,
        }

    def _log(self, message: str, level: str = "info"):
        """Логирует сообщение"""
        if self.db and self.account_id:
            try:
                self.db.add_log(self.account_id, level, message)
            except Exception:
                pass
        print(f"[Inviter] {message}")

    def get_progress(self) -> Dict:
        """Снимок in-memory прогресса текущей/последней сессии."""
        return dict(self.progress)

    def _set_progress(self, **kwargs):
        self.progress.update(kwargs)

    def _clamp_daily_limit(self, daily_limit: Optional[int]) -> int:
        cap = int(getattr(Config, "INVITER_DAILY_LIMIT", 40) or 40)
        if daily_limit is None:
            return cap
        try:
            val = int(daily_limit)
        except (TypeError, ValueError):
            return cap
        if val <= 0:
            return cap
        return min(val, cap)

    def _numeric_entity_id(self, entity) -> int:
        """Stable numeric id for stats (prefer full channel peer id when available)."""
        eid = getattr(entity, "id", None)
        if eid is None:
            raise ValueError("entity_without_id")
        is_channel = bool(getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False))
        if is_channel and eid > 0:
            return int(f"-100{eid}")
        return int(eid)

    async def _resolve_entity(self, chat_id_or_username):
        """
        Умный резолвинг чата: @username, t.me/…, numeric ID, tg://user?id=.
        """
        if not isinstance(chat_id_or_username, (str, int)):
            return chat_id_or_username

        parsed = parse_telegram_target(chat_id_or_username)
        if parsed.resolvable:
            ref = target_entity_ref(parsed)
            if parsed.kind == "username":
                return await self.client.get_entity(ref)

            num = int(ref)
            candidates = [num]
            if num > 0:
                candidates.append(PeerChannel(num))
                candidates.append(int(f"-100{num}"))
            else:
                s = str(num)
                if s.startswith("-100"):
                    try:
                        candidates.append(PeerChannel(int(s[4:])))
                    except ValueError:
                        pass

            for cand in candidates:
                try:
                    return await self.client.get_entity(cand)
                except (ValueError, TypeError):
                    continue

            async for dialog in self.client.iter_dialogs():
                ent = dialog.entity
                ent_id = getattr(ent, "id", None)
                if ent_id is not None and (
                    ent_id == num or int(f"-100{ent_id}") == num or ent_id == abs(num)
                ):
                    return ent

            raise ValueError(
                f"Не удалось найти чат по ID {chat_id_or_username}. "
                "Для числовых ID аккаунт должен состоять в чате — используйте @username или ссылку t.me/…"
            )

        raw = str(chat_id_or_username).strip()
        if parsed.kind == "invite" or raw.startswith("+") or "joinchat" in raw.lower():
            return await self.client.get_entity(chat_id_or_username)

        if "t.me/" in raw or "telegram.me/" in raw:
            tail = raw.split("/")[-1]
            raw = tail if tail else raw

        cleaned = raw.lstrip("@")
        if not cleaned.lstrip("-").isdigit():
            return await self.client.get_entity(raw)

        num = int(cleaned)
        candidates = [num]
        if num > 0:
            candidates.append(PeerChannel(num))
            candidates.append(int(f"-100{num}"))
        else:
            s = str(num)
            if s.startswith("-100"):
                candidates.append(PeerChannel(int(s[4:])))

        for cand in candidates:
            try:
                return await self.client.get_entity(cand)
            except (ValueError, TypeError):
                continue

        async for dialog in self.client.iter_dialogs():
            ent = dialog.entity
            ent_id = getattr(ent, "id", None)
            if ent_id is not None and (ent_id == num or int(f"-100{ent_id}") == num):
                return ent

        raise ValueError(
            f"Не удалось найти чат по ID {chat_id_or_username}. "
            "Для числовых ID аккаунт должен состоять в чате — используйте @username или ссылку t.me/…"
        )

    async def parse_users_from_chat(self, chat_id_or_username, limit: int = 5000) -> int:
        """
        Парсит пользователей из чата/группы С ПАГИНАЦИЕЙ.
        Каждая запись связывается с source_chat через parsed_user_sources.
        """
        try:
            entity = await self._resolve_entity(chat_id_or_username)
            title = getattr(entity, 'title', str(chat_id_or_username))
            source_id = getattr(entity, 'id', None)

            BATCH = 200
            offset = 0
            seen = 0
            new_count = 0
            valid_count = 0
            empty_batches = 0

            while seen < limit:
                try:
                    result = await self.client(GetParticipantsRequest(
                        channel=entity,
                        filter=ChannelParticipantsSearch(''),
                        offset=offset,
                        limit=min(BATCH, limit - seen),
                        hash=0
                    ))
                except FloodWaitError as e:
                    self._log(f"FloodWait {e.seconds}с во время парсинга, жду", "warning")
                    if e.seconds > 600:
                        self._log("FloodWait слишком большой, останавливаю парсинг", "warning")
                        break
                    await asyncio.sleep(e.seconds + 5)
                    continue

                users = result.users or []
                if not users:
                    empty_batches += 1
                    if empty_batches >= 2:
                        break
                    offset += BATCH
                    continue
                empty_batches = 0

                for user in users:
                    seen += 1
                    if user.bot or user.deleted:
                        continue
                    valid_count += 1
                    if self.db.add_parsed_user(
                        self.account_id,
                        user.id,
                        user.username,
                        user.first_name,
                        user.last_name,
                        source_id,
                        title
                    ):
                        new_count += 1

                offset += len(users)
                await asyncio.sleep(random.uniform(0.5, 1.5))

            self._log(
                f"Спарсено {new_count} новых (просмотрено {seen}, живых {valid_count}) "
                f"из {title}"
            )
            return new_count

        except ChannelPrivateError:
            self._log("Канал приватный, нет доступа", "error")
            return 0
        except ChatAdminRequiredError:
            self._log("Нужны права администратора для парсинга", "error")
            return 0
        except FloodWaitError as e:
            self._log(f"FloodWait: ожидание {e.seconds} секунд", "warning")
            await asyncio.sleep(e.seconds + 10)
            return 0
        except Exception as e:
            self._log(f"Ошибка парсинга: {e}", "error")
            return 0

    async def _invite_with_flood_retry(self, channel, user):
        """Приглашает одного пользователя; после разумного FloodWait повторяет ровно один раз."""
        for attempt in range(2):
            try:
                await self.client(InviteToChannelRequest(channel=channel, users=[user]))
                return
            except FloodWaitError as e:
                if e.seconds > 600 or attempt > 0:
                    raise
                self._log(f"FloodWait {e.seconds}с: жду и повторяю этого же пользователя один раз", "warning")
                await asyncio.sleep(e.seconds + 10)

    async def invite_users_to_channel(self, channel_id, user_ids: list,
                                      daily_limit: int = None) -> Dict:
        """
        Приглашает пользователей в канал.
        channel_id может быть int / @username / t.me link — резолвится;
        в stats пишется resolved numeric id.
        """
        daily_limit = self._clamp_daily_limit(daily_limit)

        stats = {'success': 0, 'errors': 0, 'skipped': 0, 'total': 0, 'status': 'running'}

        try:
            channel = await self._resolve_entity(channel_id)
            resolved_channel_id = self._numeric_entity_id(channel)
        except Exception as e:
            self._log(f"Не удалось получить канал {channel_id}: {e}", "error")
            self._set_progress(status="failed", last_error=str(e))
            stats['status'] = 'failed'
            return stats

        try:
            already = self.db.get_invited_user_ids(self.account_id, resolved_channel_id)
        except Exception:
            already = set()

        normalized_users: List = []
        for u in user_ids or []:
            try:
                if isinstance(u, int):
                    uid = u
                else:
                    p = parse_telegram_target(u)
                    if p.kind == "user_id" and isinstance(p.value, int):
                        uid = int(p.value)
                    elif str(u).lstrip("-").isdigit():
                        uid = int(u)
                    else:
                        uid = u
                if isinstance(uid, int) and uid in already:
                    stats['skipped'] += 1
                    continue
                normalized_users.append(uid)
            except Exception:
                continue

        batch = normalized_users[:daily_limit]
        self._set_progress(
            status="running",
            channel_id=resolved_channel_id,
            total=len(batch),
            processed=0,
            success=0,
            errors=0,
            skipped=stats['skipped'],
            last_error=None,
        )

        for user_ref in batch:
            stats['total'] += 1
            uid_for_stats = 0
            try:
                if isinstance(user_ref, int):
                    user = await self.client.get_entity(user_ref)
                    uid_for_stats = user_ref
                else:
                    p = parse_telegram_target(user_ref)
                    if p.resolvable:
                        user = await self.client.get_entity(target_entity_ref(p))
                    else:
                        user = await self.client.get_entity(user_ref)
                    uid_for_stats = int(getattr(user, "id", 0) or 0)

                if not isinstance(user, User):
                    raise ValueError("Цель инвайта не является пользователем")

                if uid_for_stats and uid_for_stats in already:
                    stats['skipped'] += 1
                    self._set_progress(
                        processed=stats['total'],
                        skipped=self.progress.get('skipped', 0) + 1,
                    )
                    continue

                await self._invite_with_flood_retry(channel, user)
                stats['success'] += 1
                self.db.add_invite_result(
                    self.account_id, resolved_channel_id, uid_for_stats, 'success'
                )
                already.add(uid_for_stats)
                self._log(f"Приглашен пользователь {uid_for_stats}")

            except UserPrivacyRestrictedError:
                stats['skipped'] += 1
                self.db.add_invite_result(
                    self.account_id, resolved_channel_id, uid_for_stats,
                    'skipped', 'privacy restricted'
                )
                self._log("Пользователь ограничил приватность", "warning")

            except UserNotMutualContactError:
                stats['skipped'] += 1
                self.db.add_invite_result(
                    self.account_id, resolved_channel_id, uid_for_stats,
                    'skipped', 'not mutual contact'
                )

            except UserChannelsTooMuchError:
                stats['skipped'] += 1
                self.db.add_invite_result(
                    self.account_id, resolved_channel_id, uid_for_stats,
                    'skipped', 'user in too many channels'
                )

            except FloodWaitError as e:
                # Повтор уже был выполнен внутри _invite_with_flood_retry для
                # короткого ожидания; после повторного/длинного FloodWait сессию
                # безопасно останавливаем и не переходим к следующей цели.
                stats['errors'] += 1
                stats['status'] = 'flood_wait_stop'
                self._log(f"FloodWait {e.seconds}с после retry: останавливаем инвайт", "warning")
                try:
                    self.db.add_invite_result(
                        self.account_id,
                        resolved_channel_id,
                        uid_for_stats,
                        'error',
                        f'flood_wait:{e.seconds}',
                    )
                    self.db.record_flood_wait(self.account_id, int(e.seconds) + 10)
                except Exception:
                    pass
                self._set_progress(
                    status="flood_wait_stop",
                    errors=stats['errors'],
                    last_error=f"flood_wait:{e.seconds}",
                )
                break

            except PeerFloodError:
                stats['errors'] += 1
                self.db.add_invite_result(
                    self.account_id, resolved_channel_id, uid_for_stats,
                    'error', 'peer flood'
                )
                cooldown = int(getattr(Config, "PEER_FLOOD_COOLDOWN_SECONDS", 43200) or 43200)
                try:
                    self.db.record_flood_wait(self.account_id, cooldown)
                except Exception:
                    pass
                self._log(
                    f"PeerFloodError: hard-stop инвайтинга, cooldown {cooldown}s",
                    "error",
                )
                stats['status'] = 'peer_flood'
                self._set_progress(
                    status="peer_flood",
                    processed=stats['total'],
                    errors=stats['errors'],
                    last_error="peer_flood",
                )
                return stats

            except Exception as e:
                stats['errors'] += 1
                self.db.add_invite_result(
                    self.account_id, resolved_channel_id, uid_for_stats,
                    'error', str(e)
                )
                self._log(f"Ошибка инвайта пользователя {user_ref}: {e}", "error")

            self._set_progress(
                processed=stats['total'],
                success=stats['success'],
                errors=stats['errors'],
                skipped=stats['skipped'],
            )
            await asyncio.sleep(
                random.randint(Config.INVITER_DELAY_MIN, Config.INVITER_DELAY_MAX)
            )

        if stats['status'] == 'running':
            stats['status'] = 'completed'
        self._set_progress(status=stats['status'])
        self._log(
            f"Сессия завершена: {stats['success']} успешно, "
            f"{stats['errors']} ошибок, {stats['skipped']} пропущено"
        )
        return stats

    async def get_available_chats(self) -> List[Dict]:
        """Получает список доступных групп/супергрупп."""
        chats = []
        try:
            async for dialog in self.client.iter_dialogs(limit=200):
                if dialog.is_group or (
                    dialog.is_channel and not getattr(dialog.entity, 'broadcast', False)
                ):
                    chats.append({
                        "id": dialog.entity.id,
                        "title": dialog.entity.title,
                        "members": getattr(dialog.entity, 'participants_count', 0)
                    })
        except Exception as e:
            self._log(f"Ошибка получения списка чатов: {e}", "error")

        return chats

    async def get_stats(self) -> Dict:
        """Получает статистику инвайтов для текущего аккаунта + in-memory progress."""
        base = self.db.get_invite_stats(self.account_id)
        base["progress"] = self.get_progress()
        return base

    async def run_invite_session(self, channel_id, source_chat_id, limit: int = 50) -> Dict:
        """
        Полная сессия инвайтинга:
        1. Парсит пользователей из ИМЕННО source_chat_id
        2. Берёт pool только из этого source (junction filter)
        3. Скипает prior attempts
        4. Приглашает с clamp(limit)
        """
        limit = self._clamp_daily_limit(limit)
        self._set_progress(
            status="parsing",
            channel_id=None,
            source_chat_id=str(source_chat_id),
            total=0,
            processed=0,
            success=0,
            errors=0,
            skipped=0,
            last_error=None,
        )

        try:
            source_entity = await self._resolve_entity(source_chat_id)
            source_numeric = getattr(source_entity, "id", None)
        except Exception as e:
            self._log(f"Не удалось резолвить source chat {source_chat_id}: {e}", "error")
            self._set_progress(status="failed", last_error=str(e))
            return {'success': 0, 'errors': 0, 'skipped': 0, 'total': 0, 'status': 'failed'}

        parsed = await self.parse_users_from_chat(source_chat_id, limit=max(limit * 5, limit))
        self._log(f"Спарсено {parsed} пользователей из источника {source_chat_id}")

        users = self.db.get_parsed_users(
            self.account_id, limit=limit * 3, offset=0, source_chat_id=source_numeric
        )
        user_ids = [u['user_id'] for u in users]

        try:
            channel_for_filter = channel_id
            try:
                ch_ent = await self._resolve_entity(channel_id)
                channel_for_filter = self._numeric_entity_id(ch_ent)
            except Exception:
                if isinstance(channel_id, int):
                    channel_for_filter = channel_id
            already_invited = self.db.get_invited_user_ids(
                self.account_id,
                int(channel_for_filter) if isinstance(channel_for_filter, int) else None,
            )
        except Exception:
            already_invited = set()

        filtered_user_ids = [uid for uid in user_ids if uid not in already_invited]
        self._log(f"После фильтрации: {len(filtered_user_ids)} пользователей для инвайта")
        self._set_progress(status="inviting", source_chat_id=source_numeric)

        result = await self.invite_users_to_channel(
            channel_id, filtered_user_ids, daily_limit=limit
        )
        return result

    async def run_auto_invite_batch(
        self,
        channel_id,
        source_chats: list,
        per_cycle: int = 5,
        daily_limit: int = None,
    ) -> Dict:
        """
        Один «тик» автоматического инвайтинга для цикла воркера.
        Pool никогда не расширяется за пределы указанных source_chats.
        """
        daily_limit = self._clamp_daily_limit(daily_limit)
        per_cycle = max(1, min(int(per_cycle or 5), daily_limit))

        stats = {
            'success': 0, 'errors': 0, 'skipped': 0, 'total': 0,
            'parsed': 0, 'reached_daily_limit': False, 'status': 'running',
        }

        try:
            today_count = self.db.get_invite_stats(self.account_id).get('today_count', 0)
        except Exception:
            today_count = 0

        remaining_today = daily_limit - today_count
        if remaining_today <= 0:
            stats['reached_daily_limit'] = True
            stats['status'] = 'daily_limit'
            self._log(
                f"Дневной лимит инвайтов достигнут ({today_count}/{daily_limit})",
                "info",
            )
            return stats

        batch_size = min(per_cycle, remaining_today)

        try:
            ch_ent = await self._resolve_entity(channel_id)
            resolved_channel_id = self._numeric_entity_id(ch_ent)
        except Exception as e:
            self._log(f"Авто-инвайт: канал не резолвится ({e})", "error")
            stats['status'] = 'failed'
            return stats

        try:
            already_invited = self.db.get_invited_user_ids(
                self.account_id, resolved_channel_id
            )
        except Exception:
            already_invited = set()

        source_ids: List[int] = []
        source_raw_map = []
        for src in source_chats or []:
            src = str(src).strip()
            if not src:
                continue
            try:
                ent = await self._resolve_entity(src)
                sid = getattr(ent, "id", None)
                if sid is not None:
                    source_ids.append(int(sid))
                    source_raw_map.append((src, int(sid)))
            except Exception as e:
                self._log(f"Авто-инвайт: source {src} не резолвится: {e}", "warning")

        def _fresh_user_ids():
            """Users only from the requested source chats — never global pool."""
            if not source_ids:
                return []
            collected = []
            seen = set()
            for sid in source_ids:
                rows = self.db.get_parsed_users(
                    self.account_id, limit=500, source_chat_id=sid
                )
                for u in rows:
                    uid = u['user_id']
                    if uid in already_invited or uid in seen:
                        continue
                    seen.add(uid)
                    collected.append(uid)
            return collected

        candidates = _fresh_user_ids()

        if len(candidates) < batch_size and source_raw_map:
            for src, sid in source_raw_map:
                try:
                    parsed = await self.parse_users_from_chat(src, limit=1000)
                    stats['parsed'] += parsed
                except Exception as e:
                    self._log(f"Авто-парсинг из {src} не удался: {e}", "warning")
                candidates = _fresh_user_ids()
                if len(candidates) >= batch_size:
                    break

        if not candidates:
            self._log(
                "Нет новых пользователей для авто-инвайта "
                "(источники пусты или все приглашены)",
                "info",
            )
            stats['status'] = 'empty'
            return stats

        batch = candidates[:batch_size]
        result = await self.invite_users_to_channel(
            resolved_channel_id, batch, daily_limit=batch_size
        )
        stats['success'] += result.get('success', 0)
        stats['errors'] += result.get('errors', 0)
        stats['skipped'] += result.get('skipped', 0)
        stats['total'] += result.get('total', 0)
        stats['status'] = result.get('status', 'completed')
        return stats
