"""
Модуль создания и настройки собственных Telegram каналов.
Поддерживает создание канала, установку аватара, прогрев постами через Gemini AI.
"""
import asyncio
import random
import io
import os
from typing import Optional, Dict, List

from telethon import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError, ChatWriteForbiddenError, ChannelPrivateError
from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest, EditPhotoRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.types import InputChatUploadedPhoto

from config import Config
from utils.database import Database
from utils.async_http import AsyncHTTPClient


class ChannelCreator:
    """Создание и настройка собственных Telegram каналов"""

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
        print(f"[ChannelCreator] {message}")

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

    async def create_channel(self, title: str, about: str = "", username_base: str = None) -> Optional[object]:
        """
        Создает новый Telegram канал.

        Args:
            title: Название канала
            about: Описание канала
            username_base: Базовый username для канала

        Returns:
            Channel entity или None при ошибке
        """
        try:
            result = await self.client(CreateChannelRequest(
                title=title,
                about=about,
                megagroup=False,
                broadcast=True
            ))

            channel = result.chats[0]
            self._log(f"Канал создан: {title} (ID: {channel.id})")

            if username_base:
                await self._set_channel_username(channel, username_base)

            return channel

        except FloodWaitError as e:
            self._log(f"FloodWait при создании канала: ждем {e.seconds}с", "warning")
            await asyncio.sleep(e.seconds)
            return None
        except Exception as e:
            error_name = type(e).__name__
            if error_name == "ChannelsTooMuchError":
                self._log("Достигнут лимит количества каналов", "error")
                return None
            self._log(f"Ошибка создания канала: {e}", "error")
            return None

    async def _set_channel_username(self, channel, username_base: str) -> Optional[str]:
        """
        Устанавливает username для канала с retry логикой.

        Args:
            channel: Entity канала
            username_base: Базовый username

        Returns:
            Установленный username или None
        """
        try:
            await self.client(UpdateUsernameRequest(channel=channel, username=username_base))
            self._log(f"Username установлен: @{username_base}")
            return username_base
        except Exception:
            pass

        # Пробуем вариации с случайными цифрами
        for _ in range(5):
            suffix = random.randint(100, 9999)
            variation = f"{username_base}{suffix}"
            try:
                await self.client(UpdateUsernameRequest(channel=channel, username=variation))
                self._log(f"Username установлен: @{variation}")
                return variation
            except Exception:
                continue

        self._log(f"Не удалось установить username для канала (base: {username_base})", "warning")
        return None

    async def set_channel_avatar(self, channel, image_bytes: bytes) -> bool:
        """
        Устанавливает аватар канала.

        Args:
            channel: Entity канала
            image_bytes: Байты изображения

        Returns:
            True если успешно
        """
        try:
            file = await self.client.upload_file(
                io.BytesIO(image_bytes),
                file_name="channel_avatar.jpg"
            )
            await self.client(EditPhotoRequest(
                channel=channel,
                photo=InputChatUploadedPhoto(file=file)
            ))
            self._log("Аватар канала установлен")
            return True
        except Exception as e:
            self._log(f"Ошибка установки аватара канала: {e}", "error")
            return False

    async def add_channel_to_bio(self, channel_username: str) -> bool:
        """
        Добавляет ссылку на канал в bio профиля.

        Args:
            channel_username: Username канала (без @)

        Returns:
            True если успешно
        """
        try:
            me = await self.client.get_me()
            full = await self.client(GetFullUserRequest(me))
            current_bio = full.full_user.about or ""

            new_link = f"t.me/{channel_username}"

            if new_link in current_bio:
                return True

            if current_bio:
                new_bio = f"{current_bio} | {new_link}"
            else:
                new_bio = new_link

            # Telegram bio limit is 70 chars
            new_bio = new_bio[:70]

            await self.client(UpdateProfileRequest(about=new_bio))
            self._log(f"Ссылка на канал добавлена в bio: {new_link}")
            return True
        except Exception as e:
            self._log(f"Ошибка добавления канала в bio: {e}", "error")
            return False

    async def publish_warmup_posts(self, channel, topic: str, count: int = None) -> int:
        """
        Публикует прогревочные посты в канал через Gemini AI.

        Args:
            channel: Entity канала
            topic: Тема постов
            count: Количество постов (по умолчанию Config.CHANNEL_WARMUP_POSTS_COUNT)

        Returns:
            Количество успешно опубликованных постов
        """
        if count is None:
            count = Config.CHANNEL_WARMUP_POSTS_COUNT

        posted = 0
        for i in range(1, count + 1):
            try:
                prompt = (
                    f"Write a short engaging Telegram channel post about {topic}. "
                    f"Post #{i}. Keep it under 200 words. Write in Russian."
                )
                text = await self._generate_content(prompt)

                if text:
                    await self.client.send_message(channel, text)
                    posted += 1
                    self._log(f"Прогревочный пост #{i} опубликован")
                else:
                    self._log(f"Не удалось сгенерировать пост #{i}", "warning")

                await asyncio.sleep(random.randint(30, 120))

            except FloodWaitError as e:
                self._log(f"FloodWait при публикации: ждем {e.seconds}с", "warning")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                self._log(f"Ошибка публикации поста #{i}: {e}", "error")

        self._log(f"Прогрев завершен: {posted}/{count} постов опубликовано")
        return posted

    async def create_and_setup_channel(self, title: str, about: str, username_base: str,
                                       topic: str, avatar_bytes: bytes = None,
                                       publish_warmup: bool = True) -> Optional[Dict]:
        """
        Полный цикл создания и настройки канала.

        Args:
            title: Название канала
            about: Описание канала
            username_base: Базовый username
            topic: Тема для прогревочных постов
            avatar_bytes: Байты аватара (опционально)
            publish_warmup: Публиковать ли авто-посты по теме после создания

        Returns:
            Словарь с данными канала или None при ошибке
        """
        try:
            # 1. Создаем канал
            channel = await self.create_channel(title, about, username_base)
            if not channel:
                return None

            # 2. Устанавливаем аватар
            if avatar_bytes:
                await self.set_channel_avatar(channel, avatar_bytes)

            # 3. Получаем username
            username = getattr(channel, 'username', None)

            # 4. Добавляем в bio
            if username:
                await self.add_channel_to_bio(username)

            # 5. Публикуем прогревочные посты (только если явно включено)
            if publish_warmup and topic:
                posted = await self.publish_warmup_posts(channel, topic)
            else:
                posted = 0
                self._log("Авто-посты при создании отключены — канал создан пустым")

            # 6. Получаем invite link
            invite_link = ""
            try:
                result = await self.client(ExportChatInviteRequest(peer=channel))
                invite_link = result.link
            except Exception as e:
                self._log(f"Ошибка получения invite link: {e}", "warning")

            # 7. Сохраняем в БД
            self.db.add_own_channel(
                self.account_id,
                channel.id,
                username or "",
                title,
                about,
                invite_link
            )

            result_data = {
                "channel_id": channel.id,
                "username": username,
                "title": title,
                "invite_link": invite_link,
                "warmup_posts": posted
            }

            self._log(f"Канал полностью настроен: {title} (@{username})")
            return result_data

        except Exception as e:
            self._log(f"Ошибка в create_and_setup_channel: {e}", "error")
            return None

    async def close(self):
        """Закрывает HTTP клиент"""
        if self._http_client:
            await self._http_client.close()
            self._http_client = None
