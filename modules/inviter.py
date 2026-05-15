"""
Модуль инвайтинга пользователей в каналы/группы.
Парсинг участников чатов и приглашение в целевые каналы.
"""
import asyncio
import random
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest, InviteToChannelRequest
from telethon.tl.types import ChannelParticipantsSearch
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

    async def parse_users_from_chat(self, chat_id_or_username, limit: int = 200) -> int:
        """
        Парсит пользователей из чата/группы.

        Args:
            chat_id_or_username: ID или username чата
            limit: Максимальное количество пользователей для парсинга

        Returns:
            Количество спарсенных пользователей
        """
        try:
            entity = await self.client.get_entity(chat_id_or_username)

            result = await self.client(GetParticipantsRequest(
                channel=entity,
                filter=ChannelParticipantsSearch(''),
                offset=0,
                limit=limit,
                hash=0
            ))

            count = 0
            for user in result.users:
                if user.bot or user.deleted:
                    continue

                self.db.add_parsed_user(
                    self.account_id,
                    user.id,
                    user.username,
                    user.first_name,
                    user.last_name,
                    entity.id,
                    getattr(entity, 'title', str(chat_id_or_username))
                )
                count += 1

            self._log(f"Спарсено {count} пользователей из {getattr(entity, 'title', chat_id_or_username)}")
            return count

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
            channel = await self.client.get_entity(channel_id)
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
