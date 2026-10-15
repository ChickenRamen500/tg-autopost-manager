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
        kb.button(text="🔀 Маршрутизация", callback_data="menu:rules")
        kb.button(text="➕ Добавить", callback_data="menu:add")
        kb.button(text="⚙️ Настройки", callback_data="menu:settings")
    kb.adjust(2, 1, 2, 2)
    return kb.as_markup()


def back_button() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data="menu:main")
    return kb.as_markup()


def need_queue_menu() -> InlineKeyboardMarkup:
    """Экран «Нет очередей» для правил маршрутизации: создать очередь или назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать очередь", callback_data="menu:add_queue")
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def settings_menu() -> InlineKeyboardMarkup:
    """Меню настроек приложения (блок D)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📂 Служебный канал (архив)", callback_data="set:archive")
    kb.button(text="🔔 Порог уведомлений", callback_data="set:threshold")
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def queues_list(queues: list[dict]) -> InlineKeyboardMarkup:
    """Список очередей кнопками. Помечает очереди без канала «(без канала)»."""
    kb = InlineKeyboardBuilder()
    for q in queues:
        active = "✅" if q.get("is_active") else "⏸"
        chan = q.get("channel_name") or "(без канала)"
        kb.button(
            text=f"{active} #{q['id']} «{q['name']}» → {chan} ({q['interval_minutes']}мин)",
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
    """Карточка поста: выбрать очередь, удалить из очереди, удалить пост."""
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
    kb.button(text="🗑 Удалить пост", callback_data=f"menu:post:{post['id']}:del")
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
    kb.button(text="🔀 Правило", callback_data="menu:add_rule")
    kb.button(text="👤 Пользователя", callback_data="menu:add_user")
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(2, 2, 1)
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


# ---------------------------------------------------------------------------
# Правила маршрутизации (ТЗ §2.2.2)
# ---------------------------------------------------------------------------

# Типы источников для правила маршрутизации (ТЗ §2.2.1).
# telegram_direct и telegram_user убраны: посты из ЛС бота и пересылки от юзеров
# НЕ маршрутизируются правилами — для них работает интерактивный выбор очереди
# (блок A): бот отвечает сообщением с кнопками каналов → очередей.
ROUTING_SOURCE_TYPES = [
    ("telegram_channel", "📢 Telegram-канал"),
    ("local_folder", "📁 Локальная папка"),
    ("gelbooru", "🖼 Gelbooru"),
    ("any", "⭐ Любой (по умолчанию)"),
]


def source_type_picker() -> InlineKeyboardMarkup:
    """Выбор типа источника для правила маршрутизации."""
    kb = InlineKeyboardBuilder()
    for value, label in ROUTING_SOURCE_TYPES:
        kb.button(text=label, callback_data=f"menu:rule:stype:{value}")
    kb.button(text="🔙 Отмена", callback_data="menu:add")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def rule_pick_queue(queues: list[dict]) -> InlineKeyboardMarkup:
    """Выбор целевой очереди для правила маршрутизации."""
    kb = InlineKeyboardBuilder()
    for q in queues:
        chan = q.get("channel_name") or "(без канала)"
        kb.button(
            text=f"📋 «{q['name']}» (#{q['id']}) → {chan}",
            callback_data=f"menu:rule:queue:{q['id']}",
        )
    kb.button(text="🔙 Отмена", callback_data="menu:add")
    kb.adjust(1)
    return kb.as_markup()


def rules_list(rules: list[dict], queues_map: dict[int, str]) -> InlineKeyboardMarkup:
    """Список правил маршрутизации с кнопками удаления."""
    kb = InlineKeyboardBuilder()
    for r in rules:
        active = "✅" if r.get("is_active") else "⏸"
        qname = queues_map.get(r["target_queue_id"], f"#{r['target_queue_id']}")
        pattern = r["source_pattern"][:25]
        kb.button(
            text=f"{active} #{r['id']} {r['source_type']} «{pattern}» → «{qname}» (p={r['priority']})",
            callback_data=f"menu:rule:{r['id']}",
        )
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def rule_actions(rule_id: int) -> InlineKeyboardMarkup:
    """Действия с правилом: удалить, вкл/выкл."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить", callback_data=f"menu:rule:{rule_id}:del")
    kb.button(text="🔙 К правилам", callback_data="menu:rules")
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Интерактивный выбор очереди (при посте из ЛС — блок A)
# ---------------------------------------------------------------------------

def pick_channel_for_post(post_id: int, channels: list[dict]) -> InlineKeyboardMarkup:
    """Выбор канала для поста из Входящих (после приёма из ЛС).

    Кнопки каналов → затем покажет очереди выбранного канала.
    """
    kb = InlineKeyboardBuilder()
    for c in channels[:8]:
        kb.button(
            text=f"📢 «{c['name']}» (@{c.get('username') or '—'})",
            callback_data=f"menu:post:{post_id}:pickch:{c['id']}",
        )
    if not channels:
        kb.button(text="📢 Каналов нет", callback_data="noop")
    kb.button(text="➕ Создать канал", callback_data="menu:add_channel")
    kb.button(text="📥 Оставить во Входящих", callback_data="noop")
    kb.adjust(1)
    return kb.as_markup()


def pick_queue_for_post(post_id: int, channel_id: int, queues: list[dict]) -> InlineKeyboardMarkup:
    """Выбор очереди канала для поста (после выбора канала)."""
    kb = InlineKeyboardBuilder()
    for q in queues[:8]:
        kb.button(
            text=f"📋 «{q['name']}» (#{q['id']}, каждые {q['interval_minutes']}мин)",
            callback_data=f"menu:post:{post_id}:toq:{q['id']}",
        )
    if not queues:
        kb.button(text="📋 Очередей нет", callback_data="noop")
    # Создать очередь для этого канала, затем вернуться к посту
    kb.button(text="➕ Создать очередь", callback_data=f"menu:addq_forch:{channel_id}:{post_id}")
    kb.button(text="🔙 К каналам", callback_data=f"menu:post:{post_id}:pickch_back")
    kb.adjust(1)
    return kb.as_markup()


def queue_settings_menu(
    strategy: str = "continue_next_day",
    group_albums: bool = False,
    strip_caption: bool = False,
    shuffle: bool = True,
    has_signature: bool = False,
) -> InlineKeyboardMarkup:
    """Экран настроек очереди с toggle-кнопками (шаг 6/6).

    Кнопки-переключатели: стратегия (цикл), группировка альбомов, удаление подписей,
    перемешивание, подпись, создать, отмена.
    """
    kb = InlineKeyboardBuilder()
    strategy_labels = {
        "continue_next_day": "🔄 Стратегия: продлить на след. день",
        "continue_after_end": "🔄 Стратегия: продолжить после конца",
        "move_to_inbox": "🔄 Стратегия: во Входящие",
        "delete": "🔄 Стратегия: удалить",
    }
    kb.button(text=strategy_labels.get(strategy, "🔄 Стратегия"), callback_data="qset:strategy")
    kb.button(
        text=f"📸 Альбомы группой: {'✅ да' if group_albums else '❌ нет'}",
        callback_data="qset:group_albums",
    )
    kb.button(
        text=f"🗑 Удалять подписи: {'✅ да' if strip_caption else '❌ нет'}",
        callback_data="qset:strip_caption",
    )
    kb.button(
        text=f"🔀 Перемешать: {'✅ да' if shuffle else '❌ нет'}",
        callback_data="qset:shuffle",
    )
    kb.button(
        text=f"✍️ Подпись: {'✅ есть' if has_signature else '❌ нет'}",
        callback_data="qset:signature",
    )
    kb.button(text="✅ Создать очередь", callback_data="qset:create")
    kb.button(text="🔙 Отмена", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def create_rule_prompt(post_id: int, queue_id: int, source_id: str) -> InlineKeyboardMarkup:
    """Предложение создать правило маршрутизации после выбора очереди для поста.

    Кнопки: создать правило / нет.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, создать правило", callback_data=f"rule:create:{post_id}:{queue_id}:{source_id}")
    kb.button(text="❌ Нет", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()
