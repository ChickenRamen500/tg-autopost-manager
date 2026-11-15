"""Уведомления об автодеплое и ошибках из лога контейнера (вне ТЗ v5 — доработка CI/CD).

Две функции:

1. ``notify_deploy_restart()`` — вызывается из lifespan при старте приложения.
   Если файл-триггер ``data/deploy.trigger`` был создан недавно (не старше
   ``DEPLOY_RESTART_WINDOW_SECONDS``, по умолчанию 15 мин) — значит контейнер
   был пересобран watcher'ом после webhook'а /webhook/deploy, и админу шлётся
   «🚀 Бот перезапущен после автодеплоя» (в ЛС + служебный канал, через
   notifications.notify_admin). Сообщение содержит short SHA коммита из
   trigger-файла.

   Безопасность: если watcher НЕ успел забрать trigger (например, git pull
   откатился к СТАРОМУ коду, где есть webhook, но нет этой функции) — файл
   может пережить перезапуск. Чтобы не спамить повторными уведомлениями,
   после отправки commit SHA записывается в ``data/deploy.notified``;
   тот же commit повторно не уведомляется.

2. ``start_log_error_watcher()`` — фоновая задача: читает собственный stdout-лог
   процесса (``/proc/1/log`` — FIFO, в который Docker/entrypoint направляет
   stdout контейнера; на хост-запуске без Docker отсутствует → задача молча
   завершается). Сканер с offset'ом выбирает новые строки и ищет ERROR/CRITICAL
   (уровни логирования из формата main.py и записи uvicorn). Найденные ошибки
   отправляются админу одним сообщением (не чаще раза в интервал сканирования,
   максимум ``LOG_ERRORS_MAX_LINES_PER_REPORT`` строк).

Все сбои отправки только логируются — модуль никогда не роняет приложение.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.config.settings import settings
from app.services.notifications import notify_admin

log = logging.getLogger("deploy_notify")

# Префикс сообщения о рестарте — один на все уведомления (детект в тестах).
RESTART_MSG_PREFIX = "🚀 Бот перезапущен после автодеплоя"


def _trigger_path() -> Path:
    return settings.data_dir / "deploy.trigger"


def _notified_path() -> Path:
    return settings.data_dir / "deploy.notified"


def _parse_trigger(content: str) -> tuple[str, str]:
    """Возвращает (commit, ref) из содержимого deploy.trigger.

    Формат файла (пишет webhook_deploy в app/main.py): первая строка — commit
    (payload.after), вторая — ref. Пустое/битое содержимое → ("unknown", ...).
    """
    lines = [ln.strip() for ln in content.splitlines()]
    commit = lines[0] if lines and lines[0] else "unknown"
    ref = lines[1] if len(lines) > 1 and lines[1] else "unknown"
    return commit, ref


def _already_notified(commit: str) -> bool:
    try:
        p = _notified_path()
        return p.exists() and commit in p.read_text(encoding="utf-8").split()
    except OSError:
        return False


def _mark_notified(commit: str) -> None:
    try:
        p = _notified_path()
        # Храним последние ~20 SHA — файл не растёт бесконечно.
        history = []
        if p.exists():
            history = p.read_text(encoding="utf-8").split()[-19:]
        history.append(commit)
        p.write_text("\n".join(history) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("deploy_notify: не удалось записать %s: %s", _notified_path(), e)


async def notify_deploy_restart() -> bool:
    """Уведомляет админа о перезапуске после автодеплоя, если это он и есть.

    Возвращает True, если уведомление отправлено. Вызывается из lifespan
    ПОСЛЕ готовности бота к отправке (polling запущен). Ошибки не всплывают.
    """
    try:
        tp = _trigger_path()
        if not tp.exists():
            return False
        age = time.time() - tp.stat().st_mtime
        window = settings.deploy_restart_window_seconds
        if age > window:
            # Trigger старый (watcher упал сразу после создания файла, либо
            # деплой не привёл к рестарту) — не уведомляем, чтобы не врать.
            log.debug("deploy_notify: trigger устарел (%.0f сек > %d) — пропускаем", age, window)
            return False
        commit, ref = _parse_trigger(tp.read_text(encoding="utf-8"))
        if _already_notified(commit):
            log.info("deploy_notify: commit %s уже уведомлён — пропускаем", commit[:12])
            return False
        text = (
            f"<b>{RESTART_MSG_PREFIX}</b>\n"
            f"Коммит: <code>{commit[:40]}</code>\n"
            f"Ref: <code>{ref[:80]}</code>\n"
            f"Перезапуск: {age:.0f} сек назад"
        )
        await notify_admin(text)
        _mark_notified(commit)
        log.info("deploy_notify: отправлено уведомление о рестарте (commit=%s)", commit[:12])
        return True
    except Exception as e:  # noqa: BLE001 — уведомления не должны ронять старт
        log.warning("deploy_notify: ошибка уведомления о рестарте: %r", e)
        return False


# ---------------------------------------------------------------------------
# Наблюдатель за ERROR/CRITICAL в собственном stdout-логе контейнера
# ---------------------------------------------------------------------------

# Кандидаты: /proc/1/log — FIFO stdout контейнера внутри Docker (читается с
# любого uid); /dev/stdout — работает при запуске на хосте (перенаправлен в
# файл), но в Docker указывает на тот же поток, что и /proc/1/log.
_LOG_CANDIDATES = ("/proc/1/log", "/dev/stdout")


def _is_error_line(line: str) -> bool:
    """True для строк нашего лог-формата с уровнем ERROR/CRITICAL и traceback'ов.

    Формат (main.py): ``2026-11-15 10:34:00 [ERROR] name: message``.
    Также ловим ``Traceback (most recent call last):`` — заголовки трейсбэков,
    которые пишутся через log.exception (следующая за ними строка уровня ERROR
    уже будет поймана, но заголовок даёт контекст).
    """
    if "[ERROR]" in line or "[CRITICAL]" in line:
        return True
    return line.startswith("Traceback (most recent call last)")


class LogErrorWatcher:
    """Инкрементальный сканер текстового потока на ERROR-строки.

    Читает файл побайтово до маркера новой строки: для FIFO (/proc/1/log)
    нельзя использовать readline()/итератор целиком — буферизация съедает
    данные или блокирует. Offset хранится в байтах; при ротации логов Docker
    (max-size) файл обрезается/пересоздаётся — если размер стал меньше offset,
    читаем заново с нуля.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.offset = 0

    async def scan(self) -> list[str]:
        """Возвращает новые ERROR-строки с прошлого вызова (или [])."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._scan_sync)

    def _scan_sync(self) -> list[str]:
        import os

        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        # Ротация лога: файл стал меньше нашего offset — перечитываем с начала.
        if size < self.offset:
            self.offset = 0

        fd: int | None = None
        errors: list[str] = []
        buf = b""
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            os.lseek(fd, self.offset, os.SEEK_SET)
            new_offset = self.offset
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    new_offset += len(raw) + 1
                    line = raw.decode("utf-8", errors="replace")
                    if _is_error_line(line):
                        errors.append(line)
            # Неполная последняя строка остаётся в buf — offset не продвигаем,
            # дочитаем в следующий скан (риск дубля минимален: строка появится
            # целиком раньше, чем мы снова её увидим с той же позиции).
            self.offset = new_offset
        except OSError:
            pass
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return errors


_watcher_task: asyncio.Task | None = None


async def _log_error_watch_loop() -> None:
    watcher: LogErrorWatcher | None = None
    for cand in _LOG_CANDIDATES:
        try:
            Path(cand).stat()
            watcher = LogErrorWatcher(cand)
            break
        except OSError:
            continue
    if watcher is None:
        log.info("log_error_watcher: лог-файл недоступен (%s) — наблюдатель отключён", _LOG_CANDIDATES)
        return
    log.info("log_error_watcher: следит за %s (интервал %d сек)", watcher.path, settings.log_errors_scan_interval_seconds)
    interval = max(10, settings.log_errors_scan_interval_seconds)
    while True:
        try:
            errors = await watcher.scan()
            if errors:
                limit = settings.log_errors_max_lines_per_report
                shown = errors[:limit]
                extra = len(errors) - len(shown)
                body = "\n".join(e[:300] for e in shown)
                if extra > 0:
                    body += f"\n…и ещё {extra} строк (см. docker logs tg-bot)"
                await notify_admin(
                    f"⚠️ <b>Ошибки в логе контейнера</b> (последние {interval} сек+)\n"
                    f"<code>{body[:3500]}</code>"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — цикл не должен умирать
            log.warning("log_error_watcher: ошибка скана: %r", e)
        await asyncio.sleep(interval)


def start_log_error_watcher() -> asyncio.Task | None:
    """Создаёт фоновую задачу наблюдения за логом (если включена настройкой)."""
    global _watcher_task
    if settings.log_errors_scan_interval_seconds <= 0:
        log.info("log_error_watcher: отключён (LOG_ERRORS_SCAN_INTERVAL_SECONDS=0)")
        return None
    _watcher_task = asyncio.create_task(_log_error_watch_loop(), name="log_error_watcher")
    return _watcher_task


async def stop_log_error_watcher() -> None:
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
    _watcher_task = None
