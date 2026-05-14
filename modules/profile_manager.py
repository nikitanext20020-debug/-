"""
Модуль управления профилями Telegram аккаунтов.
Позволяет массово обновлять аватарки, имена и описания.
"""
import os
import io
import base64
import asyncio
from typing import Optional, Dict, List, Any
from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest, DeletePhotosRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPhoto


class ProfileManager:
    """Менеджер профилей для Telegram аккаунтов"""
    
    def __init__(self, client: TelegramClient, db=None, account_id: int = None):
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
        print(f"[ProfileManager] {message}")
    
    async def get_current_profile(self) -> Dict[str, Any]:
        """
        Получает текущую информацию о профиле.
        
        Returns:
            Словарь с данными профиля
        """
        try:
            me = await self.client.get_me()
            full = await self.client(GetFullUserRequest(me))
            
            return {
                "id": me.id,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "username": me.username or "",
                "phone": me.phone or "",
                "bio": full.full_user.about or "",
                "has_photo": me.photo is not None
            }
        except Exception as e:
            self._log(f"❌ Ошибка получения профиля: {e}", "error")
            return {}
    
    async def update_name(self, first_name: str, last_name: str = "") -> bool:
        """
        Обновляет имя и фамилию профиля.
        
        Args:
            first_name: Новое имя
            last_name: Новая фамилия (опционально)
            
        Returns:
            True если успешно
        """
        try:
            await self.client(UpdateProfileRequest(
                first_name=first_name,
                last_name=last_name
            ))
            self._log(f"✅ Имя обновлено: {first_name} {last_name}".strip())
            return True
        except Exception as e:
            self._log(f"❌ Ошибка обновления имени: {e}", "error")
            return False
    
    async def update_bio(self, bio: str) -> bool:
        """
        Обновляет описание профиля (bio).
        
        Args:
            bio: Новое описание (макс. 70 символов)
            
        Returns:
            True если успешно
        """
        try:
            # Telegram ограничивает bio до 70 символов
            bio = bio[:70] if len(bio) > 70 else bio
            
            await self.client(UpdateProfileRequest(about=bio))
            self._log(f"✅ Bio обновлено: {bio[:30]}...")
            return True
        except Exception as e:
            self._log(f"❌ Ошибка обновления bio: {e}", "error")
            return False
    
    async def update_avatar(self, image_data: bytes) -> bool:
        """
        Обновляет аватарку профиля.
        
        Args:
            image_data: Байты изображения (JPEG/PNG)
            
        Returns:
            True если успешно
        """
        try:
            # Загружаем файл
            file = await self.client.upload_file(
                io.BytesIO(image_data),
                file_name="avatar.jpg"
            )
            
            # Устанавливаем как фото профиля
            await self.client(UploadProfilePhotoRequest(file=file))
            self._log("✅ Аватарка обновлена")
            return True
        except Exception as e:
            self._log(f"❌ Ошибка обновления аватарки: {e}", "error")
            return False
    
    async def update_avatar_from_base64(self, base64_data: str) -> bool:
        """
        Обновляет аватарку из base64 строки.
        
        Args:
            base64_data: Изображение в формате base64
            
        Returns:
            True если успешно
        """
        try:
            # Убираем префикс data:image/...;base64, если есть
            if ',' in base64_data:
                base64_data = base64_data.split(',')[1]
            
            image_bytes = base64.b64decode(base64_data)
            return await self.update_avatar(image_bytes)
        except Exception as e:
            self._log(f"❌ Ошибка декодирования base64: {e}", "error")
            return False
    
    async def update_avatar_from_file(self, file_path: str) -> bool:
        """
        Обновляет аватарку из файла.
        
        Args:
            file_path: Путь к файлу изображения
            
        Returns:
            True если успешно
        """
        try:
            if not os.path.exists(file_path):
                self._log(f"❌ Файл не найден: {file_path}", "error")
                return False
            
            with open(file_path, 'rb') as f:
                image_data = f.read()
            
            return await self.update_avatar(image_data)
        except Exception as e:
            self._log(f"❌ Ошибка чтения файла: {e}", "error")
            return False
    
    async def delete_avatar(self) -> bool:
        """
        Удаляет текущую аватарку.
        
        Returns:
            True если успешно
        """
        try:
            me = await self.client.get_me()
            if me.photo:
                photos = await self.client.get_profile_photos('me', limit=1)
                if photos:
                    await self.client(DeletePhotosRequest(
                        id=[InputPhoto(
                            id=photos[0].id,
                            access_hash=photos[0].access_hash,
                            file_reference=photos[0].file_reference
                        )]
                    ))
                    self._log("✅ Аватарка удалена")
                    return True
            return False
        except Exception as e:
            self._log(f"❌ Ошибка удаления аватарки: {e}", "error")
            return False
    
    async def update_full_profile(
        self, 
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        bio: Optional[str] = None,
        avatar_base64: Optional[str] = None
    ) -> Dict[str, bool]:
        """
        Обновляет все указанные поля профиля.
        
        Args:
            first_name: Новое имя
            last_name: Новая фамилия
            bio: Новое описание
            avatar_base64: Аватарка в base64
            
        Returns:
            Словарь с результатами каждой операции
        """
        results = {}
        
        # Обновляем имя если указано
        if first_name is not None:
            results['name'] = await self.update_name(
                first_name, 
                last_name or ""
            )
            await asyncio.sleep(1)  # Небольшая задержка между операциями
        
        # Обновляем bio если указано
        if bio is not None:
            results['bio'] = await self.update_bio(bio)
            await asyncio.sleep(1)
        
        # Обновляем аватарку если указана
        if avatar_base64:
            results['avatar'] = await self.update_avatar_from_base64(avatar_base64)
        
        return results
