FROM python:3.11-slim

WORKDIR /app

# Системные зависимости: ffmpeg (кроп видео, ТЗ §2.2), curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Создаём непривилегированного пользователя (P3-37: security — не root)
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY data ./data

RUN chown -R appuser:appuser /app

ENV PYTHONUNBUFFERED=1 TZ=Asia/Yekaterinburg

USER appuser

EXPOSE 8000

# Healthcheck для контейнера bot (P3-37: ранее только local-bot-api имел healthcheck)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fs -H "X-Secret-Token: ${SECRET_TOKEN}" http://localhost:8000/health || exit 1

# FastAPI + aiogram polling (Фаза 2 реализована)
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
