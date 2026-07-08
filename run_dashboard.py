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
import subprocess
import sys
import threading
import time
import webbrowser


def _ensure_dependencies():
    """
    Проверяет, что ключевые зависимости установлены. Если нет — ставит их
    из requirements.txt в текущий Python автоматически.
    Так `python run_dashboard.py` работает даже без ручной установки.
    """
    try:
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401
        import telethon  # noqa: F401
        return
    except ImportError:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    req = os.path.join(here, 'requirements.txt')
    if not os.path.exists(req):
        print("❌ requirements.txt не найден — не могу установить зависимости.")
        sys.exit(1)

    print("=" * 52)
    print("  📦 Первый запуск: устанавливаю зависимости...")
    print("  (это займёт 1-2 минуты, нужен интернет)")
    print("=" * 52)
    try:
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', req]
        )
    except subprocess.CalledProcessError:
        print("\n❌ Не удалось установить зависимости.")
        print("   Установи вручную:  python -m pip install -r requirements.txt")
        sys.exit(1)
    print("\n✅ Зависимости установлены.\n")


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

    # Ставим зависимости, если их ещё нет (uvicorn/fastapi/telethon и т.д.)
    _ensure_dependencies()

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
