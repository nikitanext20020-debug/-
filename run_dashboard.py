#!/usr/bin/env python3
"""
Локальный запуск NEURO.CORE на своём ПК (Windows / macOS / Linux).

Что делает:
- Поднимает FastAPI-бэкенд (backend.main:app) через uvicorn.
- Бэкенд сам отдаёт дашборд (static/index.html) — отдельный фронтенд не нужен.
- Открывает браузер на http://localhost:<PORT> автоматически.

Запуск:
    python run_dashboard.py

Настройки берутся из файла 1.envv (скопируй из 1.envv.example и заполни).
Порт можно переопределить через переменную окружения PORT (по умолчанию 8000).
"""
import os
import sys
import threading
import time
import webbrowser


def _open_browser_later(url: str, delay: float = 2.0):
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def main():
    # Гарантируем, что корень проекта в sys.path (для `from backend.main import app`)
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    os.chdir(here)

    # Грузим 1.envv (Config тоже делает это сам, но PORT/HOST нужны здесь).
    try:
        from dotenv import load_dotenv
        load_dotenv('1.envv')
    except Exception:
        pass

    host = os.getenv('HOST', '127.0.0.1')
    port = int(os.getenv('PORT', '8000'))

    # Для авто-открытия браузера используем localhost, даже если слушаем 0.0.0.0
    browser_host = 'localhost' if host in ('0.0.0.0', '127.0.0.1') else host
    url = f"http://{browser_host}:{port}"

    print("=" * 52)
    print("  🚀 NEURO.CORE — Нейрокомментинг Dashboard")
    print("=" * 52)
    print(f"  Адрес:   {url}")
    if os.getenv('DASHBOARD_TOKEN', '').strip():
        print("  Доступ:  🔐 защищён DASHBOARD_TOKEN")
    else:
        print("  Доступ:  ⚠️  открыт (DASHBOARD_TOKEN не задан)")
    print("  Остановить: Ctrl+C")
    print("=" * 52)

    _open_browser_later(url)

    import uvicorn
    try:
        uvicorn.run(
            "backend.main:app",
            host=host,
            port=port,
            log_level=os.getenv('LOG_LEVEL', 'info'),
            workers=1,
            reload=False,
            access_log=False,
        )
    except KeyboardInterrupt:
        print("\n🛑 Остановка NEURO.CORE...")


if __name__ == "__main__":
    main()
