from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
from concurrent.futures import TimeoutError as FutureTimeoutError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Union
import os
import sys
import shutil
import io
import base64
import asyncio
import threading
import time
import uuid

# Добавляем корневую директорию в путь для импорта
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from utils.database import Database
from utils.validator import InputValidator
from utils.telegram_targets import normalize_targets, preview_targets
from config import Config
import logging

# Импорты для работы с профилем Telegram
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.tl.functions.account import UpdateUsernameRequest, UpdateProfileRequest

# Отключаем спам от polling запросов в консоли
class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        noisy_endpoints = ['/logs', '/accounts', '/stats', '/settings', '/proxies', '/discovery', '/comments', '/health']
        return not any(ep in msg and '200' in msg for ep in noisy_endpoints)

logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

# FIX: импорты были битые (dashboard.backend.* вместо backend.*)
from backend.auth_manager import AuthManager
from backend.worker import BotWorker, set_global_pause as worker_set_global_pause
from modules.channel_explorer import ChannelExplorer
from modules.channel_health_watcher import ChannelHealthWatcher

# Корни проекта
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STATIC_DIR = os.path.join(ROOT_DIR, 'static')

# Serverless (Vercel) detection.
# На Vercel файловая система только для чтения, писать можно лишь в /tmp,
# а фоновые воркеры/Telethon-сессии там всё равно не живут (функция короткоживущая).
# Поэтому на serverless: (1) все данные пишем в /tmp, (2) не стартуем воркеры.
IS_SERVERLESS = bool(os.getenv('VERCEL') or os.getenv('AWS_LAMBDA_FUNCTION_NAME'))

# Базовая директория для данных (БД + сессии).
# DATA_DIR env > /tmp на serverless > <project>/data локально.
_data_dir_env = os.environ.get('DATA_DIR')
if _data_dir_env:
    DATA_DIR = _data_dir_env
elif IS_SERVERLESS:
    DATA_DIR = '/tmp/neuro-commenting'
else:
    DATA_DIR = os.path.join(ROOT_DIR, 'data')

# Путь к SQLite-базе (singleton Database инициализируется этим путём первым).
DATABASE_PATH = os.path.join(DATA_DIR, 'bot.db')

# Определяем директорию сессий: SESSIONS_DIR env > DATA_DIR/sessions > sessions/ (legacy)
_sessions_dir_env = os.environ.get('SESSIONS_DIR')
if _sessions_dir_env:
    SESSIONS_DIR = _sessions_dir_env
elif IS_SERVERLESS:
    SESSIONS_DIR = os.path.join(DATA_DIR, 'sessions')
else:
    _data_sessions = os.path.join(DATA_DIR, 'sessions')
    _legacy_sessions = os.path.join(ROOT_DIR, 'sessions')
    if os.path.exists(_data_sessions) or not (os.path.exists(_legacy_sessions) and os.listdir(_legacy_sessions)):
        SESSIONS_DIR = _data_sessions
    else:
        SESSIONS_DIR = _legacy_sessions

os.makedirs(SESSIONS_DIR, exist_ok=True)

# Пробрасываем итоговый путь воркерам через окружение, чтобы main и worker
# всегда использовали ОДНУ И ТУ ЖЕ директорию сессий (иначе воркер ищет файл
# сессии не там, создаёт пустой и падает с "Сессия не авторизована").
os.environ['SESSIONS_DIR'] = SESSIONS_DIR

# Холдер для глобального health-watcher
health_watcher: Optional[ChannelHealthWatcher] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global health_watcher

    # На serverless (Vercel) фоновые задачи и воркеры не работают:
    # функция короткоживущая, а Telethon-сессии/потоки не переживают инстанс.
    # Поднимаем только HTTP-дашборд, ничего в фоне не запускаем.
    if IS_SERVERLESS:
        print("⚡ Serverless-режим: воркеры и health-watcher отключены (только дашборд).")
        yield
        return

    # Очистка старых логов при старте (старше 7 дней)
    deleted_logs = db.cleanup_old_logs(days=7)
    if deleted_logs > 0:
        print(f"🧹 Очищено {deleted_logs} старых записей логов")
    
    # Очистка каналов с закрытыми комментариями
    closed = db.cleanup_closed_channels()
    if closed > 0:
        print(f"🧹 Удалено {closed} каналов с закрытыми комментариями")
    
    # Запуск фонового watcher базы каналов
    health_watcher = ChannelHealthWatcher(db, workers_registry=workers)
    health_watcher.start()
    print("👁️  ChannelHealthWatcher запущен (валидация каналов в фоне)")
    
    # Авто-старт ранее активных аккаунтов (для "автономно через хост")
    if os.getenv('AUTOSTART_WORKERS', '1') == '1':
        try:
            for acc in db.get_accounts():
                if acc.get('status') == 'active':
                    _start_worker_for_account(acc)
                    print(f"🚀 Авто-старт воркера для {acc.get('phone')}")
        except Exception as e:
            print(f"[AUTOSTART] Ошибка: {e}")
    
    yield
    
    # Shutdown
    if health_watcher:
        health_watcher.stop()
    for w in list(workers.values()):
        try:
            w.stop()
        except Exception:
            pass


app = FastAPI(title="Neuro-Commenting API", lifespan=lifespan)


# --- Bearer-token middleware (опциональный, включается через DASHBOARD_TOKEN) ---
DASHBOARD_TOKEN = os.getenv('DASHBOARD_TOKEN', '').strip()
PUBLIC_PATHS = {'/', '/index.html', '/login', '/health', '/favicon.ico', '/auth-status'}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not DASHBOARD_TOKEN:
        # Auth выключен — пропускаем всё (для локальной разработки)
        return await call_next(request)
    path = request.url.path
    # Публичные пути и static — без авторизации
    if path in PUBLIC_PATHS or path.startswith('/static/') or path.startswith('/assets/'):
        return await call_next(request)
    # Пропускаем preflight CORS
    if request.method == 'OPTIONS':
        return await call_next(request)
    # Принимаем токен либо из заголовка, либо из query (?token=)
    auth_header = request.headers.get('authorization', '')
    token = ''
    if auth_header.lower().startswith('bearer '):
        token = auth_header[7:].strip()
    if not token:
        token = request.query_params.get('token', '').strip()
    if token != DASHBOARD_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


# CORS — ограничиваем (если задан DASHBOARD_ORIGIN), иначе * для разработки
allow_origins = [o.strip() for o in os.getenv('DASHBOARD_ORIGIN', '*').split(',') if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = Database(DATABASE_PATH)
db.sanitize_channels()
auth = AuthManager(sessions_dir=SESSIONS_DIR)

# Реестр активных воркеров
workers: Dict[int, BotWorker] = {}
# Защита от гонок start/stop/replace одного и того же account_id
_workers_lock = threading.RLock()
# account_id → monotonic ts когда воркер «стартует» (ещё is_running=False)
_workers_starting: Dict[int, float] = {}


def _worker_is_alive(worker: Optional[BotWorker]) -> bool:
    """True если воркер уже running или его поток ещё жив (starting/stopping)."""
    if worker is None:
        return False
    try:
        if getattr(worker, "is_running", False):
            return True
    except Exception:
        pass
    thread = getattr(worker, "thread", None)
    try:
        if thread is not None and thread.is_alive():
            return True
    except Exception:
        pass
    return False


def _wait_worker_stopped(worker: Optional[BotWorker], timeout: float = 20.0) -> bool:
    """Ждёт остановки потока воркера. Координируется с BotWorker.stop()/is_running."""
    if worker is None:
        return True
    deadline = time.monotonic() + max(0.5, float(timeout))
    thread = getattr(worker, "thread", None)
    while time.monotonic() < deadline:
        alive_thread = False
        try:
            alive_thread = bool(thread and thread.is_alive())
        except Exception:
            alive_thread = False
        running = False
        try:
            running = bool(getattr(worker, "is_running", False))
        except Exception:
            running = False
        if not alive_thread and not running:
            return True
        time.sleep(0.1)
    # final check
    try:
        if thread is not None and thread.is_alive():
            return False
    except Exception:
        return False
    return not bool(getattr(worker, "is_running", False))


def _start_worker_for_account(account: dict) -> Optional[BotWorker]:
    """
    Helper для старта воркера (автостарт и /start).
    Не заменяет живой/стартующий воркер и не создаёт второй session на тот же id.
    """
    acc_id = account['id']
    with _workers_lock:
        existing = workers.get(acc_id)
        if _worker_is_alive(existing):
            print(f"[workers] skip start acc={acc_id}: already live/starting")
            return existing
        # stale registry entry (stopped) — drop before replace
        if existing is not None:
            try:
                if not _worker_is_alive(existing):
                    workers.pop(acc_id, None)
            except Exception:
                workers.pop(acc_id, None)

        if account.get('ip'):
            account['proxy'] = {
                'ip': account['ip'],
                'port': account['port'],
                'proxy_user': account.get('proxy_user'),
                'proxy_pass': account.get('proxy_pass'),
                'proxy_type': account.get('proxy_type', 'http')
            }
        _workers_starting[acc_id] = time.monotonic()
        worker = BotWorker(account, db)
        workers[acc_id] = worker
        try:
            worker.start()
        except Exception:
            workers.pop(acc_id, None)
            _workers_starting.pop(acc_id, None)
            raise
        return worker


def _stop_worker_for_account(acc_id: int, *, wait_timeout: float = 20.0) -> bool:
    """
    Останавливает воркер и ЖДЁТ завершения потока перед удалением из registry.
    Возвращает True если воркер был (или считался) остановлен.
    """
    with _workers_lock:
        worker = workers.get(acc_id)
        if worker is None:
            _workers_starting.pop(acc_id, None)
            return False
        try:
            worker.stop()
        except Exception as e:
            print(f"[workers] stop() error acc={acc_id}: {e}")

    # wait outside lock so stop side-effects can progress
    stopped = _wait_worker_stopped(worker, timeout=wait_timeout)

    with _workers_lock:
        current = workers.get(acc_id)
        # удаляем только тот же объект, чтобы не снести параллельно стартовавший новый
        if current is worker:
            if stopped or not _worker_is_alive(current):
                workers.pop(acc_id, None)
            else:
                # поток всё ещё жив — оставляем запись, но is_running должен быть False
                print(f"[workers] acc={acc_id}: stop wait timed out, keeping registry entry")
        _workers_starting.pop(acc_id, None)
    return True


def _account_is_paused(acc_id: int) -> bool:
    """True если аккаунт на паузе/rate-limit — mass-send/invite должны отказать."""
    try:
        if db.should_pause_account(acc_id):
            return True
        if db.get_account_pause_seconds(acc_id) > 0:
            return True
    except Exception:
        pass
    try:
        health = db.get_account_health(acc_id) or {}
        if health.get("health_status") in ("paused", "rate_limited"):
            return True
    except Exception:
        pass
    return False


def _clamp_limit(value: Optional[int], cap: int) -> int:
    cap = int(cap)
    if value is None:
        return cap
    try:
        v = int(value)
    except (TypeError, ValueError):
        return cap
    if v <= 0:
        return cap
    return min(v, cap)


def _normalize_explicit_targets(
    targets: List[Union[int, str]],
    target_type: Optional[str] = None,
):
    """Нормализует явный список и отклоняет запрос целиком при любой ошибке."""
    valid, rejected = normalize_targets(targets)
    wrong_type = []
    if target_type == "user":
        wrong_type = [item for item in valid if item.kind == "chat_id"]
    elif target_type == "group":
        wrong_type = [item for item in valid if item.kind == "user_id"]
    if wrong_type:
        rejected.extend(wrong_type)
        rejected_keys = {item.send_key for item in wrong_type}
        valid = [item for item in valid if item.send_key not in rejected_keys]
    if rejected:
        details = ", ".join(
            f"{item.original or '∅'} ({'не тот тип цели' if item in wrong_type else (item.error or item.kind)})"
            for item in rejected[:10]
        )
        raise ValueError(f"исправьте невалидные цели: {details}")
    if not valid:
        raise ValueError("список целей пуст")
    return valid


# Флаг глобальной паузы (FIX: синхронизирован с worker'ом через worker_set_global_pause)
global_pause = False


def _set_global_pause(value: bool):
    """Единая точка установки global_pause для main и worker."""
    global global_pause
    global_pause = bool(value)
    try:
        worker_set_global_pause(bool(value))
    except Exception:
        pass

# Время запуска сервера для отслеживания новых каналов
from datetime import datetime, timezone, timedelta
msk_tz = timezone(timedelta(hours=3))
SERVER_STARTUP_TIME = datetime.now(msk_tz).strftime('%Y-%m-%d %H:%M:%S')

class AccountCreate(BaseModel):
    phone: str
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    session_name: Optional[str] = None

class CodeSendRequest(BaseModel):
    phone: str
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    proxy_id: Optional[int] = None  # ID прокси для безопасной авторизации

class CodeVerifyRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str

class PasswordVerifyRequest(BaseModel):
    phone: str
    password: str

#startup_event removed and moved to lifespan

@app.get("/accounts")
async def get_accounts():
    from datetime import datetime, timezone
    
    accounts = db.get_accounts()
    now = datetime.now()  # ✅ БЕЗ timezone чтобы сравнивать с наивными датами из БД
    
    # Обогащаем данными о работе, статистикой и банами
    for acc in accounts:
        acc['is_running'] = acc['id'] in workers and workers[acc['id']].is_running
        stats = db.get_stats_summary(acc['id'])
        acc['stats'] = stats
        # Добавляем информацию о банах
        banned_channels = db.get_banned_channels_for_account(acc['id'])
        acc['banned_channels_count'] = len(banned_channels)
        acc['banned_channels'] = banned_channels[:5]  # Первые 5 для превью
        # Проверяем health status
        health = db.get_account_health(acc['id'])
        acc['health_status'] = health.get('health_status', 'unknown')
        
        # ✅ Добавляем display_name
        acc['display_name'] = db.get_account_display_name(acc['id'])
        
        # ✅ Добавляем обратный отсчёт для rate_limited
        if acc.get('rate_limited_until'):
            try:
                rate_limited_str = str(acc['rate_limited_until'])
                # Парсим дату
                if '+' in rate_limited_str or 'Z' in rate_limited_str:
                    limited_until = datetime.fromisoformat(rate_limited_str.replace('Z', '+00:00'))
                    # Переводим в наивную дату если now тоже наивна
                    if limited_until.tzinfo is not None:
                        limited_until = limited_until.replace(tzinfo=None)
                else:
                    # Наивная дата
                    limited_until = datetime.fromisoformat(rate_limited_str)
                
                remaining = max(0, int((limited_until - now).total_seconds()))
                acc['remaining_seconds'] = remaining
                if remaining > 0:
                    print(f"[get_accounts] Account {acc['id']}: remaining={remaining}s")
            except Exception as e:
                print(f"[get_accounts] ERROR: {e}")
                acc['remaining_seconds'] = 0
        else:
            acc['remaining_seconds'] = 0
        
        # ✅ Добавляем count вступлений
        acc['joined_channels_count'] = db.get_account_joined_count(acc['id'])
    
    return accounts

# ✅ НОВЫЕ ЭНДПОИНТЫ ДЛЯ 8 ФИЧ

@app.get("/accounts/{account_id}/stats-full")
async def get_account_stats_full(account_id: int):
    """Полная статистика аккаунта (все метрики в одном месте)"""
    try:
        account = db.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        display_name = db.get_account_display_name(account_id)
        
        # Общая статистика
        stats = db.get_stats_summary(account_id)
        
        # Вступления
        joined_count = db.get_account_joined_count(account_id)
        
        # Забаны
        banned_channels = db.get_banned_channels_for_account(account_id)
        banned_count = len(banned_channels)
        
        # Здоровье
        health = db.get_account_health(account_id)
        
        return {
            "account_id": account_id,
            "display_name": display_name,
            "phone": account.get('phone'),
            "comments": stats.get('comments_total', 0),
            "invites": stats.get('invites_total', 0),
            "joined_channels": joined_count,
            "banned_count": banned_count,
            "health_status": health.get('health_status', 'unknown'),
            "rate_limited_until": account.get('rate_limited_until'),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/accounts/{account_id}/joined-channels")
async def get_account_joined_channels(account_id: int):
    """Получить список каналов, в которых вступил аккаунт"""
    try:
        channels = db.get_account_joined_channels(account_id)
        count = len(channels)
        return {
            "account_id": account_id,
            "count": count,
            "channels": channels
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/discovery/chats")
async def get_found_chats(keyword: str = None):
    """Получить все найденные чаты/группы"""
    try:
        chats = db.get_found_chats(keyword=keyword, limit=500)
        return {
            "count": len(chats),
            "chats": chats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/discovery/chats/clear")
async def clear_found_chats():
    """Очистить найденные чаты"""
    try:
        db.clear_found_chats()
        return {"success": True, "message": "Найденные чаты очищены"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ✅ NEW: Smart rate limit management
@app.get("/accounts/{account_id}/rate-limit-check")
async def check_rate_limit_status(account_id: int):
    """Smart check: проверить может ли аккаунт работать, если да - запустить"""
    try:
        account = db.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        health = db.get_account_health(account_id)
        rate_limited_until = account.get('rate_limited_until')
        
        # Проверяем истекла ли пауза
        if rate_limited_until:
            from datetime import datetime, timezone
            try:
                rate_limited_str = str(rate_limited_until)
                # Парсим дату
                if '+' in rate_limited_str or 'Z' in rate_limited_str:
                    limited_until = datetime.fromisoformat(rate_limited_str.replace('Z', '+00:00'))
                    if limited_until.tzinfo is not None:
                        limited_until = limited_until.replace(tzinfo=None)
                else:
                    limited_until = datetime.fromisoformat(rate_limited_str)
                
                now = datetime.now()  # ✅ Наивная дата
                if now < limited_until:
                    # Ещё в лимите
                    remaining = int((limited_until - now).total_seconds())
                    return {
                        "status": "rate_limited",
                        "remaining_seconds": remaining,
                        "blocked": True
                    }
            except Exception as e:
                print(f"[rate-limit-check] Error: {e}")
        
        # ✅ Пауза истекла! Автоматически запустить если был stopped
        if health.get('health_status') in ['paused', 'rate_limited']:
            if account_id not in workers:
                # Создать новый воркер и запустить
                try:
                    await API.post(f'/accounts/{account_id}/start')
                except:
                    pass
            elif not workers[account_id].is_running:
                # Перезапустить существующий
                try:
                    workers[account_id].start()
                except:
                    pass
        
        return {
            "status": "ready",
            "remaining_seconds": 0,
            "blocked": False,
            "auto_restarted": True
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/accounts/{account_id}/rate-limit-history")
async def get_rate_limit_history(account_id: int):
    """Получить историю rate limit'ов с анализом"""
    try:
        history = db.get_rate_limit_history(account_id)
        stats = db.get_rate_limit_stats(account_id)
        
        return {
            "account_id": account_id,
            "history": history,
            "stats": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ✅ NEW: Получить имена аккаунтов из Telegram для running аккаунтов
@app.post("/accounts/sync-profiles")
async def sync_account_profiles():
    """Синхронизировать профили running аккаунтов с Telegram"""
    try:
        synced = []
        print(f"[sync-profiles] Начало синхронизации, workers: {list(workers.keys())}")
        
        for account_id, worker in workers.items():
            print(f"[sync-profiles] Проверка аккаунта {account_id}, is_running={worker.is_running}")
            
            if not worker.is_running:
                continue
            
            try:
                # ✅ Запускаем async функцию в loop воркера
                async def get_profile():
                    try:
                        me = await worker.client.get_me()
                        first_name = me.first_name or ""
                        username = me.username or ""
                        
                        print(f"[sync-profiles] Account {account_id}: first_name='{first_name}', username='{username}'")
                        
                        if first_name or username:
                            db.update_account_profile(account_id, first_name, username)
                            print(f"[sync-profiles] ✅ Обновлён профиль для аккаунта {account_id}")
                            return {
                                "account_id": account_id,
                                "name": first_name,
                                "username": username
                            }
                    except Exception as e:
                        print(f"[sync-profiles] Ошибка для {account_id}: {e}")
                        import traceback
                        traceback.print_exc()
                    return None
                
                # Запускаем в loop воркера
                if hasattr(worker, 'loop') and worker.loop and worker.loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(get_profile(), worker.loop)
                    result = future.result(timeout=5)
                    if result:
                        synced.append(result)
                else:
                    print(f"[sync-profiles] Loop воркера {account_id} не запущен")
            except Exception as e:
                print(f"[sync-profiles] Exception для {account_id}: {e}")
                import traceback
                traceback.print_exc()
        
        print(f"[sync-profiles] ✅ Синхронизация завершена, synced={len(synced)}")
        return {
            "synced": len(synced),
            "accounts": synced
        }
    except Exception as e:
        print(f"[sync-profiles] FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/accounts/{account_id}/set-owned-channel")
async def set_owned_channel(account_id: int, data: dict):
    """Установить личный канал для аккаунта"""
    try:
        channel = data.get('channel', '').strip()
        
        account = db.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        db.set_owned_channel(account_id, channel)
        
        return {
            "success": True,
            "account_id": account_id,
            "owned_channel": channel if channel else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/auth/send-code")
async def send_code(req: CodeSendRequest):
    # Валидация телефона
    valid, error = InputValidator.validate_phone(req.phone)
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    # Резолвим API ID/Hash: ручной ввод -> БД -> 1.envv
    api_id, api_hash = _resolve_api_credentials(req.api_id, req.api_hash)
    if not api_id or not api_hash:
        raise HTTPException(status_code=400, detail="Не заданы API ID / Hash. Укажите их или пропишите в 1.envv")

    try:
        # --- ЛОГИКА ПРОКСИ ---
        proxy_tuple = None
        if req.proxy_id:
            # Если передан ID прокси, берем из базы
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM proxies WHERE id = ?", (req.proxy_id,))
                p = cursor.fetchone()
                if p:
                    import socks
                    p_type = socks.SOCKS5 if p['type'] == 'socks5' else socks.HTTP
                    proxy_tuple = (p_type, p['ip'], p['port'], True, p['username'], p['password'])
        
        # Отправляем код С ПРОКСИ (или без, если proxy_id=None)
        hash = await auth.send_code(req.phone, api_id, api_hash, proxy_tuple, req.proxy_id)
        return {"phone_code_hash": hash}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auth/verify-code")
async def verify_code(req: CodeVerifyRequest):
    try:
        result = await auth.verify_code(req.phone, req.code, req.phone_code_hash)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auth/verify-password")
async def verify_password(req: PasswordVerifyRequest):
    try:
        result = await auth.verify_password(req.phone, req.password)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/accounts")
async def add_account(account: AccountCreate):
    # Резолвим API ID/Hash: ручной ввод -> БД -> 1.envv
    api_id, api_hash = _resolve_api_credentials(account.api_id, account.api_hash)

    # Валидация входных данных
    valid, error = InputValidator.validate_account_data(
        account.phone, 
        api_id, 
        api_hash
    )
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    
    # Санитизация имени сессии
    session_name = account.session_name or f"session_{account.phone.replace('+', '')}"
    session_name = InputValidator.sanitize_string(session_name)
    
    # ✅ Проверяем дубликаты session_name и добавляем UUID если нужно
    existing_accounts_list = db.get_accounts()
    existing_sessions = {acc.get('session_name') for acc in existing_accounts_list if acc.get('session_name')}
    
    if session_name in existing_sessions:
        session_name = f"{session_name}_{uuid.uuid4().hex[:8]}"
        print(f"⚠️ session_name уже существует, используем: {session_name}")

    # Нельзя атомарно заменить .session, пока её использует активный воркер.
    existing_account = next(
        (a for a in db.get_accounts() if str(a.get('phone')) == str(account.phone)),
        None,
    )
    if existing_account and _worker_is_alive(workers.get(existing_account['id'])):
        raise HTTPException(
            status_code=409,
            detail="Сначала остановите уже запущенный аккаунт перед повторной авторизацией",
        )

    try:
        # Получаем proxy_id из auth_manager (если был использован при авторизации)
        proxy_id = await auth.finish_auth(account.phone, session_name)
        
        # Добавляем аккаунт с proxy_id
        acc_id = db.add_account(account.phone, session_name, api_id, api_hash)
        
        # Если был использован прокси, привязываем его к аккаунту
        if proxy_id:
            db.assign_proxy_to_account(acc_id, proxy_id)
        
        return {"status": "success", "id": acc_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _resolve_api_credentials(api_id: Optional[int], api_hash: Optional[str]):
    """
    Возвращает (api_id, api_hash) с приоритетом:
      1) переданные вручную — при этом сохраняются в БД (запоминаются);
      2) ранее сохранённые в БД;
      3) глобальный конфиг из 1.envv.
    """
    api_hash = (api_hash or '').strip() or None

    # 1) Переданные вручную — запоминаем в настройках
    if api_id and api_hash:
        try:
            db.set_setting('telegram_api_id', int(api_id))
            db.set_setting('telegram_api_hash', api_hash)
        except Exception as e:
            print(f"[WARN] не удалось сохранить API-ключи: {e}")
        return int(api_id), api_hash

    # 2) Сохранённые в БД
    saved_id = db.get_setting('telegram_api_id', None)
    saved_hash = db.get_setting('telegram_api_hash', None)
    if saved_id and saved_hash:
        return int(saved_id), str(saved_hash)

    # 3) Глобальный конфиг
    return Config.API_ID, Config.API_HASH


@app.post("/import-session")
async def import_session(
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    phone: Optional[str] = Form(None),
    api_id: Optional[int] = Form(None),
    api_hash: Optional[str] = Form(None)
):
    """
    Импорт готового .session файла.
    phone / api_id / api_hash — опциональны:
      - phone берётся из имени файла, если не указан;
      - api_id / api_hash берутся из глобального конфига (1.envv), если не указаны.
    Так для готовой сессии достаточно просто загрузить файл.
    Принимает как поле `file`, так и `files` (совместимость со старым фронтендом).
    """
    # Совместимость: старый клиент мог отправлять поле `files`
    if file is None and files:
        file = files[0]
    if file is None:
        raise HTTPException(status_code=400, detail="Не передан .session файл")

    # Валидация
    if not file.filename.endswith('.session'):
        raise HTTPException(status_code=400, detail="Файл должен быть .session")

    # Телефон: если не задан — извлекаем из имени файла
    if not phone or not phone.strip():
        raw = file.filename.replace('.session', '').replace('session_', '').replace('session', '')
        digits = ''.join(filter(str.isdigit, raw))
        phone = ('+' + digits) if len(digits) >= 10 else (raw or file.filename.replace('.session', ''))

    # API-ключи: введённые вручную запоминаются в БД, иначе берём из БД/конфига
    api_id, api_hash = _resolve_api_credentials(api_id, api_hash)

    if not api_id or not api_hash:
        raise HTTPException(
            status_code=400,
            detail="Не заданы API ID / API Hash. Укажите их один раз при импорте — дальше запомнятся."
        )

    # Формируем имя сессии
    clean_phone = str(phone).replace('+', '').replace(' ', '')
    session_name = f"session_{clean_phone}"
    
    # ✅ Проверяем дубликаты session_name и добавляем timestamp если нужно
    existing_accounts = db.get_accounts()
    existing_sessions = {acc.get('session_name') for acc in existing_accounts if acc.get('session_name')}
    
    if session_name in existing_sessions:
        # Если уже существует сессия с таким именем, добавляем UUID
        session_name = f"{session_name}_{uuid.uuid4().hex[:8]}"
        print(f"⚠️ session_name {clean_phone} уже существует, используем: {session_name}")
    
    sessions_dir = SESSIONS_DIR
    session_path = os.path.join(sessions_dir, f"{session_name}.session")
    
    # Создаём папку если нет
    os.makedirs(sessions_dir, exist_ok=True)
    
    # Сохраняем файл
    try:
        with open(session_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        # Добавляем аккаунт в базу
        acc_id = db.add_account(phone, session_name, api_id, api_hash)
        
        return {"status": "success", "id": acc_id, "message": f"Сессия импортирована: {session_name}"}
    except Exception as e:
        # Удаляем файл если что-то пошло не так
        if os.path.exists(session_path):
            os.remove(session_path)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/import-sessions-bulk")
async def import_sessions_bulk(
    files: List[UploadFile] = File(...),
    api_id: Optional[int] = Form(None),
    api_hash: Optional[str] = Form(None)
):
    """
    Массовый импорт .session файлов.
    api_id / api_hash опциональны — если не заданы, берутся из 1.envv.
    Телефон извлекается из имени каждого файла.
    """
    # API-ключи: введённые вручную запоминаются в БД, иначе берём из БД/конфига
    api_id, api_hash = _resolve_api_credentials(api_id, api_hash)

    if not api_id or not api_hash:
        raise HTTPException(
            status_code=400,
            detail="Не заданы API ID / API Hash. Укажите их один раз при импорте — дальше запомнятся."
        )

    results = []
    sessions_dir = SESSIONS_DIR
    os.makedirs(sessions_dir, exist_ok=True)
    
    for file in files:
        try:
            if not file.filename.endswith('.session'):
                results.append({"file": file.filename, "status": "error", "message": "Не .session файл"})
                continue
            
            # Извлекаем телефон из имени файла
            # Форматы: session_1234567890.session или 1234567890.session или любое_имя.session
            filename = file.filename.replace('.session', '')
            phone_part = filename.replace('session_', '').replace('session', '')
            
            # Очищаем от нецифровых символов для телефона
            phone_digits = ''.join(filter(str.isdigit, phone_part))
            
            if len(phone_digits) < 10:
                # Если не удалось извлечь телефон, используем имя файла
                phone = phone_part if phone_part else filename
            else:
                phone = phone_digits
            
            session_name = f"session_{phone.replace('+', '').replace(' ', '')}"
            
            # ✅ Проверяем дубликаты session_name и добавляем UUID если нужно
            existing_accounts = db.get_accounts()
            existing_sessions = {acc.get('session_name') for acc in existing_accounts if acc.get('session_name')}
            
            if session_name in existing_sessions:
                session_name = f"{session_name}_{uuid.uuid4().hex[:8]}"
                print(f"⚠️ session_name {phone} уже существует, используем: {session_name}")
            
            session_path = os.path.join(sessions_dir, f"{session_name}.session")
            
            # Проверяем не существует ли уже
            if os.path.exists(session_path):
                results.append({"file": file.filename, "status": "skip", "message": "Сессия уже существует"})
                continue
            
            # Сохраняем файл
            with open(session_path, "wb") as f:
                content = await file.read()
                f.write(content)
            
            # Добавляем аккаунт в базу
            acc_id = db.add_account(phone, session_name, api_id, api_hash)
            results.append({"file": file.filename, "status": "success", "id": acc_id, "phone": phone})
            
        except Exception as e:
            results.append({"file": file.filename, "status": "error", "message": str(e)})
    
    success_count = sum(1 for r in results if r['status'] == 'success')
    return {"total": len(files), "success": success_count, "results": results}


@app.get("/accounts/{acc_id}/profile")
async def get_account_profile(acc_id: int):
    """Получает профиль аккаунта из Telegram (имя, фото, био)"""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    
    worker = workers[acc_id]
    
    try:
        from modules.profile_manager import ProfileManager
        import base64
        
        async def get_profile():
            me = await worker.client.get_me()
            
            # Получаем аватарку
            avatar_base64 = None
            try:
                photo = await worker.client.download_profile_photo(me, bytes)
                if photo:
                    avatar_base64 = f"data:image/jpeg;base64,{base64.b64encode(photo).decode()}"
            except:
                pass
            
            # Получаем био
            from telethon.tl.functions.users import GetFullUserRequest
            full_user = await worker.client(GetFullUserRequest(me))
            bio = full_user.full_user.about or ""
            
            # ✅ Получаем owned_channel из БД
            account = db.get_account(acc_id)
            owned_channel = account.get('owned_channel', '') if account else ""
            
            return {
                "id": me.id,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "username": me.username or "",
                "phone": me.phone or "",
                "bio": bio,
                "avatar": avatar_base64,
                "owned_channel": owned_channel or ""
            }
        
        coro = get_profile()
        future = worker.run_task(coro)
        if future:
            return future.result(timeout=30)
        else:
            coro.close()
            raise HTTPException(status_code=500, detail="Не удалось получить профиль")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    bio: Optional[str] = None
    avatar_base64: Optional[str] = None
    username: Optional[str] = None  # Добавляем поддержку username


@app.put("/accounts/{acc_id}/profile")
async def update_account_profile(acc_id: int, data: ProfileUpdate):
    """Обновляет профиль аккаунта в Telegram: имя, био, аватар, username"""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    
    worker = workers[acc_id]
    
    # Проверяем что loop воркера доступен
    if not hasattr(worker, 'loop') or not worker.loop or not worker.loop.is_running():
        raise HTTPException(status_code=400, detail="Event loop воркера недоступен")
    
    try:
        async def _do_update():
            """Внутренняя функция, которая выполняется в loop воркера"""
            results = {}
            
            try:
                me = await worker.client.get_me()
                
                # 1. Обновляем Username (если передан и отличается)
                if data.username is not None:
                    current_username = me.username or ""
                    if data.username != current_username:
                        try:
                            await worker.client(UpdateUsernameRequest(data.username))
                            results['username'] = f"✅ Username изменен на @{data.username}"
                            print(f"[Profile] Username изменен на @{data.username}")
                        except Exception as e:
                            # Username может быть занят
                            results['username'] = f"❌ Ошибка смены username: {str(e)}"
                            print(f"[Profile] ❌ Ошибка смены username: {e}")
                    else:
                        results['username'] = "⏭️ Username не изменился"
                
                # 2. Имя и Фамилия
                if data.first_name is not None or data.last_name is not None:
                    try:
                        await worker.client(UpdateProfileRequest(
                            first_name=data.first_name if data.first_name is not None else me.first_name or "",
                            last_name=data.last_name if data.last_name is not None else me.last_name or ""
                        ))
                        results['name'] = "✅ Имя обновлено"
                        print(f"[Profile] Имя обновлено")
                    except Exception as e:
                        results['name'] = f"❌ Ошибка обновления имени: {str(e)}"
                        print(f"[Profile] ❌ Ошибка смены имени: {e}")
                
                # 3. Bio
                if data.bio is not None:
                    try:
                        bio = data.bio[:70]  # Telegram ограничение
                        await worker.client(UpdateProfileRequest(about=bio))
                        results['bio'] = "✅ Био обновлено"
                        print(f"[Profile] Био обновлено")
                    except Exception as e:
                        results['bio'] = f"❌ Ошибка обновления био: {str(e)}"
                        print(f"[Profile] ❌ Ошибка смены bio: {e}")
                
                # 4. Аватар (увеличенная надежность с детальным логированием)
                if data.avatar_base64:
                    print(f"[Profile] Начинаю загрузку аватара...")
                    try:
                        # Декодируем Base64
                        header_end = data.avatar_base64.find(',')
                        b64_data = data.avatar_base64[header_end+1:] if header_end != -1 else data.avatar_base64
                        image_bytes = base64.b64decode(b64_data)
                        print(f"[Profile] Размер изображения: {len(image_bytes) // 1024} KB")
                        
                        # Telethon требует файловый объект
                        file_obj = io.BytesIO(image_bytes)
                        file_obj.name = "avatar.jpg"  # Имя обязательно для определения MIME типа
                        
                        # Загрузка файла в Telegram
                        print(f"[Profile] Загрузка файла в Telegram...")
                        uploaded_file = await worker.client.upload_file(file_obj)
                        
                        # Установка фото профиля
                        print(f"[Profile] Установка фото профиля...")
                        await worker.client(UploadProfilePhotoRequest(file=uploaded_file))
                        
                        results['avatar'] = "✅ Аватар обновлен"
                        print(f"[Profile] ✅ Аватар успешно обновлен")
                    except Exception as e:
                        results['avatar'] = f"❌ Ошибка загрузки аватара: {str(e)}"
                        print(f"[Profile] ❌ ОШИБКА ЗАГРУЗКИ АВАТАРА: {type(e).__name__}: {e}")
                        # Не выбрасываем исключение дальше, чтобы не обрушить весь запрос
                
                return {"status": "success", "results": results}
            except Exception as e:
                print(f"[Profile] ❌ Общая ошибка обновления профиля: {e}")
                return {"status": "error", "detail": str(e)}
        
        # Запускаем с увеличенным таймаутом (180 сек = 3 минуты)
        # Загрузка картинок через мобильный прокси может быть медленной
        future = asyncio.run_coroutine_threadsafe(_do_update(), worker.loop)
        result = future.result(timeout=180.0)
        
        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("detail"))
        return result
    except asyncio.TimeoutError:
        # Если таймаут — возвращаем понятную ошибку
        print(f"[Profile] TIMEOUT: Загрузка аватара заняла более 3 минут")
        raise HTTPException(status_code=408, detail="Превышено время ожидания. Загрузка аватарки заняла слишком долго (плохой прокси?). Попробуйте изображение меньшего размера.")
    except HTTPException:
        raise
    except Exception as e:
        print(f"CRITICAL Error updating profile thread execution: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка выполнения: {str(e)}")

@app.post("/accounts/{acc_id}/start")
async def start_bot(acc_id: int):
    accounts = db.get_accounts()
    account = next((a for a in accounts if a['id'] == acc_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    
    if acc_id in workers and workers[acc_id].is_running:
        return {"status": "already_running"}
    
    _start_worker_for_account(account)
    db.update_account_status(acc_id, 'active')
    return {"status": "started"}

@app.post("/accounts/{acc_id}/stop")
async def stop_bot(acc_id: int):
    if acc_id in workers:
        workers[acc_id].stop()
        del workers[acc_id]
        db.update_account_status(acc_id, 'stopped')
        return {"status": "stopped"}
    return {"status": "not_running"}

@app.delete("/accounts/{acc_id}")
async def delete_account(acc_id: int):
    # Останавливаем бота если он работает
    if acc_id in workers:
        workers[acc_id].stop()
        del workers[acc_id]
    
    # Удаляем из базы
    db.delete_account(acc_id)
    return {"status": "deleted"}

class ProxyAssign(BaseModel):
    proxy_id: Optional[int] = None  # None = отвязать прокси

@app.post("/accounts/{acc_id}/proxy")
async def assign_proxy(acc_id: int, data: ProxyAssign):
    """Привязывает или отвязывает прокси от аккаунта"""
    accounts = db.get_accounts()
    account = next((a for a in accounts if a['id'] == acc_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    
    # Проверяем существование прокси если указан
    if data.proxy_id is not None:
        proxies = db.get_proxies()
        proxy = next((p for p in proxies if p['id'] == data.proxy_id), None)
        if not proxy:
            raise HTTPException(status_code=404, detail="Прокси не найден")
    
    # Обновляем привязку
    db.assign_proxy_to_account(acc_id, data.proxy_id)
    
    # Если бот работает — нужно перезапустить для применения прокси
    needs_restart = acc_id in workers and workers[acc_id].is_running
    
    return {
        "status": "success", 
        "proxy_id": data.proxy_id,
        "needs_restart": needs_restart,
        "message": "Прокси привязан. Перезапустите бота для применения." if needs_restart else "Прокси привязан"
    }

@app.get("/settings")
async def get_settings():
    return {
        "gemini_api_key": db.get_setting("gemini_api_key", Config.GEMINI_API_KEY),
        "gemini_model": db.get_setting("gemini_model", Config.GEMINI_MODEL),
        "search_enabled": db.get_setting("search_enabled", Config.SEARCH_ENABLED),
        "search_keywords": db.get_setting("search_keywords", "\n".join(Config.load_keywords())),
        "min_channel_subs": db.get_setting("min_channel_subs", 20000),
        "min_chat_members": db.get_setting("min_chat_members", 100),
        "react_to_posts": db.get_setting("react_to_posts", True),
        "like_other_comments": db.get_setting("like_other_comments", False),
        "chat_interaction_enabled": db.get_setting("chat_interaction_enabled", True),
        "like_chat_messages": db.get_setting("like_chat_messages", True),
        "lusty_mode": db.get_setting("lusty_mode", False),
        "work_mode": db.get_setting("work_mode", "neutral"),
        "auto_night_mode": db.get_setting("auto_night_mode", False),
        "warmup_mode": db.get_setting("warmup_mode", False),  # Режим прогрева для новых аккаунтов
        "quick_comment_mode": db.get_setting("quick_comment_mode", False),  # Быстрый коммент + редактирование
        "quick_comment_max_age_minutes": db.get_setting("quick_comment_max_age_minutes", 5),  # Макс возраст поста для быстрого режима
        # Комментить от имени канала/группы (send_as)
        "comment_as_channel": db.get_setting("comment_as_channel", False),
        "comment_as_channel_username": db.get_setting("comment_as_channel_username", ""),
        # Новые настройки
        "max_post_age_hours": db.get_setting("max_post_age_hours", 12),
        "comment_delay_min": db.get_setting("comment_delay_min", 1),
        "comment_delay_max": db.get_setting("comment_delay_max", 3),
        "search_interval_cycles": db.get_setting("search_interval_cycles", 20),
        "cycle_pause_seconds": db.get_setting("cycle_pause_seconds", 60),
        "likes_per_post": db.get_setting("likes_per_post", 3),
        "comment_prompt": db.get_setting("comment_prompt", Config.COMMENT_PERSONA_SYSTEM),
        "comment_channels_mode": db.get_setting("comment_channels_mode", "all"),
        "chat_message_interval_minutes": db.get_setting("chat_message_interval_minutes", 30),
        "chat_reply_chance": db.get_setting("chat_reply_chance", 0.03),  # Шанс ответить в чате (3%)
        # === Новые тоглы поведения в чатах ===
        "leave_if_no_write": db.get_setting("leave_if_no_write", True),       # Выход при ChatWriteForbidden
        "auto_leave_junk": db.get_setting("auto_leave_junk", False),          # Авто-отписка от мусорных чатов через AI
        "spambot_unblock": db.get_setting("spambot_unblock", True),           # Писать @SpamBot при PeerFloodError
        # === Health watcher ===
        "channel_watcher_enabled": db.get_setting("channel_watcher_enabled", True),
        "channel_watcher_interval_minutes": db.get_setting("channel_watcher_interval_minutes", 30),
        # === Авто-инвайт (фоновый) ===
        "auto_invite_enabled": db.get_setting("auto_invite_enabled", False),
        "auto_invite_target_channel": db.get_setting("auto_invite_target_channel", ""),
        "auto_invite_source_chats": db.get_setting("auto_invite_source_chats", ""),
        "auto_invite_per_cycle": db.get_setting("auto_invite_per_cycle", 3),
        "auto_invite_daily_limit": db.get_setting("auto_invite_daily_limit", Config.INVITER_DAILY_LIMIT),
        "auto_invite_interval_cycles": db.get_setting("auto_invite_interval_cycles", 5),
        # === Проверка публикации комментариев (детект теневого бана) ===
        "verify_comment_published": db.get_setting("verify_comment_published", True),
        # === Глобальная пауза ===
        "global_pause": global_pause,
    }

class SettingsUpdate(BaseModel):
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    search_enabled: Optional[bool] = None
    search_keywords: Optional[str] = None
    min_channel_subs: Optional[int] = None
    min_chat_members: Optional[int] = None
    react_to_posts: Optional[bool] = None
    like_other_comments: Optional[bool] = None
    chat_interaction_enabled: Optional[bool] = None
    like_chat_messages: Optional[bool] = None
    lusty_mode: Optional[bool] = None
    work_mode: Optional[str] = None
    auto_night_mode: Optional[bool] = None
    warmup_mode: Optional[bool] = None  # Режим прогрева для новых аккаунтов
    quick_comment_mode: Optional[bool] = None  # Быстрый коммент + редактирование
    quick_comment_max_age_minutes: Optional[int] = None  # Макс возраст поста для быстрого режима (минуты)
    # Комментить от имени канала/группы (send_as)
    comment_as_channel: Optional[bool] = None
    comment_as_channel_username: Optional[str] = None
    # Новые настройки
    max_post_age_hours: Optional[int] = None
    comment_delay_min: Optional[int] = None
    comment_delay_max: Optional[int] = None
    search_interval_cycles: Optional[int] = None
    cycle_pause_seconds: Optional[int] = None
    likes_per_post: Optional[int] = None
    comment_prompt: Optional[str] = None
    comment_channels_mode: Optional[str] = None  # all, public, private, joined
    chat_message_interval_minutes: Optional[int] = None  # Минимальный интервал между сообщениями в один чат
    chat_reply_chance: Optional[float] = None  # Шанс ответить в чате (0.0 - 1.0)
    # === Новые тоглы поведения в чатах ===
    leave_if_no_write: Optional[bool] = None
    auto_leave_junk: Optional[bool] = None
    spambot_unblock: Optional[bool] = None
    # === Health watcher ===
    channel_watcher_enabled: Optional[bool] = None
    channel_watcher_interval_minutes: Optional[int] = None
    # === Авто-инвайт (фоновый) ===
    auto_invite_enabled: Optional[bool] = None
    auto_invite_target_channel: Optional[str] = None
    auto_invite_source_chats: Optional[str] = None
    auto_invite_per_cycle: Optional[int] = None
    auto_invite_daily_limit: Optional[int] = None
    auto_invite_interval_cycles: Optional[int] = None
    # === Проверка публикации комментариев (детект теневого бана) ===
    verify_comment_published: Optional[bool] = None
    # === Глобальная пауза (через единый сеттер) ===
    global_pause: Optional[bool] = None

@app.post("/settings")
async def update_settings(settings: SettingsUpdate):
    payload = settings.model_dump(exclude_none=True)
    # Глобальная пауза — отдельной веткой, синхронизируем с воркерами
    if 'global_pause' in payload:
        _set_global_pause(payload.pop('global_pause'))
    for key, value in payload.items():
        db.set_setting(key, value)
    return {"status": "success"}

@app.post("/settings/test-ai")
async def test_ai_connection():
    """Тестирует подключение к нейросети"""
    import aiohttp
    
    api_key = db.get_setting("gemini_api_key", Config.GEMINI_API_KEY)
    model = db.get_setting("gemini_model", Config.GEMINI_MODEL)
    base_url = Config.GEMINI_BASE_URL.rstrip('/')
    endpoint = f"{base_url}/chat/completions"
    
    if not api_key:
        return {"status": "error", "message": "❌ API ключ не настроен"}
    
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Say 'OK' if you can hear me"}],
            "max_tokens": 10
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    reply = data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                    return {
                        "status": "ok", 
                        "message": f"✅ Нейросеть работает! Модель: {model}",
                        "reply": reply
                    }
                else:
                    error_text = await resp.text()
                    return {
                        "status": "error",
                        "message": f"❌ Ошибка API ({resp.status}): {error_text[:200]}"
                    }
    except Exception as e:
        return {"status": "error", "message": f"❌ Ошибка подключения: {str(e)}"}

@app.get("/proxies")
async def get_proxies():
    return db.get_proxies()

class ProxyCreate(BaseModel):
    ip: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    type: str = "http"

class ProxyCheck(BaseModel):
    ip: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    type: str = "http"

@app.post("/proxies/check")
async def check_proxy(proxy: ProxyCheck):
    """Проверяет работоспособность прокси"""
    import socket
    import socks as proxy_socks
    
    try:
        # Создаём сокет с прокси
        if proxy.type.lower() == "socks5":
            proxy_type = proxy_socks.SOCKS5
        else:
            proxy_type = proxy_socks.HTTP
        
        s = proxy_socks.socksocket()
        s.set_proxy(proxy_type, proxy.ip, proxy.port, 
                   username=proxy.username, password=proxy.password)
        s.settimeout(10)
        
        # Пробуем подключиться к Telegram
        s.connect(("149.154.167.50", 443))  # Telegram DC
        s.close()
        
        return {"status": "ok", "message": f"✅ Прокси {proxy.ip}:{proxy.port} работает"}
    except Exception as e:
        return {"status": "error", "message": f"❌ Прокси не работает: {str(e)}"}

@app.post("/proxies")
async def add_proxy(proxy: ProxyCreate):
    # Валидация входных данных
    valid, error = InputValidator.validate_proxy(proxy.ip, proxy.port, proxy.type)
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    
    # Санитизация username и password
    username = InputValidator.sanitize_string(proxy.username) if proxy.username else None
    password = InputValidator.sanitize_string(proxy.password) if proxy.password else None
    
    proxy_id = db.add_proxy(proxy.ip, proxy.port, username, password, proxy.type)
    return {"id": proxy_id, "status": "added"}

@app.delete("/proxies/{proxy_id}")
async def delete_proxy(proxy_id: int):
    """Удаляет прокси из базы данных"""
    try:
        db.delete_proxy(proxy_id)
        return {"status": "deleted", "proxy_id": proxy_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка удаления прокси: {str(e)}")


@app.get("/discovery/channels")
async def get_discovered_channels(status: Optional[str] = None):
    # Показываем все каналы с индикатором статуса комментариев
    return db.get_found_channels(status=status, only_open_comments=False)

@app.get("/discovery/pending")
async def get_pending_join_requests(account_id: Optional[int] = None):
    """Возвращает список pending join requests (каналы с модерацией)"""
    return db.get_pending_joins(account_id=account_id, status='pending')

@app.get("/discovery/pending/all")
async def get_all_pending_requests():
    """Возвращает все pending requests со всеми статусами"""
    pending = db.get_pending_joins(status='pending')
    approved = db.get_pending_joins(status='approved')
    expired = db.get_pending_joins(status='expired')
    rejected = db.get_pending_joins(status='rejected')
    
    return {
        "pending": pending,
        "approved": approved,
        "expired": expired,
        "rejected": rejected,
        "total_pending": len(pending)
    }

@app.post("/discovery/channels/{channel:path}/pin")
async def pin_channel(channel: str):
    """Ставит «замочек» — канал не удаляется авточисткой и остаётся в общей базе."""
    import urllib.parse
    channel = urllib.parse.unquote(channel).lstrip('@')
    ok = db.set_channel_pinned(channel, True)
    if not ok:
        raise HTTPException(status_code=404, detail="Канал не найден")
    return {"status": "pinned", "channel": channel}

@app.delete("/discovery/channels/{channel:path}/pin")
async def unpin_channel(channel: str):
    """Снимает «замочек» с канала."""
    import urllib.parse
    channel = urllib.parse.unquote(channel).lstrip('@')
    ok = db.set_channel_pinned(channel, False)
    if not ok:
        raise HTTPException(status_code=404, detail="Канал не найден")
    return {"status": "unpinned", "channel": channel}

@app.delete("/discovery/channels/{channel:path}")
async def delete_discovered_channel(channel: str):
    """Удаляет канал из базы найденных каналов (принудительно, включая manual)"""
    import urllib.parse

    # Декодируем URL-encoded строку
    channel = urllib.parse.unquote(channel)
    original_channel = channel  # Сохраняем оригинал для поиска в базе

    # Нормализуем канал - извлекаем хэш из полной ссылки
    normalized = channel
    if 't.me/' in normalized:
        # Извлекаем часть после t.me/
        parts = normalized.split('t.me/')
        if len(parts) > 1:
            normalized = parts[-1]

    # Убираем joinchat/ если есть
    if normalized.startswith('joinchat/'):
        normalized = '+' + normalized[9:]

    normalized = normalized.lstrip('@')

    try:
        # Пробуем удалить по оригинальному значению (как в базе)
        deleted = db.delete_found_channel(original_channel, force=True)

        # Если не удалилось - пробуем по нормализованному
        if not deleted:
            db.delete_found_channel(normalized, force=True)

        return {"status": "deleted", "channel": channel}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/discovery/cleanup")
async def cleanup_closed_channels():
    """Удаляет все каналы с закрытыми комментами (кроме manual)"""
    count = db.cleanup_closed_channels()
    return {"deleted": count}

@app.post("/discovery/cleanup-unpinned")
async def cleanup_unpinned_channels(older_than_days: Optional[int] = None):
    """
    Удаляет незакреплённые каналы (без замочка и не manual).
    older_than_days: если задано — удаляет только старше N дней.
    """
    count = db.cleanup_unpinned_channels(older_than_days=older_than_days)
    return {"deleted": count}


@app.get("/discovery/exclusions")
async def get_global_channel_exclusions(limit: int = 500):
    """Постоянный общий бан-лист структурно неподходящих каналов."""
    return db.list_global_exclusions(limit=max(1, min(int(limit), 5000)))


@app.post("/discovery/channels/{channel}/recheck")
async def recheck_channel(channel: str):
    """Перепроверяет канал на наличие открытых комментариев"""
    # Находим любой запущенный воркер для проверки
    running_workers = [w for w in workers.values() if w.is_running]
    if not running_workers:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов для проверки")
    
    worker = running_workers[0]
    normalized = channel.lstrip('@')
    
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        
        # Получаем канал
        entity = await worker.client.get_entity(normalized)
        full = await worker.client(GetFullChannelRequest(entity))
        
        # Проверяем есть ли linked_chat (группа обсуждений)
        has_comments = full.full_chat.linked_chat_id is not None
        
        # Успешный GetFullChannelRequest без linked_chat_id — структурное
        # доказательство. Глобально исключённые записи не «оживляем» автоматически.
        globally_excluded = db.is_channel_globally_excluded(normalized)
        if not has_comments:
            db.update_channel_comments_status(
                normalized,
                False,
                structural=True,
                reason="manual_recheck_no_linked_chat",
                evidence={"linked_chat_id": None},
                source_module="api_recheck",
            )
        elif not globally_excluded:
            db.update_channel_comments_status(normalized, True)
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM channel_bans WHERE channel = ?", (normalized,))

        return {
            "channel": normalized,
            "has_open_comments": has_comments and not globally_excluded,
            "status": "globally_excluded" if globally_excluded else ("open" if has_comments else "closed"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/discovery/channels/recheck-all-closed")
async def recheck_all_closed_channels():
    """Перепроверяет все каналы с закрытыми комментариями"""
    running_workers = [w for w in workers.values() if w.is_running]
    if not running_workers:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов для проверки")
    
    worker = running_workers[0]
    
    # Получаем все закрытые каналы
    channels = db.get_found_channels(only_open_comments=False)
    closed_channels = [ch for ch in channels if not ch.get('can_comment')]
    
    results = {"checked": 0, "opened": 0, "still_closed": 0, "errors": 0}
    
    from telethon.tl.functions.channels import GetFullChannelRequest
    import asyncio
    
    for ch in closed_channels:
        channel = ch['channel']
        try:
            entity = await worker.client.get_entity(channel)
            full = await worker.client(GetFullChannelRequest(entity))
            has_comments = full.full_chat.linked_chat_id is not None
            
            globally_excluded = db.is_channel_globally_excluded(channel)
            if not has_comments:
                db.update_channel_comments_status(
                    channel,
                    False,
                    structural=True,
                    reason="bulk_recheck_no_linked_chat",
                    evidence={"linked_chat_id": None},
                    source_module="api_recheck",
                )
            elif not globally_excluded:
                db.update_channel_comments_status(channel, True)
            results["checked"] += 1

            if has_comments and not globally_excluded:
                results["opened"] += 1
                # Снимаем только account-local баны; permanent exclusions не трогаем.
                with db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM channel_bans WHERE channel = ?", (channel,))
            else:
                results["still_closed"] += 1
            
            await asyncio.sleep(0.5)  # Небольшая задержка между проверками
        except Exception as e:
            results["errors"] += 1
    
    return results


@app.post("/discovery/channels/join-private")
async def join_all_private_channels():
    """Вступает во все приватные каналы всеми запущенными аккаунтами"""
    global global_pause
    from telethon.tl.functions.messages import ImportChatInviteRequest
    from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
    import asyncio
    import re
    
    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running]
    if not running_workers:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов")
    
    # Получаем все приватные каналы из базы
    all_channels = db.get_found_channels(limit=500, only_open_comments=False)
    private_channels = [ch for ch in all_channels if ch['channel'].startswith('+')]
    
    if not private_channels:
        return {"status": "ok", "message": "Нет приватных каналов", "total": 0}
    
    # Включаем глобальную паузу
    global_pause = True
    print("[join-private] ⏸️ Глобальная пауза включена")
    
    total = len(private_channels)
    results = {"joined": 0, "already": 0, "errors": 0, "channels_updated": 0, "total": total, "processed": 0, "frozen": 0, "rate_limited": 0}
    
    # Словарь для отслеживания rate limit по аккаунтам
    account_wait_until = {}
    
    print(f"[join-private] Начинаем вступление в {total} приватных каналов...")
    
    for idx, ch in enumerate(private_channels):
        invite_hash = ch['channel'][1:]  # Убираем +
        channel_title = ch.get('title', ch['channel'])
        
        print(f"[join-private] [{idx+1}/{total}] Обрабатываем: {channel_title}")
        
        # Ищем аккаунт без rate limit
        current_time = asyncio.get_event_loop().time()
        available_worker = None
        
        for acc_id, worker in running_workers:
            # Проверяем rate limit
            wait_until = account_wait_until.get(acc_id, 0)
            if current_time >= wait_until:
                available_worker = (acc_id, worker)
                break
        
        if not available_worker:
            # Все аккаунты в rate limit - ждём минимальное время
            min_wait = min(account_wait_until.values()) - current_time
            if min_wait > 0:
                print(f"[join-private] ⏳ Все аккаунты в rate limit, ждём {int(min_wait)} сек...")
                results["rate_limited"] += 1
                await asyncio.sleep(min(min_wait, 60))  # Ждём максимум 60 сек
            available_worker = running_workers[idx % len(running_workers)]
        
        acc_id, worker = available_worker
        
        async def try_join():
            try:
                result = await worker.client(ImportChatInviteRequest(invite_hash))
                entity = result.chats[0] if result.chats else None
                if entity:
                    # Сохраняем channel_id и title в базу
                    db.update_channel_info(
                        ch['channel'], 
                        channel_id=entity.id,
                        title=getattr(entity, 'title', None)
                    )
                    
                    # Вступаем в группу обсуждений
                    try:
                        full = await worker.client(GetFullChannelRequest(entity))
                        if full.full_chat.linked_chat_id:
                            linked = await worker.client.get_entity(full.full_chat.linked_chat_id)
                            await worker.client(JoinChannelRequest(linked))
                    except:
                        pass
                    
                    return "joined", getattr(entity, 'title', 'unknown')
            except Exception as e:
                err_str = str(e)
                err_lower = err_str.lower()
                
                if 'already' in err_lower or 'user_already' in err_lower:
                    return "already", None
                elif 'frozen' in err_lower:
                    return "frozen", acc_id  # Возвращаем ID аккаунта
                elif 'wait' in err_lower:
                    # Извлекаем время ожидания
                    match = re.search(r'(\d+)\s*seconds', err_str)
                    wait_time = int(match.group(1)) if match else 300
                    return "rate_limit", wait_time
                
                return "error", str(e)
            return "error", "unknown"
        
        try:
            coro = try_join()
            future = worker.run_task(coro)
            if future:
                result, info = future.result(timeout=30)
                if result == "joined":
                    results["joined"] += 1
                    results["channels_updated"] += 1
                    print(f"[join-private] ✅ Вступили: {info}")
                elif result == "already":
                    results["already"] += 1
                    print(f"[join-private] 📌 Уже вступили")
                elif result == "frozen":
                    results["frozen"] += 1
                    # Помечаем аккаунт как frozen и исключаем из списка
                    frozen_acc_id = info
                    running_workers = [(a, w) for a, w in running_workers if a != frozen_acc_id]
                    db.update_account_status(frozen_acc_id, "frozen_join")
                    print(f"[join-private] 🥶 Аккаунт {frozen_acc_id} заморожен для вступлений, исключаем")
                    if not running_workers:
                        print(f"[join-private] ❌ Все аккаунты заморожены!")
                        break
                elif result == "rate_limit":
                    results["rate_limited"] += 1
                    wait_time = info if isinstance(info, int) else 300
                    account_wait_until[acc_id] = asyncio.get_event_loop().time() + wait_time
                    print(f"[join-private] ⏳ Rate limit {wait_time} сек для аккаунта {acc_id}")
                else:
                    results["errors"] += 1
                    print(f"[join-private] ❌ Ошибка: {info}")
        except Exception as e:
            results["errors"] += 1
            print(f"[join-private] ❌ Таймаут: {e}")
        
        results["processed"] = idx + 1
        
        # Случайная задержка 30-60 сек чтобы не ловить rate limit
        import random
        delay = random.randint(30, 60)
        print(f"[join-private] ⏳ Ждём {delay} сек...")
        await asyncio.sleep(delay)
    
    # Снимаем глобальную паузу
    global_pause = False
    print("[join-private] ▶️ Глобальная пауза снята")
    
    print(f"[join-private] Готово! Вступили: {results['joined']}, Уже были: {results['already']}, Заморожено: {results['frozen']}, Rate limit: {results['rate_limited']}, Ошибок: {results['errors']}")
    return results

@app.post("/discovery/channels/fix-private-titles")
async def fix_private_channel_titles():
    """Исправляет названия приватных каналов, получая реальные названия из Telegram"""
    from telethon.tl.types import PeerChannel
    from telethon.tl.functions.messages import CheckChatInviteRequest
    import asyncio
    
    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running]
    if not running_workers:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов")
    
    # Берём первый запущенный аккаунт
    acc_id, worker = running_workers[0]
    
    # Получаем все приватные каналы из базы
    all_channels = db.get_found_channels(limit=500, only_open_comments=False)
    private_channels = [ch for ch in all_channels if ch['channel'].startswith('+')]
    
    if not private_channels:
        return {"status": "ok", "message": "Нет приватных каналов", "fixed": 0}
    
    # Сначала получаем все диалоги аккаунта и строим словарь channel_id -> title
    print("[fix-titles] Загружаю диалоги аккаунта...")
    dialogs_map = {}  # channel_id -> {title, subs}
    try:
        async for dialog in worker.client.iter_dialogs(limit=500):
            if dialog.is_channel:
                dialogs_map[dialog.entity.id] = {
                    'title': dialog.entity.title,
                    'subs': getattr(dialog.entity, 'participants_count', 0)
                }
        print(f"[fix-titles] Загружено {len(dialogs_map)} каналов из диалогов")
    except Exception as e:
        print(f"[fix-titles] Ошибка загрузки диалогов: {e}")
    
    fixed = 0
    errors = 0
    skipped = 0
    
    for ch in private_channels:
        invite_hash = ch['channel'].lstrip('+')
        channel_id = ch.get('channel_id')
        current_title = ch.get('title', '')
        
        try:
            real_title = None
            subs = 0
            
            # Способ 1: Если есть channel_id - ищем в диалогах
            if channel_id and int(channel_id) in dialogs_map:
                info = dialogs_map[int(channel_id)]
                real_title = info['title']
                subs = info['subs']
                print(f"[fix-titles] Найден в диалогах: {invite_hash} -> {real_title}")
            
            # Способ 2: Проверяем через CheckChatInviteRequest
            if not real_title:
                try:
                    result = await worker.client(CheckChatInviteRequest(invite_hash))
                    
                    # ChatInviteAlready - уже состоим в канале
                    if hasattr(result, 'chat'):
                        real_title = result.chat.title
                        subs = getattr(result.chat, 'participants_count', 0)
                        new_channel_id = result.chat.id
                        # Обновляем channel_id если его не было
                        if not channel_id:
                            db.update_channel_info(ch['channel'], channel_id=new_channel_id)
                            print(f"[fix-titles] Обновлён channel_id: {invite_hash} -> {new_channel_id}")
                    # ChatInvite - ещё не вступили, но можем получить название
                    elif hasattr(result, 'title'):
                        real_title = result.title
                        subs = getattr(result, 'participants_count', 0)
                except Exception as e:
                    err_str = str(e).lower()
                    if 'expired' in err_str or 'invalid' in err_str:
                        print(f"[fix-titles] Ссылка истекла: {invite_hash}")
                    else:
                        print(f"[fix-titles] CheckChatInvite ошибка для {invite_hash}: {e}")
            
            # Обновляем если получили название и оно отличается
            if real_title:
                if real_title != current_title:
                    db.update_channel_info(ch['channel'], title=real_title)
                    # Также обновляем subs_count
                    with db.get_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE found_channels SET subs_count = ? WHERE channel = ?",
                            (subs, ch['channel'].lstrip('+'))
                        )
                    print(f"[fix-titles] ✅ Исправлено: {ch['channel']} '{current_title}' -> '{real_title}'")
                    fixed += 1
                else:
                    skipped += 1
            else:
                errors += 1
                
        except Exception as e:
            print(f"[fix-titles] Ошибка для {ch['channel']}: {e}")
            errors += 1
        
        # Небольшая задержка
        await asyncio.sleep(0.3)
    
    print(f"[fix-titles] Готово! Исправлено: {fixed}, Пропущено (уже верно): {skipped}, Ошибок: {errors}")
    return {"status": "ok", "fixed": fixed, "skipped": skipped, "errors": errors, "total": len(private_channels)}

@app.post("/discovery/channels/{channel}/comment-now")
async def comment_now(channel: str):
    """Отправляет комментарий в канал прямо сейчас через один из запущенных аккаунтов.
    При бане автоматически пробует другой аккаунт."""
    import random
    
    # Находим запущенный аккаунт который не забанен в этом канале
    normalized = channel.lstrip('@')
    
    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running]
    if not running_workers:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов")
    
    # Фильтруем аккаунты которые не забанены в этом канале
    available_workers = []
    for acc_id, worker in running_workers:
        if not db.is_banned(acc_id, normalized):
            available_workers.append((acc_id, worker))
    
    if not available_workers:
        raise HTTPException(status_code=400, detail="Все аккаунты забанены в этом канале")
    
    # Получаем информацию о канале из базы
    channel_info = db.get_channel_info(normalized)
    saved_channel_id = channel_info.get('channel_id') if channel_info else None
    saved_title = channel_info.get('title') if channel_info else None
    
    # Перемешиваем аккаунты для случайного выбора
    random.shuffle(available_workers)
    
    # Паттерны для определения бана
    ban_patterns = [
        "banned", "restricted", "kicked", "user_banned",
        "chat_write_forbidden", "channel_private", 
        "chat_admin_required", "you have been banned",
        "you're not allowed", "access denied", "forbidden",
        "you can't write", "send messages"
    ]
    
    async def send_comment_with_worker(acc_id, worker):
        """Пытается отправить комментарий через конкретный воркер"""
        from telethon.tl.functions.messages import GetDiscussionMessageRequest
        from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest
        import asyncio
        
        entity = None
        
        # Получаем/вступаем в канал
        try:
            if normalized.startswith('+'):
                # Приватный канал
                invite_hash = normalized[1:]
                
                # Если есть сохранённый channel_id - ищем по нему
                if saved_channel_id:
                    try:
                        entity = await worker.client.get_entity(saved_channel_id)
                        print(f"[comment-now] acc:{acc_id} Нашли канал по ID: {saved_channel_id}")
                    except:
                        pass
                
                # Если есть title - ищем по нему в диалогах
                if not entity and saved_title and saved_title != normalized:
                    async for dialog in worker.client.iter_dialogs(limit=200):
                        if dialog.is_channel and dialog.title == saved_title:
                            entity = dialog.entity
                            # Сохраняем ID для будущего использования
                            db.update_channel_info(normalized, channel_id=entity.id)
                            print(f"[comment-now] acc:{acc_id} Нашли по title: {saved_title}")
                            break
                
                # Пробуем вступить
                if not entity:
                    try:
                        result = await worker.client(ImportChatInviteRequest(invite_hash))
                        entity = result.chats[0] if result.chats else None
                        if entity:
                            # Сохраняем ID и title
                            db.update_channel_info(normalized, channel_id=entity.id, title=getattr(entity, 'title', None))
                            print(f"[comment-now] acc:{acc_id} Вступили: {getattr(entity, 'title', 'unknown')}")
                        await asyncio.sleep(2)
                    except Exception as e:
                        err_str = str(e).lower()
                        if 'already' not in err_str and 'user_already' not in err_str:
                            print(f"[comment-now] acc:{acc_id} Ошибка вступления: {e}")
                
                # Последняя попытка - ищем любой приватный канал
                if not entity:
                    async for dialog in worker.client.iter_dialogs(limit=300):
                        if dialog.is_channel and not getattr(dialog.entity, 'username', None):
                            entity = dialog.entity
                            print(f"[comment-now] acc:{acc_id} Fallback канал: {dialog.title}")
                            break
            else:
                # Публичный канал
                entity = await worker.client.get_entity(normalized)
                try:
                    await worker.client(JoinChannelRequest(entity))
                except:
                    pass
        except Exception as e:
            return {"status": "error", "detail": f"Ошибка при работе с каналом: {e}", "is_ban": False}
        
        if not entity:
            return {"status": "error", "detail": "Канал не найден. Нажмите 'Вступить во все' чтобы аккаунты вступили в приватные каналы.", "is_ban": False}
        
        # Вступаем в группу обсуждений
        try:
            full_channel = await worker.client(GetFullChannelRequest(entity))
            if full_channel.full_chat.linked_chat_id:
                linked_chat = await worker.client.get_entity(full_channel.full_chat.linked_chat_id)
                await worker.client(JoinChannelRequest(linked_chat))
                await asyncio.sleep(2)
        except Exception as e:
            err_str = str(e).lower()
            # Проверяем на бан при вступлении в группу обсуждений
            if any(p in err_str for p in ban_patterns):
                return {"status": "error", "detail": f"Аккаунт забанен в группе обсуждений: {e}", "is_ban": True}
            if 'already' not in err_str and 'user_already' not in err_str:
                pass  # Игнорируем, попробуем написать
        
        # Получаем последние посты и ищем тот, у которого есть комментарии
        messages = await worker.client.get_messages(entity, limit=10)
        if not messages:
            return {"status": "error", "detail": "Нет постов в канале", "is_ban": False}
        
        post = None
        discussion = None
        for msg in messages:
            if not msg or not msg.id:
                continue
            try:
                discussion = await worker.client(GetDiscussionMessageRequest(peer=entity, msg_id=msg.id))
                post = msg
                break  # Нашли пост с комментариями
            except Exception as e:
                err_str = str(e).lower()
                # Проверяем на бан при получении обсуждения
                if any(p in err_str for p in ban_patterns):
                    return {"status": "error", "detail": f"Аккаунт забанен: {e}", "is_ban": True}
                continue  # Пробуем следующий пост
        
        if not post:
            return {"status": "error", "detail": "Не найден пост с открытыми комментариями", "is_ban": False}
        
        # Генерируем комментарий через нейросеть
        from modules.comment_generator import CommentGenerator
        generator = CommentGenerator(api_key=None, db=db)  # API ключ берётся из БД
        
        image_bytes = None
        if post.photo:
            try:
                image_bytes = await worker.client.download_media(post.photo, bytes)
            except:
                pass
        
        comment = await generator.generate_comment_async(post.raw_text or "", image_bytes)
        
        if not comment:
            return {"status": "error", "detail": "Не удалось сгенерировать комментарий через нейросеть", "is_ban": False}
        
        # Отправляем комментарий
        try:
            result = await worker.client.send_message(entity=entity, message=comment, comment_to=post.id)
        except Exception as e:
            err_str = str(e).lower()
            # Проверяем на бан при отправке
            if any(p in err_str for p in ban_patterns):
                return {"status": "error", "detail": f"Аккаунт забанен при отправке: {e}", "is_ban": True}
            return {"status": "error", "detail": f"Ошибка отправки: {e}", "is_ban": False}
        
        # Сохраняем в базу
        db.save_comment(acc_id, normalized, comment, post.id, result.id)
        db.increment_stat(acc_id, 'comments')
        
        # Формируем ссылку на комментарий
        if normalized.startswith('+'):
            # Приватный канал - ссылка через c/channel_id
            # ID канала нужно преобразовать (убрать -100 префикс)
            channel_id = entity.id
            if channel_id < 0:
                channel_id = int(str(channel_id).replace('-100', ''))
            link = f"https://t.me/c/{channel_id}/{post.id}?comment={result.id}"
        else:
            link = f"https://t.me/{normalized}/{post.id}?comment={result.id}"
        
        return {
            "status": "success",
            "channel": normalized,
            "comment": comment,
            "post_id": post.id,
            "message_id": result.id,
            "account_id": acc_id,
            "link": link
        }
    
    # Пробуем отправить через каждый доступный аккаунт
    last_error = None
    tried_accounts = []
    
    for acc_id, worker in available_workers:
        tried_accounts.append(acc_id)
        print(f"[comment-now] Пробую аккаунт {acc_id} для {normalized}...")
        
        try:
            # Создаём корутину
            coro = send_comment_with_worker(acc_id, worker)
            future = worker.run_task(coro)
            
            if not future:
                # Воркер не запущен - закрываем корутину чтобы избежать warning
                coro.close()
                print(f"[comment-now] Аккаунт {acc_id} не запущен, пропускаю")
                continue
                
            result = future.result(timeout=60)
            
            if isinstance(result, dict):
                if result.get("status") == "success":
                    # Успех!
                    return result
                elif result.get("is_ban"):
                    # Бан - помечаем аккаунт и пробуем следующий
                    print(f"[comment-now] Аккаунт {acc_id} забанен в {normalized}: {result.get('detail')}")
                    db.mark_banned(acc_id, normalized)
                    last_error = result.get("detail")
                    continue
                else:
                    # Другая ошибка - не бан, возвращаем сразу
                    last_error = result.get("detail")
                    # Для некоторых ошибок пробуем следующий аккаунт
                    if "не найден" in last_error.lower() or "вступить" in last_error.lower():
                        continue
                    raise HTTPException(status_code=400, detail=last_error)
        except HTTPException:
            raise
        except Exception as e:
            err_str = str(e).lower()
            # Проверяем на бан в исключении
            if any(p in err_str for p in ban_patterns):
                print(f"[comment-now] Аккаунт {acc_id} забанен (exception): {e}")
                db.mark_banned(acc_id, normalized)
                last_error = str(e)
                continue
            last_error = str(e)
            continue
    
    # Все аккаунты не смогли отправить
    if last_error:
        raise HTTPException(
            status_code=400, 
            detail=f"Не удалось отправить комментарий. Попробовано аккаунтов: {len(tried_accounts)}. Последняя ошибка: {last_error}"
        )


# --- Comments API ---
@app.get("/comments")
async def get_comments(limit: int = 100, account_id: Optional[int] = None):
    """Возвращает список отправленных комментариев и сообщений в чатах"""
    comments = db.get_all_comments(limit=limit, account_id=account_id)
    # Добавляем ссылку на комментарий/сообщение
    for c in comments:
        message_type = c.get('message_type', 'comment')
        
        if message_type == 'chat':
            # Сообщение в чате - ссылка через c/chat_id/message_id
            chat_id = c.get('chat_id')
            message_id = c.get('message_id')
            if chat_id and message_id:
                # Преобразуем chat_id (убираем -100 если есть)
                chat_id_str = str(chat_id)
                if chat_id_str.startswith('-100'):
                    chat_id_str = chat_id_str[4:]
                elif chat_id_str.startswith('-'):
                    chat_id_str = chat_id_str[1:]
                c['link'] = f"https://t.me/c/{chat_id_str}/{message_id}"
            else:
                c['link'] = None
        else:
            # Комментарий к посту
            if c.get('channel') and c.get('post_id'):
                channel = c['channel'].lstrip('@')
                # Проверяем приватный ли канал
                if channel.startswith('+'):
                    # Приватный канал - нужен channel_id из базы
                    channel_info = db.get_channel_info(channel)
                    if channel_info and channel_info.get('channel_id'):
                        ch_id = channel_info['channel_id']
                        ch_id_str = str(ch_id)
                        if ch_id_str.startswith('-100'):
                            ch_id_str = ch_id_str[4:]
                        if c.get('message_id'):
                            c['link'] = f"https://t.me/c/{ch_id_str}/{c['post_id']}?comment={c['message_id']}"
                        else:
                            c['link'] = f"https://t.me/c/{ch_id_str}/{c['post_id']}"
                    else:
                        c['link'] = None
                else:
                    # Публичный канал
                    if c.get('message_id'):
                        c['link'] = f"https://t.me/{channel}/{c['post_id']}?comment={c['message_id']}"
                    else:
                        c['link'] = f"https://t.me/{channel}/{c['post_id']}"
            else:
                c['link'] = None
    return comments

@app.get("/comments/{comment_id}")
async def get_comment(comment_id: int):
    """Возвращает комментарий по ID"""
    comment = db.get_comment_by_id(comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    return comment

class CommentEdit(BaseModel):
    new_text: str

@app.put("/comments/{comment_id}")
async def edit_comment(comment_id: int, data: CommentEdit):
    """Редактирует комментарий в Telegram и обновляет в БД"""
    comment = db.get_comment_by_id(comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    
    # Проверяем наличие message_id
    if not comment.get('message_id'):
        raise HTTPException(status_code=400, detail="Невозможно редактировать: нет ID сообщения (старый комментарий)")
    
    account_id = comment['account_id']
    if account_id not in workers or not workers[account_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен. Запустите аккаунт для редактирования.")
    
    worker = workers[account_id]
    
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        
        # Получаем entity канала
        channel = await worker.client.get_entity(comment['channel'])
        
        # Получаем группу обсуждений (где находятся комментарии)
        full_channel = await worker.client(GetFullChannelRequest(channel))
        if full_channel.full_chat.linked_chat_id:
            discussion_group = await worker.client.get_entity(full_channel.full_chat.linked_chat_id)
        else:
            discussion_group = channel
        
        # Редактируем сообщение в группе обсуждений
        await worker.client.edit_message(
            entity=discussion_group,
            message=comment['message_id'],
            text=data.new_text
        )
        
        # Обновляем в БД
        db.update_comment_text(comment_id, data.new_text)
        
        return {"status": "success", "message": "Комментарий отредактирован"}
    except Exception as e:
        err_str = str(e).lower()
        if "can't write" in err_str or "forbidden" in err_str or "banned" in err_str:
            raise HTTPException(status_code=403, detail="Нет доступа к каналу (возможно бот забанен)")
        elif "message to edit not found" in err_str or "message not found" in err_str:
            raise HTTPException(status_code=404, detail="Сообщение не найдено (возможно удалено)")
        raise HTTPException(status_code=500, detail=f"Ошибка редактирования: {str(e)}")

@app.delete("/comments/{comment_id}")
async def delete_comment(comment_id: int):
    """Удаляет комментарий из Telegram и БД"""
    comment = db.get_comment_by_id(comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    
    account_id = comment['account_id']
    
    # Пробуем удалить из Telegram если аккаунт запущен и есть message_id
    if comment.get('message_id') and account_id in workers and workers[account_id].is_running:
        worker = workers[account_id]
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest
            
            channel = await worker.client.get_entity(comment['channel'])
            
            # Получаем группу обсуждений
            full_channel = await worker.client(GetFullChannelRequest(channel))
            if full_channel.full_chat.linked_chat_id:
                discussion_group = await worker.client.get_entity(full_channel.full_chat.linked_chat_id)
            else:
                discussion_group = channel
            
            await worker.client.delete_messages(discussion_group, [comment['message_id']])
        except Exception as e:
            # Не критично если не удалось удалить из Telegram
            pass
    
    # Удаляем из БД
    db.delete_comment_record(comment_id)
    return {"status": "deleted"}

@app.post("/discovery/start")
async def start_discovery(background_tasks: BackgroundTasks):
    # Берем первый работающий аккаунт для исследования
    active_worker = next((w for w in workers.values() if w.is_running), None)
    if not active_worker:
        raise HTTPException(status_code=400, detail="Для поиска каналов нужен хотя бы один запущенный аккаунт")
    
    explorer = ChannelExplorer(active_worker.client, db, active_worker.account_id)
    # Сохраняем ссылку на explorer в воркере для возможности остановки
    active_worker.channel_explorer = explorer
    # Запускаем в цикле воркера, так как клиент привязан к нему
    coro = explorer.run_discovery_cycle()
    future = active_worker.run_task(coro)
    if not future:
        coro.close()
        raise HTTPException(status_code=500, detail="Не удалось запустить поиск каналов")
    
    return {"status": "discovery_started"}

@app.post("/discovery/add-to-active")
async def add_to_active(channels: List[str]):
    """Активирует каналы в БД"""
    added = 0
    for ch in channels:
        # Нормализуем канал
        normalized = Config.normalize_channel(ch)
        if normalized:
            db.update_channel_status(normalized, 'active')
            added += 1
    
    return {"added": added}
    return {"added": added}


class ManualChannelAdd(BaseModel):
    channel: str
    title: str = ""

@app.post("/discovery/channels/add")
async def add_channel_manually(data: ManualChannelAdd):
    """Добавляет канал вручную в базу"""
    channel = data.channel.strip()
    print(f"[add_channel] Получен канал: {channel}")
    
    # Используем общую функцию нормализации
    channel = Config.normalize_channel(channel)
    print(f"[add_channel] После нормализации: {channel}")
    
    if not channel:
        raise HTTPException(status_code=400, detail="Название канала не может быть пустым")
    
    title = data.title.strip() or channel
    
    # Добавляем в базу найденных каналов
    is_new = db.add_found_channel(
        channel=channel,
        title=title,
        keyword="manual",
        source="manual",
        subs=0,
        views=0,
        can_comment=True,  # Предполагаем что комменты открыты
        min_subs=0  # Не фильтруем по подписчикам для ручного добавления
    )
    print(f"[add_channel] Добавлен в БД: is_new={is_new}")
    
    # Устанавливаем статус active
    db.update_channel_status(channel, 'active')
    
    return {"status": "added", "channel": channel, "is_new": is_new}

# --- Сброс статуса каналов и банов ---

@app.post("/discovery/channels/reset-closed")
async def reset_closed_channels():
    """
    Сбрасывает статус 'can_comment' на 1 (открыто) для всех каналов, где он был 0.
    Позволяет новым аккаунтам попробовать написать в них.
    """
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            # Сбрасываем только временные статусы. Permanent structural exclusions
            # намеренно остаются заблокированными для всех текущих и будущих аккаунтов.
            exclusion_guard = (
                "LOWER(channel) NOT IN "
                "(SELECT LOWER(channel) FROM channel_global_exclusions)"
            )
            cursor.execute(
                f"UPDATE found_channels SET can_comment = 1 "
                f"WHERE can_comment = 0 AND {exclusion_guard}"
            )
            count = cursor.rowcount
            cursor.execute(
                f"UPDATE found_channels SET status = 'active' "
                f"WHERE status = 'rejected' AND {exclusion_guard}"
            )
            rejected_count = cursor.rowcount
        
        return {
            "status": "ok", 
            "updated": count,
            "rejected_reset": rejected_count,
            "message": f"Сброшено {count} каналов в статус 'Открыто'. Бот попробует написать в них снова."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/accounts/{account_id}/reset-bans")
async def reset_account_bans(account_id: int):
    """
    Удаляет записи о банах для конкретного аккаунта.
    Полезно для новых аккаунтов, чтобы они не пропускали каналы,
    в которых был забанен старый аккаунт.
    """
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM channel_bans WHERE account_id = ?", (account_id,))
            count = cursor.rowcount
            # Также сбрасываем счетчик ошибок здоровья
            cursor.execute(
                "UPDATE accounts SET consecutive_errors = 0, health_status = 'healthy' WHERE id = ?", 
                (account_id,)
            )
        
        return {"status": "ok", "removed": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/logs")
async def get_logs(
    limit: int = 50,
    account_id: Optional[int] = None,
    level: Optional[str] = None,
    search: Optional[str] = None,
):
    return db.get_logs(limit=limit, account_id=account_id, level=level, search=search)

@app.get("/logs/summary")
async def logs_summary(hours: int = 24):
    """Счётчики логов по уровням за N часов — для бейджей в панели."""
    return db.get_log_level_counts(hours=hours)

@app.get("/stats/export")
async def export_stats(account_id: int, days: int = 7):
    """
    Экспортирует статистику аккаунта в JSON формате.
    
    Args:
        account_id: ID аккаунта
        days: Количество дней для выборки (по умолчанию 7)
    """
    # Проверяем существование аккаунта
    accounts = db.get_accounts()
    account = next((a for a in accounts if a['id'] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    
    # Собираем статистику
    daily_stats = db.get_daily_stats(account_id, days)
    aggregated = db.get_aggregated_daily_stats(account_id, days)
    success_rate = db.get_success_rate(account_id, days)
    health = db.get_account_health(account_id)
    
    return {
        "account_id": account_id,
        "phone": account['phone'],
        "period_days": days,
        "success_rate": round(success_rate, 2),
        "health": health,
        "daily_aggregated": aggregated,
        "daily_by_channel": daily_stats
    }

@app.get("/stats/summary")
async def get_stats_summary(account_id: int):
    """Возвращает краткую сводку статистики аккаунта"""
    accounts = db.get_accounts()
    account = next((a for a in accounts if a['id'] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    
    summary = db.get_stats_summary(account_id)
    success_rate = db.get_success_rate(account_id, 7)
    health = db.get_account_health(account_id)
    
    return {
        "account_id": account_id,
        **summary,
        "success_rate_7d": round(success_rate, 2),
        "health_status": health['health_status']
    }

@app.get("/health")
async def health_check():
    """Расширенный health-check для мониторинга на хостинге."""
    running_workers = sum(1 for w in workers.values() if w.is_running)
    try:
        accounts_count = len(db.get_accounts())
    except Exception:
        accounts_count = -1
    return {
        "status": "ok",
        "workers_running": running_workers,
        "workers_total": len(workers),
        "accounts": accounts_count,
        "global_pause": global_pause,
        "watcher_running": bool(health_watcher and health_watcher.is_running),
        "uptime_since": SERVER_STARTUP_TIME,
    }

@app.get("/stats/global")
async def get_global_stats():
    """Возвращает глобальную статистику по всем каналам и аккаунтам"""
    stats = db.get_global_stats()
    
    # Добавляем каналы, найденные с момента запуска
    stats["new_since_startup"] = db.get_channels_found_since_startup(SERVER_STARTUP_TIME)
    stats["server_startup_time"] = SERVER_STARTUP_TIME
    
    # Проверяем и удаляем мёртвые каналы
    dead_removed = db.check_and_remove_dead_channels()
    if dead_removed > 0:
        stats["dead_channels_removed"] = dead_removed
    
    return stats

@app.get("/stats/24h")
async def get_stats_24h():
    """Возвращает статистику за последние 24 часа"""
    return db.get_stats_24h()

@app.post("/admin/clear-stats")
async def clear_all_stats():
    """
    Полностью очищает статистику: комментарии, лайки, логи.
    Использовать, если данные устарели или нужно начать с чистого листа.
    """
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Удаляем отправленные комментарии
            cursor.execute("DELETE FROM sent_comments")
            comments_deleted = cursor.rowcount
            
            # 2. Сбрасываем счетчики лайков/комментов у аккаунтов
            cursor.execute("DELETE FROM account_stats")
            stats_deleted = cursor.rowcount
            
            # 3. Очищаем дневную статистику
            cursor.execute("DELETE FROM daily_stats")
            daily_deleted = cursor.rowcount
            
            # 4. Очищаем логи
            cursor.execute("DELETE FROM logs")
            logs_deleted = cursor.rowcount
            
            # 5. Очищаем обработанные посты (чтобы боты могли писать заново)
            cursor.execute("DELETE FROM processed_posts")
            posts_deleted = cursor.rowcount
            
            # 6. Сбрасываем список банов для всех аккаунтов
            cursor.execute("DELETE FROM channel_bans")
            bans_deleted = cursor.rowcount
            
            # 7. Сбрасываем статусы "закрытых комментариев" у каналов
            cursor.execute("UPDATE found_channels SET can_comment = 1, status = 'active'")
            channels_updated = cursor.rowcount
            
            # 8. Сбрасываем health статусы аккаунтов
            cursor.execute("UPDATE accounts SET consecutive_errors = 0, health_status = 'healthy'")
            accounts_updated = cursor.rowcount
            
        return {
            "status": "ok",
            "message": f"База очищена. Удалено комментариев: {comments_deleted}.",
            "details": {
                "comments": comments_deleted,
                "stats": stats_deleted,
                "daily_stats": daily_deleted,
                "logs": logs_deleted,
                "processed_posts": posts_deleted,
                "bans": bans_deleted,
                "channels_updated": channels_updated,
                "accounts_updated": accounts_updated
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/channels/available/{account_id}")
async def get_available_channels(account_id: int):
    """Возвращает каналы, доступные для конкретного аккаунта"""
    accounts = db.get_accounts()
    account = next((a for a in accounts if a['id'] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    
    channels = db.get_available_channels_for_account(account_id)
    banned = db.get_banned_channels_for_account(account_id)
    
    return {
        "available_count": len(channels),
        "banned_count": len(banned),
        "channels": channels[:50],  # Лимит для UI
        "banned_channels": banned[:20]
    }

@app.get("/channels/{channel}/bans")
async def get_channel_ban_stats(channel: str):
    """Возвращает статистику банов для канала"""
    stats = db.get_ban_stats_for_channel(channel)
    banned_accounts = db.get_accounts_banned_in_channel(channel)
    
    return {
        **stats,
        "banned_account_ids": banned_accounts
    }


# --- Bulk Profile Management ---
class BulkProfileUpdate(BaseModel):
    account_ids: Optional[List[int]] = None  # None = все аккаунты
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    bio: Optional[str] = None
    avatar_base64: Optional[str] = None

@app.post("/accounts/bulk-profile")
async def bulk_update_profiles(data: BulkProfileUpdate):
    """
    Массово обновляет профили нескольких аккаунтов.
    Если account_ids не указан — обновляет все запущенные аккаунты.
    """
    from modules.profile_manager import ProfileManager
    
    # Определяем какие аккаунты обновлять
    if data.account_ids:
        target_ids = data.account_ids
    else:
        # Все запущенные аккаунты
        target_ids = [acc_id for acc_id, w in workers.items() if w.is_running]
    
    if not target_ids:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов для обновления")
    
    results = {}
    
    for acc_id in target_ids:
        if acc_id not in workers or not workers[acc_id].is_running:
            results[acc_id] = {"status": "skipped", "reason": "Аккаунт не запущен"}
            continue
        
        worker = workers[acc_id]
        
        try:
            pm = ProfileManager(worker.client, db, acc_id)
            
            coro = pm.update_full_profile(
                first_name=data.first_name,
                last_name=data.last_name,
                bio=data.bio,
                avatar_base64=data.avatar_base64
            )
            future = worker.run_task(coro)
            
            if future:
                update_results = future.result(timeout=60)
                results[acc_id] = {"status": "success", "results": update_results}
            else:
                coro.close()
                results[acc_id] = {"status": "error", "reason": "Не удалось запустить задачу"}
        except Exception as e:
            results[acc_id] = {"status": "error", "reason": str(e)}
    
    # Подсчитываем статистику
    success_count = sum(1 for r in results.values() if r.get("status") == "success")
    
    return {
        "total": len(target_ids),
        "success": success_count,
        "failed": len(target_ids) - success_count,
        "details": results
    }


# === Models for new modules ===
class ChannelCreateRequest(BaseModel):
    title: str
    about: str = ""
    username_base: str
    topic: str = ""
    avatar_base64: Optional[str] = None
    publish_warmup: bool = False  # По умолчанию НЕ постить авто-посты при создании

class ChannelPostRequest(BaseModel):
    text: str
    media_base64: Optional[str] = None
    media_type: Optional[str] = None
    format_type: str = "md"

class GeneratePostRequest(BaseModel):
    topic: str

class PostQueueRequest(BaseModel):
    text: str
    scheduled_at: Optional[str] = None
    media_base64: Optional[str] = None
    media_type: Optional[str] = None
    format_type: str = "md"

class InviteParseRequest(BaseModel):
    chat_id: str

class InviteStartRequest(BaseModel):
    # channel may be numeric id, @username or t.me link (resolved at invite time)
    channel_id: Union[int, str]
    source_chat_id: Optional[str] = None
    # explicit user targets only (ids / @usernames) — never auto-broaden
    user_ids: Optional[List[Union[int, str]]] = None
    daily_limit: Optional[int] = None

class MassSendDMRequest(BaseModel):
    # explicit string/int targets only
    user_ids: List[Union[int, str]]
    message_template: str
    media_base64: Optional[str] = None
    hourly_limit: Optional[int] = None
    # источники целей: добавить всех спарсенных из БД инвайтера
    use_parsed: Optional[bool] = False
    # спарсить участников чата-донора перед рассылкой и добавить в цели
    parse_from_chat: Optional[str] = None

class MassSendGroupRequest(BaseModel):
    chat_ids: List[Union[int, str]]
    message_template: str
    media_base64: Optional[str] = None
    hourly_limit: Optional[int] = None


class TelegramTargetsPreviewRequest(BaseModel):
    targets: List[Union[int, str]]
    target_type: Optional[str] = None


# === Channel Creator API ===

@app.post("/accounts/{acc_id}/channel/create")
async def create_channel(acc_id: int, data: ChannelCreateRequest):
    """Создает новый канал для аккаунта"""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    
    worker = workers[acc_id]
    
    try:
        avatar_bytes = None
        if data.avatar_base64:
            import base64 as b64
            header_end = data.avatar_base64.find(',')
            b64_data = data.avatar_base64[header_end+1:] if header_end != -1 else data.avatar_base64
            avatar_bytes = b64.b64decode(b64_data)
        
        async def _create():
            return await worker.channel_creator.create_and_setup_channel(
                title=data.title,
                about=data.about,
                username_base=data.username_base,
                topic=data.topic,
                avatar_bytes=avatar_bytes,
                publish_warmup=data.publish_warmup
            )
        
        future = worker.run_task(_create())
        if future:
            result = future.result(timeout=300)  # 5 min timeout for warmup posts
            if result:
                return {"status": "success", **result}
            raise HTTPException(status_code=500, detail="Не удалось создать канал")
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/accounts/{acc_id}/channels")
async def get_own_channels(acc_id: int):
    """Получает список собственных каналов аккаунта"""
    return db.get_own_channels(acc_id)


# === Channel Poster API ===

@app.post("/accounts/{acc_id}/channel/{channel_id}/post")
async def post_to_channel(acc_id: int, channel_id: int, data: ChannelPostRequest):
    """Публикует пост в канал"""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    
    worker = workers[acc_id]
    
    try:
        media_bytes = None
        if data.media_base64:
            import base64 as b64
            header_end = data.media_base64.find(',')
            b64_data = data.media_base64[header_end+1:] if header_end != -1 else data.media_base64
            media_bytes = b64.b64decode(b64_data)
        
        async def _post():
            if media_bytes:
                return await worker.channel_poster.post_message_with_bytes(
                    channel_id, data.text, media_bytes, data.media_type, data.format_type
                )
            else:
                return await worker.channel_poster.post_message(
                    channel_id, data.text, format_type=data.format_type
                )
        
        future = worker.run_task(_post())
        if future:
            result = future.result(timeout=60)
            if result:
                return {"status": "success", "message_id": result.id}
            raise HTTPException(status_code=500, detail="Не удалось отправить пост")
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/accounts/{acc_id}/channel/{channel_id}/generate-post")
async def generate_post(acc_id: int, channel_id: int, data: GeneratePostRequest):
    """Генерирует пост через AI и публикует"""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    
    worker = workers[acc_id]
    
    try:
        async def _gen():
            return await worker.channel_poster.generate_and_post(channel_id, data.topic)
        
        future = worker.run_task(_gen())
        if future:
            result = future.result(timeout=60)
            if result:
                return {"status": "success", "message_id": result.id, "text": result.text}
            raise HTTPException(status_code=500, detail="Не удалось сгенерировать пост")
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/accounts/{acc_id}/channel/{channel_id}/queue")
async def get_post_queue(acc_id: int, channel_id: int):
    """Получает очередь постов для канала"""
    posts = db.get_pending_posts(acc_id)
    # Filter by channel_id
    return [p for p in posts if p.get('channel_id') == channel_id]


@app.post("/accounts/{acc_id}/channel/{channel_id}/queue")
async def add_to_post_queue(acc_id: int, channel_id: int, data: PostQueueRequest):
    """Добавляет пост в очередь"""
    media_path = None
    if data.media_base64:
        import base64 as b64
        import uuid
        media_dir = os.path.join(ROOT_DIR, 'data', 'media')
        os.makedirs(media_dir, exist_ok=True)
        header_end = data.media_base64.find(',')
        b64_data = data.media_base64[header_end+1:] if header_end != -1 else data.media_base64
        media_bytes = b64.b64decode(b64_data)
        ext = '.jpg' if data.media_type == 'photo' else '.mp4' if data.media_type == 'video' else '.bin'
        filename = f"{uuid.uuid4().hex}{ext}"
        media_path = os.path.join(media_dir, filename)
        with open(media_path, 'wb') as f:
            f.write(media_bytes)

    post_id = db.add_post_to_queue(
        acc_id, channel_id, data.text,
        media_path=media_path, media_type=data.media_type,
        format_type=data.format_type, scheduled_at=data.scheduled_at
    )
    return {"status": "success", "post_id": post_id}


# === Inviter API ===

@app.post("/accounts/{acc_id}/inviter/parse")
async def parse_users(acc_id: int, data: InviteParseRequest):
    """Парсит пользователей только из явно указанного чата-источника."""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    if _account_is_paused(acc_id):
        raise HTTPException(status_code=429, detail="Аккаунт на паузе / rate-limit")

    worker = workers[acc_id]

    try:
        async def _parse():
            return await worker.inviter.parse_users_from_chat(data.chat_id)

        future = worker.run_task(_parse())
        if future:
            # Пагинация больших чатов может занимать больше минуты.
            count = future.result(timeout=300)
            return {"status": "success", "parsed_count": count, "source": data.chat_id}
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    except HTTPException:
        raise
    except FutureTimeoutError:
        raise HTTPException(
            status_code=202,
            detail="Парсинг продолжается в фоне; обновите список кандидатов через несколько минут",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/accounts/{acc_id}/inviter/users")
async def get_parsed_users_api(
    acc_id: int,
    limit: int = 100,
    offset: int = 0,
    source_chat_id: Optional[int] = None,
):
    """Получает кандидатов; при source_chat_id — строго из этого источника."""
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    return db.get_parsed_users(
        acc_id,
        limit=safe_limit,
        offset=safe_offset,
        source_chat_id=source_chat_id,
    )


@app.post("/accounts/{acc_id}/inviter/start")
async def start_invite(acc_id: int, data: InviteStartRequest):
    """Запускает одну ограниченную сессию инвайтинга с явной целью и пулом."""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    if _account_is_paused(acc_id):
        remaining = db.get_account_pause_seconds(acc_id)
        raise HTTPException(
            status_code=429,
            detail=f"Аккаунт на паузе / rate-limit; осталось около {remaining} сек.",
        )

    worker = workers[acc_id]
    effective_limit = _clamp_limit(data.daily_limit, Config.INVITER_DAILY_LIMIT)

    try:
        async def _invite():
            # Явный список имеет приоритет и никогда не расширяется из БД.
            if data.user_ids:
                return await worker.inviter.invite_users_to_channel(
                    data.channel_id,
                    data.user_ids,
                    daily_limit=effective_limit,
                )
            if data.source_chat_id:
                return await worker.inviter.run_invite_session(
                    data.channel_id,
                    data.source_chat_id,
                    limit=effective_limit,
                )

            # Осознанный fallback: только уже спарсенные пользователи этого аккаунта.
            users = db.get_parsed_users(acc_id, limit=effective_limit)
            user_ids = [u['user_id'] for u in users]
            if not user_ids:
                return {
                    "status": "failed",
                    "success": 0,
                    "errors": 0,
                    "skipped": 0,
                    "total": 0,
                    "last_error": "no_candidates",
                }
            return await worker.inviter.invite_users_to_channel(
                data.channel_id,
                user_ids,
                daily_limit=effective_limit,
            )

        future = worker.run_task(_invite())
        if future:
            return {
                "status": "started",
                "message": "Инвайт запущен; прогресс доступен в статистике инвайтера.",
                "effective_daily_limit": effective_limit,
            }
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/accounts/{acc_id}/inviter/stats")
async def get_invite_stats_api(acc_id: int):
    """Получает накопительную статистику и прогресс текущей сессии."""
    stats = db.get_invite_stats(acc_id)
    worker = workers.get(acc_id)
    if worker and worker.is_running and getattr(worker, "inviter", None):
        try:
            stats["progress"] = worker.inviter.get_progress()
        except Exception:
            stats["progress"] = None
    else:
        stats["progress"] = None
    stats["pause_seconds"] = db.get_account_pause_seconds(acc_id)
    return stats


@app.get("/accounts/{acc_id}/inviter/chats")
async def get_available_chats(acc_id: int):
    """Получает список доступных чатов для парсинга"""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    
    worker = workers[acc_id]
    
    try:
        async def _chats():
            return await worker.inviter.get_available_chats()
        
        future = worker.run_task(_chats())
        if future:
            return future.result(timeout=30)
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Mass Send API ===

@app.post("/telegram-targets/preview")
async def telegram_targets_preview(data: TelegramTargetsPreviewRequest):
    """Syntax-only preview/normalizer for explicit Telegram targets (no network)."""
    target_type = data.target_type if data.target_type in ("user", "group") else None
    return preview_targets(data.targets, target_type=target_type)


@app.post("/accounts/{acc_id}/mass-send/dm")
async def start_dm_campaign(acc_id: int, data: MassSendDMRequest):
    """Запускает рассылку в ЛС по явному списку targets."""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    if _account_is_paused(acc_id):
        raise HTTPException(status_code=429, detail="Аккаунт на паузе / rate-limit")

    worker = workers[acc_id]

    try:
        valid = _normalize_explicit_targets(data.user_ids, target_type="user")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    hourly_limit = _clamp_limit(data.hourly_limit, Config.MASS_SEND_HOURLY_LIMIT)
    # Materialize normalized originals for the sender (ordered unique)
    normalized_targets = [t.original for t in valid]

    # Фича A: подгрузка целей из базы спарсенных пользователей инвайтера
    if data.parse_from_chat:
        try:
            async def _parse():
                return await worker.inviter.parse_users_from_chat(data.parse_from_chat)
            future = worker.run_task(_parse())
            if future:
                # НЕ future.result() — он заблокирует event loop FastAPI на весь
                # парсинг. wrap_future ждёт completion асинхронно.
                await asyncio.wrap_future(future)
            else:
                raise RuntimeError("Воркер недоступен")
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Не удалось спарсить участников чата '{data.parse_from_chat}': {e}",
            )

    if data.use_parsed or data.parse_from_chat:
        parsed_users = db.get_parsed_users(acc_id, limit=10000)
        for u in parsed_users:
            username = (u.get("username") or "").strip()
            if username:
                normalized_targets.append("@" + username.lstrip("@"))
            elif u.get("user_id"):
                normalized_targets.append(u["user_id"])

    # Дедупликация с сохранением порядка
    seen = set()
    deduped_targets = []
    for t in normalized_targets:
        key = str(t).lower() if isinstance(t, str) else t
        if key in seen:
            continue
        seen.add(key)
        deduped_targets.append(t)
    normalized_targets = deduped_targets

    if not normalized_targets:
        raise HTTPException(status_code=400, detail="Нет целей для рассылки")

    try:
        campaign_id = db.add_mass_send_campaign(
            acc_id, f"DM Campaign {time.time():.0f}",
            data.message_template, target_type="dm",
            total_targets=len(normalized_targets),
        )

        media_path = None
        if data.media_base64:
            import base64 as b64
            import uuid
            media_dir = os.path.join(ROOT_DIR, 'data', 'media')
            os.makedirs(media_dir, exist_ok=True)
            header_end = data.media_base64.find(',')
            b64_data = data.media_base64[header_end+1:] if header_end != -1 else data.media_base64
            media_bytes = b64.b64decode(b64_data)
            filename = f"{uuid.uuid4().hex}.bin"
            media_path = os.path.join(media_dir, filename)
            with open(media_path, 'wb') as f:
                f.write(media_bytes)

        async def _send():
            return await worker.mass_sender.run_dm_campaign(
                campaign_id, normalized_targets, data.message_template,
                media_path=media_path, hourly_limit=hourly_limit
            )

        future = worker.run_task(_send())
        if future:
            return {
                "status": "started",
                "campaign_id": campaign_id,
                "targets": len(normalized_targets),
                "hourly_limit": hourly_limit,
            }
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/accounts/{acc_id}/mass-send/groups")
async def start_group_campaign(acc_id: int, data: MassSendGroupRequest):
    """Запускает рассылку в группы по явному списку targets."""
    if acc_id not in workers or not workers[acc_id].is_running:
        raise HTTPException(status_code=400, detail="Аккаунт не запущен")
    if _account_is_paused(acc_id):
        raise HTTPException(status_code=429, detail="Аккаунт на паузе / rate-limit")

    worker = workers[acc_id]

    try:
        valid = _normalize_explicit_targets(data.chat_ids, target_type="group")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    hourly_limit = _clamp_limit(data.hourly_limit, Config.MASS_SEND_HOURLY_LIMIT)
    normalized_targets = [t.original for t in valid]

    try:
        campaign_id = db.add_mass_send_campaign(
            acc_id, f"Group Campaign {time.time():.0f}",
            data.message_template, target_type="group",
            total_targets=len(normalized_targets),
        )

        media_path = None
        if data.media_base64:
            import base64 as b64
            import uuid
            media_dir = os.path.join(ROOT_DIR, 'data', 'media')
            os.makedirs(media_dir, exist_ok=True)
            header_end = data.media_base64.find(',')
            b64_data = data.media_base64[header_end+1:] if header_end != -1 else data.media_base64
            media_bytes = b64.b64decode(b64_data)
            filename = f"{uuid.uuid4().hex}.bin"
            media_path = os.path.join(media_dir, filename)
            with open(media_path, 'wb') as f:
                f.write(media_bytes)

        async def _send():
            return await worker.mass_sender.run_group_campaign(
                campaign_id, normalized_targets, data.message_template,
                media_path=media_path, hourly_limit=hourly_limit
            )

        future = worker.run_task(_send())
        if future:
            return {
                "status": "started",
                "campaign_id": campaign_id,
                "targets": len(normalized_targets),
                "hourly_limit": hourly_limit,
            }
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/accounts/{acc_id}/mass-send/campaigns")
async def get_campaigns(acc_id: int):
    """Получает список кампаний рассылки"""
    return db.get_mass_send_campaigns(acc_id)


@app.get("/accounts/{acc_id}/mass-send/campaigns/{campaign_id}/stats")
async def get_campaign_stats_api(acc_id: int, campaign_id: int):
    """Получает статистику + lifecycle кампании"""
    return db.get_campaign_stats(campaign_id)


# === Channel Filter API ===

class FilterCriteria(BaseModel):
    min_subscribers: int = 1000
    min_avg_views: int = 300
    max_days_since_last_post: int = 7
    require_open_comments: bool = True
    junk_filter: bool = True
    min_posts_per_week: int = 2

class FilterRequest(BaseModel):
    channels: Optional[List[str]] = None
    criteria: Optional[FilterCriteria] = None

@app.post("/api/channels/filter/start")
async def start_channel_filter(request: FilterRequest):
    """Запускает массовую фильтрацию каналов. Неблокирующий - возвращает сразу, прогресс по /progress."""
    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running and w.channel_filter]
    if not running_workers:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов с модулем фильтрации")

    acc_id, worker = running_workers[0]
    criteria_dict = request.criteria.model_dump() if request.criteria else None

    if request.channels:
        # Фильтруем указанный список каналов
        channels = [Config.normalize_channel(ch) for ch in request.channels if ch.strip()]
        channels = [ch for ch in channels if ch]
        total = len(channels)

        async def _run():
            return await worker.channel_filter.bulk_filter(channels, criteria_dict)

        future = worker.run_task(_run())
        if not future:
            raise HTTPException(status_code=500, detail="Воркер недоступен")
        return {"status": "started", "total": total}
    else:
        # Фильтруем всю БД
        db_channels = db.get_found_channels(limit=5000, only_open_comments=False)
        total = len(db_channels)

        async def _run_db():
            return await worker.channel_filter.filter_existing_db(criteria_dict)

        future = worker.run_task(_run_db())
        if not future:
            raise HTTPException(status_code=500, detail="Воркер недоступен")
        return {"status": "started", "total": total}


@app.post("/api/channels/filter/import")
async def import_and_filter_channels(file: UploadFile = File(...), criteria: str = Form("{}")):
    """Загружает файл со списком каналов и фильтрует. Формат: одна строка = один канал."""
    import json as json_mod

    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running and w.channel_filter]
    if not running_workers:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов с модулем фильтрации")

    acc_id, worker = running_workers[0]

    # Читаем файл
    content = await file.read()
    text_content = content.decode('utf-8', errors='ignore')

    # Парсим критерии
    try:
        criteria_dict = json_mod.loads(criteria) if criteria and criteria.strip() != '{}' else None
    except (json_mod.JSONDecodeError, ValueError):
        criteria_dict = None

    # Подсчитываем количество каналов для ответа
    lines = [l.strip() for l in text_content.strip().splitlines() if l.strip() and not l.strip().startswith('#')]
    total = len(lines)

    async def _run_import():
        return await worker.channel_filter.import_and_filter(text_content, criteria_dict)

    future = worker.run_task(_run_import())
    if not future:
        raise HTTPException(status_code=500, detail="Воркер недоступен")
    return {"status": "started", "total": total}


@app.get("/api/channels/filter/progress")
async def get_filter_progress():
    """Возвращает прогресс текущей фильтрации."""
    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running and w.channel_filter]
    if not running_workers:
        return {"running": False, "processed": 0, "total": 0, "passed": 0, "rejected": 0, "errors": 0}

    _, worker = running_workers[0]
    return worker.channel_filter._progress


@app.get("/api/channels/filter/results")
async def get_filter_results():
    """Возвращает результаты последней фильтрации."""
    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running and w.channel_filter]
    if not running_workers:
        return {"total": 0, "passed": 0, "rejected": 0, "errors": 0, "results": []}

    _, worker = running_workers[0]
    results = worker.channel_filter.get_results()
    passed = sum(1 for r in results if r['status'] == 'passed')
    rejected = sum(1 for r in results if r['status'] == 'rejected')
    errors = sum(1 for r in results if r['status'] == 'error')
    return {
        "total": len(results),
        "passed": passed,
        "rejected": rejected,
        "errors": errors,
        "results": results,
    }


@app.post("/api/channels/filter/apply")
async def apply_filter_results():
    """Применяет результаты фильтрации - удаляет rejected каналы из БД."""
    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running and w.channel_filter]
    if not running_workers:
        raise HTTPException(status_code=400, detail="Нет запущенных аккаунтов с модулем фильтрации")

    _, worker = running_workers[0]

    async def _apply():
        return await worker.channel_filter.apply_results(remove_rejected=True)

    future = worker.run_task(_apply())
    if not future:
        raise HTTPException(status_code=500, detail="Воркер недоступен")

    try:
        result = future.result(timeout=30)
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/channels/filter/stop")
async def stop_channel_filter():
    """Останавливает текущую фильтрацию."""
    running_workers = [(acc_id, w) for acc_id, w in workers.items() if w.is_running and w.channel_filter]
    if not running_workers:
        return {"status": "not_running"}

    _, worker = running_workers[0]
    worker.channel_filter.stop()
    return {"status": "stopped"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)


# === Web UI (отдаём frontend из /static) ===========================
# Этот блок должен быть в самом конце, чтобы не перехватывать API-роуты.

# /static/* — отдаём ассеты (CSS, JS, картинки)
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def serve_index():
    """Главная страница — однофайловый дашборд."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({
        "error": "Frontend не собран. Файл static/index.html не найден.",
        "api_docs": "/docs",
    }, status_code=404)


@app.get("/favicon.ico")
async def favicon():
    fav = os.path.join(STATIC_DIR, "favicon.svg")
    if os.path.exists(fav):
        return FileResponse(fav, media_type="image/svg+xml")
    return JSONResponse({}, status_code=204)


@app.get("/auth-status")
async def auth_status():
    """Сообщает фронту, нужна ли авторизация (для login-формы)."""
    return {"auth_required": bool(DASHBOARD_TOKEN)}


@app.get("/config-status")
async def config_status():
    """Сообщает фронту, заданы ли API ID/Hash (сохранённые в БД или из 1.envv).
    Если да — при импорте сессий не нужно вводить их вручную."""
    saved_id = db.get_setting('telegram_api_id', None)
    saved_hash = db.get_setting('telegram_api_hash', None)
    configured = bool((saved_id and saved_hash) or (Config.API_ID and Config.API_HASH))
    return {"api_configured": configured}
