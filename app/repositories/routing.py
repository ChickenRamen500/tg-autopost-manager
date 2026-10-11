"""Репозиторий правил маршрутизации (ТЗ §5.1 routing_rules, §2.2.2)."""
from __future__ import annotations

from typing import Any

from app.db import get_db


async def create_rule(
    source_type: str,
    source_pattern: str,
    target_queue_id: int,
    priority: int = 0,
    is_active: bool = True,
) -> int:
    db = get_db()
    cur = await db.execute(
        """
        INSERT INTO routing_rules (source_type, source_pattern, target_queue_id, priority, is_active)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source_type, source_pattern, target_queue_id, priority, 1 if is_active else 0),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def get_rule(rule_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM routing_rules WHERE id = ?", (rule_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def list_rules(active_only: bool = True) -> list[dict[str, Any]]:
    """Активные правила, отсортированные по приоритету по убыванию (ТЗ §2.2.2 алгоритм)."""
    db = get_db()
    sql = "SELECT * FROM routing_rules"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY priority DESC, created_at ASC"
    cur = await db.execute(sql)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def set_active(rule_id: int, is_active: bool) -> bool:
    db = get_db()
    cur = await db.execute("UPDATE routing_rules SET is_active = ? WHERE id = ?", (1 if is_active else 0, rule_id))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def remove_rule(rule_id: int) -> bool:
    db = get_db()
    cur = await db.execute("DELETE FROM routing_rules WHERE id = ?", (rule_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed
