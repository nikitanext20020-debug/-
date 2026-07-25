#!/usr/bin/env python3
"""
Точка входа для запуска NEURO.CORE на хостинге (Render / Railway / VPS / Docker).

Что делает:
- Запускает FastAPI-приложение из backend.main:app через uvicorn.
- Слушает 0.0.0.0:$PORT (по умолчанию 8000).
- ChannelHealthWatcher и (опциональный) автостарт активных воркеров
  поднимаются автоматически в lifespan приложения.

Переменные окружения:
- PORT                    — порт (по умолчанию 8000)
- HOST                    — bind-адрес (по умолчанию 0.0.0.0)
- DASHBOARD_TOKEN         — токен для защиты дашборда (если не задан, открыт)
- DASHBOARD_ORIGIN        — список разрешённых Origin для CORS, через запятую
- AUTOSTART_WORKERS       — 1/0, авто-стартовать активные воркеры (по умолч. 1)
- LOG_LEVEL               — uvicorn log level: critical|error|warning|info|debug

Запуск:
    python host.py
    # или
    uvicorn backend.main:app --host 0.0.0.0 --port 8000
"""
import os
import sys

# Ensure project root is on sys.path (нужно для `from backend.main import app`)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def main():
    # Грузим .env-подобный файл если он есть (1.envv) — Config делает это сам,
    # но дублируем тут чтобы PORT/DASHBOARD_TOKEN тоже подцепились.
    try:
        from dotenv import load_dotenv
        load_dotenv('1.envv')
    except Exception:
        pass

    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '8000'))
    log_level = os.getenv('LOG_LEVEL', 'info')

    import uvicorn

    print(f"🚀 NEURO.CORE стартует на http://{host}:{port}")
    if os.getenv('DASHBOARD_TOKEN', '').strip():
        print("🔐 Авторизация ВКЛЮЧЕНА (DASHBOARD_TOKEN задан)")
    else:
        print("⚠️  DASHBOARD_TOKEN не задан — дашборд открыт всем")

    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        log_level=log_level,
        # Workers всегда 1 — у нас есть BotWorker'ы внутри процесса (threads
        # с собственными event loop'ами), множить копии fastapi-процессов
        # нельзя: у каждого будет свой watcher и свои Telethon-сессии.
        workers=1,
        # На bare metal не нужен reload; на dev-машине лучше использовать
        # `uvicorn backend.main:app --reload`
        reload=False,
        access_log=False,
        proxy_headers=True,        # для работы за reverse-proxy
        forwarded_allow_ips='*',
    )


if __name__ == '__main__':
    main()
