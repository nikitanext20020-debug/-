"""
Модуль массовой рассылки сообщений.
Отправка в личные сообщения и группы с поддержкой спинтакса.
"""
import asyncio
import random
import re
import os
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient
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
            except:
                pass
        print(f"[MassSender] {message}")

    def _process_spintax(self, text: str) -> str:
        """
        Обрабатывает спинтакс паттерны вида {word1|word2|word3}.

        Args:
            text: Текст со спинтакс-паттернами

        Returns:
            Текст с случайно выбранными вариантами
        """
        def _replace(match):
            options = match.group(1).split('|')
            return random.choice(options)

        return re.sub(r'\{([^{}]+)\}', _replace, text)

    async def send_to_user(self, user_id_or_username, text: str, media_path: str = None, media_type: str = None) -> Tuple[bool, Optional[str]]:
        """
        Отправляет сообщение пользователю в ЛС.

        Args:
            user_id_or_username: ID или username пользователя
            text: Текст сообщения
            media_path: Путь к медиафайлу
            media_type: Тип медиа

        Returns:
            (success, error) - кортеж с результатом
        """
        try:
            # Имитация набора текста
            await self.client.action(user_id_or_username, 'typing')
            await asyncio.sleep(random.uniform(2, 5))

            if media_path and os.path.exists(media_path):
                await self.client.send_file(user_id_or_username, media_path, caption=text)
            else:
                await self.client.send_message(user_id_or_username, text)

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
            # Fallback: check error message strings
            err_str = str(e).lower()
            if "blocked" in err_str:
                return (False, "user_blocked")
            if "deactivated" in err_str:
                return (False, "user_deactivated")
            return (False, str(e))

    async def send_to_group(self, chat_id, text: str, media_path: str = None, media_type: str = None) -> Tuple[bool, Optional[str]]:
        """
        Отправляет сообщение в группу/чат.

        Args:
            chat_id: ID или username группы
            text: Текст сообщения
            media_path: Путь к медиафайлу
            media_type: Тип медиа

        Returns:
            (success, error) - кортеж с результатом
        """
        try:
            if media_path and os.path.exists(media_path):
                await self.client.send_file(chat_id, media_path, caption=text)
            else:
                await self.client.send_message(chat_id, text)

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

    async def run_dm_campaign(self, campaign_id: int, user_list: list, message_template: str, media_path: str = None, hourly_limit: int = None) -> Dict:
        """
        Запускает кампанию рассылки в ЛС.

        Args:
            campaign_id: ID кампании
            user_list: Список пользователей (ID или username)
            message_template: Шаблон сообщения (поддерживает спинтакс)
            media_path: Путь к медиафайлу
            hourly_limit: Лимит сообщений в час

        Returns:
            Статистика кампании
        """
        if hourly_limit is None:
            hourly_limit = Config.MASS_SEND_HOURLY_LIMIT

        stats = {'sent': 0, 'blocked': 0, 'errors': 0, 'total': 0}
        consecutive_errors = 0

        for user in user_list[:hourly_limit]:
            stats['total'] += 1

            # Обработка спинтакса для каждого сообщения
            text = self._process_spintax(message_template)

            success, error = await self.send_to_user(user, text, media_path)

            if success:
                stats['sent'] += 1
                consecutive_errors = 0
                self.db.add_mass_send_result(campaign_id, self.account_id, str(user), 'user', 'sent')
                self._log(f"Отправлено пользователю {user}")

            elif error and ("blocked" in error or "deactivated" in error):
                stats['blocked'] += 1
                self.db.add_mass_send_result(campaign_id, self.account_id, str(user), 'user', 'blocked', error)
                self._log(f"Пользователь {user} заблокирован/деактивирован", "warning")

            elif error and "peer_flood" in error:
                self._log("PeerFloodError: критическая ошибка, останавливаем кампанию", "error")
                self.db.add_mass_send_result(campaign_id, self.account_id, str(user), 'user', 'error', error)
                break

            elif error and "flood_wait" in error:
                # Извлекаем время ожидания
                try:
                    seconds = int(error.split(":")[1])
                except (IndexError, ValueError):
                    seconds = 60
                self._log(f"FloodWait: ожидание {seconds} секунд", "warning")
                await asyncio.sleep(seconds + 30)
                # Не считаем как ошибку

            else:
                stats['errors'] += 1
                consecutive_errors += 1
                self.db.add_mass_send_result(campaign_id, self.account_id, str(user), 'user', 'error', error)
                self._log(f"Ошибка отправки пользователю {user}: {error}", "error")

            # Проверка порога ошибок
            if consecutive_errors >= Config.MASS_SEND_ERROR_THRESHOLD:
                self._log(f"Превышен порог ошибок ({consecutive_errors}), останавливаем кампанию", "error")
                break

            # Случайная задержка между сообщениями
            await asyncio.sleep(random.randint(Config.MASS_SEND_DELAY_MIN, Config.MASS_SEND_DELAY_MAX))

        self._log(f"DM кампания завершена: {stats['sent']} отправлено, {stats['blocked']} заблокировано, {stats['errors']} ошибок")
        return stats

    async def run_group_campaign(self, campaign_id: int, chat_list: list, message_template: str, media_path: str = None, hourly_limit: int = None) -> Dict:
        """
        Запускает кампанию рассылки в группы.

        Args:
            campaign_id: ID кампании
            chat_list: Список групп (ID или username)
            message_template: Шаблон сообщения (поддерживает спинтакс)
            media_path: Путь к медиафайлу
            hourly_limit: Лимит сообщений в час

        Returns:
            Статистика кампании
        """
        if hourly_limit is None:
            hourly_limit = Config.MASS_SEND_HOURLY_LIMIT

        stats = {'sent': 0, 'blocked': 0, 'errors': 0, 'total': 0}
        consecutive_errors = 0

        for chat in chat_list[:hourly_limit]:
            stats['total'] += 1

            text = self._process_spintax(message_template)

            success, error = await self.send_to_group(chat, text, media_path)

            if success:
                stats['sent'] += 1
                consecutive_errors = 0
                self.db.add_mass_send_result(campaign_id, self.account_id, str(chat), 'group', 'sent')
                self._log(f"Отправлено в группу {chat}")

            elif error and "peer_flood" in error:
                self._log("PeerFloodError: останавливаем кампанию", "error")
                self.db.add_mass_send_result(campaign_id, self.account_id, str(chat), 'group', 'error', error)
                break

            elif error and "flood_wait" in error:
                try:
                    seconds = int(error.split(":")[1])
                except (IndexError, ValueError):
                    seconds = 60
                self._log(f"FloodWait: ожидание {seconds} секунд", "warning")
                await asyncio.sleep(seconds + 30)

            elif error and ("write_forbidden" in error or "channel_private" in error):
                stats['blocked'] += 1
                self.db.add_mass_send_result(campaign_id, self.account_id, str(chat), 'group', 'blocked', error)
                self._log(f"Нет доступа к группе {chat}: {error}", "warning")

            else:
                stats['errors'] += 1
                consecutive_errors += 1
                self.db.add_mass_send_result(campaign_id, self.account_id, str(chat), 'group', 'error', error)
                self._log(f"Ошибка отправки в группу {chat}: {error}", "error")

            if consecutive_errors >= Config.MASS_SEND_ERROR_THRESHOLD:
                self._log(f"Превышен порог ошибок ({consecutive_errors}), останавливаем кампанию", "error")
                break

            # Более короткие задержки для групп
            await asyncio.sleep(random.randint(30, 60))

        self._log(f"Групповая кампания завершена: {stats['sent']} отправлено, {stats['blocked']} заблокировано, {stats['errors']} ошибок")
        return stats

    async def get_campaign_stats(self, campaign_id: int) -> Dict:
        """
        Получает статистику кампании.

        Args:
            campaign_id: ID кампании

        Returns:
            Словарь со статистикой
        """
        return self.db.get_campaign_stats(campaign_id)

    async def get_available_targets(self) -> Dict:
        """
        Получает доступные цели для рассылки.

        Returns:
            Словарь с пользователями и группами
        """
        # Получаем пользователей из БД
        users = self.db.get_parsed_users(self.account_id, limit=500)

        # Получаем группы из диалогов
        groups = []
        try:
            async for dialog in self.client.iter_dialogs(limit=200):
                if dialog.is_group or (dialog.is_channel and not getattr(dialog.entity, 'broadcast', False)):
                    groups.append({
                        "id": dialog.entity.id,
                        "title": dialog.entity.title,
                        "members": getattr(dialog.entity, 'participants_count', 0)
                    })
        except Exception as e:
            self._log(f"Ошибка получения списка групп: {e}", "error")

        return {"users": users, "groups": groups}
