"""Репозиторий постов (ТЗ §5.1 posts, §2.1.1, §2.3.4 статусы).

Жизненный цикл поста (ТЗ §2.3.4):
  new -> downloading -> pending -> sent
                          |  └-> failed
                          └-> duplicate / possible_duplicate
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_db


def _tags_to_json(tags: list[str] | None) -> str | None:
    if tags is None:
        return None
    return json.dumps(tags, ensure_ascii=False)


def _row_to_post(row: Any) -> dict[str, Any]:
    d = dict(row)
    # JSON-теги распарсим в список (для удобства в handlers/services)
    raw_tags = d.get("tags")
    if raw_tags:
        try:
            d["tags"] = json.loads(raw_tags)
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
    else:
        d["tags"] = []
    return d


async def create_post(
    *,
    source_type: str,
    file_type: str,
    queue_id: int | None = None,
    source_id: str | None = None,
    source_url: str | None = None,
    source_channel: str | None = None,
    file_path: str | None = None,
    tg_file_id: str | None = None,
    file_unique_id: str | None = None,
    file_size: int | None = None,
    mime_type: str | None = None,
    original_caption: str | None = None,
    processed_caption: str | None = None,
    tags: list[str] | None = None,
    status: str = "pending",
    was_processed: bool = False,
    media_group_id: str | None = None,
) -> int:
    """Создаёт пост. По умолчанию статус 'pending' (готов к публикации).

    Для внешних источников (Chrome-расширение, парсеры) используйте status='new'
    — worker Фазы 3 скачает файл и переведёт в pending.
    """
    if status not in ("new", "downloading", "pending", "sent", "failed", "duplicate", "possible_duplicate"):
        raise ValueError(f"Недопустимый статус поста: {status!r}")
    if file_type not in ("photo", "video", "gif", "document"):
        raise ValueError(f"Недопустимый file_type: {file_type!r}")

    db = get_db()
    cur = await db.execute(
        """
        INSERT INTO posts (
            queue_id, source_type, source_id, source_url, source_channel,
            file_path, file_type, tg_file_id, file_unique_id, file_size, mime_type,
            was_processed, original_caption, processed_caption, tags, status, scheduled_time, media_group_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            queue_id,
            source_type,
            source_id,
            source_url,
            source_channel,
            file_path,
            file_type,
            tg_file_id,
            file_unique_id,
            file_size,
            mime_type,
            1 if was_processed else 0,
            original_caption,
            processed_caption,
            _tags_to_json(tags),
            status,
            None,
            media_group_id,
        ),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def delete_post(post_id: int) -> bool:
    """Удаляет пост (для ручной очистки дубликатов из Входящих)."""
    db = get_db()
    cur = await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def find_album_posts(queue_id: int, media_group_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Все pending-посты альбома (с одинаковым media_group_id) в очереди.

    Для групповой отправки через send_media_group (ТЗ §2.1.1).
    Возвращает посты в порядке создания.
    """
    db = get_db()
    cur = await db.execute(
        """
        SELECT * FROM posts
        WHERE queue_id = ? AND media_group_id = ? AND status = 'pending'
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (queue_id, media_group_id, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def get_post(post_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
    row = await cur.fetchone()
    await cur.close()
    return _row_to_post(row) if row else None


async def get_next_pending(queue_id: int) -> dict[str, Any] | None:
    """Следующий пост к отправке из очереди (ТЗ §2.5.1).

    Порядок: по scheduled_time asc (NULLs first), затем по created_at asc.
    При shuffle_enabled очереди перемешивание будет в планировщике (Фаза 5).
    """
    db = get_db()
    cur = await db.execute(
        """
        SELECT * FROM posts
        WHERE queue_id = ? AND status = 'pending'
        ORDER BY scheduled_time IS NOT NULL, scheduled_time ASC, created_at ASC
        LIMIT 1
        """,
        (queue_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return _row_to_post(row) if row else None


async def list_queue_posts(queue_id: int, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    db = get_db()
    if status:
        cur = await db.execute(
            "SELECT * FROM posts WHERE queue_id = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
            (queue_id, status, limit),
        )
    else:
        cur = await db.execute(
            "SELECT * FROM posts WHERE queue_id = ? ORDER BY created_at DESC LIMIT ?",
            (queue_id, limit),
        )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def list_inbox(limit: int = 100) -> list[dict[str, Any]]:
    """«Входящие» — посты без очереди (queue_id IS NULL), ручная сортировка (ТЗ §2.2.2)."""
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM posts WHERE queue_id IS NULL ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def update_status(post_id: int, status: str, error: str | None = None) -> bool:
    if status not in ("new", "downloading", "pending", "sent", "failed", "duplicate", "possible_duplicate"):
        raise ValueError(f"Недопустимый статус поста: {status!r}")
    db = get_db()
    if status == "sent":
        cur = await db.execute(
            "UPDATE posts SET status = 'sent', sent_at = CURRENT_TIMESTAMP, last_error = NULL WHERE id = ?",
            (post_id,),
        )
    else:
        cur = await db.execute(
            "UPDATE posts SET status = ?, last_error = ? WHERE id = ?",
            (status, error, post_id),
        )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def mark_sent(post_id: int) -> bool:
    return await update_status(post_id, "sent")


async def mark_failed(post_id: int, error: str) -> bool:
    return await update_status(post_id, "failed", error=error)


async def increment_attempt(post_id: int) -> int:
    """Увеличивает счётчик попыток отправки. Возвращает новое значение (ТЗ §2.5.2 п.4)."""
    db = get_db()
    cur = await db.execute(
        "UPDATE posts SET send_attempts = send_attempts + 1 WHERE id = ? RETURNING send_attempts",
        (post_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    await db.commit()
    return row["send_attempts"] if row else 0


async def assign_queue(post_id: int, queue_id: int | None) -> bool:
    """Назначить/перенести пост в очередь (или во «Входящие» при queue_id=None)."""
    db = get_db()
    cur = await db.execute("UPDATE posts SET queue_id = ? WHERE id = ?", (queue_id, post_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def set_phash(post_id: int, phash: str) -> bool:
    db = get_db()
    cur = await db.execute("UPDATE posts SET phash = ? WHERE id = ?", (phash, post_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def find_by_file_unique_id(file_unique_id: str) -> list[dict[str, Any]]:
    """Точный поиск дубликата по file_unique_id Telegram (ТЗ §2.4.3)."""
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM posts WHERE file_unique_id = ? ORDER BY created_at DESC LIMIT 5",
        (file_unique_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_post(r) for r in rows]


async def count_by_status(status: str) -> int:
    db = get_db()
    cur = await db.execute("SELECT COUNT(*) AS c FROM posts WHERE status = ?", (status,))
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0


async def total_count() -> int:
    db = get_db()
    cur = await db.execute("SELECT COUNT(*) AS c FROM posts")
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0
