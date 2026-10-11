"""Репозиторий очередей (ТЗ §5.1 queues, §2.3)."""
from __future__ import annotations

from typing import Any

from app.db import get_db


async def create_queue(
    channel_id: int,
    name: str,
    interval_minutes: int = 60,
    crop_video: bool = False,
    signature_template: str | None = None,
    shuffle_enabled: bool = True,
    is_active: bool = True,
) -> int:
    db = get_db()
    cur = await db.execute(
        """
        INSERT INTO queues
            (channel_id, name, interval_minutes, is_active, crop_video, signature_template, shuffle_enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            channel_id,
            name,
            interval_minutes,
            1 if is_active else 0,
            1 if crop_video else 0,
            signature_template,
            1 if shuffle_enabled else 0,
        ),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def get_queue(queue_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM queues WHERE id = ?", (queue_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def list_queues(active_only: bool = False) -> list[dict[str, Any]]:
    """Список очередей с каналом (для показа /list_queues и планировщика)."""
    db = get_db()
    base_sql = """
        SELECT q.*, c.name AS channel_name, c.tg_chat_id, c.username AS channel_username
        FROM queues q
        JOIN channels c ON c.id = q.channel_id
    """
    if active_only:
        base_sql += " WHERE q.is_active = 1 AND c.is_active = 1"
    base_sql += " ORDER BY q.created_at ASC"
    cur = await db.execute(base_sql)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def list_active_queues() -> list[dict[str, Any]]:
    """Только активные очереди (для APScheduler, ТЗ §2.5.1)."""
    return await list_queues(active_only=True)


async def set_active(queue_id: int, is_active: bool) -> bool:
    db = get_db()
    cur = await db.execute("UPDATE queues SET is_active = ? WHERE id = ?", (1 if is_active else 0, queue_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def remove_queue(queue_id: int) -> bool:
    db = get_db()
    cur = await db.execute("DELETE FROM queues WHERE id = ?", (queue_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def count_pending(queue_id: int) -> int:
    """Количество постов в очереди со статусом pending (позиция для уведомления)."""
    db = get_db()
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE queue_id = ? AND status = 'pending'",
        (queue_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0
