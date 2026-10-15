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

ENV PYTHONUNBUFFERED=1 TZ=Asia/Yekaterinburg

EXPOSE 8000

# FastAPI + aiogram polling (Фаза 2 реализована)
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
