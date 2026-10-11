"""Хендлер /start и /help (ТЗ §8.1, §8.2)."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="start")


@router.message(Command("start"))
async def cmd_start(message: Message, user: dict | None = None, user_role: str | None = None) -> None:
    if user is None:
        # Гость: bootstrap admin сработал в middleware, но пользователя всё ещё нет в БД
        await message.answer(
            "👋 Привет! Я бот-менеджер очередей медиаконтента.\n\n"
            "🔒 У тебя пока нет доступа. Обратись к администратору, чтобы тебя добавили."
        )
        return
    role_label = {"admin": "Администратор", "moderator": "Модератор"}.get(user_role, user_role or "—")
    await message.answer(
        f"👋 Привет, @{user.get('username') or 'друг'}!\n"
        f" badge роли: **{role_label}**\n\n"
        "Пришли мне фото/видео/GIF/документ — я поставлю их в очередь по правилам маршрутизации.\n"
        "Команда /help — список доступных команд."
    )


@router.message(Command("help"))
async def cmd_help(message: Message, user: dict | None = None, user_role: str | None = None) -> None:
    is_admin = user_role == "admin"
    lines = [
        "📖 **Справка**\n",
        "Общие:",
        "/start — начать работу",
        "/help — эта справка",
        "/list_queues — список очередей",
        "/stats — статистика бота",
        "",
        "Модератор:",
        "/queue <queue_id> — показать очередь",
        "/remove_from_queue <post_id> — удалить пост из очереди",
    ]
    if is_admin:
        lines += [
            "",
            "Администратор:",
            "/add_user @username <role> — добавить пользователя (admin/moderator)",
            "/remove_user @username — удалить пользователя",
            "/list_users — список пользователей",
            "/add_channel <tg_chat_id> <name> [@username] — добавить канал",
            "/list_channels — список каналов",
            "/remove_channel <channel_id> — удалить канал",
            "/add_queue <channel_id> <name> <interval_min> — добавить очередь",
            "/remove_queue <queue_id> — удалить очередь",
            "/subscribe gelbooru <tag1> <tag2> ... — подписка на теги Gelbooru",
            "/list_subscriptions — список подписок",
        ]
    await message.answer("\n".join(lines))
