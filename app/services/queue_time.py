"""Логика временного окна очереди (блок B — полировка Фазы 2, ТЗ §2.5.1).

Проверяет, может ли очередь отправлять посты в данный момент:
- start_at / end_at — абсолютные даты (UTC, ISO-строка)
- daily_window_start / daily_window_end — ежедневное окно "HH:MM" (локальное время TZ)
- end_strategy — что делать с pending постами при достижении end_at

Часовой пояс: settings.timezone (Asia/Yekaterinburg по умолчанию).
start_at/end_at хранятся в UTC, daily_window — в локальном времени.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from app.config.settings import settings
from app.repositories import posts as posts_repo

log = logging.getLogger("queue_time")

# Кешируем ZoneInfo, чтобы не пересоздавать на каждый вызов (zoneinfo — тяжёлая инициализация)
_TZ: ZoneInfo | None = None
_TZ_LOCK = asyncio.Lock()


def _parse_iso_utc(s: str | None) -> datetime | None:
    """Парсит ISO-строку в aware datetime (UTC). Возвращает None если s пусто."""
    if not s:
        return None
    try:
        # SQLite может хранить как "2026-10-14 06:00:00" или ISO с T
        s = s.replace("T", " ")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError) as e:
        log.warning("Не удалось распарсить дату %r: %s", s, e)
        return None


def _parse_hhmm(s: str | None) -> time | None:
    """Парсит "HH:MM" в time. Возвращает None если s пусто."""
    if not s:
        return None
    try:
        parts = s.split(":")
        return time(hour=int(parts[0]), minute=int(parts[1]))
    except (ValueError, IndexError, TypeError) as e:
        log.warning("Не удалось распарсить HH:MM %r: %s", s, e)
        return None


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _now_local() -> datetime:
    """Текущее время в локальном TZ (для daily_window).

    ZoneInfo кешируется на уровне модуля (первый вызов создаёт, последующие переиспользуют).
    """
    global _TZ
    if _TZ is None:
        _TZ = ZoneInfo(settings.timezone)
    return datetime.now(_TZ)


def is_in_time_window(queue: dict) -> bool:
    """Может ли очередь отправлять посты прямо сейчас.

    Проверки по порядку:
    1. start_at: если задано и now < start_at → False (ещё не началось)
    2. end_at: если задано и now > end_at → зависит от end_strategy
       (continue_after_end → True, остальные → False, обработка в handle_end_strategy)
    3. daily_window: если задано и текущее локальное время вне окна → False
    """
    now = _now_utc()

    start_at = _parse_iso_utc(queue.get("start_at"))
    if start_at is not None and now < start_at:
        log.debug("Очередь #%s: ещё не началась (start_at=%s, now=%s)", queue.get("id", "?"), start_at, now)
        return False

    end_at = _parse_iso_utc(queue.get("end_at"))
    if end_at is not None and now > end_at:
        # Если end_at прошёл — проверяем end_strategy
        strategy = queue.get("end_strategy", "continue_next_day")
        if strategy == "continue_after_end":
            # Игнорируем end_at — продолжаем по интервалу
            log.debug("Очередь #%s: end_at прошёл, но strategy=continue_after_end — продолжаем", queue.get("id", "?"))
            # Дальше проверим daily_window
        else:
            # continue_next_day / move_to_inbox / delete — не отправляем сейчас,
            # обработка стратегии в handle_end_strategy (вызывает отправителя)
            log.debug("Очередь #%s: end_at прошёл, strategy=%s — не в окне", queue.get("id", "?"), strategy)
            return False

    # daily_window — ежедневное окно в локальном времени.
    # Если задан только один из start/end — warning и игнорируем окно (не падаем).
    ws = _parse_hhmm(queue.get("daily_window_start"))
    we = _parse_hhmm(queue.get("daily_window_end"))
    if (ws is None) != (we is None):  # ровно один задан — warning
        log.warning("Очередь #%s: daily_window задан частично (start=%s, end=%s) — игнорирую окно", queue.get("id", "?"), ws, we)
    elif ws is not None and we is not None:
        local_now = _now_local().time()
        if ws <= we:
            # Обычное окно: 06:00..22:00
            if not (ws <= local_now < we):
                log.debug("Очередь #%s: вне daily_window (%s < %s < %s)", queue.get("id", "?"), ws, local_now, we)
                return False
        else:
            # Окно через полночь: 22:00..06:00
            if not (local_now >= ws or local_now < we):
                log.debug("Очередь #%s: вне daily_window (ночь)", queue.get("id", "?"))
                return False

    return True


async def handle_end_strategy(queue: dict) -> bool:
    """Обрабатывает завершение очереди (когда end_at прошёл).

    Вызывается когда is_in_time_window вернул False из-за end_at.
    Возвращает True если стратегия обработана (посты перемещены/удалены),
    False — если стратегия требует ожидания (continue_next_day).

    Стратегии:
    - continue_next_day: ждём следующего daily_window (ничего не делаем, return False)
    - continue_after_end: не должно сюда попасть (is_in_time_window вернул True)
    - move_to_inbox: все pending → Входящие (queue_id=NULL)
    - delete: удалить все pending посты очереди
    """
    strategy = queue.get("end_strategy", "continue_next_day")
    queue_id = queue.get("id", "?")

    if strategy == "continue_next_day":
        # Просто ждём — ничего не делаем с постами
        log.info("Очередь #%s: end_at прошёл, strategy=continue_next_day — ждём следующего окна", queue_id)
        return False

    if strategy == "move_to_inbox":
        # Все pending посты → Входящие (queue_id=NULL, status остаётся pending)
        posts = await posts_repo.list_queue_posts(queue_id, status="pending", limit=1000)
        moved = 0
        for p in posts:
            await posts_repo.assign_queue(p["id"], None)
            moved += 1
        log.info("Очередь #%s: end_at прошёл, strategy=move_to_inbox — перемещено %d постов во Входящие", queue_id, moved)
        return True

    if strategy == "delete":
        # Удалить все pending посты очереди
        posts = await posts_repo.list_queue_posts(queue_id, status="pending", limit=1000)
        deleted = 0
        for p in posts:
            await posts_repo.delete_post(p["id"])
            deleted += 1
        log.info("Очередь #%s: end_at прошёл, strategy=delete — удалено %d постов", queue_id, deleted)
        return True

    # continue_after_end не должен сюда попасть
    return False


def format_queue_time_info(queue: dict) -> str:
    """Человекочитаемое описание временных настроек очереди (для UI)."""
    parts: list[str] = []
    start = queue.get("start_at")
    end = queue.get("end_at")
    ws = queue.get("daily_window_start")
    we = queue.get("daily_window_end")
    if start:
        parts.append(f"старт: {start}")
    if end:
        parts.append(f"конец: {end}")
    if ws and we:
        parts.append(f"окно: {ws}–{we}")
    strategy = queue.get("end_strategy", "continue_next_day")
    strategy_labels = {
        "continue_next_day": "продолжить на следующий день",
        "continue_after_end": "продолжить после конца",
        "move_to_inbox": "перенести во Входящие",
        "delete": "удалить",
    }
    parts.append(f"стратегия: {strategy_labels.get(strategy, strategy)}")
    return " | ".join(parts) if parts else "без ограничений (бесконечно)"
