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
    add_queue_group_albums = State()
    add_queue_start_end = State()    # start_at / end_at (блок B)
    add_queue_daily_window = State()  # daily_window (блок B)
    add_queue_end_strategy = State()  # end_strategy (блок B)
    # Правило маршрутизации
    add_rule_pattern = State()
    add_rule_pick_queue = State()
    add_rule_priority = State()


# ===========================================================================
# ПРИОРИТЕТНЫЕ ХЕНДЛЕРЫ (перехватывают /cancel, /menu, /start в любом состоянии)
# Должны быть зарегистрированы ПЕРВЫМИ, иначе FSM-хендлеры их перехватят.
# ===========================================================================

@router.message(F.text == "/cancel")
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
        "🏠 **Главное меню**\n\nИспользуй кнопки ниже. Пришли фото/видео/GIF — поставлю в очередь.",
        reply_markup=kb.main_menu(is_admin),
    )


@router.callback_query(F.data == "menu:main")
async def cb_main(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    await state.clear()
    is_admin = user_role == "admin"
    await call.message.edit_text(
        "🏠 **Главное меню**\n\nИспользуй кнопки ниже.",
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
    group_label = "да (send_media_group)" if queue.get("group_albums") else "нет (по одному)"
    from app.services.queue_time import format_queue_time_info

    time_info = format_queue_time_info(queue)
    text = (
        f"📋 **Очередь #{qid} «{queue['name']}»**\n"
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
    text = "📥 **Входящие** (посты без очереди)" if posts else "📥 Входящие пусты"
    await call.message.edit_text(text, reply_markup=kb.inbox_posts(posts))
    await call.answer()


# ===========================================================================
# Карточка поста + действия
# ===========================================================================

@router.callback_query(F.data.startswith("menu:post:"))
async def cb_post(call: CallbackQuery) -> None:
    parts = call.data.split(":")
    post_id = int(parts[2])
    action = parts[3] if len(parts) > 3 else None

    # Интерактивный выбор очереди (блок A): выбор канала → показ очередей канала
    if action == "pickch":
        channel_id = int(parts[4])
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
                "📥 **Входящие** (посты без очереди)" if posts else "📥 Входящие пусты",
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


async def _render_post_card(call: CallbackQuery, post: dict, queues: list[dict]) -> None:
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
    await call.message.edit_text("📢 **Каналы** (куда публикуем):", reply_markup=kb.channels_list(channels))
    await call.answer()


@router.callback_query(F.data == "menu:add_channel")
async def cb_add_channel(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    await state.set_state(AddStates.add_channel_flow)
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
        "📋 **Добавление очереди**\nШаг 1/7: Нажми на канал-получатель:",
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
    channel_id = int(parts[2])
    post_id = int(parts[3])
    # Проверяем канал
    channel = await channels_repo.get_channel(channel_id)
    if not channel:
        await call.answer(f"Канал #{channel_id} не найден", show_alert=True)
        return
    await state.update_data(channel_id=channel_id, return_to_post=post_id)
    await state.set_state(AddStates.add_queue_name)
    await call.message.edit_text(
        f"📋 **Добавление очереди** для канала «{channel['name']}»\n"
        f"Шаг 2/7: Пришли название очереди (например: Утренние посты)",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.callback_query(AddStates.add_queue_pick_channel, F.data.startswith("menu:channel:"))
async def add_queue_pick_channel(call: CallbackQuery, state: FSMContext) -> None:
    ch_id = int(call.data.split(":")[2])
    await state.update_data(channel_id=ch_id)
    await state.set_state(AddStates.add_queue_name)
    await call.message.edit_text(
        f"Канал #{ch_id} выбран.\nШаг 2/7: Пришли название очереди (например: Утренние посты)",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(AddStates.add_queue_name, F.text)
async def add_queue_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введи ещё раз или /cancel")
        return
    await state.update_data(name=name)
    await state.set_state(AddStates.add_queue_interval)
    await message.answer("Шаг 3/7: Пришли интервал публикации в минутах (например 60)", reply_markup=kb.confirm_cancel())


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
    await state.set_state(AddStates.add_queue_group_albums)
    await message.answer(
        "Шаг 4/7: Группировать альбомы при отправке?\n\n"
        "• 'да' — альбом из N фото отправится в канал одним сообщением (send_media_group)\n"
        "• 'нет' — каждое фото отдельным сообщением\n\n"
        "Пришли 'да' или 'нет'.",
        reply_markup=kb.confirm_cancel(),
    )


@router.message(AddStates.add_queue_group_albums, F.text)
async def add_queue_group_albums(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    if text in ("да", "yes", "1", "true", "д"):
        group_albums = True
    elif text in ("нет", "no", "0", "false", "н"):
        group_albums = False
    else:
        await message.answer("Пришли 'да' или 'нет'. Введи ещё раз или /cancel")
        return
    await state.update_data(group_albums=group_albums)
    await state.set_state(AddStates.add_queue_start_end)
    await message.answer(
        "Шаг 5/7: Время начала и конца очереди.\n\n"
        "Формат: `дд.мм.гг чч:мм - дд.мм.гг чч:мм`\n"
        "Пример: `14.10.26 06:00 - 16.10.26 22:00`\n\n"
        "Или `-` если без ограничений (сразу/бесконечно).",
        reply_markup=kb.confirm_cancel(),
    )


def _parse_start_end(text: str) -> tuple[str | None, str | None] | None:
    """Парсит 'дд.мм.гг чч:мм - дд.мм.гг чч:мм' → (start_at_utc, end_at_utc) ISO.

    Возвращает (None, None) для '-', None если парсинг не удался.
    Время вводится в локальном TZ (Asia/Yekaterinburg), хранится в UTC.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    text = text.strip()
    if text == "-":
        return (None, None)
    # Разделяем по ' - '
    if " - " not in text and "-" not in text:
        return None
    parts = text.replace(" - ", "-").split("-", 1)
    if len(parts) != 2:
        return None
    try:
        tz = ZoneInfo(settings.timezone)
        start_str = parts[0].strip()
        end_str = parts[1].strip()
        start_local = datetime.strptime(start_str, "%d.%m.%y %H:%M").replace(tzinfo=tz)
        end_local = datetime.strptime(end_str, "%d.%m.%y %H:%M").replace(tzinfo=tz)
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
        parsed = _parse_start_end(text)
        if parsed is None:
            await message.answer(
                "Не понял формат. Пример: `14.10.26 06:00 - 16.10.26 22:00`\n"
                "Или `-` если без ограничений.",
                reply_markup=kb.confirm_cancel(),
            )
            return
        await state.update_data(start_at=parsed[0], end_at=parsed[1])
    await state.set_state(AddStates.add_queue_daily_window)
    await message.answer(
        "Шаг 6/7: Ежедневное окно отправки (часы работы каждый день).\n\n"
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
        # Парсим "чч:мм - чч:мм"
        import re

        m = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text)
        if not m:
            await message.answer(
                "Не понял формат. Пример: `06:00 - 22:00`\nИли `-` если без окна.",
                reply_markup=kb.confirm_cancel(),
            )
            return
        ws = f"{int(m.group(1)):02d}:{m.group(2)}"
        we = f"{int(m.group(3)):02d}:{m.group(4)}"
        await state.update_data(daily_window_start=ws, daily_window_end=we)
    await state.set_state(AddStates.add_queue_end_strategy)
    await message.answer(
        "Шаг 7/7: Что делать с неотправленными постами, когда время очереди закончилось?\n\n"
        "1 — продолжить на следующий день (по умолчанию)\n"
        "2 — продолжить после конца (игнорировать end_at)\n"
        "3 — перенести во Входящие\n"
        "4 — удалить неотправленные\n\n"
        "Пришли номер (1-4):",
        reply_markup=kb.confirm_cancel(),
    )


@router.message(AddStates.add_queue_end_strategy, F.text)
async def add_queue_end_strategy(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await state.clear()
        return
    text = (message.text or "").strip()
    strategies = {
        "1": "continue_next_day",
        "2": "continue_after_end",
        "3": "move_to_inbox",
        "4": "delete",
    }
    if text not in strategies:
        await message.answer("Пришли номер 1-4. Введи ещё раз или /cancel")
        return
    end_strategy = strategies[text]
    data = await state.get_data()
    ch_id = data["channel_id"]
    name = data["name"]
    interval = data["interval"]
    group_albums = data.get("group_albums", False)
    start_at = data.get("start_at")
    end_at = data.get("end_at")
    daily_window_start = data.get("daily_window_start")
    daily_window_end = data.get("daily_window_end")
    qid = await queues_repo.create_queue(
        ch_id,
        name,
        interval_minutes=interval,
        group_albums=group_albums,
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

    # Если очередь создана из интерактивного выбора поста — вернёмся к выбору очереди
    return_to_post = data.get("return_to_post")
    if return_to_post:
        post = await posts_repo.get_post(return_to_post)
        if post and post.get("queue_id") is None:
            all_queues = await queues_repo.list_queues(active_only=False)
            channel_queues = [q for q in all_queues if q["channel_id"] == ch_id]
            await message.answer(
                f"✓ Очередь «{name}» создана (#{qid}, каждые {interval} мин, {group_label}).\n"
                f"Время: {time_info}\n"
                f"Теперь выбери её для поста #{return_to_post}:",
                reply_markup=kb.pick_queue_for_post(return_to_post, ch_id, channel_queues),
            )
            await state.clear()
            return
    await message.answer(
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
                "🔀 **Правила маршрутизации**\n\n"
                "Правил пока нет. Посты без правила идут во «Входящие».\n\n"
                "⚠️ Чтобы создать правило, нужна хотя бы одна очередь —\n"
                "правило указывает, в КАКУЮ очередь направлять посты.\n\n"
                "Сначала создай очередь.",
                reply_markup=kb.need_queue_menu(),
            )
            await call.answer()
            return
        await call.message.edit_text(
            "🔀 **Правила маршрутизации**\n\n"
            "Правил пока нет. Посты без правила идут во «Входящие».\n\n"
            "Добавь правило кнопкой «➕ Добавить» → «🔀 Правило».",
            reply_markup=kb.back_button(),
        )
        await call.answer()
        return
    await call.message.edit_text("🔀 **Правила маршрутизации** (нажми для удаления):", reply_markup=kb.rules_list(rules, queues_map))
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
            "🔀 **Добавление правила маршрутизации**\n\n"
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
        "🔀 **Добавление правила маршрутизации**\n\n"
        "Шаг 1/4: Выбери тип источника:",
        reply_markup=kb.source_type_picker(),
    )
    await call.answer()


@router.callback_query(AddStates.add_rule_pattern, F.data.startswith("menu:rule:stype:"))
async def add_rule_pick_source(call: CallbackQuery, state: FSMContext) -> None:
    source_type = call.data.split(":")[3]
    await state.update_data(source_type=source_type)
    await call.message.edit_text(
        f"Тип: **{source_type}**\n\n"
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
    qid = int(call.data.split(":")[3])
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
    rid = int(parts[2])
    action = parts[3] if len(parts) > 3 else None

    if action == "del":
        await routing_repo.remove_rule(rid)
        await call.answer(f"Правило #{rid} удалено", show_alert=True)
        rules = await routing_repo.list_rules(active_only=False)
        queues = await queues_repo.list_queues(active_only=False)
        queues_map = {q["id"]: q["name"] for q in queues}
        await call.message.edit_text(
            "🔀 **Правила маршрутизации**:" if rules else "🔀 Правил нет.",
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
        f"🔀 **Правило #{rule['id']}**\n"
        f"Тип источника: {rule['source_type']}\n"
        f"Паттерн: `{rule['source_pattern']}`\n"
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


# ===========================================================================
# Подменю «Добавить»
# ===========================================================================

@router.callback_query(F.data == "menu:add")
async def cb_add(call: CallbackQuery, user_role: str | None = None) -> None:
    if user_role != "admin":
        await call.answer("Только admin", show_alert=True)
        return
    await call.message.edit_text("➕ **Добавить**:", reply_markup=kb.add_menu())
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
        "📊 **Статистика**\n"
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
