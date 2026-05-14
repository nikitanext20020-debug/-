"""
Асинхронный HTTP клиент с retry-логикой и exponential backoff
"""
import asyncio
import aiohttp
from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class HTTPResponse:
    """Результат HTTP запроса"""
    status: int
    data: Any
    headers: Dict[str, str]
    success: bool


class AsyncHTTPClient:
    """Асинхронный HTTP клиент с автоматическим retry и exponential backoff"""
    
    def __init__(
        self, 
        max_retries: int = 3, 
        base_delay: float = 1.0,
        timeout: float = 30.0
    ):
        """
        Args:
            max_retries: Максимальное количество повторных попыток
            base_delay: Базовая задержка для exponential backoff (секунды)
            timeout: Таймаут для запросов (секунды)
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Получает или создаёт сессию"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session
    
    async def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json_data: Optional[Dict] = None,
        data: Optional[Any] = None,
        **kwargs
    ) -> HTTPResponse:
        """
        Выполняет HTTP запрос с автоматическим retry.
        
        Args:
            method: HTTP метод (GET, POST, etc.)
            url: URL для запроса
            headers: Заголовки запроса
            json_data: JSON данные для отправки
            data: Сырые данные для отправки
            **kwargs: Дополнительные параметры для aiohttp
        
        Returns:
            HTTPResponse с результатом
            
        Raises:
            aiohttp.ClientError: После исчерпания всех попыток
        """
        session = await self._get_session()
        last_exception = None
        
        for attempt in range(self.max_retries + 1):
            try:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_data,
                    data=data,
                    **kwargs
                ) as response:
                    # Пытаемся получить JSON, если не получается - текст
                    try:
                        response_data = await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        response_data = await response.text()
                    
                    return HTTPResponse(
                        status=response.status,
                        data=response_data,
                        headers=dict(response.headers),
                        success=200 <= response.status < 300
                    )
                    
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exception = e
                
                # Если это последняя попытка - не ждём
                if attempt < self.max_retries:
                    # Exponential backoff: base_delay * 2^attempt
                    delay = self.base_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
        
        # Все попытки исчерпаны
        raise last_exception or aiohttp.ClientError("Request failed after all retries")
    
    async def get(self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs) -> HTTPResponse:
        """GET запрос"""
        return await self.request("GET", url, headers=headers, **kwargs)
    
    async def post(
        self, 
        url: str, 
        json_data: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs
    ) -> HTTPResponse:
        """POST запрос"""
        return await self.request("POST", url, headers=headers, json_data=json_data, **kwargs)
    
    async def post_json(
        self, 
        url: str, 
        data: Dict,
        headers: Optional[Dict[str, str]] = None
    ) -> Dict:
        """
        POST запрос с JSON телом. Возвращает только данные ответа.
        
        Args:
            url: URL для запроса
            data: Данные для отправки как JSON
            headers: Дополнительные заголовки
            
        Returns:
            Данные ответа (dict или str)
            
        Raises:
            aiohttp.ClientError: При ошибке запроса
            ValueError: При неуспешном статусе ответа
        """
        default_headers = {"Content-Type": "application/json"}
        if headers:
            default_headers.update(headers)
        
        response = await self.post(url, json_data=data, headers=default_headers)
        
        if not response.success:
            raise ValueError(f"HTTP {response.status}: {response.data}")
        
        return response.data
    
    async def close(self):
        """Закрывает сессию"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


# Глобальный экземпляр для переиспользования
_default_client: Optional[AsyncHTTPClient] = None


def get_http_client() -> AsyncHTTPClient:
    """Получает глобальный HTTP клиент (создаёт если нужно)"""
    global _default_client
    if _default_client is None:
        _default_client = AsyncHTTPClient()
    return _default_client


async def close_http_client():
    """Закрывает глобальный HTTP клиент"""
    global _default_client
    if _default_client:
        await _default_client.close()
        _default_client = None
