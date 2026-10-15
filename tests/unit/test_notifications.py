"""Тесты уведомлений + throttle-state (блок C).

PR-40 (H-T4): app/services/notifications.py раньше не имел тестов — coverage = 0%.
Покрываем:
  1. notify_admin — вызывает bot.send_message с правильным chat_id (admin_tg_id).
  2. notify_queue_almost_empty — throttle: второй вызов в течение 5 мин НЕ отправляет.
  3. notify_queue_finished — one-shot: второй вызов НЕ отправляет.
  4. reset_finished(queue_id) — сбрасывает one-shot, следующий finished отправляется.
  5. notify_send_error — не падает даже если bot.send_message падает (логирует warning).
  6. reset_finished(unknown_queue_id) — не падает (discard молча).

Важно: throttle-state (module-level dict/set) очищается перед каждым тестом через
autouse-фикстуру reset_throttle — иначе тесты влияют друг на друга.

Используем mock для app.bot.runtime.get_bot (возвращает MagicMock с AsyncMock send_message).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import notifications


@pytest.fixture(autouse=True)
def reset_throttle():
    """Очищает module-level throttle-state до и после каждого теста.

    Без этого _last_almost_empty / _last_finished «протекают» между тестами —
    первый тест может заблокировать второй (one-shot flag).
    """
    notifications._last_almost_empty.clear()
    notifications._last_finished.clear()
    yield
    notifications._last_almost_empty.clear()
    notifications._last_finished.clear()


def _set_admin_tg_id(tg_id: int) -> None:
    """Временно выставляет settings.admin_tg_id — frozen dataclass, через object.__setattr__."""
    from app.config.settings import settings

    object.__setattr__(settings, "admin_tg_id", tg_id)


def _make_mock_bot() -> tuple[MagicMock, AsyncMock]:
    """MagicMock bot с AsyncMock для send_message.

    Возвращает (bot, send_message_mock) для удобных assert'ов.
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot, bot.send_message


# ---------------------------------------------------------------------------
# notify_admin
# ---------------------------------------------------------------------------


def test_notify_admin_sends_to_admin_chat_id():
    """notify_admin вызывает bot.send_message с chat_id=settings.admin_tg_id."""
    _set_admin_tg_id(123456789)
    bot, send_msg = _make_mock_bot()

    async def body():
        # notify_admin импортирует get_bot лениво — мокаем внутри app.bot.runtime
        with patch("app.bot.runtime.get_bot", return_value=bot):
            # app_settings.get_archive_channel_id тоже может быть вызван — мокаем
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=None),
            ):
                await notifications.notify_admin("Привет, админ!")

    import asyncio

    asyncio.run(body())
    send_msg.assert_awaited_once()
    # Первый позиционный аргумент — chat_id
    args, kwargs = send_msg.await_args
    assert args[0] == 123456789 or kwargs.get("chat_id") == 123456789
    # Второй аргумент — text
    assert args[1] == "Привет, админ!" or kwargs.get("text") == "Привет, админ!"


def test_notify_admin_skips_when_admin_tg_id_zero():
    """Если admin_tg_id=0 — send_message не вызывается (ранний return)."""
    _set_admin_tg_id(0)
    bot, send_msg = _make_mock_bot()

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            await notifications.notify_admin("text")

    import asyncio

    asyncio.run(body())
    send_msg.assert_not_awaited()


def test_notify_admin_swallows_send_error():
    """Если bot.send_message падает — notify_admin логирует warning, но не падает."""
    _set_admin_tg_id(111)
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=None),
            ):
                # Не должно поднять исключение
                await notifications.notify_admin("test")

    import asyncio

    asyncio.run(body())  # не упало — OK


# ---------------------------------------------------------------------------
# notify_queue_almost_empty — throttle
# ---------------------------------------------------------------------------


def test_notify_queue_almost_empty_throttle_second_call_skipped():
    """Два вызова почти_empty в течение 5 минут — второй НЕ отправляет (throttle)."""
    _set_admin_tg_id(222)
    bot, send_msg = _make_mock_bot()

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=None),
            ):
                with patch(
                    "app.repositories.app_settings.get_notify_threshold",
                    new=AsyncMock(return_value=10),
                ):
                    # Первый вызов — remaining=5 (≤ threshold=10) → отправляет
                    await notifications.notify_queue_almost_empty(1, "Q1", "Ch1", 5)
                    # Второй вызов — throttle сработает, НЕ отправляет
                    await notifications.notify_queue_almost_empty(1, "Q1", "Ch1", 5)

    import asyncio

    asyncio.run(body())
    # Только один send_message (для admin) — второй throttled
    send_msg.assert_awaited_once()
    # Внутренний state должен помнить queue_id=1
    assert 1 in notifications._last_almost_empty


def test_notify_queue_almost_empty_skipped_if_above_threshold():
    """Если remaining > threshold — не отправляет (даже первый раз)."""
    _set_admin_tg_id(333)
    bot, send_msg = _make_mock_bot()

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=None),
            ):
                with patch(
                    "app.repositories.app_settings.get_notify_threshold",
                    new=AsyncMock(return_value=5),
                ):
                    # remaining=10 > threshold=5 → не отправляет
                    await notifications.notify_queue_almost_empty(2, "Q2", "Ch2", 10)

    import asyncio

    asyncio.run(body())
    send_msg.assert_not_awaited()
    # throttle-state не установлен (ранний return до throttle-логики)
    assert 2 not in notifications._last_almost_empty


# ---------------------------------------------------------------------------
# notify_queue_finished — one-shot
# ---------------------------------------------------------------------------


def test_notify_queue_finished_one_shot_second_call_skipped():
    """notify_queue_finished — один раз на queue_id; второй вызов НЕ отправляет."""
    _set_admin_tg_id(444)
    bot, send_msg = _make_mock_bot()

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=None),
            ):
                # Первый вызов — отправляет
                await notifications.notify_queue_finished(10, "Q10", "Ch10")
                # Второй — one-shot flag уже стоит, НЕ отправляет
                await notifications.notify_queue_finished(10, "Q10", "Ch10")

    import asyncio

    asyncio.run(body())
    send_msg.assert_awaited_once()
    assert 10 in notifications._last_finished


def test_reset_finished_clears_one_shot_flag():
    """reset_finished сбрасывает one-shot — следующий finished снова отправляется."""
    _set_admin_tg_id(555)
    bot, send_msg = _make_mock_bot()

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=None),
            ):
                # Первый finished — отправляет
                await notifications.notify_queue_finished(20, "Q20", "Ch20")
                # Сбрасываем one-shot
                notifications.reset_finished(20)
                # Снова finished — должен отправить
                await notifications.notify_queue_finished(20, "Q20", "Ch20")

    import asyncio

    asyncio.run(body())
    assert send_msg.await_count == 2


def test_reset_finished_unknown_queue_id_does_not_crash():
    """reset_finished для неизвестного queue_id — discard молча, не падает."""
    # Нет БД, нет bot — просто проверяем что функция не поднимает исключение
    notifications.reset_finished(999_999_999)
    # И повторно — тоже не падает
    notifications.reset_finished(999_999_999)


# ---------------------------------------------------------------------------
# notify_send_error
# ---------------------------------------------------------------------------


def test_notify_send_error_calls_notify_admin_with_formatted_text():
    """notify_send_error формирует текст и вызывает notify_admin (через bot.send_message)."""
    _set_admin_tg_id(666)
    bot, send_msg = _make_mock_bot()

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=None),
            ):
                await notifications.notify_send_error(
                    queue_id=30, queue_name="ErrorQ", post_id=42, error="Forbidden: bot was blocked"
                )

    import asyncio

    asyncio.run(body())
    send_msg.assert_awaited_once()
    args, kwargs = send_msg.await_args
    # Текст должен содержать queue_id, queue_name, post_id, error
    text = args[1] if len(args) > 1 else kwargs.get("text", "")
    assert "30" in text
    assert "ErrorQ" in text
    assert "42" in text
    assert "Forbidden: bot was blocked" in text


def test_notify_send_error_truncates_long_error_text():
    """error длиннее 200 символов — обрезается до error[:200]."""
    _set_admin_tg_id(777)
    bot, send_msg = _make_mock_bot()
    long_error = "X" * 500

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=None),
            ):
                await notifications.notify_send_error(
                    queue_id=40, queue_name="LongQ", post_id=1, error=long_error
                )

    import asyncio

    asyncio.run(body())
    args, kwargs = send_msg.await_args
    text = args[1] if len(args) > 1 else kwargs.get("text", "")
    # 500 X'ов обрезаны до 200
    assert "X" * 200 in text
    assert "X" * 201 not in text


# ---------------------------------------------------------------------------
# Дублирование в служебный канал
# ---------------------------------------------------------------------------


def test_notify_admin_duplicates_to_archive_channel_when_set():
    """Если archive_channel_id задан — notify_admin отправляет и туда (с префиксом 🔔)."""
    _set_admin_tg_id(888)
    bot, send_msg = _make_mock_bot()

    async def body():
        with patch("app.bot.runtime.get_bot", return_value=bot):
            with patch(
                "app.repositories.app_settings.get_archive_channel_id",
                new=AsyncMock(return_value=-1001234567890),
            ):
                await notifications.notify_admin("Внимание!")

    import asyncio

    asyncio.run(body())
    # Должно быть два вызова: один на admin, один на archive channel
    assert send_msg.await_count == 2
    # Первый — admin chat_id
    first_call = send_msg.await_args_list[0]
    first_args = first_call.args
    assert first_args[0] == 888
    # Второй — archive channel
    second_call = send_msg.await_args_list[1]
    second_args = second_call.args
    assert second_args[0] == -1001234567890
    assert "🔔" in second_args[1]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
