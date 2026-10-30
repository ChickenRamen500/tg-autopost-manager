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
    group_albums: bool = False,
    start_at: str | None = None,
    end_at: str | None = None,
    daily_window_start: str | None = None,
    daily_window_end: str | None = None,
    end_strategy: str = "continue_next_day",
    strip_caption: bool = False,
) -> int:
    db = get_db()
    cur = await db.execute(
        """
        INSERT INTO queues
            (channel_id, name, interval_minutes, is_active, crop_video, signature_template,
             shuffle_enabled, group_albums, start_at, end_at, daily_window_start, daily_window_end,
             end_strategy, strip_caption)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            channel_id,
            name,
            interval_minutes,
            1 if is_active else 0,
            1 if crop_video else 0,
            signature_template,
            1 if shuffle_enabled else 0,
            1 if group_albums else 0,
            start_at,
            end_at,
            daily_window_start,
            daily_window_end,
            end_strategy,
            1 if strip_caption else 0,
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
    """Список очередей с каналом (для показа /list_queues, правил маршрутизации и планировщика).

    Использует LEFT JOIN: если канал удалён/недоступен, очередь всё равно видна
    (channel_name=None) — иначе INNER JOIN «прячет» такие очереди и пользователь
    получает ложное «Нет очередей» при создании правила (баг из фидбека).
    """
    db = get_db()
    base_sql = """
        SELECT q.*, c.name AS channel_name, c.tg_chat_id, c.username AS channel_username
        FROM queues q
        LEFT JOIN channels c ON c.id = q.channel_id
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


async def set_group_albums(queue_id: int, group_albums: bool) -> bool:
    """Включить/выключить групповую отправку альбомов для очереди (ТЗ §2.1.1)."""
    db = get_db()
    cur = await db.execute(
        "UPDATE queues SET group_albums = ? WHERE id = ?",
        (1 if group_albums else 0, queue_id),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def update_queue(
    queue_id: int,
    *,
    name: str | None = None,
    channel_id: int | None = None,
    interval_minutes: int | None = None,
    is_active: bool | None = None,
    crop_video: bool | None = None,
    signature_template: str | None = None,
    shuffle_enabled: bool | None = None,
    group_albums: bool | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    daily_window_start: str | None = None,
    daily_window_end: str | None = None,
    end_strategy: str | None = None,
    strip_caption: bool | None = None,
) -> bool:
    """Обновляет поля очереди (только переданные не-None).

    None означает «не трогать это поле» (частичное обновление как PATCH).
    Для полей, допускающих NULL (signature_template, start_at, ...),
    передавайте пустую строку "" чтобы сохранить NULL в БД, или None чтобы
    пропустить. Это позволяет сбросить дату через start_at="".
    """
    db = get_db()
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if channel_id is not None:
        sets.append("channel_id = ?")
        params.append(channel_id)
    if interval_minutes is not None:
        sets.append("interval_minutes = ?")
        params.append(interval_minutes)
    if is_active is not None:
        sets.append("is_active = ?")
        params.append(1 if is_active else 0)
    if crop_video is not None:
        sets.append("crop_video = ?")
        params.append(1 if crop_video else 0)
    if signature_template is not None:
        sets.append("signature_template = ?")
        params.append(signature_template or None)
    if shuffle_enabled is not None:
        sets.append("shuffle_enabled = ?")
        params.append(1 if shuffle_enabled else 0)
    if group_albums is not None:
        sets.append("group_albums = ?")
        params.append(1 if group_albums else 0)
    if start_at is not None:
        sets.append("start_at = ?")
        params.append(start_at or None)
    if end_at is not None:
        sets.append("end_at = ?")
        params.append(end_at or None)
    if daily_window_start is not None:
        sets.append("daily_window_start = ?")
        params.append(daily_window_start or None)
    if daily_window_end is not None:
        sets.append("daily_window_end = ?")
        params.append(daily_window_end or None)
    if end_strategy is not None:
        sets.append("end_strategy = ?")
        params.append(end_strategy)
    if strip_caption is not None:
        sets.append("strip_caption = ?")
        params.append(1 if strip_caption else 0)
    if not sets:
        return False
    params.append(queue_id)
    cur = await db.execute(f"UPDATE queues SET {', '.join(sets)} WHERE id = ?", params)
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed
