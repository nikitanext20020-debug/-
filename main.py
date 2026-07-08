"""
Vercel entrypoint (default location).

Vercel's Python/FastAPI detector only scans default locations for the ASGI
`app` variable (root `main.py`, `app.py`, `index.py`, `api/index.py`), not
`backend/main.py`. This thin shim re-exports the real FastAPI app so the build
can find it.

NOTE: This project is a long-running Telegram automation app (persistent
Telethon sessions, background asyncio workers, local SQLite). Vercel serverless
functions are short-lived and stateless, so only the dashboard HTTP endpoints
are usable here — the actual commenting worker is meant to run on a VPS/server
via `python run_dashboard.py` or `uvicorn backend.main:app`.
"""

import os
import sys

# Ensure the project root is importable so `backend.*` / `utils.*` resolve.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.main import app  # noqa: E402,F401
