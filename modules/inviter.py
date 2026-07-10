"""
Модуль инвайтинга пользователей в каналы/группы.
Парсинг участников чатов и приглашение в целевые каналы.
"""
import asyncio
import random
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest, InviteToChannelRequest
from telethon.tl.types import ChannelParticipantsSearch, PeerChannel, PeerChat
from telethon.errors import (
    FloodWaitError, PeerFloodError, ChannelPrivateError,
    UserPrivacyRestrictedError, UserNotMutualContactError,
    ChatAdminRequiredError, UserChannelsTooMuchError
)

from config import Config
from utils.database import Database


class Inviter:
    """Менеджер инвайтинга пользователей"""

    def __init__(self, client: TelegramClient, db: Database, account_id: int):
        self.client = client
        self.db = db
        self.account_id = account_id

    def _log(self, message: str, level: str = "info"):
        """Логирует сообщение"""
        if self.db and self.account_id:
            try:
                self.db.add_log(self.account_id, level, message)
            except:
                pass
        print(f"[Inviter] {message}")

    async def _resolve_entity(self, chat_id_or_username):
        """
        Умный резолвинг чата: принимает @username, ссылку t.me/…, инвайт-ссылку
        или числовой ID. Числовой ID Telethon не может преобразовать в сущность
        без предварительного «знакомства», поэтому пробуем несколько вариантов и
        как fallback ищем чат среди диалогов аккаунта.
        """
        raw = str(chat_id_or_username).strip()

        # Ссылки t.me / telegram.me → вытаскиваем username или инвайт
        if "t.me/" in raw or "telegram.me/" in raw:
            tail = raw.split("/")[-1]
            raw = tail if tail else raw

        # Приватная инвайт-ссылка (+hash или joinchat) — отдаём как есть
        if raw.startswith("+") or "joinchat" in str(chat_id_or_username):
            return await self.client.get_entity(chat_id_or_username)

        # Username (не число) — прямой резолвинг
        cleaned = raw.lstrip("@")
        if not cleaned.lstrip("-").isdigit():
            return await self.client.get_entity(raw)

        # Числовой ID: пробуем разные представления
        num = int(cleaned)
        candidates = [num]
        if num > 0:
            # ID канала/супергруппы без префикса -100 → достраиваем оба варианта
            candidates.append(PeerChannel(num))
            candidates.append(int(f"-100{num}"))
        else:
            # Уже с минусом: -100XXXXXXXXXX → извлекаем чистый channel_id
            s = str(num)
            if s.startswith("-100"):
                candidates.append(PeerChannel(int(s[4:])))

        for cand in candidates:
            try:
                return await self.client.get_entity(cand)
            except (ValueError, TypeError):
                continue

        # Fallback: ищем среди диалогов, где аккаунт уже состоит
        async for dialog in self.client.iter_dialogs():
            ent = dialog.entity
            ent_id = getattr(ent, "id", None)
            if ent_id is not None and (ent_id == num or int(f"-100{ent_id}") == num):
                return ent

        # Ничего не вышло — понятная ошибка вместо «Cannot find any entity»
        raise ValueError(
            f"Не удалось найти чат по ID {chat_id_or_username}. "
            "Для числовых ID аккаунт должен состоять в чате — используйте @username или ссылку t.me/…"
        )

    async def parse_users_from_chat(self, chat_id_or_username, limit: int = 5000) -> int:
        """
        Парсит пользователей из чата/группы С ПАГИНАЦИЕЙ.

        Telegram за один GetParticipantsRequest отдаёт ограниченный батч
        (обычно до 100-200 юзеров). Чтобы собрать всех доступных участников,
        нужно листать список с растущим offset, пока батчи не закончатся.

        Args:
            chat_id_or_username: ID или username чата
            limit: Максимальное количество пользователей для парсинга (потолок)

        Returns:
            Количество НОВЫХ (добавленных в БД) пользователей
        """
        try:
            entity = await self._resolve_entity(chat_id_or_username)
            title = getattr(entity, 'title', str(chat_id_or_username))

            BATCH = 200          # размер одного запроса к Telegram
            offset = 0
            seen = 0             # всего просмотрено участников (для detecтa конца)
            new_count = 0        # новых записей в БД
            valid_count = 0      # живых (не бот/не удалён) участников
            empty_batches = 0    # подряд идущих пустых батчей

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
                    # FloodWait в середине пагинации: ждём и продолжаем с того же offset
                    self._log(f"FloodWait {e.seconds}с во время парсинга, жду", "warning")
                    if e.seconds > 600:
                        self._log("FloodWait слишком большой, останавливаю парсинг", "warning")
                        break
                    await asyncio.sleep(e.seconds + 5)
                    continue

                users = result.users or []
                if not users:
                    # Пустой батч — участники закончились (или Telegram скрывает остальных)
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
                    # add_parsed_user возвращает True только для новых записей
                    if self.db.add_parsed_user(
                        self.account_id,
                        user.id,
                        user.username,
                        user.first_name,
                        user.last_name,
                        entity.id,
                        title
                    ):
                        new_count += 1

                offset += len(users)

                # Небольшая пауза между страницами — снижает риск FloodWait
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

    async def invite_users_to_channel(self, channel_id: int, user_ids: list, daily_limit: int = None) -> Dict:
        """
        Приглашает пользователей в канал.

        Args:
            channel_id: ID целевого канала
            user_ids: Список ID пользователей для приглашения
            daily_limit: Дневной лимит приглашений

        Returns:
            Статистика: {success, errors, skipped, total}
        """
        if daily_limit is None:
            daily_limit = Config.INVITER_DAILY_LIMIT

        stats = {'success': 0, 'errors': 0, 'skipped': 0, 'total': 0}

        try:
            channel = await self._resolve_entity(channel_id)
        except Exception as e:
            self._log(f"Не удалось получить канал {channel_id}: {e}", "error")
            return stats

        for user_id in user_ids[:daily_limit]:
            stats['total'] += 1

            try:
                user = await self.client.get_entity(user_id)
                await self.client(InviteToChannelRequest(channel=channel, users=[user]))
                stats['success'] += 1
                self.db.add_invite_result(self.account_id, channel_id, user_id, 'success')
                self._log(f"Приглашен пользователь {user_id}")

            except UserPrivacyRestrictedError:
                stats['skipped'] += 1
                self.db.add_invite_result(self.account_id, channel_id, user_id, 'skipped', 'privacy restricted')
                self._log(f"Пользователь {user_id} ограничил приватность", "warning")

            except UserNotMutualContactError:
                stats['skipped'] += 1
                self.db.add_invite_result(self.account_id, channel_id, user_id, 'skipped', 'not mutual contact')
                self._log(f"Пользователь {user_id} не в контактах", "warning")

            except UserChannelsTooMuchError:
                stats['skipped'] += 1
                self.db.add_invite_result(self.account_id, channel_id, user_id, 'skipped', 'user in too many channels')

            except FloodWaitError as e:
                self._log(f"FloodWait: {e.seconds} секунд", "warning")
                if e.seconds > 600:
                    self._log("FloodWait слишком большой, останавливаем сессию", "error")
                    break
                await asyncio.sleep(e.seconds + 30)

            except PeerFloodError:
                stats['errors'] += 1
                self.db.add_invite_result(self.account_id, channel_id, user_id, 'error', 'peer flood')
                self._log("PeerFloodError: критическая ошибка, останавливаем инвайтинг", "error")
                break

            except Exception as e:
                stats['errors'] += 1
                self.db.add_invite_result(self.account_id, channel_id, user_id, 'error', str(e))
                self._log(f"Ошибка инвайта пользователя {user_id}: {e}", "error")

            # Случайная задержка между инвайтами
            await asyncio.sleep(random.randint(Config.INVITER_DELAY_MIN, Config.INVITER_DELAY_MAX))

        self._log(f"Сессия завершена: {stats['success']} успешно, {stats['errors']} ошибок, {stats['skipped']} пропущено")
        return stats

    async def get_available_chats(self) -> List[Dict]:
        """
        Получает список доступных групп/супергрупп.

        Returns:
            Список словарей с информацией о чатах
        """
        chats = []
        try:
            async for dialog in self.client.iter_dialogs(limit=200):
                if dialog.is_group or (dialog.is_channel and not getattr(dialog.entity, 'broadcast', False)):
                    chats.append({
                        "id": dialog.entity.id,
                        "title": dialog.entity.title,
                        "members": getattr(dialog.entity, 'participants_count', 0)
                    })
        except Exception as e:
            self._log(f"Ошибка получения списка чатов: {e}", "error")

        return chats

    async def get_stats(self) -> Dict:
        """
        Получает статистику инвайтов для текущего аккаунта.

        Returns:
            Словарь со статистикой
        """
        return self.db.get_invite_stats(self.account_id)

    async def run_invite_session(self, channel_id: int, source_chat_id, limit: int = 50) -> Dict:
        """
        Запускает полную сессию инвайтинга.

        1. Парсит пользователей из источника
        2. Фильтрует уже приглашенных
        3. Приглашает оставшихся

        Args:
            channel_id: ID целевого канала
            source_chat_id: ID или username исходного чата
            limit: Лимит пользователей

        Returns:
            Статистика сессии
        """
        # 1. Парсим пользователей из источника
        parsed = await self.parse_users_from_chat(source_chat_id, limit=limit)
        self._log(f"Спарсено {parsed} пользователей из источника")

        # 2. Получаем спарсенных пользователей из БД
        users = self.db.get_parsed_users(self.account_id, limit=limit)
        user_ids = [u['user_id'] for u in users]

        # 3. Фильтруем уже приглашенных
        invite_stats = self.db.get_invite_stats(self.account_id)
        # Получаем список уже приглашенных в этот канал
        already_invited = set()
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT user_id FROM invite_stats WHERE account_id = ? AND channel_id = ? AND status = 'success'",
                    (self.account_id, channel_id)
                )
                already_invited = {row[0] for row in cursor.fetchall()}
        except Exception:
            pass

        filtered_user_ids = [uid for uid in user_ids if uid not in already_invited]
        self._log(f"После фильтрации: {len(filtered_user_ids)} пользователей для инвайта")

        # 4. Приглашаем
        result = await self.invite_users_to_channel(channel_id, filtered_user_ids)
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

        1. Проверяет дневной лимит (по invite_stats за сегодня).
        2. Если спарсенных ещё не приглашённых юзеров мало — парсит из
           источников (по очереди).
        3. Приглашает небольшой батч (per_cycle) в целевой канал.

        Возвращает статистику батча + флаг reached_daily_limit.
        """
        if daily_limit is None:
            daily_limit = Config.INVITER_DAILY_LIMIT

        stats = {'success': 0, 'errors': 0, 'skipped': 0, 'total': 0,
                 'parsed': 0, 'reached_daily_limit': False}

        # 1. Дневной лимит
        try:
            today_count = self.db.get_invite_stats(self.account_id).get('today_count', 0)
        except Exception:
            today_count = 0

        remaining_today = daily_limit - today_count
        if remaining_today <= 0:
            stats['reached_daily_limit'] = True
            self._log(f"Дневной лимит инвайтов достигнут ({today_count}/{daily_limit})", "info")
            return stats

        batch_size = min(per_cycle, remaining_today)

        # 2. Уже приглашённые в этот канал (чтобы не дёргать повторно)
        already_invited = set()
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT user_id FROM invite_stats WHERE account_id = ? AND channel_id = ?",
                    (self.account_id, channel_id)
                )
                already_invited = {row[0] for row in cursor.fetchall()}
        except Exception:
            pass

        def _fresh_user_ids():
            users = self.db.get_parsed_users(self.account_id, limit=500)
            return [u['user_id'] for u in users if u['user_id'] not in already_invited]

        candidates = _fresh_user_ids()

        # 3. Если кандидатов мало — подпарсиваем из источников
        if len(candidates) < batch_size and source_chats:
            for src in source_chats:
                src = str(src).strip()
                if not src:
                    continue
                try:
                    parsed = await self.parse_users_from_chat(src, limit=1000)
                    stats['parsed'] += parsed
                except Exception as e:
                    self._log(f"Авто-парсинг из {src} не удался: {e}", "warning")
                candidates = _fresh_user_ids()
                if len(candidates) >= batch_size:
                    break

        if not candidates:
            self._log("Нет новых пользователей для авто-инвайта (источники пусты или все приглашены)", "info")
            return stats

        # 4. Приглашаем батч
        batch = candidates[:batch_size]
        result = await self.invite_users_to_channel(channel_id, batch, daily_limit=batch_size)
        stats['success'] += result.get('success', 0)
        stats['errors'] += result.get('errors', 0)
        stats['skipped'] += result.get('skipped', 0)
        stats['total'] += result.get('total', 0)
        return stats
