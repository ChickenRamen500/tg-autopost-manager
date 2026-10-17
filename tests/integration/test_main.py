"""Интеграционные тесты FastAPI app + endpoints (H-T2).

Покрывает:
  1. /health БЕЗ auth (всегда 200, только {"status":"ok"}) — Docker HEALTHCHECK endpoint.
  2. /health/scheduler без X-Secret-Token (когда SECRET_TOKEN задан) → 401 Unauthorized.
  3. /health/scheduler с неверным токеном → 401.
  4. /health/scheduler с верным токеном → 200 + список jobs (возможно пустой).
  5. /health всегда 200 (auth не требуется) — даже с заданным SECRET_TOKEN.
  6. /health НЕ раскрывает bot_api_url/config_problems (no config leakage).

Технические решения:
- ASGITransport(httpx) НЕ запускает lifespan — поэтому scheduler/bot не стартуют,
  что подходит для тестирования голых endpoints без heavyweight инициализации.
- get_scheduler() возвращает lazily-created singleton (если None — создаст новый,
  не запущенный, .get_jobs() = []). shutdown не нужен.
- SECRET_TOKEN выставываем через object.__setattr__ (frozen dataclass) —
  с восстановлением в finally (как в conftest.run_with_db).

Соответствует актуальному поведению после PR #37 (security hardening):
- /health — без auth (для load balancer / Docker HEALTHCHECK), без config leakage
- verify_token: 401 Unauthorized (семантически правильно для auth), timing-safe compare
- secrets.compare_digest для SECRET_TOKEN
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


def test_health_without_secret_token_returns_ok_only():
    """/health без SECRET_TOKEN — отдаёт ТОЛЬКО {"status": "ok"} без config leakage.

    После PR #37: /health — это liveness probe БЕЗ auth, НЕ раскрывает конфигурацию
    (no bot_api_url, no config_problems). Это для Docker HEALTHCHECK и load balancer.
    """
    with _temp_settings(secret_token=""):
        status, body = asyncio.run(_client_get("/health"))

    assert status == 200, f"Ожидали 200, получили {status}: {body}"
    assert body == {"status": "ok"}, (
        f"/health должен возвращать только {{'status':'ok'}}, получил {body} — "
        "нельзя утекать bot_api_url/config_problems через unauthenticated endpoint"
    )


def test_health_with_secret_token_and_correct_header():
    """/health с заданным SECRET_TOKEN + корректный X-Secret-Token → 200."""
    with _temp_settings(secret_token="my_secret_xyz"):
        status, body = asyncio.run(_client_get("/health", headers={"X-Secret-Token": "my_secret_xyz"}))

    assert status == 200, f"Ожидали 200 с корректным токеном, получили {status}"
    assert body["status"] == "ok"


def test_health_with_secret_token_and_missing_header_returns_200():
    """/health ВСЕГДА 200 — endpoint не требует auth (liveness probe).

    После PR #37: /health отделён от защищённых endpoints и не использует verify_token.
    SECRET_TOKEN защищает только /health/bot-api и /health/scheduler.
    """
    with _temp_settings(secret_token="my_secret_xyz"):
        status, body = asyncio.run(_client_get("/health"))

    assert status == 200, f"/health должен быть открыт всегда (liveness probe), получили {status}"
    assert body == {"status": "ok"}


def test_health_with_secret_token_and_wrong_header_returns_200():
    """/health с неверным X-Secret-Token — всё равно 200 (liveness probe без auth)."""
    with _temp_settings(secret_token="my_secret_xyz"):
        status, body = asyncio.run(_client_get("/health", headers={"X-Secret-Token": "wrong_token"}))

    assert status == 200, f"/health не должен требовать auth, получили {status}"
    assert body == {"status": "ok"}


# ---------------------------------------------------------------------------
# /health/scheduler
# ---------------------------------------------------------------------------


def test_health_scheduler_without_token_returns_401():
    """/health/scheduler без X-Secret-Token (когда SECRET_TOKEN задан) → 401 Unauthorized.

    После PR #37: verify_token возвращает 401 (семантически правильно для auth-credentials,
    403 подразумевает аутентифицированного пользователя без прав, здесь же именно
    credential отсутствует/невалиден). Используется secrets.compare_digest (timing-safe).
    """
    _reset_scheduler_singleton()
    with _temp_settings(secret_token="sched_secret"):
        status, body = asyncio.run(_client_get("/health/scheduler"))

    assert status == 401, f"Ожидали 401 без токена (auth required), получили {status}"
    assert "Unauthorized" in body["detail"] or "Forbidden" in body["detail"]


def test_health_scheduler_with_wrong_token_returns_401():
    """/health/scheduler с неверным токеном → 401 (timing-safe compare)."""
    _reset_scheduler_singleton()
    with _temp_settings(secret_token="sched_secret"):
        status, body = asyncio.run(_client_get("/health/scheduler", headers={"X-Secret-Token": "wrong"}))

    assert status == 401, f"Ожидали 401 с неверным токеном, получили {status}"
    assert "Unauthorized" in body["detail"] or "Forbidden" in body["detail"]


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
    from unittest.mock import AsyncMock, patch

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
