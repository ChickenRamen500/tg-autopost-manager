"""Хендлер /help — короткая справка + кнопка главного меню.

/start и /menu обрабатываются в menu.py (показывают inline-меню).
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards import builder as kb

router = Router(name="start")


@router.message(Command("help"))
async def cmd_help(message: Message, user: dict | None = None, user_role: str | None = None) -> None:
    """Короткая справка + кнопка главного меню."""
    is_admin = user_role == "admin"
    text = (
        "📖 **Справка**\n\n"
        "Всё управление — через кнопки главного меню (/menu или /start).\n\n"
        "Быстрые действия:\n"
        "• Пришли фото/видео/GIF — поставлю в очередь\n"
        "• /menu — главное меню с кнопками\n"
        "• /cancel — отменить текущее действие\n"
        "• /start — открыть главное меню\n\n"
    )
    if user is None:
        text += "🔒 У тебя нет доступа. Обратись к администратору."
    await message.answer(text, reply_markup=kb.main_menu(is_admin))
