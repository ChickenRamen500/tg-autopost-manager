"""Inline-меню (ТЗ §8.3) — главное взаимодействие через кнопки.

Замена текстовым командам: всё через callback-кнопки. FSM используется для
ввода данных при добавлении канала/очереди/пользователя.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import builder as kb
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
    users as users_repo,
)
from app.scheduler import reload_scheduler

logger = logging.getLogger("handlers.menu")

router = Router(name="menu")


# Все FSM-состояния определяем ВНАЧАЛЕ файла — до декораторов, которые их используют.
class AddStates(StatesGroup):
    """FSM состояния для добавления сущностей."""
    # Пользователь
    add_user_tg_id = State()
    add_user_role = State()
    # Канал
    add_channel_name = State()    # chat_id уже сохранён в data при старте FSM
    add_channel_username = State()
    # Очередь
    add_queue_pick_channel = State()
    add_queue_name = State()
    add_queue_interval = State()


# ---------------------------------------------------------------------------
# Главное меню
# ---------------------------------------------------------------------------

@router.message(Command("menu"))
@router.message(Command("start"))
async def cmd_menu(message: Message, user: dict | None = None, user_role: str | None = None) -> None:
    is_admin = user_role == "admin"
    await message.answer(
        "🏠 **Главное меню**\n\nИспользуй кнопки ниже. Пришли фото/видео/GIF — поставлю в очередь.",
        reply_markup=kb.main_menu(is_admin),
    )


@router.callback_query(F.data == "menu:main")
async def cb_main(call: CallbackQuery, user_role: str | None = None) -> None:
    is_admin = user_role == "admin"
    await call.message.edit_text(
        "🏠 **Главное меню**\n\nИспользуй кнопки ниже.",
        reply_markup=kb.main_menu(is_admin),
    )
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


# ---------------------------------------------------------------------------
# Очереди
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:queues")
async def cb_queues(call: CallbackQuery) -> None:
    queues = await queues_repo.list_queues(active_only=False)
    if not queues:
        await call.message.edit_text("📋 Очередей пока нет.", reply_markup=kb.back_button())
        await call.answer()
        return
    await call.message.edit_text("📋 **Очереди** (нажми для просмотра постов):", reply_markup=kb.queues_list(queues))
    await call.answer()


@router.callback_query(F.data.startswith("menu:queue:"))
async def cb_queue(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[2])
    posts = await posts_repo.list_queue_posts(qid, limit=15)
    queue = await queues_repo.get_queue(qid)
    if not queue:
        await call.answer("Очередь не найдена", show_alert=True)
        return
    pending = await queues_repo.count_pending(qid)
    text = (
        f"📋 **Очередь #{qid} «{queue['name']}»**\n"
        f"Канал: #{queue['channel_id']}\n"
        f"Интервал: каждые {queue['interval_minutes']} мин\n"
        f"pending постов: {pending}\n\n"
        f"Последние посты ({len(posts)}):"
    )
    await call.message.edit_text(text, reply_markup=kb.queue_posts(qid, posts))
    await call.answer()


# ---------------------------------------------------------------------------
# Входящие
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:inbox")
async def cb_inbox(call: CallbackQuery) -> None:
    posts = await posts_repo.list_inbox(limit=15)
    text = "📥 **Входящие** (посты без очереди)" if posts else "📥 Входящие пусты"
    await call.message.edit_text(text, reply_markup=kb.inbox_posts(posts))
    await call.answer()


# ---------------------------------------------------------------------------
# Карточка поста + действия
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("menu:post:"))
async def cb_post(call: CallbackQuery) -> None:
    """Карточка поста: menu:post:<id> или menu:post:<id>:<action>[:arg]."""
    parts = call.data.split(":")
    post_id = int(parts[2])
    action = parts[3] if len(parts) > 3 else None

    # Действия над постом
    if action == "toq":
        # menu:post:<id>:toq:<qid> — поставить в очередь
        queue_id = int(parts[4])
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

    # Просто показать карточку
    post = await posts_repo.get_post(post_id)
    if not post:
        await call.answer("Пост не найден", show_alert=True)
        return
    queues = await queues_repo.list_queues(active_only=True)
    await _render_post_card(call, post, queues)


async def _render_post_card(call: CallbackQuery, post: dict, queues: list[dict]) -> None:
    """Отрисовать карточку поста (общий хелпер)."""
    text = (
        f"🖼 **Пост #{post['id']}**\n"
        f"Тип: {post['file_type']}\n"
        f"Источник: {post['source_type']}\n"
        f"Статус: {post['status']}\n"
        f"Очередь: {post.get('queue_id') or '— (Входящие)'}\n"
    )
    if post.get("original_caption"):
        text += f"Подпись: {post['original_caption'][:100]}\n"
    if post.get("tg_file_id"):
        text += f"file_id: {post['tg_file_id'][:20]}...\n"
    await call.message.edit_text(text, reply_markup=kb.post_card(post, queues))


# ---------------------------------------------------------------------------
# Каналы
# ---------------------------------------------------------------------------

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
    await call.message.edit_text("📢 **Каналы** (куда публикуем):", reply_markup=kb.channels_list(channels))
    await call.answer()


@router.callback_query(F.data == "menu:add_channel")
async def cb_add_channel(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    # Первое состояние — вводим chat_id прямо в cb_add_channel_chat_id через message
    await state.set_state(AddStates.add_channel_name)
    await state.update_data(step="chat_id")
    await call.message.edit_text(
        "📢 **Добавление канала**\n\n"
        "Шаг 1/3: Пришли числовой ID канала (вида -1002257882757).\n\n"
        "Как узнать ID:\n"
        "• Перешли любое сообщение из канала боту @userinfobot — он ответит ID\n"
        "• Бот должен быть админом в канале с правом публикации\n\n"
        "Или /cancel для отмены.",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(AddStates.add_channel_name)
async def add_channel_flow(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    """Единый обработчик для всех 3 шагов добавления канала."""
    if user_role != "admin":
        await state.clear()
        return
    data = await state.get_data()
    step = data.get("step", "chat_id")
    text = message.text.strip() if message.text else ""

    if step == "chat_id":
        try:
            tg_chat_id = int(text)
        except ValueError:
            await message.answer("Нужно число вида -1002257882757. Попробуй ещё раз или /cancel")
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


# ---------------------------------------------------------------------------
# Очередь (FSM)
# ---------------------------------------------------------------------------

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
        "📋 **Добавление очереди**\nШаг 1/3: Нажми на канал-получатель:",
        reply_markup=kb.channels_list(channels),
    )
    await call.answer()


@router.callback_query(AddStates.add_queue_pick_channel, F.data.startswith("menu:channel:"))
async def add_queue_pick_channel(call: CallbackQuery, state: FSMContext) -> None:
    ch_id = int(call.data.split(":")[2])
    await state.update_data(channel_id=ch_id)
    await state.set_state(AddStates.add_queue_name)
    await call.message.edit_text(
        f"Канал #{ch_id} выбран.\nШаг 2/3: Пришли название очереди (например: Утренние посты)",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(AddStates.add_queue_name)
async def add_queue_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip() if message.text else ""
    if not name:
        await message.answer("Название не может быть пустым. Попробуй ещё раз или /cancel")
        return
    await state.update_data(name=name)
    await state.set_state(AddStates.add_queue_interval)
    await message.answer("Шаг 3/3: Пришли интервал публикации в минутах (например 60)", reply_markup=kb.confirm_cancel())


@router.message(AddStates.add_queue_interval)
async def add_queue_interval(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await state.clear()
        return
    text = message.text.strip() if message.text else ""
    try:
        interval = int(text)
        if interval < 1:
            raise ValueError
    except ValueError:
        await message.answer("Нужно целое число минут (≥1). Попробуй ещё раз или /cancel")
        return
    data = await state.get_data()
    ch_id = data["channel_id"]
    name = data["name"]
    qid = await queues_repo.create_queue(ch_id, name, interval_minutes=interval)
    n = await reload_scheduler()
    await message.answer(
        f"✓ Очередь «{name}» добавлена (id={qid}, каждые {interval} мин).\n"
        f"✓ Планировщик: активных очередей {n}.",
        reply_markup=kb.main_menu(is_admin=True),
    )
    await state.clear()


# ---------------------------------------------------------------------------
# Пользователи (FSM)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:add_user")
async def cb_add_user(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    await state.set_state(AddStates.add_user_tg_id)
    await call.message.edit_text(
        "👤 **Добавление пользователя**\n\n"
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
    tg_id = None
    username = None
    # Пытаемся извлечь из пересланного сообщения
    if message.forward_origin is not None:
        sender = getattr(message.forward_origin, "sender_user", None)
        if sender is not None:
            tg_id = sender.id
            username = getattr(sender, "username", None)
    if tg_id is None:
        text = message.text.strip() if message.text else ""
        try:
            tg_id = int(text)
        except ValueError:
            await message.answer("Нужно число (tg_id) или пересланное сообщение. Попробуй ещё раз или /cancel")
            return
    await state.update_data(tg_id=tg_id, username=username)
    await state.set_state(AddStates.add_user_role)
    await message.answer(
        f"tg_id={tg_id} принят.\nШаг 2/2: Пришли роль — 'admin' или 'moderator'",
        reply_markup=kb.confirm_cancel(),
    )


@router.message(AddStates.add_user_role)
async def add_user_role(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await state.clear()
        return
    role = (message.text.strip().lower() if message.text else "")
    if role not in ("admin", "moderator"):
        await message.answer("Нужно 'admin' или 'moderator'. Попробуй ещё раз или /cancel")
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
    await call.message.edit_text("👥 **Пользователи**:", reply_markup=kb.users_list(users, current_tg))
    await call.answer()


@router.callback_query(F.data.startswith("menu:user:"))
async def cb_user_actions(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    parts = call.data.split(":")
    uid = int(parts[2])
    if len(parts) > 3:
        action = parts[3]
        user = await users_repo.get_user(uid)
        if not user:
            await call.answer("Не найден", show_alert=True)
            return
        if action == "toggle":
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


# ---------------------------------------------------------------------------
# Добавить (подменю)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:add")
async def cb_add(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    await call.message.edit_text("➕ **Добавить**:", reply_markup=kb.add_menu())
    await call.answer()


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:stats")
async def cb_stats(call: CallbackQuery) -> None:
    total = await posts_repo.total_count()
    pending = await posts_repo.count_by_status("pending")
    sent = await posts_repo.count_by_status("sent")
    failed = await posts_repo.count_by_status("failed")
    inbox = len(await posts_repo.list_inbox(limit=100))
    queues = await queues_repo.list_queues(active_only=True)
    users = await users_repo.list_users()
    text = (
        "📊 **Статистика**\n"
        f"Постов всего: {total}\n"
        f"  • pending: {pending}\n"
        f"  • sent: {sent}\n"
        f"  • failed: {failed}\n"
        f"  • во Входящих: {inbox}\n"
        f"Активных очередей: {len(queues)}\n"
        f"Пользователей: {len(users)}"
    )
    await call.message.edit_text(text, reply_markup=kb.back_button())
    await call.answer()


# ---------------------------------------------------------------------------
# Отмена FSM
# ---------------------------------------------------------------------------

@router.message(F.text == "/cancel")
async def cmd_cancel(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    current = await state.get_state()
    await state.clear()
    if current is not None:
        await message.answer("❌ Действие отменено.", reply_markup=kb.main_menu(user_role == "admin"))
    else:
        await message.answer("🏠 Главное меню.", reply_markup=kb.main_menu(user_role == "admin"))
