"""Репозиторий подписей (ТЗ §5.1 signatures).

Конвейер обработки подписей (фильтры, добавление случайных фраз) — Фаза 5;
здесь только CRUD.
"""
from __future__ import annotations

from typing import Any

from app.db import get_db


async def create_signature(text: str, is_active: bool = True) -> int:
    db = get_db()
    cur = await db.execute("INSERT INTO signatures (text, is_active) VALUES (?, ?)", (text, 1 if is_active else 0))
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def list_signatures(active_only: bool = True) -> list[dict[str, Any]]:
    db = get_db()
    sql = "SELECT * FROM signatures"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY created_at DESC"
    cur = await db.execute(sql)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def set_active(signature_id: int, is_active: bool) -> bool:
    db = get_db()
    cur = await db.execute("UPDATE signatures SET is_active = ? WHERE id = ?", (1 if is_active else 0, signature_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def remove_signature(signature_id: int) -> bool:
    db = get_db()
    cur = await db.execute("DELETE FROM signatures WHERE id = ?", (signature_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def random_signature() -> str | None:
    """Случайная активная подпись (для конвейера подписей, ТЗ §2.4.1)."""
    db = get_db()
    cur = await db.execute("SELECT text FROM signatures WHERE is_active = 1 ORDER BY RANDOM() LIMIT 1")
    row = await cur.fetchone()
    await cur.close()
    return row["text"] if row else None
