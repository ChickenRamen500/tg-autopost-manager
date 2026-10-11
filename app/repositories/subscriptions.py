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
) -> int:
    if site not in ("gelbooru", "danbooru"):
        raise ValueError(f"Недопустимый сайт: {site!r} (разрешены gelbooru/danbooru)")
    if not tags:
        raise ValueError("Список тегов пуст")
    db = get_db()
    cur = await db.execute(
        """
        INSERT INTO tag_subscriptions (user_id, site, tags, check_interval_hours)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, site, _tags_to_json(tags), check_interval_hours),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


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
