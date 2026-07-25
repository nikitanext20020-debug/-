"""
Модуль работы с SQLite базой данных (Multi-account version)
"""
import sqlite3
import json
import os
import time
import threading
from functools import wraps
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any
from contextlib import contextmanager


# Глобальный lock для всех операций с БД
_db_lock = threading.Lock()


def db_retry(max_attempts=3, default=None):
    """Декоратор для retry при ошибках БД"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    last_error = e
                    err_str = str(e).lower()
                    # Retry для locked и readonly ошибок
                    if ("database is locked" in err_str or "readonly" in err_str) and attempt < max_attempts - 1:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    break
                except Exception as e:
                    last_error = e
                    break
            
            # Если все попытки провалились
            print(f"[DB ERROR] {func.__name__}: {last_error}")
            return default
        return wrapper
    return decorator


class Database:
    """Класс для работы с SQLite базой данных в многоаккаунтном режиме"""
    
    _instance = None
    _initialized = False
    
    def __new__(cls, db_path: str = 'data/bot.db'):
        # Singleton pattern - один экземпляр на весь процесс
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, db_path: str = 'data/bot.db'):
        # Инициализируем только один раз (на процесс). Для тестов — reset_instance().
        if Database._initialized:
            return
        Database._initialized = True
        
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
        self._init_database()

    @classmethod
    def reset_instance(cls):
        """Сбрасывает singleton (только для тестов / re-bind на другой путь)."""
        cls._instance = None
        cls._initialized = False
    
    @contextmanager
    def get_connection(self):
        """Контекстный менеджер для получения соединения с БД"""
        global _db_lock
        
        # Получаем глобальный lock перед любой операцией с БД
        # Используем timeout чтобы не зависнуть навечно
        acquired = _db_lock.acquire(timeout=30)
        if not acquired:
            raise sqlite3.OperationalError("database is locked (lock timeout)")
        
        conn = None
        try:
            for attempt in range(10):  # Увеличено до 10 попыток
                try:
                    conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=120)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=60000")  # 60 секунд
                    conn.execute("PRAGMA synchronous=NORMAL")  # Быстрее записи
                    break
                except sqlite3.OperationalError as e:
                    err_str = str(e).lower()
                    if ("database is locked" in err_str or "readonly" in err_str) and attempt < 9:
                        time.sleep(1.0 * (attempt + 1))  # Увеличенная задержка
                        continue
                    raise
            
            try:
                yield conn
                conn.commit()
            except sqlite3.OperationalError as e:
                if conn:
                    conn.rollback()
                # Retry для readonly ошибок при commit
                err_str = str(e).lower()
                if "readonly" in err_str:
                    print(f"[DB WARNING] Readonly error, retrying: {e}")
                    time.sleep(0.5)
                raise
            except Exception:
                if conn:
                    conn.rollback()
                raise
            finally:
                if conn:
                    conn.close()
        finally:
            _db_lock.release()
    
    def _init_database(self):
        """Инициализация таблиц базы данных"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица аккаунтов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE,
                    session_name TEXT NOT NULL,
                    api_id INTEGER,
                    api_hash TEXT,
                    proxy_id INTEGER,
                    status TEXT DEFAULT 'stopped',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица прокси
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS proxies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT,
                    password TEXT,
                    type TEXT DEFAULT 'http',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица обработанных постов (с привязкой к аккаунту)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processed_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    channel TEXT NOT NULL,
                    post_id INTEGER NOT NULL,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_id, channel, post_id)
                )
            ''')
            
            # Таблица диалогов (с привязкой к аккаунту)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dialog_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица найденных каналов (общая)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS found_channels (
                    channel TEXT PRIMARY KEY,
                    title TEXT,
                    keyword TEXT,
                    subs_count INTEGER DEFAULT 0,
                    avg_views INTEGER DEFAULT 0,
                    can_comment BOOLEAN DEFAULT 0,
                    status TEXT DEFAULT 'new', -- new, verified, rejected, active
                    last_checked TIMESTAMP,
                    source TEXT, -- search, link, telemetr
                    is_pinned INTEGER DEFAULT 0, -- «замочек»: защита от авточистки
                    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица настроек (ключ-значение)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

            # Таблица логов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    level TEXT,
                    message TEXT,
                    module TEXT,
                    stack_trace TEXT,
                    extra_json TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Миграция: добавляем новые колонки в logs если их нет
            try:
                cursor.execute("SELECT module FROM logs LIMIT 1")
            except sqlite3.OperationalError:
                cursor.execute("ALTER TABLE logs ADD COLUMN module TEXT")
                cursor.execute("ALTER TABLE logs ADD COLUMN stack_trace TEXT")
                cursor.execute("ALTER TABLE logs ADD COLUMN extra_json TEXT")

            # Таблица статистики (лайки и прочее, чего нет в других таблицах)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS account_stats (
                    account_id INTEGER,
                    metric TEXT,
                    value INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, metric)
                )
            ''')

            # Таблица банов (локальные для аккаунта)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channel_bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    channel TEXT,
                    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_id, channel)
                )
            ''')
            
            # Таблица дневной статистики
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    date DATE NOT NULL,
                    comments_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    UNIQUE(account_id, channel, date)
                )
            ''')
            
            # Создание индексов с обработкой миграции
            try:
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_processed_acc_chan ON processed_posts(account_id, channel)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_dialog_acc_user ON dialog_history(account_id, user_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_stats_account ON daily_stats(account_id)')
            except sqlite3.OperationalError as e:
                if "no such column: account_id" in str(e):
                    # Миграция: добавляем колонку account_id если её нет
                    cursor.execute('ALTER TABLE processed_posts ADD COLUMN account_id INTEGER')
                    cursor.execute('ALTER TABLE dialog_history ADD COLUMN account_id INTEGER')
                    # Пробуем создать индексы снова
                    cursor.execute('CREATE INDEX IF NOT EXISTS idx_processed_acc_chan ON processed_posts(account_id, channel)')
                    cursor.execute('CREATE INDEX IF NOT EXISTS idx_dialog_acc_user ON dialog_history(account_id, user_id)')
                else:
                    raise e
            
            # Проверка и миграция для found_channels (добавляем новые колонки)
            try:
                cursor.execute("SELECT source FROM found_channels LIMIT 1")
            except sqlite3.OperationalError:
                # Если колонки source нет, значит нужны миграции
                columns = [
                    ('subs_count', 'INTEGER DEFAULT 0'),
                    ('avg_views', 'INTEGER DEFAULT 0'),
                    ('can_comment', 'BOOLEAN DEFAULT 0'),
                    ('status', "TEXT DEFAULT 'new'"),
                    ('last_checked', 'TIMESTAMP'),
                    ('source', 'TEXT')
                ]
                for col_name, col_type in columns:
                    try:
                        cursor.execute(f"ALTER TABLE found_channels ADD COLUMN {col_name} {col_type}")
                    except:
                        pass
            
            # Миграция: добавляем channel_id для приватных каналов
            try:
                cursor.execute("SELECT channel_id FROM found_channels LIMIT 1")
            except sqlite3.OperationalError:
                try:
                    cursor.execute("ALTER TABLE found_channels ADD COLUMN channel_id INTEGER")
                except:
                    pass

            # Миграция: is_pinned - «замочек». Закреплённые каналы никогда не
            # удаляются авточисткой и переживают смену аккаунта (таблица общая).
            try:
                cursor.execute("SELECT is_pinned FROM found_channels LIMIT 1")
            except sqlite3.OperationalError:
                try:
                    cursor.execute("ALTER TABLE found_channels ADD COLUMN is_pinned INTEGER DEFAULT 0")
                except:
                    pass
            
            # Миграция: добавляем колонки для health monitoring в accounts
            health_columns = [
                ('consecutive_errors', 'INTEGER DEFAULT 0'),
                ('last_success_at', 'TIMESTAMP'),
                ('health_status', "TEXT DEFAULT 'healthy'"),
                ('rate_limited_until', 'TIMESTAMP')
            ]
            for col_name, col_type in health_columns:
                try:
                    cursor.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass  # Колонка уже существует
            
            # Таблица отправленных комментариев
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    comment_text TEXT NOT NULL,
                    post_id INTEGER,
                    message_id INTEGER,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_sent_comments_channel ON sent_comments(channel, sent_at)
            ''')
            
            # Миграция: добавляем колонку message_id если её нет
            cursor.execute("PRAGMA table_info(sent_comments)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'message_id' not in columns:
                cursor.execute('ALTER TABLE sent_comments ADD COLUMN message_id INTEGER')
            
            # Миграция: добавляем колонку message_type (comment/chat) и chat_id
            if 'message_type' not in columns:
                cursor.execute("ALTER TABLE sent_comments ADD COLUMN message_type TEXT DEFAULT 'comment'")
            if 'chat_id' not in columns:
                cursor.execute("ALTER TABLE sent_comments ADD COLUMN chat_id INTEGER")
            if 'reply_to_id' not in columns:
                cursor.execute("ALTER TABLE sent_comments ADD COLUMN reply_to_id INTEGER")

            # Таблица собственных каналов (созданных аккаунтом)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS own_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    username TEXT,
                    title TEXT,
                    description TEXT,
                    invite_link TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица очереди постов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS post_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    content_text TEXT,
                    media_path TEXT,
                    media_type TEXT,
                    format_type TEXT DEFAULT 'md',
                    scheduled_at TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    posted_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица спарсенных пользователей для инвайтера
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS parsed_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    source_chat_id INTEGER,
                    source_chat_title TEXT,
                    parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_id, user_id)
                )
            ''')

            # Таблица результатов инвайтов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS invite_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT,
                    error_message TEXT,
                    invited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица кампаний массовой рассылки
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mass_send_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    name TEXT,
                    message_template TEXT NOT NULL,
                    media_path TEXT,
                    media_type TEXT,
                    target_type TEXT DEFAULT 'dm',
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица результатов массовой рассылки
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mass_send_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    target_id TEXT NOT NULL,
                    target_type TEXT,
                    status TEXT,
                    error_message TEXT,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Индексы для новых таблиц
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_own_channels_account ON own_channels(account_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_post_queue_status ON post_queue(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_parsed_users_account ON parsed_users(account_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_invite_stats_account ON invite_stats(account_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mass_send_results_campaign ON mass_send_results(campaign_id)')

            # Глобальные исключения каналов (НЕ account-local privacy/ban).
            # Privacy/ban остаются в channel_bans; сюда — только явный global exclude.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channel_global_exclusions (
                    channel TEXT PRIMARY KEY,
                    channel_id INTEGER,
                    reason TEXT NOT NULL,
                    evidence TEXT,
                    source_module TEXT,
                    excluded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Совместимая миграция для баз, где таблица уже была создана ранней версией.
            cursor.execute("PRAGMA table_info(channel_global_exclusions)")
            exclusion_cols = {col[1] for col in cursor.fetchall()}
            for col_name, col_type in (
                ('channel_id', 'INTEGER'),
                ('evidence', 'TEXT'),
                ('source_module', 'TEXT'),
                ('updated_at', 'TIMESTAMP'),
            ):
                if col_name not in exclusion_cols:
                    try:
                        cursor.execute(
                            f"ALTER TABLE channel_global_exclusions ADD COLUMN {col_name} {col_type}"
                        )
                    except sqlite3.OperationalError:
                        pass
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_global_exclusions_reason "
                "ON channel_global_exclusions(reason)"
            )

            # Junction: пользователь мог быть спарсен из нескольких source-чатов.
            # parsed_users.source_chat_id сохраняем для совместимости (первый/последний),
            # а фильтрация по источнику идёт через junction.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS parsed_user_sources (
                    account_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    source_chat_id INTEGER NOT NULL,
                    source_chat_title TEXT,
                    parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, user_id, source_chat_id)
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_parsed_user_sources_src '
                'ON parsed_user_sources(account_id, source_chat_id)'
            )

            # Миграция lifecycle-колонок кампаний mass_send
            cursor.execute("PRAGMA table_info(mass_send_campaigns)")
            campaign_cols = {col[1] for col in cursor.fetchall()}
            for col_name, col_type in (
                ('total_targets', 'INTEGER DEFAULT 0'),
                ('processed_count', 'INTEGER DEFAULT 0'),
                ('error_message', 'TEXT'),
                ('finished_at', 'TIMESTAMP'),
            ):
                if col_name not in campaign_cols:
                    try:
                        cursor.execute(
                            f"ALTER TABLE mass_send_campaigns ADD COLUMN {col_name} {col_type}"
                        )
                    except sqlite3.OperationalError:
                        pass

            # Backfill junction from legacy source_chat_id on parsed_users
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO parsed_user_sources
                        (account_id, user_id, source_chat_id, source_chat_title, parsed_at)
                    SELECT account_id, user_id, source_chat_id, source_chat_title, parsed_at
                    FROM parsed_users
                    WHERE source_chat_id IS NOT NULL
                ''')
            except sqlite3.OperationalError:
                pass

    # --- Global channel exclusions (structural / operator-driven only) ---
    def exclude_channel_globally(
        self,
        channel: str,
        reason: str = "structural_no_comments",
        channel_id: int = None,
        evidence=None,
        source_module: str = None,
    ) -> bool:
        """
        Добавляет канал в общий постоянный exclude-list.
        Вызывать только для структурных причин, подтверждённых данными Telegram
        (например, успешный GetFullChannelRequest без linked_chat_id), а не для
        ошибок доступа отдельного аккаунта.
        """
        normalized = (channel or "").lstrip("@").strip().casefold()
        if not normalized:
            return False
        if evidence is None:
            evidence_json = None
        elif isinstance(evidence, str):
            evidence_json = evidence
        else:
            evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)
        now = datetime.now()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO channel_global_exclusions
                    (channel, channel_id, reason, evidence, source_module, excluded_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel) DO UPDATE SET
                    channel_id = COALESCE(excluded.channel_id, channel_global_exclusions.channel_id),
                    reason = excluded.reason,
                    evidence = COALESCE(excluded.evidence, channel_global_exclusions.evidence),
                    source_module = COALESCE(excluded.source_module, channel_global_exclusions.source_module),
                    updated_at = excluded.updated_at
                ''',
                (
                    normalized,
                    channel_id,
                    reason or "structural_no_comments",
                    evidence_json,
                    source_module,
                    now,
                    now,
                ),
            )
            cursor.execute(
                "UPDATE found_channels SET can_comment = 0, status = 'rejected' "
                "WHERE LOWER(channel) = ?",
                (normalized,),
            )
            return True

    def is_channel_globally_excluded(self, channel: str) -> bool:
        normalized = (channel or "").lstrip("@").strip().casefold()
        if not normalized:
            return False
        variants = {
            normalized,
            normalized.lstrip("+"),
            "+" + normalized.lstrip("+"),
        }
        with self.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in variants)
            cursor.execute(
                f"SELECT 1 FROM channel_global_exclusions "
                f"WHERE LOWER(channel) IN ({placeholders}) LIMIT 1",
                tuple(variants),
            )
            return cursor.fetchone() is not None

    def list_global_exclusions(self, limit: int = 500) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT channel, channel_id, reason, evidence, source_module,
                       excluded_at, updated_at
                FROM channel_global_exclusions
                ORDER BY COALESCE(updated_at, excluded_at) DESC
                LIMIT ?
                ''',
                (max(1, min(int(limit), 5000)),),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                if item.get("evidence"):
                    try:
                        item["evidence"] = json.loads(item["evidence"])
                    except (TypeError, ValueError):
                        pass
                rows.append(item)
            return rows

    # --- Найденные каналы ---
    def add_found_channel(self, channel: str, title: str, keyword: str = "", source: str = "search", 
                          subs: int = 0, views: int = 0, can_comment: bool = False, min_subs: int = 500) -> bool:
        """
        Добавляет найденный канал в базу.
        
        Args:
            min_subs: Минимальное количество подписчиков (по умолчанию 500)
            
        Returns:
            True если канал добавлен (новый), False если уже существовал или отфильтрован
        """
        normalized_channel = (channel or '').lstrip('@').strip().casefold()

        # Global exclusion gate — never re-add excluded channels
        if self.is_channel_globally_excluded(normalized_channel):
            return False
        
        # Фильтруем маленькие каналы (если известно количество подписчиков)
        if subs > 0 and subs < min_subs:
            return False
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Проверяем, существует ли канал
            cursor.execute("SELECT 1 FROM found_channels WHERE channel = ?", (normalized_channel,))
            is_new = cursor.fetchone() is None
            
            cursor.execute('''
                INSERT INTO found_channels (channel, title, keyword, source, subs_count, avg_views, can_comment, last_checked, found_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel) DO UPDATE SET
                title = excluded.title,
                subs_count = CASE WHEN excluded.subs_count > 0 THEN excluded.subs_count ELSE subs_count END,
                avg_views = CASE WHEN excluded.avg_views > 0 THEN excluded.avg_views ELSE avg_views END,
                can_comment = excluded.can_comment,
                last_checked = excluded.last_checked
            ''', (normalized_channel, title, keyword, source, subs, views, 1 if can_comment else 0, datetime.now(), datetime.now()))
            
            return is_new

    def get_found_channels(self, status: str = None, limit: int = 100, only_open_comments: bool = True) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM found_channels"
            params = []
            conditions = []
            
            if status:
                conditions.append("status = ?")
                params.append(status)
            
            # По умолчанию показываем только каналы с открытыми комментами
            if only_open_comments:
                conditions.append("can_comment = 1")

            # Скрываем глобально исключённые
            conditions.append(
                "LOWER(channel) NOT IN (SELECT LOWER(channel) FROM channel_global_exclusions)"
            )
            
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            query += " ORDER BY found_at DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_channel_info(self, channel: str) -> Optional[Dict]:
        """Получает информацию о канале по его идентификатору"""
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM found_channels WHERE channel = ?", (normalized_channel,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_channel_info(self, channel: str, channel_id: int = None, title: str = None):
        """Обновляет информацию о канале (channel_id и/или title)"""
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            updates = []
            params = []
            if channel_id is not None:
                updates.append("channel_id = ?")
                params.append(channel_id)
            if title is not None:
                updates.append("title = ?")
                params.append(title)
            if updates:
                params.append(normalized_channel)
                cursor.execute(f"UPDATE found_channels SET {', '.join(updates)} WHERE channel = ?", params)

    def delete_found_channel(self, channel: str, force: bool = False) -> bool:
        """
        Удаляет канал из базы найденных каналов.
        Каналы с source='manual' или 'manual_join' не удаляются автоматически (только с force=True).
        При force=True также добавляет канал в список удалённых чтобы не добавлять снова.
        """
        original_channel = channel  # Сохраняем оригинал
        
        # Нормализуем канал - извлекаем хэш из полной ссылки
        normalized_channel = channel
        if 't.me/' in normalized_channel:
            parts = normalized_channel.split('t.me/')
            if len(parts) > 1:
                normalized_channel = parts[-1]
        if normalized_channel.startswith('joinchat/'):
            normalized_channel = '+' + normalized_channel[9:]
        normalized_channel = normalized_channel.lstrip('@')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Создаём таблицу deleted_channels если её нет
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS deleted_channels (
                    channel TEXT PRIMARY KEY,
                    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            deleted_count = 0
            
            if force:
                # Принудительное удаление (например из UI) - пробуем оба варианта
                # Сначала по оригиналу (как в базе)
                cursor.execute("DELETE FROM found_channels WHERE channel = ?", (original_channel,))
                deleted_count += cursor.rowcount
                
                # Потом по нормализованному
                if original_channel != normalized_channel:
                    cursor.execute("DELETE FROM found_channels WHERE channel = ?", (normalized_channel,))
                    deleted_count += cursor.rowcount
                
                # Запоминаем оба варианта чтобы не добавлять снова
                cursor.execute("INSERT OR REPLACE INTO deleted_channels (channel) VALUES (?)", (normalized_channel,))
                if original_channel != normalized_channel:
                    cursor.execute("INSERT OR REPLACE INTO deleted_channels (channel) VALUES (?)", (original_channel,))
            else:
                # Автоматическое удаление - не трогаем manual и закреплённые (замочек) каналы
                cursor.execute(
                    "DELETE FROM found_channels WHERE channel = ? AND source NOT IN ('manual', 'manual_join') AND COALESCE(is_pinned, 0) = 0", 
                    (normalized_channel,)
                )
                deleted_count = cursor.rowcount
            
            return deleted_count > 0
    
    def is_channel_deleted(self, channel: str) -> bool:
        """Проверяет, был ли канал удалён пользователем"""
        # Нормализуем канал
        normalized_channel = channel.lstrip('@')
        
        # Также проверяем варианты для приватных каналов
        variants = [normalized_channel]
        
        # Если это полная ссылка - извлекаем хэш
        if 't.me/' in normalized_channel:
            parts = normalized_channel.split('t.me/')
            if len(parts) > 1:
                hash_part = parts[-1]
                if hash_part.startswith('joinchat/'):
                    hash_part = '+' + hash_part[9:]
                variants.append(hash_part)
                variants.append(hash_part.lstrip('+'))
        
        # Добавляем вариант с + и без
        if normalized_channel.startswith('+'):
            variants.append(normalized_channel[1:])
        else:
            variants.append('+' + normalized_channel)
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Создаём таблицу если её нет
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS deleted_channels (
                    channel TEXT PRIMARY KEY,
                    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Проверяем все варианты
            placeholders = ','.join(['?' for _ in variants])
            cursor.execute(f"SELECT 1 FROM deleted_channels WHERE channel IN ({placeholders})", variants)
            return cursor.fetchone() is not None
    
    def restore_channel(self, channel: str):
        """Убирает канал из списка удалённых (если пользователь хочет добавить его снова)"""
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM deleted_channels WHERE channel = ?", (normalized_channel,))
    
    def cleanup_closed_channels(self) -> int:
        """Удаляет все каналы с закрытыми комментами из базы (кроме manual и закреплённых)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            where = "can_comment = 0 AND source NOT IN ('manual', 'manual_join') AND COALESCE(is_pinned, 0) = 0"
            cursor.execute(f"SELECT COUNT(*) FROM found_channels WHERE {where}")
            count = cursor.fetchone()[0]
            cursor.execute(f"DELETE FROM found_channels WHERE {where}")
            return count

    def set_channel_pinned(self, channel: str, pinned: bool) -> bool:
        """
        Ставит/снимает «замочек» на канале. Закреплённые каналы не удаляются
        авточисткой и остаются в общей базе для всех аккаунтов.
        """
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE found_channels SET is_pinned = ? WHERE channel = ?",
                (1 if pinned else 0, normalized_channel)
            )
            return cursor.rowcount > 0

    def cleanup_unpinned_channels(self, older_than_days: int = None) -> int:
        """
        Удаляет НЕзакреплённые каналы.

        - Всегда сохраняет каналы с замочком (is_pinned=1) и manual-каналы.
        - Если задан older_than_days — удаляет только те, что старше N дней
          (по found_at). Если None — удаляет все незакреплённые/не-manual.

        Returns: количество удалённых записей.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            where = "source NOT IN ('manual', 'manual_join') AND COALESCE(is_pinned, 0) = 0"
            params = []
            if older_than_days is not None and older_than_days > 0:
                where += " AND found_at < datetime('now', ?)"
                params.append(f'-{int(older_than_days)} days')
            cursor.execute(f"SELECT COUNT(*) FROM found_channels WHERE {where}", params)
            count = cursor.fetchone()[0]
            cursor.execute(f"DELETE FROM found_channels WHERE {where}", params)
            return count

    def update_channel_status(self, channel: str, status: str):
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE found_channels SET status = ? WHERE channel = ?", (status, normalized_channel))

    def update_channel_comments_status(
        self,
        channel: str,
        has_open_comments: bool,
        structural: bool = False,
        reason: str = None,
        evidence=None,
        source_module: str = None,
    ):
        """
        Обновляет статус комментариев. structural=True допустим только при
        подтверждённом свойстве самого канала; account-local ошибки остаются
        исключительно в channel_bans.
        """
        normalized_channel = (channel or '').lstrip('@').strip().casefold()
        if structural and not has_open_comments:
            return self.exclude_channel_globally(
                normalized_channel,
                reason=reason or "structural_no_comments",
                evidence=evidence,
                source_module=source_module,
            )

        # Обычное обновление не создаёт permanent exclusion.
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE found_channels SET can_comment = ? WHERE LOWER(channel) = ?",
                (1 if has_open_comments else 0, normalized_channel),
            )
        return True

    def sanitize_channels(self):
        """Очищает базу от двойных @@ в названиях каналов"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Находим каналы, которые начинаются с @ в базе (так как мы храним без @, то может быть мусор)
            # Но если мы храним без @, то @@chan превратится в @chan в базе
            # Проверим channels
            cursor.execute("SELECT channel FROM found_channels")
            channels = cursor.fetchall()
            for row in channels:
                ch = row['channel']
                if ch.startswith('@'):
                    new_ch = ch.lstrip('@')
                    # Пробуем обновить
                    try:
                        cursor.execute("UPDATE OR IGNORE found_channels SET channel = ? WHERE channel = ?", (new_ch, ch))
                        # Если не обновилось (из-за конфликта), удаляем старый с @
                        cursor.execute("DELETE FROM found_channels WHERE channel = ? AND channel != ?", (ch, new_ch))
                    except:
                        pass
    
    @db_retry(max_attempts=5, default=None)
    def mark_banned(self, account_id: int, channel: str):
        """Помечает канал как забаненный для конкретного аккаунта"""
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO channel_bans (account_id, channel)
                VALUES (?, ?)
            ''', (account_id, normalized_channel))

    @db_retry(max_attempts=5, default=False)
    def is_banned(self, account_id: int, channel: str) -> bool:
        """Проверяет, забанен ли аккаунт в данном канале"""
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 1 FROM channel_bans 
                WHERE account_id = ? AND channel = ?
            ''', (account_id, normalized_channel))
            return cursor.fetchone() is not None
    
    # --- Прокси ---
    def add_proxy(self, ip: str, port: int, user: str = None, password: str = None, type: str = "http") -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO proxies (ip, port, username, password, type)
                VALUES (?, ?, ?, ?, ?)
            ''', (ip, port, user, password, type))
            return cursor.lastrowid

    def get_proxies(self) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM proxies")
            return [dict(row) for row in cursor.fetchall()]
    
    def delete_proxy(self, proxy_id: int):
        """Удаляет прокси из базы данных и отвязывает от аккаунтов"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Сначала отвязываем прокси от всех аккаунтов
            cursor.execute("UPDATE accounts SET proxy_id = NULL WHERE proxy_id = ?", (proxy_id,))
            # Затем удаляем прокси
            cursor.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))

    # --- Аккаунты ---
    def add_account(self, phone: str, session_name: str, api_id: int, api_hash: str) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO accounts (phone, session_name, api_id, api_hash)
                VALUES (?, ?, ?, ?)
            ''', (phone, session_name, api_id, api_hash))
            return cursor.lastrowid

    def get_accounts(self) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT a.*, p.ip, p.port, p.username as proxy_user, p.password as proxy_pass, p.type as proxy_type
                FROM accounts a
                LEFT JOIN proxies p ON a.proxy_id = p.id
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_account(self, account_id: int) -> Dict:
        """Получить один аккаунт по ID"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    def assign_proxy_to_account(self, account_id: int, proxy_id: Optional[int]):
        """Привязывает или отвязывает прокси от аккаунта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE accounts SET proxy_id = ? WHERE id = ?", (proxy_id, account_id))

    def delete_account(self, account_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Удаляем аккаунт
            cursor.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            # Удаляем связанные данные
            cursor.execute("DELETE FROM processed_posts WHERE account_id = ?", (account_id,))
            cursor.execute("DELETE FROM dialog_history WHERE account_id = ?", (account_id,))
            cursor.execute("DELETE FROM logs WHERE account_id = ?", (account_id,))

    def update_account_status(self, account_id: int, status: str):
        """Обновляет статус аккаунта (active, banned, frozen, frozen_join, deactivated)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account_id))

    # --- Обработанные посты ---
    @db_retry(max_attempts=5, default=False)
    def is_post_processed(self, account_id: int, channel: str, post_id: int) -> bool:
        if not channel:
            return False
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 1 FROM processed_posts 
                WHERE account_id = ? AND (channel = ? OR channel = ?) AND post_id = ?
            ''', (account_id, normalized_channel, f"@{normalized_channel}", post_id))
            return cursor.fetchone() is not None

    @db_retry(max_attempts=5, default=False)
    def is_post_processed_by_any(self, channel: str, post_id: int) -> bool:
        """Проверяет, обработан ли пост ЛЮБЫМ аккаунтом (для предотвращения дублей)"""
        if not channel:
            return False
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 1 FROM processed_posts 
                WHERE (channel = ? OR channel = ?) AND post_id = ?
            ''', (normalized_channel, f"@{normalized_channel}", post_id))
            return cursor.fetchone() is not None

    @db_retry(max_attempts=5, default=None)
    def mark_post_processed(self, account_id: int, channel: str, post_id: int):
        if not channel:
            return
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO processed_posts (account_id, channel, post_id)
                VALUES (?, ?, ?)
            ''', (account_id, normalized_channel, post_id))

    # --- Диалоги и Автоответчик ---
    def get_dialog_history(self, user_id: int, limit: int = 10, max_age_hours: int = 24) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT role, content, timestamp 
                FROM dialog_history 
                WHERE user_id = ? AND (strftime('%s', 'now') - timestamp) < ? * 3600
                ORDER BY timestamp DESC LIMIT ?
            ''', (user_id, max_age_hours, limit))
            return [dict(row) for row in reversed(cursor.fetchall())]

    def save_message(self, user_id: int, role: str, content: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            msk_tz = timezone(timedelta(hours=3))
            now_msk = datetime.now(msk_tz)
            cursor.execute('''
                INSERT INTO dialog_history (user_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (user_id, role, content, now_msk.timestamp()))

    def get_recent_messages_count(self, user_id: int, minutes: int = 5) -> int:
        """Возвращает количество сообщений ботом данному пользователю за последние N минут"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            msk_tz = timezone(timedelta(hours=3))
            now_ts = datetime.now(msk_tz).timestamp()
            limit_ts = now_ts - (minutes * 60)
            
            cursor.execute('''
                SELECT count(*) as cnt 
                FROM dialog_history 
                WHERE user_id = ? AND role = 'assistant' AND timestamp > ?
            ''', (user_id, limit_ts))
            row = cursor.fetchone()
            return row['cnt'] if row else 0

    def get_message_count(self, user_id: int) -> int:
        # Для простоты храним счетчик в таблице settings или отдельной, 
        # но сейчас просто посчитаем сообщения от пользователя
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM dialog_history WHERE user_id = ? AND role = 'user'", (user_id,))
            return cursor.fetchone()[0]

    def increment_message_count(self, user_id: int):
        # Нам не нужно это отдельно, если мы считаем через get_message_count
        pass

    # --- Статистика ---
    def increment_stat(self, account_id: int, metric: str, amount: int = 1):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO account_stats (account_id, metric, value)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id, metric) DO UPDATE SET
                value = value + ?,
                updated_at = CURRENT_TIMESTAMP
            ''', (account_id, metric, amount, amount))
    
    def get_stats_summary(self, account_id: int) -> Dict[str, int]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Комментарии - считаем только УСПЕШНО отправленные из daily_stats
            cursor.execute("SELECT COALESCE(SUM(success_count), 0) FROM daily_stats WHERE account_id = ?", (account_id,))
            comments = cursor.fetchone()[0]
            
            # Входящие сообщения (dialog_history, role='user')
            cursor.execute("SELECT COUNT(*) FROM dialog_history WHERE account_id = ? AND role = 'user'", (account_id,))
            incoming_msgs = cursor.fetchone()[0]
            
            # Лайки (из account_stats)
            cursor.execute("SELECT value FROM account_stats WHERE account_id = ? AND metric = 'likes'", (account_id,))
            row = cursor.fetchone()
            likes = row['value'] if row else 0
            
            return {
                "comments": comments,
                "incoming_messages": incoming_msgs,
                "likes": likes
            }

    # --- Настройки ---
    def set_setting(self, key: str, value: Any):
        """Сохраняет настройку с обработкой ошибок блокировки"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        except sqlite3.OperationalError as e:
            print(f"[DB ERROR] set_setting({key}): {e}")
            raise

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Получает настройку с обработкой ошибок блокировки"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
                row = cursor.fetchone()
                return json.loads(row['value']) if row else default
        except sqlite3.OperationalError as e:
            print(f"[DB ERROR] get_setting({key}): {e}, returning default")
            return default
        except Exception as e:
            print(f"[DB ERROR] get_setting({key}): {e}, returning default")
            return default

    # --- Логи ---
    def add_log(self, account_id: Optional[int], level: str, message: str):
        """Добавляет запись лога с обработкой ошибок блокировки БД"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # Московское время (UTC+3)
                msk_tz = timezone(timedelta(hours=3))
                now_msk = datetime.now(msk_tz)
                ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
                
                cursor.execute('''
                    INSERT INTO logs (account_id, level, message, timestamp)
                    VALUES (?, ?, ?, ?)
                ''', (account_id, level, message, ts_str))
        except sqlite3.OperationalError as e:
            # Если БД заблокирована, просто выводим в консоль
            print(f"[LOG FALLBACK] [{level}] {message} (DB error: {e})")
        except Exception as e:
            print(f"[LOG FALLBACK] [{level}] {message} (Error: {e})")

    def add_structured_log(
        self, 
        account_id: Optional[int], 
        level: str, 
        message: str,
        module: Optional[str] = None,
        stack_trace: Optional[str] = None,
        extra_json: Optional[str] = None
    ):
        """Добавляет структурированную запись лога с дополнительными полями"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                msk_tz = timezone(timedelta(hours=3))
                now_msk = datetime.now(msk_tz)
                ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
                
                cursor.execute('''
                    INSERT INTO logs (account_id, level, message, timestamp, module, stack_trace, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (account_id, level, message, ts_str, module, stack_trace, extra_json))
        except sqlite3.OperationalError as e:
            print(f"[LOG FALLBACK] [{level}] {message} (DB error: {e})")
        except Exception as e:
            print(f"[LOG FALLBACK] [{level}] {message} (Error: {e})")

    def get_logs(self, limit: int = 100, account_id: Optional[int] = None,
                 level: Optional[str] = None, search: Optional[str] = None) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT l.*, a.phone FROM logs l LEFT JOIN accounts a ON l.account_id = a.id"
            params = []
            conditions = []
            
            if account_id:
                conditions.append("l.account_id = ?")
                params.append(account_id)
            if level:
                # Поддержка нескольких уровней через запятую: "warning,error"
                levels = [x.strip() for x in str(level).split(",") if x.strip()]
                if len(levels) == 1:
                    conditions.append("l.level = ?")
                    params.append(levels[0])
                elif len(levels) > 1:
                    conditions.append("l.level IN (%s)" % ",".join("?" * len(levels)))
                    params.extend(levels)
            if search:
                conditions.append("l.message LIKE ?")
                params.append(f"%{search}%")
            
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            query += " ORDER BY l.timestamp DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_log_level_counts(self, hours: int = 24) -> Dict[str, int]:
        """Возвращает количество логов по уровням за последние N часов (для бейджей)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                msk_tz = timezone(timedelta(hours=3))
                since = (datetime.now(msk_tz) - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute(
                    "SELECT level, COUNT(*) AS c FROM logs WHERE timestamp >= ? GROUP BY level",
                    (since,)
                )
                return {row["level"]: row["c"] for row in cursor.fetchall()}
        except Exception:
            return {}

    def cleanup_old_logs(self, days: int = 7) -> int:
        """
        Удаляет логи старше указанного количества дней.
        
        Args:
            days: Количество дней (по умолчанию 7)
            
        Returns:
            Количество удалённых записей
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            msk_tz = timezone(timedelta(hours=3))
            cutoff = datetime.now(msk_tz) - timedelta(days=days)
            cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
            
            # Считаем сколько удалим
            cursor.execute("SELECT COUNT(*) FROM logs WHERE timestamp < ?", (cutoff_str,))
            count = cursor.fetchone()[0]
            
            # Удаляем
            cursor.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff_str,))
            
            return count

    # --- Health Monitoring ---
    def record_success(self, account_id: int):
        """Записывает успешную операцию и сбрасывает счётчик ошибок"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            msk_tz = timezone(timedelta(hours=3))
            now_msk = datetime.now(msk_tz)
            ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
            
            cursor.execute('''
                UPDATE accounts 
                SET consecutive_errors = 0,
                    last_success_at = ?,
                    health_status = 'healthy'
                WHERE id = ?
            ''', (ts_str, account_id))
    
    def record_error(self, account_id: int) -> str:
        """
        Записывает ошибку и обновляет статус здоровья.
        
        Returns:
            Новый статус здоровья (healthy, warning, critical, paused)
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Получаем текущее количество ошибок
            cursor.execute("SELECT consecutive_errors FROM accounts WHERE id = ?", (account_id,))
            row = cursor.fetchone()
            current_errors = row['consecutive_errors'] if row else 0
            
            new_errors = current_errors + 1
            
            # Определяем новый статус
            if new_errors >= 5:
                new_status = 'paused'
            elif new_errors >= 3:
                new_status = 'critical'
            elif new_errors >= 1:
                new_status = 'warning'
            else:
                new_status = 'healthy'
            
            cursor.execute('''
                UPDATE accounts 
                SET consecutive_errors = ?,
                    health_status = ?
                WHERE id = ?
            ''', (new_errors, new_status, account_id))
            
            return new_status
    
    def record_flood_wait(self, account_id: int, seconds: int):
        """
        Записывает FloodWait и помечает аккаунт как rate-limited если > 1 часа.
        
        Args:
            account_id: ID аккаунта
            seconds: Количество секунд FloodWait
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            msk_tz = timezone(timedelta(hours=3))
            
            # Вычисляем время окончания rate limit
            rate_limited_until = datetime.now(msk_tz) + timedelta(seconds=seconds + 10)
            ts_str = rate_limited_until.strftime('%Y-%m-%d %H:%M:%S')
            
            # Если FloodWait > 1 часа, помечаем как rate-limited
            if seconds > 3600:
                cursor.execute('''
                    UPDATE accounts 
                    SET rate_limited_until = ?,
                        health_status = 'rate_limited'
                    WHERE id = ?
                ''', (ts_str, account_id))
            else:
                cursor.execute('''
                    UPDATE accounts 
                    SET rate_limited_until = ?
                    WHERE id = ?
                ''', (ts_str, account_id))
    
    def get_account_health(self, account_id: int) -> Dict:
        """Возвращает информацию о здоровье аккаунта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT consecutive_errors, last_success_at, health_status, rate_limited_until
                FROM accounts WHERE id = ?
            ''', (account_id,))
            row = cursor.fetchone()
            
            if row:
                return {
                    'consecutive_errors': row['consecutive_errors'] or 0,
                    'last_success_at': row['last_success_at'],
                    'health_status': row['health_status'] or 'healthy',
                    'rate_limited_until': row['rate_limited_until']
                }
            return {
                'consecutive_errors': 0,
                'last_success_at': None,
                'health_status': 'unknown',
                'rate_limited_until': None
            }
    
    def should_pause_account(self, account_id: int) -> bool:
        """Проверяет, нужно ли приостановить аккаунт"""
        return self.get_account_pause_seconds(account_id) > 0

    def get_account_pause_seconds(self, account_id: int) -> int:
        """
        Сколько секунд ещё действует pause/rate-limit для аккаунта.
        0 = можно работать. Учитывает health_status и rate_limited_until.
        """
        health = self.get_account_health(account_id)
        msk_tz = timezone(timedelta(hours=3))
        now = datetime.now(msk_tz)

        remaining = 0

        if health.get('rate_limited_until'):
            try:
                rate_limited = datetime.strptime(
                    health['rate_limited_until'], '%Y-%m-%d %H:%M:%S'
                ).replace(tzinfo=msk_tz)
                if now < rate_limited:
                    remaining = max(remaining, int((rate_limited - now).total_seconds()))
            except Exception:
                pass

        # paused without an explicit until → treat as still paused
        if health.get('health_status') in ('paused', 'rate_limited') and remaining <= 0:
            # If status says paused but no until timestamp, report a positive sentinel
            if health.get('health_status') == 'paused' and not health.get('rate_limited_until'):
                return 86400
            if health.get('health_status') == 'rate_limited' and not health.get('rate_limited_until'):
                return 3600

        return max(0, remaining)

    def reset_account_health(self, account_id: int):
        """Сбрасывает статус здоровья аккаунта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE accounts 
                SET consecutive_errors = 0,
                    health_status = 'healthy',
                    rate_limited_until = NULL
                WHERE id = ?
            ''', (account_id,))

    # --- Daily Statistics ---
    def increment_daily_stat(self, account_id: int, channel: str, success: bool = True):
        """
        Инкрементирует дневную статистику для аккаунта/канала.
        
        Args:
            account_id: ID аккаунта
            channel: Название канала
            success: True для успешной операции, False для ошибки
        """
        normalized_channel = channel.lstrip('@')
        msk_tz = timezone(timedelta(hours=3))
        today = datetime.now(msk_tz).strftime('%Y-%m-%d')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            if success:
                cursor.execute('''
                    INSERT INTO daily_stats (account_id, channel, date, comments_count, success_count, error_count)
                    VALUES (?, ?, ?, 1, 1, 0)
                    ON CONFLICT(account_id, channel, date) DO UPDATE SET
                    comments_count = comments_count + 1,
                    success_count = success_count + 1
                ''', (account_id, normalized_channel, today))
            else:
                cursor.execute('''
                    INSERT INTO daily_stats (account_id, channel, date, comments_count, success_count, error_count)
                    VALUES (?, ?, ?, 0, 0, 1)
                    ON CONFLICT(account_id, channel, date) DO UPDATE SET
                    error_count = error_count + 1
                ''', (account_id, normalized_channel, today))
    
    def get_daily_stats(self, account_id: int, days: int = 7) -> List[Dict]:
        """
        Возвращает дневную статистику за последние N дней.
        
        Args:
            account_id: ID аккаунта
            days: Количество дней (по умолчанию 7)
            
        Returns:
            Список записей статистики
        """
        msk_tz = timezone(timedelta(hours=3))
        cutoff = (datetime.now(msk_tz) - timedelta(days=days)).strftime('%Y-%m-%d')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT date, channel, comments_count, success_count, error_count
                FROM daily_stats
                WHERE account_id = ? AND date >= ?
                ORDER BY date DESC, channel
            ''', (account_id, cutoff))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_success_rate(self, account_id: int, days: int = 7) -> float:
        """
        Возвращает процент успешных операций за последние N дней.
        
        Args:
            account_id: ID аккаунта
            days: Количество дней
            
        Returns:
            Процент успешных операций (0.0 - 100.0)
        """
        msk_tz = timezone(timedelta(hours=3))
        cutoff = (datetime.now(msk_tz) - timedelta(days=days)).strftime('%Y-%m-%d')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    SUM(success_count) as total_success,
                    SUM(error_count) as total_errors
                FROM daily_stats
                WHERE account_id = ? AND date >= ?
            ''', (account_id, cutoff))
            row = cursor.fetchone()
            
            if row:
                total_success = row['total_success'] or 0
                total_errors = row['total_errors'] or 0
                total = total_success + total_errors
                
                if total > 0:
                    return (total_success / total) * 100.0
            
            return 0.0
    
    def get_aggregated_daily_stats(self, account_id: int, days: int = 7) -> List[Dict]:
        """
        Возвращает агрегированную статистику по дням.
        
        Args:
            account_id: ID аккаунта
            days: Количество дней
            
        Returns:
            Список с агрегированной статистикой по дням
        """
        msk_tz = timezone(timedelta(hours=3))
        cutoff = (datetime.now(msk_tz) - timedelta(days=days)).strftime('%Y-%m-%d')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    date,
                    SUM(comments_count) as total_comments,
                    SUM(success_count) as total_success,
                    SUM(error_count) as total_errors,
                    COUNT(DISTINCT channel) as channels_count
                FROM daily_stats
                WHERE account_id = ? AND date >= ?
                GROUP BY date
                ORDER BY date DESC
            ''', (account_id, cutoff))
            return [dict(row) for row in cursor.fetchall()]


    # --- Comment Tracking ---
    def save_comment(self, account_id: int, channel: str, comment_text: str, post_id: int, message_id: int = None):
        """
        Сохраняет отправленный комментарий для проверки дубликатов.
        
        Args:
            account_id: ID аккаунта
            channel: Название канала
            comment_text: Текст комментария
            post_id: ID поста
            message_id: ID отправленного сообщения (для ссылки)
        """
        normalized_channel = channel.lstrip('@')
        msk_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(msk_tz)
        ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sent_comments (account_id, channel, comment_text, post_id, message_id, sent_at, message_type)
                VALUES (?, ?, ?, ?, ?, ?, 'comment')
            ''', (account_id, normalized_channel, comment_text, post_id, message_id, ts_str))
    
    def save_chat_message(self, account_id: int, chat_name: str, chat_id: int, message_text: str, 
                          message_id: int = None, reply_to_id: int = None):
        """
        Сохраняет отправленное сообщение в чате.
        
        Args:
            account_id: ID аккаунта
            chat_name: Название чата
            chat_id: ID чата (для ссылки)
            message_text: Текст сообщения
            message_id: ID отправленного сообщения
            reply_to_id: ID сообщения на которое отвечали
        """
        msk_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(msk_tz)
        ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sent_comments (account_id, channel, comment_text, message_id, sent_at, message_type, chat_id, reply_to_id)
                VALUES (?, ?, ?, ?, ?, 'chat', ?, ?)
            ''', (account_id, chat_name, message_text, message_id, ts_str, chat_id, reply_to_id))
    
    def get_recent_comments(self, channel: str, hours: int = 24) -> List[str]:
        """
        Возвращает список недавних комментариев в канале.
        
        Args:
            channel: Название канала
            hours: Количество часов для выборки (по умолчанию 24)
            
        Returns:
            Список текстов комментариев
        """
        normalized_channel = channel.lstrip('@')
        msk_tz = timezone(timedelta(hours=3))
        cutoff = (datetime.now(msk_tz) - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Проверяем существование таблицы
            cursor.execute('''
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='sent_comments'
            ''')
            if not cursor.fetchone():
                return []
            
            cursor.execute('''
                SELECT comment_text FROM sent_comments
                WHERE channel = ? AND sent_at >= ?
                ORDER BY sent_at DESC
            ''', (normalized_channel, cutoff))
            
            return [row['comment_text'] for row in cursor.fetchall()]
    
    def is_comment_duplicate(self, channel: str, comment_text: str, hours: int = 24) -> bool:
        """
        Проверяет, является ли комментарий дубликатом.
        
        Args:
            channel: Название канала
            comment_text: Текст комментария для проверки
            hours: Период проверки в часах
            
        Returns:
            True если комментарий уже был отправлен
        """
        recent_comments = self.get_recent_comments(channel, hours)
        
        # Нормализуем текст для сравнения
        normalized_new = comment_text.lower().strip()
        
        for existing in recent_comments:
            normalized_existing = existing.lower().strip()
            # Точное совпадение
            if normalized_new == normalized_existing:
                return True
            # Высокое сходство (>90% символов совпадают)
            if len(normalized_new) > 10 and len(normalized_existing) > 10:
                common = sum(1 for a, b in zip(normalized_new, normalized_existing) if a == b)
                similarity = common / max(len(normalized_new), len(normalized_existing))
                if similarity > 0.9:
                    return True
        
        return False

    def get_all_comments(self, limit: int = 100, account_id: int = None) -> List[Dict]:
        """Возвращает все отправленные комментарии и сообщения в чатах"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Проверяем существует ли таблица
            cursor.execute('''
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='sent_comments'
            ''')
            if not cursor.fetchone():
                return []
            
            if account_id:
                cursor.execute('''
                    SELECT sc.id, sc.account_id, sc.channel, sc.comment_text, sc.post_id, sc.message_id, sc.sent_at,
                           a.phone as account_phone,
                           COALESCE(sc.message_type, 'comment') as message_type,
                           sc.chat_id, sc.reply_to_id
                    FROM sent_comments sc
                    LEFT JOIN accounts a ON sc.account_id = a.id
                    WHERE sc.account_id = ?
                    ORDER BY sc.sent_at DESC
                    LIMIT ?
                ''', (account_id, limit))
            else:
                cursor.execute('''
                    SELECT sc.id, sc.account_id, sc.channel, sc.comment_text, sc.post_id, sc.message_id, sc.sent_at,
                           a.phone as account_phone,
                           COALESCE(sc.message_type, 'comment') as message_type,
                           sc.chat_id, sc.reply_to_id
                    FROM sent_comments sc
                    LEFT JOIN accounts a ON sc.account_id = a.id
                    ORDER BY sc.sent_at DESC
                    LIMIT ?
                ''', (limit,))
            
            return [dict(row) for row in cursor.fetchall()]

    def get_comment_by_id(self, comment_id: int) -> Optional[Dict]:
        """Возвращает комментарий по ID"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT sc.*, a.phone as account_phone
                FROM sent_comments sc
                LEFT JOIN accounts a ON sc.account_id = a.id
                WHERE sc.id = ?
            ''', (comment_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_comment_text(self, comment_id: int, new_text: str) -> bool:
        """Обновляет текст комментария в БД (для синхронизации после редактирования)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE sent_comments SET comment_text = ? WHERE id = ?
            ''', (new_text, comment_id))
            return cursor.rowcount > 0

    def delete_comment_record(self, comment_id: int) -> bool:
        """Удаляет запись о комментарии из БД"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM sent_comments WHERE id = ?', (comment_id,))
            return cursor.rowcount > 0

    # --- Global Statistics ---
    def get_global_stats(self) -> Dict:
        """
        Возвращает глобальную статистику по всем каналам и аккаунтам.
        
        Returns:
            Словарь с глобальной статистикой
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Всего каналов в базе
            cursor.execute("SELECT COUNT(*) FROM found_channels")
            total_channels = cursor.fetchone()[0]
            
            # Каналы с открытыми комментами
            cursor.execute("SELECT COUNT(*) FROM found_channels WHERE can_comment = 1")
            open_comments_channels = cursor.fetchone()[0]
            
            # Каналы с закрытыми комментами
            cursor.execute("SELECT COUNT(*) FROM found_channels WHERE can_comment = 0")
            closed_comments_channels = cursor.fetchone()[0]
            
            # Активные каналы (статус active)
            cursor.execute("SELECT COUNT(*) FROM found_channels WHERE status = 'active'")
            active_channels = cursor.fetchone()[0]
            
            # Новые каналы за сегодня
            msk_tz = timezone(timedelta(hours=3))
            today = datetime.now(msk_tz).strftime('%Y-%m-%d')
            cursor.execute("SELECT COUNT(*) FROM found_channels WHERE DATE(found_at) = ?", (today,))
            new_today = cursor.fetchone()[0]
            
            # Всего аккаунтов
            cursor.execute("SELECT COUNT(*) FROM accounts")
            total_accounts = cursor.fetchone()[0]
            
            # Всего комментариев за всё время
            cursor.execute("SELECT SUM(comments_count) FROM daily_stats")
            row = cursor.fetchone()
            total_comments = row[0] if row[0] else 0
            
            # Всего лайков
            cursor.execute("SELECT SUM(value) FROM account_stats WHERE metric = 'likes'")
            row = cursor.fetchone()
            total_likes = row[0] if row[0] else 0
            
            return {
                "total_channels": total_channels,
                "open_comments_channels": open_comments_channels,
                "closed_comments_channels": closed_comments_channels,
                "active_channels": active_channels,
                "new_channels_today": new_today,
                "total_accounts": total_accounts,
                "total_comments": total_comments,
                "total_likes": total_likes
            }
    
    def get_stats_24h(self) -> Dict:
        """
        Возвращает статистику за последние 24 часа.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            msk_tz = timezone(timedelta(hours=3))
            now = datetime.now(msk_tz)
            yesterday = (now - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
            today_date = now.strftime('%Y-%m-%d')
            
            # Комментарии за 24 часа
            cursor.execute("""
                SELECT COUNT(*) FROM sent_comments 
                WHERE sent_at >= ?
            """, (yesterday,))
            comments_24h = cursor.fetchone()[0]
            
            # Успешные комментарии за сегодня из daily_stats
            cursor.execute("""
                SELECT COALESCE(SUM(success_count), 0) FROM daily_stats 
                WHERE date = ?
            """, (today_date,))
            success_today = cursor.fetchone()[0]
            
            # Ошибки за сегодня
            cursor.execute("""
                SELECT COALESCE(SUM(error_count), 0) FROM daily_stats 
                WHERE date = ?
            """, (today_date,))
            errors_today = cursor.fetchone()[0]
            
            # Лайки за 24 часа (из логов)
            cursor.execute("""
                SELECT COUNT(*) FROM logs 
                WHERE timestamp >= ? AND message LIKE '%Лайкнул%'
            """, (yesterday,))
            likes_24h = cursor.fetchone()[0]
            
            # Уникальные каналы с комментариями за 24 часа
            cursor.execute("""
                SELECT COUNT(DISTINCT channel) FROM sent_comments 
                WHERE sent_at >= ?
            """, (yesterday,))
            channels_commented_24h = cursor.fetchone()[0]
            
            # Новые каналы за 24 часа
            cursor.execute("""
                SELECT COUNT(*) FROM found_channels 
                WHERE found_at >= ?
            """, (yesterday,))
            new_channels_24h = cursor.fetchone()[0]
            
            # Активность по аккаунтам за сегодня
            cursor.execute("""
                SELECT account_id, SUM(success_count) as comments
                FROM daily_stats 
                WHERE date = ?
                GROUP BY account_id
                ORDER BY comments DESC
            """, (today_date,))
            accounts_activity = [{"account_id": row[0], "comments": row[1]} for row in cursor.fetchall()]
            
            return {
                "comments_24h": comments_24h,
                "success_today": success_today,
                "errors_today": errors_today,
                "likes_24h": likes_24h,
                "channels_commented_24h": channels_commented_24h,
                "new_channels_24h": new_channels_24h,
                "accounts_activity": accounts_activity
            }
    
    def get_channels_found_since_startup(self, startup_time: str) -> int:
        """
        Возвращает количество каналов, найденных с момента запуска.
        
        Args:
            startup_time: Время запуска в формате 'YYYY-MM-DD HH:MM:SS'
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM found_channels WHERE found_at >= ?", 
                (startup_time,)
            )
            return cursor.fetchone()[0]
    
    # --- Per-Account Bans ---
    @db_retry(max_attempts=5, default=[])
    def get_banned_channels_for_account(self, account_id: int) -> List[str]:
        """
        Возвращает список каналов, в которых аккаунт забанен.
        
        Args:
            account_id: ID аккаунта
            
        Returns:
            Список названий каналов
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT channel FROM channel_bans WHERE account_id = ?",
                (account_id,)
            )
            return [row['channel'] for row in cursor.fetchall()]
    
    def get_accounts_banned_in_channel(self, channel: str) -> List[int]:
        """
        Возвращает список ID аккаунтов, забаненных в канале.
        
        Args:
            channel: Название канала
            
        Returns:
            Список ID аккаунтов
        """
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT account_id FROM channel_bans WHERE channel = ?",
                (normalized_channel,)
            )
            return [row['account_id'] for row in cursor.fetchall()]
    
    def get_ban_stats_for_channel(self, channel: str) -> Dict:
        """
        Возвращает статистику банов для канала.
        
        Args:
            channel: Название канала
            
        Returns:
            Словарь с количеством забаненных и общим количеством аккаунтов
        """
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Количество забаненных аккаунтов
            cursor.execute(
                "SELECT COUNT(*) FROM channel_bans WHERE channel = ?",
                (normalized_channel,)
            )
            banned_count = cursor.fetchone()[0]
            
            # Общее количество аккаунтов
            cursor.execute("SELECT COUNT(*) FROM accounts")
            total_accounts = cursor.fetchone()[0]
            
            return {
                "banned_count": banned_count,
                "total_accounts": total_accounts,
                "all_banned": banned_count >= total_accounts and total_accounts > 0
            }
    
    def check_and_remove_dead_channels(self) -> int:
        """
        Проверяет каналы с закрытыми комментариями и удаляет их из активных.
        НЕ удаляет каналы где все аккаунты забанены — новые аккаунты смогут там работать.
        
        Returns:
            Количество удалённых каналов
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Удаляем только каналы с закрытыми комментариями (для ВСЕХ)
            cursor.execute('''
                UPDATE found_channels 
                SET status = 'rejected' 
                WHERE can_comment = 0 AND status = 'active'
            ''')
            
            return cursor.rowcount
    
    def get_available_channels_for_account(self, account_id: int) -> List[Dict]:
        """
        Возвращает каналы, доступные для аккаунта (не забанен + открыты комменты).
        
        Args:
            account_id: ID аккаунта
            
        Returns:
            Список доступных каналов
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT fc.* FROM found_channels fc
                WHERE fc.can_comment = 1 
                AND fc.status = 'active'
                AND fc.channel NOT IN (
                    SELECT channel FROM channel_bans WHERE account_id = ?
                )
            ''', (account_id,))
            return [dict(row) for row in cursor.fetchall()]
    
    def remove_closed_comments_channels(self) -> int:
        """
        Удаляет каналы с закрытыми комментариями из активных.
        
        Returns:
            Количество удалённых каналов
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE found_channels 
                SET status = 'rejected' 
                WHERE can_comment = 0 AND status = 'active'
            ''')
            return cursor.rowcount


    # --- Channel Locks (координация между аккаунтами) ---
    def lock_channel(self, account_id: int, channel: str, lock_minutes: int = 30) -> bool:
        """
        Блокирует канал для других аккаунтов на указанное время.
        
        Args:
            account_id: ID аккаунта, который блокирует
            channel: Название канала
            lock_minutes: Время блокировки в минутах
            
        Returns:
            True если блокировка успешна, False если канал уже заблокирован
        """
        normalized_channel = channel.lstrip('@')
        msk_tz = timezone(timedelta(hours=3))
        now = datetime.now(msk_tz)
        expires_at = now + timedelta(minutes=lock_minutes)
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Создаём таблицу если её нет
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channel_locks (
                    channel TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    locked_at TIMESTAMP NOT NULL,
                    expires_at TIMESTAMP NOT NULL
                )
            ''')
            
            # Проверяем, не заблокирован ли канал другим аккаунтом
            cursor.execute('''
                SELECT account_id, expires_at FROM channel_locks WHERE channel = ?
            ''', (normalized_channel,))
            row = cursor.fetchone()
            
            if row:
                # Проверяем, не истекла ли блокировка
                try:
                    expires = datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S')
                    expires = expires.replace(tzinfo=msk_tz)
                    if now < expires and row['account_id'] != account_id:
                        return False  # Канал заблокирован другим аккаунтом
                except:
                    pass
            
            # Устанавливаем/обновляем блокировку
            cursor.execute('''
                INSERT OR REPLACE INTO channel_locks (channel, account_id, locked_at, expires_at)
                VALUES (?, ?, ?, ?)
            ''', (normalized_channel, account_id, now.strftime('%Y-%m-%d %H:%M:%S'), 
                  expires_at.strftime('%Y-%m-%d %H:%M:%S')))
            
            return True
    
    def is_channel_locked(self, account_id: int, channel: str) -> bool:
        """
        Проверяет, заблокирован ли канал другим аккаунтом.
        
        Args:
            account_id: ID текущего аккаунта
            channel: Название канала
            
        Returns:
            True если канал заблокирован ДРУГИМ аккаунтом
        """
        normalized_channel = channel.lstrip('@')
        msk_tz = timezone(timedelta(hours=3))
        now = datetime.now(msk_tz)
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Проверяем существование таблицы
            cursor.execute('''
                SELECT name FROM sqlite_master WHERE type='table' AND name='channel_locks'
            ''')
            if not cursor.fetchone():
                return False
            
            cursor.execute('''
                SELECT account_id, expires_at FROM channel_locks WHERE channel = ?
            ''', (normalized_channel,))
            row = cursor.fetchone()
            
            if not row:
                return False
            
            # Если это наш аккаунт — не заблокирован
            if row['account_id'] == account_id:
                return False
            
            # Проверяем, не истекла ли блокировка
            try:
                expires = datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S')
                expires = expires.replace(tzinfo=msk_tz)
                return now < expires
            except:
                return False
    
    def unlock_channel(self, channel: str):
        """Снимает блокировку с канала"""
        normalized_channel = channel.lstrip('@')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM channel_locks WHERE channel = ?", (normalized_channel,))
    
    def cleanup_expired_locks(self) -> int:
        """Удаляет истекшие блокировки"""
        msk_tz = timezone(timedelta(hours=3))
        now = datetime.now(msk_tz).strftime('%Y-%m-%d %H:%M:%S')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Проверяем существование таблицы
            cursor.execute('''
                SELECT name FROM sqlite_master WHERE type='table' AND name='channel_locks'
            ''')
            if not cursor.fetchone():
                return 0
            
            cursor.execute("DELETE FROM channel_locks WHERE expires_at < ?", (now,))
            return cursor.rowcount

    # --- Processed Invites (отслеживание обработанных приватных ссылок) ---
    def _ensure_processed_invites_table(self, cursor):
        """Создаёт таблицу processed_invites если её нет"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                invite_hash TEXT NOT NULL,
                channel_id INTEGER,
                channel_username TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, invite_hash)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_processed_invites_account 
            ON processed_invites(account_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_processed_invites_hash 
            ON processed_invites(invite_hash)
        ''')
    
    def is_invite_processed(self, account_id: int, invite_hash: str) -> bool:
        """
        Проверяет, был ли инвайт уже обработан этим аккаунтом.
        
        Args:
            account_id: ID аккаунта
            invite_hash: Хэш приватной ссылки (без +)
            
        Returns:
            True если инвайт уже обработан
        """
        normalized_hash = invite_hash.lstrip('+')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_processed_invites_table(cursor)
            
            cursor.execute('''
                SELECT 1 FROM processed_invites 
                WHERE account_id = ? AND invite_hash = ?
            ''', (account_id, normalized_hash))
            return cursor.fetchone() is not None
    
    def mark_invite_processed(self, account_id: int, invite_hash: str, 
                               channel_id: int = None, channel_username: str = None):
        """
        Помечает инвайт как обработанный для аккаунта.
        
        Args:
            account_id: ID аккаунта
            invite_hash: Хэш приватной ссылки (без +)
            channel_id: ID канала (опционально)
            channel_username: Username канала (опционально)
        """
        normalized_hash = invite_hash.lstrip('+')
        msk_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(msk_tz)
        ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_processed_invites_table(cursor)
            
            cursor.execute('''
                INSERT OR IGNORE INTO processed_invites 
                (account_id, invite_hash, channel_id, channel_username, processed_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (account_id, normalized_hash, channel_id, channel_username, ts_str))
    
    def get_processed_invites_count(self, account_id: int) -> int:
        """Возвращает количество обработанных инвайтов для аккаунта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_processed_invites_table(cursor)
            
            cursor.execute('''
                SELECT COUNT(*) FROM processed_invites WHERE account_id = ?
            ''', (account_id,))
            return cursor.fetchone()[0]

    # --- Pending Join Requests (каналы с модерацией) ---
    
    def _ensure_pending_joins_table(self, cursor):
        """Создаёт таблицу pending_joins если её нет"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_joins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                invite_hash TEXT NOT NULL,
                channel_title TEXT,
                status TEXT DEFAULT 'pending',
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                rejected_at TIMESTAMP,
                last_checked TIMESTAMP,
                check_count INTEGER DEFAULT 0,
                UNIQUE(account_id, invite_hash)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_pending_joins_account 
            ON pending_joins(account_id, status)
        ''')
    
    def add_pending_join(self, account_id: int, invite_hash: str, channel_title: str = None):
        """
        Добавляет запрос на вступление в очередь ожидания.
        
        Args:
            account_id: ID аккаунта
            invite_hash: Хэш приватной ссылки (без +)
            channel_title: Название канала (если известно)
        """
        normalized_hash = invite_hash.lstrip('+')
        msk_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(msk_tz)
        ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_pending_joins_table(cursor)
            
            cursor.execute('''
                INSERT OR REPLACE INTO pending_joins 
                (account_id, invite_hash, channel_title, status, requested_at)
                VALUES (?, ?, ?, 'pending', ?)
            ''', (account_id, normalized_hash, channel_title, ts_str))
    
    def get_pending_joins(self, account_id: int = None, status: str = 'pending') -> List[Dict]:
        """
        Получает список pending join requests.
        
        Args:
            account_id: ID аккаунта (None = все аккаунты)
            status: Статус (pending, approved, rejected, expired)
            
        Returns:
            Список pending requests
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_pending_joins_table(cursor)
            
            if account_id:
                cursor.execute('''
                    SELECT * FROM pending_joins 
                    WHERE account_id = ? AND status = ?
                    ORDER BY requested_at DESC
                ''', (account_id, status))
            else:
                cursor.execute('''
                    SELECT * FROM pending_joins 
                    WHERE status = ?
                    ORDER BY requested_at DESC
                ''', (status,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def update_pending_join_status(self, account_id: int, invite_hash: str, 
                                    status: str, channel_id: int = None):
        """
        Обновляет статус pending join request.
        
        Args:
            account_id: ID аккаунта
            invite_hash: Хэш приватной ссылки
            status: Новый статус (approved, rejected, expired)
            channel_id: ID канала (если одобрен)
        """
        normalized_hash = invite_hash.lstrip('+')
        msk_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(msk_tz)
        ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
        
        should_mark_processed = False
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_pending_joins_table(cursor)
            
            if status == 'approved':
                cursor.execute('''
                    UPDATE pending_joins 
                    SET status = ?, approved_at = ?, last_checked = ?
                    WHERE account_id = ? AND invite_hash = ?
                ''', (status, ts_str, ts_str, account_id, normalized_hash))
                should_mark_processed = True
                
            elif status == 'rejected':
                cursor.execute('''
                    UPDATE pending_joins 
                    SET status = ?, rejected_at = ?, last_checked = ?
                    WHERE account_id = ? AND invite_hash = ?
                ''', (status, ts_str, ts_str, account_id, normalized_hash))
            else:
                cursor.execute('''
                    UPDATE pending_joins 
                    SET status = ?, last_checked = ?
                    WHERE account_id = ? AND invite_hash = ?
                ''', (status, ts_str, account_id, normalized_hash))
        
        # Вызываем ПОСЛЕ закрытия соединения чтобы избежать deadlock
        if should_mark_processed:
            self.mark_invite_processed(account_id, invite_hash, channel_id=channel_id)
    
    def increment_pending_check_count(self, account_id: int, invite_hash: str):
        """Увеличивает счётчик проверок для pending request"""
        normalized_hash = invite_hash.lstrip('+')
        msk_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(msk_tz)
        ts_str = now_msk.strftime('%Y-%m-%d %H:%M:%S')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_pending_joins_table(cursor)
            
            cursor.execute('''
                UPDATE pending_joins 
                SET check_count = check_count + 1, last_checked = ?
                WHERE account_id = ? AND invite_hash = ?
            ''', (ts_str, account_id, normalized_hash))
    
    def cleanup_old_pending_joins(self, max_age_days: int = 7) -> int:
        """
        Удаляет старые pending requests (истекшие).
        
        Args:
            max_age_days: Максимальный возраст в днях
            
        Returns:
            Количество удалённых записей
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_pending_joins_table(cursor)
            
            # Помечаем старые как expired
            cursor.execute('''
                UPDATE pending_joins 
                SET status = 'expired'
                WHERE status = 'pending' 
                AND datetime(requested_at) < datetime('now', ? || ' days')
            ''', (f'-{max_age_days}',))
            
            return cursor.rowcount

    # --- Own Channels ---
    def get_own_channels(self, account_id: int) -> List[Dict]:
        """Возвращает список собственных каналов аккаунта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM own_channels WHERE account_id = ?
                ORDER BY created_at DESC
            ''', (account_id,))
            return [dict(row) for row in cursor.fetchall()]

    def add_own_channel(self, account_id: int, channel_id: int, username: str, title: str,
                        description: str = "", invite_link: str = "") -> int:
        """Добавляет собственный канал аккаунта. Возвращает ID записи."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO own_channels (account_id, channel_id, username, title, description, invite_link)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (account_id, channel_id, username, title, description, invite_link))
            return cursor.lastrowid

    # --- Parsed Users (Inviter) ---
    def add_parsed_user(self, account_id: int, user_id: int, username: str = None,
                        first_name: str = None, last_name: str = None,
                        source_chat_id: int = None, source_chat_title: str = None) -> bool:
        """
        Добавляет спарсенного пользователя.
        Возвращает True если пользователь новый для аккаунта.
        Всегда регистрирует source_chat_id в parsed_user_sources (junction),
        даже если user уже был спарсен из другого чата.
        """
        is_new = False
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO parsed_users (account_id, user_id, username, first_name, last_name, source_chat_id, source_chat_title) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account_id, user_id, username, first_name, last_name, source_chat_id, source_chat_title),
                )
                is_new = True
            except sqlite3.IntegrityError:
                is_new = False
                cursor.execute(
                    "UPDATE parsed_users SET username = COALESCE(?, username), "
                    "first_name = COALESCE(?, first_name), last_name = COALESCE(?, last_name), "
                    "source_chat_id = COALESCE(?, source_chat_id), "
                    "source_chat_title = COALESCE(?, source_chat_title) "
                    "WHERE account_id = ? AND user_id = ?",
                    (username, first_name, last_name, source_chat_id, source_chat_title, account_id, user_id),
                )

            if source_chat_id is not None:
                try:
                    cursor.execute(
                        "INSERT OR IGNORE INTO parsed_user_sources "
                        "(account_id, user_id, source_chat_id, source_chat_title, parsed_at) "
                        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                        (account_id, user_id, int(source_chat_id), source_chat_title),
                    )
                except sqlite3.OperationalError:
                    pass
            return is_new

    def get_parsed_users(self, account_id: int, limit: int = 100, offset: int = 0,
                         source_chat_id: int = None) -> List[Dict]:
        """
        Возвращает список спарсенных пользователей для аккаунта.
        Если передан source_chat_id — строго фильтрует через parsed_user_sources.
        Без source_chat_id поведение совместимо с прежним API.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if source_chat_id is not None:
                cursor.execute(
                    "SELECT pu.* FROM parsed_users pu "
                    "INNER JOIN parsed_user_sources pus "
                    "ON pus.account_id = pu.account_id AND pus.user_id = pu.user_id "
                    "WHERE pu.account_id = ? AND pus.source_chat_id = ? "
                    "ORDER BY pu.parsed_at DESC LIMIT ? OFFSET ?",
                    (account_id, int(source_chat_id), limit, offset),
                )
            else:
                cursor.execute(
                    "SELECT * FROM parsed_users WHERE account_id = ? "
                    "ORDER BY parsed_at DESC LIMIT ? OFFSET ?",
                    (account_id, limit, offset),
                )
            return [dict(row) for row in cursor.fetchall()]

    # --- Invite Stats ---
    def add_invite_result(self, account_id: int, channel_id: int, user_id: int,
                          status: str, error_message: str = None):
        """Добавляет результат инвайта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO invite_stats (account_id, channel_id, user_id, status, error_message) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, channel_id, user_id, status, error_message),
            )

    def get_invited_user_ids(self, account_id: int, channel_id: int = None,
                             only_success: bool = False) -> set:
        """
        ID пользователей, по которым уже были попытки инвайта.
        only_success=True — только успешные; иначе любые статусы (skip prior attempts).
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            sql = "SELECT user_id FROM invite_stats WHERE account_id = ?"
            params = [account_id]
            if channel_id is not None:
                sql += " AND channel_id = ?"
                params.append(int(channel_id))
            if only_success:
                sql += " AND status = 'success'"
            cursor.execute(sql, params)
            return {row[0] for row in cursor.fetchall()}

    def get_invite_stats(self, account_id: int) -> Dict:
        """Возвращает статистику инвайтов. today_count — только successful за сегодня."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM invite_stats WHERE account_id = ?", (account_id,))
            total = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM invite_stats WHERE account_id = ? AND status = 'success'",
                (account_id,),
            )
            success = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM invite_stats WHERE account_id = ? AND status = 'error'",
                (account_id,),
            )
            errors = cursor.fetchone()[0]

            msk_tz = timezone(timedelta(hours=3))
            today = datetime.now(msk_tz).strftime('%Y-%m-%d')
            # invited_at хранится в UTC (CURRENT_TIMESTAMP), поэтому сдвигаем
            # на +3 часа к MSK перед сравнением дат — иначе с 21:00 до 00:00 UTC
            # сегодняшние инвайты ошибочно считаются вчерашними.
            cursor.execute(
                "SELECT COUNT(*) FROM invite_stats "
                "WHERE account_id = ? AND status = 'success' AND DATE(invited_at, '+3 hours') = ?",
                (account_id, today),
            )
            today_count = cursor.fetchone()[0]

            return {
                "total": total,
                "success": success,
                "errors": errors,
                "today_count": today_count,
            }

    # --- Post Queue ---
    def add_post_to_queue(self, account_id: int, channel_id: int, content_text: str,
                          media_path: str = None, media_type: str = None,
                          format_type: str = "md", scheduled_at: str = None) -> int:
        """Добавляет пост в очередь. Возвращает ID записи."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO post_queue (account_id, channel_id, content_text, media_path, media_type, format_type, scheduled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (account_id, channel_id, content_text, media_path, media_type, format_type, scheduled_at))
            return cursor.lastrowid

    def get_pending_posts(self, account_id: int) -> List[Dict]:
        """Возвращает посты со статусом pending, у которых scheduled_at <= now (MSK) или NULL"""
        from datetime import datetime, timezone, timedelta
        msk_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(msk_tz).strftime('%Y-%m-%d %H:%M:%S')

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM post_queue
                WHERE account_id = ? AND status = 'pending'
                AND (scheduled_at IS NULL OR scheduled_at <= ?)
                ORDER BY scheduled_at ASC, created_at ASC
            ''', (account_id, now_msk))
            return [dict(row) for row in cursor.fetchall()]

    def mark_post_sent(self, post_id: int):
        """Помечает пост как отправленный"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE post_queue SET status = 'sent', posted_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (post_id,))

    def mark_post_failed(self, post_id: int):
        """Помечает пост как неудавшийся"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE post_queue SET status = 'failed'
                WHERE id = ?
            ''', (post_id,))

    # --- Mass Send Campaigns ---
    def add_mass_send_campaign(self, account_id: int, name: str, message_template: str,
                               media_path: str = None, media_type: str = None,
                               target_type: str = "dm", total_targets: int = 0) -> int:
        """Создает кампанию массовой рассылки. Возвращает ID кампании."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO mass_send_campaigns
                    (account_id, name, message_template, media_path, media_type, target_type,
                     status, total_targets, processed_count)
                VALUES (?, ?, ?, ?, ?, ?, 'running', ?, 0)
            """, (account_id, name, message_template, media_path, media_type, target_type,
                  int(total_targets or 0)))
            return cursor.lastrowid

    def update_mass_send_campaign(self, campaign_id: int, *,
                                  status: str = None,
                                  processed_count: int = None,
                                  total_targets: int = None,
                                  error_message: str = None,
                                  finished: bool = False) -> None:
        """Обновляет lifecycle-поля кампании (status/progress/error/finished_at)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            sets = []
            params = []
            if status is not None:
                sets.append("status = ?")
                params.append(status)
            if processed_count is not None:
                sets.append("processed_count = ?")
                params.append(int(processed_count))
            if total_targets is not None:
                sets.append("total_targets = ?")
                params.append(int(total_targets))
            if error_message is not None:
                sets.append("error_message = ?")
                params.append(error_message)
            if finished or (status and status in (
                'completed', 'failed', 'stopped', 'peer_flood', 'error_threshold'
            )):
                sets.append("finished_at = CURRENT_TIMESTAMP")
            if not sets:
                return
            params.append(campaign_id)
            cursor.execute(
                f"UPDATE mass_send_campaigns SET {', '.join(sets)} WHERE id = ?",
                params,
            )

    def get_mass_send_campaigns(self, account_id: int):
        """Возвращает список кампаний массовой рассылки для аккаунта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM mass_send_campaigns WHERE account_id = ?
                ORDER BY created_at DESC
            """, (account_id,))
            return [dict(row) for row in cursor.fetchall()]

    def add_mass_send_result(self, campaign_id: int, account_id: int, target_id: str,
                             target_type: str, status: str, error_message: str = None):
        """Добавляет результат отправки в кампании"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO mass_send_results (campaign_id, account_id, target_id, target_type, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (campaign_id, account_id, target_id, target_type, status, error_message))

    def get_campaign_stats(self, campaign_id: int):
        """Возвращает статистику + lifecycle кампании."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM mass_send_results WHERE campaign_id = ?", (campaign_id,))
            total = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM mass_send_results WHERE campaign_id = ? AND status = 'sent'",
                (campaign_id,),
            )
            sent = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM mass_send_results WHERE campaign_id = ? AND status = 'blocked'",
                (campaign_id,),
            )
            blocked = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM mass_send_results WHERE campaign_id = ? AND status = 'error'",
                (campaign_id,),
            )
            errors = cursor.fetchone()[0]

            cursor.execute("SELECT * FROM mass_send_campaigns WHERE id = ?", (campaign_id,))
            row = cursor.fetchone()
            lifecycle = {}
            if row:
                crow = dict(row)
                lifecycle = {
                    "campaign_id": campaign_id,
                    "status": crow.get("status"),
                    "total_targets": crow.get("total_targets") or 0,
                    "processed_count": crow.get("processed_count") or 0,
                    "error_message": crow.get("error_message"),
                    "finished_at": crow.get("finished_at"),
                    "created_at": crow.get("created_at"),
                    "target_type": crow.get("target_type"),
                    "account_id": crow.get("account_id"),
                }

            return {
                "total": total,
                "sent": sent,
                "blocked": blocked,
                "errors": errors,
                **lifecycle,
            }

    # ======================== НОВЫЕ МЕТОДЫ ДЛЯ 8 ФИЧ ========================

    def update_account_profile(self, account_id: int, first_name: str = None, username: str = None):
        """Обновляет профиль аккаунта (first_name, username)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE accounts SET first_name = ?, username = ? WHERE id = ?",
                (first_name, username, account_id)
            )
    
    def set_owned_channel(self, account_id: int, channel: str):
        """Установить личный канал для аккаунта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE accounts SET owned_channel = ? WHERE id = ?",
                (channel if channel else None, account_id)
            )

    def get_account_display_name(self, account_id: int) -> str:
        """Возвращает display name (Имя @username) или телефон"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT first_name, username, phone FROM accounts WHERE id = ?", (account_id,))
                row = cursor.fetchone()
                if row:
                    first_name, username, phone = row[0], row[1], row[2]
                    if first_name or username:
                        name = first_name or "User"
                        handle = f" (@{username})" if username else ""
                        return f"{name}{handle}"
                    return phone or "Unknown"
            except:
                pass
        return "Unknown"

    def record_channel_join(self, account_id: int, channel: str, title: str = None, status: str = 'joined'):
        """Записывает вступление в канал"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Создаём таблицу если её нет
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_channel_joins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    title TEXT,
                    status TEXT DEFAULT 'joined',
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_id, channel)
                );
            """)
            cursor.execute(
                """
                INSERT OR REPLACE INTO account_channel_joins 
                (account_id, channel, title, status, joined_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (account_id, channel, title, status)
            )

    def get_account_joined_channels(self, account_id: int, limit: int = 500) -> list:
        """Получает каналы, в которых вступил аккаунт"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT id, channel, title, status, joined_at 
                    FROM account_channel_joins 
                    WHERE account_id = ?
                    ORDER BY joined_at DESC
                    LIMIT ?
                    """,
                    (account_id, limit)
                )
                return [dict(row) for row in cursor.fetchall()]
            except:
                return []

    def get_account_joined_count(self, account_id: int) -> int:
        """Количество каналов, в которых вступил аккаунт"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT COUNT(*) FROM account_channel_joins WHERE account_id = ? AND status = 'joined'",
                    (account_id,)
                )
                return cursor.fetchone()[0]
            except:
                return 0

    def mark_channel_expired(self, channel_link: str):
        """Отмечает канал как истёкший"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "UPDATE found_channels SET status = 'expired' WHERE link = ?",
                    (channel_link,)
                )
            except:
                pass

    def get_found_chats(self, keyword: str = None, limit: int = 500) -> list:
        """Получает найденные чаты/группы"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS found_chats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat INTEGER UNIQUE NOT NULL,
                        title TEXT,
                        members_count INTEGER DEFAULT 0,
                        is_megagroup BOOLEAN DEFAULT 0,
                        keyword TEXT,
                        found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                if keyword:
                    cursor.execute(
                        "SELECT chat, title, members_count, keyword, found_at FROM found_chats WHERE keyword = ? ORDER BY found_at DESC LIMIT ?",
                        (keyword, limit)
                    )
                else:
                    cursor.execute(
                        "SELECT chat, title, members_count, keyword, found_at FROM found_chats ORDER BY found_at DESC LIMIT ?",
                        (limit,)
                    )
                return [dict(row) for row in cursor.fetchall()]
            except:
                return []

    def add_found_chat(self, chat_id: int, title: str, members_count: int = 0, keyword: str = None):
        """Добавляет найденный чат"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO found_chats 
                    (chat, title, members_count, keyword, found_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (chat_id, title, members_count, keyword)
                )
            except:
                pass

    def clear_found_chats(self):
        """Очищает таблицу найденных чатов"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("DELETE FROM found_chats")
            except:
                pass

    # ======================== SMART RATE LIMIT MANAGEMENT ========================

    def record_rate_limit_event(self, account_id: int, action: str = None, duration_seconds: int = None):
        """Записывает событие rate limit в историю"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                # Создаём таблицу если её нет
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS rate_limit_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id INTEGER NOT NULL,
                        action TEXT,
                        duration_seconds INTEGER,
                        triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        FOREIGN KEY(account_id) REFERENCES accounts(id)
                    );
                """)
                cursor.execute(
                    """
                    INSERT INTO rate_limit_history 
                    (account_id, action, duration_seconds, triggered_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (account_id, action, duration_seconds)
                )
            except:
                pass

    def get_rate_limit_history(self, account_id: int, limit: int = 50) -> list:
        """Получает историю rate limit'ов для аккаунта"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT id, action, duration_seconds, triggered_at, resolved_at 
                    FROM rate_limit_history 
                    WHERE account_id = ?
                    ORDER BY triggered_at DESC
                    LIMIT ?
                    """,
                    (account_id, limit)
                )
                return [dict(row) for row in cursor.fetchall()]
            except:
                return []

    def get_rate_limit_stats(self, account_id: int) -> dict:
        """Получает статистику rate limit'ов"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                # Всего лимитов
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM rate_limit_history WHERE account_id = ?",
                    (account_id,)
                )
                total = cursor.fetchone()['cnt'] if cursor.fetchone() else 0
                
                # Последний лимит
                cursor.execute(
                    """
                    SELECT action, duration_seconds, triggered_at 
                    FROM rate_limit_history 
                    WHERE account_id = ?
                    ORDER BY triggered_at DESC
                    LIMIT 1
                    """,
                    (account_id,)
                )
                last = dict(cursor.fetchone()) if cursor.fetchone() else None
                
                # По действиям
                cursor.execute(
                    """
                    SELECT action, COUNT(*) as cnt 
                    FROM rate_limit_history 
                    WHERE account_id = ?
                    GROUP BY action
                    """,
                    (account_id,)
                )
                by_action = {row['action']: row['cnt'] for row in cursor.fetchall()}
                
                return {
                    "total_limits": total,
                    "last_limit": last,
                    "by_action": by_action
                }
            except:
                return {"total_limits": 0, "last_limit": None, "by_action": {}}
