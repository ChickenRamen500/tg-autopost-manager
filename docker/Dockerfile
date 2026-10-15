FROM python:3.11.10-slim

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
# Скрипт healthcheck отдельно — нужен в образе для HEALTHCHECK
COPY docker/healthcheck.sh /app/healthcheck.sh
RUN chmod +x /app/healthcheck.sh

RUN chown -R appuser:appuser /app

# BOT_API_DATA_MOUNT: куда примонтирован volume local-bot-api внутри контейнера bot.
# Раньше дублировался и в docker-compose environment, теперь только здесь.
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Yekaterinburg \
    BOT_API_DATA_MOUNT=/app/bot-api-data

USER appuser

EXPOSE 8000

# Healthcheck для контейнера bot (P3-37: ранее только local-bot-api имел healthcheck).
# Скрипт читает SECRET_TOKEN из окружения и шлёт заголовок X-Secret-Token —
# токен НЕ светится в `ps`/`docker inspect` как аргумент cmdline.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["/app/healthcheck.sh"]

# FastAPI + aiogram polling (Фаза 2 реализована)
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
