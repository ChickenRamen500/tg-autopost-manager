# -*- coding: utf-8 -*-
"""Юнит-тесты настроек и подключения к Local Bot API (Фаза 1)."""
import os
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "")
os.environ["TELEGRAM_API_ID"] = "123"
os.environ["TELEGRAM_API_HASH"] = "abc"
os.environ["BOT_API_URL"] = "http://localhost:8081"

from app.config.settings import Settings  # noqa: E402
from app.utils import local_bot_api  # noqa: E402


def test_validate_reports_missing_token():
    s = Settings(bot_token="", telegram_api_id=1, telegram_api_hash="h", secret_token="x")
    problems = s.validate()
    assert any("BOT_TOKEN" in p for p in problems)


def test_validate_ok_when_filled():
    s = Settings(bot_token="123:ABC", telegram_api_id=1, telegram_api_hash="h", secret_token="x")
    assert s.validate() == []


def test_limits_constants_match_tz():
    # ТЗ §3.3: облако 50/20 МБ, локальный сервер 2000 МБ upload
    assert local_bot_api.UPLOAD_LIMIT_CLOUD_MB == 50
    assert local_bot_api.DOWNLOAD_LIMIT_CLOUD_MB == 20
    assert local_bot_api.UPLOAD_LIMIT_LOCAL_MB == 2000


def test_make_test_file_size(tmp_path: Path):
    p = local_bot_api.make_test_file(2, tmp_path)
    assert p.exists() and p.stat().st_size == 2 * 1024 * 1024
    p.unlink()
