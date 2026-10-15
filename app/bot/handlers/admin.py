"""Админ-команды и команды модератора (ТЗ §8.1, §8.2).

Доступ: через фильтр ролей. Admin-команды проверяют user_role == 'admin'.
"""
from __future__ import annotations

import html
import json
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

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
    subscriptions as subs_repo,
)
from app.repositories import (
    users as users_repo,
)

logger = logging.getLogger("handlers.admin")

router = Router(name="admin")


# --- Модератор + Админ ---

@router.message(Command("list_queues"))
async def cmd_list_queues(message: Message, user: dict | None = None, user_role: str | None = None) -> None:
    queues = await queues_repo.list_queues(active_only=False)
    if not queues:
        await message.answer("Очередей пока нет.")
        return
    lines = ["📋 <b>Очереди</b>:"]
    for q in queues:
        active = "✅" if q["is_active"] else "⏸"
        lines.append(
            f"{active} #{q['id']} «{html.escape(q['name'])}» → @{html.escape(q.get('channel_username') or q['channel_name'])} "
            f"(каждые {q['interval_minutes']} мин)"
        )
    await message.answer("\n".join(lines))


@router.message(Command("queue"))
async def cmd_queue(message: Message, command: CommandObject, user: dict | None = None) -> None:
    if not command.args:
        await message.answer("Использование: /queue <queue_id>")
        return
    try:
        qid = int(command.args.split()[0])
    except ValueError:
        await message.answer("queue_id должен быть числом.")
        return
    posts = await posts_repo.list_queue_posts(qid, limit=20)
    if not posts:
        await message.answer(f"Очередь #{qid} пуста (или не существует).")
        return
    lines = [f"📦 Очередь #{qid} (последние {len(posts)} постов):"]
    for p in posts:
        cap = (p.get("original_caption") or "")[:40]
        lines.append(f"  #{p['id']} [{p['status']}] {p['file_type']} {cap}")
    await message.answer("\n".join(lines))


@router.message(Command("remove_from_queue"))
async def cmd_remove_from_queue(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    if user_role not in ("admin", "moderator"):
        await message.answer("🔒 Доступ: admin/moderator.")
        return
    if not command.args:
        await message.answer("Использование: /remove_from_queue <post_id>")
        return
    try:
        pid = int(command.args.split()[0])
    except ValueError:
        await message.answer("post_id должен быть числом.")
        return
    ok = await posts_repo.assign_queue(pid, None)  # переносим во Входящие
    if ok:
        await message.answer(f"Пост #{pid} перенесён во «Входящие».")
    else:
        await message.answer(f"Пост #{pid} не найден.")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    total = await posts_repo.total_count()
    pending = await posts_repo.count_by_status("pending")
    sent = await posts_repo.count_by_status("sent")
    failed = await posts_repo.count_by_status("failed")
    inbox = len(await posts_repo.list_inbox(limit=1))
    queues = await queues_repo.list_queues(active_only=True)
    users = await users_repo.list_users()
    await message.answer(
        "📊 <b>Статистика</b>\n"
        f"Постов всего: {total}\n"
        f"  • pending: {pending}\n"
        f"  • sent: {sent}\n"
        f"  • failed: {failed}\n"
        f"  • во Входящих: {inbox}\n"
        f"Активных очередей: {len(queues)}\n"
        f"Пользователей: {len(users)}"
    )


# --- Только Admin ---

@router.message(Command("add_user"))
async def cmd_add_user(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    """Добавление пользователя.

    Способ 1 (рекомендуемый) — по числовому tg_id:
        /add_user <tg_id> <role> [имя]
        Пример: /add_user 479924833 moderator sankta17
        tg_id узнаёт сам пользователь у @userinfobot (или пересылаем его сообщение).

    Способ 2 — по @username (требует последующего пересылки сообщения от юзера):
        /add_user @username <role>
        Затем: /set_role @username <role>  (ответом на пересланное сообщение юзера)
    """
    if user_role != "admin":
        await message.answer("🔒 Только admin может добавлять пользователей.")
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            "Использование:\n"
            "/add_user <tg_id> <role> [имя]  — по числовому ID (рекомендуется)\n"
            "/add_user @username <role>     — по @username (нужна пересылка сообщения)\n"
            "role: admin | moderator\n\n"
            "tg_id пользователь узнаёт у @userinfobot (написать ему — он ответит своим ID)."
        )
        return
    identifier = args[0]
    role = args[1].lower()
    if role not in ("admin", "moderator"):
        await message.answer("role должна быть admin или moderator.")
        return

    # Способ 1: числовой tg_id
    if identifier.lstrip("-").isdigit():
        tg_id = int(identifier)
        username = args[2] if len(args) > 2 else None
        await users_repo.add_user(tg_id, username, role)
        await message.answer(
            f"✓ Пользователь tg_id={tg_id} добавлен ролью {role}"
            + (f" (@{username})" if username else "")
            + ".\nПопроси его написать боту /start — после этого он сможет пользоваться ботом."
        )
        return

    # Способ 2: @username — нужен forward для определения tg_id
    username = identifier.lstrip("@")
    await message.answer(
        f"Чтобы добавить @{username} ролью {role}, определи его tg_id:\n"
        "1. Попроси пользователя написать @userinfobot — он ответит числовым ID.\n"
        f"2. Затем: /add_user <его_ID> {role} {username}\n\n"
        "Либо перешли мне любое сообщение от этого пользователя и ответь на него:\n"
        f"/set_role @{username} {role}"
    )


@router.message(Command("set_role"))
async def cmd_set_role(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    """Установка роли пользователю по пересланному сообщению (админ пересылает сообщение юзера)."""
    if user_role != "admin":
        await message.answer("🔒 Только admin может менять роли.")
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Использование: /set_role @username <role> (ответом на пересланное сообщение юзера)")
        return
    username = args[0].lstrip("@")
    role = args[1].lower()
    if role not in ("admin", "moderator"):
        await message.answer("role: admin | moderator")
        return
    # Определяем tg_id целевого пользователя из пересланного сообщения или ответа
    target_tg_id = None
    target_username = None

    # Вариант 1: ответ (reply) на сообщение пользователя в этом чате
    if message.reply_to_message and message.reply_to_message.from_user:
        target_tg_id = message.reply_to_message.from_user.id
        target_username = message.reply_to_message.from_user.username

    # Вариант 2: пересланное сообщение (forward). В aiogram 3.x — forward_origin
    if not target_tg_id and message.forward_origin is not None:
        fwd = message.forward_origin
        # MessageOriginUser — пересылка от пользователя
        sender = getattr(fwd, "sender_user", None)
        if sender is not None:
            target_tg_id = sender.id
            target_username = getattr(sender, "username", None)
        # MessageOriginChannel/Chat — пересылка из канала, sender_user нет (это не ЛС-юзер)

    if not target_tg_id:
        await message.answer(
            "Не удалось определить tg_id.\n"
            "Перешли мне сообщение от будущего пользователя ИЛИ ответь (reply) на его сообщение "
            "в этом чате командой /set_role <role>.\n\n"
            "Альтернатива: добавь по числовому tg_id через /add_user <tg_id> <role>."
        )
        return
    final_username = username or (target_username.lstrip("@") if target_username else None)
    await users_repo.add_user(target_tg_id, final_username, role)
    await message.answer(f"✓ Пользователь tg_id={target_tg_id} теперь {role}.")


@router.message(Command("remove_user"))
async def cmd_remove_user(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    if user_role != "admin":
        await message.answer("🔒 Только admin может удалять пользователей.")
        return
    args = (command.args or "").split()
    if not args:
        await message.answer("Использование: /remove_user @username")
        return
    username = args[0].lstrip("@")
    target = await users_repo.find_by_username(username)
    if not target:
        await message.answer(f"Пользователь @{username} не найден.")
        return
    if target["role"] == "admin" and target["tg_id"] == getattr(message.from_user, "id", 0):
        await message.answer("Нельзя удалить самого себя.")
        return
    try:
        await users_repo.remove_user(target["tg_id"])
    except Exception as e:  # noqa: BLE001 — FK constraint error
        await message.answer(f"⚠️ Не удалось удалить пользователя (есть подписки?): {e}")
        return
    await message.answer(f"✓ @{username} удалён.")


@router.message(Command("list_users"))
async def cmd_list_users(message: Message, user_role: str | None = None) -> None:
    if user_role != "admin":
        await message.answer("🔒 Только admin видит список пользователей.")
        return
    users = await users_repo.list_users()
    if not users:
        await message.answer("Пользователей нет.")
        return
    lines = ["👥 <b>Пользователи</b>:"]
    for u in users:
        active = "✅" if u["is_active"] else "⏸"
        lines.append(f"{active} @{u.get('username') or '—'} ({u['role']}, tg_id={u['tg_id']})")
    await message.answer("\n".join(lines))


@router.message(Command("add_channel"))
async def cmd_add_channel(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    if user_role != "admin":
        await message.answer("🔒 Только admin может добавлять каналы.")
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Использование: /add_channel <tg_chat_id> <name> [@username]")
        return
    try:
        tg_chat_id = int(args[0])
    except ValueError:
        await message.answer("tg_chat_id должен быть числом (например, -1001234567890).")
        return
    name = args[1]
    username = args[2].lstrip("@") if len(args) > 2 else None

    # Проверяем дубликат tg_chat_id ДО вставки (UNIQUE constraint, ТЗ §5.1 channels)
    existing = await channels_repo.get_channel_by_chat_id(tg_chat_id)
    if existing:
        await message.answer(
            f"⚠️ Канал с tg_chat_id={tg_chat_id} уже существует: "
            f"#{existing['id']} «{existing['name']}»"
            + (f" @{html.escape(existing['username'])}" if existing.get("username") else "")
            + ".\nУдалите его (/remove_channel) или используйте другой chat_id."
        )
        return

    try:
        cid = await channels_repo.create_channel(tg_chat_id, name, username)
    except channels_repo.DuplicateChannelError:
        # Race condition: канал создан между нашей проверкой и вставкой
        await message.answer(f"⚠️ Канал с tg_chat_id={tg_chat_id} уже существует (создан параллельно).")
        return
    await message.answer(f"✓ Канал «{name}» добавлен (id={cid}, chat_id={tg_chat_id}).")


@router.message(Command("list_channels"))
async def cmd_list_channels(message: Message) -> None:
    channels = await channels_repo.list_channels()
    if not channels:
        await message.answer("Каналов нет.")
        return
    lines = ["📢 <b>Каналы</b>:"]
    for c in channels:
        active = "✅" if c["is_active"] else "⏸"
        lines.append(f"{active} #{c['id']} «{html.escape(c['name'])}» @{html.escape(c.get('username') or '—')} ({c['tg_chat_id']})")
    await message.answer("\n".join(lines))


@router.message(Command("remove_channel"))
async def cmd_remove_channel(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    if user_role != "admin":
        await message.answer("🔒 Только admin может удалять каналы.")
        return
    try:
        cid = int((command.args or "").split()[0])
    except (ValueError, IndexError):
        await message.answer("Использование: /remove_channel <channel_id>")
        return
    try:
        ok = await channels_repo.remove_channel(cid)
    except Exception as e:  # noqa: BLE001 — FK constraint error
        await message.answer(
            f"⚠️ Не удалось удалить канал: у него есть очереди/посты. "
            f"Сначала удалите очереди канала. (Ошибка: {e})"
        )
        return
    await message.answer("✓ Канал удалён." if ok else "Канал не найден.")


@router.message(Command("add_queue"))
async def cmd_add_queue(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    if user_role != "admin":
        await message.answer("🔒 Только admin может добавлять очереди.")
        return
    args = (command.args or "").split()
    if len(args) < 3:
        await message.answer("Использование: /add_queue <channel_id> <name> <interval_min>")
        return
    try:
        ch_id = int(args[0])
        interval = int(args[2])
    except ValueError:
        await message.answer("channel_id и interval_min должны быть числами.")
        return
    name = args[1]
    channel = await channels_repo.get_channel(ch_id)
    if not channel:
        await message.answer(f"Канал #{ch_id} не найден. Сначала /add_channel.")
        return
    qid = await queues_repo.create_queue(ch_id, name, interval_minutes=interval)
    # Hot-reload: перерегистрируем job'ы планировщика без перезапуска контейнера
    from app.scheduler import reload_scheduler
    n = await reload_scheduler()
    await message.answer(
        f"✓ Очередь «{name}» добавлена (id={qid}, каждые {interval} мин).\n"
        f"✓ Планировщик перерегистрирован: активных очередей — {n}. Перезапуск не нужен."
    )


@router.message(Command("remove_queue"))
async def cmd_remove_queue(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    if user_role != "admin":
        await message.answer("🔒 Только admin может удалять очереди.")
        return
    try:
        qid = int((command.args or "").split()[0])
    except (ValueError, IndexError):
        await message.answer("Использование: /remove_queue <queue_id>")
        return
    try:
        ok = await queues_repo.remove_queue(qid)
    except Exception as e:  # noqa: BLE001 — FK constraint error
        await message.answer(
            f"⚠️ Не удалось удалить очередь: в ней есть посты. "
            f"Сначала очистите посты. (Ошибка: {e})"
        )
        return
    if ok:
        from app.scheduler import reload_scheduler
        n = await reload_scheduler()
        await message.answer(f"✓ Очередь удалена. Активных очередей осталось: {n}.")
    else:
        await message.answer("Очередь не найдена.")


@router.message(Command("reload_scheduler"))
async def cmd_reload_scheduler(message: Message, user_role: str | None = None) -> None:
    """Принудительная перерегистрация job'ов планировщика (без перезапуска контейнера)."""
    if user_role != "admin":
        await message.answer("🔒 Только admin может перезагружать планировщик.")
        return
    from app.scheduler import reload_scheduler
    n = await reload_scheduler()
    await message.answer(f"✓ Планировщик перерегистрирован. Активных очередей: {n}.")


@router.message(Command("add_to_queue"))
async def cmd_add_to_queue(message: Message, command: CommandObject, user_role: str | None = None) -> None:
    """Переместить пост из «Входящих» в очередь (ручная маршрутизация).

    /add_to_queue <post_id> <queue_id>  — поставить пост в очередь
    """
    if user_role not in ("admin", "moderator"):
        await message.answer("🔒 Доступ: admin/moderator.")
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Использование: /add_to_queue <post_id> <queue_id>")
        return
    try:
        post_id = int(args[0])
        queue_id = int(args[1])
    except ValueError:
        await message.answer("post_id и queue_id должны быть числами.")
        return
    queue = await queues_repo.get_queue(queue_id)
    if not queue:
        await message.answer(f"Очередь #{queue_id} не найдена. /list_queues — список.")
        return
    ok = await posts_repo.assign_queue(post_id, queue_id)
    if ok:
        # Если пост был в failed/duplicate — вернём в pending для публикации
        post = await posts_repo.get_post(post_id)
        if post and post["status"] in ("failed", "possible_duplicate", "duplicate"):
            await posts_repo.update_status(post_id, "pending")
        await message.answer(
            f"✓ Пост #{post_id} добавлен в очередь «{html.escape(queue['name'])}» "
            f"(каждые {queue['interval_minutes']} мин)."
        )
    else:
        await message.answer(f"Пост #{post_id} не найден.")


@router.message(Command("list_inbox"))
async def cmd_list_inbox(message: Message, user_role: str | None = None) -> None:
    """Показать посты из «Входящих» (без очереди) для ручной сортировки."""
    inbox = await posts_repo.list_inbox(limit=20)
    if not inbox:
        await message.answer("📥 «Входящие» пусты.")
        return
    lines = [f"📥 «Входящие» (последние {len(inbox)}):"]
    for p in inbox:
        cap = (p.get("original_caption") or "")[:40]
        lines.append(f"  #{p['id']} [{p['status']}] {p['file_type']} {cap}")
    lines.append("\nНазначить очередь: /add_to_queue <post_id> <queue_id>")
    await message.answer("\n".join(lines))


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, command: CommandObject, user: dict | None = None, user_role: str | None = None) -> None:
    if user_role != "admin":
        await message.answer("🔒 Только admin управляет подписками.")
        return
    args = (command.args or "").split()
    if len(args) < 2 or args[0].lower() not in ("gelbooru", "danbooru"):
        await message.answer("Использование: /subscribe gelbooru <tag1> <tag2> ...")
        return
    site = args[0].lower()
    tags = args[1:]
    if not user:
        await message.answer("Пользователь не определён.")
        return
    sid = await subs_repo.create_subscription(user["id"], site, tags)
    await message.answer(f"✓ Подписка #{sid} на {site}: {', '.join(tags)} (проверка каждые 6 ч).\nАвтопарсинг — Фаза 3.")


@router.message(Command("list_subscriptions"))
async def cmd_list_subscriptions(message: Message, user_role: str | None = None) -> None:
    if user_role != "admin":
        await message.answer("🔒 Только admin видит подписки.")
        return
    subs = await subs_repo.list_subscriptions(active_only=True)
    if not subs:
        await message.answer("Подписок нет.")
        return
    lines = ["🔔 <b>Подписки</b>:"]
    for s in subs:
        tags = s.get("tags") if isinstance(s.get("tags"), list) else json.loads(s.get("tags") or "[]")
        lines.append(f"#{s['id']} {s['site']}: {', '.join(tags)}")
    await message.answer("\n".join(lines))
