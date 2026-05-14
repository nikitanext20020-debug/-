from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from typing import Dict, Optional, Tuple
import os
import socks

class AuthManager:
    def __init__(self, sessions_dir: str = "sessions"):
        self.sessions_dir = sessions_dir
        os.makedirs(sessions_dir, exist_ok=True)
        self.active_clients: Dict[str, TelegramClient] = {}
        self.client_proxies: Dict[str, Optional[int]] = {}  # Храним proxy_id для каждого клиента

    async def send_code(self, phone: str, api_id: int, api_hash: str, proxy_tuple: Optional[Tuple] = None, proxy_id: Optional[int] = None):
        """
        Отправляет код авторизации через Telegram.
        
        Args:
            phone: Номер телефона
            api_id: API ID
            api_hash: API Hash
            proxy_tuple: Кортеж прокси (type, ip, port, rdns, username, password)
            proxy_id: ID прокси в базе данных (для сохранения)
        """
        session_path = os.path.join(self.sessions_dir, f"temp_{phone.replace('+', '')}")
        
        # Создаем клиент С ПРОКСИ (если указан)
        client = TelegramClient(session_path, api_id, api_hash, proxy=proxy_tuple)
        await client.connect()
        
        result = await client.send_code_request(phone)
        self.active_clients[phone] = client
        self.client_proxies[phone] = proxy_id  # Запоминаем proxy_id
        return result.phone_code_hash

    async def verify_code(self, phone: str, code: str, phone_code_hash: str):
        client = self.active_clients.get(phone)
        if not client:
            raise Exception("Сессия не найдена. Попробуйте отправить код снова.")
        
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            # Если успешно, клиент авторизован
            return {"status": "success"}
        except SessionPasswordNeededError:
            return {"status": "password_required"}
        except Exception as e:
            await client.disconnect()
            del self.active_clients[phone]
            raise e

    async def verify_password(self, phone: str, password: str):
        client = self.active_clients.get(phone)
        if not client:
            raise Exception("Сессия не найдена.")
        
        await client.sign_in(password=password)
        return {"status": "success"}

    async def finish_auth(self, phone: str, session_name: str):
        client = self.active_clients.get(phone)
        proxy_id = self.client_proxies.get(phone)  # Получаем proxy_id
        
        if client:
            # Переименовываем файл сессии из временного в постоянный
            old_path = os.path.join(self.sessions_dir, f"temp_{phone.replace('+', '')}.session")
            new_path = os.path.join(self.sessions_dir, f"{session_name}.session")
            
            await client.disconnect()
            if os.path.exists(new_path):
                os.remove(new_path)
            os.rename(old_path, new_path)
            
            del self.active_clients[phone]
            if phone in self.client_proxies:
                del self.client_proxies[phone]
            
            return proxy_id  # Возвращаем proxy_id для сохранения в БД
        return None
