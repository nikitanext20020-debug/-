from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from typing import Dict, Optional, Tuple
import os
from utils.session_lock import SessionFileLock, SessionLockError

class AuthManager:
    def __init__(self, sessions_dir: str = "sessions"):
        self.sessions_dir = sessions_dir
        os.makedirs(sessions_dir, exist_ok=True)
        self.active_clients: Dict[str, TelegramClient] = {}
        self.client_proxies: Dict[str, Optional[int]] = {}  # Храним proxy_id для каждого клиента
        self.client_locks: Dict[str, SessionFileLock] = {}

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
        if phone in self.active_clients:
            raise RuntimeError("Авторизация для этого номера уже выполняется")

        lock = SessionFileLock(session_path)
        try:
            lock.acquire(timeout=0)
        except SessionLockError as e:
            raise RuntimeError("Временная Telegram-сессия уже используется") from e

        client = None
        try:
            # Создаем клиент С ПРОКСИ (если указан)
            client = TelegramClient(session_path, api_id, api_hash, proxy=proxy_tuple)
            await client.connect()
            result = await client.send_code_request(phone)
            self.active_clients[phone] = client
            self.client_proxies[phone] = proxy_id  # Запоминаем proxy_id
            self.client_locks[phone] = lock
            return result.phone_code_hash
        except Exception:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            lock.release()
            raise

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
            self.active_clients.pop(phone, None)
            self.client_proxies.pop(phone, None)
            lock = self.client_locks.pop(phone, None)
            if lock:
                lock.release()
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

        if not client:
            return None

        old_path = os.path.join(self.sessions_dir, f"temp_{phone.replace('+', '')}.session")
        new_path = os.path.join(self.sessions_dir, f"{session_name}.session")

        # Не перезаписываем финальную SQLite-сессию, пока её держит воркер
        # этого или другого процесса.
        final_lock = SessionFileLock(new_path)
        try:
            final_lock.acquire(timeout=0)
        except SessionLockError as e:
            raise RuntimeError(
                "Целевая Telegram-сессия сейчас используется. Остановите аккаунт и повторите сохранение."
            ) from e

        temp_lock = self.client_locks.pop(phone, None)
        try:
            await client.disconnect()
            if not os.path.exists(old_path):
                raise FileNotFoundError("Временный файл Telegram-сессии не найден")
            os.replace(old_path, new_path)
            return proxy_id
        finally:
            self.active_clients.pop(phone, None)
            self.client_proxies.pop(phone, None)
            if temp_lock:
                temp_lock.release()
            final_lock.release()
