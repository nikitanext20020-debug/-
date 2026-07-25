import asyncio
import random
import re
from typing import Dict, List, Optional
from telethon import TelegramClient, events
from telethon.tl.types import User, Chat, Channel
from config import Config
from utils.database import Database
from utils.async_http import get_http_client, AsyncHTTPClient


class AutoResponder:
    """Класс для автоматических ответов на личные сообщения через Gemini (RouterAI / OpenAI API format)"""
    
    def __init__(self, client: TelegramClient, api_key: str, db: Database, account_id: int = None):
        self.client = client
        self._api_key_override = api_key
        self.account_id = account_id
        self.db = db
        
        self.channel_link = None  # Ссылка на канал из bio
        self.bot_username = None  # Username бота для определения упоминаний
        self.active_chats = []  # Список активных чатов
        self.chat_last_message_time = {}  # Время последнего сообщения в чате
        self._http_client: Optional[AsyncHTTPClient] = None

    def _get_api_key(self) -> str:
        """Получает актуальный API ключ (из БД или конфига)"""
        if self.db:
            return self.db.get_setting("gemini_api_key", Config.GEMINI_API_KEY) or Config.GEMINI_API_KEY
        return self._api_key_override or Config.GEMINI_API_KEY
    
    def _get_model(self) -> str:
        """Получает актуальную модель (из БД или конфига)"""
        if self.db:
            return self.db.get_setting("gemini_model", Config.GEMINI_MODEL) or Config.GEMINI_MODEL
        return Config.GEMINI_MODEL
    
    def _get_headers(self) -> dict:
        """Получает заголовки с актуальным API ключом"""
        return {
            "Authorization": f"Bearer {self._get_api_key()}",
            "Content-Type": "application/json"
        }
    
    def _get_endpoint(self) -> str:
        """Получает endpoint API"""
        base_url = Config.GEMINI_BASE_URL.rstrip('/')
        return f"{base_url}/chat/completions"

    async def init(self):
        """Асинхронная инициализация данных из Telegram и проверка нейросети"""
        # Создаём HTTP клиент напрямую в async контексте
        self._http_client = AsyncHTTPClient(max_retries=3, base_delay=1.0, timeout=30.0)
        await self._load_channel_link()
        await self._load_bot_username()
        self._load_active_chats()
        await self.check_neural_network()  # Проверяем нейросеть при старте
    
    async def check_neural_network(self):
        """Проверка работы нейросети с записью в лог"""
        try:
            print("🧠 Проверка нейросети...")
            
            payload = {
                "model": self._get_model(),
                "messages": [{"role": "user", "content": "Say 'OK'"}],
                "max_tokens": 5
            }
            
            # Асинхронный вызов через AsyncHTTPClient
            response = await self._http_client.post(
                self._get_endpoint(), 
                json_data=payload, 
                headers=self._get_headers()
            )
            
            if response.success:
                text = response.data['choices'][0]['message']['content'].strip()
                msg = f"✅ Нейросеть работает! Тест: {text}"
                print(msg)
                if self.account_id:
                    self.db.add_log(self.account_id, "info", msg)
            else:
                raise Exception(f"HTTP {response.status}: {response.data}")

        except Exception as e:
            err_msg = f"❌ Нейросеть НЕ работает: {e}"
            print(err_msg)
            if self.account_id:
                self.db.add_log(self.account_id, "error", err_msg)
    
    async def _load_channel_link(self):
        """Извлекает ссылку на канал из описания профиля (Асинхронно)"""
        try:
            me = await self.client.get_me()
            full_me = await self.client.get_entity(me)
            if hasattr(full_me, 'about') and full_me.about:
                about = full_me.about
                patterns = [
                    r'@(\w+)',
                    r't\.me/(\w+)',
                    r'https?://t\.me/(\w+)',
                    r'https?://telegram\.me/(\w+)'
                ]
                for pattern in patterns:
                    match = re.search(pattern, about)
                    if match:
                        self.channel_link = f"@{match.group(1)}"
                        break
            if not self.channel_link:
                self.channel_link = "@your_anime_channel"
        except Exception as e:
            print(f"Ошибка при загрузке ссылки на канал: {e}")
            self.channel_link = "@your_anime_channel"
    
    async def _load_bot_username(self):
        """Загружает username бота (Асинхронно)"""
        try:
            me = await self.client.get_me()
            if hasattr(me, 'username') and me.username:
                self.bot_username = me.username.lower()
        except Exception as e:
            print(f"Ошибка при загрузке username бота: {e}")
    
    def _load_active_chats(self):
        """Загружает список активных чатов (Синхронно из файла)"""
        try:
            self.active_chats = Config.load_channels_from_file(Config.CHAT_ACTIVE_CHATS_FILE)
        except Exception as e:
            self.active_chats = []
    
    async def is_unknown_user(self, user_id: int) -> bool:
        """Проверяет, является ли пользователь незнакомым (Асинхронно)"""
        if not Config.AUTORESPONDER_ONLY_UNKNOWN:
            return True
        try:
            entity = await self.client.get_entity(user_id)
            if isinstance(entity, User):
                # Если is_contact=True, значит мы его знаем, возвращаем False (не незнакомец)
                # НО! В telegram 'contact' это тот кого МЫ добавили. 
                # Если нам пишет рандом, он не контакт.
                is_contact = getattr(entity, 'contact', False)
                return not is_contact
            return True
        except:
            return True
    
    async def generate_response(self, user_id: int, user_message: str) -> str:
        """Генерирует ответ через Gemini (Асинхронный вызов через AsyncHTTPClient)"""
        try:
            # Проверяем что HTTP клиент инициализирован
            if self._http_client is None:
                self._http_client = AsyncHTTPClient(max_retries=3, base_delay=1.0, timeout=30.0)
            
            # Лимит сообщений в чат/ЛС: не более 1 сообщения раз в 5 минут
            recent_count = self.db.get_recent_messages_count(user_id, minutes=5)
            if recent_count >= 1:
                print(f"⏳ Лимит сообщений для {user_id} (1 раз в 5мин). Пропускаю.")
                return ""  # Пустой ответ

            history = self.db.get_dialog_history(
                user_id, 
                limit=10, 
                max_age_hours=Config.AUTORESPONDER_DIALOG_TIMEOUT_HOURS
            )
            
            system_instruction = Config.GIRL_PERSONA_SYSTEM
            
            # Добавляем "остринку" если включен соответствующий режим
            if self.db.get_setting("lusty_mode", False):
                system_instruction += "\nВАЖНО: Включи режим игривого флирта. Твои ответы должны быть с легким сексуальным подтекстом, но не пошлыми. Используй смайлики, намеки.\n"
            
            msg_count = self.db.get_message_count(user_id)
            if msg_count >= Config.AUTORESPONDER_MIN_MESSAGES_BEFORE_PROMOTE:
                system_instruction += f"\n(ВАЖНО: Пора ненавязчиво упомянуть мой канал: {self.channel_link})"

            messages = [{"role": "system", "content": system_instruction}]
            
            for msg in history:
                role = "assistant" if msg['role'] == 'assistant' else "user"
                messages.append({"role": role, "content": msg['content']})
            
            messages.append({"role": "user", "content": user_message})

            payload = {
                "model": self._get_model(),
                "messages": messages,
                "max_tokens": 70,
                "temperature": 0.8
            }

            response = await self._http_client.post(
                self._get_endpoint(), 
                json_data=payload, 
                headers=self._get_headers()
            )
            
            if response.success:
                reply = response.data['choices'][0]['message']['content'].strip()
                reply = reply.replace('"', '').replace("'", "")
                
                self.db.save_message(user_id, 'user', user_message)
                self.db.save_message(user_id, 'assistant', reply)
                return reply
            else:
                self._log(f"❌ Ошибка API Gemini ({response.status}): {response.data}", "error")
                return ""  # Не отвечаем при ошибке API

        except Exception as e:
            self._log(f"❌ Ошибка автоответчика: {e}", "error")
            return ""  # Не отвечаем при ошибке - лучше молчать чем писать шаблон

    def is_bot_mentioned(self, message_text: str) -> bool:
        """Проверяет упоминание бота"""
        if not self.bot_username or not message_text:
            return False
        text_lower = message_text.lower()
        return f"@{self.bot_username}" in text_lower

    def _log(self, message: str, level: str = "info"):
        """Логирует сообщение в БД"""
        if self.account_id and self.db:
            self.db.add_log(self.account_id, level, message)
        print(message)

    async def generate_chat_response(self, chat_context: str, chat_name: str = "") -> str:
        """
        Генерирует ответ для группового чата через нейросеть.
        НЕ сохраняет историю диалогов (в отличие от generate_response для ЛС).
        
        Args:
            chat_context: Контекст чата (последние сообщения)
            chat_name: Название чата для контекста
            
        Returns:
            Сгенерированный ответ или пустая строка при ошибке
        """
        try:
            # Проверяем что HTTP клиент инициализирован
            if self._http_client is None:
                self._http_client = AsyncHTTPClient(max_retries=3, base_delay=1.0, timeout=30.0)
            
            system_prompt = """Ты обычный участник чата. Отвечай коротко и естественно, как живой человек.
Не используй формальный язык. Пиши 1-2 предложения максимум.
Отвечай по теме обсуждения, не уходи в сторону."""
            
            # Добавляем "остринку" если включен соответствующий режим
            if self.db.get_setting("lusty_mode", False):
                system_prompt += "\nМожешь добавить легкий флирт или игривость в ответ, но не переборщи."
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": chat_context}
            ]

            payload = {
                "model": self._get_model(),
                "messages": messages,
                "max_tokens": 50,  # Короткие ответы для чатов
                "temperature": 0.9  # Больше разнообразия
            }

            response = await self._http_client.post(
                self._get_endpoint(), 
                json_data=payload, 
                headers=self._get_headers()
            )
            
            if response.success:
                reply = response.data['choices'][0]['message']['content'].strip()
                reply = reply.replace('"', '').replace("'", "")
                return reply
            else:
                self._log(f"❌ Ошибка API для чата ({response.status}): {response.data}", "error")
                return ""

        except Exception as e:
            self._log(f"❌ Ошибка генерации для чата: {e}", "error")
            return ""

    async def handle_new_message(self, event: events.NewMessage.Event):
        """Обрабатывает входящее сообщение (Асинхронно)"""
        if not Config.AUTORESPONDER_ENABLED: 
            return
        message = event.message
        if message.out or not event.is_private: 
            return

        sender = await event.get_sender()
        if not sender or not isinstance(sender, User): 
            return
        
        # Игнорируем себя (Избранное / Saved Messages)
        if getattr(sender, 'is_self', False):
            return
            
        # Игнорируем служебные аккаунты Telegram (например 777000)
        if getattr(sender, 'id', None) == 777000:
            return
            
        try:
            me = await self.client.get_me()
            if me and sender.id == me.id:
                return
        except Exception:
            pass

        # ФИЛЬТРАЦИЯ БОТОВ
        if sender.bot:
            return
        
        # Дополнительная проверка - username заканчивается на 'bot'
        if sender.username and sender.username.lower().endswith('bot'):
            return

        user_id = sender.id
        sender_name = sender.first_name or sender.username or str(user_id)
        
        # Статистика входящих
        if self.account_id:
            self.db.increment_stat(self.account_id, 'incoming_messages')

        # Проверка на "незнакомца"
        is_unknown = await self.is_unknown_user(user_id)
        if not is_unknown:
            return
        
        self.db.increment_message_count(user_id)
        
        msg_preview = (message.text or "")[:30] + "..." if len(message.text or "") > 30 else (message.text or "")
        self._log(f"📩 ЛС от {sender_name}: {msg_preview}")

        # Генерируем ответ (асинхронно)
        reply = await self.generate_response(user_id, message.text or "")
        
        if not reply:
            return  # Лимит сработал или ошибка

        # Плавная задержка (Асинхронно)
        await asyncio.sleep(random.randint(Config.RESPONSE_DELAY_MIN, Config.RESPONSE_DELAY_MAX))
        
        try:
            await self.client.send_message(user_id, reply, reply_to=message.id)
            reply_preview = reply[:30] + "..." if len(reply) > 30 else reply
            self._log(f"💬 Ответ в ЛС для {sender_name}: {reply_preview}")
        except Exception as e:
            self._log(f"❌ Ошибка отправки ЛС: {e}", "error")

    def start_listening(self):
        """Запускает прослушивание событий (Асинхронный хендлер)"""
        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event):
            await self.handle_new_message(event)
        print("👂 Автоответчик на Gemini запущен...")
