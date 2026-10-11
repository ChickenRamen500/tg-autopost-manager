# -*- coding: utf-8 -*-
"""Точка входа (Фаза 1): healthcheck-эндпоинты для проверки Local Bot API.

На Фазе 2 сюда добавятся: инициализация БД, запуск aiogram-polling,
REST API (§6) и веб-интерфейс (§7).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.config.settings import settings
from app.utils.local_bot_api import get_me

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

app = FastAPI(title="TG Media Queue Bot", version="0.1.0-phase1")


@app.get("/health")
async def health() -> dict:
    """Живость приложения + состояние конфигурации."""
    return {
        "status": "ok",
        "bot_api_url": settings.bot_api_url,
        "config_problems": settings.validate(),
    }


@app.get("/health/bot-api")
async def health_bot_api() -> dict:
    """Прокидывает getMe через Local Bot API Server (ТЗ §3.3).

    ok:true  — сервер с --local принимает токен бота (миграция logOut выполнена);
    error    — сервер недоступен либо 401/409 (не сделан logOut / неверный токен).
    """
    me = await get_me()
    if me:
        return {"status": "ok", "bot": {"username": me.get("username"), "id": me.get("id")}}
    return {"status": "error", "detail": "Local Bot API недоступен или токен отклонён; см. scripts/check_phase1.py"}
