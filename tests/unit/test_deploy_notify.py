"""Тесты app/services/deploy_notify.py — уведомления об автодеплое и логе ошибок.

Покрывает:
  1. notify_deploy_restart: свежий trigger → уведомление; старый → нет;
     повторный тот же commit → нет (dedup через deploy.notified);
     нет trigger → нет; битый trigger → не падает.
  2. _parse_trigger: форматы содержимого deploy.trigger.
  3. _is_error_line: ERROR/CRITICAL/Traceback vs INFO/WARNING.
  4. LogErrorWatcher._scan_sync: инкрементальный offset, дедупликация прочитанного,
     обработка «ротации» (файл урезался) и неполной последней строки.

Settings — frozen dataclass, переопределяем поля через object.__setattr__
(см. conftest.py).
"""
from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

from app.config.settings import settings
from app.services import deploy_notify as dn


def _set(field_name: str, value) -> None:
    object.__setattr__(settings, field_name, value)


# ---------------------------------------------------------------------------
# _parse_trigger
# ---------------------------------------------------------------------------

def test_parse_trigger_standard_format():
    commit, ref = dn._parse_trigger("abc123def\nrefs/heads/main\n")
    assert commit == "abc123def"
    assert ref == "refs/heads/main"


def test_parse_trigger_empty_and_broken():
    assert dn._parse_trigger("") == ("unknown", "unknown")
    assert dn._parse_trigger("only-commit\n") == ("only-commit", "unknown")


# ---------------------------------------------------------------------------
# _is_error_line
# ---------------------------------------------------------------------------

def test_is_error_line_matches_levels():
    assert dn._is_error_line("2026-11-15 10:00:00 [ERROR] worker: boom")
    assert dn._is_error_line("2026-11-15 10:00:00 [CRITICAL] main: dead")
    assert dn._is_error_line("Traceback (most recent call last):")


def test_is_error_line_ignores_info_warning():
    assert not dn._is_error_line("2026-11-15 10:00:00 [INFO] main: ok")
    assert not dn._is_error_line("2026-11-15 10:00:00 [WARNING] config: meh")
    # слово ERROR внутри сообщения INFO-уровня не триггерит наш формат?
    # Формат содержит "[INFO]" — "[ERROR]" в подстроке отсутствует → False.
    assert not dn._is_error_line("2026-11-15 10:00:00 [INFO] x: no error here")


# ---------------------------------------------------------------------------
# notify_deploy_restart
# ---------------------------------------------------------------------------

def test_restart_notify_fresh_trigger(tmp_path):
    old_data_dir = settings.data_dir
    _set("data_dir", tmp_path)
    try:
        (tmp_path / "deploy.trigger").write_text("c0ffee123\nrefs/heads/main\n", encoding="utf-8")
        with patch.object(dn, "notify_admin", new_callable=AsyncMock) as m:
            sent = _run(dn.notify_deploy_restart())
        assert sent is True
        assert m.await_count == 1
        text = m.await_args.args[0]
        assert dn.RESTART_MSG_PREFIX in text
        assert "c0ffee123" in text
    finally:
        _set("data_dir", old_data_dir)


def test_restart_notify_stale_trigger_skipped(tmp_path):
    old_data_dir = settings.data_dir
    _set("data_dir", tmp_path)
    try:
        tp = tmp_path / "deploy.trigger"
        tp.write_text("deadbeef\nrefs/heads/main\n", encoding="utf-8")
        stale = time.time() - settings.deploy_restart_window_seconds - 10
        os.utime(tp, (stale, stale))
        with patch.object(dn, "notify_admin", new_callable=AsyncMock) as m:
            sent = _run(dn.notify_deploy_restart())
        assert sent is False
        assert m.await_count == 0
    finally:
        _set("data_dir", old_data_dir)


def test_restart_notify_dedup_same_commit(tmp_path):
    old_data_dir = settings.data_dir
    _set("data_dir", tmp_path)
    try:
        (tmp_path / "deploy.trigger").write_text("aaaa1111\nrefs/heads/main\n", encoding="utf-8")
        with patch.object(dn, "notify_admin", new_callable=AsyncMock) as m:
            first = _run(dn.notify_deploy_restart())
            second = _run(dn.notify_deploy_restart())
        assert first is True
        assert second is False
        assert m.await_count == 1
    finally:
        _set("data_dir", old_data_dir)


def test_restart_notify_no_trigger(tmp_path):
    old_data_dir = settings.data_dir
    _set("data_dir", tmp_path)
    try:
        with patch.object(dn, "notify_admin", new_callable=AsyncMock) as m:
            assert _run(dn.notify_deploy_restart()) is False
        assert m.await_count == 0
    finally:
        _set("data_dir", old_data_dir)


def test_restart_notify_survives_send_failure(tmp_path):
    """Уведомление не должно ронять старт, если отправка упала."""
    old_data_dir = settings.data_dir
    _set("data_dir", tmp_path)
    try:
        (tmp_path / "deploy.trigger").write_text("bbbb2222\nrefs/heads/main\n", encoding="utf-8")
        with patch.object(dn, "notify_admin", new_callable=AsyncMock) as m:
            m.side_effect = RuntimeError("bot down")
            assert _run(dn.notify_deploy_restart()) is False  # exception подавлен
    finally:
        _set("data_dir", old_data_dir)


# ---------------------------------------------------------------------------
# LogErrorWatcher._scan_sync
# ---------------------------------------------------------------------------

def test_watcher_incremental_scan(tmp_path):
    log_file = tmp_path / "container.log"
    log_file.write_text(
        "[10:00:00] [INFO] main: start\n"
        "[10:00:01] [ERROR] worker: download failed\n",
        encoding="utf-8",
    )
    w = dn.LogErrorWatcher(str(log_file))
    errors = w._scan_sync()
    assert len(errors) == 1 and "download failed" in errors[0]
    # Повторный скан без новых данных → пусто (offset продвинут)
    assert w._scan_sync() == []
    # Добавили новую ERROR-строку → увидели только её
    with log_file.open("a", encoding="utf-8") as f:
        f.write("[10:00:02] [ERROR] scheduler: send 429\n")
    errors2 = w._scan_sync()
    assert len(errors2) == 1 and "send 429" in errors2[0]


def test_watcher_handles_truncation_rotation(tmp_path):
    log_file = tmp_path / "container.log"
    log_file.write_text("x" * 500 + "\n[E] [ERROR] a: one\n", encoding="utf-8")
    w = dn.LogErrorWatcher(str(log_file))
    assert len(w._scan_sync()) == 1
    # «Ротация»: файл перезаписан целиком, стал меньше offset
    log_file.write_text("[E] [ERROR] b: two\n", encoding="utf-8")
    errors = w._scan_sync()
    assert len(errors) == 1 and "two" in errors[0]


def test_watcher_partial_last_line_not_double_counted(tmp_path):
    log_file = tmp_path / "container.log"
    # Неполная строка (без \n) — не должна попасть в результат и не должна
    # быть продублирована после дописывания.
    log_file.write_text("[E] [ERROR] part", encoding="utf-8")
    w = dn.LogErrorWatcher(str(log_file))
    assert w._scan_sync() == []
    with log_file.open("a", encoding="utf-8") as f:
        f.write("ial line\n")
    errors = w._scan_sync()
    assert len(errors) == 1 and "partial line" in errors[0]
    assert w._scan_sync() == []


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _run(coro):
    import asyncio

    return asyncio.run(coro)
