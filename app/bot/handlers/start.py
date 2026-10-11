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
        "/list_inbox — посты без очереди (Входящие)",
        "/stats — статистика бота",
        "",
        "Модератор:",
        "/queue <queue_id> — показать очередь",
        "/add_to_queue <post_id> <queue_id> — поставить пост в очередь",
        "/remove_from_queue <post_id> — вернуть пост во Входящие",
    ]
    if is_admin:
        lines += [
            "",
            "Администратор:",
            "👥 Пользователи:",
            "/add_user <tg_id> <role> [имя] — добавить по числовому ID (рекомендуется)",
            "/add_user @username <role> — добавить по @username (нужна пересылка)",
            "/set_role @username <role> — ответом на пересланное сообщение юзера",
            "/remove_user @username — удалить пользователя",
            "/list_users — список пользователей",
            "",
            "📢 Каналы (куда публикуем):",
            "/add_channel <tg_chat_id> <name> [@username]",
            "/list_channels — список каналов",
            "/remove_channel <channel_id>",
            "",
            "📋 Очереди:",
            "/add_queue <channel_id> <name> <interval_min>",
            "/remove_queue <queue_id>",
            "/reload_scheduler — перерегистрировать планировщик (без restart)",
            "",
            "🔔 Подписки (Фаза 3):",
            "/subscribe gelbooru <tag1> <tag2> ...",
            "/list_subscriptions",
        ]
    await message.answer("\n".join(lines))
