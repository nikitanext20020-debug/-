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
