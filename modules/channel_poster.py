"""
Модуль публикации контента в собственные Telegram каналы.
Поддерживает прямую отправку, генерацию через AI, кросс-постинг и очередь постов.
"""
import asyncio
import random
import io
import os
from typing import Optional, Dict, List

from telethon import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError, ChatWriteForbiddenError, ChannelPrivateError

from config import Config
from utils.database import Database
from utils.async_http import AsyncHTTPClient


class ChannelPoster:
    """Публикация контента в собственные Telegram каналы"""

    def __init__(self, client: TelegramClient, db: Database, account_id: int):
        self.client = client
        self.db = db
        self.account_id = account_id
        self._http_client: Optional[AsyncHTTPClient] = None

    def _log(self, message: str, level: str = "info"):
        """Логирует сообщение"""
        if self.db and self.account_id:
            try:
                self.db.add_log(self.account_id, level, message)
            except:
                pass
        print(f"[ChannelPoster] {message}")

    def _get_http_client(self) -> AsyncHTTPClient:
        """Получает HTTP клиент (создает если нужно)"""
        if self._http_client is None:
            self._http_client = AsyncHTTPClient(max_retries=3, base_delay=1.0, timeout=30.0)
        return self._http_client

    async def _generate_content(self, prompt: str) -> str:
        """Генерирует контент через Gemini AI"""
        try:
            api_key = self.db.get_setting("gemini_api_key", Config.GEMINI_API_KEY) if self.db else Config.GEMINI_API_KEY
            model = self.db.get_setting("gemini_model", Config.GEMINI_MODEL) if self.db else Config.GEMINI_MODEL

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            base_url = Config.GEMINI_BASE_URL.rstrip('/')
            endpoint = f"{base_url}/chat/completions"

            payload = {
                "model": model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.8
            }

            client = self._get_http_client()
            response = await client.post(endpoint, json_data=payload, headers=headers)

            if response.success and isinstance(response.data, dict):
                content = response.data.get('choices', [{}])[0].get('message', {}).get('content', '')
                return content.strip()
            else:
                self._log(f"Ошибка API генерации ({response.status}): {response.data}", "error")
                return ""

        except Exception as e:
            self._log(f"Ошибка генерации контента: {e}", "error")
            return ""

    async def post_message(self, channel_id: int, text: str, media_path: str = None,
                           media_type: str = None, format_type: str = "md") -> Optional[object]:
        """
        Отправляет сообщение в канал.

        Args:
            channel_id: ID канала
            text: Текст сообщения
            media_path: Путь к медиафайлу (опционально)
            media_type: Тип медиа (photo/video/file)
            format_type: Формат текста ('md' или 'html')

        Returns:
            Объект отправленного сообщения или None
        """
        try:
            parse_mode = 'md' if format_type == 'md' else 'html'

            if media_path and os.path.exists(media_path):
                msg = await self.client.send_file(
                    channel_id,
                    media_path,
                    caption=text,
                    parse_mode=parse_mode
                )
            else:
                msg = await self.client.send_message(
                    channel_id,
                    text,
                    parse_mode=parse_mode
                )

            self._log(f"Сообщение отправлено в канал {channel_id}")
            return msg

        except FloodWaitError as e:
            self._log(f"FloodWait при отправке: ждем {e.seconds}с", "warning")
            await asyncio.sleep(e.seconds)
            return None
        except ChatWriteForbiddenError:
            self._log(f"Нет прав на запись в канал {channel_id}", "error")
            return None
        except ChannelPrivateError:
            self._log(f"Канал {channel_id} приватный или недоступен", "error")
            return None
        except Exception as e:
            self._log(f"Ошибка отправки сообщения: {e}", "error")
            return None

    async def post_message_with_bytes(self, channel_id: int, text: str, media_bytes: bytes = None,
                                      media_type: str = None, format_type: str = "md") -> Optional[object]:
        """
        Отправляет сообщение с медиа из байтов.

        Args:
            channel_id: ID канала
            text: Текст сообщения
            media_bytes: Байты медиафайла
            media_type: Тип медиа (photo/video/file)
            format_type: Формат текста ('md' или 'html')

        Returns:
            Объект отправленного сообщения или None
        """
        try:
            parse_mode = 'md' if format_type == 'md' else 'html'

            if media_bytes:
                file_name = "media.jpg" if media_type == "photo" else "media.mp4" if media_type == "video" else "file"
                msg = await self.client.send_file(
                    channel_id,
                    io.BytesIO(media_bytes),
                    caption=text,
                    parse_mode=parse_mode,
                    file_name=file_name
                )
            else:
                msg = await self.client.send_message(
                    channel_id,
                    text,
                    parse_mode=parse_mode
                )

            self._log(f"Сообщение отправлено в канал {channel_id}")
            return msg

        except FloodWaitError as e:
            self._log(f"FloodWait при отправке: ждем {e.seconds}с", "warning")
            await asyncio.sleep(e.seconds)
            return None
        except ChatWriteForbiddenError:
            self._log(f"Нет прав на запись в канал {channel_id}", "error")
            return None
        except ChannelPrivateError:
            self._log(f"Канал {channel_id} приватный или недоступен", "error")
            return None
        except Exception as e:
            self._log(f"Ошибка отправки сообщения с байтами: {e}", "error")
            return None

    async def generate_and_post(self, channel_id: int, topic: str) -> Optional[object]:
        """
        Генерирует пост через AI и публикует в канал.

        Args:
            channel_id: ID канала
            topic: Тема поста

        Returns:
            Объект отправленного сообщения или None
        """
        prompt = (
            f"Write an engaging Telegram channel post about: {topic}. "
            f"Write in Russian. Keep under 300 words. Use markdown formatting."
        )
        text = await self._generate_content(prompt)

        if text:
            return await self.post_message(channel_id, text)

        self._log("Не удалось сгенерировать пост", "warning")
        return None

    async def cross_post(self, source_channel, target_channel_id: int, message_id: int) -> Optional[object]:
        """
        Пересылает сообщение из одного канала в другой.

        Args:
            source_channel: Исходный канал (ID или entity)
            target_channel_id: ID целевого канала
            message_id: ID сообщения для пересылки

        Returns:
            Пересланное сообщение или None
        """
        try:
            msg = await self.client.forward_messages(target_channel_id, message_id, source_channel)
            self._log(f"Сообщение {message_id} переслано в канал {target_channel_id}")
            return msg
        except FloodWaitError as e:
            self._log(f"FloodWait при пересылке: ждем {e.seconds}с", "warning")
            await asyncio.sleep(e.seconds)
            return None
        except ChatWriteForbiddenError:
            self._log(f"Нет прав на запись в канал {target_channel_id}", "error")
            return None
        except Exception as e:
            self._log(f"Ошибка кросс-постинга: {e}", "error")
            return None

    async def process_queue(self):
        """Обрабатывает очередь постов: отправляет pending посты."""
        try:
            posts = self.db.get_pending_posts(self.account_id)
            self._log(f"В очереди {len(posts)} постов для отправки")

            for post in posts:
                try:
                    msg = await self.post_message(
                        post['channel_id'],
                        post['content_text'],
                        post.get('media_path'),
                        post.get('media_type'),
                        post.get('format_type', 'md')
                    )

                    if msg:
                        self.db.mark_post_sent(post['id'])
                    else:
                        self.db.mark_post_failed(post['id'])
                        self._log(f"Пост {post['id']} не отправлен", "error")

                except Exception as e:
                    self.db.mark_post_failed(post['id'])
                    self._log(f"Ошибка отправки поста {post['id']}: {e}", "error")

                await asyncio.sleep(random.randint(5, 15))

        except Exception as e:
            self._log(f"Ошибка обработки очереди: {e}", "error")

    async def add_to_queue(self, channel_id: int, text: str, media_path: str = None,
                           media_type: str = None, format_type: str = "md",
                           scheduled_at: str = None) -> int:
        """
        Добавляет пост в очередь.

        Args:
            channel_id: ID канала
            text: Текст поста
            media_path: Путь к медиа
            media_type: Тип медиа
            format_type: Формат текста
            scheduled_at: Время публикации (ISO format)

        Returns:
            ID добавленного поста
        """
        return self.db.add_post_to_queue(
            self.account_id, channel_id, text,
            media_path, media_type, format_type, scheduled_at
        )

    async def get_channel_posts(self, channel_id: int, limit: int = 10) -> List:
        """
        Получает последние посты из канала.

        Args:
            channel_id: ID канала
            limit: Количество постов

        Returns:
            Список словарей с информацией о постах
        """
        try:
            messages = await self.client.get_messages(channel_id, limit=limit)
            posts = []
            for msg in messages:
                post_data = {
                    "id": msg.id,
                    "text": msg.text or "",
                    "date": msg.date.isoformat() if msg.date else None,
                    "media": msg.media is not None,
                    "media_type": type(msg.media).__name__ if msg.media else None
                }
                posts.append(post_data)
            return posts
        except Exception as e:
            self._log(f"Ошибка получения постов канала {channel_id}: {e}", "error")
            return []

    async def close(self):
        """Закрывает HTTP клиент"""
        if self._http_client:
            await self._http_client.close()
            self._http_client = None
