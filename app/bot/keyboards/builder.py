"""Построители inline-клавиатур (ТЗ §8.3).

Все кнопки используют callback_data с префиксами для маршрутизации в handlers/menu.py.
Соглашение по callback_data:
  menu:main               — главное меню
  menu:queues              — список очередей
  menu:queue:<id>          — конкретная очередь (посты)
  menu:inbox               — Входящие (посты без очереди)
  menu:post:<id>            — карточка поста
  menu:post:<id>:toq       — выбрать очередь для поста
  menu:post:<id>:toq:<qid>  — подтвердить постановку в очередь
  menu:channels             — список каналов
  menu:users                — список пользователей
  menu:add_user             — добавить пользователя (запрос tg_id через FSM)
  menu:add_channel          — добавить канал (FSM)
  menu:add_queue            — добавить очередь (FSM)
  menu:stats                — статистика
  menu:back                 — назад в главное меню
"""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Главное меню (ТЗ §8.3)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Входящие", callback_data="menu:inbox")
    kb.button(text="📋 Очереди", callback_data="menu:queues")
    kb.button(text="📊 Статистика", callback_data="menu:stats")
    if is_admin:
        kb.button(text="📢 Каналы", callback_data="menu:channels")
        kb.button(text="👥 Пользователи", callback_data="menu:users")
        kb.button(text="➕ Добавить", callback_data="menu:add")
    kb.adjust(2, 1, 2)
    return kb.as_markup()


def back_button() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data="menu:main")
    return kb.as_markup()


def queues_list(queues: list[dict]) -> InlineKeyboardMarkup:
    """Список очередей кнопками."""
    kb = InlineKeyboardBuilder()
    for q in queues:
        active = "✅" if q.get("is_active") else "⏸"
        kb.button(
            text=f"{active} #{q['id']} «{q['name']}» ({q['interval_minutes']}мин)",
            callback_data=f"menu:queue:{q['id']}",
        )
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def queue_posts(queue_id: int, posts: list[dict]) -> InlineKeyboardMarkup:
    """Посты очереди — каждый пост кнопкой."""
    kb = InlineKeyboardBuilder()
    for p in posts[:15]:
        cap = (p.get("original_caption") or "")[:30]
        kb.button(
            text=f"#{p['id']} [{p['status']}] {p['file_type']} {cap}",
            callback_data=f"menu:post:{p['id']}",
        )
    kb.button(text="🔙 К очередям", callback_data="menu:queues")
    kb.adjust(1)
    return kb.as_markup()


def inbox_posts(posts: list[dict]) -> InlineKeyboardMarkup:
    """Входящие посты кнопками."""
    kb = InlineKeyboardBuilder()
    for p in posts[:15]:
        cap = (p.get("original_caption") or "")[:30]
        kb.button(
            text=f"#{p['id']} [{p['status']}] {p['file_type']} {cap}",
            callback_data=f"menu:post:{p['id']}",
        )
    if not posts:
        kb.button(text="📥 Пусто", callback_data="noop")
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def post_card(post: dict, queues: list[dict]) -> InlineKeyboardMarkup:
    """Карточка поста: выбрать очередь, удалить из очереди."""
    kb = InlineKeyboardBuilder()
    status = post["status"]
    if status == "possible_duplicate":
        kb.button(text="✓ Уникальный", callback_data=f"menu:post:{post['id']}:unique")
    if post.get("queue_id") is None:
        # Пост во Входящих — показать кнопки очередей
        for q in queues[:8]:
            kb.button(
                text=f"➡ «{q['name']}» (#{q['id']})",
                callback_data=f"menu:post:{post['id']}:toq:{q['id']}",
            )
    else:
        kb.button(text="📤 В Входящие", callback_data=f"menu:post:{post['id']}:unqueue")
        kb.button(text="🔄 Повторить отправку", callback_data=f"menu:post:{post['id']}:retry")
    kb.button(text="🔙 Назад", callback_data="menu:inbox" if post.get("queue_id") is None else f"menu:queue:{post['queue_id']}")
    kb.adjust(1)
    return kb.as_markup()


def channels_list(channels: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in channels:
        active = "✅" if c.get("is_active") else "⏸"
        kb.button(
            text=f"{active} #{c['id']} «{c['name']}» {c.get('tg_chat_id')}",
            callback_data=f"menu:channel:{c['id']}",
        )
    kb.button(text="➕ Добавить канал", callback_data="menu:add_channel")
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def users_list(users: list[dict], current_tg_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for u in users:
        active = "✅" if u.get("is_active") else "⏸"
        me = " (вы)" if u["tg_id"] == current_tg_id else ""
        kb.button(
            text=f"{active} @{u.get('username') or '—'} [{u['role']}]{me}",
            callback_data=f"menu:user:{u['id']}",
        )
    kb.button(text="➕ Добавить пользователя", callback_data="menu:add_user")
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def add_menu() -> InlineKeyboardMarkup:
    """Подменю добавления."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Канал", callback_data="menu:add_channel")
    kb.button(text="📋 Очередь", callback_data="menu:add_queue")
    kb.button(text="👤 Пользователя", callback_data="menu:add_user")
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(2, 1)
    return kb.as_markup()


def confirm_cancel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Отмена", callback_data="menu:main")
    return kb.as_markup()


def user_actions(user_id: int, is_self: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if not is_self:
        kb.button(text="⬇ Понижение/повышение роли", callback_data=f"menu:user:{user_id}:toggle")
        kb.button(text="🗑 Удалить", callback_data=f"menu:user:{user_id}:del")
    kb.button(text="🔙 Назад", callback_data="menu:users")
    kb.adjust(1)
    return kb.as_markup()
