"""Интеграционные тесты FastAPI app + endpoints (PR-40 H-T2).

PR-40 (H-T2): app/main.py lifespan + REST endpoints раньше не имели тестов — coverage = 0%.
Покрываем:
  1. /health без SECRET_TOKEN — отдаёт status='ok' без утечки чувствительных данных.
  2. /health/scheduler без X-Secret-Token (когда SECRET_TOKEN задан) → 403.
  3. /health/scheduler с неверным токеном → 403.
  4. /health/scheduler с верным токеном → 200 + список jobs (возможно пустой).
  5. /health с заданным SECRET_TOKEN без токена → 403 (auth работает).
  6. /health с верным токеном → 200 и config_problems список.

Технические решения:
- ASGITransport(httpx) НЕ запускает lifespan — поэтому scheduler/bot не стартуют,
  что подходит для тестирования голых endpoints без heavyweight инициализации.
- get_scheduler() возвращает lazily-created singleton (если None — создаст новый,
  не запущенный, .get_jobs() = []). shutdown не нужен.
- SECRET_TOKEN выставляем через object.__setattr__ (frozen dataclass) —
  с восстановлением в finally (как в conftest.run_with_db).

Примечание: existing verify_token возвращает 403 (не 401) при неверном/отсутствующем
токене — это задокументированная LOW-проблема (worklog PR-40). Тесты проверяют 403
как фактическое поведение; отдельный PR может поменять на 401.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest


@contextmanager
def _temp_settings(**overrides):
    """Временно переопределяет поля Settings (frozen dataclass) с восстановлением."""
    from app.config.settings import settings

    originals = {k: getattr(settings, k) for k in overrides}
    try:
        for k, v in overrides.items():
            object.__setattr__(settings, k, v)
        yield
    finally:
        for k, v in originals.items():
            object.__setattr__(settings, k, v)


async def _client_get(path: str, headers: dict | None = None):
    """Делает GET-запрос к FastAPI app через ASGITransport (без запуска lifespan).

    Возвращает (status_code, json_body).
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(path, headers=headers or {})
    return resp.status_code, resp.json() if resp.headers.get("content-type", "").startswith("application/json") else None


def _reset_scheduler_singleton():
    """Сбрасываем singleton scheduler, чтобы тесты не влияли друг на друга."""
    from app.scheduler import reset_scheduler

    reset_scheduler()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_without_secret_token_returns_ok_and_config_problems():
    """/health без SECRET_TOKEN — отдаёт status='ok' + список проблем конфигурации.

    Без SECRET_TOKEN verify_token пропускает запрос (ранний return).
    """
    with _temp_settings(secret_token=""):
        status, body = asyncio.run(_client_get("/health"))

    assert status == 200, f"Ожидали 200, получили {status}: {body}"
    assert body["status"] == "ok"
    assert body["phase"] == 2
    # config_problems — список (может быть пустым или содержать BOT_TOKEN, SECRET_TOKEN, ADMIN_TG_ID)
    assert "config_problems" in body
    assert isinstance(body["config_problems"], list)
    # /health отдаёт bot_api_url — это OK (он не секретный, это URL local bot api)
    assert "bot_api_url" in body


def test_health_with_secret_token_and_correct_header():
    """/health с заданным SECRET_TOKEN + корректный X-Secret-Token → 200."""
    with _temp_settings(secret_token="my_secret_xyz"):
        status, body = asyncio.run(_client_get("/health", headers={"X-Secret-Token": "my_secret_xyz"}))

    assert status == 200, f"Ожидали 200 с корректным токеном, получили {status}"
    assert body["status"] == "ok"


def test_health_with_secret_token_and_missing_header_returns_403():
    """/health с заданным SECRET_TOKEN без X-Secret-Token → 403 (текущее поведение)."""
    with _temp_settings(secret_token="my_secret_xyz"):
        status, body = asyncio.run(_client_get("/health"))

    assert status == 403, f"Ожидали 403 без токена, получили {status}"
    assert "Forbidden" in body["detail"]


def test_health_with_secret_token_and_wrong_header_returns_403():
    """/health с заданным SECRET_TOKEN + неверный токен → 403."""
    with _temp_settings(secret_token="my_secret_xyz"):
        status, body = asyncio.run(_client_get("/health", headers={"X-Secret-Token": "wrong_token"}))

    assert status == 403
    assert "Forbidden" in body["detail"]


# ---------------------------------------------------------------------------
# /health/scheduler
# ---------------------------------------------------------------------------


def test_health_scheduler_without_token_returns_403():
    """/health/scheduler без X-Secret-Token (когда SECRET_TOKEN задан) → 403."""
    _reset_scheduler_singleton()
    with _temp_settings(secret_token="sched_secret"):
        status, body = asyncio.run(_client_get("/health/scheduler"))

    assert status == 403, f"Ожидали 403 без токена, получили {status}"
    assert "Forbidden" in body["detail"]


def test_health_scheduler_with_wrong_token_returns_403():
    """/health/scheduler с неверным токеном → 403."""
    _reset_scheduler_singleton()
    with _temp_settings(secret_token="sched_secret"):
        status, body = asyncio.run(_client_get("/health/scheduler", headers={"X-Secret-Token": "wrong"}))

    assert status == 403
    assert "Forbidden" in body["detail"]


def test_health_scheduler_with_correct_token_returns_200():
    """/health/scheduler с корректным токеном → 200 + список jobs (пустой — scheduler не запущен)."""
    _reset_scheduler_singleton()
    with _temp_settings(secret_token="sched_secret"):
        status, body = asyncio.run(
            _client_get("/health/scheduler", headers={"X-Secret-Token": "sched_secret"})
        )

    assert status == 200, f"Ожидали 200 с корректным токеном, получили {status}: {body}"
    assert body["status"] in ("ok", "stopped"), (
        f"Неожиданный status: {body['status']!r}. scheduler не запускался через lifespan, "
        "поэтому .running=False → статус должен быть 'stopped'."
    )
    # jobs — список (возможно пустой — scheduler пустой, т.к. setup_scheduler_async не вызывался)
    assert "jobs" in body
    assert isinstance(body["jobs"], list)


def test_health_scheduler_without_secret_token_open_access():
    """/health/scheduler без SECRET_TOKEN в config — отдаёт список jobs без auth (открытый доступ).

    Документирует HIGH-проблему из worklog: «/health и /health/scheduler открыты,
    если SECRET_TOKEN не задан — утекает список очередей с next_run_time».
    """
    _reset_scheduler_singleton()
    with _temp_settings(secret_token=""):
        status, body = asyncio.run(_client_get("/health/scheduler"))

    # Без secret_token — endpoint открыт! (verify_token возвращает None)
    assert status == 200, f"Без SECRET_TOKEN endpoint должен быть открыт, получили {status}"
    assert "jobs" in body


# ---------------------------------------------------------------------------
# /health/bot-api — без реальной сети
# ---------------------------------------------------------------------------


def test_health_bot_api_returns_error_when_local_api_unreachable():
    """/health/bot-api без запущенного local-bot-api — status=error, но HTTP 200.

    get_me() возвращает None (network error мокнут через SECRET_TOKEN empty).
    Здесь тестируем что endpoint не падает 500 — даже без сети.
    """
    # Мокаем app.utils.local_bot_api.get_me — без реальной сети
    from unittest.mock import AsyncMock

    from unittest.mock import patch

    with _temp_settings(secret_token=""):
        with patch("app.main.get_me", new=AsyncMock(return_value=None)):
            status, body = asyncio.run(_client_get("/health/bot-api"))

    assert status == 200, f"Ожидали 200, получили {status}"
    assert body["status"] == "error"
    assert "Local Bot API" in body["detail"] or "Local Bot API" in str(body)


def test_health_bot_api_returns_ok_with_mocked_me():
    """/health/bot-api с get_me → {'username': '...', 'id': ...} — status=ok + bot info."""
    from unittest.mock import AsyncMock, patch

    fake_me = {"username": "test_bot", "id": 12345, "first_name": "Test"}
    with _temp_settings(secret_token=""):
        with patch("app.main.get_me", new=AsyncMock(return_value=fake_me)):
            status, body = asyncio.run(_client_get("/health/bot-api"))

    assert status == 200
    assert body["status"] == "ok"
    assert body["bot"]["username"] == "test_bot"
    assert body["bot"]["id"] == 12345
    # Важно: НЕ утекают лишние поля (только username, id)
    assert "is_bot" not in body["bot"]
    assert "first_name" not in body["bot"]


# ---------------------------------------------------------------------------
# Lifespan — минимальный smoke-test
# ---------------------------------------------------------------------------


def test_app_object_has_lifespan_and_routes():
    """FastAPI app существует, имеет lifespan и 3 health-маршрута (+verify_token)."""
    from app.main import app

    assert app.router is not None
    # Проверяем что lifespan назначен
    assert app.router.lifespan_context is not None
    # Проверяем что все 3 health-маршрута зарегистрированы
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/health" in paths
    assert "/health/bot-api" in paths
    assert "/health/scheduler" in paths


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
