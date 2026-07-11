from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetDiscussionMessageRequest, ImportChatInviteRequest, GetBotCallbackAnswerRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import KeyboardButtonCallback
from telethon.errors import (
    MessageIdInvalidError, FloodWaitError, UserAlreadyParticipantError,
    InviteHashExpiredError, ChannelPrivateError, UserDeactivatedBanError,
    AuthKeyUnregisteredError, ChatWriteForbiddenError, SlowModeWaitError,
    PeerFloodError, InviteRequestSentError,
)
import socks
import time
import threading
import random
import asyncio
import io
import base64
import tempfile
import os
import inspect
import signal
from datetime import datetime, timezone
from typing import Optional, Dict, List
from config import Config
from utils.database import Database
from utils.health_monitor import HealthMonitor
from modules.comment_generator import CommentGenerator
from modules.autoresponder import AutoResponder
from modules.keyword_search import KeywordSearch
from modules.reactions import ReactionManager
from modules.channel_joiner import ChannelJoiner
from modules.junk_chat_classifier import JunkChatClassifier
from modules.spambot_checker import SpamBotChecker
from modules.channel_creator import ChannelCreator
from modules.channel_poster import ChannelPoster
from modules.inviter import Inviter
from modules.mass_sender import MassSender
from modules.channel_filter import ChannelFilter
from utils.logger import Logger, StructuredLogger
from utils.async_http import close_http_client

# Глобальный флаг паузы (устанавливается из main.py)
_global_pause = False

def set_global_pause(paused: bool):
    """Устанавливает глобальную паузу для всех воркеров"""
    global _global_pause
    _global_pause = paused

def is_global_paused() -> bool:
    """Проверяет активна ли глобальная пауза"""
    return _global_pause

# Глобальный флаг для graceful shutdown
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Обработчик сигналов SIGTERM и SIGINT"""
    global _shutdown_requested
    _shutdown_requested = True
    print(f"\n🛑 Получен сигнал {signum}, инициирую graceful shutdown...")


# Регистрируем обработчики сигналов
try:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
except (ValueError, OSError):
    # В некоторых средах (например, в потоках) сигналы недоступны
    pass


class BotWorker:
    def __init__(self, account_data: Dict, db: Database):
        self.account_id = account_data['id']
        self.phone = account_data['phone']
        self.session_name = account_data['session_name']
        self.api_id = account_data['api_id']
        self.api_hash = account_data['api_hash']
        self.proxy_data = account_data.get('proxy')
        
        self.db = db
        self.logger = StructuredLogger(db, module_name="worker")
        self.health_monitor = HealthMonitor(db)
        self.client = None
        self.is_running = False
        
        # Модули
        self.comment_generator = CommentGenerator(Config.GEMINI_API_KEY, db=db)
        self.autoresponder = None
        self.keyword_search = None
        self.reaction_manager = None
        self.channel_explorer = None  # Для отслеживания и остановки
        
        # Каналы теперь берутся из БД, не из файла
        self.active_channels = []
        
        # Трекинг последних сообщений в чаты (chat_id -> timestamp)
        self._last_chat_message: Dict[int, float] = {}
        self._chat_cooldown_seconds = 1800  # 30 минут между сообщениями в один чат
        
        # Список pending tasks для отмены при остановке
        self._pending_tasks: List[asyncio.Future] = []
        
        # Кэш вступленных групп обсуждений (чтобы не вступать повторно каждый цикл)
        self._joined_discussion_groups: set = set()
        
        # Группы обсуждений, куда отправлена заявка и мы ждём одобрения админа.
        # Пока linked_chat_id здесь — канал не банится, а периодически перепроверяется.
        self._pending_discussion_groups: set = set()
        
        # Кэш вступленных каналов в этой сессии (channel -> True)
        self._joined_channels: set = set()
        
        # Кэш настроек (60 секунд TTL)
        self._settings_cache: Dict[str, tuple] = {}  # key -> (value, timestamp)
        self._settings_cache_ttl = 60

        # Кэш entity каналов для "комментить от имени группы" (send_as)
        # username -> entity | None (None = не удалось резолвить)
        self._send_as_cache: Dict[str, object] = {}
        
        # === Новые компоненты для трёх тоглов ===
        # Классификатор мусорных чатов (создаётся лениво в start()).
        self.junk_classifier: Optional[JunkChatClassifier] = None
        # Проверка @SpamBot (создаётся лениво).
        self.spambot_checker: Optional[SpamBotChecker] = None
        # Защита от слишком частых обращений к @SpamBot
        self._last_spambot_check: float = 0.0
        self._spambot_cooldown_seconds: int = 600  # 10 мин
        # Трекинг счётчика PeerFloodError (для эскалации)
        self._peer_flood_count: int = 0
        # Кэш чатов, которые уже признали мусором — не проверяем повторно
        self._junk_chat_decisions: Dict[int, bool] = {}
        # Время последнего прохода _maybe_leave_junk_chats
        self._last_junk_scan: float = 0.0
        # Сколько чатов покинуть за один проход (анти-флуд)
        self._junk_leave_per_scan = 3
        
        # Новые модули
        self.channel_creator = None
        self.channel_poster = None
        self.inviter = None
        self.mass_sender = None
        self.channel_filter = None
        
    def _get_proxy(self):
        if not self.proxy_data or not self.proxy_data.get('ip'):
            return None
        proxy_type = socks.SOCKS5 if self.proxy_data.get('proxy_type') == 'socks5' else socks.HTTP
        return (proxy_type, self.proxy_data['ip'], self.proxy_data['port'], True, 
                self.proxy_data.get('proxy_user'), self.proxy_data.get('proxy_pass'))

    async def _join_discussion_group_if_needed(self, linked_chat_id: int, channel_name: str):
        """
        Вступает в группу обсуждений если ещё не вступали.

        Возвращает:
          - True      — вступили (или уже состоим);
          - "pending" — группа с модерацией, заявка отправлена, ждём одобрения
                        админа (канал НЕ банить, перепроверить в след. цикле);
          - False     — реальная ошибка (нет прав / приватная / не найдена).
        """
        if linked_chat_id in self._joined_discussion_groups:
            return True  # Уже вступали в этой сессии
        
        # Ждём одобрения — не спамим повторными заявками, но проверяем, не приняли ли уже.
        if linked_chat_id in self._pending_discussion_groups:
            try:
                linked_chat = await self.client.get_entity(linked_chat_id)
                # get_participant кинет исключение, если мы ещё не участники
                from telethon.tl.functions.channels import GetParticipantRequest
                await self.client(GetParticipantRequest(linked_chat, 'me'))
                # Дошли сюда — заявку одобрили
                self._pending_discussion_groups.discard(linked_chat_id)
                self._joined_discussion_groups.add(linked_chat_id)
                self.log(f"✅ Заявка в обсуждения {channel_name} одобрена, теперь я участник")
                return True
            except Exception:
                self.log(f"⏳ Заявка в обсуждения {channel_name} ещё не одобрена, жду", "info")
                return "pending"
        
        try:
            linked_chat = await self.client.get_entity(linked_chat_id)
            await self.client(JoinChannelRequest(linked_chat))
            self._joined_discussion_groups.add(linked_chat_id)
            self.log(f"✅ Вступил в группу обсуждений для {channel_name}")
            return True
        except InviteRequestSentError:
            # Группа с модерацией — заявка отправлена, ждём одобрения
            self._pending_discussion_groups.add(linked_chat_id)
            self.log(f"⏳ Заявка на вступление в обсуждения {channel_name} отправлена, жду одобрения админа", "warning")
            return "pending"
        except Exception as e:
            err_str = str(e).lower()
            # Если уже участник - это ок, добавляем в кэш
            if "already" in err_str or "participant" in err_str:
                self._joined_discussion_groups.add(linked_chat_id)
                self._pending_discussion_groups.discard(linked_chat_id)
                return True
            # Разные формулировки "заявка отправлена, ждите одобрения"
            if (("request" in err_str and "sent" in err_str)
                    or "successfully requested" in err_str
                    or "join request" in err_str
                    or "request to join" in err_str):
                self._pending_discussion_groups.add(linked_chat_id)
                self.log(f"⏳ Заявка на вступление в обсуждения {channel_name} отправлена, жду одобрения", "warning")
                return "pending"
            return False

    def _get_cached_setting(self, key: str, default=None):
        """
        Получает настройку из кэша (TTL 60 сек) или из БД.
        Снижает нагрузку на БД на 90%.
        """
        current_time = time.time()
        
        # Проверяем кэш
        if key in self._settings_cache:
            value, timestamp = self._settings_cache[key]
            if current_time - timestamp < self._settings_cache_ttl:
                return value
        
        # Кэш устарел или отсутствует - запрашиваем из БД
        value = self.db.get_setting(key, default)
        self._settings_cache[key] = (value, current_time)
        return value

    async def _resolve_send_as(self):
        """
        Возвращает entity канала, от имени которого нужно писать комменты
        ("от имени группы"), либо None если фича выключена / аккаунт пишет от себя.

        Требует, чтобы аккаунт был администратором указанного канала.
        Результат кэшируется, чтобы не дёргать get_entity каждый раз.
        """
        if not self._get_cached_setting("comment_as_channel", False):
            return None

        username = (self._get_cached_setting("comment_as_channel_username", "") or "").strip()
        if not username:
            return None

        username = Config.normalize_channel(username)
        if username in self._send_as_cache:
            return self._send_as_cache[username]

        try:
            entity = await self.client.get_entity(username)
        except Exception as e:
            self.log(f"⚠️ Не удалось получить канал для send-as @{username}: {e}. Пишу от аккаунта.", "warning")
            entity = None

        self._send_as_cache[username] = entity
        return entity

    async def _send_comment_message(self, channel, message, post_id):
        """
        Отправляет комментарий к посту.

        Если включён тоггл "комментить от имени группы" (send_as) — сначала
        пробует запостить от имени канала. При любой ошибке отправки от имени
        канала — откатывается на отправку от личного аккаунта.
        """
        send_as = await self._resolve_send_as()
        if send_as is not None:
            try:
                return await self.client.send_message(
                    entity=channel, message=message, comment_to=post_id, send_as=send_as
                )
            except Exception as e:
                self.log(
                    f"⚠️ Не удалось запостить от имени канала ({e}). Пишу от личного аккаунта.",
                    "warning",
                )
        return await self.client.send_message(entity=channel, message=message, comment_to=post_id)

    async def _verify_comment_published(self, result, name: str) -> bool:
        """
        Проверяет, что отправленный комментарий действительно виден в чате.

        send_message может вернуть объект сообщения без ошибки, но при этом
        комментарий будет невидим для других (теневой бан) или уйти в
        премодерацию группы обсуждений. В таких случаях в логах раньше было
        «✅ Комментарий отправлен», а по факту нового коммента не появлялось.

        Логика: через небольшую паузу перечитываем сообщение по его id. Если
        Telegram его больше не отдаёт — коммент, скорее всего, снят/на модерации.

        Returns:
            True  — сообщение подтверждено (или проверку не удалось выполнить);
            False — сообщение не найдено (вероятный теневой бан/премодерация).
        """
        if result is None:
            return False

        # Даём Telegram время применить модерацию/скрытие
        delay = self._get_cached_setting("verify_comment_delay", 5)
        try:
            await asyncio.sleep(max(2, int(delay)))
        except Exception:
            await asyncio.sleep(5)

        try:
            chat_ref = getattr(result, "chat_id", None) or getattr(result, "peer_id", None)
            msg_id = getattr(result, "id", None)
            if chat_ref is None or msg_id is None:
                return True  # не с чем сверяться — не поднимаем ложную тревогу

            fetched = await self.client.get_messages(chat_ref, ids=msg_id)

            if fetched is None or (isinstance(fetched, list) and not any(fetched)):
                self.log(
                    f"👻 Комментарий в {name} отправлен, но не виден при перепроверке — "
                    f"вероятен теневой бан или премодерация комментариев. Помечаю канал.",
                    "warning",
                )
                # Помечаем как забанен в этом канале, чтобы не тратить попытки впустую
                try:
                    self.db.mark_banned(self.account_id, name)
                except Exception:
                    pass
                return False

            return True
        except Exception as e:
            # Ошибка проверки не должна ломать основной поток
            self.log(f"⚠️ Не удалось проверить публикацию коммента в {name}: {e}", "warning")
            return True

    async def _simulate_typing(self, entity, seconds: float = 2.0):
        """
        Имитирует набор текста перед отправкой/редактированием.
        Добавляет реализм и снижает риск бана.
        
        Args:
            entity: Канал/чат куда отправляется сообщение
            seconds: Базовое время набора (будет варьироваться ±50%)
        """
        try:
            # Отправляем действие "печатает"
            await self.client.send_read_acknowledge(entity)
            await self.client.action(entity, 'typing')
            # Ждем случайное время (имитация набора)
            await asyncio.sleep(random.uniform(seconds * 0.5, seconds * 1.5))
        except Exception:
            pass  # Игнорируем ошибки действий (не критично)

    def _cleanup_memory_caches(self):
        """
        Очистка кэшей во избежание утечек памяти.
        Храним только последние 500 записей в каждом кэше.
        """
        # Очистка кэша вступленных каналов
        if len(self._joined_channels) > 500:
            # Преобразуем в список, берем последние 500, обратно в set
            self._joined_channels = set(list(self._joined_channels)[-500:])
            self.log(f"🧹 Очистка кэша каналов: оставлено 500 из {len(self._joined_channels) + 500}")
        
        # Очистка кэша групп обсуждений
        if len(self._joined_discussion_groups) > 500:
            self._joined_discussion_groups = set(list(self._joined_discussion_groups)[-500:])
            self.log(f"🧹 Очистка кэша групп обсуждений: оставлено 500 из {len(self._joined_discussion_groups) + 500}")


    def log(self, message: str, level: str = "info"):
        """Логирует сообщение с привязкой к аккаунту (с fallback на консоль).

        Любой уровень гарантированно сохраняется в БД. Уровень 'info' с зелёной
        галочкой в начале авто-повышается до 'success' для наглядной подсветки.
        """
        try:
            lvl = (level or "info").lower()
            # Авто-детект успешных действий по эмодзи-галочке в начале сообщения
            if lvl == "info" and message.lstrip().startswith(("✅", "☑️", "🎉")):
                lvl = "success"
            if lvl == "error":
                self.logger.error(message, account_id=self.account_id, exc_info=True)
            else:
                self.logger.log(lvl, message, account_id=self.account_id)
        except Exception as e:
            # Fallback на консоль если БД недоступна
            print(f"[{str(level).upper()}][acc:{self.account_id}] {message} (log error: {e})")

    def _update_account_status(self, status: str):
        """Обновляет статус аккаунта в базе данных"""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, self.account_id))
        except Exception as e:
            print(f"[ERROR] Failed to update account status: {e}")

    def start(self):
        if self.is_running: return
        try:
            self.log("🚀 Запуск воркера...")
        except Exception as e:
            print(f"Ошибка логирования в БД: {e}")
        
        # Запускаем всё в отдельном потоке с собственным event loop
        self.thread = threading.Thread(target=self._init_and_run, daemon=True)
        self.thread.start()

    def _init_and_run(self):
        """Инициализация и запуск в отдельном потоке"""
        try:
            # Создаём новый event loop для этого потока и сохраняем его
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            
            proxy = self._get_proxy()
            
            # Логируем информацию о прокси
            if proxy:
                proxy_type_name = "SOCKS5" if proxy[0] == socks.SOCKS5 else "HTTP"
                proxy_ip = proxy[1]
                proxy_port = proxy[2]
                self.log(f"🌐 Подключение через прокси: {proxy_type_name} {proxy_ip}:{proxy_port}")
            else:
                self.log("🌐 Подключение без прокси (прямое соединение)")
            
            # Определяем директорию сессий: SESSIONS_DIR env > data/sessions/ > sessions/ (legacy)
            sessions_dir = os.environ.get('SESSIONS_DIR')
            if not sessions_dir:
                # worker.py лежит в <project>/backend/, поэтому корень проекта — это "..",
                # а НЕ "../.." (иначе путь уходит на уровень выше проекта и сессия не находится).
                project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                data_sessions = os.path.join(project_root, "data", "sessions")
                legacy_sessions = os.path.join(project_root, "sessions")
                
                if os.path.exists(data_sessions) or not (os.path.exists(legacy_sessions) and os.listdir(legacy_sessions)):
                    sessions_dir = data_sessions
                else:
                    sessions_dir = legacy_sessions
            
            os.makedirs(sessions_dir, exist_ok=True)
            session_path = os.path.join(sessions_dir, self.session_name)
            self.client = TelegramClient(session_path, self.api_id, self.api_hash, proxy=proxy)
            
            # Подключаемся (проверяем, возвращается ли корутина)
            try:
                res = self.client.connect()
                if inspect.isawaitable(res):
                    self.loop.run_until_complete(res)
                
                if proxy:
                    self.log(f"✅ Прокси работает: {proxy[1]}:{proxy[2]}")
            except Exception as proxy_err:
                if proxy:
                    self.log(f"❌ Ошибка прокси {proxy[1]}:{proxy[2]}: {proxy_err}", "error")
                raise
            
            # Проверяем авторизацию
            res = self.client.is_user_authorized()
            if inspect.isawaitable(res):
                is_auth = self.loop.run_until_complete(res)
            else:
                is_auth = res
                
            if not is_auth:
                raise Exception("Сессия не авторизована. Создайте сессию заново через панель управления.")
            
            # Проверяем не забанен/заморожен ли аккаунт
            try:
                me_res = self.client.get_me()
                if inspect.isawaitable(me_res):
                    me = self.loop.run_until_complete(me_res)
                else:
                    me = me_res
                    
                if me and me.restricted:
                    self.log("🚫 Аккаунт ограничен Telegram!", "error")
                    self._update_account_status("banned")
                    raise Exception("Аккаунт ограничен (restricted) Telegram")
                
                # Проверяем заморозку - пробуем выполнить простое действие
                try:
                    dialogs_res = self.client.get_dialogs(limit=1)
                    if inspect.isawaitable(dialogs_res):
                        dialogs = self.loop.run_until_complete(dialogs_res)
                    else:
                        dialogs = dialogs_res
                except Exception as freeze_err:
                    err_str = str(freeze_err).upper()
                    if 'FROZEN' in err_str or 'AUTH_KEY_UNREGISTERED' in err_str:
                        self.log("🥶 Аккаунт заморожен (FROZEN)!", "error")
                        self._update_account_status("frozen")
                        raise Exception("Аккаунт заморожен (FROZEN). Требуется разморозка через приложение Telegram.")
                    elif 'DEACTIVATED' in err_str:
                        self.log("💀 Аккаунт деактивирован!", "error")
                        self._update_account_status("deactivated")
                        raise Exception("Аккаунт деактивирован")
                    # Другие ошибки пропускаем - возможно временные
                    
            except Exception as ban_check_err:
                err_str = str(ban_check_err).lower()
                if "banned" in err_str or "deactivated" in err_str:
                    self.log("🚫 Аккаунт забанен/деактивирован!", "error")
                    self._update_account_status("banned")
                    raise Exception("Аккаунт забанен или деактивирован")
                elif "frozen" in err_str:
                    self.log("🥶 Аккаунт заморожен!", "error")
                    self._update_account_status("frozen")
                    raise Exception("Аккаунт заморожен")
                raise  # Пробрасываем другие ошибки
            
            self.is_running = True
            self.log("✅ Воркер успешно запущен")
            
            # Инициализация подмодулей
            self.autoresponder = AutoResponder(self.client, Config.GEMINI_API_KEY, self.db, self.account_id)
            
            # Асинхронная инициализация автоответчика
            res = self.autoresponder.init()
            if inspect.isawaitable(res):
                self.loop.run_until_complete(res)
            self.autoresponder.start_listening()
            
            # Запускаем обработчик антиспам-кнопок
            self._setup_antispam_handler()
            
            self.keyword_search = KeywordSearch(self.client, self.db)
            self.reaction_manager = ReactionManager(self.client, self.db, self.account_id)
            self.channel_joiner = ChannelJoiner(self.client, db=self.db)
            
            # Инициализация новых модулей
            self.channel_creator = ChannelCreator(self.client, self.db, self.account_id)
            self.channel_poster = ChannelPoster(self.client, self.db, self.account_id)
            self.inviter = Inviter(self.client, self.db, self.account_id)
            self.mass_sender = MassSender(self.client, self.db, self.account_id)
            self.channel_filter = ChannelFilter(self.client, self.db, self.account_id)
            
            # Вступаем в каналы из channels_to_join.txt при запуске
            self.loop.run_until_complete(self._join_pending_channels())
            
            # Запускаем основной цикл
            self.loop.run_until_complete(self._run_loop())
            
        except Exception as e:
            self.log(f"❌ Ошибка запуска: {e}", "error")
            self.is_running = False
        finally:
            try:
                if hasattr(self, 'loop') and self.loop.is_running():
                    self.loop.close()
            except: pass

    async def _run_loop(self):
        cycle = 0
        
        # Проверяем, не приостановлен ли аккаунт
        if self.health_monitor.should_pause(self.account_id):
            wait_time = self.health_monitor.get_wait_time(self.account_id)
            if wait_time > 0:
                self.log(f"⏸️ Аккаунт на паузе (rate limit). Ожидание {wait_time} сек...")
                await asyncio.sleep(wait_time)
        
        # Вступаем в каналы из channels_to_join.txt (включая приватные)
        await self._join_pending_channels()
        
        # Первичная синхронизация при запуске
        self.log("🚀 Синхронизация ваших подписок и каналов...")
        await self._sync_user_subscriptions()
        
        while self.is_running and not _shutdown_requested:
            try:
                # Проверяем глобальную паузу
                if is_global_paused():
                    self.log("⏸️ Глобальная пауза, ожидаю...")
                    await asyncio.sleep(5)
                    continue
                
                # Проверяем подключение
                if not self.client or not self.client.is_connected():
                    self.log("⚠️ Клиент отключен, пробую переподключиться...", "warning")
                    try:
                        await self.client.connect()
                    except Exception as e:
                        self.log(f"❌ Не удалось переподключиться: {e}", "error")
                        break
                
                cycle += 1
                self.log(f"🔄 Цикл #{cycle}")
                
                # Обновляем список активных каналов (исключаем забаненные для этого аккаунта)
                self._update_active_channels()
                
                # Основная логика комментирования (посты)
                await self._process_channels()
                
                # Логика общения в чатах
                if self._get_cached_setting("chat_interaction_enabled", True):
                    await self._process_chats()
                
                # Постепенное вступление в каналы из базы (1 канал за цикл)
                if cycle % 3 == 0:  # Каждые 3 цикла
                    await self._gradual_join_from_database()
                
                # Автоматический инвайтинг (парсинг + приглашение батча)
                auto_invite_interval = int(self._get_cached_setting("auto_invite_interval_cycles", 5) or 5)
                if (cycle % max(1, auto_invite_interval) == 0
                        and self._get_cached_setting("auto_invite_enabled", False)):
                    self.log("📨 Авто-инвайт: цикл парсинга и приглашения...")
                    await self._process_auto_invite()
                
                # Периодические задачи
                if cycle % 10 == 0:
                    self.log("📡 Повторная проверка подписок...")
                    await self._sync_user_subscriptions()
                    
                    # Вступаем в новые каналы (могли добавиться из рекламы)
                    await self._join_pending_channels()
                
                search_interval = max(1, int(self._get_cached_setting("search_interval_cycles", 20)))
                if cycle % search_interval == 0 and self._get_cached_setting("search_enabled", Config.SEARCH_ENABLED):
                    raw_keywords = self.db.get_setting("search_keywords", "")
                    if isinstance(raw_keywords, str) and raw_keywords.strip():
                        # Поддерживаем ввод по строкам и через запятую, удаляем дубли.
                        parsed_keywords = []
                        seen_keywords = set()
                        for item in raw_keywords.replace(',', '\n').splitlines():
                            keyword = item.strip()
                            key = keyword.casefold()
                            if keyword and key not in seen_keywords:
                                seen_keywords.add(key)
                                parsed_keywords.append(keyword)
                        self.keyword_search.keywords = parsed_keywords or Config.load_keywords()
                    else:
                        self.keyword_search.keywords = Config.load_keywords()
                    self.log(f"🎌 Поиск новых каналов по {len(self.keyword_search.keywords)} тегам...")
                    await self.keyword_search.search_all(auto_add_to_active=True)
                
                # Очистка кэшей памяти каждые 50 циклов (предотвращение утечек)
                if cycle % 50 == 0:
                    self._cleanup_memory_caches()
                
                # Обработка очереди постов каждые 5 циклов
                if cycle % 5 == 0 and self.channel_poster:
                    try:
                        await self.channel_poster.process_queue()
                    except Exception as e:
                        self.log(f"Ошибка обработки очереди постов: {e}", "warning")
                
                # === Тоггл "auto_leave_junk" ===
                # Авто-отписка от мусорных чатов (раз в ~25 циклов).
                if (cycle % 25 == 0
                        and self._get_cached_setting("auto_leave_junk", False)):
                    try:
                        await self._maybe_leave_junk_chats()
                    except Exception as e:
                        self.log(f"⚠️ junk_chat scan error: {e}", "warning")
                
                cycle_pause = self._get_cached_setting("cycle_pause_seconds", 60)
                await asyncio.sleep(cycle_pause)
            except Exception as e:
                self.log(f"❌ Ошибка в цикле: {e}", "error")
                await asyncio.sleep(30)

    async def _process_auto_invite(self):
        """
        Автоматический инвайтинг в цикле воркера.

        Раньше инвайт был ТОЛЬКО ручным (кнопки в панели). Теперь, если в
        настройках включён тоггл auto_invite_enabled, воркер сам:
          1. парсит участников из чатов-источников (с пагинацией),
          2. приглашает небольшой батч в целевой канал каждый вызов,
          3. соблюдает дневной лимит и делает паузы между инвайтами.
        """
        if not self.inviter:
            return

        target = (self._get_cached_setting("auto_invite_target_channel", "") or "").strip()
        if not target:
            self.log("⚠️ Авто-инвайт включён, но не задан целевой канал (auto_invite_target_channel)", "warning")
            return

        # Источники: строка через запятую/перенос строки или список
        raw_sources = self._get_cached_setting("auto_invite_source_chats", "") or ""
        if isinstance(raw_sources, list):
            sources = [str(s).strip() for s in raw_sources if str(s).strip()]
        else:
            sources = [s.strip() for s in str(raw_sources).replace("\n", ",").split(",") if s.strip()]

        per_cycle = int(self._get_cached_setting("auto_invite_per_cycle", 3) or 3)
        daily_limit = int(self._get_cached_setting("auto_invite_daily_limit", Config.INVITER_DAILY_LIMIT)
                          or Config.INVITER_DAILY_LIMIT)

        try:
            stats = await self.inviter.run_auto_invite_batch(
                channel_id=target,
                source_chats=sources,
                per_cycle=per_cycle,
                daily_limit=daily_limit,
            )
            if stats.get('reached_daily_limit'):
                self.log(f"🛑 Авто-инвайт: дневной лимит достигнут ({daily_limit})")
            elif stats.get('total'):
                self.log(
                    f"📨 Авто-инвайт в {target}: +{stats.get('success', 0)} приглашено, "
                    f"{stats.get('skipped', 0)} пропущено, {stats.get('errors', 0)} ошибок "
                    f"(спарсено новых: {stats.get('parsed', 0)})"
                )
        except Exception as e:
            self.log(f"❌ Ошибка авто-инвайта: {e}", "error")

    def _update_active_channels(self):
        """Обновляет список активных каналов из БД, исключая забаненные для этого аккаунта"""
        # Получаем каналы из базы данных (только с открытыми комментами)
        try:
            db_channels = self.db.get_found_channels(limit=500, only_open_comments=True)
            all_channels = [ch['channel'] for ch in db_channels]
        except Exception as e:
            print(f"[DB ERROR] get_found_channels: {e}")
            all_channels = []
        
        try:
            banned_channels = self.db.get_banned_channels_for_account(self.account_id)
        except Exception as e:
            print(f"[DB ERROR] get_banned_channels: {e}")
            banned_channels = []
        
        # Фильтруем забаненные каналы
        self.active_channels = [ch for ch in all_channels if ch.lstrip('@') not in banned_channels]

    async def _process_channels(self):
        mode = self._get_current_mode()
        prob = Config.MODE_PROBABILITY.get(mode, 1.0)
        
        # Получаем режим комментирования
        comment_mode = self._get_cached_setting("comment_channels_mode", "all")
        
        # Очищаем истекшие блокировки
        self.db.cleanup_expired_locks()
        
        processed_count = 0
        skipped_old = 0
        skipped_processed = 0
        
        for name in self.active_channels:
            if not self.is_running: break
            
            # Фильтрация по режиму комментирования
            # Определяем приватный ли канал (все форматы)
            is_private = (
                name.startswith('+') or 
                't.me/+' in name or 
                'joinchat/' in name or
                'telegram.me/+' in name
            )
            
            if comment_mode == 'public' and is_private:
                continue  # Пропускаем приватные
            elif comment_mode == 'private' and not is_private:
                continue  # Пропускаем публичные
            elif comment_mode == 'joined' and is_private:
                # Проверяем есть ли channel_id (значит вступили)
                channel_info = self.db.get_channel_info(name)
                if not channel_info or not channel_info.get('channel_id'):
                    continue  # Пропускаем если не вступили
            
            # Для приватных каналов - проверяем есть ли channel_id
            if is_private:
                channel_info = self.db.get_channel_info(name)
                if not channel_info or not channel_info.get('channel_id'):
                    # Нет channel_id - бот ещё не вступил, пропускаем без ошибки
                    continue
            
            # Проверка бана НА ЭТОМ аккаунте в этом канале
            if self.db.is_banned(self.account_id, name):
                continue
            
            # Проверка блокировки другим аккаунтом И сразу блокируем если свободен
            if self.db.is_channel_locked(self.account_id, name):
                continue
            
            # Пробуем заблокировать канал СРАЗУ (до любых запросов к Telegram)
            if not self.db.lock_channel(self.account_id, name, lock_minutes=30):
                # Канал уже заблокирован другим аккаунтом
                continue
                
            # Шанс пропуска поста в зависимости от режима
            if random.random() > prob:
                self.db.unlock_channel(name)  # Освобождаем если пропускаем
                continue

            try:
                # Для числовых ID (приватные каналы) используем PeerChannel
                if name.lstrip('-').isdigit():
                    from telethon.tl.types import PeerChannel
                    try:
                        channel = await self.client.get_entity(PeerChannel(int(name)))
                    except Exception:
                        # Если не получилось - пропускаем, канал недоступен
                        self.db.unlock_channel(name)
                        continue
                elif is_private:
                    # Приватный канал по инвайт-хешу (любой формат)
                    # Пробуем получить по channel_id из базы
                    channel_info = self.db.get_channel_info(name)
                    if channel_info and channel_info.get('channel_id'):
                        try:
                            from telethon.tl.types import PeerChannel
                            channel = await self.client.get_entity(PeerChannel(int(channel_info['channel_id'])))
                        except Exception:
                            self.db.unlock_channel(name)
                            continue
                    else:
                        # Нет channel_id - пропускаем без ошибки
                        self.db.unlock_channel(name)
                        continue
                else:
                    try:
                        channel = await self.client.get_entity(name)
                    except ValueError as e:
                        # Cannot find any entity - канал недоступен
                        if "cannot find any entity" in str(e).lower() or "not part of" in str(e).lower():
                            self.db.unlock_channel(name)
                            continue
                        raise
                
                # Проверяем что это канал с broadcast (не чат/группа)
                if not getattr(channel, 'broadcast', False):
                    self.db.unlock_channel(name)
                    continue
                
                messages = await self.client.get_messages(channel, limit=1)
                if not messages:
                    self.db.unlock_channel(name)
                    continue
                
                post = messages[0]
                
                # РАННЯЯ ПРОВЕРКА: Быстрая проверка возраста поста (до других проверок)
                # Предотвращает лишние API запросы для старых постов
                now = datetime.now(timezone.utc)
                if post.date:
                    post_date = post.date
                    if post_date.tzinfo is None:
                        post_date = post_date.replace(tzinfo=timezone.utc)
                    post_age_hours = (now - post_date).total_seconds() / 3600
                    max_age = self._get_cached_setting("max_post_age_hours", Config.COMMENT_MAX_POST_AGE_HOURS)
                    
                    if post_age_hours > max_age:
                        # Пост слишком старый - пропускаем канал сразу
                        self.db.mark_post_processed(self.account_id, name, post.id)
                        skipped_old += 1
                        self.db.unlock_channel(name)
                        continue
                
                # Проверяем, не обработан ли пост ЛЮБЫМ аккаунтом (предотвращаем дубли)
                if self.db.is_post_processed_by_any(name, post.id):
                    skipped_processed += 1
                    self.db.unlock_channel(name)
                    continue
                
                # Проверяем, открыты ли комментарии
                discussion_chat = None
                try:
                    discussion = await self.client(GetDiscussionMessageRequest(peer=channel, msg_id=post.id))
                    if discussion and discussion.chats:
                        discussion_chat = discussion.chats[0]
                except Exception as e:
                    # Если комментарии закрыты или нас забанили
                    err_str = str(e).lower()
                    
                    # Проверка на необходимость вступить в группу обсуждений
                    if "join" in err_str and "discussion" in err_str:
                        try:
                            # Получаем группу обсуждений и вступаем
                            full_channel = await self.client(GetFullChannelRequest(channel))
                            if full_channel.full_chat.linked_chat_id:
                                joined = await self._join_discussion_group_if_needed(
                                    full_channel.full_chat.linked_chat_id, name
                                )
                                if joined == "pending":
                                    # Заявка на модерации — НЕ баним, вернёмся к каналу позже
                                    self.db.unlock_channel(name)
                                    continue
                                if joined:
                                    # Повторяем попытку получить обсуждение
                                    await asyncio.sleep(2)
                                    discussion = await self.client(GetDiscussionMessageRequest(peer=channel, msg_id=post.id))
                                    if discussion and discussion.chats:
                                        discussion_chat = discussion.chats[0]
                        except Exception as join_err:
                            join_err_str = str(join_err).lower()
                            # При ЛЮБОЙ ошибке вступления в обсуждения - помечаем канал
                            # чтобы не пытаться снова и снова
                            self.log(f"⚠️ Не удалось вступить в группу обсуждений {name}: {join_err}. Помечаю канал.", "warning")
                            self.db.mark_banned(self.account_id, name)
                            self.db.mark_post_processed(self.account_id, name, post.id)
                            self.db.unlock_channel(name)
                            continue
                    
                    # Расширенный список паттернов для детекции бана
                    ban_patterns = [
                        "banned", "restricted", "kicked", "user_banned",
                        "chat_write_forbidden", "channel_private", 
                        "chat_admin_required",
                        "you have been banned",
                        "you're not allowed", "access denied",
                        "allow_payment_required"  # Требуется Premium
                    ]
                    
                    # Паттерны которые НЕ являются баном (просто нужно вступить или нет прав)
                    soft_error_patterns = [
                        "you can't write", "user_not_participant",
                        "join", "discussion", "not a member"
                    ]
                    
                    is_soft_error = any(pattern in err_str for pattern in soft_error_patterns)
                    
                    # Проверка на бан в канале (только если это не soft error)
                    is_banned = any(pattern in err_str for pattern in ban_patterns) and not is_soft_error
                    if is_banned:
                        self.log(f"🚫 Аккаунт забанен/ограничен в {name}. Помечаю в базе.", "warning")
                        self.db.mark_banned(self.account_id, name)
                        self.health_monitor.record_error(self.account_id, e)
                        self.db.unlock_channel(name)
                        continue
                    
                    # Soft errors - пробуем вступить в группу обсуждений
                    if is_soft_error:
                        # Пробуем вступить в группу обсуждений
                        try:
                            full_channel = await self.client(GetFullChannelRequest(channel))
                            if full_channel.full_chat.linked_chat_id:
                                joined = await self._join_discussion_group_if_needed(
                                    full_channel.full_chat.linked_chat_id, name
                                )
                                if joined:
                                    # Не помечаем пост как обработанный - попробуем в следующем цикле
                                    self.db.unlock_channel(name)
                                    continue
                            else:
                                self.log(f"⚠️ У канала {name} нет группы обсуждений. Помечаю как недоступный.", "warning")
                                self.db.mark_banned(self.account_id, name)
                                self.db.mark_post_processed(self.account_id, name, post.id)
                                self.db.unlock_channel(name)
                                continue
                        except Exception as join_err:
                            join_err_str = str(join_err).lower()
                            # Если ошибка связана с приватностью/доступом - помечаем канал как недоступный
                            if any(p in join_err_str for p in ["private", "permission", "banned", "forbidden", "access"]):
                                self.log(f"⚠️ Группа обсуждений {name} недоступна: {join_err}. Помечаю канал.", "warning")
                                self.db.mark_banned(self.account_id, name)
                            else:
                                self.log(f"⚠️ Не удалось вступить в группу обсуждений {name}: {join_err}", "warning")
                            self.db.mark_post_processed(self.account_id, name, post.id)
                            self.db.unlock_channel(name)
                            continue

                    if "private" in err_str or "permission" in err_str or "forbidden" in err_str:
                        # Если это ошибка доступа (например канал стал приватным),
                        # то возможно он недоступен для ВСЕХ. Но мы пока пометим только для себя.
                        self.log(f"🚫 Ошибка доступа к {name} ({e}). Пропускаю.", "warning")
                        self.db.mark_banned(self.account_id, name)
                        self.db.mark_post_processed(self.account_id, name, post.id)
                        self.db.unlock_channel(name)
                        
                        # Проверяем, все ли аккаунты забанены в этом канале
                        ban_stats = self.db.get_ban_stats_for_channel(name)
                        if ban_stats["all_banned"]:
                            self.db.update_channel_comments_status(name, has_open_comments=False)
                            self.log(f"🔒 Все аккаунты ({ban_stats['banned_count']}) забанены в {name}. Помечаю как закрытый.", "warning")
                        continue

                    # Комментарии недоступны - помечаем аккаунт как забаненный
                    self.log(f"⚠️ Комментарии в {name} недоступны для этого аккаунта ({e}).", "warning")
                    
                    # Помечаем как забанен для этого аккаунта
                    self.db.mark_banned(self.account_id, name)
                    self.db.unlock_channel(name)
                    
                    # Проверяем, все ли аккаунты забанены в этом канале
                    ban_stats = self.db.get_ban_stats_for_channel(name)
                    if ban_stats["all_banned"]:
                        self.db.update_channel_comments_status(name, has_open_comments=False)
                        self.log(f"🔒 Все аккаунты ({ban_stats['banned_count']}) забанены в {name}. Помечаю как закрытый.", "warning")
                    else:
                        self.log(f"ℹ️ Забанено {ban_stats['banned_count']}/{ban_stats['total_accounts']} аккаунтов в {name}. Другие попробуют.", "info")
                    
                    # Помечаем пост как обработанный чтобы не зацикливаться
                    self.db.mark_post_processed(self.account_id, name, post.id)
                    continue

                self.log(f"📝 Новый пост в {name}, генерирую комментарий...")

                # Реакция на пост
                if self._get_cached_setting("react_to_posts", True):
                    await self.reaction_manager.send_reaction(channel, post.id)
                
                # Проверяем режим быстрого комментария
                # Быстрый режим работает ТОЛЬКО для свежих постов (по умолчанию < 5 минут)
                quick_mode = self._get_cached_setting("quick_comment_mode", False)
                quick_max_age_minutes = self._get_cached_setting("quick_comment_max_age_minutes", 5)
                
                # Проверяем возраст поста для быстрого режима
                if quick_mode and post.date:
                    post_date = post.date
                    if post_date.tzinfo is None:
                        post_date = post_date.replace(tzinfo=timezone.utc)
                    post_age_minutes = (now - post_date).total_seconds() / 60
                    
                    if post_age_minutes > quick_max_age_minutes:
                        # Пост слишком старый для быстрого режима - используем обычный
                        quick_mode = False
                        self.log(f"⚠️ Пост старше {quick_max_age_minutes} мин ({post_age_minutes:.1f} мин), Quick Mode отключен - используем обычный режим")
                    else:
                        self.log(f"⚡ Пост свежий ({post_age_minutes:.1f} мин), Quick Mode активен!")
                
                # Список заглушек для быстрого режима.
                # Все начинаются с "_" (как просил юзер): кидаем "_" чтобы быть
                # первым в комментах, а потом редактируем на текст нейронки.
                quick_placeholders = [
                    "_", "_.", "_..", "_ ", "_-", "_·", "_,", "_—"
                ]
                
                result = None
                
                if quick_mode:
                    # БЫСТРЫЙ РЕЖИМ: сначала отправляем заглушку
                    placeholder = random.choice(quick_placeholders)
                    try:
                        result = await self._send_comment_message(channel, placeholder, post.id)
                        self.log(f"⚡ Быстрый коммент отправлен в {name}, генерирую текст...")
                    except Exception as e:
                        self.log(f"❌ Ошибка быстрого комментария в {name}: {e}", "error")
                        self.db.unlock_channel(name)
                        raise
                
                # Скачиваем изображение если есть
                image_bytes = None
                if post.photo and Config.SUPPORT_IMAGES:
                    try:
                        image_bytes = await self.client.download_media(post.photo, bytes)
                        self.log(f"📷 Изображение загружено ({len(image_bytes) // 1024}KB)")
                    except Exception as e:
                        self.log(f"⚠️ Не удалось загрузить изображение: {e}", "warning")
                
                # Генерация комментария (ASYNC - не блокирует event loop)
                comment = await self.comment_generator.generate_comment_async(post.raw_text or "", image_bytes)
                
                # Проверка на пустой комментарий (нейросеть не сработала)
                if not comment or not comment.strip():
                    reason = getattr(self.comment_generator, "last_error", "") or "неизвестная причина"
                    self.log(f"⚠️ Нейросеть не вернула комментарий для {name} ({reason}), пропускаю", "warning")
                    # Если был быстрый коммент - удаляем заглушку
                    if quick_mode and result:
                        try:
                            await self.client.delete_messages(channel, [result.id])
                        except:
                            pass
                    self.db.unlock_channel(name)
                    continue
                
                # Проверка на дубликат
                if self.db.is_comment_duplicate(name, comment, hours=24):
                    self.log(f"🔄 Дубликат комментария в {name}, перегенерирую...")
                    # Пробуем сгенерировать другой комментарий
                    for _ in range(3):
                        comment = await self.comment_generator.generate_comment_async(post.raw_text or "", image_bytes)
                        # Проверяем что комментарий не пустой и не дубликат
                        if comment and comment.strip() and not self.db.is_comment_duplicate(name, comment, hours=24):
                            break
                    else:
                        self.log(f"⚠️ Не удалось сгенерировать уникальный комментарий для {name}, пропускаю")
                        # Если был быстрый коммент - удаляем заглушку
                        if quick_mode and result:
                            try:
                                await self.client.delete_messages(channel, [result.id])
                            except:
                                pass
                        self.db.mark_post_processed(self.account_id, name, post.id)
                        self.db.unlock_channel(name)
                        continue
                
                if quick_mode and result:
                    # БЫСТРЫЙ РЕЖИМ: редактируем заглушку на сгенерированный текст
                    # Важно: редактируем в группе обсуждений, а не в канале!
                    try:
                        edit_entity = discussion_chat if discussion_chat else channel
                        
                        # Имитируем набор текста (0.05 сек на символ, 2-5 сек в среднем)
                        typing_time = min(len(comment) * 0.05, 5.0)  # Максимум 5 секунд
                        await self._simulate_typing(edit_entity, seconds=typing_time)
                        
                        await self.client.edit_message(edit_entity, result.id, comment)
                        self.log(f"✏️ Комментарий отредактирован в {name}")
                    except Exception as e:
                        self.log(f"⚠️ Не удалось отредактировать: {e}", "warning")
                        # Если не удалось отредактировать - удаляем заглушку и отправляем новый
                        try:
                            await self.client.delete_messages(discussion_chat or channel, [result.id])
                        except:
                            pass
                        result = await self._send_comment_message(channel, comment, post.id)
                else:
                    # ОБЫЧНЫЙ РЕЖИМ: задержка и отправка
                    delay_mult = Config.MODE_DELAY_MULT.get(mode, 1.0)
                    delay_min = self._get_cached_setting("comment_delay_min", Config.COMMENT_DELAY_MIN)
                    delay_max = self._get_cached_setting("comment_delay_max", Config.COMMENT_DELAY_MAX)
                    
                    # Режим прогрева - увеличиваем задержки в 2.5 раза (только для обычного р��жима)
                    if self._get_cached_setting("warmup_mode", False):
                        delay_mult *= 2.5
                        self.log(f"🔥 Warmup Mode активен - задержки увеличены в 2.5 раза")
                    
                    delay = random.randint(delay_min, delay_max) * delay_mult
                    self.log(f"⏳ Ожидание {delay:.1f} сек. (Режим: {mode})")
                    await asyncio.sleep(delay)
                    
                    result = await self._send_comment_message(channel, comment, post.id)
                
                self.db.mark_post_processed(self.account_id, name, post.id)
                self.db.increment_stat(self.account_id, 'comments')
                
                # Сохраняем комментарий с message_id для ссылки
                self.db.save_comment(self.account_id, name, comment, post.id, result.id)
                
                # Записываем успешную операцию
                self.health_monitor.record_success(self.account_id)
                self.db.increment_daily_stat(self.account_id, name, success=True)
                
                # Формируем ссылку на комментарий
                clean_name = name.lstrip('@')
                log_link = f"https://t.me/{clean_name}/{post.id}?comment={result.id}"
                
                self.log(f"✅ Комментарий отправлен в {name}: {log_link}")

                # Проверяем, что коммент реально виден (ловим теневой бан/премодерац��ю)
                if self._get_cached_setting("verify_comment_published", True):
                    await self._verify_comment_published(result, name)
                
                # Разблокируем канал после успешного комментария
                self.db.unlock_channel(name)
                
                # Парсим ссылки из поста для пополнения базы каналов
                await self._extract_links_from_post(post, name)
                
                # Лайк чужих комментариев (имитация популярности)
                if self.db.get_setting("like_other_comments", False):
                    await self._like_random_comments(channel, post.id)

            except PeerFloodError as e:
                # === Тоггл "spambot_unblock" ===
                # Telegram временно ограничил исходящие — это анти-спам.
                self._peer_flood_count += 1
                self.log(
                    f"🚨 PeerFloodError в {name}. Анти-спам Telegram. Счётчик: {self._peer_flood_count}",
                    "warning",
                )
                self.db.mark_banned(self.account_id, name)
                self.db.unlock_channel(name)
                # Записываем в health-monitor (как FloodWait), чтобы воркер встал на паузу
                try:
                    self.health_monitor.record_flood_wait(self.account_id, 600)
                except Exception:
                    pass
                # Если включён тоггл — идём в @SpamBot за статусом
                if self._get_cached_setting("spambot_unblock", True):
                    await self._consult_spambot(reason=f"PeerFloodError на {name}")
                # Делаем большую паузу прежде чем продолжать
                await asyncio.sleep(60)

            except FloodWaitError as e:
                # Обработка FloodWait с буфером +10 секунд
                wait_seconds = e.seconds + 10
                is_paused = self.health_monitor.record_flood_wait(self.account_id, e.seconds)
                
                # Разблокируем канал
                self.db.unlock_channel(name)
                
                if is_paused:
                    self.log(f"⏸️ FloodWait {e.seconds}с (>1ч). Аккаунт приостановлен.", "warning")
                else:
                    self.log(f"⏳ FloodWait {e.seconds}с. Ожидание {wait_seconds}с...", "warning")
                    await asyncio.sleep(wait_seconds)
            
            except SlowModeWaitError as e:
                # Обработка Slow Mode (ограничение частоты сообщений в канале)
                self.log(f"🐢 SlowMode в {name}, ждем {e.seconds} сек", "warning")
                # Не помечаем канал как плохой, просто ждем и пробуем в след. цикле
                self.db.unlock_channel(name)
                await asyncio.sleep(e.seconds + 5)  # +5 сек запаса
            
            except ChannelPrivateError:
                # Канал приватный или нас забанили - помечаем аккаунт как забаненный
                self.log(f"⚠️ Канал {name} приватный/недоступен для этого аккаунта", "warning")
                self.db.mark_banned(self.account_id, name)
                self.db.unlock_channel(name)
                
                # Проверяем, все ли аккаунты забанены
                ban_stats = self.db.get_ban_stats_for_channel(name)
                if ban_stats["all_banned"]:
                    self.db.update_channel_comments_status(name, has_open_comments=False)
                    self.log(f"🔒 Все аккаунты забанены в {name}. Помечаю как закрытый.", "warning")
            
            except ChatWriteForbiddenError:
                # Нет прав писать в этом чате - помечаем как забанен в канале, НЕ ошибка аккаунта
                self.log(f"⚠️ Нет прав писать в {name}, помечаю как забанен в канале", "warning")
                self.db.mark_banned(self.account_id, name)
                self.db.mark_post_processed(self.account_id, name, post.id)
                self.db.unlock_channel(name)

                # === Тоггл "leave_if_no_write" ===
                # Если включён — выходим из дискуссионной группы
                if self._get_cached_setting("leave_if_no_write", True):
                    await self._safe_leave_discussion_group(channel, name)
                
                # Проверяем, все ли аккаунты забанены
                ban_stats = self.db.get_ban_stats_for_channel(name)
                if ban_stats["all_banned"]:
                    self.db.update_channel_comments_status(name, has_open_comments=False)
                    self.log(f"🔒 Все аккаунты забанены в {name}. Помечаю как закрытый.", "warning")
            
            except (UserDeactivatedBanError, AuthKeyUnregisteredError) as e:
                # Аккаунт забанен или деактивирован
                self.log(f"🚫 Аккаунт забанен/деактивирован: {e}", "error")
                self._update_account_status("banned")
                self.db.unlock_channel(name)  # Разблокируем канал
                self.is_running = False
                return  # Выходим из цикла
                    
            except Exception as e:
                err_str = str(e).lower()
                err_upper = str(e).upper()
                
                # Проверка на заморозку аккаунта для определённых методов
                if 'FROZEN' in err_upper:
                    self.log(f"🥶 Аккаунт заморожен для некоторых действий: {e}", "warning")
                    self._update_account_status("frozen_join")
                    self.db.mark_banned(self.account_id, name)
                    self.db.unlock_channel(name)
                    continue  # Продолжаем с другими каналами
                
                # Проверка на бан аккаунта
                if "banned" in err_str or "deactivated" in err_str or "account was deleted" in err_str:
                    self.log(f"🚫 Аккаунт забанен: {e}", "error")
                    self._update_account_status("banned")
                    self.db.unlock_channel(name)  # Разблокируем канал
                    self.is_running = False
                    return
                
                # ChatWriteForbidden - нет прав писать (не бан аккаунта, а ограничение канала)
                if "chatwriteforbidden" in err_str or "can't write" in err_str:
                    self.log(f"⚠️ Нет прав писать в {name}, помечаю как забанен в канале", "warning")
                    self.db.mark_banned(self.account_id, name)
                    self.db.mark_post_processed(self.account_id, name, post.id)
                    self.db.unlock_channel(name)  # Разблокируем канал
                    
                    # Проверяем, все ли аккаунты забанены
                    ban_stats = self.db.get_ban_stats_for_channel(name)
                    if ban_stats["all_banned"]:
                        self.db.update_channel_comments_status(name, has_open_comments=False)
                        self.log(f"🔒 Все аккаунты забанены в {name}. Помечаю как закрытый.", "warning")
                    continue  # НЕ считаем ошибкой аккаунта!
                
                # Проверяем статус аккаунта - если frozen, не удаляем каналы
                is_account_frozen = False
                try:
                    with self.db.get_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT status FROM accounts WHERE id = ?", (self.account_id,))
                        row = cursor.fetchone()
                        if row and row['status'] in ('frozen', 'frozen_join'):
                            is_account_frozen = True
                except:
                    pass
                
                # Канал не существует - удаляем из базы, НЕ считаем ошибкой аккаунта
                # НО: если аккаунт заморожен, не удаляем - это может быть ложная ошибка
                if "no user has" in err_str and "as username" in err_str:
                    if is_account_frozen:
                        self.log(f"⚠️ Канал {name} недоступен (аккаунт заморожен), пропускаю", "warning")
                        self.db.mark_banned(self.account_id, name)
                        self.db.unlock_channel(name)
                        continue
                    self.log(f"🗑️ Канал {name} не существует, удаляю из базы", "warning")
                    self.db.delete_found_channel(name)
                    self.db.unlock_channel(name)  # Разблокируем канал
                    continue  # НЕ считаем ошибкой аккаунта!
                
                # Канал недоступен - username не занят или невалидный
                if "username not occupied" in err_str or "username invalid" in err_str:
                    if is_account_frozen:
                        self.log(f"⚠️ Канал {name} недоступен (аккаунт заморожен), пропускаю", "warning")
                        self.db.mark_banned(self.account_id, name)
                        self.db.unlock_channel(name)
                        continue
                    self.log(f"🗑️ Канал {name} недоступен (username), удаляю из базы", "warning")
                    self.db.delete_found_channel(name)
                    self.db.unlock_channel(name)  # Разблокируем канал
                    continue  # НЕ считаем ошибкой аккаунта!
                
                # Ошибка invite hash - приватный канал, помечаем как закрытый но НЕ удаляем
                if "invite hash" in err_str:
                    self.log(f"⚠️ Канал {name} приватный (invite hash), помечаю как закрытый", "warning")
                    self.db.mark_banned(self.account_id, name)
                    self.db.unlock_channel(name)  # Разблокируем канал
                    ban_stats = self.db.get_ban_stats_for_channel(name)
                    if ban_stats["all_banned"]:
                        self.db.update_channel_comments_status(name, has_open_comments=False)
                    continue
                
                # Ошибки доступа к каналу - помечаем аккаунт как забаненный
                if "private" in err_str or "not accessible" in err_str:
                    self.log(f"⚠️ Канал {name} недоступен для этого аккаунта", "warning")
                    self.db.mark_banned(self.account_id, name)
                    self.db.unlock_channel(name)  # Разблокируем канал
                    
                    # Проверяем, все ли аккаунты забанены
                    ban_stats = self.db.get_ban_stats_for_channel(name)
                    if ban_stats["all_banned"]:
                        self.db.update_channel_comments_status(name, has_open_comments=False)
                        self.log(f"🔒 Все аккаунты забанены в {name}. Помечаю как закрытый.", "warning")
                    continue  # НЕ считаем ошибкой аккаунта!
                
                # Не состоим в канале/группе - пропускаем без ошибки
                if "not part of" in err_str or "cannot get entity" in err_str:
                    self.log(f"⚠️ Не состоим в канале {name}, пропускаю", "warning")
                    self.db.unlock_channel(name)
                    continue  # НЕ считаем ошибкой аккаунта!
                
                # Нужно вступить в группу обсуждений
                if "join" in err_str and "discussion" in err_str:
                    try:
                        full_channel = await self.client(GetFullChannelRequest(channel))
                        if full_channel.full_chat.linked_chat_id:
                            joined = await self._join_discussion_group_if_needed(
                                full_channel.full_chat.linked_chat_id, name
                            )
                            if not joined:
                                # Помечаем канал как забаненный чтобы не пытаться снова
                                self.log(f"⚠️ Не удалось вступить в группу обсуждений {name}. Помечаю.", "warning")
                                self.db.mark_banned(self.account_id, name)
                    except Exception as join_err:
                        # Помечаем канал как забане��ный чтобы не пытаться снова
                        self.log(f"⚠️ Не удалось вступить в группу обсуждений {name}: {join_err}. Помечаю.", "warning")
                        self.db.mark_banned(self.account_id, name)
                    self.db.unlock_channel(name)  # Разблокируем канал
                    continue  # Попробуем в следующем цикле
                
                # Только реальные ошибки аккаунта записываем в health monitor
                self.log(f"⚠️ Ошибка при обработке {name}: {e}", "error")
                self.db.unlock_channel(name)  # Разблокируем канал при любой ошибке
                new_status = self.health_monitor.record_error(self.account_id, e)
                self.db.increment_daily_stat(self.account_id, name, success=False)
                
                if new_status == "paused":
                    self.log(f"⛔ Слишком много ошибок. Аккаунт приостановлен.", "error")
        
        # Итоговый лог цикла
        if skipped_old > 0 or skipped_processed > 0:
            self.log(f"📊 Итог: старых постов {skipped_old}, уже обработано {skipped_processed}")

    def _get_current_mode(self) -> str:
        """Определяет текущий режим работы на основе настроек и времени"""
        try:
            global_mode = self.db.get_setting("work_mode", "neutral")
            
            # Если включен авто-ночной режим
            auto_night = self.db.get_setting("auto_night_mode", True)
            if auto_night:
                # Время в МСК (UTC+3)
                import datetime
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                msk_hour = (now_utc.hour + 3) % 24
                
                # Ночь с 00:00 до 08:00
                if 0 <= msk_hour < 8:
                    return "chill"
            
            return global_mode
        except Exception as e:
            print(f"[DB ERROR] _get_current_mode: {e}")
            return "neutral"

    async def _process_chats(self):
        """Логика общения в группах/чатах - ответы на сообщения через нейросеть"""
        try:
            from telethon.tl.types import Channel, Chat
            current_time = time.time()
            
            # Минимальное количество участников для чатов
            min_chat_members = self.db.get_setting("min_chat_members", 100)
            
            # Интервал между сообщениями в один чат (из настроек, по умолчанию 30 минут)
            chat_interval_minutes = self.db.get_setting("chat_message_interval_minutes", 30)
            chat_cooldown_seconds = chat_interval_minutes * 60
            
            # Шанс ответить в чате (из настроек, по умолчанию 3%)
            chat_reply_chance = self.db.get_setting("chat_reply_chance", 0.03)
            
            async for dialog in self.client.iter_dialogs(limit=30):
                if not self.is_running:
                    break
                    
                # Нас интересуют только группы (не каналы)
                if isinstance(dialog.entity, (Channel, Chat)) and getattr(dialog.entity, 'broadcast', False) == False:
                    chat_id = dialog.entity.id
                    chat_name = f"chat_{chat_id}"  # Уникальный идентификатор для блокировки
                    
                    # Фильтруем маленькие чаты по количеству участников
                    members_count = getattr(dialog.entity, 'participants_count', 0)
                    if members_count > 0 and members_count < min_chat_members:
                        continue
                    
                    # Проверяем блокировку другим аккаунтом
                    if self.db.is_channel_locked(self.account_id, chat_name):
                        continue
                    
                    # Проверяем, не забанен ли аккаунт в этом чате
                    if self.db.is_banned(self.account_id, chat_name):
                        continue
                    
                    # Проверяем кулдаун для этого чата (локальный)
                    last_msg_time = self._last_chat_message.get(chat_id, 0)
                    if current_time - last_msg_time < chat_cooldown_seconds:
                        continue
                    
                    # Шанс ответить в этом цикле
                    if random.random() > chat_reply_chance:
                        continue
                    
                    # Пробуем заблокировать чат (на время интервала)
                    if not self.db.lock_channel(self.account_id, chat_name, lock_minutes=chat_interval_minutes):
                        continue  # Другой аккаунт уже заблокировал
                    
                    # Получаем последние сообщения
                    msgs = await self.client.get_messages(dialog.entity, limit=10)
                    if not msgs:
                        self.db.unlock_channel(chat_name)
                        continue
                    
                    # Проверяем, не наше ли последнее сообщение
                    me = await self.client.get_me()
                    if msgs[0].sender_id == me.id:
                        self.db.unlock_channel(chat_name)
                        continue
                    
                    # Фильтруем сообщения - только от других людей, с текстом
                    other_msgs = [m for m in msgs if m.text and m.sender_id and m.sender_id != me.id]
                    if not other_msgs:
                        self.db.unlock_channel(chat_name)
                        continue
                    
                    # Выбираем случайное сообщение для ответа
                    target_msg = random.choice(other_msgs[:5])  # Из последних 5
                    
                    # Собираем контекст чата для нейросети
                    context_msgs = [m.text for m in msgs[:5] if m.text]
                    context = "\n".join(context_msgs)
                    
                    # Генерируем ответ через нейросеть
                    self.log(f"💬 Генерирую ответ в чате {dialog.name}...")
                    
                    # Используем специальный промпт для чатов
                    chat_prompt = f"""Контекст чата "{dialog.name}":
{context}

Напиши короткий естественный ответ на последнее сообщение. 
Отвечай по теме обсуждения, как обычный участник чата.
Максимум 1-2 предложения."""
                    
                    # Используем специальный метод для чатов (без сохранения истории)
                    reply = await self.autoresponder.generate_chat_response(chat_prompt, dialog.name)
                    
                    if not reply:
                        # Разблокируем чат чтобы другие могли попробовать
                        self.db.unlock_channel(chat_name)
                        continue
                    
                    # Задержка перед отправкой
                    await asyncio.sleep(random.randint(5, 15))
                    
                    # Отправляем ответ на конкретное сообщение
                    try:
                        entity = await self.client.get_input_entity(dialog.entity)
                        result = await self.client.send_message(entity, reply, reply_to=target_msg.id)
                        
                        self.log(f"💬 Ответил в чате {dialog.name}: {reply[:40]}...")
                        self._last_chat_message[chat_id] = current_time
                        
                        # Сохраняем сообщение в базу
                        self.db.save_chat_message(
                            account_id=self.account_id,
                            chat_name=dialog.name,
                            chat_id=chat_id,
                            message_text=reply,
                            message_id=result.id if result else None,
                            reply_to_id=target_msg.id
                        )
                        
                    except Exception as send_err:
                        err_str = str(send_err).lower()
                        # Если забанен в чате - помечаем
                        if 'banned' in err_str or 'forbidden' in err_str or 'kicked' in err_str:
                            self.log(f"🚫 Забанен в чате {dialog.name}, помечаю", "warning")
                            self.db.mark_banned(self.account_id, chat_name)
                        else:
                            self.log(f"⚠️ Не удалось отправить в {dialog.name}: {send_err}", "warning")
                    
                    # Разблокируем чат после попытки
                    self.db.unlock_channel(chat_name)

        except Exception as e:
            self.log(f"⚠️ Ошибка в чат-модуле: {e}", "error")

    async def _join_pending_channels(self):
        """Вступает в каналы из БД (поддерживает каналы с модерацией)"""
        # Получаем каналы из БД со статусом 'new' или без channel_id (не вступили)
        all_channels = self.db.get_found_channels(limit=50, only_open_comments=True)
        
        # Фильтруем - только те, в которые ещё не вступили
        channels_to_join = []
        for ch in all_channels:
            channel = ch['channel']
            # Пропускаем если уже обработан
            if self.db.is_invite_processed(self.account_id, channel):
                continue
            # Пропускаем удалённые
            if self.db.is_channel_deleted(channel):
                continue
            # Пропускаем забаненные для этого аккаунта
            if self.db.is_banned(self.account_id, channel):
                continue
            channels_to_join.append(channel)
        
        if not channels_to_join:
            # Проверяем pending requests
            await self._check_pending_join_requests()
            return
        
        self.log(f"📥 Вступаю в {len(channels_to_join)} новых каналов...")
        
        for channel in channels_to_join:
            try:
                # Проверяем блокировку - может другой аккаунт уже обрабатывает
                if self.db.is_channel_locked(self.account_id, channel):
                    continue
                
                # Блокируем канал сразу
                if not self.db.lock_channel(self.account_id, channel, lock_minutes=60):
                    continue
                
                # Используем улучшенный метод с поддержкой модерации
                success, message, extra_info = await self.channel_joiner.join_channel_async(channel)
                
                # Проверяем, это pending request (канал с модерацией)
                if extra_info.get('pending'):
                    # Сохраняем в pending_joins с правильным account_id
                    invite_hash = self.channel_joiner._extract_invite_hash(channel)
                    self.db.add_pending_join(
                        account_id=self.account_id,
                        invite_hash=invite_hash,
                        channel_title=extra_info.get('title')
                    )
                    # Помечаем как обработанный чтобы не пытаться вступить снова
                    self.db.mark_invite_processed(
                        self.account_id, 
                        channel,
                        channel_id=extra_info.get('channel_id')
                    )
                    self.log(f"⏳ {message}")
                else:
                    # Помечаем как обработанный для этого аккаунта
                    self.db.mark_invite_processed(
                        self.account_id, 
                        channel,
                        channel_id=extra_info.get('channel_id')
                    )
                
                if success and not extra_info.get('pending'):
                    self.log(f"✅ {message}")
                    
                    # Для приватных каналов сохраняем информацию в базу
                    if channel.startswith('+') or 't.me/+' in channel:
                        channel_id = extra_info.get('channel_id')
                        title = extra_info.get('title')
                        
                        if channel_id and title:
                            self.db.add_found_channel(
                                channel=channel,
                                title=title,
                                source="invite_link",
                                subs=0,
                                can_comment=True,
                                min_subs=0
                            )
                            self.db.update_channel_info(channel, channel_id=channel_id, title=title)
                        elif channel_id:
                            # Есть channel_id но нет title - получаем entity напрямую
                            try:
                                from telethon.tl.types import PeerChannel
                                entity = await self.client.get_entity(PeerChannel(channel_id))
                                real_title = getattr(entity, 'title', f'Channel {channel_id}')
                                subs = getattr(entity, 'participants_count', 0)
                                self.db.add_found_channel(
                                    channel=channel,
                                    title=real_title,
                                    source="invite_link",
                                    subs=subs,
                                    can_comment=True,
                                    min_subs=0
                                )
                                self.db.update_channel_info(channel, channel_id=channel_id, title=real_title)
                            except Exception as e:
                                self.log(f"⚠️ Не удалось получить название ��анала {channel}: {e}", "warning")
                                # Сохраняем с хэшем как названием
                                self.db.add_found_channel(
                                    channel=channel,
                                    title=channel,  # Используем хэш как название
                                    source="invite_link",
                                    subs=0,
                                    can_comment=True,
                                    min_subs=0
                                )
                        else:
                            # Нет ни channel_id ни title - сохраняем с хэшем как названием
                            self.log(f"⚠️ Нет данных о канале {channel}, сохраняю с хэшем", "warning")
                            self.db.add_found_channel(
                                channel=channel,
                                title=channel,  # Используем хэш как название
                                source="invite_link",
                                subs=0,
                                can_comment=True,
                                min_subs=0
                            )
                    
                elif not success:
                    self.log(f"⚠️ {message}", "warning")
                    
                    # Проверяем, заморожен ли аккаунт для вступлений
                    if extra_info.get('frozen_join'):
                        self.log(f"🥶 Аккаунт заморожен для вступлений! Пропускаю остальные каналы.", "warning")
                        self._update_account_status("frozen_join")
                        self.db.unlock_channel(channel)
                        break  # Прекращаем попытки вступления
                    
                    # Помечаем как обработанный чтобы не пытаться снова
                    self.db.mark_invite_processed(self.account_id, channel)
                
                # Задержка между вступлениями
                await asyncio.sleep(random.randint(10, 30))
                
            except Exception as e:
                self.log(f"❌ Ошибка вступления в {channel}: {e}", "error")
        
        # Проверяем pending requests после обработки новых каналов
        await self._check_pending_join_requests()

    async def _check_pending_join_requests(self):
        """Проверяет статус pending join requests (каналы с модерацией)"""
        try:
            pending_requests = self.db.get_pending_joins(account_id=self.account_id, status='pending')
            
            if not pending_requests:
                return
            
            self.log(f"🔍 Проверяю {len(pending_requests)} ожидающих одобрения каналов...")
            
            for request in pending_requests:
                invite_hash = request['invite_hash']
                
                # Увеличиваем счётчик проверок
                self.db.increment_pending_check_count(self.account_id, invite_hash)
                
                # Проверяем статус
                status, channel_id = await self.channel_joiner.check_pending_status(invite_hash)
                
                if status == 'approved':
                    self.log(f"✅ Одобрен вход в канал: {request.get('channel_title', invite_hash)}")
                    self.db.update_pending_join_status(
                        self.account_id, 
                        invite_hash, 
                        'approved',
                        channel_id=channel_id
                    )
                    
                    # Добавляем в found_channels
                    if channel_id:
                        # Получаем информацию о канале
                        try:
                            from telethon.tl.types import PeerChannel
                            entity = await self.client.get_entity(PeerChannel(channel_id))
                            self.db.add_found_channel(
                                channel=f"+{invite_hash}",
                                title=entity.title,
                                source="pending_approved",
                                subs=getattr(entity, 'participants_count', 0),
                                can_comment=True,
                                min_subs=0
                            )
                            self.db.update_channel_info(f"+{invite_hash}", channel_id=channel_id, title=entity.title)
                        except Exception as e:
                            self.log(f"⚠️ Не удалось получить инфо о канале {invite_hash}: {e}", "warning")
                    
                elif status == 'expired':
                    self.log(f"❌ Ссылка истекла: {request.get('channel_title', invite_hash)}")
                    self.db.update_pending_join_status(self.account_id, invite_hash, 'expired')
                    
                elif status == 'pending':
                    # Всё ещё ждём одобрения
                    check_count = request.get('check_count', 0) + 1
                    if check_count > 50:  # ~50 проверок = ~7 дней при проверке каждые 3 часа
                        self.log(f"⏰ Слишком долго ждём одобрения: {request.get('channel_title', invite_hash)}")
                        self.db.update_pending_join_status(self.account_id, invite_hash, 'expired')
                
                # Задержка между проверками
                await asyncio.sleep(2)
            
            # Очищаем старые pending requests
            self.db.cleanup_old_pending_joins(max_age_days=7)
            
        except Exception as e:
            self.log(f"⚠️ Ошибка проверки pending requests: {e}", "error")

    async def _gradual_join_from_database(self):
        """
        Постепенно вступает в каналы из базы данных (1 канал за вызов).
        Для новых аккаунтов с warmup_mode - увеличенные задержки.
        Если аккаунт frozen_join - пропускает приватные каналы.
        """
        try:
            # Получаем каналы с открытыми комментариями из базы
            all_channels = self.db.get_found_channels(limit=200, only_open_comments=True)
            
            if not all_channels:
                return
            
            # Получаем список каналов, в которых аккаунт уже состоит
            my_dialogs = set()
            try:
                async for dialog in self.client.iter_dialogs(limit=300):
                    if dialog.is_channel:
                        # Сохраняем username и id
                        if dialog.entity.username:
                            my_dialogs.add(dialog.entity.username.lower())
                        my_dialogs.add(str(dialog.entity.id))
            except Exception as e:
                self.log(f"⚠️ Ошибка получения диалогов: {e}", "warning")
                return
            
            # Проверяем статус аккаунта для пропуска приватных каналов
            is_frozen_join = False
            try:
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT status FROM accounts WHERE id = ?", (self.account_id,))
                    row = cursor.fetchone()
                    if row and row['status'] == 'frozen_join':
                        is_frozen_join = True
            except:
                pass
            
            # Ищем канал, в который ещё не вступили
            channel_to_join = None
            for ch in all_channels:
                channel = ch['channel']
                channel_id = ch.get('channel_id')
                
                # Проверяем кэш вступленных каналов в этой сессии
                if channel in self._joined_channels:
                    continue
                
                # Проверяем, не удалён ли канал пользователем
                if self.db.is_channel_deleted(channel):
                    continue
                
                # Если аккаунт frozen_join - пропускаем приватные каналы
                if is_frozen_join and channel.startswith('+'):
                    continue
                
                # Проверяем, не забанен ли аккаунт в этом канале
                if self.db.is_banned(self.account_id, channel):
                    continue
                
                # Проверяем, вступили ли уже
                is_joined = False
                
                # Проверка по channel_id
                if channel_id and str(channel_id) in my_dialogs:
                    is_joined = True
                    self._joined_channels.add(channel)  # Добавляем в кэш
                
                # Проверка по username (для публичных каналов)
                if not is_joined and not channel.startswith('+'):
                    normalized = channel.lstrip('@').lower()
                    if normalized in my_dialogs:
                        is_joined = True
                        self._joined_channels.add(channel)  # Добавляем в кэш
                
                if not is_joined:
                    channel_to_join = ch
                    break
            
            if not channel_to_join:
                # Все каналы уже присоединены или забанены
                return
            
            channel = channel_to_join['channel']
            title = channel_to_join.get('title', channel)
            
            # Проверяем warmup_mode для увеличенных задержек
            warmup_mode = self.db.get_setting("warmup_mode", False)
            base_delay = random.randint(30, 60)
            if warmup_mode:
                base_delay = int(base_delay * 2.5)  # 75-150 сек в warmup режиме
            
            self.log(f"📥 Постепенное вступление: {title}")
            
            # Вступаем в канал
            try:
                if channel.startswith('+'):
                    # Приватный канал
                    invite_hash = channel[1:]
                    try:
                        result = await self.client(ImportChatInviteRequest(invite_hash))
                        entity = result.chats[0] if result.chats else None
                        
                        if entity:
                            # Сохраняем channel_id и title
                            self.db.update_channel_info(channel, channel_id=entity.id, title=getattr(entity, 'title', None))
                            
                            # Вступаем в группу обсуждений
                            try:
                                full = await self.client(GetFullChannelRequest(entity))
                                if full.full_chat.linked_chat_id:
                                    await self._join_discussion_group_if_needed(
                                        full.full_chat.linked_chat_id, 
                                        getattr(entity, 'title', channel)
                                    )
                            except:
                                pass
                            
                            self._joined_channels.add(channel)  # Добавляем в кэш
                            self.log(f"✅ Вступил в приватный канал: {getattr(entity, 'title', channel)}")
                    
                    except UserAlreadyParticipantError:
                        self._joined_channels.add(channel)  # Уже в канале - добавляем в кэш
                        self.log(f"📌 Уже в канале: {title}")
                    except InviteHashExpiredError:
                        self.log(f"❌ Ссылка истекла: {title}", "warning")
                        self.db.mark_banned(self.account_id, channel)
                    except ChannelPrivateError:
                        self.log(f"🔒 Канал приватный/недоступен: {title}", "warning")
                        self.db.mark_banned(self.account_id, channel)
                    except FloodWaitError as e:
                        self.log(f"⏳ FloodWait {e.seconds} сек при вступлении", "warning")
                        self.health_monitor.record_error(self.account_id, e)
                    except Exception as e:
                        err_str = str(e).upper()
                        if 'FROZEN' in err_str:
                            self.log(f"🥶 Аккаунт заморожен для вступлений! Пропускаю приватные каналы.", "warning")
                            self._update_account_status("frozen_join")
                            return  # Прекращаем попытки вступления
                        raise  # Пробрасываем другие ошибки
                else:
                    # Публичный канал
                    try:
                        entity = await self.client.get_entity(channel)
                        await self.client(JoinChannelRequest(entity))
                        
                        # Вступаем в группу обсуждений
                        try:
                            full = await self.client(GetFullChannelRequest(entity))
                            if full.full_chat.linked_chat_id:
                                await self._join_discussion_group_if_needed(
                                    full.full_chat.linked_chat_id,
                                    getattr(entity, 'title', channel)
                                )
                        except:
                            pass
                        
                        self._joined_channels.add(channel)  # Добавляем в кэш
                        self.log(f"✅ Вступил в канал: {getattr(entity, 'title', channel)}")
                    
                    except UserAlreadyParticipantError:
                        self._joined_channels.add(channel)  # Уже в канале - добавляем в кэш
                        self.log(f"📌 Уже в канале: {title}")
                    except ChannelPrivateError:
                        self.log(f"🔒 Канал приватный: {title}", "warning")
                        self.db.mark_banned(self.account_id, channel)
                    except FloodWaitError as e:
                        self.log(f"⏳ FloodWait {e.seconds} сек при вступлении", "warning")
                        self.health_monitor.record_error(self.account_id, e)
                
                # Задержка после вступления
                await asyncio.sleep(base_delay)
                
            except Exception as e:
                err_str = str(e).lower()
                if 'flood' in err_str:
                    self.log(f"⏳ Rate limit при вступлении в {title}", "warning")
                    self.health_monitor.record_error(self.account_id, e)
                elif 'banned' in err_str or 'forbidden' in err_str:
                    self.log(f"🚫 Забанен/запрещён в {title}", "warning")
                    self.db.mark_banned(self.account_id, channel)
                else:
                    self.log(f"⚠️ Ошибка вступления в {title}: {e}", "warning")
                    
        except Exception as e:
            self.log(f"⚠️ Ошибка постепенного вступления: {e}", "error")

    async def _comment_after_join(self, channel_identifier: str):
        """Комментирует последний пост после вступления в канал"""
        try:
            # Получаем entity канала
            if channel_identifier.startswith('+'):
                # Приватный канал - нужно найти его в диалогах
                await asyncio.sleep(2)  # Даём время на обновление диалогов
                async for dialog in self.client.iter_dialogs(limit=20):
                    if dialog.is_channel:
                        channel = dialog.entity
                        break
                else:
                    return
            else:
                channel = await self.client.get_entity(channel_identifier)
            
            # Получаем последние посты
            messages = await self.client.get_messages(channel, limit=3)
            if not messages:
                return
            
            for post in messages:
                # Проверяем возраст поста
                now = datetime.now(timezone.utc)
                if post.date:
                    # Приводим post.date к aware datetime если нужно
                    post_date = post.date
                    if post_date.tzinfo is None:
                        post_date = post_date.replace(tzinfo=timezone.utc)
                    post_age = (now - post_date).total_seconds() / 3600
                    max_age = self.db.get_setting("max_post_age_hours", Config.COMMENT_MAX_POST_AGE_HOURS)
                    if post_age > max_age:
                        continue
                
                # Проверяем, не обработан ли пост
                channel_name = getattr(channel, 'username', None) or str(channel.id)
                if self.db.is_post_processed(self.account_id, channel_name, post.id):
                    continue
                
                # Проверяем комментарии
                try:
                    discussion = await self.client(GetDiscussionMessageRequest(peer=channel, msg_id=post.id))
                except Exception as e:
                    err_str = str(e).lower()
                    # Пробуем вступить в группу обсуждений
                    if "join" in err_str or "discussion" in err_str or "not a member" in err_str:
                        try:
                            full_channel = await self.client(GetFullChannelRequest(channel))
                            if full_channel.full_chat.linked_chat_id:
                                joined = await self._join_discussion_group_if_needed(
                                    full_channel.full_chat.linked_chat_id, channel_name
                                )
                                if joined:
                                    await asyncio.sleep(2)
                                else:
                                    continue
                            else:
                                continue
                        except:
                            continue
                    else:
                        continue
                
                # Скачиваем изображение если есть
                image_bytes = None
                if post.photo and Config.SUPPORT_IMAGES:
                    try:
                        image_bytes = await self.client.download_media(post.photo, bytes)
                    except:
                        pass
                
                # Генерируем и отправляем комментарий (ASYNC)
                comment = await self.comment_generator.generate_comment_async(post.raw_text or "", image_bytes)
                
                # Проверка на пустой комментарий
                if not comment or not comment.strip():
                    continue
                
                await asyncio.sleep(random.randint(5, 15))
                
                result = await self.client.send_message(entity=channel, message=comment, comment_to=post.id)
                self.db.mark_post_processed(self.account_id, channel_name, post.id)
                self.db.increment_stat(self.account_id, 'comments')
                self.db.save_comment(self.account_id, channel_name, comment, post.id, result.id)
                
                self.log(f"✅ Комментарий после вступления в {channel_identifier}")
                break  # Один комментарий достаточно
                
        except Exception as e:
            self.log(f"⚠️ Не удалось прокомментировать после вступления: {e}", "warning")

    def _setup_antispam_handler(self):
        """Настраивает обработчик для автоматического нажатия антиспам-кнопок"""
        
        # Ключевые слова для определения антиспам-сообщений
        antispam_keywords = [
            'не бот', 'не робот', 'not a bot', 'not bot', 'human', 'человек',
            'подтвердить', 'confirm', 'verify', 'верификация', 'verification',
            'нажмите кнопку', 'нажми кнопку', 'click', 'press', 'tap',
            'капча', 'captcha', 'проверка', 'check',
            'доступ', 'access', 'разблокировать', 'unlock',
            'чтобы иметь возможность писать', 'чтобы писать', 'to write', 'to chat',
            'в течение', 'within'
        ]
        
        # Ключевые слова на кнопках которые нужно нажимать
        button_keywords = [
            'я не бот', 'i am not a bot', 'не бот', 'not bot', 'human',
            'подтвердить', 'confirm', 'verify', 'да', 'yes', 'ок', 'ok',
            'продолжить', 'continue', 'далее', 'next', 'войти', 'enter',
            'разблокировать', 'unlock', 'получить доступ', 'get access',
            'вступить', 'join', 'принять', 'accept', '✅', '👤', '🔓',
            'я человек', "i'm human", 'start', 'начать'
        ]
        
        # Известные антиспам-боты (проверяем по имени отправителя)
        antispam_bot_names = [
            'rose', 'combot', 'shieldy', 'captcha', 'guard', 'gatekeeper',
            'verificator', 'антиспам', 'antispam', 'welcome', 'greeter',
            'join', 'entry', 'doorman', 'bouncer', 'protector'
        ]
        
        @self.client.on(events.NewMessage(incoming=True))
        async def antispam_handler(event):
            """Обрабатывает сообщения с антиспам-кнопками"""
            try:
                message = event.message
                
                # Проверяем только сообщения в группах/каналах
                if event.is_private:
                    return
                
                # Проверяем наличие inline-кнопок
                if not message.reply_markup:
                    return
                
                # Проверяем текст сообщения на антиспам-ключевые слова
                msg_text = (message.text or message.message or '').lower()
                is_antispam_msg = any(kw in msg_text for kw in antispam_keywords)
                
                # Также проверяем имя отправителя на известные антиспам-боты
                sender = await event.get_sender()
                sender_name = ''
                if sender:
                    sender_name = (getattr(sender, 'first_name', '') or '').lower()
                    sender_username = (getattr(sender, 'username', '') or '').lower()
                    is_known_bot = any(bn in sender_name or bn in sender_username for bn in antispam_bot_names)
                    
                    # Если это известный антиспам-бот, считаем сообщение антиспамом
                    if is_known_bot:
                        is_antispam_msg = True
                
                if not is_antispam_msg:
                    return
                
                # Ищем подходящую кнопку для нажатия
                for row in message.reply_markup.rows:
                    for button in row.buttons:
                        if not isinstance(button, KeyboardButtonCallback):
                            continue
                        
                        button_text = (button.text or '').lower()
                        
                        # Проверяем текст кнопки
                        if any(kw in button_text for kw in button_keywords):
                            # Небольшая задержка перед нажатием (имитация человека)
                            await asyncio.sleep(random.uniform(1.5, 4.0))
                            
                            try:
                                # Нажимаем кнопку
                                await self.client(GetBotCallbackAnswerRequest(
                                    peer=event.chat_id,
                                    msg_id=message.id,
                                    data=button.data
                                ))
                                
                                chat_name = getattr(event.chat, 'title', str(event.chat_id))
                                self.log(f"🤖 Антиспам: нажал '{button.text}' в {chat_name}")
                                return  # Нажали одну кнопку - выходим
                                
                            except Exception as btn_err:
                                # Игнорируем ошибки нажатия (возможно уже нажато)
                                if 'timeout' not in str(btn_err).lower():
                                    pass
                                return
                
            except Exception as e:
                # Тихо игнорируем ошибки чтобы не спамить логи
                pass
        
        self.log("🛡️ Обработчик антиспам-кнопок активирован")

    async def _sync_user_subscriptions(self):
        """Проверяет новые ручные подписки пользователя и добавляет их в базу"""
        # Проверяем что клиент подключен
        if not self.is_running or not self.client or not self.client.is_connected():
            return
        
        # Ключевые слова для фильтрации каналов (аниме тематика)
        anime_keywords = [
            'anime', 'аниме', 'manga', 'манга', 'манхва', 'manhwa', 'webtoon',
            'otaku', 'отаку', 'waifu', 'вайфу', 'хентай', 'hentai', 'ecchi',
            'naruto', 'наруто', 'one piece', 'ван пис', 'bleach', 'блич',
            'attack on titan', 'атака титанов', 'demon slayer', 'клинок',
            'jojo', 'джоджо', 'dragon ball', 'драгон болл', 'genshin',
            'chainsaw', 'бензопила', 'jujutsu', 'магическ', 'hunter',
            'хантер', 'boku no hero', 'hero academia', 'геройская',
            'tokyo ghoul', 'токийский гуль', 'death note', 'тетрадь смерти',
            'fullmetal', 'стальной алхимик', 'sword art', 'мастер меча',
            'spy x family', 'семья шпиона', 'bocchi', 'frieren', 'фрирен',
            'oshi no ko', 'звездное дитя', 'blue lock', 'блю лок',
            'dandadan', 'дандадан', 'kaiju', 'кайдзю', 'solo leveling',
            'поднятие уровня', 'mushoku', 'безработн', 'konosuba', 'коносуба',
            'overlord', 'оверлорд', 're:zero', 'ре зеро', 'slime', 'слайм',
            'isekai', 'исекай', 'shonen', 'сёнен', 'seinen', 'сэйнен',
            'cosplay', 'косплей', 'japan', 'япон', 'kawaii', 'каваи',
            'senpai', 'сенпай', 'chan', 'чан', 'kun', 'кун', 'sama', 'сама',
            'neko', 'неко', 'catgirl', 'кошкодевочка', 'kemono', 'кемоно',
            'yaoi', 'яой', 'yuri', 'юри', 'shoujo', 'сёдзё', 'josei',
            'light novel', 'ранобэ', 'ranobe', 'visual novel', 'новелла',
            'dorama', 'дорама', 'k-pop', 'кпоп', 'j-pop', 'джей поп',
            'аниме арт', 'anime art', 'wallpaper', 'обои'
        ]
            
        try:
            from telethon.tl.types import Channel
            checked = 0
            added = 0
            
            async for dialog in self.client.iter_dialogs():
                if not self.is_running: break
                # Проверяем подключение в цикле
                if not self.client.is_connected():
                    break
                if isinstance(dialog.entity, Channel):
                    # Проверяем что это именно канал (broadcast), а не группа
                    if not getattr(dialog.entity, 'broadcast', False):
                        continue
                    
                    # Для приватных каналов используем ID
                    if dialog.entity.username:
                        username = dialog.entity.username.lstrip('@')
                        # Исключаем ботов (username заканчивается на 'bot')
                        if username.lower().endswith('bot'):
                            continue
                        channel_identifier = f"@{username}"
                    else:
                        # Приватный канал без username - пропускаем в синхронизации
                        # Они добавляются через channels_to_join.txt с оригинальной ссылкой
                        continue
                    
                    if not username: continue
                    if username.lower().endswith('bot'):
                        continue
                    
                    # Фильтр по ключевым словам (аниме тематика)
                    channel_title = (dialog.entity.title or '').lower()
                    channel_username = username.lower()
                    
                    is_anime_related = any(
                        kw in channel_title or kw in channel_username 
                        for kw in anime_keywords
                    )
                    
                    if not is_anime_related:
                        continue  # Пропускаем каналы не связанные с аниме
                    
                    checked += 1
                    
                    # Если канала нет в нашем активном списке
                    if channel_identifier not in self.active_channels:
                        # Проверяем, не был ли канал удалён пользователем
                        if self.db.is_channel_deleted(username):
                            continue  # Пропускаем удалённые каналы
                        
                        # Проверка возможностей
                        try:
                            can_comment = False
                            msgs = await self.client.get_messages(dialog.entity, limit=5)
                            for m in msgs:
                                try:
                                    await self.client(GetDiscussionMessageRequest(peer=dialog.entity, msg_id=m.id))
                                    can_comment = True
                                    break
                                except: continue
                            
                            # Получаем минимальное количество подписчиков для каналов из настроек
                            min_channel_subs = self.db.get_setting("min_channel_subs", 20000)
                            subs_count = getattr(dialog.entity, 'participants_count', 0)
                            
                            # Сохраняем результат (с фильтрацией по подписчикам)
                            is_new = self.db.add_found_channel(
                                channel=username,
                                title=dialog.entity.title,
                                source="manual_join",
                                subs=subs_count,
                                can_comment=can_comment,
                                min_subs=min_channel_subs
                            )
                            
                            # Пропускаем маленькие каналы (только если известно кол-во сабов)
                            # Для приватных каналов subs_count может быть None
                            if subs_count is not None and subs_count > 0 and subs_count < min_channel_subs:
                                continue
                            
                            if can_comment:
                                self.log(f"✅ Найден аниме-канал: {channel_identifier} ({subs_count} подписчиков)")
                                self.active_channels.append(channel_identifier)
                                # Обновляем статус на active явно
                                self.db.update_channel_status(username, 'active')
                                added += 1
                            else:
                                # self.log(f"ℹ️ {channel_identifier} - комменты закрыты")
                                pass
                                
                        except Exception as e:
                            pass
            
            if added > 0:
                self.log(f"📥 Синхронизация: добавлено {added} аниме-каналов из ваших подписок")

        except Exception as e:
            self.log(f"❌ Ошибка синхронизации подписок: {e}", "error")

    async def _like_random_comments(self, channel, post_id: int):
        """Лайкает случайные комментарии под постом для имитации активности"""
        try:
            # Получаем комментарии к посту
            discussion = await self.client(GetDiscussionMessageRequest(peer=channel, msg_id=post_id))
            if not discussion or not discussion.messages:
                return
            
            # Получаем настройку количества лайков
            max_likes = self.db.get_setting("likes_per_post", 3)
            
            # Фильтруем только чужие комментарии (не свои)
            me = await self.client.get_me()
            other_comments = [m for m in discussion.messages[:15] if m.from_id and getattr(m.from_id, 'user_id', None) != me.id]
            
            if not other_comments:
                return
            
            # Выбираем случайные комментарии для лайка
            num_to_like = min(random.randint(1, max_likes), len(other_comments))
            comments_to_like = random.sample(other_comments, num_to_like)
            
            liked_count = 0
            discussion_chat = discussion.chats[0] if discussion.chats else None
            if not discussion_chat:
                return
                
            for comment in comments_to_like:
                try:
                    # Ставим реакцию без логирования (залогируем сами)
                    success = await self.reaction_manager.send_reaction(discussion_chat, comment.id, log_action=False)
                    if success:
                        liked_count += 1
                    await asyncio.sleep(random.uniform(1, 3))
                except Exception:
                    pass
            
            if liked_count > 0:
                channel_name = getattr(channel, 'username', None) or getattr(channel, 'title', str(channel.id))
                self.log(f"👍 Лайкнул {liked_count} комментов в @{channel_name}")
                
        except Exception as e:
            # Не критичная ошибка, просто пропускаем
            pass

    async def _extract_links_from_post(self, post, source_channel: str):
        """Извлекает ссылки на каналы из поста и добавляет в базу"""
        import re
        tg_link_pattern = re.compile(r'(?:t\.me/|@)([a-zA-Z0-9_]{5,32})')
        # Паттерн для приватных инвайт-ссылок (t.me/+hash или t.me/joinchat/hash)
        invite_pattern = re.compile(r't\.me/(?:\+|joinchat/)([a-zA-Z0-9_-]+)')
        stop_words = {'telegram', 'joinchat', 'addstickers', 'bot', 'share', 'proxy', 'socks', 'vote'}
        found_links = set()
        found_invites = set()
        
        try:
            # Парсим текст поста
            if post.text:
                matches = tg_link_pattern.findall(post.text)
                for m in matches:
                    if m.lower() not in stop_words:
                        found_links.add(m)
                # Ищем приватные инвайт-ссылки
                invite_matches = invite_pattern.findall(post.text)
                for m in invite_matches:
                    found_invites.add(f"+{m}")
            
            # Парсим кнопки (реклама часто в кнопках)
            if post.reply_markup:
                try:
                    for row in post.reply_markup.rows:
                        for button in row.buttons:
                            if hasattr(button, 'url') and button.url:
                                url_matches = tg_link_pattern.findall(button.url)
                                for m in url_matches:
                                    if m.lower() not in stop_words:
                                        found_links.add(m)
                                # Ищем приватные инвайт-ссылки в кнопках
                                invite_matches = invite_pattern.findall(button.url)
                                for m in invite_matches:
                                    found_invites.add(f"+{m}")
                except: pass
            
            # Парсим entities (ссылки в тексте)
            if post.entities:
                for ent in post.entities:
                    if hasattr(ent, 'url') and ent.url:
                        url_matches = tg_link_pattern.findall(ent.url)
                        for m in url_matches:
                            if m.lower() not in stop_words:
                                found_links.add(m)
                        # Ищем приватные инвайт-ссылки
                        invite_matches = invite_pattern.findall(ent.url)
                        for m in invite_matches:
                            found_invites.add(f"+{m}")
            
            # Обрабатываем приватные инвайт-ссылки
            for invite_hash in found_invites:
                try:
                    # Проверяем, не обработан ли уже
                    if self.db.is_invite_processed(self.account_id, invite_hash):
                        continue
                    
                    # Пробуем вступить и проверить комменты
                    try:
                        hash_clean = invite_hash.lstrip('+')
                        result = await self.client(ImportChatInviteRequest(hash_clean))
                        chat = result.chats[0] if result.chats else None
                        
                        if chat:
                            # Проверяем комментарии
                            can_comment = False
                            try:
                                msgs = await self.client.get_messages(chat, limit=3)
                                for m in msgs:
                                    try:
                                        await self.client(GetDiscussionMessageRequest(peer=chat, msg_id=m.id))
                                        can_comment = True
                                        break
                                    except:
                                        continue
                            except:
                                pass
                            
                            if can_comment:
                                # Добавляем в БД
                                self.db.add_found_channel(
                                    channel=invite_hash,
                                    title=chat.title,
                                    keyword="link",
                                    source="link",
                                    can_comment=True,
                                    min_subs=0
                                )
                                self.db.mark_invite_processed(self.account_id, invite_hash, channel_id=chat.id)
                                self.log(f"🔗 Найден приватный канал с комментами: {invite_hash}")
                    
                    except UserAlreadyParticipantError:
                        # Уже состоим - проверяем комменты
                        pass
                    except:
                        continue
                    
                    await asyncio.sleep(2)
                except:
                    continue
            
            # Добавляем найденные публичные каналы в базу
            min_channel_subs = self.db.get_setting("min_channel_subs", 20000)
            
            for link in found_links:
                try:
                    # Исключаем ботов
                    if link.lower().endswith('bot'):
                        continue
                    
                    # Проверяем что это канал
                    from telethon.tl.types import Channel
                    peer = await self.client.get_entity(link)
                    if not isinstance(peer, Channel):
                        continue
                    
                    # Проверяем что это broadcast канал, а не группа
                    if not getattr(peer, 'broadcast', False):
                        continue
                    
                    subs_count = getattr(peer, 'participants_count', 0)
                    
                    # Пропускаем маленькие каналы (только если известно кол-во сабов)
                    # Для приватных каналов subs_count может быть None
                    if subs_count is not None and subs_count > 0 and subs_count < min_channel_subs:
                        continue
                    
                    # Фильтр по ключевым словам (аниме тематика)
                    anime_keywords = [
                        'anime', 'аниме', 'manga', 'манга', 'манхва', 'manhwa',
                        'otaku', 'отаку', 'waifu', 'naruto', 'наруто', 'one piece',
                        'bleach', 'attack on titan', 'demon slayer', 'клинок',
                        'jojo', 'dragon ball', 'genshin', 'chainsaw', 'бензопила',
                        'jujutsu', 'hunter', 'хантер', 'hero academia', 'геройская',
                        'tokyo ghoul', 'death note', 'fullmetal', 'sword art',
                        'spy x family', 'frieren', 'фрирен', 'blue lock', 'dandadan',
                        'solo leveling', 'mushoku', 'konosuba', 'overlord', 're:zero',
                        'isekai', 'исекай', 'cosplay', 'косплей', 'japan', 'япон',
                        'kawaii', 'neko', 'неко', 'yaoi', 'яой', 'yuri', 'юри',
                        'ranobe', 'ранобэ', 'novel', 'новелла', 'dorama', 'дорама',
                        'wallpaper', 'обои', 'art', 'арт'
                    ]
                    
                    channel_title = (getattr(peer, 'title', '') or '').lower()
                    channel_username = link.lower()
                    
                    is_anime_related = any(
                        kw in channel_title or kw in channel_username 
                        for kw in anime_keywords
                    )
                    
                    if not is_anime_related:
                        continue  # Пропускаем каналы не связанные с аниме
                    
                    # Проверяем, не был ли канал удалён пользователем
                    if self.db.is_channel_deleted(link.lstrip('@')):
                        continue  # Пропускаем удалённые каналы
                    
                    # Проверяем комментарии
                    can_comment = False
                    try:
                        await self.client(GetDiscussionMessageRequest(peer=peer, msg_id=post.id))
                        can_comment = True
                    except:
                        pass
                    
                    # Добавляем в базу
                    is_new = self.db.add_found_channel(
                        channel=link.lstrip('@'),
                        title=getattr(peer, 'title', link),
                        source=f"ad_from_{source_channel}",
                        subs=subs_count,
                        can_comment=can_comment,
                        min_subs=min_channel_subs
                    )
                    
                    if can_comment and is_new:
                        self.log(f"🔗 Найден канал из рекламы: @{link} ({subs_count} подписчиков)")
                    
                    await asyncio.sleep(1)  # Небольшая задержка
                except:
                    continue
                    
        except Exception as e:
            pass  # Не критичная ошибка

    # ==================================================================
    # === Helpers для трёх тоглов поведения =============================
    # ==================================================================

    async def _safe_leave_discussion_group(self, channel, channel_name: str):
        """
        Покидает дискуссионную группу канала, в которой нет прав писать.
        Используется при ChatWriteForbiddenError, если включён leave_if_no_write.
        Не выходим из самого канала-источника, только из группы обсуждений.
        """
        try:
            full = await self.client(GetFullChannelRequest(channel))
            linked_chat_id = getattr(full.full_chat, "linked_chat_id", None)
            if not linked_chat_id:
                return
            try:
                linked = await self.client.get_entity(linked_chat_id)
                await self.client(LeaveChannelRequest(linked))
                self.log(f"🚪 Вышел из группы обсуждений {channel_name} (нет прав писать)")
                # убираем из локального кэша вступленных
                self._joined_discussion_groups.discard(linked_chat_id)
            except Exception as e:
                self.log(f"⚠️ Не удалось выйти из группы обсуждений {channel_name}: {e}", "warning")
        except Exception as e:
            # Не падаем — это второстепенное действие
            self.log(f"⚠️ leave_discussion_group: {e}", "warning")

    async def _consult_spambot(self, reason: str = ""):
        """
        Идёт в @SpamBot и проверяет статус аккаунта.
        Используется при PeerFloodError, если включён spambot_unblock.
        Применяется cooldown 10 минут, чтобы не спамить @SpamBot.
        """
        now = time.time()
        if now - self._last_spambot_check < self._spambot_cooldown_seconds:
            self.log("⏳ @SpamBot уже опрашивался недавно, пропускаю", "warning")
            return
        self._last_spambot_check = now

        if self.spambot_checker is None:
            self.spambot_checker = SpamBotChecker(self.client)

        self.log(f"🤖 Иду в @SpamBot ({reason})...")
        try:
            res = await self.spambot_checker.check(press_buttons=True, wait_seconds=4.0)
        except Exception as e:
            self.log(f"⚠️ Ошибка @SpamBot: {e}", "warning")
            return

        # Логируем итог + обновляем статус аккаунта при необходимости
        msg_short = res.message[:120] if res.message else ""
        if res.status == "ok":
            self.log(f"✅ @SpamBot: всё чисто. {msg_short}")
        elif res.status == "unblocked":
            self.log(f"✅ @SpamBot снял блок. {msg_short}")
        elif res.status == "limited":
            unblock = (res.will_unblock_at.isoformat()
                       if res.will_unblock_at else "неизвестно")
            self.log(
                f"⚠️ @SpamBot: спам-блок активен (до {unblock}). {msg_short}",
                "warning",
            )
            self._update_account_status("spamblock")
        elif res.status == "blocked":
            self.log(f"🚫 @SpamBot: аккаунт заблокирован. {msg_short}", "error")
            self._update_account_status("spamblock")
            # Снимаем с работы — нет смысла продолжать
            self.is_running = False
        else:
            self.log(f"❓ @SpamBot: неопознанный статус. {msg_short}", "warning")

    async def _maybe_leave_junk_chats(self):
        """
        Раз в N циклов проходит по диалогам, классифицирует их через
        JunkChatClassifier и выходит из мусорных. Покидает не более
        _junk_leave_per_scan чатов за один проход (анти-флуд Telegram).
        """
        # Минимум 1 час между сканами
        now = time.time()
        if now - self._last_junk_scan < 3600:
            return
        self._last_junk_scan = now

        if self.junk_classifier is None:
            self.junk_classifier = JunkChatClassifier(db=self.db)

        # сбрасываем LLM-бюджет на этот проход
        self.junk_classifier.reset_llm_budget(max_calls=10)

        try:
            dialogs = await self.client.get_dialogs(limit=200)
        except Exception as e:
            self.log(f"⚠️ Не удалось получить диалоги для junk-сканирования: {e}", "warning")
            return

        left_count = 0
        checked_count = 0

        for dialog in dialogs:
            if not self.is_running:
                break
            if left_count >= self._junk_leave_per_scan:
                break

            # Только группы и мегагруппы (НЕ broadcast-каналы!)
            entity = dialog.entity
            is_group = (getattr(entity, "megagroup", False)
                        or getattr(entity, "gigagroup", False)
                        or hasattr(entity, "participants_count"))
            if not is_group or getattr(entity, "broadcast", False):
                continue
            # Не трогаем личные диалоги
            if dialog.is_user:
                continue

            chat_id = entity.id
            # уже принимали решение — пропускаем
            if chat_id in self._junk_chat_decisions:
                continue

            title = getattr(entity, "title", "") or ""
            username = getattr(entity, "username", "") or ""
            members = getattr(entity, "participants_count", 0) or 0
            about = ""
            try:
                full = await self.client(GetFullChannelRequest(entity))
                about = (getattr(full.full_chat, "about", "") or "")[:500]
                # Если в full есть свежее число участников — уточняем
                fresh_members = getattr(full.full_chat, "participants_count", None)
                if fresh_members:
                    members = fresh_members
            except Exception:
                # Если full не получили — классифицируем по тому что есть
                pass

            checked_count += 1

            try:
                verdict = await self.junk_classifier.classify(
                    title=title, about=about, members_count=members, username=username,
                )
            except Exception as e:
                self.log(f"⚠️ junk classify '{title}': {e}", "warning")
                continue

            self._junk_chat_decisions[chat_id] = verdict.is_junk

            if verdict.is_junk and verdict.confidence >= 0.6:
                try:
                    await self.client(LeaveChannelRequest(entity))
                    left_count += 1
                    self.log(
                        f"🗑️ Вышел из мусорного чата '{title}' "
                        f"(reason: {verdict.reason}, conf: {verdict.confidence:.2f})"
                    )
                    # Анти-флуд: па��за между LeaveChannelRequest
                    await asyncio.sleep(random.uniform(8.0, 15.0))
                except FloodWaitError as fw:
                    self.log(f"⏳ FloodWait при отписке: {fw.seconds}с — стопим скан", "warning")
                    break
                except Exception as e:
                    self.log(f"⚠️ Не удалось выйти из '{title}': {e}", "warning")
            # throttle между классификациями
            await asyncio.sleep(0.5)

        if checked_count > 0:
            self.log(
                f"🧹 junk-scan: проверено {checked_count}, покинуто {left_count}"
            )

    # ==================================================================

    def run_task(self, coro):
        """Запускает корутину в цикле воркера (thread-safe)"""
        if self.is_running and hasattr(self, 'loop') and self.loop:
            try:
                future = asyncio.run_coroutine_threadsafe(coro, self.loop)
                # Отслеживаем pending tasks для отмены при остановке
                self._pending_tasks.append(future)
                return future
            except Exception as e:
                print(f"[run_task] Ошибка при запуске задачи: {e}")
                return None
        return None

    def _cancel_pending_tasks(self):
        """Отменяет все pending tasks"""
        cancelled = 0
        for future in self._pending_tasks:
            if not future.done():
                future.cancel()
                cancelled += 1
        self._pending_tasks.clear()
        if cancelled > 0:
            self.log(f"🛑 Отменено {cancelled} pending tasks")

    async def _graceful_shutdown(self):
        """Корректное завершение всех ресурсов"""
        self.log("🔄 Начинаю graceful shutdown...")
        
        # Останавливаем channel_explorer если запущен
        if self.channel_explorer:
            self.channel_explorer.stop()
            self.log("🛑 ChannelExplorer остановлен")
        
        # Останавливаем keyword_search если запущен
        if self.keyword_search:
            self.keyword_search.stop()
            self.log("🛑 KeywordSearch остановлен")
        
        # Закрываем HTTP клиент
        try:
            await close_http_client()
        except Exception as e:
            self.log(f"⚠️ Ошибка закрытия HTTP клиента: {e}", "warning")
        
        # Закрываем junk classifier (его собственный HTTP клиент)
        if self.junk_classifier:
            try:
                await self.junk_classifier.close()
            except Exception:
                pass
        
        # Отключаем Telegram клиент
        if self.client:
            try:
                await self.client.disconnect()
                self.log("✅ Telegram клиент отключен")
            except Exception as e:
                self.log(f"⚠️ Ошибка отключения Telegram: {e}", "warning")

    def stop(self):
        """Останавливает воркер с graceful shutdown"""
        global _shutdown_requested
        
        self.log("🛑 Останавливаю воркер...")
        self.is_running = False
        _shutdown_requested = True
        
        # Отменяем все pending tasks
        self._cancel_pending_tasks()
        
        # Останавливаем channel_explorer если запущен
        if self.channel_explorer:
            self.channel_explorer.stop()
        
        # Останавливаем keyword_search если запущен
        if self.keyword_search:
            self.keyword_search.stop()
        
        # Cleanup new module HTTP clients
        if self.channel_creator:
            try:
                if hasattr(self, 'loop') and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.channel_creator.close(), self.loop).result(timeout=5)
            except Exception:
                pass
        if self.channel_poster:
            try:
                if hasattr(self, 'loop') and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.channel_poster.close(), self.loop).result(timeout=5)
            except Exception:
                pass
        
        if self.client:
            try:
                # Пытаемся отключиться корректно через сохраненный loop
                if hasattr(self, 'loop') and self.loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._graceful_shutdown(), 
                        self.loop
                    )
                    # Ждем завершения отключения
                    try:
                        future.result(timeout=10)
                    except Exception as e:
                        self.log(f"⚠️ Timeout при shutdown: {e}", "warning")
                else:
                    # Синхронное отключение
                    try:
                        self.client.disconnect()
                    except:
                        pass
            except Exception as e:
                self.log(f"⚠️ Ошибка при остановке: {e}", "warning")
        
        self.log("🛑 Воркер остановлен")

