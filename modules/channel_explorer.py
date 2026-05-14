"""
Модуль для глубокого поиска и исследования каналов (Channel Discovery)
"""
import re
import asyncio
from typing import List, Dict, Set
from telethon import functions, types
from telethon import TelegramClient
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.errors import RPCError
from config import Config
from utils.database import Database

class ChannelExplorer:
    def __init__(self, client: TelegramClient, db: Database, account_id: int = None):
        self.client = client
        self.db = db
        self.account_id = account_id
        self._stopped = False  # Флаг для остановки
        # Регулярка для поиска ссылок на каналы и чаты
        self.tg_link_pattern = re.compile(r'(?:t\.me/|@)([a-zA-Z0-9_]{5,32})')
        # Паттерн для joinchat ссылок
        self.joinchat_pattern = re.compile(r't\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)')
        
        # Стоп-слова для фильтрации
        self.stop_words = {'telegram', 'joinchat', 'addstickers', 'bot', 'share', 'proxy', 'socks', 'vote'}

    def stop(self):
        """Останавливает explorer"""
        self._stopped = True

    def _is_client_connected(self) -> bool:
        """Проверяет, подключён ли клиент"""
        if self._stopped:
            return False
        try:
            return self.client and self.client.is_connected()
        except:
            return False

    def _log(self, message: str, level: str = "info"):
        """Логирует сообщение в БД"""
        if self.account_id and self.db:
            self.db.add_log(self.account_id, level, message)
        print(message)

    async def check_channel_viability(self, peer) -> Dict:
        """Проверяет канал на соответствие критериям (сабы, комменты)"""
        # Проверяем подключение клиента
        if not self._is_client_connected():
            return {"ok": False, "reason": "Client disconnected"}
        
        try:
            full = await self.client(GetFullChannelRequest(peer))
            subs = full.full_chat.participants_count
            
            # Фильтр по сабам
            if subs < Config.SEARCH_MIN_SUBSCRIBERS:
                return {"ok": False, "reason": f"Мало сабов ({subs})"}

            # Проверка на комментарии
            can_comment = False
            avg_views = 0
            try:
                # Проверяем подключение перед запросом
                if not self._is_client_connected():
                    return {"ok": False, "reason": "Client disconnected"}
                
                # Берем последний пост для проверки обсуждения
                msgs = await self.client.get_messages(peer, limit=20)
                if msgs:
                    # Считаем активность (ср. просмотры)
                    views = [m.views for m in msgs if m.views]
                    avg_views = sum(views) / len(views) if views else 0
                    
                    if avg_views < Config.SEARCH_MIN_AVG_VIEWS:
                        return {"ok": False, "reason": f"Низкие просмотры ({avg_views:.0f})"}
                    
                    # Проверяем возможность комментирования одного из последних постов
                    for m in msgs[:5]:
                        if not self._is_client_connected():
                            return {"ok": False, "reason": "Client disconnected"}
                        try:
                            await self.client(GetDiscussionMessageRequest(peer=peer, msg_id=m.id))
                            # Если исключение не вылетело, значит комменты есть
                            can_comment = True
                            break
                        except Exception as e:
                            # 'CHANNEL_PRIVATE', 'CHAT_FORBIDDEN' и т.д. означают что комментов нет или нас там нет
                            continue
                else:
                    avg_views = 0
            except Exception as e:
                # Если не удалось получить сообщения, возможно канал приватный
                return {"ok": False, "reason": f"Ошибка чтения постов ({e})"}

            return {
                "ok": True, 
                "subs": subs, 
                "avg_views": avg_views, 
                "can_comment": can_comment,
                "title": full.chats[0].title
            }
        except Exception as e:
             # Ловим глобальные ошибки (например приватный канал)
            return {"ok": False, "reason": str(e)}

    async def discover_from_posts(self, source_channel: str, limit: int = 50):
        """Парсит посты канала в поисках ссылок на другие каналы"""
        # Проверяем подключение клиента
        if not self._is_client_connected():
            self._log(f"⚠️ Клиент отключён, пропускаю исследование {source_channel}", "warning")
            return 0
        
        self._log(f"🕵️ Исследование канала {source_channel}...")
        found_links: Set[str] = set()
        
        try:
            entity = await self.client.get_entity(source_channel)
            async for msg in self.client.iter_messages(entity, limit=limit):
                # Проверяем остановку и подключение
                if self._stopped or not self._is_client_connected():
                    self._log(f"⚠️ Исследование {source_channel} прервано (клиент отключён)", "warning")
                    return 0
                
                # Парсим текст поста
                if msg.text:
                    matches = self.tg_link_pattern.findall(msg.text)
                    for m in matches:
                        if m.lower() not in self.stop_words:
                            found_links.add(m)
                
                # Парсим кнопки (часто реклама в кнопках)
                if msg.reply_markup:
                    try:
                        for row in msg.reply_markup.rows:
                            for button in row.buttons:
                                if hasattr(button, 'url') and button.url:
                                    url_matches = self.tg_link_pattern.findall(button.url)
                                    for m in url_matches:
                                        if m.lower() not in self.stop_words:
                                            found_links.add(m)
                    except: pass
                
                # Парсим пересланные сообщения
                if msg.fwd_from and msg.fwd_from.from_id:
                    if isinstance(msg.fwd_from.from_id, types.PeerChannel):
                        try:
                            if self._is_client_connected():
                                fwd_entity = await self.client.get_entity(msg.fwd_from.from_id)
                                if hasattr(fwd_entity, 'username') and fwd_entity.username:
                                    found_links.add(fwd_entity.username)
                        except: pass
                
                # Парсим entities (ссылки в тексте)
                if msg.entities:
                    for ent in msg.entities:
                        if hasattr(ent, 'url') and ent.url:
                            url_matches = self.tg_link_pattern.findall(ent.url)
                            for m in url_matches:
                                if m.lower() not in self.stop_words:
                                    found_links.add(m)

            self._log(f"🔗 Найдено {len(found_links)} ссылок в {source_channel}")
            
            # Получаем минимальное количество подписчиков для каналов из настроек
            min_channel_subs = self.db.get_setting("min_channel_subs", 20000) if self.db else 20000
            
            newly_discovered = 0
            for link in found_links:
                # Проверяем остановку и подключение
                if self._stopped or not self._is_client_connected():
                    self._log(f"⚠️ Обработка ссылок прервана (клиент отключён)", "warning")
                    break
                
                try:
                    peer = await self.client.get_entity(link)
                    if not isinstance(peer, (types.Channel, types.Chat)): continue
                    
                    res = await self.check_channel_viability(peer)
                    
                    # Если клиент отключился во время проверки
                    if res.get('reason') == 'Client disconnected':
                        break
                    
                    # Пропускаем маленькие каналы
                    if res.get('subs', 0) > 0 and res.get('subs', 0) < min_channel_subs:
                        continue
                    
                    if res.get('ok'):
                        is_new = self.db.add_found_channel(
                            channel=link.lstrip('@'),
                            title=res['title'],
                            source=f"link_from_{source_channel}",
                            subs=res['subs'],
                            views=res['avg_views'],
                            can_comment=res['can_comment'],
                            min_subs=min_channel_subs
                        )
                        if res['can_comment'] and is_new:
                            self._log(f"🌟 Найден канал: @{link} ({res['subs']} сабов, комменты открыты)")
                            newly_discovered += 1
                    
                    await asyncio.sleep(2)
                except Exception as e:
                    continue
            
            return newly_discovered
        except Exception as e:
            self._log(f"❌ Ошибка исследования {source_channel}: {e}", "error")
            return 0

    async def discover_from_comments(self, channel: str, post_id: int, limit: int = 20):
        """Парсит комментарии под постом в поисках ссылок"""
        # Проверяем подключение клиента
        if not self._is_client_connected():
            return set()
        
        found_links: Set[str] = set()
        
        try:
            entity = await self.client.get_entity(channel)
            discussion = await self.client(GetDiscussionMessageRequest(peer=entity, msg_id=post_id))
            
            if discussion and discussion.messages:
                for msg in discussion.messages[:limit]:
                    if msg.text:
                        matches = self.tg_link_pattern.findall(msg.text)
                        for m in matches:
                            if m.lower() not in self.stop_words:
                                found_links.add(m)
            
            return found_links
        except:
            return set()

    async def run_discovery_cycle(self):
        """Запускает полный цикл поиска по активным каналам"""
        # Проверяем подключение клиента
        if not self._is_client_connected():
            self._log("⚠️ Клиент отключён, discovery cycle отменён", "warning")
            return 0
        
        # Берём каналы из БД
        all_channels = self.db.get_found_channels(limit=20, only_open_comments=True)
        seeds = [ch['channel'] for ch in all_channels[:10]]
        
        total_new = 0
        for seed in seeds:
            # Проверяем остановку и подключение перед каждым каналом
            if self._stopped or not self._is_client_connected():
                self._log("⚠️ Discovery cycle прерван (клиент отключён)", "warning")
                break
            total_new += await self.discover_from_posts(seed)
        
        return total_new
