"""Тесты AccessMiddleware (RBAC, bootstrap admin, обновление username, guest block).

PR-40 (H-T1): AccessMiddleware (app/bot/middleware/access.py) раньше не имел
ни одного теста — coverage = 0%. Эти тесты покрывают:
  1. Гость с /start — проходит (должен попробовать bootstrap_admin и пустить к handler).
  2. Гость с /menu — блокируется сообщением «Доступ закрыт».
  3. Неактивный admin — блокируется.
  4. Активный moderator — проходит для /menu.
  5. Активный moderator — пробрасывается к handler с user_role='moderator'.
  6. Event без from_user (channel_post) — пробрасывается без проверки.
  7. /start@botname — блокируется (известная MEDIUM-проблема).
  8. Смена username существующего пользователя — обновляется в БД.

Используем run_with_db helper из conftest.py — каждая корутина крутится
в своём event loop с временной БД.

Техническое примечание: middleware делает isinstance(event, Message/CallbackQuery).
MagicMock(spec=Message) проходит эту проверку, потому что mock подменяет __class__
на spec-класс. Поэтому все фейковые события строятся через MagicMock(spec=...).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message


class FakeUser:
    """Эмуляция aiogram User — только поля, используемые middleware."""

    def __init__(self, tg_id: int, username: str | None = None) -> None:
        self.id = tg_id
        self.username = username


def make_message(text: str = "", user: FakeUser | None = None) -> Any:
    """MagicMock(spec=Message) — проходит isinstance(event, Message).

    Метод answer — AsyncMock, чтобы можно было делать await event.answer(...).
    """
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = user
    msg.answer = AsyncMock()
    # getattr(event, "from_user", None) — должно вернуть user (или None)
    return msg


def make_callback_query(user: FakeUser | None = None) -> Any:
    """MagicMock(spec=CallbackQuery) — проходит isinstance(event, CallbackQuery)."""
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = user
    cb.answer = AsyncMock()
    # у CallbackQuery есть .message (Message)
    cb.message = make_message(text="", user=user)
    return cb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _invoke_mw(event: Any) -> dict[str, Any] | None:
    """Запускает AccessMiddleware на event и возвращает data, переданную handler'у.

    handler — это mock, который просто сохраняет полученные (event, data) и возвращает их.
    Если middleware вернула None (блокировка) — возвращает None.
    """
    from app.bot.middleware.access import AccessMiddleware

    captured: dict[str, Any] = {}

    async def fake_handler(ev, data):
        captured["data"] = data
        captured["event"] = ev
        return "ok"

    mw = AccessMiddleware()
    result = await mw(fake_handler, event, {"bot": MagicMock()})
    if result is None:
        return None
    return captured


def _set_admin_tg_id(tg_id: int) -> None:
    """Временно выставляет settings.admin_tg_id — frozen dataclass, через object.__setattr__."""
    from app.config.settings import settings

    object.__setattr__(settings, "admin_tg_id", tg_id)


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


def test_guest_start_is_allowed(tmp_path):
    """Гость с /start — проходит (bootstrap_admin не падает, handler вызывается)."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        _set_admin_tg_id(999_999_999)  # другой tg_id — bootstrap не сработает
        guest = FakeUser(tg_id=111, username="guest_user")
        msg = make_message(text="/start", user=guest)

        # bootstrap_admin вызывается для admin_tg_id — у нас guest, но всё равно вызывается
        # (внутри middleware есть проверка `tg_id == settings.admin_tg_id`). Мокаем на всякий.
        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(msg)

        assert captured is not None, "Гость с /start должен пройти к handler"
        # user_role должен быть None (гость)
        assert captured["data"]["user_role"] is None
        # answer не должен вызываться (нет сообщения «доступ закрыт»)
        msg.answer.assert_not_called()

    run_with_db(tmp_path, body)


def test_guest_menu_is_blocked(tmp_path):
    """Гость с /menu — блокируется, выводится «Доступ закрыт»."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        _set_admin_tg_id(999_999_999)
        guest = FakeUser(tg_id=222, username=None)
        msg = make_message(text="/menu", user=guest)

        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(msg)

        assert captured is None, "Гость с /menu должен быть заблокирован"
        msg.answer.assert_awaited_once()
        # Проверяем текст сообщения — должен содержать «Доступ закрыт»
        call_args = msg.answer.await_args
        assert call_args is not None
        assert "Доступ закрыт" in call_args.args[0]

    run_with_db(tmp_path, body)


def test_inactive_admin_is_blocked(tmp_path):
    """Пользователь в БД с is_active=0 — блокируется (даже если role=admin)."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        from app.repositories import users as users_repo

        _set_admin_tg_id(999_999_999)
        inactive_tg = 333
        await users_repo.add_user(inactive_tg, "inactive_dude", role="admin")
        await users_repo.set_active(inactive_tg, False)
        # verify
        u = await users_repo.get_user_by_tg_id(inactive_tg)
        assert u and not u["is_active"]

        msg = make_message(text="/menu", user=FakeUser(tg_id=inactive_tg, username="inactive_dude"))
        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(msg)

        assert captured is None, "Неактивный пользователь должен быть заблокирован"
        msg.answer.assert_awaited_once()

    run_with_db(tmp_path, body)


def test_active_moderator_passes_with_role(tmp_path):
    """Активный moderator — проходит к handler с user_role='moderator'."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        from app.repositories import users as users_repo

        _set_admin_tg_id(999_999_999)
        mod_tg = 444
        await users_repo.add_user(mod_tg, "mod_user", role="moderator")

        msg = make_message(text="/menu", user=FakeUser(tg_id=mod_tg, username="mod_user"))
        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(msg)

        assert captured is not None, "Активный moderator должен пройти"
        assert captured["data"]["user_role"] == "moderator"
        assert captured["data"]["user"]["role"] == "moderator"

    run_with_db(tmp_path, body)


def test_callback_query_active_user_passes(tmp_path):
    """Callback от активного пользователя — пробрасывается к handler (не блокируется)."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        from app.repositories import users as users_repo

        _set_admin_tg_id(999_999_999)
        tg = 555
        await users_repo.add_user(tg, "callback_user", role="admin")

        cb = make_callback_query(user=FakeUser(tg_id=tg, username="callback_user"))
        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(cb)

        assert captured is not None, "Активный admin (callback) должен пройти"
        cb.answer.assert_not_called()

    run_with_db(tmp_path, body)


def test_callback_query_guest_blocked(tmp_path):
    """Callback от гостя — блокируется через answer(show_alert=True)."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        _set_admin_tg_id(999_999_999)
        cb = make_callback_query(user=FakeUser(tg_id=666, username=None))

        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(cb)

        assert captured is None, "Гость (callback) должен быть заблокирован"
        cb.answer.assert_awaited_once()
        # show_alert=True
        call_kwargs = cb.answer.await_args.kwargs
        assert call_kwargs.get("show_alert") is True

    run_with_db(tmp_path, body)


def test_event_without_from_user_passes_through(tmp_path):
    """Событие без from_user (channel_post, edited_message от бота) — пробрасывается без проверки."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        # Message с from_user=None
        msg = make_message(text="channel post body", user=None)
        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(msg)

        assert captured is not None, "Событие без from_user должно пройти без проверки"
        msg.answer.assert_not_called()

    run_with_db(tmp_path, body)


def test_start_at_botname_currently_blocks_guest(tmp_path):
    """/start@my_bot — текущий middleware блокирует гостя (известная MEDIUM-проблема).

    middleware проверяет `parts[0] in ("/start", "/help")`, где parts[0] после
    text.split() — это "/start@my_test_bot" целиком. Это НЕ равно "/start",
    поэтому гость блокируется. Тест документирует текущее поведение и фиксирует
    регрессию (если кто-то исправит middleware, тест надо будет обновить).
    """
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        _set_admin_tg_id(999_999_999)
        guest = FakeUser(tg_id=777, username="guest_start_at")
        msg = make_message(text="/start@my_test_bot", user=guest)

        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(msg)

        assert captured is None, (
            "Текущий middleware блокирует /start@botname (parts[0]='/start@my_test_bot' != '/start') "
            "— это известная MEDIUM-проблема, зафиксированная в worklog."
        )

    run_with_db(tmp_path, body)


def test_username_change_updates_db(tmp_path):
    """Смена @username существующим пользователем — обновляется в БД (upsert_user)."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import patch

        from app.repositories import users as users_repo

        _set_admin_tg_id(999_999_999)
        tg = 888
        await users_repo.add_user(tg, "old_name", role="moderator")

        # Идём в middleware с новым username
        msg = make_message(text="/menu", user=FakeUser(tg_id=tg, username="new_name"))
        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()):
            captured = await _invoke_mw(msg)

        assert captured is not None, "Активный moderator должен пройти"
        # После middleware должен сработать upsert_user с новым username
        u = await users_repo.get_user_by_tg_id(tg)
        assert u["username"] == "new_name", f"Username не обновился: {u['username']!r}"

    run_with_db(tmp_path, body)


def test_bootstrap_admin_called_for_admin_tg_id(tmp_path):
    """Если tg_id == settings.admin_tg_id — middleware вызывает bootstrap_admin."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import AsyncMock, patch

        _set_admin_tg_id(12345)
        guest_admin = FakeUser(tg_id=12345, username="the_admin")
        msg = make_message(text="/start", user=guest_admin)

        with patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()) as mock_bs:
            await _invoke_mw(msg)
            mock_bs.assert_awaited_once_with(12345, "the_admin")

    run_with_db(tmp_path, body)


def test_db_error_treats_as_guest(tmp_path):
    """Если get_user_by_tg_id падает — пользователь трактуется как гость (не роняет middleware)."""
    from conftest import run_with_db

    async def body():
        from unittest.mock import AsyncMock, patch

        _set_admin_tg_id(999_999_999)
        guest = FakeUser(tg_id=999, username="db_error_user")
        msg = make_message(text="/menu", user=guest)

        # Мокаем users_repo так, чтобы get_user_by_tg_id выбросил исключение
        with (
            patch("app.bot.middleware.access.bootstrap_admin", new=AsyncMock()),
            patch(
                "app.bot.middleware.access.users_repo.get_user_by_tg_id",
                new=AsyncMock(side_effect=RuntimeError("DB down")),
            ),
        ):
            captured = await _invoke_mw(msg)

        # Должен заблокировать как гостя (db_user=None → гостевой путь)
        assert captured is None, "При ошибке БД пользователь должен блокироваться как гость"
        msg.answer.assert_awaited_once()

    run_with_db(tmp_path, body)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
