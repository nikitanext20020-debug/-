"""
Модуль автоматического вступления в каналы
"""
import time
import random
import re
from typing import List, Dict, Tuple, Optional
from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.errors import (
    UsernameInvalidError, UsernameNotOccupiedError, PeerFloodError,
    ChannelPrivateError, FloodWaitError, InviteHashExpiredError,
    UserAlreadyParticipantError, InviteRequestSentError
)
from config import Config


class ChannelJoiner:
    """Класс для автоматического вступления в каналы"""
    
    # Паттерны для извлечения invite hash
    INVITE_PATTERNS = [
        re.compile(r't\.me/\+([a-zA-Z0-9_-]+)'),           # t.me/+hash
        re.compile(r't\.me/joinchat/([a-zA-Z0-9_-]+)'),    # t.me/joinchat/hash
        re.compile(r'telegram\.me/\+([a-zA-Z0-9_-]+)'),    # telegram.me/+hash
        re.compile(r'telegram\.me/joinchat/([a-zA-Z0-9_-]+)'),  # telegram.me/joinchat/hash
    ]
    
    def __init__(self, client: TelegramClient, db=None):
        self.client = client
        self.db = db
    
    def _extract_invite_hash(self, link: str) -> str:
        """
        Извлекает invite hash из различных форматов ссылок.
        
        Поддерживаемые форматы:
        - +hash
        - t.me/+hash
        - t.me/joinchat/hash
        - https://t.me/+hash
        - telegram.me/+hash
        """
        link = link.strip()
        
        # Если это просто хэш (начинается с +)
        if link.startswith('+'):
            return link[1:]
        
        # Пробуем паттерны
        for pattern in self.INVITE_PATTERNS:
            match = pattern.search(link)
            if match:
                return match.group(1)
        
        # Если ничего не нашли, возвращаем как есть (может быть username)
        return link.lstrip('@')
    
    def add_channel(self, channel: str):
        """Добавляет канал в БД для вступления"""
        if self.db:
            normalized = Config.normalize_channel(channel)
            self.db.add_found_channel(
                channel=normalized,
                title=normalized,
                keyword="manual",
                source="manual",
                can_comment=True,
                min_subs=0
            )
    
    def remove_channel(self, channel: str):
        """Удаляет канал из БД"""
        if self.db:
            normalized = Config.normalize_channel(channel)
            self.db.delete_found_channel(normalized)
    
    async def check_invite_info(self, invite_hash: str) -> Optional[Dict]:
        """
        Проверяет информацию о приватной ссылке без вступления.
        
        Returns:
            Dict с информацией о канале или None при ошибке
        """
        try:
            from telethon.tl.types import ChatInvite, ChatInviteAlready, ChatInvitePeek
            
            clean_hash = self._extract_invite_hash(invite_hash)
            result = await self.client(CheckChatInviteRequest(clean_hash))
            
            # Проверяем тип результата напрямую
            result_type = type(result).__name__
            print(f"[check_invite_info] {invite_hash}: type={result_type}")
            
            # ChatInviteAlready - уже состоим (проверяем ПЕРВЫМ!)
            if isinstance(result, ChatInviteAlready):
                chat = result.chat
                print(f"[check_invite_info] Уже состоим: chat_id={chat.id}, title={chat.title}")
                return {
                    'title': chat.title,
                    'participants_count': getattr(chat, 'participants_count', 0),
                    'request_needed': False,
                    'already_member': True,
                    'channel_id': chat.id
                }
            # ChatInvite - ещё не вступили
            elif isinstance(result, ChatInvite):
                print(f"[check_invite_info] Не состоим: title={result.title}")
                return {
                    'title': result.title,
                    'participants_count': getattr(result, 'participants_count', 0),
                    'request_needed': getattr(result, 'request_needed', False),
                    'already_member': False
                }
            # ChatInvitePeek - можно посмотреть превью
            elif isinstance(result, ChatInvitePeek):
                print(f"[check_invite_info] Peek: chat={result.chat.title}")
                return {
                    'title': result.chat.title,
                    'participants_count': getattr(result.chat, 'participants_count', 0),
                    'request_needed': False,
                    'already_member': False
                }
            else:
                # Fallback на старую логику
                if hasattr(result, 'chat'):
                    chat = result.chat
                    return {
                        'title': chat.title,
                        'participants_count': getattr(chat, 'participants_count', 0),
                        'request_needed': False,
                        'already_member': True,
                        'channel_id': chat.id
                    }
                elif hasattr(result, 'title'):
                    return {
                        'title': result.title,
                        'participants_count': getattr(result, 'participants_count', 0),
                        'request_needed': getattr(result, 'request_needed', False),
                        'already_member': False
                    }
            
            return None
        except InviteHashExpiredError:
            return {'error': 'expired', 'message': 'Ссылка истекла'}
        except Exception as e:
            return {'error': 'unknown', 'message': str(e)}
    
    async def join_channel_async(self, channel: str) -> Tuple[bool, str, Optional[Dict]]:
        """
        Асинхронно вступает в канал (поддерживает приватные каналы с модерацией).
        
        Returns:
            (success: bool, message: str, extra_info: dict)
            extra_info содержит:
            - pending: True если запрос отправлен и ждёт одобрения
            - channel_id: ID канала (если успешно вступили)
            - title: Название канала
        """
        extra_info = {}
        
        try:
            # Определяем тип ссылки
            is_invite_link = (
                channel.startswith('+') or 
                'joinchat/' in channel or 
                't.me/+' in channel or
                'telegram.me/+' in channel
            )
            
            if is_invite_link:
                # Приватный канал с инвайт-хешем
                invite_hash = self._extract_invite_hash(channel)
                
                # Сначала проверяем информацию о канале
                info = await self.check_invite_info(invite_hash)
                if info:
                    if info.get('error') == 'expired':
                        return False, f"Инвайт-ссылка истекла: {channel}", extra_info
                    
                    extra_info['title'] = info.get('title', 'Unknown')
                    
                    if info.get('already_member'):
                        extra_info['channel_id'] = info.get('channel_id')
                        return True, f"Уже состою в канале {info.get('title', channel)}", extra_info
                    
                    # Проверяем нужна ли модерация
                    if info.get('request_needed'):
                        extra_info['request_needed'] = True
                
                # Пытаемся вступить
                try:
                    result = await self.client(ImportChatInviteRequest(invite_hash))
                    
                    # Успешно вступили
                    if hasattr(result, 'chats') and result.chats:
                        chat = result.chats[0]
                        extra_info['channel_id'] = chat.id
                        extra_info['title'] = chat.title
                        
                        # Сохраняем в БД если есть
                        if self.db:
                            self.db.add_found_channel(
                                channel=f"+{invite_hash}",
                                title=chat.title,
                                source="invite_link",
                                subs=getattr(chat, 'participants_count', 0),
                                can_comment=True,
                                min_subs=0
                            )
                            self.db.update_channel_info(f"+{invite_hash}", channel_id=chat.id, title=chat.title)
                        
                        return True, f"Успешно вступил в {chat.title}", extra_info
                    
                    return True, f"Успешно вступил в приватный канал {channel}", extra_info
                    
                except InviteRequestSentError:
                    # Запрос на вступление отправлен (канал с модерацией)
                    extra_info['pending'] = True
                    
                    # Сохраняем в pending_joins
                    if self.db:
                        self.db.add_pending_join(
                            account_id=0,  # Будет обновлено в worker
                            invite_hash=invite_hash,
                            channel_title=extra_info.get('title')
                        )
                    
                    return True, f"Запрос на вступление отправлен: {extra_info.get('title', channel)} (ожидает одобрения админа)", extra_info
                    
            else:
                # Обычный публичный канал
                entity = await self.client.get_entity(channel)
                await self.client(JoinChannelRequest(entity))
                
                extra_info['channel_id'] = entity.id
                extra_info['title'] = getattr(entity, 'title', channel)
                
                return True, f"Успешно вступил в канал {channel}", extra_info
                
        except UserAlreadyParticipantError:
            return True, f"Уже состою в канале {channel}", extra_info
        except InviteHashExpiredError:
            return False, f"Инвайт-ссылка истекла: {channel}", extra_info
        except FloodWaitError as e:
            return False, f"FloodWait {e.seconds}с для {channel}", extra_info
        except UsernameInvalidError:
            return False, f"Неверное имя канала: {channel}", extra_info
        except UsernameNotOccupiedError:
            return False, f"Канал не существует: {channel}", extra_info
        except ChannelPrivateError:
            return False, f"Канал {channel} приватный или недоступен", extra_info
        except Exception as e:
            error_msg = str(e).lower()
            error_upper = str(e).upper()
            
            # Проверяем на заморозку аккаунта для вступлений
            if 'FROZEN' in error_upper:
                extra_info['frozen_join'] = True
                return False, f"Аккаунт заморожен для вступлений: {channel}", extra_info
            
            # Успешный запрос на вступление (требует одобрения) - альтернативное сообщение
            if "successfully requested to join" in error_msg or "request" in error_msg and "sent" in error_msg:
                extra_info['pending'] = True
                
                if self.db:
                    invite_hash = self._extract_invite_hash(channel)
                    self.db.add_pending_join(
                        account_id=0,
                        invite_hash=invite_hash,
                        channel_title=extra_info.get('title')
                    )
                
                return True, f"Запрос на вступление отправлен: {channel} (ожидает одобрения)", extra_info
            
            return False, f"Ошибка при вступлении в {channel}: {str(e)}", extra_info
    
    async def check_pending_status(self, invite_hash: str) -> Tuple[str, Optional[int]]:
        """
        Проверяет статус pending join request.
        
        Returns:
            (status: str, channel_id: int or None)
            status: 'pending', 'approved', 'rejected', 'expired'
        """
        try:
            clean_hash = self._extract_invite_hash(invite_hash)
            info = await self.check_invite_info(clean_hash)
            
            if info:
                if info.get('error') == 'expired':
                    return 'expired', None
                
                if info.get('already_member'):
                    return 'approved', info.get('channel_id')
                
                # Ещё не одобрен
                return 'pending', None
            
            return 'unknown', None
            
        except Exception as e:
            error_msg = str(e).lower()
            if 'expired' in error_msg:
                return 'expired', None
            return 'error', None
    
    def join_channel(self, channel: str) -> tuple[bool, str]:
        """
        Вступает в канал (синхронная версия)
        
        Returns:
            (success: bool, message: str)
        """
        try:
            # Получаем entity канала
            entity = self.client.get_entity(channel)
            
            # Пытаемся вступить
            self.client(JoinChannelRequest(entity))
            
            return True, f"Успешно вступил в канал {channel}"
            
        except FloodWaitError as e:
            # КРИТИЧНО: Правильная обработка FloodWait
            wait_seconds = e.seconds + 10  # Добавляем 10 секунд запаса
            print(f"⚠️ FloodWait: нужно подождать {wait_seconds} секунд")
            time.sleep(wait_seconds)
            # Пытаемся снова после ожидания
            try:
                self.client(JoinChannelRequest(entity))
                return True, f"Успешно вступил в канал {channel} после FloodWait"
            except Exception as retry_e:
                return False, f"Ошибка после FloodWait: {str(retry_e)}"
        except UsernameInvalidError:
            return False, f"Неверное имя канала: {channel}"
        except UsernameNotOccupiedError:
            return False, f"Канал не существует: {channel}"
        except PeerFloodError:
            # Старая ошибка, но на всякий случай обрабатываем
            return False, f"Слишком много действий. Нужна задержка перед вступлением в {channel}"
        except ChannelPrivateError:
            return False, f"Канал {channel} приватный или недоступен"
        except Exception as e:
            return False, f"Ошибка при вступлении в {channel}: {str(e)}"
    
    def join_all_channels(self, owner_id: str = None) -> Dict[str, tuple[bool, str]]:
        """
        Вступает во все каналы из списка с задержками
        
        Returns:
            Dict[channel: str, (success: bool, message: str)]
        """
        results = {}
        
        if not self.channels_to_join:
            if owner_id:
                self.client.send_message(owner_id, "📋 Список каналов для вступления пуст.")
            return results
        
        if owner_id:
            self.client.send_message(
                owner_id, 
                f"🔄 Начинаю вступление в {len(self.channels_to_join)} каналов..."
            )
        
        for i, channel in enumerate(self.channels_to_join, 1):
            print(f"[{i}/{len(self.channels_to_join)}] Вступаю в {channel}...")
            
            success, message = self.join_channel(channel)
            results[channel] = (success, message)
            
            if success:
                print(f"✅ {message}")
            else:
                print(f"❌ {message}")
            
            # Задержка между вступлениями (кроме последнего)
            if i < len(self.channels_to_join):
                delay = random.randint(
                    Config.JOIN_CHANNEL_DELAY_MIN, 
                    Config.JOIN_CHANNEL_DELAY_MAX
                )
                print(f"⏳ Задержка {delay} секунд...")
                time.sleep(delay)
        
        # Отправляем отчет владельцу
        if owner_id:
            success_count = sum(1 for success, _ in results.values() if success)
            report = f"📊 Отчет о вступлении в каналы:\n\n"
            report += f"✅ Успешно: {success_count}/{len(results)}\n"
            report += f"❌ Ошибок: {len(results) - success_count}/{len(results)}\n\n"
            
            for channel, (success, msg) in results.items():
                status = "✅" if success else "❌"
                report += f"{status} {channel}: {msg}\n"
            
            self.client.send_message(owner_id, report)
        
        return results
    
    def get_joined_channels(self) -> List[str]:
        """Получает список каналов, в которые уже вступил аккаунт"""
        try:
            dialogs = self.client.get_dialogs()
            channels = []
            for dialog in dialogs:
                if dialog.is_channel:
                    # Получаем username канала если есть
                    try:
                        if hasattr(dialog.entity, 'username') and dialog.entity.username:
                            channels.append(f"@{dialog.entity.username}")
                        else:
                            # Используем ID если нет username
                            channels.append(str(dialog.entity.id))
                    except:
                        continue
            return channels
        except Exception as e:
            print(f"Ошибка при получении списка каналов: {e}")
            return []

