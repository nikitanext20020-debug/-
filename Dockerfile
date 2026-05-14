FROM python:3.11-slim

# Системные зависимости (для opencv-python)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgl1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала ставим зависимости (для лучшего docker-cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем приложение
COPY . .

# Каталоги, которые должны существовать
RUN mkdir -p data sessions

# Не кэшируем .pyc, не буферизуем stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

EXPOSE 8000

# Healthcheck для оркестраторов (k8s/docker compose)
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys,os; \
    p=os.environ.get('PORT','8000'); \
    sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health',timeout=5).status==200 else 1)" \
    || exit 1

CMD ["python", "host.py"]
