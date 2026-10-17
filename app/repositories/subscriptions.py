"""Репозиторий подписок на теги Gelbooru/Danbooru (ТЗ §5.1 tag_subscriptions, §2.1.4).

Сам автопарсинг подписок — Фаза 3 (worker); здесь CRUD для управления из Telegram (§8.1).
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_db


def _tags_to_json(tags: list[str]) -> str:
    return json.dumps(tags, ensure_ascii=False)


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def create_subscription(
    user_id: int,
    site: str,
    tags: list[str],
    check_interval_hours: int = 6,
    notify_interval_minutes: int = 60,
) -> int:
    """Создаёт подписку на теги.

    Args:
        user_id: ID пользователя в БД (не tg_id).
        site: 'gelbooru' или 'danbooru'.
        tags: список тегов (могут содержать '-tag' для исключения).
        check_interval_hours: как часто worker опрашивает gelbooru.
        notify_interval_minutes: как часто уведомлять админа о новых постах (0 = при каждом чеке).
    """
    if site not in ("gelbooru", "danbooru"):
        raise ValueError(f"Недопустимый сайт: {site!r} (разрешены gelbooru/danbooru)")
    if not tags:
        raise ValueError("Список тегов пуст")
    if check_interval_hours <= 0:
        raise ValueError("check_interval_hours должен быть > 0")
    if notify_interval_minutes < 0:
        raise ValueError("notify_interval_minutes должен быть >= 0")
    db = get_db()
    cur = await db.execute(
        """
        INSERT INTO tag_subscriptions (user_id, site, tags, check_interval_hours, notify_interval_minutes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, site, _tags_to_json(tags), check_interval_hours, notify_interval_minutes),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def get_subscription(subscription_id: int) -> dict[str, Any] | None:
    """Возвращает подписку по id (с распарсенными tags)."""
    db = get_db()
    cur = await db.execute("SELECT * FROM tag_subscriptions WHERE id = ?", (subscription_id,))
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    d = dict(row)
    d["tags"] = _parse_tags(d.get("tags"))
    return d


async def list_subscriptions(user_id: int | None = None, active_only: bool = True) -> list[dict[str, Any]]:
    db = get_db()
    sql = "SELECT * FROM tag_subscriptions"
    conditions = []
    params: list[Any] = []
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    if active_only:
        conditions.append("is_active = 1")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC"
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = _parse_tags(d.get("tags"))
        out.append(d)
    return out


async def list_due_subscriptions() -> list[dict[str, Any]]:
    """Подписки, у которых подошло время проверки (для автопарсинга Фазы 3)."""
    db = get_db()
    cur = await db.execute(
        """
        SELECT * FROM tag_subscriptions
        WHERE is_active = 1
          AND (last_checked_at IS NULL
               OR datetime(last_checked_at, '+' || check_interval_hours || ' hours') <= datetime('now'))
        ORDER BY last_checked_at ASC NULLS FIRST
        """
    )
    rows = await cur.fetchall()
    await cur.close()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = _parse_tags(d.get("tags"))
        out.append(d)
    return out


async def mark_checked(subscription_id: int) -> bool:
    db = get_db()
    cur = await db.execute(
        "UPDATE tag_subscriptions SET last_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
        (subscription_id,),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def set_last_notified(subscription_id: int) -> bool:
    """Обновляет last_notified_at (вызывается после отправки уведомления админу)."""
    db = get_db()
    cur = await db.execute(
        "UPDATE tag_subscriptions SET last_notified_at = CURRENT_TIMESTAMP WHERE id = ?",
        (subscription_id,),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def set_default_queue(subscription_id: int, queue_id: int | None) -> bool:
    """Устанавливает очередь по умолчанию для подписки.

    queue_id=None = всегда спрашивать при отправке (старое поведение).
    Используется в UI карточки поста: если default_queue_id задан, «Отправить»
    сразу отправляет в неё без выбора очереди.
    """
    db = get_db()
    cur = await db.execute(
        "UPDATE tag_subscriptions SET default_queue_id = ? WHERE id = ?",
        (queue_id, subscription_id),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def is_due_for_notify(subscription_id: int) -> bool:
    """True если подошло время уведомления (с учётом notify_interval_minutes).

    notify_interval_minutes=0 → всегда True (уведомлять при каждом чеке).
    Иначе — если last_notified_at NULL или прошло >= notify_interval_minutes минут.
    """
    db = get_db()
    cur = await db.execute(
        "SELECT notify_interval_minutes, last_notified_at FROM tag_subscriptions WHERE id = ?",
        (subscription_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return False
    interval = row["notify_interval_minutes"]
    if interval == 0:
        return True
    if row["last_notified_at"] is None:
        return True
    cur = await db.execute(
        "SELECT CASE WHEN datetime(last_notified_at, '+' || ? || ' minutes') <= datetime('now') "
        "THEN 1 ELSE 0 END AS due FROM tag_subscriptions WHERE id = ?",
        (interval, subscription_id),
    )
    r = await cur.fetchone()
    await cur.close()
    return bool(r and r["due"])


async def set_active(subscription_id: int, is_active: bool) -> bool:
    db = get_db()
    cur = await db.execute(
        "UPDATE tag_subscriptions SET is_active = ? WHERE id = ?",
        (1 if is_active else 0, subscription_id),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def remove_subscription(subscription_id: int) -> bool:
    db = get_db()
    cur = await db.execute("DELETE FROM tag_subscriptions WHERE id = ?", (subscription_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed
