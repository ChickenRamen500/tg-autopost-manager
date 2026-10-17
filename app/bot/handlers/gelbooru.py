"""Handlers UI Gelbooru — подписки, blacklist, просмотр новых постов (ТЗ §2.1.4, Фаза 3 этап 4).

Интерфейс:
  🖼 Gelbooru (главное меню)
    ├── 🆕 Новые посты — список всех новых постов по всем подпискам
    │   └── карточка поста: [📤 Отправить][⏭ Пропустить][⬅ Назад][Далее ➡]
    │       └── Отправить → выбор очереди (или «Во Входящие»)
    ├── 📋 Подписки — список + добавить
    │   └── карточка подписки: [🔄 Проверить][⏸/▶ Вкл][🗑 Удалить]
    └── 🚫 Blacklist тегов — список + добавить/удалить

Доступ: только admin (RBAC в menu.py уже ограничивает кнопку, но дублируем проверку
в каждом handler — защита от прямых callback_data).

FSM:
  GelbooruStates.add_sub_tags — ввод тегов подписки (через пробел)
  GelbooruStates.add_bl_tag  — ввод blacklist-тега
"""
from __future__ import annotations

import html
import logging
from urllib.parse import unquote

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.keyboards import builder as kb
from app.bot.utils.safe_send import safe_edit_text

logger = logging.getLogger("handlers.gelbooru")

router = Router(name="gelbooru")


class GelbooruStates(StatesGroup):
    """FSM состояния для добавления подписки / blacklist-тега."""
    add_sub_tags = State()
    add_bl_tag = State()


# ===========================================================================
# Helpers
# ===========================================================================


async def _safe_int(value: str, call: CallbackQuery | None = None, label: str = "ID") -> int | None:
    """Безопасно парсит int из callback_data."""
    try:
        return int(value)
    except (ValueError, TypeError, IndexError):
        if call:
            await call.answer(f"Неверный {label}: {value}", show_alert=True)
        return None


async def _require_admin(call: CallbackQuery, user_role: str | None) -> bool:
    """Проверка admin-прав. Возвращает True если OK, иначе отвечает alert и False."""
    if user_role != "admin":
        await call.answer("⛔ Только admin", show_alert=True)
        return False
    return True


def _format_seen_card(seen: dict) -> str:
    """Форматирует текст карточки поста Gelbooru."""
    tags = seen.get("tags") or []
    tags_str = ", ".join(tags[:10]) if tags else "—"
    if len(tags) > 10:
        tags_str += f" (+{len(tags) - 10})"
    rating = seen.get("rating") or "—"
    rating_emoji = {"safe": "🟢", "questionable": "🟡", "explicit": "🔴"}.get(rating, "⚪")
    sub_id = seen.get("subscription_id")
    return (
        f"🖼 <b>Пост Gelbooru #{seen['id']}</b>\n"
        f"gelbooru post_id: <code>{seen['post_id']}</code>\n"
        f"Подписка: #{sub_id}\n"
        f"Rating: {rating_emoji} {html.escape(rating)}\n"
        f"Теги: {html.escape(tags_str)}\n"
        f"URL: {html.escape(seen.get('source_url') or '—')}"
    )


async def _replace_message(call: CallbackQuery, text: str, reply_markup=None) -> None:
    """Отправляет НОВОЕ текстовое сообщение и удаляет старое.

    Используется вместо safe_edit_text когда текущее сообщение может быть фото
    (preview поста). edit_text на фото-сообщении падает с 'there is no text in
    the message to edit'. Этот helper всегда работает: отправляет новое + удаляет старое.
    """
    try:
        await call.message.answer(text, reply_markup=reply_markup)
    except Exception as e:  # noqa: BLE001
        logger.warning("_replace_message: не удалось отправить сообщение: %s", e)
    try:
        await call.message.delete()
    except Exception:  # noqa: BLE001 — сообщение уже удалено или нет прав
        pass


async def _send_seen_card(call: CallbackQuery, seen_id: int, list_ctx: dict | None = None) -> None:
    """Рисует карточку seen_post + кнопки навигации.

    Если есть preview_url — отправляет НОВОЕ сообщение с фото (call.message.answer_photo),
    затем удаляет старое. safe_edit_text не умеет превращать текстовое сообщение
    в фото-сообщение, поэтому используем answer_photo + delete.

    list_ctx — контекст списка для навигации (sub_id или None=все).
    Определяет has_prev/has_next по позиции в списке.
    """
    from app.repositories import gelbooru_subs as gs_repo

    seen = await gs_repo.get_seen_post(seen_id)
    if seen is None:
        await safe_edit_text(call.message, "❌ Пост не найден (возможно, уже обработан).", reply_markup=kb.back_button())
        await call.answer()
        return
    if seen["status"] != "new":
        await safe_edit_text(
            call.message,
            f"ℹ️ Пост #{seen_id} уже обработан (статус: {seen['status']}).",
            reply_markup=kb.back_button(),
        )
        await call.answer()
        return

    # Определяем позицию в списке для навигации
    if list_ctx and list_ctx.get("sub_id"):
        items = await gs_repo.list_new_posts(list_ctx["sub_id"])
    else:
        items = await gs_repo.list_new_posts_all()

    # Находим индекс текущего
    idx = -1
    for i, it in enumerate(items):
        if it["id"] == seen_id:
            idx = i
            break
    has_prev = idx > 0
    has_next = 0 <= idx < len(items) - 1

    text = _format_seen_card(seen)
    markup = kb.gelbooru_seen_card(seen_id, has_prev, has_next)

    # Если есть preview_url — скачиваем его (с Referer/UA, т.к. gelbooru блокирует
    # прямые URL для Telegram) и отправляем как BufferedInputFile. send_photo с URL
    # даёт "wrong type of the web page content" — Telegram не может достучаться.
    preview_url = seen.get("preview_url")
    if preview_url:
        try:
            import aiohttp
            from aiogram.types import BufferedInputFile

            # Скачиваем preview через aiohttp с Referer (gelbooru требует)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Referer": "https://gelbooru.com/",
            }
            img_data: bytes | None = None
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(preview_url, headers=headers) as resp:
                    if resp.status == 200:
                        img_data = await resp.read()
                    else:
                        logger.warning("Превью %s: HTTP %d", preview_url, resp.status)

            if img_data:
                from app.bot.runtime import get_bot

                bot = get_bot()
                await bot.send_photo(
                    call.message.chat.id,
                    photo=BufferedInputFile(img_data, filename=f"preview_{seen_id}.jpg"),
                    caption=text,
                    reply_markup=markup,
                )
                # Удаляем старое сообщение
                try:
                    await call.message.delete()
                except Exception:  # noqa: BLE001
                    pass
                await call.answer()
                return
        except Exception as e:  # noqa: BLE001 — fallback на текст
            logger.warning("Не удалось отправить превью для поста #%s: %s — показываю текст", seen_id, e)

    # Fallback: только текст (если нет preview_url или ошибка отправки фото).
    # Используем _replace_message (не safe_edit_text) — текущее сообщение может
    # быть фото от предыдущей карточки, edit_text на фото падает.
    await _replace_message(call, text, reply_markup=markup)
    await call.answer()


# ===========================================================================
# Главное меню Gelbooru
# ===========================================================================


@router.callback_query(F.data == "gb:menu")
async def cb_gelbooru_menu(call: CallbackQuery, user_role: str | None = None) -> None:
    if not await _require_admin(call, user_role):
        return
    from app.repositories import gelbooru_subs as gs_repo

    new_count = await gs_repo.count_new_posts_all()
    text = (
        "🖼 <b>Gelbooru</b>\n\n"
        f"🆕 Новых постов: {new_count}\n\n"
        "Подписки автоматически опрашиваются worker'ом. "
        "Новые посты появляются здесь для ручного разбора."
    )
    await safe_edit_text(call.message, text, reply_markup=kb.gelbooru_main_menu(new_count))
    await call.answer()


# ===========================================================================
# Подписки
# ===========================================================================


@router.callback_query(F.data == "gb:subs")
async def cb_gelbooru_subs(call: CallbackQuery, user_role: str | None = None) -> None:
    if not await _require_admin(call, user_role):
        return
    from app.repositories import gelbooru_subs as gs_repo
    from app.repositories import subscriptions as subs_repo

    subs = await subs_repo.list_subscriptions(active_only=False)
    # Считаем новые посты для каждой подписки (для бейджа)
    new_counts: dict[int, int] = {}
    for s in subs:
        new_counts[s["id"]] = await gs_repo.count_new_posts(s["id"])
    text = f"📋 <b>Подписки Gelbooru</b> ({len(subs)}):"
    await safe_edit_text(call.message, text, reply_markup=kb.gelbooru_subs_list(subs, new_counts))
    await call.answer()


@router.callback_query(F.data.startswith("gb:sub:"))
async def cb_gelbooru_sub_card(call: CallbackQuery, user_role: str | None = None) -> None:
    if not await _require_admin(call, user_role):
        return
    parts = call.data.split(":")
    # gb:sub:<id> | gb:sub:<id>:toggle | gb:sub:<id>:del | gb:sub:<id>:check
    sub_id = await _safe_int(parts[2], call, "sub_id")
    if sub_id is None:
        return
    action = parts[3] if len(parts) > 3 else None

    from app.repositories import gelbooru_subs as gs_repo
    from app.repositories import subscriptions as subs_repo

    sub = await subs_repo.get_subscription(sub_id)
    if sub is None:
        await call.answer("Подписка не найдена", show_alert=True)
        return

    if action == "toggle":
        new_active = not bool(sub["is_active"])
        await subs_repo.set_active(sub_id, new_active)
        sub["is_active"] = 1 if new_active else 0
        await call.answer(f"Подписка {'включена' if new_active else 'выключена'}")
    elif action == "del":
        await subs_repo.remove_subscription(sub_id)
        await call.answer("✅ Подписка удалена")
        # Возвращаемся к списку
        subs = await subs_repo.list_subscriptions(active_only=False)
        new_counts = {s["id"]: await gs_repo.count_new_posts(s["id"]) for s in subs}
        await safe_edit_text(
            call.message, "📋 <b>Подписки Gelbooru</b>:", reply_markup=kb.gelbooru_subs_list(subs, new_counts)
        )
        return
    elif action == "check":
        # Ручная проверка — запускаем check_subscription синхронно.
        # НЕ вызываем call.answer("🔄...") здесь — Telegram разрешает только ОДИН
        # answer на callback; второй бросит TelegramRetryAfter. Финальный answer ниже.
        from app.services.gelbooru_subscriptions import check_subscription

        try:
            new_count = await check_subscription(sub)
            await subs_repo.mark_checked(sub_id)
            sub = await subs_repo.get_subscription(sub_id)  # обновлённый
            await call.answer(f"✅ Проверено: {new_count} новых постов", show_alert=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Ручная проверка подписки #%s: %s", sub_id, e)
            await call.answer(f"⚠️ Ошибка проверки: {e}", show_alert=True)
    elif action == "setqueue":
        # Показать список очередей для выбора default_queue_id
        from app.repositories import queues as queues_repo

        queues = await queues_repo.list_queues(active_only=True)
        # Текущий default_queue_id
        current_qid = sub.get("default_queue_id")
        kb_buttons = InlineKeyboardBuilder()
        kb_buttons.button(
            text="🚫 Всегда спрашивать (сбросить)" if current_qid else "✅ Всегда спрашивать (текущее)",
            callback_data=f"gb:sub:{sub_id}:setqueue:0",
        )
        for q in queues[:10]:
            mark = "✅ " if q["id"] == current_qid else "📋 "
            kb_buttons.button(
                text=f"{mark}«{q['name']}» (#{q['id']})",
                callback_data=f"gb:sub:{sub_id}:setqueue:{q['id']}",
            )
        kb_buttons.button(text="🔙 К подписке", callback_data=f"gb:sub:{sub_id}")
        kb_buttons.adjust(1)
        await safe_edit_text(
            call.message,
            f"📥 <b>Очередь по умолчанию для подписки #{sub_id}</b>\n\n"
            "При нажатии «Отправить» в карточке поста — пост сразу отправляется в эту очередь "
            "(без выбора каждый раз).",
            reply_markup=kb_buttons.as_markup(),
        )
        await call.answer()
        return
    elif action == "setcheck":
        # Показать picker для выбора интервала проверки (часы)
        current = sub.get("check_interval_hours", 6)
        await safe_edit_text(
            call.message,
            f"⏱ <b>Интервал проверки подписки #{sub_id}</b>\n\n"
            "Как часто worker опрашивает gelbooru на новые посты.",
            reply_markup=kb.gelbooru_interval_picker(sub_id, "setcheck", current),
        )
        await call.answer()
        return
    elif action == "setnotify":
        # Показать picker для выбора интервала уведомлений (минуты)
        current = sub.get("notify_interval_minutes", 60)
        await safe_edit_text(
            call.message,
            f"🔔 <b>Интервал уведомлений подписки #{sub_id}</b>\n\n"
            "Как часто присылать уведомление о новых постах. "
            "0 = при каждом чеке (может спамить).",
            reply_markup=kb.gelbooru_interval_picker(sub_id, "setnotify", current),
        )
        await call.answer()
        return
    elif action == "setqueue" and len(call.data.split(":")) > 3:
        # gb:sub:<id>:setqueue:<qid> — установлен default queue
        # (обрабатывается в отдельном elif ниже, т.к. action уже "setqueue")
        pass

    # Проверяем расширенный action: gb:sub:<id>:setqueue:<qid>
    full_parts = call.data.split(":")
    if len(full_parts) >= 5 and full_parts[3] == "setqueue":
        qid_raw = full_parts[4]
        qid = await _safe_int(qid_raw, call, "queue_id")
        if qid is not None:
            # qid=0 = сброс (None), иначе реальный queue_id
            await subs_repo.set_default_queue(sub_id, None if qid == 0 else qid)
            await call.answer("✅ Очередь по умолчанию обновлена")
            sub = await subs_repo.get_subscription(sub_id)

    # Проверяем gb:sub:<id>:setcheck:<hours>
    if len(full_parts) >= 5 and full_parts[3] == "setcheck":
        hours_raw = full_parts[4]
        hours = await _safe_int(hours_raw, call, "hours")
        if hours is not None and hours > 0:
            try:
                await subs_repo.set_check_interval(sub_id, hours)
                await call.answer(f"✅ Интервал проверки: {hours}ч")
                sub = await subs_repo.get_subscription(sub_id)
            except ValueError as e:
                await call.answer(f"⚠️ {e}", show_alert=True)

    # Проверяем gb:sub:<id>:setnotify:<minutes>
    if len(full_parts) >= 5 and full_parts[3] == "setnotify":
        minutes_raw = full_parts[4]
        minutes = await _safe_int(minutes_raw, call, "minutes")
        if minutes is not None and minutes >= 0:
            try:
                await subs_repo.set_notify_interval(sub_id, minutes)
                label = "при каждом чеке" if minutes == 0 else f"{minutes}мин"
                await call.answer(f"✅ Интервал уведомлений: {label}")
                sub = await subs_repo.get_subscription(sub_id)
            except ValueError as e:
                await call.answer(f"⚠️ {e}", show_alert=True)

    # Карточка подписки (default + после toggle/check/setqueue)
    sub = await subs_repo.get_subscription(sub_id)
    if sub is None:
        return
    new_count = await gs_repo.count_new_posts(sub_id)
    tags_str = " ".join(sub.get("tags") or [])
    # Имя default queue для отображения
    default_queue_name = None
    if sub.get("default_queue_id"):
        from app.repositories import queues as queues_repo

        q = await queues_repo.get_queue(sub["default_queue_id"])
        if q:
            default_queue_name = q["name"]
    text = (
        f"📋 <b>Подписка #{sub['id']}</b>\n"
        f"Теги: <code>{html.escape(tags_str)}</code>\n"
        f"Сайт: {sub['site']}\n"
        f"Интервал проверки: {sub['check_interval_hours']}ч\n"
        f"Интервал уведомлений: {sub['notify_interval_minutes']}мин\n"
        f"Очередь по умолчанию: {default_queue_name or 'не задана (спрашивать)'}\n"
        f"Активна: {'✅ да' if sub['is_active'] else '❌ нет'}\n"
        f"Последняя проверка: {sub.get('last_checked_at') or 'никогда'}\n"
        f"🆕 Новых постов: {new_count}"
    )
    await safe_edit_text(call.message, text, reply_markup=kb.gelbooru_sub_card(sub, new_count, default_queue_name))
    if not action:
        await call.answer()


# --- FSM: добавить подписку ---

@router.callback_query(F.data == "gb:add_sub")
async def cb_gelbooru_add_sub(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if not await _require_admin(call, user_role):
        return
    await state.set_state(GelbooruStates.add_sub_tags)
    await safe_edit_text(
        call.message,
        "➕ <b>Добавление подписки Gelbooru</b>\n\n"
        "Пришли теги через пробел. Теги с минусом исключают (-1boy, -rating_explicit).\n\n"
        "<b>Пример:</b> <code>1girl solo blue_hair -1boy</code>\n\n"
        "Глобальные blacklist-теги добавляются автоматически ко всем запросам.",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(GelbooruStates.add_sub_tags, F.text)
async def fsm_add_sub_tags(message: Message, state: FSMContext, user: dict | None = None, user_role: str | None = None) -> None:
    if user_role != "admin":
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Теги не могут быть пустыми. /cancel для отмены.")
        return
    # Разбиваем по пробелам, фильтруем пустые
    tags = [t for t in text.split() if t]
    if not tags:
        await message.answer("Не удалось распарсить теги. /cancel для отмены.")
        return

    from app.repositories import subscriptions as subs_repo

    try:
        sub_id = await subs_repo.create_subscription(
            user_id=user["id"] if user else 1,
            site="gelbooru",
            tags=tags,
            check_interval_hours=6,
            notify_interval_minutes=60,
        )
    except Exception as e:  # noqa: BLE001
        await state.clear()
        await message.answer(f"⚠️ Ошибка создания подписки: {e}")
        return

    await state.clear()
    # Показываем карточку подписки сразу — пользователь видит настройки
    # (check_interval, notify_interval, default_queue) и может изменить их.
    from app.repositories import gelbooru_subs as gs_repo

    sub = await subs_repo.get_subscription(sub_id)
    new_count = await gs_repo.count_new_posts(sub_id)
    tags_str = " ".join(sub.get("tags") or [])
    text = (
        f"✅ <b>Подписка #{sub_id} создана</b>\n\n"
        f"Теги: <code>{html.escape(tags_str)}</code>\n"
        f"Сайт: {sub['site']}\n"
        f"Интервал проверки: {sub['check_interval_hours']}ч\n"
        f"Интервал уведомлений: {sub['notify_interval_minutes']}мин\n"
        f"Очередь по умолчанию: не задана (спрашивать)\n"
        f"Активна: ✅ да\n\n"
        f"📡 Первичная проверка произойдёт автоматически (worker каждые 60с).\n"
        f"Или нажми «🔄 Проверить сейчас» для ручного запуска.\n\n"
        f"📥 Настрой «Очередь по умолчанию» — тогда «Отправить» в карточке поста "
        f"будет сразу отправлять в неё без выбора."
    )
    await message.answer(text, reply_markup=kb.gelbooru_sub_card(sub, new_count, None))


# ===========================================================================
# Новые посты — список + карточка
# ===========================================================================


@router.callback_query(F.data == "gb:seen")
async def cb_gelbooru_seen_all(call: CallbackQuery, user_role: str | None = None) -> None:
    """Список всех новых постов (по всем подпискам)."""
    if not await _require_admin(call, user_role):
        return
    from app.repositories import gelbooru_subs as gs_repo

    items = await gs_repo.list_new_posts_all(limit=15)
    text = f"🆕 <b>Новые посты Gelbooru</b> ({len(items)})"
    await safe_edit_text(call.message, text, reply_markup=kb.gelbooru_seen_list(items, sub_id=None))
    await call.answer()


@router.callback_query(F.data.startswith("gb:seen:"))
async def cb_gelbooru_seen_dispatch(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    """Диспетчер для gb:seen:* callbacks.

    Форматы:
      gb:seen:<sub_id>              — новые посты конкретной подписки (sub_id — int)
      gb:seen:<seen_id>:send        — показать выбор очереди
      gb:seen:<seen_id>:send:<qid>  — подтверждение отправки (qid=0 → Входящие)
      gb:seen:<seen_id>:skip        — пропустить (mark_ignored)
      gb:seen:<seen_id>:next        — следующий пост
      gb:seen:<seen_id>:prev        — предыдущий пост
    """
    if not await _require_admin(call, user_role):
        return
    parts = call.data.split(":")
    # Минимум gb:seen:<id>
    if len(parts) < 3:
        await call.answer("Неверный callback", show_alert=True)
        return

    # gb:seen:<sub_id> — простой случай (2 части после gb:seen)
    if len(parts) == 3:
        sub_id = await _safe_int(parts[2], call, "sub_id")
        if sub_id is None:
            return
        from app.repositories import gelbooru_subs as gs_repo

        items = await gs_repo.list_new_posts(sub_id, limit=15)
        text = f"🆕 <b>Новые посты подписки #{sub_id}</b> ({len(items)})"
        await safe_edit_text(call.message, text, reply_markup=kb.gelbooru_seen_list(items, sub_id=sub_id))
        await call.answer()
        return

    # gb:seen:<seen_id>:<action>[:<qid>]
    seen_id = await _safe_int(parts[2], call, "seen_id")
    if seen_id is None:
        return
    action = parts[3] if len(parts) > 3 else None

    if action == "send":
        # Выбор очереди: gb:seen:<id>:send → показ очередей
        # gb:seen:<id>:send:<qid> → подтверждение
        if len(parts) >= 5:
            qid_raw = parts[4]
            qid = await _safe_int(qid_raw, call, "queue_id")
            if qid is None:
                return
            await _do_send(call, seen_id, queue_id=None if qid == 0 else qid)
            return
        # Проверяем default_queue_id у подписки — если задан, отправляем сразу
        from app.repositories import gelbooru_subs as gs_repo
        from app.repositories import subscriptions as subs_repo

        seen = await gs_repo.get_seen_post(seen_id)
        if seen:
            sub = await subs_repo.get_subscription(seen["subscription_id"])
            if sub and sub.get("default_queue_id"):
                # Отправляем сразу в default queue без выбора
                await _do_send(call, seen_id, queue_id=sub["default_queue_id"])
                return
        # Нет default_queue_id — показываем выбор очереди
        await _show_queue_picker(call, seen_id)
        return

    if action == "skip":
        await _do_skip(call, seen_id)
        return

    if action == "next":
        await _navigate(call, seen_id, direction=1)
        return

    if action == "prev":
        await _navigate(call, seen_id, direction=-1)
        return

    await call.answer(f"Неизвестное действие: {action}", show_alert=True)


async def _show_queue_picker(call: CallbackQuery, seen_id: int) -> None:
    """Показывает кнопки выбора очереди для отправки seen_post."""
    from app.repositories import queues as queues_repo

    queues = await queues_repo.list_queues(active_only=True)
    # Используем _replace_message — текущее сообщение может быть фото (preview).
    await _replace_message(
        call,
        f"📤 <b>Отправить пост #{seen_id}</b>\n\nВыбери очередь или «Во Входящие»:",
        reply_markup=kb.gelbooru_seen_card(seen_id, has_prev=False, has_next=False, queues=queues),
    )
    await call.answer()


async def _do_send(call: CallbackQuery, seen_id: int, queue_id: int | None) -> None:
    """Отправляет seen_post в очередь через send_seen_post_to_queue."""
    from app.services.gelbooru_subscriptions import send_seen_post_to_queue

    try:
        post_pk = await send_seen_post_to_queue(seen_id, queue_id=queue_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Отправка seen_post #%s: %s", seen_id, e)
        await call.answer(f"⚠️ Ошибка: {e}", show_alert=True)
        return

    if post_pk is None:
        await call.answer("❌ Не удалось отправить (пост уже обработан или нет URL)", show_alert=True)
        return

    queue_label = "очередь" if queue_id else "Входящие"
    await call.answer(f"✅ Отправлено в {queue_label} (пост #{post_pk})", show_alert=True)
    # После отправки переходим к следующему посту (как «Далее»), а не к списку.
    from app.repositories import gelbooru_subs as gs_repo

    seen = await gs_repo.get_seen_post(seen_id)
    sub_id = seen.get("subscription_id") if seen else None
    if sub_id:
        items = await gs_repo.list_new_posts(sub_id, limit=50)
    else:
        items = await gs_repo.list_new_posts_all(limit=50)

    if items:
        next_seen_id = items[0]["id"]
        await _send_seen_card(call, next_seen_id, list_ctx={"sub_id": sub_id})
    else:
        all_items = await gs_repo.list_new_posts_all(limit=15)
        await _replace_message(
            call,
            f"🆕 <b>Новые посты Gelbooru</b> ({len(all_items)})\n\nВсе посты просмотрены!",
            reply_markup=kb.gelbooru_seen_list(all_items, sub_id=None),
        )


async def _do_skip(call: CallbackQuery, seen_id: int) -> None:
    """Пропускает seen_post (mark_ignored) и переходит к следующему посту.

    ТЗ §2.1.4: «Пропустить» работает как «Далее» — после пропуска показывается
    следующий пост (а не список). Это удобнее: не нужно искать где остановился.
    Если следующего поста нет — возвращаемся к списку.
    """
    from app.repositories import gelbooru_subs as gs_repo

    ok = await gs_repo.mark_ignored(seen_id)
    if not ok:
        await call.answer("❌ Пост уже обработан или не найден", show_alert=True)
        return
    await call.answer("⏭ Пропущен")

    # Получаем список, чтобы найти следующий пост.
    # Используем подписку текущего поста если она есть, иначе общий список.
    seen = await gs_repo.get_seen_post(seen_id)
    sub_id = seen.get("subscription_id") if seen else None
    if sub_id:
        items = await gs_repo.list_new_posts(sub_id, limit=50)
    else:
        items = await gs_repo.list_new_posts_all(limit=50)

    # Текущий пост уже ignored — его не будет в списке new.
    # Берём ПЕРВЫЙ пост из оставшихся новых (он следующий по порядку).
    if items:
        next_seen_id = items[0]["id"]
        await _send_seen_card(call, next_seen_id, list_ctx={"sub_id": sub_id})
    else:
        # Посты закончились — возвращаемся к списку
        all_items = await gs_repo.list_new_posts_all(limit=15)
        await _replace_message(
            call,
            f"🆕 <b>Новые посты Gelbooru</b> ({len(all_items)})\n\nВсе посты просмотрены!",
            reply_markup=kb.gelbooru_seen_list(all_items, sub_id=None),
        )


async def _navigate(call: CallbackQuery, seen_id: int, direction: int) -> None:
    """Навигация: next (+1) / prev (-1) по списку новых постов."""
    from app.repositories import gelbooru_subs as gs_repo

    seen = await gs_repo.get_seen_post(seen_id)
    if seen is None:
        await call.answer("Пост не найден", show_alert=True)
        return
    sub_id = seen.get("subscription_id")
    # Используем список конкретной подписки если она есть, иначе общий
    if sub_id:
        items = await gs_repo.list_new_posts(sub_id, limit=50)
    else:
        items = await gs_repo.list_new_posts_all(limit=50)

    idx = -1
    for i, it in enumerate(items):
        if it["id"] == seen_id:
            idx = i
            break
    if idx < 0:
        await call.answer("Пост не найден в списке", show_alert=True)
        return
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(items):
        await call.answer("Достигнут конец списка", show_alert=True)
        return
    next_seen_id = items[new_idx]["id"]
    await _send_seen_card(call, next_seen_id, list_ctx={"sub_id": sub_id})


@router.callback_query(F.data.startswith("gb:seen_card:"))
async def cb_gelbooru_seen_card(call: CallbackQuery, user_role: str | None = None) -> None:
    """Открытие карточки поста из списка."""
    if not await _require_admin(call, user_role):
        return
    parts = call.data.split(":")
    seen_id = await _safe_int(parts[2], call, "seen_id")
    if seen_id is None:
        return
    await _send_seen_card(call, seen_id, list_ctx=None)


# ===========================================================================
# Blacklist тегов
# ===========================================================================


@router.callback_query(F.data == "gb:blacklist")
async def cb_gelbooru_blacklist(call: CallbackQuery, user_role: str | None = None) -> None:
    if not await _require_admin(call, user_role):
        return
    from app.repositories import gelbooru_subs as gs_repo

    tags = await gs_repo.list_blacklist_tags()
    text = (
        f"🚫 <b>Blacklist тегов Gelbooru</b> ({len(tags)})\n\n"
        "Эти теги исключаются из ВСЕХ подписок (добавляются как -tag к запросу)."
    )
    await safe_edit_text(call.message, text, reply_markup=kb.gelbooru_blacklist_list(tags))
    await call.answer()


@router.callback_query(F.data.startswith("gb:bl:"))
async def cb_gelbooru_bl_action(call: CallbackQuery, user_role: str | None = None) -> None:
    """gb:bl:<tag_encoded>:del — удалить blacklist-тег."""
    if not await _require_admin(call, user_role):
        return
    parts = call.data.split(":")
    # gb:bl:<tag_encoded>:del
    if len(parts) < 4:
        await call.answer("Неверный callback", show_alert=True)
        return
    encoded_tag = parts[2]
    action = parts[3]
    tag = unquote(encoded_tag)

    from app.repositories import gelbooru_subs as gs_repo

    if action == "del":
        ok = await gs_repo.remove_blacklist_tag(tag)
        if ok:
            await call.answer(f"✅ Тег «{tag}» удалён из blacklist")
        else:
            await call.answer(f"Тег «{tag}» не найден", show_alert=True)
        # Обновляем список
        tags = await gs_repo.list_blacklist_tags()
        await safe_edit_text(
            call.message,
            f"🚫 <b>Blacklist тегов Gelbooru</b> ({len(tags)})",
            reply_markup=kb.gelbooru_blacklist_list(tags),
        )
        return

    await call.answer(f"Неизвестное действие: {action}", show_alert=True)


# --- FSM: добавить blacklist-тег ---

@router.callback_query(F.data == "gb:bl_add")
async def cb_gelbooru_bl_add(call: CallbackQuery, state: FSMContext, user_role: str | None = None) -> None:
    if not await _require_admin(call, user_role):
        return
    await state.set_state(GelbooruStates.add_bl_tag)
    await safe_edit_text(
        call.message,
        "➕ <b>Добавить blacklist-тег</b>\n\n"
        "Пришли тег (без минуса). Например: <code>rating_explicit</code> или <code>1boy</code>.\n\n"
        "Тег будет исключаться из ВСЕХ подписок.",
        reply_markup=kb.confirm_cancel(),
    )
    await call.answer()


@router.message(GelbooruStates.add_bl_tag, F.text)
async def fsm_add_bl_tag(message: Message, state: FSMContext, user_role: str | None = None) -> None:
    if user_role != "admin":
        await state.clear()
        return
    tag = (message.text or "").strip()
    if not tag:
        await message.answer("Тег не может быть пустым. /cancel для отмены.")
        return

    from app.repositories import gelbooru_subs as gs_repo

    try:
        rid = await gs_repo.add_blacklist_tag(tag)
    except ValueError as e:
        await message.answer(f"⚠️ {e}")
        return
    except Exception as e:  # noqa: BLE001
        await state.clear()
        await message.answer(f"⚠️ Ошибка: {e}")
        return

    await state.clear()
    clean = tag.strip().lstrip("-").lower()
    if rid is None:
        await message.answer(f"ℹ️ Тег «{clean}» уже в blacklist.", reply_markup=kb.back_button())
    else:
        await message.answer(f"✅ Тег «{clean}» добавлен в blacklist.", reply_markup=kb.back_button())


# ===========================================================================
# Catch-all для не-текстовых сообщений в FSM GelbooruStates
# (предотвращает «зависание» FSM если пользователь прислал фото вместо тегов)
# ===========================================================================


@router.message(StateFilter(GelbooruStates), ~F.text)
async def fsm_non_text_in_gelbooru_state(message: Message) -> None:
    await message.answer(
        "✏️ Я жду текстовый ответ (теги).\n"
        "Отправь теги или нажми /cancel для отмены."
    )
