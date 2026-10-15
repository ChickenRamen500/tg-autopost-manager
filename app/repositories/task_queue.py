"""Репозиторий персистентной очереди задач (ТЗ §5.1 task_queue, §4.2.2).

Персистентность переживает рестарт (в отличие от asyncio.Queue).
Worker циклически читает task_queue со статусом queued.
Сам worker — Фаза 3; здесь CRUD-операции + метод take_next для атомарного захвата.
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_db


def _payload_to_json(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False)


def _parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def enqueue(post_id: int, task_type: str, payload: dict[str, Any] | None = None) -> int:
    """Поставить задачу в очередь (status='queued')."""
    if task_type not in ("download", "process", "repost"):
        raise ValueError(f"Недопустимый task_type: {task_type!r}")
    db = get_db()
    cur = await db.execute(
        "INSERT INTO task_queue (post_id, task_type, payload, status) VALUES (?, ?, ?, 'queued')",
        (post_id, task_type, _payload_to_json(payload)),
    )
    await db.commit()
    rid = cur.lastrowid
    await cur.close()
    return rid


async def take_next() -> dict[str, Any] | None:
    """Атомарно захватить следующую queued-задачу (new -> in_progress).

    Использует UPDATE ... RETURNING (SQLite ≥3.35) — один SQL-запрос, без гонок.
    Возвращает задачу с распарсенным payload или None, если очередь пуста.
    """
    db = get_db()
    cur = await db.execute(
        """
        UPDATE task_queue
        SET status = 'in_progress', attempts = attempts + 1
        WHERE id = (
            SELECT id FROM task_queue WHERE status = 'queued'
            ORDER BY created_at ASC LIMIT 1
        )
        RETURNING *
        """
    )
    row = await cur.fetchone()
    await cur.close()
    await db.commit()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = _parse_payload(d.get("payload"))
    return d


async def mark_done(task_id: int) -> bool:
    db = get_db()
    cur = await db.execute("UPDATE task_queue SET status = 'done' WHERE id = ?", (task_id,))
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def mark_error(task_id: int, error: str) -> bool:
    db = get_db()
    cur = await db.execute(
        "UPDATE task_queue SET status = 'error', error = ? WHERE id = ?",
        (error, task_id),
    )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def requeue(task_id: int, reset_attempts: bool = False) -> bool:
    """Вернуть ошибочную задачу обратно в очередь (для ручного retry).

    По умолчанию (reset_attempts=False) счётчик attempts НЕ сбрасывается —
    сохраняется видимая «история» количества неудачных попыток (CR-L-R3).
    Если передать reset_attempts=True — счётчик обнулится (полный retry).
    """
    db = get_db()
    if reset_attempts:
        cur = await db.execute(
            "UPDATE task_queue SET status = 'queued', error = NULL, attempts = 0 WHERE id = ?",
            (task_id,),
        )
    else:
        cur = await db.execute(
            "UPDATE task_queue SET status = 'queued', error = NULL WHERE id = ?",
            (task_id,),
        )
    await db.commit()
    changed = cur.rowcount > 0
    await cur.close()
    return changed


async def count_stale_in_progress(timeout_minutes: int = 30) -> int:
    """Количество зависших in_progress задач (worker упал, не вернув done/error).

    in_progress старше timeout_minutes считается «перевисшим» — его worker
    уже не подтвердит (упал / рестарт контейнера / OOM-kill).

    Сравнение <= (а не <): при timeout_minutes=0 все in_progress-задачи должны
    считаться stale, а строгий < пропускал бы задачи, созданные в ту же
    секунду (created_at == now, T < T = False). Это критично для тестов и
    для случая «worker упал мгновенно после старта».
    """
    db = get_db()
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM task_queue "
        "WHERE status = 'in_progress' "
        "AND datetime(created_at, '+' || ? || ' minutes') <= datetime('now')",
        (timeout_minutes,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0


async def reset_stale_in_progress(timeout_minutes: int = 30) -> int:
    """Сбрасывает зависшие in_progress задачи обратно в 'queued'.

    Возвращает количество сброшенных задач. Idempotent: при повторном вызове
    без новых stale-задач вернёт 0. Атомарный UPDATE ... RETURNING был бы
    избыточен — фоновый cleanup запускается по расписанию, гонок нет.
    """
    db = get_db()
    cur = await db.execute(
        "UPDATE task_queue SET status = 'queued' "
        "WHERE status = 'in_progress' "
        "AND datetime(created_at, '+' || ? || ' minutes') <= datetime('now')",
        (timeout_minutes,),
    )
    await db.commit()
    changed = cur.rowcount
    await cur.close()
    return changed


async def count_by_status(status: str) -> int:
    db = get_db()
    cur = await db.execute("SELECT COUNT(*) AS c FROM task_queue WHERE status = ?", (status,))
    row = await cur.fetchone()
    await cur.close()
    return row["c"] if row else 0
