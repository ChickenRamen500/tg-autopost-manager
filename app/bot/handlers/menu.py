"""Inline-меню (ТЗ §8.3) — главное взаимодействие через кнопками.

Ключевые принципы (исправление FSM-ловушки из логов 6-7):
1. cmd_cancel — ПЕРВЫЙ хендлер, перехватывает /cancel В ЛЮБОМ состоянии
   (без фильтра StateFilter). Без этого /cancel перехватывался FSM-хендлером.
2. cmd_menu/cmd_start — тоже первыми (очищают состояние, показывают меню).
3. FSM message-хендлеры — с фильтром F.text (только текстовые сообщения),
   чтобы фото/видео НЕ триггерили «Название не может быть пустым».
4. Catch-all для не-текстовых сообщений в FSM — «Я жду текст, /cancel для отмены».
   Фото/видео в FSM НЕ создают посты (предотвращает путаницу).
"""
from __future__ import annotations

import html
import logging
from datetime import UTC

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import builder as kb
from app.config.settings import settings
from app.repositories import (
    channels as channels_repo,
)
from app.repositories import (
    posts as posts_repo,
)
from app.repositories import (
    queues as queues_repo,
)
from app.repositories import (
    routing as routing_repo,
)
from app.repositories import (
    users as users_repo,
)
from app.scheduler import reload_scheduler

logger = logging.getLogger("handlers.menu")

router = Router(name="menu")


# Все FSM-состояния определяем ВНАЧАЛЕ файла — до декораторов.
class AddStates(StatesGroup):
    """FSM состояния для добавления сущностей."""
    # Пользователь
    add_user_tg_id = State()
    add_user_role = State()
    # Канал (3 шага через единый flow)
    add_channel_flow = State()
    # Очередь
    add_queue_pick_channel = State()
    add_queue_name = State()
    add_queue_interval = State()
    add_queue_start_end = State()    # start_at / end_at (блок B) — только даты
    add_queue_daily_window = State()  # daily_window (блок B)
    add_queue_end_strategy = State()  # toggle-экран настроек (блок B фикс)
    add_queue_signature = State()     # ввод signature_template
    # Настройки приложения (блок D)
    add_archive_channel = State()     # ввод chat_id архивного канала
    add_threshold = State()            # ввод порога уведомлений
    # Правило маршрутизации
    add_rule_pattern = State()
    add_rule_pick_queue = State()
    add_rule_priority = State()


# ===========================================================================
# Хелперы (code review M1: safe int parsing for callback data)
# ===========================================================================

async def _safe_int(value: str, call: CallbackQuery | None = None, label: str = "ID") -> int | None:
    """Безопасно парсит int из callback_data. При ошибке — call.answer + return None.

    P2-14: async def (не sync с asyncio.ensure_future), ловит IndexError на bounds.
    """
    try:
        return int(value)
    except (ValueError, TypeError, IndexError):
        if call:
            await call.answer(f"Неверный {label}: {value}", show_alert=True)
        return None


# ===========================================================================
# ПРИОРИТЕТНЫЕ ХЕНДЛЕРЫ (перехватывают /cancel, /menu, /start в любом состоянии)
# Должны быть зарегистрированы ПЕРВЫМИ, иначе FSM-хендлеры их перехватят.
# ===========================================================================

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    """Отмена любого FSM-действия. Перехватывает /cancel В ЛЮБОМ состоянии."""
    current = await state.get_state()
    await state.clear()
    if current is not None:
        await message.answer("❌ Действие отменено.", reply_markup=kb.main_menu(user_role == "admin"))
    else:
        await message.answer("🏠 Главное меню.", reply_markup=kb.main_menu(user_role == "admin"))


@router.message(Command("menu"))
@router.message(Command("start"))
async def cmd_menu(message: Message, state: FSMContext, user: dict | None = None, user_role: str | None = None) -> None:
    """Открывает главное inline-меню. Очищает FSM-состояние если было."""
    await state.clear()
    is_admin = user_role == "admin"
    await message.answer(
        "🏠 <b>Главное меню</b>\n\nИспользуй кнопки ниже. Пришли фото/видео/GIF — поставлю в очередь.",
        reply_markup=kb.main_menu(is_admin),
    )


@router.callback_query(F.data == "menu:main")
async def cb_main(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    await state.clear()
    is_admin = user_role == "admin"
    await call.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nИспользуй кнопки ниже.",
        reply_markup=kb.main_menu(is_admin),
    )
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


# ===========================================================================
# ВСПОМОГАТЕЛЬНЫЙ: catch не-текстовых сообщений в FSM-состоянии
# Фото/видео/документ, отправленные во время FSM (добавление очереди/канала/...),
# НЕ создают посты и НЕ триггерят «название пустое» — выводим подсказку.
# ===========================================================================

@router.message(StateFilter(AddStates), ~F.text)
async def fsm_non_text_in_state(message: Message) -> None:
    """Перехватывает фото/видео/документ/голосовое в любом FSM-состоянии.

    Без этого фото попадало в FSM-хендлер → «Название не может быть пустым».
    Теперь пользователь видит понятную подсказку: жду текст, /cancel для отмены.
    """
    await message.answer(
        "✏️ Я жду текстовый ответ (не медиа).\n"
        "Отправь текст или нажми /cancel для отмены."
    )


# ===========================================================================
# Очереди
# ===========================================================================

@router.callback_query(F.data == "menu:queues")
async def cb_queues(call: CallbackQuery) -> None:
    queues = await queues_repo.list_queues(active_only=False)
    if not queues:
        await call.message.edit_text("📋 Очередей пока нет.", reply_markup=kb.back_button())
        await call.answer()
        return
    await call.message.edit_text("📋 <b>Очереди</b> (нажми для просмотра постов):", reply_markup=kb.queues_list(queues))
    await call.answer()


@router.callback_query(F.data.startswith("menu:queue:"))
async def cb_queue(call: CallbackQuery) -> None:
    qid = await _safe_int(call.data.split(":")[2], call, "queue_id")
    if qid is None:
        return
    posts = await posts_repo.list_queue_posts(qid, limit=15)
    queue = await queues_repo.get_queue(qid)
    if not queue:
        await call.answer("Очередь не найдена", show_alert=True)
        return
    pending = await queues_repo.count_pending(qid)
    group_label = "да (send_media_group)" if queue.get("group_albums") else "нет (по одному)"
    from app.services.queue_time import format_queue_time_info

    time_info = format_queue_time_info(queue)
    text = (
        f"📋 <b>Очередь #{qid} «{html.escape(queue['name'])}»</b>\n"
        f"Канал: #{queue['channel_id']}\n"
        f"Интервал: каждые {queue['interval_minutes']} мин\n"
        f"Альбомы группой: {group_label}\n"
        f"Время: {time_info}\n"
        f"pending постов: {pending}\n\n"
        f"Последние посты ({len(posts)}):"
    )
    await call.message.edit_text(text, reply_markup=kb.queue_posts(qid, posts))
    await call.answer()


# ===========================================================================
# Входящие
# ===========================================================================

@router.callback_query(F.data == "menu:inbox")
async def cb_inbox(call: CallbackQuery) -> None:
    posts = await posts_repo.list_inbox(limit=15)
    text = "📥 <b>Входящие</b> (посты без очереди)" if posts else "📥 Входящие пусты"
    await call.message.edit_text(text, reply_markup=kb.inbox_posts(posts))
    await call.answer()


# ===========================================================================
# Карточка поста + действия
# ===========================================================================

@router.callback_query(F.data.startswith("rule:create:"))
async def cb_create_rule_from_post(call: CallbackQuery, user_role: str | None = None) -> None:
    """Создаёт правило маршрутизации одной кнопкой (из предложения после выбора очереди).

    Callback: rule:create:<post_id>:<queue_id>:<source_id>
    """
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    parts = call.data.split(":")
    queue_id = await _safe_int(parts[3], call, "queue_id")
    if queue_id is None:
        return
    source_id = ":".join(parts[4:])

    from app.repositories import routing as routing_repo
    existing_rules = await routing_repo.list_rules(active_only=True)
    has_rule = any(
        r["source_type"] == "telegram_channel" and r["source_pattern"] == source_id
        for r in existing_rules
    )
    if has_rule:
        await call.answer("Правило уже существует", show_alert=True)
        return

    # P2-27: try/except вокруг create_rule — не оставлять spinner при DB ошибке
    try:
        rid = await routing_repo.create_rule(
            source_type="telegram_channel",
            source_pattern=source_id,
            target_queue_id=queue_id,
            priority=10,
        )
    except Exception as e:  # noqa: BLE001
        await call.answer(f"⚠️ Ошибка создания правила: {e}", show_alert=True)
        return
    await call.answer(f"✓ Правило #{rid} создано", show_alert=True)
    await call.message.edit_text(
        f"✓ Правило #{rid} создано!\n"
        f"Источник: telegram_channel `{source_id}`\n"
        f"→ Очередь: #{queue_id} (priority 10)\n\n"
        f"Будущие посты из этого канала будут автоматически идти в эту очередь.",
        reply_markup=kb.main_menu(is_admin=True),
    )


@router.callback_query(F.data.startswith("menu:post:"))
async def cb_post(call: CallbackQuery) -> None:
    parts = call.data.split(":")
    post_id = await _safe_int(parts[2], call, "post_id")
    if post_id is None:
        return
    action = parts[3] if len(parts) > 3 else None

    # Интерактивный выбор очереди (блок A): выбор канала → показ очередей канала
    if action == "pickch":
        channel_id = await _safe_int(parts[4], call, "channel_id")
        if channel_id is None:
            return
        # Проверяем, что пост ещё существует и во Входящих
        post = await posts_repo.get_post(post_id)
        if not post:
            await call.answer("Пост не найден (возможно, удалён)", show_alert=True)
            return
        if post.get("queue_id") is not None:
            await call.answer("Пост уже в очереди", show_alert=True)
            return
        # Очереди выбранного канала
        all_queues = await queues_repo.list_queues(active_only=False)
        channel_queues = [q for q in all_queues if q["channel_id"] == channel_id]
        channel = await channels_repo.get_channel(channel_id)
        ch_name = channel["name"] if channel else f"#{channel_id}"
        if not channel_queues:
            await call.message.edit_text(
                f"📢 Канал «{ch_name}» выбран.\n\n"
                f"⚠️ В этом канале нет очередей. Создай очередь для него:",
                reply_markup=kb.pick_queue_for_post(post_id, channel_id, []),
            )
        else:
            await call.message.edit_text(
                f"📢 Канал «{ch_name}». Выбери очередь:",
                reply_markup=kb.pick_queue_for_post(post_id, channel_id, channel_queues),
            )
        await call.answer()
        return

    if action == "pickch_back":
        # Назад к списку каналов (из выбора очереди)
        post = await posts_repo.get_post(post_id)
        if not post:
            await call.answer("Пост не найден", show_alert=True)
            return
        if post.get("queue_id") is not None:
            await call.answer("Пост уже в очереди", show_alert=True)
            return
        channels = await channels_repo.list_channels(active_only=True)
        await call.message.edit_text(
            f"📥 Пост #{post_id} → Входящие. Куда отправить?",
            reply_markup=kb.pick_channel_for_post(post_id, channels),
        )
        await call.answer()
        return

    if action == "toq":
        queue_id = await _safe_int(parts[4], call, "queue_id")
        if queue_id is None:
            return
        post = await posts_repo.get_post(post_id)
        if not post:
            await call.answer("Пост не найден", show_alert=True)
            return
        await posts_repo.assign_queue(post_id, queue_id)
        if post["status"] in ("failed", "possible_duplicate", "duplicate"):
            await posts_repo.update_status(post_id, "pending")
        queue = await queues_repo.get_queue(queue_id)
        qname = queue["name"] if queue else f"#{queue_id}"
        await call.answer(f"✓ Пост #{post_id} → «{qname}»", show_alert=True)

        # Вопрос «создать правило?» — если источник канала и правила для него нет
        should_prompt_rule = (
            post.get("source_type") == "telegram_channel" and post.get("source_id")
        )
        if should_prompt_rule:
            from app.repositories import routing as routing_repo
            existing_rules = await routing_repo.list_rules(active_only=True)
            has_rule = any(
                r["source_type"] == "telegram_channel" and r["source_pattern"] == post["source_id"]
                for r in existing_rules
            )
            if not has_rule:
                post = await posts_repo.get_post(post_id)
                queues = await queues_repo.list_queues(active_only=True)
                await _render_post_card(call, post, queues)
                await call.message.answer(
                    f"💡 Создать правило маршрутизации?\n"
                    f"Источник: канал {html.escape(post.get('source_id', '?'))}\n"
                    f"→ Очередь: «{qname}» (#{queue_id})\n\n"
                    f"Будущие посты из этого канала будут автоматически идти в эту очередь.",
                    reply_markup=kb.create_rule_prompt(post_id, queue_id, post["source_id"] or ""),
                )
                return

        post = await posts_repo.get_post(post_id)
        queues = await queues_repo.list_queues(active_only=True)
        await _render_post_card(call, post, queues)
        return

    if action == "unqueue":
        await posts_repo.assign_queue(post_id, None)
        await call.answer(f"Пост #{post_id} → Входящие", show_alert=True)
        post = await posts_repo.get_post(post_id)
        queues = await queues_repo.list_queues(active_only=True)
        await _render_post_card(call, post, queues)
        return

    if action == "retry":
        await posts_repo.update_status(post_id, "pending")
        await call.answer(f"Пост #{post_id} снова pending", show_alert=True)
        post = await posts_repo.get_post(post_id)
        queues = await queues_repo.list_queues(active_only=True)
        await _render_post_card(call, post, queues)
        return

    if action == "unique":
        await posts_repo.update_status(post_id, "pending")
        await call.answer(f"Пост #{post_id} → уникальный, pending", show_alert=True)
        post = await posts_repo.get_post(post_id)
        queues = await queues_repo.list_queues(active_only=True)
        await _render_post_card(call, post, queues)
        return

    if action == "del":
        # Удаление поста (для очистки дубликатов из Входящих)
        post = await posts_repo.get_post(post_id)
        if not post:
            await call.answer("Пост не найден", show_alert=True)
            return
        await posts_repo.delete_post(post_id)
        await call.answer(f"Пост #{post_id} удалён", show_alert=True)
        # Возврат туда, откуда пришли: Входящие или очередь
        if post.get("queue_id") is None:
            posts = await posts_repo.list_inbox(limit=15)
            await call.message.edit_text(
                "📥 <b>Входящие</b> (посты без очереди)" if posts else "📥 Входящие пусты",
                reply_markup=kb.inbox_posts(posts),
            )
        else:
            qid = post["queue_id"]
            posts = await posts_repo.list_queue_posts(qid, limit=15)
            queue = await queues_repo.get_queue(qid)
            qname = queue["name"] if queue else f"#{qid}"
            await call.message.edit_text(
                f"📋 Очередь #{qid} «{qname}» (постов: {len(posts)})",
                reply_markup=kb.queue_posts(qid, posts),
            )
        return

    post = await posts_repo.get_post(post_id)
    if not post:
        await call.answer("Пост не найден", show_alert=True)
        return
    queues = await queues_repo.list_queues(active_only=True)
    await _render_post_card(call, post, queues)
    await call.answer()


async def _render_post_card(call: CallbackQuery, post: dict, queues: list[dict]) -> None:
    text = (
        f"🖼 <b>Пост #{post['id']}</b>\n"
        f"Тип: {post['file_type']}\n"
        f"Источник: {post['source_type']}\n"
        f"Статус: {post['status']}\n"
        f"Очередь: {post.get('queue_id') or '— (Входящие)'}\n"
    )
    if post.get("original_caption"):
        text += f"Подпись: {html.escape(post['original_caption'][:97])}...\n" if len(post['original_caption']) > 100 else f"Подпись: {html.escape(post['original_caption'])}\n"
    if post.get("tg_file_id"):
        text += f"file_id: {post['tg_file_id'][:20]}...\n"
    if post.get("media_group_id"):
        text += f"📸 Альбом: {post['media_group_id'][:20]}...\n"
    await call.message.edit_text(text, reply_markup=kb.post_card(post, queues))


# ===========================================================================
# Каналы (FSM через единый flow)
# ===========================================================================

@router.callback_query(F.data == "menu:channels")
async def cb_channels(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    channels = await channels_repo.list_channels()
    if not channels:
        await call.message.edit_text("📢 Каналов нет. Добавь через кнопку «➕»", reply_markup=kb.back_button())
        await call.answer()
        return
    await call.message.edit_text("📢 <b>Каналы</b> (куда публикуем):", reply_markup=kb.channels_list(channels))
    await call.answer()


@router.callback_query(F.data.startswith("menu:channel:"))
async def cb_channel_info(call: CallbackQuery, state: FSMContext) -> None:
    """Показывает информацию о канале при клике из списка каналов.

    Также обрабатывает выбор канала при создании очереди (если в состоянии add_queue_pick_channel).
    """
    ch_id = await _safe_int(call.data.split(":")[2], call, "channel_id")
    if ch_id is None:
        return

    # Если в состоянии создания очереди — передаём в add_queue_pick_channel flow
    current_state = await state.get_state()
    if current_state == AddStates.add_queue_pick_channel:
        await state.update_data(channel_id=ch_id)
        await state.set_state(AddStates.add_queue_name)
        channel = await channels_repo.get_channel(ch_id)
        ch_name = channel["name"] if channel else f"#{ch_id}"
        await call.message.edit_text(
            f"Канал #{ch_id} «{ch_name}» выбран.\nШаг 2/6: Пришли название очереди (например: Утренние посты)",
            reply_markup=kb.confirm_cancel(),
        )
        await call.answer()
        return

    # Вне FSM — показываем инфо о канале
    channel = await channels_repo.get_channel(ch_id)
    if not channel:
        await call.answer("Канал не найден", show_alert=True)
        return
    # Считаем очереди канала
    all_queues = await queues_repo.list_queues(active_only=False)
    channel_queues = [q for q in all_queues if q["channel_id"] == ch_id]
    text = (
        f"📢 <b>Канал #{ch_id} «{html.escape(channel['name'])}»</b>\n"
        f"Chat ID: {channel['tg_chat_id']}\n"
        f"Username: @{channel.get('username') or '—'}\n"
        f"Активен: {'да' if channel['is_active'] else 'нет'}\n"
        f"Очередей: {len(channel_queues)}"
    )
    await call.message.edit_text(text, reply_markup=kb.back_button())
    await call.answer()


@router.callback_query(F.data == "menu:add_channel")
async def cb_add_channel(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    await state.set_state(AddStates.add_channel_flow)
    await state.update_data(step="chat_id")
    await call.message.edit_text(
        "📢 <b>Добавление канала</b>\n\n"
        "Шаг 1/3: Пришли числовой ID канала (вида -1002257882757).\n\n"
        "Как узнать ID:\n"
        "• Перешли любое сообщение из канала боту @userinfobot — он ответит ID\n"
        "• Бот должен быть админом в канале с правом публикации\n\n"
        "Или /cancel для отмены.",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(AddStates.add_channel_flow, F.text)
async def add_channel_flow(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    """Единый обработчик для всех 3 шагов добавления канала (только текст)."""
    if user_role != "admin":
        await state.clear()
        return
    data = await state.get_data()
    step = data.get("step", "chat_id")
    text = (message.text or "").strip()

    if step == "chat_id":
        try:
            tg_chat_id = int(text)
        except ValueError:
            await message.answer("Нужно число вида -1002257882757. Попробуй ещё раз или /cancel")
            return
        # P2-22: валидация -100 prefix (Telegram channel/supergroup chat_id)
        if not str(tg_chat_id).startswith("-100"):
            await message.answer(
                "⚠️ chat_id канала должен начинаться с -100 (например -1002257882757).\n"
                "Проверь ID (узнать у @userinfobot). Попробуй ещё раз или /cancel"
            )
            return
        await state.update_data(tg_chat_id=tg_chat_id, step="name")
        await message.answer("Шаг 2/3: Пришли название канала (например: МойКанал)", reply_markup=kb.confirm_cancel())
        return

    if step == "name":
        if not text:
            await message.answer("Название не может быть пустым. Введи ещё раз или /cancel")
            return
        await state.update_data(name=text, step="username")
        await message.answer(
            "Шаг 3/3: Пришли @username канала (например @my_channel) или '-' если нет публичного",
            reply_markup=kb.confirm_cancel(),
        )
        return

    if step == "username":
        tg_chat_id = data["tg_chat_id"]
        name = data["name"]
        raw = text
        username = None if raw in ("-", "") else raw.lstrip("@")

        existing = await channels_repo.get_channel_by_chat_id(tg_chat_id)
        if existing:
            await message.answer(
                f"⚠️ Канал с chat_id={tg_chat_id} уже существует: #{existing['id']} «{existing['name']}»",
                reply_markup=kb.back_button(),
            )
            await state.clear()
            return
        try:
            cid = await channels_repo.create_channel(tg_chat_id, name, username)
        except channels_repo.DuplicateChannelError:
            await message.answer("⚠️ Канал с таким chat_id уже существует.", reply_markup=kb.back_button())
            await state.clear()
            return
        await message.answer(f"✓ Канал «{name}» добавлен (id={cid})", reply_markup=kb.main_menu(is_admin=True))
        await state.clear()


# ===========================================================================
# Очередь (FSM)
# ===========================================================================

@router.callback_query(F.data == "menu:add_queue")
async def cb_add_queue(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    channels = await channels_repo.list_channels(active_only=True)
    if not channels:
        await call.message.edit_text(
            "Сначала добавь канал (кнопка «Каналы» → «Добавить»).",
            reply_markup=kb.back_button(),
        )
        await call.answer()
        return
    await state.set_state(AddStates.add_queue_pick_channel)
    await call.message.edit_text(
        "📋 <b>Добавление очереди</b>\nШаг 1/6: Нажми на канал-получатель:",
        reply_markup=kb.channels_list(channels),
    )
    await call.answer()


@router.callback_query(F.data.startswith("menu:addq_forch:"))
async def cb_add_queue_for_channel(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    """Создать очередь для выбранного канала (из интерактивного выбора поста).

    Callback: menu:addq_forch:<channel_id>:<post_id>
    Канал уже выбран — переходим сразу к шагу имени очереди.
    После создания вернёмся к выбору очереди для поста (post_id сохраняем в FSM).
    """
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    parts = call.data.split(":")
    channel_id = await _safe_int(parts[2], call, "channel_id")
    if channel_id is None:
        return
    post_id = await _safe_int(parts[3], call, "post_id")
    if post_id is None:
        return
    # Проверяем канал
    channel = await channels_repo.get_channel(channel_id)
    if not channel:
        await call.answer(f"Канал #{channel_id} не найден", show_alert=True)
        return
    await state.update_data(channel_id=channel_id, return_to_post=post_id)
    await state.set_state(AddStates.add_queue_name)
    await call.message.edit_text(
        f"📋 <b>Добавление очереди</b> для канала «{html.escape(channel['name'])}»\n"
        f"Шаг 2/6: Пришли название очереди (например: Утренние посты)",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


# NOTE: add_queue_pick_channel handler убран — теперь cb_channel_info обрабатывает
# выбор канала как в FSM, так и вне его (code review M4).


@router.message(AddStates.add_queue_name, F.text)
async def add_queue_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введи ещё раз или /cancel")
        return
    await state.update_data(name=name)
    await state.set_state(AddStates.add_queue_interval)
    await message.answer("Шаг 3/6: Пришли интервал публикации в минутах (например 60)", reply_markup=kb.confirm_cancel())


@router.message(AddStates.add_queue_interval, F.text)
async def add_queue_interval(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        interval = int(text)
        if interval < 1:
            raise ValueError
    except ValueError:
        await message.answer("Нужно целое число минут (≥1). Введи ещё раз или /cancel")
        return
    await state.update_data(interval=interval)
    await state.set_state(AddStates.add_queue_start_end)
    await message.answer(
        "Шаг 4/6: Даты начала и конца очереди.\n\n"
        "Формат: `дд.мм.гг - дд.мм.гг`\n"
        "Пример: `14.10.26 - 16.10.26`\n\n"
        "Или `-` если без ограничений (сразу/бесконечно).",
        reply_markup=kb.confirm_cancel(),
    )


def _parse_dates(text: str) -> tuple[str | None, str | None] | None:
    """Парсит 'дд.мм.гг - дд.мм.гг' → (start_at_utc, end_at_utc) ISO (00:00:00 / 23:59:59).

    Возвращает (None, None) для '-', None если парсинг не удался.
    Даты в локальном TZ (Asia/Yekaterinburg), хранятся в UTC.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    text = text.strip()
    if text == "-":
        return (None, None)
    if " - " not in text and "-" not in text:
        return None
    parts = text.replace(" - ", "-").split("-", 1)
    if len(parts) != 2:
        return None
    try:
        tz = ZoneInfo(settings.timezone)
        start_str = parts[0].strip()
        end_str = parts[1].strip()
        start_local = datetime.strptime(start_str, "%d.%m.%y").replace(hour=0, minute=0, tzinfo=tz)
        end_local = datetime.strptime(end_str, "%d.%m.%y").replace(hour=23, minute=59, second=59, tzinfo=tz)
        start_utc = start_local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        end_utc = end_local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        return (start_utc, end_utc)
    except ValueError:
        return None


@router.message(AddStates.add_queue_start_end, F.text)
async def add_queue_start_end(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == "-":
        await state.update_data(start_at=None, end_at=None)
    else:
        parsed = _parse_dates(text)
        if parsed is None:
            await message.answer(
                "Не понял формат. Пример: `14.10.26 - 16.10.26`\n"
                "Или `-` если без ограничений.",
                reply_markup=kb.confirm_cancel(),
            )
            return
        await state.update_data(start_at=parsed[0], end_at=parsed[1])
    await state.set_state(AddStates.add_queue_daily_window)
    await message.answer(
        "Шаг 5/6: Ежедневное окно отправки (часы работы каждый день).\n\n"
        "Формат: `чч:мм - чч:мм`\n"
        "Пример: `06:00 - 22:00` (посты только с 6:00 до 22:00)\n\n"
        "Или `-` если без окна (посты в любое время суток).",
        reply_markup=kb.confirm_cancel(),
    )


@router.message(AddStates.add_queue_daily_window, F.text)
async def add_queue_daily_window(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == "-":
        await state.update_data(daily_window_start=None, daily_window_end=None)
    else:
        import re

        m = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text)
        if not m:
            await message.answer(
                "Не понял формат. Пример: `06:00 - 22:00`\nИли `-` если без окна.",
                reply_markup=kb.confirm_cancel(),
            )
            return
        # P2-23: валидация диапазонов часов 0-23 и минут 0-59
        h1, mi1, h2, mi2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if not (0 <= h1 <= 23 and 0 <= mi1 <= 59 and 0 <= h2 <= 23 and 0 <= mi2 <= 59):
            await message.answer("⚠️ Часы 0-23, минуты 0-59. Попробуй ещё раз или /cancel")
            return
        ws = f"{h1:02d}:{mi1:02d}"
        we = f"{h2:02d}:{mi2:02d}"
        await state.update_data(daily_window_start=ws, daily_window_end=we)
    # Дефолты для toggle-экрана (шаг 6/6)
    await state.update_data(
        end_strategy="continue_next_day",
        group_albums=False,
        strip_caption=False,
        shuffle_enabled=True,
        signature_template=None,
    )
    await state.set_state(AddStates.add_queue_end_strategy)
    await _render_queue_settings(message, state)


async def _render_queue_settings(message_or_call: Message | CallbackQuery, state: FSMContext) -> None:
    """Шаг 6/6: экран настроек очереди с toggle-кнопками."""
    data = await state.get_data()
    strategy = data.get("end_strategy", "continue_next_day")
    group_albums = data.get("group_albums", False)
    strip_caption = data.get("strip_caption", False)
    shuffle = data.get("shuffle_enabled", True)
    sig = data.get("signature_template")

    strategy_labels = {
        "continue_next_day": "1) продлить на след. день",
        "continue_after_end": "2) продолжить после конца",
        "move_to_inbox": "3) во Входящие",
        "delete": "4) удалить",
    }
    text = (
        f"📋 <b>Настройки очереди «{data['name']}»</b>\n\n"
        f"Что делать с постами, когда время закончилось:\n"
        f"Стратегия: {strategy_labels.get(strategy, strategy)}\n\n"
        f"Группировать альбомы: {'✅ да' if group_albums else '❌ нет'}\n"
        f"Удалять подписи: {'✅ да' if strip_caption else '❌ нет'}\n"
        f"Перемешать: {'✅ да' if shuffle else '❌ нет'}\n"
        f"Подпись: {sig[:30] + '...' if sig else '— нет'}\n"
    )
    markup = kb.queue_settings_menu(
        strategy=strategy,
        group_albums=group_albums,
        strip_caption=strip_caption,
        shuffle=shuffle,
        has_signature=bool(sig),
    )
    # CallbackQuery → редактируем сообщение бота (call.message.edit_text).
    # Message → отправляем новое сообщение (нельзя редактировать сообщение пользователя).
    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.message.edit_text(text, reply_markup=markup)
    else:
        await message_or_call.answer(text, reply_markup=markup)


# --- Callback-хендлеры toggle-экрана настроек очереди ---

@router.callback_query(AddStates.add_queue_end_strategy, F.data.startswith("qset:"))
async def queue_settings_toggle(call: CallbackQuery, state: FSMContext) -> None:
    """Обрабатывает нажатия toggle-кнопок на экране настроек очереди."""
    action = call.data.split(":")[1]
    data = await state.get_data()

    if action == "strategy":
        strategies = ["continue_next_day", "continue_after_end", "move_to_inbox", "delete"]
        current = data.get("end_strategy", "continue_next_day")
        # Защита от ValueError при corrupted FSM state (CR-H-V3): если в state
        # записано неизвестное значение (старая БД, ручная правка), .index()
        # бросит ValueError → пользователь не сможет открыть настройки очереди.
        try:
            idx = strategies.index(current)
        except ValueError:
            idx = 0
        new_strategy = strategies[(idx + 1) % 4]
        await state.update_data(end_strategy=new_strategy)

    elif action == "group_albums":
        await state.update_data(group_albums=not data.get("group_albums", False))

    elif action == "strip_caption":
        await state.update_data(strip_caption=not data.get("strip_caption", False))

    elif action == "shuffle":
        await state.update_data(shuffle_enabled=not data.get("shuffle_enabled", True))

    elif action == "signature":
        # Переход к вводу подписи
        await state.set_state(AddStates.add_queue_signature)
        await call.message.edit_text(
            "✍️ Пришли текст подписи (будет добавлен к каждому посту очереди):\n"
            "Или `-` чтобы убрать подпись.",
            reply_markup=kb.confirm_cancel(),
        )
        await call.answer()
        return

    elif action == "create":
        # Создать очередь с текущими настройками
        await _create_queue_from_state(call, state)
        await call.answer()
        return

    await call.answer()
    await _render_queue_settings(call, state)


@router.message(AddStates.add_queue_signature, F.text)
async def add_queue_signature(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == "-":
        await state.update_data(signature_template=None)
    else:
        # P2-24: лимит 800 символов (оставляем запас для caption, Telegram limit 1024)
        if len(text) > 800:
            await message.answer("⚠️ Подпись слишком длинная (макс 800 символов). Сократи или /cancel")
            return
        await state.update_data(signature_template=text)
    await state.set_state(AddStates.add_queue_end_strategy)
    await _render_queue_settings(message, state)


async def _create_queue_from_state(call_or_msg: Message | CallbackQuery, state: FSMContext) -> None:
    """Финальное создание очереди из FSM-данных."""
    data = await state.get_data()
    # P2-26: .get() вместо [] для защиты от corrupted state
    ch_id = data.get("channel_id")
    name = data.get("name")
    interval = data.get("interval")
    if ch_id is None or name is None or interval is None:
        # Corrupted FSM state — не крашим, а показываем ошибку
        answer_fn = getattr(call_or_msg, "answer", None) or call_or_msg.message.answer
        await answer_fn("⚠️ Ошибка: данные очереди утеряны. Начни заново: /menu → ➕ Добавить → 📋 Очередь")
        await state.clear()
        return
    group_albums = data.get("group_albums", False)
    strip_caption = data.get("strip_caption", False)
    shuffle = data.get("shuffle_enabled", True)
    sig = data.get("signature_template")
    start_at = data.get("start_at")
    end_at = data.get("end_at")
    daily_window_start = data.get("daily_window_start")
    daily_window_end = data.get("daily_window_end")
    end_strategy = data.get("end_strategy", "continue_next_day")

    qid = await queues_repo.create_queue(
        ch_id,
        name,
        interval_minutes=interval,
        group_albums=group_albums,
        strip_caption=strip_caption,
        shuffle_enabled=shuffle,
        signature_template=sig,
        start_at=start_at,
        end_at=end_at,
        daily_window_start=daily_window_start,
        daily_window_end=daily_window_end,
        end_strategy=end_strategy,
    )
    n = await reload_scheduler()
    group_label = "альбомы группой" if group_albums else "по одному"
    from app.services.queue_time import format_queue_time_info

    time_info = format_queue_time_info({
        "start_at": start_at,
        "end_at": end_at,
        "daily_window_start": daily_window_start,
        "daily_window_end": daily_window_end,
        "end_strategy": end_strategy,
    })

    return_to_post = data.get("return_to_post")
    answer = getattr(call_or_msg, "message", call_or_msg).answer if hasattr(call_or_msg, "message") else call_or_msg.answer
    if return_to_post:
        post = await posts_repo.get_post(return_to_post)
        if post and post.get("queue_id") is None:
            all_queues = await queues_repo.list_queues(active_only=False)
            channel_queues = [q for q in all_queues if q["channel_id"] == ch_id]
            await answer(
                f"✓ Очередь «{name}» создана (#{qid}, каждые {interval} мин, {group_label}).\n"
                f"Время: {time_info}\n"
                f"Теперь выбери её для поста #{return_to_post}:",
                reply_markup=kb.pick_queue_for_post(return_to_post, ch_id, channel_queues),
            )
            await state.clear()
            return
    await answer(
        f"✓ Очередь «{name}» добавлена (id={qid}, каждые {interval} мин, {group_label}).\n"
        f"Время: {time_info}\n"
        f"✓ Планировщик: активных очередей {n}.",
        reply_markup=kb.main_menu(is_admin=True),
    )
    await state.clear()


# ===========================================================================
# Правила маршрутизации (ТЗ §2.2.2) — НОВОЕ
# ===========================================================================

@router.callback_query(F.data == "menu:rules")
async def cb_rules(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    rules = await routing_repo.list_rules(active_only=False)
    queues = await queues_repo.list_queues(active_only=False)
    queues_map = {q["id"]: q["name"] for q in queues}
    if not rules:
        # Если очередей тоже нет — подсказываем создать очередь (правило без очереди не нужно)
        if not queues:
            await call.message.edit_text(
                "🔀 <b>Правила маршрутизации</b>\n\n"
                "Правил пока нет. Посты без правила идут во «Входящие».\n\n"
                "⚠️ Чтобы создать правило, нужна хотя бы одна очередь —\n"
                "правило указывает, в КАКУЮ очередь направлять посты.\n\n"
                "Сначала создай очередь.",
                reply_markup=kb.need_queue_menu(),
            )
            await call.answer()
            return
        await call.message.edit_text(
            "🔀 <b>Правила маршрутизации</b>\n\n"
            "Правил пока нет. Посты без правила идут во «Входящие».\n\n"
            "Добавь правило кнопкой «➕ Добавить» → «🔀 Правило».",
            reply_markup=kb.back_button(),
        )
        await call.answer()
        return
    await call.message.edit_text("🔀 <b>Правила маршрутизации</b> (нажми для удаления):", reply_markup=kb.rules_list(rules, queues_map))
    await call.answer()


@router.callback_query(F.data == "menu:add_rule")
async def cb_add_rule(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    # Проверяем наличие очередей ДО начала flow правила маршрутизации.
    # Правило БЕЗ целевой очереди не имеет смысла — лучше сразу направить
    # пользователя создать очередь, чем позволить заполнить шаги и потом
    # выдать «Нет очередей» (плохой UX из фидбека пользователя).
    queues = await queues_repo.list_queues(active_only=False)
    if not queues:
        await call.message.edit_text(
            "🔀 <b>Добавление правила маршрутизации</b>\n\n"
            "⚠️ Чтобы создать правило, нужна хотя бы одна очередь —\n"
            "правило указывает, в КАКУЮ очередь направлять посты.\n\n"
            "Сначала создай очередь, затем вернись к правилам.",
            reply_markup=kb.need_queue_menu(),
        )
        await call.answer()
        return
    await state.set_state(AddStates.add_rule_pattern)
    await state.update_data(step="source_type")
    await call.message.edit_text(
        "🔀 <b>Добавление правила маршрутизации</b>\n\n"
        "Шаг 1/4: Выбери тип источника:",
        reply_markup=kb.source_type_picker(),
    )
    await call.answer()


@router.callback_query(AddStates.add_rule_pattern, F.data.startswith("menu:rule:stype:"))
async def add_rule_pick_source(call: CallbackQuery, state: FSMContext) -> None:
    source_type = call.data.split(":")[3]
    await state.update_data(source_type=source_type)
    await call.message.edit_text(
        f"Тип: <b>{source_type}</b>\n\n"
        "Шаг 2/4: Пришли паттерн для source_id.\n\n"
        "Примеры:\n"
        "• Для канала: `-1002257882757` (точное совпадение) или `@anime_channel`\n"
        "• Regex (если есть спецсимволы `.*[]()`): `.*rating:safe.*`\n"
        "• Любой: `.*`\n\n"
        "Или /cancel для отмены.",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(AddStates.add_rule_pattern, F.text)
async def add_rule_pattern(message: Message, state: FSMContext) -> None:
    pattern = (message.text or "").strip()
    if not pattern:
        await message.answer("Паттерн не может быть пустым. Введи ещё раз или /cancel")
        return
    await state.update_data(source_pattern=pattern)
    await state.set_state(AddStates.add_rule_pick_queue)
    queues = await queues_repo.list_queues(active_only=False)
    if not queues:
        await message.answer(
            "⚠️ Нет очередей. Сначала создай очередь:\n"
            "/menu → ➕ Добавить → 📋 Очередь",
            reply_markup=kb.back_button(),
        )
        await state.clear()
        return
    await message.answer("Шаг 3/4: Выбери целевую очередь:", reply_markup=kb.rule_pick_queue(queues))


@router.callback_query(AddStates.add_rule_pick_queue, F.data.startswith("menu:rule:queue:"))
async def add_rule_pick_queue(call: CallbackQuery, state: FSMContext) -> None:
    qid = await _safe_int(call.data.split(":")[3], call, "queue_id")
    if qid is None:
        return
    await state.update_data(target_queue_id=qid)
    await state.set_state(AddStates.add_rule_priority)
    await call.message.edit_text(
        f"Очередь #{qid} выбрана.\n"
        "Шаг 4/4: Пришли приоритет (целое число, выше = важнее, например 10).\n"
        "Или 0 для правила по умолчанию.",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(AddStates.add_rule_priority, F.text)
async def add_rule_priority(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await state.clear()
        return
    text = (message.text or "").strip()
    try:
        priority = int(text)
    except ValueError:
        await message.answer("Нужно целое число (например 10). Введи ещё раз или /cancel")
        return
    data = await state.get_data()
    rid = await routing_repo.create_rule(
        source_type=data["source_type"],
        source_pattern=data["source_pattern"],
        target_queue_id=data["target_queue_id"],
        priority=priority,
    )
    await message.answer(
        f"✓ Правило #{rid} создано.\n"
        f"  {data['source_type']} «{data['source_pattern']}» → очередь #{data['target_queue_id']} (приоритет {priority})",
        reply_markup=kb.main_menu(is_admin=True),
    )
    await state.clear()


@router.callback_query(F.data.startswith("menu:rule:"))
async def cb_rule_actions(call: CallbackQuery, user_role: str | None = None) -> None:
    """Действия с правилом: menu:rule:<id> (карточка) или menu:rule:<id>:del."""
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    parts = call.data.split(":")
    rid = await _safe_int(parts[2], call, "rule_id")
    if rid is None:
        return
    action = parts[3] if len(parts) > 3 else None

    if action == "del":
        await routing_repo.remove_rule(rid)
        await call.answer(f"Правило #{rid} удалено", show_alert=True)
        rules = await routing_repo.list_rules(active_only=False)
        queues = await queues_repo.list_queues(active_only=False)
        queues_map = {q["id"]: q["name"] for q in queues}
        await call.message.edit_text(
            "🔀 <b>Правила маршрутизации</b>:" if rules else "🔀 Правил нет.",
            reply_markup=kb.rules_list(rules, queues_map) if rules else kb.back_button(),
        )
        return

    rule = await routing_repo.get_rule(rid)
    if not rule:
        await call.answer("Не найдено", show_alert=True)
        return
    queues = await queues_repo.list_queues(active_only=False)
    qname = next((q["name"] for q in queues if q["id"] == rule["target_queue_id"]), f"#{rule['target_queue_id']}")
    text = (
        f"🔀 <b>Правило #{rule['id']}</b>\n"
        f"Тип источника: {rule['source_type']}\n"
        f"Паттерн: `{html.escape(rule['source_pattern'])}`\n"
        f"Очередь: «{qname}» (#{rule['target_queue_id']})\n"
        f"Приоритет: {rule['priority']}\n"
        f"Активно: {'да' if rule['is_active'] else 'нет'}"
    )
    await call.message.edit_text(text, reply_markup=kb.rule_actions(rule["id"]))
    await call.answer()


# ===========================================================================
# Пользователи (FSM)
# ===========================================================================

@router.callback_query(F.data == "menu:add_user")
async def cb_add_user(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    await state.set_state(AddStates.add_user_tg_id)
    await call.message.edit_text(
        "👤 <b>Добавление пользователя</b>\n\n"
        "Шаг 1/2: Пришли числовой tg_id или перешли сообщение от пользователя.\n\n"
        "Как узнать tg_id:\n"
        "• Попроси пользователя написать @userinfobot — он ответит числом\n"
        "• Или перешли сообщение от пользователя — я извлеку ID\n\n"
        "Или /cancel для отмены.",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(AddStates.add_user_tg_id)
async def add_user_tg_id(message: Message, state: FSMContext) -> None:
    """Принимает И текст (tg_id), И пересланное сообщение (forward)."""
    tg_id = None
    username = None
    if message.forward_origin is not None:
        sender = getattr(message.forward_origin, "sender_user", None)
        if sender is not None:
            tg_id = sender.id
            username = getattr(sender, "username", None)
    if tg_id is None and message.text:
        try:
            tg_id = int(message.text.strip())
        except ValueError:
            await message.answer("Нужно число (tg_id) или пересланное сообщение. Попробуй ещё раз или /cancel")
            return
    if tg_id is None:
        await message.answer("Не понял. Пришли числовой tg_id или перешли сообщение. Или /cancel")
        return
    await state.update_data(tg_id=tg_id, username=username)
    await state.set_state(AddStates.add_user_role)
    await message.answer(
        f"tg_id={tg_id} принят.\nШаг 2/2: Пришли роль — 'admin' или 'moderator'",
        reply_markup=kb.confirm_cancel(),
    )


@router.message(AddStates.add_user_role, F.text)
async def add_user_role(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await state.clear()
        return
    role = (message.text or "").strip().lower()
    if role not in ("admin", "moderator"):
        await message.answer("Нужно 'admin' или 'moderator'. Введи ещё раз или /cancel")
        return
    data = await state.get_data()
    tg_id = data["tg_id"]
    username = data.get("username")
    await users_repo.add_user(tg_id, username, role)
    await message.answer(
        f"✓ Пользователь tg_id={tg_id} добавлен ролью {role}.\nПопроси его написать боту /start.",
        reply_markup=kb.main_menu(is_admin=True),
    )
    await state.clear()


@router.callback_query(F.data == "menu:users")
async def cb_users(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    users = await users_repo.list_users()
    current_tg = call.from_user.id if call.from_user else 0
    await call.message.edit_text("👥 <b>Пользователи</b>:", reply_markup=kb.users_list(users, current_tg))
    await call.answer()


@router.callback_query(F.data.startswith("menu:user:"))
async def cb_user_actions(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    parts = call.data.split(":")
    uid = await _safe_int(parts[2], call, "user_id")
    if uid is None:
        return
    if len(parts) > 3:
        action = parts[3]
        user = await users_repo.get_user(uid)
        if not user:
            await call.answer("Не найден", show_alert=True)
            return
        if action == "toggle":
            # Защита от потери единственного admin (CR-C1 — критика код-ревью):
            # нельзя понизить последнего активного admin до moderator — иначе
            # система останется без admin'а и /add_user перестанет работать.
            # Также нельзя поменять свою роль (как и в action == "del").
            current_tg = call.from_user.id if call.from_user else 0
            if user["tg_id"] == current_tg:
                await call.answer("Нельзя менять свою роль", show_alert=True)
                return
            if user["role"] == "admin":
                users_list = await users_repo.list_users()
                admin_count = sum(
                    1 for u in users_list if u["role"] == "admin" and u.get("is_active")
                )
                if admin_count <= 1:
                    await call.answer("Нельзя понизить последнего admin", show_alert=True)
                    return
            new_role = "moderator" if user["role"] == "admin" else "admin"
            await users_repo.update_user_role(user["tg_id"], new_role)
            await call.answer(f"Роль → {new_role}", show_alert=True)
        elif action == "del":
            if user["tg_id"] == (call.from_user.id if call.from_user else 0):
                await call.answer("Нельзя удалить себя", show_alert=True)
                return
            await users_repo.remove_user(user["tg_id"])
            await call.answer("Удалён", show_alert=True)
        users = await users_repo.list_users()
        current_tg = call.from_user.id if call.from_user else 0
        await call.message.edit_text("👥 Пользователи:", reply_markup=kb.users_list(users, current_tg))
        return
    user = await users_repo.get_user(uid)
    if not user:
        await call.answer("Не найден", show_alert=True)
        return
    is_self = user["tg_id"] == (call.from_user.id if call.from_user else 0)
    text = f"👤 @{user.get('username') or '—'}\nРоль: {user['role']}\ntg_id: {user['tg_id']}"
    await call.message.edit_text(text, reply_markup=kb.user_actions(uid, is_self))
    await call.answer()


# ===========================================================================
# Подменю «Добавить»
# ===========================================================================

@router.callback_query(F.data == "menu:add")
async def cb_add(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    await call.message.edit_text("➕ <b>Добавить</b>:", reply_markup=kb.add_menu())
    await call.answer()


# ===========================================================================
# Статистика
# ===========================================================================

@router.callback_query(F.data == "menu:stats")
async def cb_stats(call: CallbackQuery) -> None:
    total = await posts_repo.total_count()
    pending = await posts_repo.count_by_status("pending")
    sent = await posts_repo.count_by_status("sent")
    failed = await posts_repo.count_by_status("failed")
    inbox = len(await posts_repo.list_inbox(limit=100))
    queues = await queues_repo.list_queues(active_only=True)
    users = await users_repo.list_users()
    rules = await routing_repo.list_rules(active_only=True)
    text = (
        "📊 <b>Статистика</b>\n"
        f"Постов всего: {total}\n"
        f"  • pending: {pending}\n"
        f"  • sent: {sent}\n"
        f"  • failed: {failed}\n"
        f"  • во Входящих: {inbox}\n"
        f"Активных очередей: {len(queues)}\n"
        f"Правил маршрутизации: {len(rules)}\n"
        f"Пользователей: {len(users)}"
    )
    await call.message.edit_text(text, reply_markup=kb.back_button())
    await call.answer()


# ===========================================================================
# Настройки (блок D — служебный канал, пороги уведомлений)
# ===========================================================================

@router.callback_query(F.data == "menu:settings")
async def cb_settings(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    from app.repositories import app_settings as app_settings_repo

    archive_id = await app_settings_repo.get_archive_channel_id()
    threshold = await app_settings_repo.get_notify_threshold()
    archive_text = f"Включён (chat_id={archive_id})" if archive_id else "выключен"
    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"📂 Служебный канал (архив): {archive_text}\n"
        f"🔔 Порог «скоро закончится»: {threshold} постов\n"
    )
    await call.message.edit_text(text, reply_markup=kb.settings_menu())
    await call.answer()


@router.callback_query(F.data.startswith("set:"))
async def settings_toggle(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    action = call.data.split(":")[1]

    if action == "archive":
        # Ввод chat_id архивного канала
        await state.set_state(AddStates.add_archive_channel)
        await call.message.edit_text(
            "📂 <b>Служебный канал (архив)</b>\n\n"
            "Пришли числовой chat_id канала (вида -1002257882757).\n"
            "Бот должен быть админом в нём.\n\n"
            "Или `-` чтобы выключить архив.",
            reply_markup=kb.confirm_cancel(),
        )
        await call.answer()
        return

    if action == "threshold":
        await state.set_state(AddStates.add_threshold)
        await call.message.edit_text(
            "🔔 Порог «скоро закончится»\n\nПришли число (по умолчанию 5):",
            reply_markup=kb.confirm_cancel(),
        )
        await call.answer()
        return

    await call.answer()


@router.message(AddStates.add_archive_channel, F.text)
async def add_archive_channel(message: Message, state: FSMContext) -> None:
    from app.repositories import app_settings as app_settings_repo

    text = (message.text or "").strip()
    if text == "-":
        await app_settings_repo.set_archive_channel_id(None)
        await message.answer("📂 Архив выключен.", reply_markup=kb.main_menu(is_admin=True))
    else:
        try:
            chat_id = int(text)
        except ValueError:
            await message.answer("Нужно число (chat_id). Попробуй ещё раз или /cancel")
            return
        await app_settings_repo.set_archive_channel_id(chat_id)
        await message.answer(f"✓ Архив включён: chat_id={chat_id}", reply_markup=kb.main_menu(is_admin=True))
    await state.clear()


@router.message(AddStates.add_threshold, F.text)
async def add_threshold(message: Message, state: FSMContext) -> None:
    from app.repositories import app_settings as app_settings_repo

    text = (message.text or "").strip()
    try:
        n = int(text)
        if n < 1:
            raise ValueError
    except ValueError:
        await message.answer("Нужно целое число ≥1. Попробуй ещё раз или /cancel")
        return
    await app_settings_repo.set_notify_threshold(n)
    await message.answer(f"✓ Порог «скоро закончится» = {n} постов", reply_markup=kb.main_menu(is_admin=True))
    await state.clear()
