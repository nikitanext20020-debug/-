"""
Модуль генерации комментариев через Gemini (совместимый с OpenAI API для RouterAI)
Поддерживает как синхронный, так и асинхронный режим работы.
"""
import asyncio
import base64
import random
from typing import Optional
from config import Config
from utils.async_http import AsyncHTTPClient, get_http_client


class CommentGenerator:
    """Класс для генерации комментариев через Gemini (RouterAI / OpenAI API format)"""
    
    def __init__(self, api_key: str = None, db = None):
        self._api_key_override = api_key
        self.db = db
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
    
    def _get_http_client(self) -> AsyncHTTPClient:
        """Получает HTTP клиент (создаёт если нужно)"""
        if self._http_client is None:
            self._http_client = AsyncHTTPClient(max_retries=3, base_delay=1.0, timeout=30.0)
        return self._http_client
    
    async def generate_comment_async(
        self, 
        post_text: str = "", 
        image_bytes: Optional[bytes] = None
    ) -> str:
        """
        Асинхронно генерирует комментарий к посту.
        
        Args:
            post_text: Текст поста
            image_bytes: Байты изображения (опционально)
            
        Returns:
            Сгенерированный комментарий
        """
        try:
            messages = []
            system_prompt = Config.COMMENT_PERSONA_SYSTEM
            
            # Берём промпт из БД если есть
            if self.db:
                system_prompt = self.db.get_setting("comment_prompt", Config.COMMENT_PERSONA_SYSTEM) or Config.COMMENT_PERSONA_SYSTEM
            
            if post_text.strip():
                user_text_prompt = f"Текст поста: {post_text}\n\nНапиши комментарий."
            else:
                user_text_prompt = "Напиши комментарий к этому изображению/посту (текста нет)."

            user_content = [{"type": "text", "text": user_text_prompt}]

            # Обработка изображения (Vision)
            if image_bytes and Config.SUPPORT_IMAGES:
                b64_image = base64.b64encode(image_bytes).decode('utf-8')
                image_url = f"data:image/jpeg;base64,{b64_image}"
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url}
                })

            messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_content})

            payload = {
                "model": self._get_model(),
                "messages": messages,
                "max_tokens": Config.COMMENT_MAX_TOKENS,
                "temperature": Config.COMMENT_TEMPERATURE
            }

            client = self._get_http_client()
            response = await client.post(self._get_endpoint(), json_data=payload, headers=self._get_headers())
            
            if response.success and isinstance(response.data, dict):
                content = response.data.get('choices', [{}])[0].get('message', {}).get('content', '')
                comment = content.strip().replace('"', '').replace("'", "")
                
                # Обрезка если слишком длинно
                words = comment.split()
                if len(words) > Config.COMMENT_MAX_WORDS + 5:
                    comment = " ".join(words[:Config.COMMENT_MAX_WORDS])
                
                return comment if comment else ""  # Пустая строка вместо fallback
            else:
                print(f"Ошибка API ({response.status}): {response.data}")
                return ""  # Не отправляем шаблонные комментарии

        except Exception as e:
            print(f"Ошибка генерации комментария: {e}")
            return ""  # Лучше не комментировать чем писать шаблон
    
    def generate_comment(
        self, 
        post_text: str = "", 
        image_bytes: Optional[bytes] = None
    ) -> str:
        """
        Синхронная обёртка для генерации комментария.
        Использует существующий event loop или создаёт новый.
        
        Args:
            post_text: Текст поста
            image_bytes: Байты изображения (опционально)
            
        Returns:
            Сгенерированный комментарий
        """
        try:
            # Пробуем получить текущий event loop
            try:
                loop = asyncio.get_running_loop()
                # Мы уже в async контексте - создаём новый HTTP клиент для синхронного вызова
                # и используем requests вместо aiohttp
                return self._generate_comment_sync(post_text, image_bytes)
            except RuntimeError:
                # Нет активного event loop - можем использовать asyncio.run
                return asyncio.run(self.generate_comment_async(post_text, image_bytes))
        except Exception as e:
            print(f"Ошибка в синхронной обёртке: {e}")
            return ""  # Не отправляем шаблонные комментарии
    
    def _generate_comment_sync(self, post_text: str = "", image_bytes: Optional[bytes] = None) -> str:
        """Синхронная генерация комментария через requests"""
        try:
            import requests
            
            messages = []
            system_prompt = Config.COMMENT_PERSONA_SYSTEM
            
            # Берём промпт из БД если есть
            if self.db:
                system_prompt = self.db.get_setting("comment_prompt", Config.COMMENT_PERSONA_SYSTEM) or Config.COMMENT_PERSONA_SYSTEM
            
            if post_text.strip():
                user_text_prompt = f"Текст поста: {post_text}\n\nНапиши комментарий."
            else:
                user_text_prompt = "Напиши комментарий к этому изображению/посту (текста нет)."

            user_content = [{"type": "text", "text": user_text_prompt}]

            # Обработка изображения (Vision)
            if image_bytes and Config.SUPPORT_IMAGES:
                b64_image = base64.b64encode(image_bytes).decode('utf-8')
                image_url = f"data:image/jpeg;base64,{b64_image}"
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url}
                })

            messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_content})

            payload = {
                "model": self._get_model(),
                "messages": messages,
                "max_tokens": Config.COMMENT_MAX_TOKENS,
                "temperature": Config.COMMENT_TEMPERATURE
            }

            response = requests.post(
                self._get_endpoint(),
                json=payload,
                headers=self._get_headers(),
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                comment = content.strip().replace('"', '').replace("'", "")
                
                # Обрезка если слишком длинно
                words = comment.split()
                if len(words) > Config.COMMENT_MAX_WORDS + 5:
                    comment = " ".join(words[:Config.COMMENT_MAX_WORDS])
                
                return comment if comment else ""  # Пустая строка вместо fallback
            else:
                print(f"Ошибка API ({response.status_code}): {response.text}")
                return ""  # Не отправляем шаблонные комментарии

        except Exception as e:
            print(f"Ошибка синхронной генерации: {e}")
            return ""  # Лучше не комментировать чем писать шаблон
    
    def _get_fallback_comment(self) -> str:
        """DEPRECATED: Возвращает пустую строку - шаблонные комментарии отключены"""
        return ""  # Шаблонные комментарии отключены
    
    async def close(self):
        """Закрывает HTTP клиент"""
        if self._http_client:
            await self._http_client.close()
            self._http_client = None
