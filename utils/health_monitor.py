"""
Модуль мониторинга здоровья аккаунтов
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from utils.database import Database


@dataclass
class AccountHealth:
    """Состояние здоровья аккаунта"""
    account_id: int
    consecutive_errors: int = 0
    last_success: Optional[datetime] = None
    status: str = "healthy"  # healthy, warning, critical, paused
    rate_limited_until: Optional[datetime] = None


class HealthMonitor:
    """
    Мониторинг здоровья аккаунтов.
    
    Отслеживает:
    - Последовательные ошибки
    - Время последней успешной операции
    - FloodWait и rate limiting
    - Статус здоровья (healthy, warning, critical, paused)
    """
    
    ERROR_THRESHOLD_WARNING = 3
    ERROR_THRESHOLD_CRITICAL = 5
    FLOOD_WAIT_PAUSE_THRESHOLD = 3600  # 1 час в секундах
    
    def __init__(self, db: Database):
        self.db = db
        self._cache: Dict[int, AccountHealth] = {}
    
    def _get_msk_now(self) -> datetime:
        """Возвращает текущее время в МСК"""
        msk_tz = timezone(timedelta(hours=3))
        return datetime.now(msk_tz)
    
    def _load_from_db(self, account_id: int) -> AccountHealth:
        """Загружает состояние здоровья из БД"""
        msk_tz = timezone(timedelta(hours=3))
        
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT consecutive_errors, last_success_at, health_status, rate_limited_until
                FROM accounts WHERE id = ?
            ''', (account_id,))
            row = cursor.fetchone()
            
            if row:
                last_success = None
                if row['last_success_at']:
                    try:
                        last_success = datetime.fromisoformat(row['last_success_at'])
                        # Добавляем timezone если нет
                        if last_success.tzinfo is None:
                            last_success = last_success.replace(tzinfo=msk_tz)
                    except:
                        pass
                
                rate_limited = None
                if row['rate_limited_until']:
                    try:
                        rate_limited = datetime.fromisoformat(row['rate_limited_until'])
                        # Добавляем timezone если нет
                        if rate_limited.tzinfo is None:
                            rate_limited = rate_limited.replace(tzinfo=msk_tz)
                    except:
                        pass
                
                return AccountHealth(
                    account_id=account_id,
                    consecutive_errors=row['consecutive_errors'] or 0,
                    last_success=last_success,
                    status=row['health_status'] or 'healthy',
                    rate_limited_until=rate_limited
                )
            
            return AccountHealth(account_id=account_id)
    
    def _save_to_db(self, health: AccountHealth):
        """Сохраняет состояние здоровья в БД"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            
            last_success_str = None
            if health.last_success:
                last_success_str = health.last_success.strftime('%Y-%m-%d %H:%M:%S')
            
            rate_limited_str = None
            if health.rate_limited_until:
                rate_limited_str = health.rate_limited_until.strftime('%Y-%m-%d %H:%M:%S')
            
            cursor.execute('''
                UPDATE accounts SET
                    consecutive_errors = ?,
                    last_success_at = ?,
                    health_status = ?,
                    rate_limited_until = ?
                WHERE id = ?
            ''', (
                health.consecutive_errors,
                last_success_str,
                health.status,
                rate_limited_str,
                health.account_id
            ))
    
    def get_status(self, account_id: int) -> AccountHealth:
        """Возвращает текущий статус аккаунта"""
        if account_id not in self._cache:
            self._cache[account_id] = self._load_from_db(account_id)
        return self._cache[account_id]
    
    def record_success(self, account_id: int):
        """Записывает успешную операцию"""
        health = self.get_status(account_id)
        health.consecutive_errors = 0
        health.last_success = self._get_msk_now()
        health.status = "healthy"
        health.rate_limited_until = None
        
        self._cache[account_id] = health
        self._save_to_db(health)
    
    def record_error(self, account_id: int, error: Exception = None) -> str:
        """
        Записывает ошибку и обновляет статус.
        
        Returns:
            Новый статус аккаунта
        """
        health = self.get_status(account_id)
        health.consecutive_errors += 1
        
        # Обновляем статус на основе количества ошибок
        if health.consecutive_errors >= self.ERROR_THRESHOLD_CRITICAL:
            health.status = "paused"
        elif health.consecutive_errors >= self.ERROR_THRESHOLD_WARNING:
            health.status = "critical"
        elif health.consecutive_errors >= 1:
            health.status = "warning"
        
        self._cache[account_id] = health
        self._save_to_db(health)
        
        return health.status
    
    def record_flood_wait(self, account_id: int, seconds: int) -> bool:
        """
        Записывает FloodWait.
        
        Args:
            account_id: ID аккаунта
            seconds: Время ожидания в секундах
            
        Returns:
            True если аккаунт помечен как rate-limited (FloodWait > 1 час)
        """
        health = self.get_status(account_id)
        
        # Добавляем буфер 10 секунд
        wait_until = self._get_msk_now() + timedelta(seconds=seconds + 10)
        health.rate_limited_until = wait_until
        
        # Если FloodWait больше часа - помечаем как rate-limited
        if seconds >= self.FLOOD_WAIT_PAUSE_THRESHOLD:
            health.status = "paused"
            is_paused = True
        else:
            is_paused = False
        
        self._cache[account_id] = health
        self._save_to_db(health)
        
        return is_paused
    
    def should_pause(self, account_id: int) -> bool:
        """Проверяет, нужно ли приостановить аккаунт"""
        health = self.get_status(account_id)
        
        # Проверяем статус
        if health.status == "paused":
            return True
        
        # Проверяем rate limit
        if health.rate_limited_until:
            now = self._get_msk_now()
            rate_until = health.rate_limited_until
            # Приводим к одному timezone если нужно
            if rate_until.tzinfo is None:
                msk_tz = timezone(timedelta(hours=3))
                rate_until = rate_until.replace(tzinfo=msk_tz)
            
            if now < rate_until:
                return True
            else:
                # Rate limit истёк, сбрасываем
                health.rate_limited_until = None
                self._save_to_db(health)
        
        return False
    
    def get_wait_time(self, account_id: int) -> int:
        """
        Возвращает время ожидания в секундах до снятия rate limit.
        
        Returns:
            Секунды до снятия лимита или 0 если лимита нет
        """
        health = self.get_status(account_id)
        
        if health.rate_limited_until:
            now = self._get_msk_now()
            rate_until = health.rate_limited_until
            # Приводим к одному timezone если нужно
            if rate_until.tzinfo is None:
                msk_tz = timezone(timedelta(hours=3))
                rate_until = rate_until.replace(tzinfo=msk_tz)
            
            delta = rate_until - now
            if delta.total_seconds() > 0:
                return int(delta.total_seconds())
        
        return 0
    
    def reset_account(self, account_id: int):
        """Сбрасывает состояние здоровья аккаунта"""
        health = AccountHealth(account_id=account_id)
        self._cache[account_id] = health
        self._save_to_db(health)
