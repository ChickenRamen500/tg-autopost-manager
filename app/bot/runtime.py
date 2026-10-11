"""Runtime-ссылка на Bot — разрывает циклический импорт.

app.bot импортирует handlers (menu/media/admin), которые импортируют app.scheduler,
который импортирует app.bot (для get_bot). Чтобы разорвать цикл, Bot хранится здесь
и устанавливается при старте приложения (app/main.py lifespan).
"""
from __future__ import annotations

import logging

from aiogram import Bot

log = logging.getLogger("bot.runtime")

_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


def get_bot() -> Bot:
    if _bot is None:
        raise RuntimeError("Bot не инициализирован — вызовите set_bot(bot) при старте приложения")
    return _bot


async def close_bot() -> None:
    global _bot
    if _bot is not None:
        await _bot.session.close()
        _bot = None
