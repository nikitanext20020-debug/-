"""
Модуль логирования
"""
import logging
import os
import traceback
import json
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from config import Config


class Logger:
    """Класс для логирования действий бота"""
    
    def __init__(self, log_file: str = 'data/bot.log'):
        os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else '.', exist_ok=True)
        
        # Настройка логирования
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler()  # Также выводим в консоль
            ]
        )
        
        self.logger = logging.getLogger(__name__)
    
    def info(self, message: str):
        self.logger.info(message)
    
    def error(self, message: str):
        self.logger.error(message)
    
    def warning(self, message: str):
        self.logger.warning(message)
    
    def debug(self, message: str):
        self.logger.debug(message)


@dataclass
class LogEntry:
    """Структура записи лога"""
    timestamp: datetime
    level: str
    message: str
    account_id: Optional[int] = None
    module: Optional[str] = None
    stack_trace: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертирует в словарь для сохранения"""
        result = asdict(self)
        result['timestamp'] = self.timestamp.isoformat()
        if self.extra:
            result['extra'] = json.dumps(self.extra)
        return result


class StructuredLogger:
    """
    Структурированный логгер с поддержкой БД.
    
    Включает:
    - Timestamp, level, account_id, module в каждой записи
    - Stack trace для ошибок
    - Сохранение в БД
    """
    
    def __init__(self, db, module_name: str, log_file: str = None):
        """
        Args:
            db: Экземпляр Database для сохранения логов
            module_name: Имя модуля для идентификации источника логов
            log_file: Путь к файлу логов (не используется, логи только в БД и консоль)
        """
        self.db = db
        self.module_name = module_name
        
        self._logger = logging.getLogger(f"structured.{module_name}")
        self._logger.setLevel(logging.DEBUG)
        
        # Убираем дублирование если хендлеры уже есть
        if not self._logger.handlers:
            formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'
            )
            
            # Только консольный хендлер (файловый лог отключен - всё в БД)
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self._logger.addHandler(console_handler)
    
    def _create_entry(
        self, 
        level: str, 
        message: str, 
        account_id: Optional[int] = None,
        exc_info: bool = False,
        **extra
    ) -> LogEntry:
        """Создаёт структурированную запись лога"""
        stack_trace = None
        if exc_info:
            stack_trace = traceback.format_exc()
            if stack_trace == "NoneType: None\n":
                stack_trace = None
        
        return LogEntry(
            timestamp=datetime.now(),
            level=level,
            message=message,
            account_id=account_id,
            module=self.module_name,
            stack_trace=stack_trace,
            extra=extra if extra else None
        )
    
    def _save_to_db(self, entry: LogEntry):
        """Сохраняет запись в БД"""
        try:
            extra_json = json.dumps(entry.extra) if entry.extra else None
            self.db.add_structured_log(
                account_id=entry.account_id,
                level=entry.level,
                message=entry.message,
                module=entry.module,
                stack_trace=entry.stack_trace,
                extra_json=extra_json
            )
        except Exception as e:
            # Не падаем если БД недоступна
            self._logger.warning(f"Failed to save log to DB: {e}")
    
    def _format_message(self, message: str, account_id: Optional[int], **extra) -> str:
        """Форматирует сообщение с метаданными"""
        parts = []
        if account_id:
            parts.append(f"[acc:{account_id}]")
        parts.append(message)
        if extra:
            parts.append(f"| {extra}")
        return " ".join(parts)
    
    def info(self, message: str, account_id: Optional[int] = None, **extra):
        """Логирует информационное сообщение"""
        entry = self._create_entry("info", message, account_id, **extra)
        formatted = self._format_message(message, account_id, **extra)
        self._logger.info(formatted)
        self._save_to_db(entry)
    
    def warning(self, message: str, account_id: Optional[int] = None, **extra):
        """Логирует предупреждение"""
        entry = self._create_entry("warning", message, account_id, **extra)
        formatted = self._format_message(message, account_id, **extra)
        self._logger.warning(formatted)
        self._save_to_db(entry)
    
    def error(
        self, 
        message: str, 
        account_id: Optional[int] = None, 
        exc_info: bool = True, 
        **extra
    ):
        """
        Логирует ошибку с опциональным stack trace.
        
        Args:
            message: Текст ошибки
            account_id: ID аккаунта (опционально)
            exc_info: Включать ли stack trace (по умолчанию True)
            **extra: Дополнительные данные
        """
        entry = self._create_entry("error", message, account_id, exc_info=exc_info, **extra)
        formatted = self._format_message(message, account_id, **extra)
        self._logger.error(formatted, exc_info=exc_info)
        self._save_to_db(entry)
    
    def debug(self, message: str, account_id: Optional[int] = None, **extra):
        """Логирует отладочное сообщение"""
        entry = self._create_entry("debug", message, account_id, **extra)
        formatted = self._format_message(message, account_id, **extra)
        self._logger.debug(formatted)
        # Debug логи не сохраняем в БД для экономии места

    def success(self, message: str, account_id: Optional[int] = None, **extra):
        """Логирует успешное действие (отдельный уровень для зелёной подсветки)"""
        entry = self._create_entry("success", message, account_id, **extra)
        formatted = self._format_message(message, account_id, **extra)
        self._logger.info(formatted)
        self._save_to_db(entry)

    def log(self, level: str, message: str, account_id: Optional[int] = None,
            exc_info: bool = False, **extra):
        """
        Универсальный метод: логирует сообщение с произвольным уровнем.
        Гарантирует, что запись попадёт в БД, даже если уровень нестандартный.
        """
        level = (level or "info").lower()
        entry = self._create_entry(level, message, account_id, exc_info=exc_info, **extra)
        formatted = self._format_message(message, account_id, **extra)
        # Маппинг на python-logging уровни для консоли
        py_level = {"success": logging.INFO, "info": logging.INFO,
                    "warning": logging.WARNING, "error": logging.ERROR,
                    "critical": logging.CRITICAL, "debug": logging.DEBUG}.get(level, logging.INFO)
        self._logger.log(py_level, formatted, exc_info=exc_info)
        # debug не пишем в БД для экономии места
        if level != "debug":
            self._save_to_db(entry)

