"""Репозиторий настроек приложения (блок D — служебный канал, пороги уведомлений).

Key-value хранилище в таблице app_settings.
"""
from __future__ import annotations

from typing import Any

from app.db import get_db

# Ключи настроек
ARCHIVE_CHANNEL_ID = "archive_channel_id"  # chat_id служебного канала (None = выключен)
NOTIFY_THRESHOLD = "notify_threshold"  # порог «скоро закончится» (по умолчанию 5)


async def get(key: str, default: Any = None) -> Any:
    db = get_db()
    cur = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    await cur.close()
    return row["value"] if row else default


async def set(key: str, value: Any) -> None:
    db = get_db()
    await db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (key, str(value) if value is not None else None),
    )
    await db.commit()


async def delete(key: str) -> bool:
    db = get_db()
    cur = await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def all_settings() -> dict[str, str]:
    db = get_db()
    cur = await db.execute("SELECT key, value FROM app_settings ORDER BY key")
    rows = await cur.fetchall()
    await cur.close()
    return {r["key"]: r["value"] for r in rows}


# --- Служебный канал (архив) ---

async def get_archive_channel_id() -> int | None:
    """Возвращает chat_id служебного канала (None = выключен)."""
    val = await get(ARCHIVE_CHANNEL_ID)
    return int(val) if val else None


async def set_archive_channel_id(chat_id: int | None) -> None:
    if chat_id is None:
        await delete(ARCHIVE_CHANNEL_ID)
    else:
        await set(ARCHIVE_CHANNEL_ID, str(chat_id))


# --- Порог уведомлений ---

async def get_notify_threshold() -> int:
    val = await get(NOTIFY_THRESHOLD, "5")
    try:
        return int(val)
    except (ValueError, TypeError):
        return 5


async def set_notify_threshold(n: int) -> None:
    await set(NOTIFY_THRESHOLD, str(n))
