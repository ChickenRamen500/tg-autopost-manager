"""Репозиторий каналов (ТЗ §5.1 channels)."""
from __future__ import annotations

from typing import Any

from app.db import get_db


async def create_channel(tg_chat_id: int, name: str, username: str | None = None, is_active: bool = True) -> int:
    db = get_db()
    cur = await db.execute(
        "INSERT INTO channels (tg_chat_id, name, username, is_active) VALUES (?, ?, ?, ?)",
        (tg_chat_id, name, username.lstrip("@") if username else None, 1 if is_active else 0),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def get_channel(channel_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def get_channel_by_chat_id(tg_chat_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM channels WHERE tg_chat_id = ?", (tg_chat_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def list_channels(active_only: bool = False) -> list[dict[str, Any]]:
    db = get_db()
    if active_only:
        cur = await db.execute("SELECT * FROM channels WHERE is_active = 1 ORDER BY name")
    else:
        cur = await db.execute("SELECT * FROM channels ORDER BY name")
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def set_active(channel_id: int, is_active: bool) -> bool:
    db = get_db()
    cur = await db.execute("UPDATE channels SET is_active = ? WHERE id = ?", (1 if is_active else 0, channel_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def remove_channel(channel_id: int) -> bool:
    db = get_db()
    cur = await db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed
