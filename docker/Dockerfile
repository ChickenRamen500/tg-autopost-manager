FROM python:3.11-slim

WORKDIR /app

# Системные зависимости: ffmpeg (кроп видео, ТЗ §2.2), curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY data ./data

ENV PYTHONUNBUFFERED=1 TZ=Europe/Moscow

EXPOSE 8000

# Полноценный вход (FastAPI + aiogram polling) появится на Фазе 2;
# на Фазе 1 контейнер может жить в режиме ожидания.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
