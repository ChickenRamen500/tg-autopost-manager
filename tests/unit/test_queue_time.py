"""Тесты queue_time (блок B) — выделено из test_regression.py (PR-40).

Покрывает app/services/queue_time.py:
  - is_in_time_window (5 сценариев) + новые edge-cases (overnight, partial, invalid)
  - handle_end_strategy (move_to_inbox, delete)
  - _parse_dates (handler menu) — '-', валидная дата, невалидная
  - format_queue_time_info
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def test_is_in_time_window_no_restrictions():
    """Очередь без ограничений — всегда в окне."""
    from app.services.queue_time import is_in_time_window

    queue = {"id": 1, "start_at": None, "end_at": None, "daily_window_start": None, "daily_window_end": None}
    assert is_in_time_window(queue) is True


def test_is_in_time_window_start_in_future():
    """start_at в будущем — не в окне."""
    from app.services.queue_time import is_in_time_window

    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    queue = {"id": 1, "start_at": future, "end_at": None, "daily_window_start": None, "daily_window_end": None}
    assert is_in_time_window(queue) is False


def test_is_in_time_window_start_in_past():
    """start_at в прошлом — в окне (нет других ограничений)."""
    from app.services.queue_time import is_in_time_window

    past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    queue = {"id": 1, "start_at": past, "end_at": None, "daily_window_start": None, "daily_window_end": None}
    assert is_in_time_window(queue) is True


def test_is_in_time_window_end_passed_continue_after_end():
    """end_at прошёл, strategy=continue_after_end — в окне (продолжаем)."""
    from app.services.queue_time import is_in_time_window

    past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    queue = {
        "id": 1, "start_at": None, "end_at": past,
        "daily_window_start": None, "daily_window_end": None,
        "end_strategy": "continue_after_end",
    }
    assert is_in_time_window(queue) is True


def test_is_in_time_window_end_passed_continue_next_day():
    """end_at прошёл, strategy=continue_next_day — не в окне (ждём)."""
    from app.services.queue_time import is_in_time_window

    past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    queue = {
        "id": 1, "start_at": None, "end_at": past,
        "daily_window_start": None, "daily_window_end": None,
        "end_strategy": "continue_next_day",
    }
    assert is_in_time_window(queue) is False


# ---------------------------------------------------------------------------
# PR-40: новые edge-cases для daily_window
# ---------------------------------------------------------------------------


def test_is_in_time_window_daily_window_overnight():
    """daily_window через полночь (22:00..06:00) — корректно проверяет попадание.

    Этот edge-case раньше не покрывался. Если ws > we — окно «ночное»
    (local_now >= ws OR local_now < we).
    """
    from app.services.queue_time import is_in_time_window
    from unittest.mock import patch

    queue = {
        "id": 1, "start_at": None, "end_at": None,
        "daily_window_start": "22:00", "daily_window_end": "06:00",
    }
    # Мокаем _now_local так, чтобы .time() возвращал нужное время.
    # _now_local возвращает datetime; .time() на нём даёт time-объект.
    # Поэтому возвращаем реальный datetime с нужным часом.
    fake_dt_evening = datetime(2026, 1, 1, 23, 0, 0)  # 23:00
    with patch("app.services.queue_time._now_local", return_value=fake_dt_evening):
        # 23:00 >= 22:00 → в окне (ночное окно через полночь)
        result = is_in_time_window(queue)
        assert result is True, f"23:00 должно быть в окне 22:00-06:00, получили {result}"


def test_is_in_time_window_daily_window_overnight_outside():
    """Вне ночного окна (12:00, окно 22:00..06:00) — не в окне."""
    from app.services.queue_time import is_in_time_window
    from unittest.mock import patch

    queue = {
        "id": 1, "start_at": None, "end_at": None,
        "daily_window_start": "22:00", "daily_window_end": "06:00",
    }
    fake_dt_noon = datetime(2026, 1, 1, 12, 0, 0)  # 12:00
    with patch("app.services.queue_time._now_local", return_value=fake_dt_noon):
        # 12:00 — не в окне 22:00-06:00 (не >= 22:00 и не < 06:00)
        result = is_in_time_window(queue)
        assert result is False, f"12:00 не должно быть в окне 22:00-06:00, получили {result}"


def test_is_in_time_window_partial_daily_window_ignored():
    """daily_window задан частично (только start) — окно игнорируется, не падает.

    В log есть warning, но функция возвращает True (или продолжает проверки).
    """
    from app.services.queue_time import is_in_time_window

    queue = {
        "id": 1, "start_at": None, "end_at": None,
        "daily_window_start": "09:00", "daily_window_end": None,
    }
    # Должно вернуться True — частичное окно игнорируется (warning в логе)
    assert is_in_time_window(queue) is True

    # Обратный случай — только end
    queue2 = {
        "id": 1, "start_at": None, "end_at": None,
        "daily_window_start": None, "daily_window_end": "22:00",
    }
    assert is_in_time_window(queue2) is True


def test_is_in_time_window_invalid_date_strings():
    """Невалидные строки start_at/end_at — не падает, трактуется как None."""
    from app.services.queue_time import is_in_time_window

    queue = {
        "id": 1,
        "start_at": "not-a-date",
        "end_at": "garbage",
        "daily_window_start": None,
        "daily_window_end": None,
    }
    # _parse_iso_utc вернёт None для невалидной строки — проверки start_at/end_at пропустятся
    # Результат — True (нет валидных ограничений)
    result = is_in_time_window(queue)
    assert result is True, f"Невалидные даты должны трактоваться как None, получили {result}"


def test_is_in_time_window_invalid_hhmm_ignored():
    """Невалидные daily_window_start/end — не падает, трактуется как None."""
    from app.services.queue_time import is_in_time_window

    queue = {
        "id": 1, "start_at": None, "end_at": None,
        "daily_window_start": "not-a-time",
        "daily_window_end": "also-bad",
    }
    # _parse_hhmm вернёт None для обоих → (None is None) != (None is None) → False → elif не сработает
    # То есть обе невалидные → окно игнорируется → True
    result = is_in_time_window(queue)
    assert result is True


def test_handle_end_strategy_move_to_inbox(tmp_path):
    """end_strategy=move_to_inbox — все pending посты → Входящие."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo
        from app.services.queue_time import handle_end_strategy

        ch_id = await channels_repo.create_channel(-1000000000088, "EndCh")
        qid = await queues_repo.create_queue(ch_id, "EndQ")
        # 3 pending поста
        for i in range(3):
            await posts_repo.create_post(
                source_type="telegram_direct", file_type="photo", queue_id=qid,
                tg_file_id=f"f{i}", file_unique_id=f"u{i}",
            )
        past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        queue = await queues_repo.get_queue(qid)
        queue["end_at"] = past
        queue["end_strategy"] = "move_to_inbox"

        result = await handle_end_strategy(queue)
        assert result is True
        # Все посты должны быть во Входящих (queue_id=None)
        inbox = await posts_repo.list_inbox(limit=100)
        assert len(inbox) == 3, f"Ожидали 3 поста во Входящих, получили {len(inbox)}"

    run_with_db(tmp_path, body)


def test_handle_end_strategy_delete(tmp_path):
    """end_strategy=delete — все pending посты удалены."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo
        from app.services.queue_time import handle_end_strategy

        ch_id = await channels_repo.create_channel(-1000000000099, "DelCh")
        qid = await queues_repo.create_queue(ch_id, "DelQ")
        for i in range(3):
            await posts_repo.create_post(
                source_type="telegram_direct", file_type="photo", queue_id=qid,
                tg_file_id=f"df{i}", file_unique_id=f"du{i}",
            )
        past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        queue = await queues_repo.get_queue(qid)
        queue["end_at"] = past
        queue["end_strategy"] = "delete"

        result = await handle_end_strategy(queue)
        assert result is True
        # Все посты удалены
        total = await posts_repo.total_count()
        assert total == 0, f"Ожидали 0 постов, получили {total}"

    run_with_db(tmp_path, body)


def test_handle_end_strategy_continue_next_day_returns_false(tmp_path):
    """end_strategy=continue_next_day — ничего не делает, возвращает False."""
    from conftest import run_with_db

    async def body():
        from app.repositories import channels as channels_repo
        from app.repositories import posts as posts_repo
        from app.repositories import queues as queues_repo
        from app.services.queue_time import handle_end_strategy

        ch_id = await channels_repo.create_channel(-1000000000077, "CndCh")
        qid = await queues_repo.create_queue(ch_id, "CndQ")
        # 2 pending поста — НЕ должны быть удалены/перемещены
        for i in range(2):
            await posts_repo.create_post(
                source_type="telegram_direct", file_type="photo", queue_id=qid,
                tg_file_id=f"cnd{i}", file_unique_id=f"cnd_u{i}",
            )
        queue = await queues_repo.get_queue(qid)
        queue["end_strategy"] = "continue_next_day"

        result = await handle_end_strategy(queue)
        assert result is False, "continue_next_day должен вернуть False (ждём)"

        # Посты остались в очереди
        total = await posts_repo.total_count()
        assert total == 2, f"Посты не должны быть удалены, но total={total}"

    run_with_db(tmp_path, body)


def test_parse_dates_dash():
    """Парсинг '-' → (None, None) — без ограничений."""
    from app.bot.handlers.menu import _parse_dates

    result = _parse_dates("-")
    assert result == (None, None)


def test_parse_dates_valid():
    """Парсинг '14.10.26 06:00 - 16.10.26 22:00' → (start_utc, end_utc)."""
    from app.bot.handlers.menu import _parse_dates

    result = _parse_dates("14.10.26 - 16.10.26")
    assert result is not None
    assert result[0] is not None
    assert result[1] is not None
    # Проверяем, что это ISO-строки
    assert "2026-10-13" in result[0]
    assert "2026-10-16" in result[1]


def test_parse_dates_invalid():
    """Парсинг невалидной строки → None."""
    from app.bot.handlers.menu import _parse_dates

    assert _parse_dates("неправильный формат") is None


def test_format_queue_time_info():
    """Человекочитаемое описание временных настроек очереди."""
    from app.services.queue_time import format_queue_time_info

    queue = {
        "start_at": "2026-10-13 06:00:00",
        "end_at": "2026-10-15 22:00:00",
        "daily_window_start": "06:00",
        "daily_window_end": "22:00",
        "end_strategy": "continue_next_day",
    }
    info = format_queue_time_info(queue)
    assert "старт" in info
    assert "конец" in info
    assert "окно" in info
    assert "продолжить на следующий день" in info


def test_format_queue_time_info_no_restrictions():
    """Все поля None — format_queue_time_info возвращает только стратегию.

    Примечание: «без ограничений (бесконечно)» в коде срабатывает только если parts=[]
    (что невозможно — стратегия всегда добавляется). При всех None вернётся только
    строка стратегии. Это можно считать небольшим UX-багом, но не критичным.
    """
    from app.services.queue_time import format_queue_time_info

    queue = {
        "start_at": None,
        "end_at": None,
        "daily_window_start": None,
        "daily_window_end": None,
        "end_strategy": "continue_next_day",
    }
    info = format_queue_time_info(queue)
    # strategy всегда добавляется — даже при пустых полях
    assert "стратегия" in info
    assert "продолжить на следующий день" in info


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
