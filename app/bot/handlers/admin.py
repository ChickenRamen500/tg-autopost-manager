"""Админ-команды и команды модератора (ТЗ §8.1, §8.2).

Доступ: через фильтр ролей. Admin-команды проверяют user_role == 'admin'.
"""
from __future__ import annotations

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
    lines = ["📋 **Очереди**:"]
    for q in queues:
        active = "✅" if q["is_active"] else "⏸"
        lines.append(
            f"{active} #{q['id']} «{q['name']}» → @{q.get('channel_username') or q['channel_name']} "
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
async def cmd_remove_from_queue(message: Message, command: CommandObject) -> None:
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
        "📊 **Статистика**\n"
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
    if user_role != "admin":
        await message.answer("🔒 Только admin может добавлять пользователей.")
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Использование: /add_user @username <role>\nrole: admin | moderator")
        return
    username = args[0].lstrip("@")
    role = args[1].lower()
    if role not in ("admin", "moderator"):
        await message.answer("role должна быть admin или moderator.")
        return
    # Нам нужен tg_id — его нельзя получить из @username через Bot API без прямого контакта.
    # Поэтому просим админа переслать сообщение от будущего пользователя (как в gbooru_tg),
    # либо используем /add_user_after_forward. Для простоты — просим переслать.
    await message.answer(
        f"Чтобы добавить @{username} ролью {role}, попроси его написать боту /start, "
        "затем перешли мне любое его сообщение командой:\n"
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
    fwd_user = getattr(message, "forward_from", None)
    target_tg_id = None
    if fwd_user is not None:
        target_tg_id = fwd_user.id
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_tg_id = message.reply_to_message.from_user.id
    if not target_tg_id:
        await message.answer("Не удалось определить tg_id. Перешли сообщение от будущего пользователя.")
        return
    await users_repo.add_user(target_tg_id, username, role)
    await message.answer(f"✓ @{username} (tg_id={target_tg_id}) теперь {role}.")


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
    await users_repo.remove_user(target["tg_id"])
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
    lines = ["👥 **Пользователи**:"]
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
            + (f" @{existing['username']}" if existing.get("username") else "")
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
    lines = ["📢 **Каналы**:"]
    for c in channels:
        active = "✅" if c["is_active"] else "⏸"
        lines.append(f"{active} #{c['id']} «{c['name']}» @{c.get('username') or '—'} ({c['tg_chat_id']})")
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
    ok = await channels_repo.remove_channel(cid)
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
    await message.answer(
        f"✓ Очередь «{name}» добавлена (id={qid}, каждые {interval} мин).\n"
        "⚠️ Планировщик подхватит новую очередь после перезапуска контейнера "
        "(docker compose restart bot)."
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
    ok = await queues_repo.remove_queue(qid)
    await message.answer("✓ Очередь удалена." if ok else "Очередь не найдена.")


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
    lines = ["🔔 **Подписки**:"]
    for s in subs:
        tags = s.get("tags") if isinstance(s.get("tags"), list) else json.loads(s.get("tags") or "[]")
        lines.append(f"#{s['id']} {s['site']}: {', '.join(tags)}")
    await message.answer("\n".join(lines))
