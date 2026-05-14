"""
Модуль поиска каналов и постов по ключевым словам (с фильтрацией)
"""
import asyncio
from typing import List, Dict, Optional
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, ChannelFull
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from config import Config
from utils.database import Database


class KeywordSearch:
    """Класс для поиска каналов и постов по ключевым словам с жесткой фильтрацией"""
    
    def __init__(self, client: TelegramClient, db: Database):
        self.client = client
        self.db = db
        self.keywords = Config.load_keywords()
        self._stopped = False  # Флаг для остановки
    
    def stop(self):
        """Останавливает поиск"""
        self._stopped = True
    
    def _is_client_connected(self) -> bool:
        """Проверяет, подключён ли клиент"""
        if self._stopped:
            return False
        try:
            return self.client and self.client.is_connected()
        except:
            return False
    
    async def check_channel_activity(self, channel_entity) -> bool:
        """
        Проверяет активность канала: подписчики > 5000 и средние просмотры
        """
        # Проверяем подключение клиента
        if not self._is_client_connected():
            print("  ⚠️ Клиент отключён, пропускаю проверку")
            return False
        
        try:
            # Получаем полную информацию о канале (Асинхронно)
            full_channel = await self.client(GetFullChannelRequest(channel_entity))
            subs_count = 0
            
            # Различные способы получения счетчика участников в разных версиях Telethon
            if hasattr(full_channel.full_chat, 'participants_count'):
                subs_count = full_channel.full_chat.participants_count
            elif hasattr(full_channel.full_chat, 'stats'):
                # В некоторых случаях инфа в stats
                subs_count = getattr(full_channel.full_chat.stats, 'participants_count', 0)
            
            # 1. Фильтр по подписчикам
            if subs_count < Config.SEARCH_MIN_SUBSCRIBERS:
                print(f"  ❌ Мало подписчиков: {subs_count} (нужно {Config.SEARCH_MIN_SUBSCRIBERS})")
                return False
            
            # Проверяем подключение перед следующим запросом
            if not self._is_client_connected():
                return False
            
            # 2. Фильтр по просмотрам (последние N постов)
            messages = await self.client.get_messages(channel_entity, limit=Config.SEARCH_CHECK_LAST_POSTS)
            if not messages:
                print("  ❌ В канале нет постов")
                return False
            
            total_views = 0
            count = 0
            for msg in messages:
                if hasattr(msg, 'views') and msg.views:
                    total_views += msg.views
                    count += 1
            
            if count == 0:
                print("  ❌ Не удалось получить просмотры (возможно, канал скрыт)")
                return False
            
            avg_views = total_views / count
            if avg_views < Config.SEARCH_MIN_AVG_VIEWS:
                print(f"  ❌ Низкая активность: {avg_views:.0f} просмотров (нужно {Config.SEARCH_MIN_AVG_VIEWS})")
                return False
            
            print(f"  ✅ Канал прошел проверку: {subs_count} сабов, {avg_views:.0f} ср. просмотров")
            return True
            
        except Exception as e:
            print(f"  ❌ Ошибка при проверке активности: {e}")
            return False

    async def search_channels_by_keywords(self, keywords: Optional[List[str]] = None) -> List[Dict]:
        """
        Ищет каналы и фильтрует их по качеству
        """
        if keywords is None:
            keywords = self.keywords
        
        if not keywords:
            print("⚠️ Нет ключевых слов для поиска")
            return []
        
        found_channels = []
        
        for keyword in keywords:
            print(f"🔍 Ищу каналы по ключевому слову: {keyword}")
            try:
                # Глобальный поиск через Telegram (Асинхронно)
                result = await self.client(SearchRequest(q=keyword, limit=20))
                
                # Получаем минимальное количество подписчиков для каналов из настроек
                min_channel_subs = self.db.get_setting("min_channel_subs", 20000) if self.db else 20000
                
                for peer in result.chats:
                    if hasattr(peer, 'username') and peer.username:
                        channel_username = f"@{peer.username}"
                        
                        # Пропускаем уже найденные в этом сеансе
                        if channel_username in [ch.get('username') for ch in found_channels]:
                            continue
                        
                        # Пропускаем маленькие каналы
                        subs_count = getattr(peer, 'participants_count', 0)
                        if subs_count > 0 and subs_count < min_channel_subs:
                            continue

                        print(f"  🔎 Проверяю канал: {channel_username} ({subs_count} подписчиков)...")
                        
                        # Кросс-фильтрация по активности
                        if await self.check_channel_activity(peer):
                            channel_info = {
                                'username': channel_username,
                                'id': peer.id,
                                'title': getattr(peer, 'title', 'Без названия'),
                                'keyword': keyword,
                                'subs': subs_count
                            }
                            found_channels.append(channel_info)
                            # Сохраняем в БД как найденный
                            self.db.add_found_channel(
                                channel_username, 
                                channel_info['title'], 
                                keyword,
                                subs=subs_count,
                                min_subs=min_channel_subs
                            )
                        
                        # Задержка против флуда (Асинхронно)
                        await asyncio.sleep(4)
                
            except Exception as e:
                print(f"  ❌ Ошибка при поиске по '{keyword}': {e}")
                await asyncio.sleep(5)
        
        return found_channels
    
    async def search_all(self, auto_add_to_active: bool = True) -> Dict:
        """
        Выполняет полный поиск: находит качественные каналы и добавляет их в ротацию
        """
        if not Config.SEARCH_ENABLED:
            return {'channels': [], 'posts': [], 'added_channels': []}
        
        print("🔍 Начинаю поиск качественных аниме каналов...")
        channels = await self.search_channels_by_keywords()
        
        added_channels = []
        if channels:
            for ch in channels:
                username = ch['username']
                # Добавляем в БД
                is_new = self.db.add_found_channel(
                    channel=username,
                    title=ch.get('title', username),
                    keyword="search",
                    source="search",
                    subs=ch.get('participants_count', 0),
                    can_comment=True,
                    min_subs=0
                )
                if is_new:
                    self.db.update_channel_status(username, 'active')
                    added_channels.append(username)
                    print(f"  ➕ Добавлен качественный канал: {username}")
            
            if added_channels:
                print(f"✅ Добавлено {len(added_channels)} новых аниме каналов в БД")
        
        return {
            'channels': channels,
            'posts': [], # Посты проверяются в основном цикле main_new.py
            'added_channels': added_channels
        }
