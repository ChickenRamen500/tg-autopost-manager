"""Репозиторий пользователей + RBAC (ТЗ §5.1 users, §2.6).

Ролевая модель (ТЗ §2.6):
- admin    — полный доступ (пользователи, каналы, очереди, маршрутизация, настройки)
- moderator — управление очередями/постами/тегами, без доступа к маршрутизации и пользователям

admin неявно имеет права moderator (см. check_access).
"""
from __future__ import annotations

from typing import Any

from app.db import get_db


async def get_user_by_tg_id(tg_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def get_user(user_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def upsert_user(tg_id: int, username: str | None, role: str, is_active: bool = True) -> int:
    """Вставляет или обновляет пользователя. Возвращает id строки."""
    db = get_db()
    cur = await db.execute(
        """
        INSERT INTO users (tg_id, username, role, is_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET
            username = COALESCE(excluded.username, users.username),
            role = excluded.role,
            is_active = excluded.is_active
        """,
        (tg_id, username, role, 1 if is_active else 0),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    if rid:
        return rid
    # ON CONFLICT не возвращает новый lastrowid; достаём вручную
    cur = await db.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,))
    row = await cur.fetchone()
    await cur.close()
    return row["id"] if row else 0


async def add_user(tg_id: int, username: str | None, role: str = "moderator") -> int:
    """Добавляет пользователя ролью moderator по умолчанию (ТЗ §8.1 /add_user)."""
    if role not in ("admin", "moderator"):
        raise ValueError(f"Недопустимая роль: {role!r} (разрешены admin/moderator)")
    return await upsert_user(tg_id, username, role, is_active=True)


async def update_user_role(tg_id: int, role: str) -> bool:
    if role not in ("admin", "moderator"):
        raise ValueError(f"Недопустимая роль: {role!r}")
    db = get_db()
    cur = await db.execute("UPDATE users SET role = ? WHERE tg_id = ?", (role, tg_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def set_active(tg_id: int, is_active: bool) -> bool:
    db = get_db()
    cur = await db.execute("UPDATE users SET is_active = ? WHERE tg_id = ?", (1 if is_active else 0, tg_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def list_users() -> list[dict[str, Any]]:
    db = get_db()
    cur = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def find_by_username(username: str) -> dict[str, Any] | None:
    """Поиск пользователя по @username (без @). Для /remove_user."""
    clean = username.lstrip("@")
    db = get_db()
    cur = await db.execute("SELECT * FROM users WHERE username = ?", (clean,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def remove_user(tg_id: int) -> bool:
    db = get_db()
    cur = await db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


# ---------------------------------------------------------------------------
# RBAC (ТЗ §2.6)
# ---------------------------------------------------------------------------

# Иерархия ролей: старшая роль включает права младших
_ROLE_LEVEL = {"moderator": 1, "admin": 2}


def check_access(user: dict[str, Any] | None, required_role: str) -> bool:
    """Проверка доступа по роли (ТЗ §2.6).

    user — словарь строки users (из get_user_by_tg_id) или None.
    required_role — 'admin' или 'moderator'.
    admin неявно проходит проверку moderator.
    """
    if not user or not user.get("is_active", 0):
        return False
    user_role = user.get("role")
    if user_role not in _ROLE_LEVEL:
        return False
    if required_role not in _ROLE_LEVEL:
        return False
    return _ROLE_LEVEL[user_role] >= _ROLE_LEVEL[required_role]


async def check_access_tg(tg_id: int, required_role: str) -> bool:
    """Удобная обёртка: проверка доступа по tg_id с запросом в БД."""
    user = await get_user_by_tg_id(tg_id)
    return check_access(user, required_role)
