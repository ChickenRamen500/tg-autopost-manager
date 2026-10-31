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
    kb.button(text="✅ Архив", callback_data="menu:sent")
    kb.button(text="📊 Статистика", callback_data="menu:stats")
    if is_admin:
        kb.button(text="📢 Каналы", callback_data="menu:channels")
        kb.button(text="👥 Пользователи", callback_data="menu:users")
        kb.button(text="🔀 Маршрутизация", callback_data="menu:rules")
        kb.button(text="➕ Добавить", callback_data="menu:add")
        kb.button(text="🖼 Gelbooru", callback_data="gb:menu")
        kb.button(text="⚙️ Настройки", callback_data="menu:settings")
    kb.adjust(2, 1, 1, 2, 2, 2, 1)
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
    """Посты очереди — каждый пост кнопкой + удаление очереди."""
    kb = InlineKeyboardBuilder()
    for p in posts[:15]:
        cap = (p.get("original_caption") or "")[:30]
        kb.button(
            text=f"#{p['id']} [{p['status']}] {p['file_type']} {cap}",
            callback_data=f"menu:post:{p['id']}",
        )
    kb.button(text="🗑 Удалить очередь", callback_data=f"menu:del_queue:{queue_id}")
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


def channel_actions(channel_id: int) -> InlineKeyboardMarkup:
    """Действия с каналом: удалить, назад к списку."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить канал", callback_data=f"menu:del_channel:{channel_id}")
    kb.button(text="🔙 К каналам", callback_data="menu:channels")
    kb.adjust(1)
    return kb.as_markup()


def queue_actions(queue_id: int) -> InlineKeyboardMarkup:
    """Действия с очередью: удалить, назад к списку."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить очередь", callback_data=f"menu:del_queue:{queue_id}")
    kb.button(text="🔙 К очередям", callback_data="menu:queues")
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


def confirm_delete(kind: str, item_id: int) -> InlineKeyboardMarkup:
    """Двухшаговое подтверждение удаления (пост/пользователь/правило).

    kind: "post" | "user" | "rule" — определяет callback-префикс.
    callback_data: confirm_del:<kind>:<id> / cancel_del
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Подтвердить удаление", callback_data=f"confirm_del:{kind}:{item_id}")
    kb.button(text="🔙 Отмена", callback_data="cancel_del")
    kb.adjust(1)
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


# ===========================================================================
# Gelbooru (Фаза 3 этап 4 — подписки + blacklist + просмотр новых постов)
# ===========================================================================
#
# Callback-конвенция:
#   gb:menu                      — главное меню Gelbooru
#   gb:subs                      — список подписок
#   gb:sub:<id>                  — карточка подписки (действия)
#   gb:sub:<id>:toggle           — вкл/выкл подписку
#   gb:sub:<id>:del              — удалить подписку (с confirm)
#   gb:sub:<id>:check            — запустить проверку вручную (немедленно)
#   gb:add_sub                   — FSM: добавить подписку (tags)
#   gb:seen                      — список новых постов (все подписки)
#   gb:seen:<sub_id>             — новые посты конкретной подписки
#   gb:seen_card:<seen_id>       — карточка поста [Назад][Отправить][Пропустить][Далее]
#   gb:seen:<seen_id>:send       — отправить → выбор очереди
#   gb:seen:<seen_id>:send:<qid> — подтверждение отправки в очередь qid
#   gb:seen:<seen_id>:skip       — пропустить (mark_ignored)
#   gb:seen:<seen_id>:next       — следующий пост (в рамках списка)
#   gb:seen:<seen_id>:prev       — предыдущий пост
#   gb:blacklist                 — список blacklist тегов
#   gb:bl_add                    — FSM: добавить blacklist-тег
#   gb:bl:<tag>:del              — удалить blacklist-тег (tag url-encoded)


def gelbooru_main_menu(new_posts_count: int = 0) -> InlineKeyboardMarkup:
    """Главное меню Gelbooru: подписки / новые посты / blacklist."""
    kb = InlineKeyboardBuilder()
    new_label = f"🆕 Новые посты ({new_posts_count})" if new_posts_count > 0 else "🆕 Новые посты"
    kb.button(text=new_label, callback_data="gb:seen")
    kb.button(text="📋 Подписки", callback_data="gb:subs")
    kb.button(text="🚫 Blacklist тегов", callback_data="gb:blacklist")
    kb.button(text="🔙 Назад", callback_data="menu:main")
    kb.adjust(1, 2, 1)
    return kb.as_markup()


def gelbooru_subs_list(subs: list[dict], new_counts: dict[int, int] | None = None) -> InlineKeyboardMarkup:
    """Список подписок кнопками.

    subs — список dict из subscriptions.list_subscriptions (с распарсенными tags).
    new_counts — {subscription_id: count_new_posts} для бейджа «N новых».
    """
    new_counts = new_counts or {}
    kb = InlineKeyboardBuilder()
    for s in subs[:20]:
        active = "✅" if s.get("is_active") else "⏸"
        tags_str = " ".join(s.get("tags") or []) or "(пусто)"
        if len(tags_str) > 40:
            tags_str = tags_str[:37] + "..."
        new_n = new_counts.get(s["id"], 0)
        badge = f" [+{new_n}]" if new_n > 0 else ""
        kb.button(
            text=f"{active} #{s['id']} {tags_str}{badge}",
            callback_data=f"gb:sub:{s['id']}",
        )
    kb.button(text="➕ Добавить подписку", callback_data="gb:add_sub")
    kb.button(text="🔙 Назад", callback_data="gb:menu")
    kb.adjust(1)
    return kb.as_markup()


def gelbooru_sub_card(sub: dict, new_count: int = 0, default_queue_name: str | None = None) -> InlineKeyboardMarkup:
    """Карточка подписки: проверить, настройки интервалов, default queue, вкл/выкл, удалить."""
    kb = InlineKeyboardBuilder()
    if new_count > 0:
        kb.button(text=f"🆕 Новые посты ({new_count})", callback_data=f"gb:seen:{sub['id']}")
    kb.button(text="🔄 Проверить сейчас", callback_data=f"gb:sub:{sub['id']}:check")
    # Настройки интервалов
    kb.button(
        text=f"⏱ Интервал проверки: {sub['check_interval_hours']}ч",
        callback_data=f"gb:sub:{sub['id']}:setcheck",
    )
    kb.button(
        text=f"🔔 Интервал уведомлений: {sub['notify_interval_minutes']}мин",
        callback_data=f"gb:sub:{sub['id']}:setnotify",
    )
    # Default queue — показать текущую или «не задана»
    q_label = f"📥 Очередь по умолчанию: {default_queue_name}" if default_queue_name else "📥 Очередь по умолчанию: не задана"
    kb.button(text=q_label, callback_data=f"gb:sub:{sub['id']}:setqueue")
    toggle_label = "⏸ Выключить" if sub.get("is_active") else "▶ Включить"
    kb.button(text=toggle_label, callback_data=f"gb:sub:{sub['id']}:toggle")
    kb.button(text="🗑 Удалить подписку", callback_data=f"gb:sub:{sub['id']}:del")
    kb.button(text="🔙 К подпискам", callback_data="gb:subs")
    kb.adjust(1)
    return kb.as_markup()


def gelbooru_interval_picker(sub_id: int, kind: str, current: int) -> InlineKeyboardMarkup:
    """Выбор интервала (check/notify) — пресет кнопок + назад.

    kind: 'setcheck' (часы) или 'setnotify' (минуты).
    """
    kb = InlineKeyboardBuilder()
    if kind == "setcheck":
        # Часы: 1, 3, 6, 12, 24
        for h in [1, 3, 6, 12, 24]:
            mark = "✅ " if h == current else ""
            kb.button(text=f"{mark}{h}ч", callback_data=f"gb:sub:{sub_id}:setcheck:{h}")
    else:
        # Минуты: 0 (при каждом чеке), 15, 30, 60, 180, 360
        for m in [0, 15, 30, 60, 180, 360]:
            mark = "✅ " if m == current else ""
            label = f"{mark}{'при каждом чеке' if m == 0 else f'{m}мин'}"
            kb.button(text=label, callback_data=f"gb:sub:{sub_id}:setnotify:{m}")
    kb.button(text="🔙 К подписке", callback_data=f"gb:sub:{sub_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def gelbooru_seen_list(items: list[dict], sub_id: int | None = None) -> InlineKeyboardMarkup:
    """Список новых постов кнопками (для выбора карточки).

    items — list из gelbooru_subs.list_new_posts / list_new_posts_all.
    sub_id — если просмотр в рамках одной подписки, «Назад» → gb:seen:<sub_id>,
    иначе → gb:menu.
    """
    kb = InlineKeyboardBuilder()
    for it in items[:15]:
        tags = it.get("tags") or []
        tags_str = tags[0] if tags else f"#{it['post_id']}"
        rating_badge = {"safe": "🟢", "questionable": "🟡", "explicit": "🔴"}.get(it.get("rating"), "⚪")
        kb.button(
            text=f"{rating_badge} #{it['id']} gelbooru:{it['post_id']} {tags_str}",
            callback_data=f"gb:seen_card:{it['id']}",
        )
    if not items:
        kb.button(text="📭 Пусто", callback_data="noop")
    back_cb = f"gb:seen:{sub_id}" if sub_id else "gb:menu"
    kb.button(text="🔙 Назад", callback_data=back_cb)
    kb.adjust(1)
    return kb.as_markup()


def gelbooru_seen_card(
    seen_id: int,
    has_prev: bool,
    has_next: bool,
    queues: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    """Карточка поста Gelbooru: [Назад][Отправить в...][Пропустить][Далее].

    ТЗ §2.1.4: кнопки [Назад] [Отправить] [Пропустить] [Далее].
    «Отправить» открывает выбор очереди (inline кнопки очередей), либо
    «Во Входящие» (queue_id=None).

    Если queues передан — показываем кнопки очередей вместо основного меню
    (режим выбора очереди). Иначе — основное меню карточки.
    """
    kb = InlineKeyboardBuilder()
    if queues is not None:
        # Режим выбора очереди
        kb.button(text="📥 Во Входящие (без очереди)", callback_data=f"gb:seen:{seen_id}:send:0")
        for q in queues[:8]:
            kb.button(
                text=f"📋 «{q['name']}» (#{q['id']})",
                callback_data=f"gb:seen:{seen_id}:send:{q['id']}",
            )
        kb.button(text="🔙 Отмена", callback_data=f"gb:seen_card:{seen_id}")
        kb.adjust(1)
        return kb.as_markup()

    # Основное меню карточки
    kb.button(text="📤 Отправить", callback_data=f"gb:seen:{seen_id}:send")
    kb.button(text="⏭ Пропустить", callback_data=f"gb:seen:{seen_id}:skip")
    # Навигация
    nav_added = False
    if has_prev:
        kb.button(text="⬅ Назад", callback_data=f"gb:seen:{seen_id}:prev")
        nav_added = True
    if has_next:
        kb.button(text="Далее ➡", callback_data=f"gb:seen:{seen_id}:next")
        nav_added = True
    if nav_added:
        # [Отправить][Пропустить] на одной строке, [Назад][Далее] на следующей
        kb.adjust(2, 2)
    else:
        kb.adjust(2)
    kb.button(text="📋 К списку", callback_data="gb:seen")
    kb.adjust(2, 2, 1) if nav_added else kb.adjust(2, 1)
    return kb.as_markup()


def gelbooru_blacklist_list(tags: list[str]) -> InlineKeyboardMarkup:
    """Список blacklist-тегов с кнопками удаления."""
    kb = InlineKeyboardBuilder()
    for tag in tags[:30]:
        # URL-encode tag для callback_data (Telegram limit 64 байта)
        from urllib.parse import quote

        encoded = quote(tag, safe="")
        kb.button(text=f"🚫 {tag}  🗛", callback_data=f"gb:bl:{encoded}:del")
    if not tags:
        kb.button(text="📭 Blacklist пуст", callback_data="noop")
    kb.button(text="➕ Добавить тег", callback_data="gb:bl_add")
    kb.button(text="🔙 Назад", callback_data="gb:menu")
    kb.adjust(1)
    return kb.as_markup()
