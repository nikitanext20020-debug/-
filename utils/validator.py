"""
Модуль валидации входных данных API
"""
import re
from typing import Tuple, Optional


class InputValidator:
    """Валидатор входных данных для API endpoints"""
    
    # Международный формат телефона: +7XXXXXXXXXX или 7XXXXXXXXXX (7-15 цифр)
    PHONE_PATTERN = re.compile(r'^\+?[1-9]\d{6,14}$')
    
    # IPv4 адрес
    IP_PATTERN = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')
    
    @classmethod
    def validate_phone(cls, phone: str) -> Tuple[bool, Optional[str]]:
        """Валидирует номер телефона"""
        if not phone:
            return False, "Номер телефона не может быть пустым"
        cleaned = re.sub(r'[\s\-\(\)]', '', phone)
        if not cls.PHONE_PATTERN.match(cleaned):
            return False, "Неверный формат номера телефона"
        return True, None

    @classmethod
    def validate_ip(cls, ip: str) -> Tuple[bool, Optional[str]]:
        """Валидирует IPv4 адрес"""
        if not ip:
            return False, "IP адрес не может быть пустым"
        match = cls.IP_PATTERN.match(ip.strip())
        if not match:
            return False, "Неверный формат IP адреса"
        for i in range(1, 5):
            if int(match.group(i)) > 255:
                return False, "Октет IP должен быть 0-255"
        return True, None
    
    @classmethod
    def validate_port(cls, port: int) -> Tuple[bool, Optional[str]]:
        """Валидирует номер порта"""
        try:
            port = int(port)
        except (ValueError, TypeError):
            return False, "Порт должен быть числом"
        if port < 1 or port > 65535:
            return False, "Порт должен быть 1-65535"
        return True, None
    
    @classmethod
    def sanitize_string(cls, value: str) -> str:
        """Очищает строку от опасных символов"""
        if not value:
            return value
        result = value.replace("'", "''")
        result = result.replace("<", "&lt;").replace(">", "&gt;")
        return result.strip()
    
    @classmethod
    def validate_proxy(cls, ip: str, port: int, proxy_type: str = None) -> Tuple[bool, Optional[str]]:
        """Валидирует данные прокси"""
        valid, error = cls.validate_ip(ip)
        if not valid:
            return False, error
        valid, error = cls.validate_port(port)
        if not valid:
            return False, error
        if proxy_type and proxy_type.lower() not in ['socks5', 'socks4', 'http', 'https']:
            return False, "Неверный тип прокси"
        return True, None
    
    @classmethod
    def validate_account_data(cls, phone: str, api_id: int = None, api_hash: str = None) -> Tuple[bool, Optional[str]]:
        """Валидирует данные аккаунта"""
        valid, error = cls.validate_phone(phone)
        if not valid:
            return False, error
        if api_id is not None and (not isinstance(api_id, int) or api_id <= 0):
            return False, "API ID должен быть положительным числом"
        if api_hash is not None and (not api_hash or len(api_hash) != 32):
            return False, "API Hash должен быть 32 символа"
        return True, None
