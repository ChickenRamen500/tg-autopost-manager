"""Инфраструктура Local Bot API Server (ТЗ §3.3).

Содержит:
- create_bot() — подключение aiogram к локальному серверу через TelegramAPIServer.from_base
                  (ключевой момент: без --local-сервера действуют лимиты 50 МБ upload / 20 МБ getFile);
- logout_from_cloud() — разовая миграция бота с облачного API (curl logOut из ТЗ),
                       без неё локальный сервер вернёт 401/409;
- get_me() / healthcheck() — проверка связности с локальным сервером;
- make_test_file() / test_big_upload_download() — проверка из п.4 Фазы 1:
                       отправка файла ~300 МБ и getFile файла > 20 МБ.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BufferedInputFile

from app.config.settings import settings

log = logging.getLogger("local_bot_api")

UPLOAD_LIMIT_CLOUD_MB = 50      # облачный Bot API
DOWNLOAD_LIMIT_CLOUD_MB = 20    # getFile через облако
UPLOAD_LIMIT_LOCAL_MB = 2000    # local-bot-api с --local (ТЗ §3.3)


# Таймауты по умолчанию в aiogram — 60с; загрузка файла 200-300 МБ на хосте
# дольше 60с даёт TelegramNetworkError("Request timeout error"), хотя файл
# физически доходит до чата. Для big-test поднимаем таймауты.
BIG_UPLOAD_TIMEOUT_S = float(os.getenv("BOT_API_UPLOAD_TIMEOUT", "600"))


def create_bot(timeout_s: float | None = None) -> Bot:
    """Bot, подключённый к Local Bot API Server (код из ТЗ §3.3).

    timeout_s — таймаут HTTP-запросов (по умолчанию aiogram 60с; для отправки
    больших файлов передавайте больше, см. BIG_UPLOAD_TIMEOUT_S).
    """
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    kwargs = {}
    if timeout_s:
        kwargs = {"timeout": timeout_s}
    session = AiohttpSession(
        api=TelegramAPIServer.from_base(settings.bot_api_url), **kwargs
    )
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def logout_from_cloud() -> bool:
    """Разлогинивает бота с облачного Bot API (обязательно ОДИН РАЗ перед работой через локальный сервер).

    curl https://api.telegram.org/bot<TOKEN>/logOut  →  {"ok":true,"result":true}
    Повторный вызов безопасен (идемпотентен).
    """
    url = f"https://api.telegram.org/bot{settings.bot_token}/logOut"
    async with aiohttp.ClientSession() as s, s.post(url) as r:
        data = await r.json(content_type=None)
        ok = bool(data.get("ok")) if isinstance(data, dict) else False
        log.info("logOut облачного API: %s", data)
        return ok


async def get_me() -> dict | None:
    """GET /getMe на локальном сервере — базовый healthcheck.

    Защищена от не-JSON ответов (HTML error page, пустое тело, таймаут, разрыв соединения):
    aiohttp.ClientResponseError/ContentTypeError, json.JSONDecodeError, ValueError,
    AttributeError, TypeError — все логируются как «некорректный ответ local-bot-api».
    """
    url = f"{settings.bot_api_url}/bot{settings.bot_token}/getMe"
    try:
        # таймаут 15с: если local-bot-api ещё не готов (или токен отклонён),
        # запрос должен завершиться ошибкой, а не висеть бесконечно
        to = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=to) as s, s.get(url) as r:
            # content_type=None — не требовать application/json в ответе
            # (local-bot-api на ошибках отдаёт text/html или пустое тело)
            data = await r.json(content_type=None)
            if isinstance(data, dict) and data.get("ok"):
                return data.get("result")
            log.error("getMe вернул некорректный ответ: %r", data)
            return None
    except (TimeoutError, aiohttp.ClientError) as e:
        log.error("Local Bot API недоступен по %s: %s", settings.bot_api_url, e)
        return None
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
        # Тело пришло, но это не валидный JSON (HTML error page / пусто / битый JSON).
        log.error("getMe: некорректный ответ local-bot-api (не JSON): %r", e)
        return None


def make_test_file(size_mb: int, dest_dir: Path) -> Path:
    """Создаёт случайный файл заданного размера (не сжимается Telegram как документ).

    os.urandom быстрее secrets.token_bytes для крупных файлов; криптостойкость
    для тестового мусорного файла не нужна (он не используется как ключ/секрет).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"test_{size_mb}mb_{secrets.token_hex(4)}.bin"
    chunk = 1024 * 1024
    with open(path, "wb") as f:
        f.writelines(os.urandom(chunk) for _ in range(size_mb))
    return path


async def test_big_upload_download(chat_id: int, size_mb: int = 300) -> bool:
    """Проверка п.4 Фазы 1 ТЗ: загрузка документа ~300 МБ (>лимит облака 50 МБ)
    и скачивание его обратно через getFile (>лимит облака 20 МБ).

    Работает только если local-bot-api запущен с --local, иначе Telegram
    отклонит загрузку file_too_big.
    """
    if not chat_id:
        raise ValueError("chat_id не задан (TEST_CHAT_ID в .env)")

    tmp = make_test_file(size_mb, settings.downloads_dir)
    ok = False
    try:
        # Таймаут 60с по умолчанию не выдерживает upload 200-300 МБ на хосте —
        # aiogram отдаёт TelegramNetworkError("Request timeout error"), хотя
        # файл доходит до чата. Поднимаем до BIG_UPLOAD_TIMEOUT_S (default 600с).
        bot = create_bot(timeout_s=BIG_UPLOAD_TIMEOUT_S)
        async with bot:
            # aiogram 3.x принимает в InputFile только объект BufferedInputFile/FSInputFile,
            # "голый" open("rb") (BufferedReader) не проходит валидацию pydantic.
            with tmp.open("rb") as fh:
                doc = await bot.send_document(
                    chat_id,
                    document=BufferedInputFile(fh.read(), filename=tmp.name),
                    caption=f"Фаза 1: тест загрузки {size_mb} МБ",
                )
            log.info("Отправлено: file_unique_id=%s", doc.document.file_unique_id)
            file = await bot.get_file(doc.document.file_id)
            # На сервере с --local file.file_path — абсолютный путь ВНУТРИ контейнера
            # local-bot-api (/var/lib/telegram-bot-api/...). Скачивание по облачной ссылке
            # api.telegram.org/file/bot<token>/ для таких файлов даёт "file is too big"
            # (лимит облака 20 МБ) — читаем файл напрямую из volume bot-api-data.
            log.info("getFile: file_path=%s", file.file_path)
            local_file_size = resolve_local_file_size(file.file_path)
            if local_file_size is None:
                log.warning(
                    "Файл недоступен для прямого чтения (volume bot-api-data не примонтирован "
                    "в контейнер bot). Убедитесь, что сообщение доставлено в чат — это и есть "
                    "подтверждение работы --local."
                )
            else:
                log.info("Прямое чтение из volume: %.1f МБ", local_file_size / 1024 / 1024)
                if local_file_size < size_mb * 1024 * 1024 * 0.9:
                    raise RuntimeError(
                        f"Размер файла в volume ({local_file_size}) не соответствует "
                        f"отправленному ({size_mb} МБ)"
                    )
            await bot.send_message(chat_id, "✅ Фаза 1: upload>50МБ и getFile>20МБ прошли — --local работает.")
            ok = True
    finally:
        tmp.unlink(missing_ok=True)  # временные файлы удаляем сразу (ТЗ §3.1)
    return ok


def _local_api_mount() -> Path:
    """Точка монтирования volume local-bot-api внутри контейнера bot.

    Функция (а не module-level константа) — чтобы тесты могли патчить env
    и получать разные пути между вызовами без reload модуля.
    """
    return Path(os.getenv("BOT_API_DATA_MOUNT", "/app/bot-api-data"))


def resolve_local_file_size(file_path: str | None) -> int | None:
    """Размер файла на диске local-bot-api через проброшенный volume.

    getFile() при --local возвращает путь внутри контейнера local-bot-api.
    Формат пути зависит от версии сервера: старые — /var/lib/... (абсолютный),
    новые — documents/file_0.bin (относительный от корня dir). Перепролируем
    оба варианта в точку монтирования volume в контейнере bot, включая прямые
    попытки чтения по исходному пути (на случай других mount'ов).
    Возвращает None, если файл не найден (volume не примонтирован и т.п.).
    """
    if not file_path:
        return None
    candidates = _resolve_local_file_candidates(file_path)
    for c in candidates:
        try:
            if c.is_file():
                return c.stat().st_size
        except OSError:
            continue
    return None


def _resolve_local_file_candidates(file_path: str) -> list[Path]:
    """Возвращает список candidate-путей для файла local-bot-api.

    Вынесено в отдельную функцию чтобы переиспользовать в read_local_file_bytes.
    """
    local_mount = _local_api_mount()
    rel = file_path.lstrip("/")
    candidates = [
        local_mount / rel,
        local_mount / rel.removeprefix("var/lib/telegram-bot-api/"),
        Path(file_path),  # прямой путь (если скрипт запускается вне Docker)
    ]
    # Поиск по basename: local-bot-api может использовать разную структуру
    # директорий (/<token>/photos/, /<bot_id>/photos/, /photos/). Ищем файл
    # по имени в любых поддиректориях.
    if local_mount.is_dir():
        basename = Path(file_path).name
        if basename:
            try:
                # **/<basename> — рекурсивный поиск в любых поддиректориях
                candidates += sorted(local_mount.glob(f"**/{basename}"))
            except OSError:
                pass
    return candidates


def read_local_file_bytes(file_path: str | None) -> bytes | None:
    """Читает файл с диска local-bot-api через проброшенный volume.

    Используется вместо bot.download() для Local Bot API Server с --local:
    bot.download() формирует HTTP URL {api_url}/file/bot{token}/{file_path},
    но при --local file_path — абсолютный (/var/lib/telegram-bot-api/...),
    что даёт двойной путь и 404.

    Возвращает bytes файла или None если файл не найден / не читается.
    """
    if not file_path:
        return None
    candidates = _resolve_local_file_candidates(file_path)
    for c in candidates:
        try:
            if c.is_file():
                data = c.read_bytes()
                if data:
                    return data
        except OSError:
            continue
    # Debug-логирование: показываем все candidates и существует ли local_mount.
    # Помогает диагностировать проблемы с монтированием volume в Docker.
    local_mount = _local_api_mount()
    # Показываем содержимое local_mount (первые 2 уровня) для диагностики
    mount_contents = []
    try:
        if local_mount.is_dir():
            for entry in sorted(local_mount.iterdir())[:10]:
                mount_contents.append(str(entry))
                if entry.is_dir():
                    for sub in sorted(entry.iterdir())[:5]:
                        mount_contents.append(f"  {sub}")
    except OSError:
        pass
    log.warning(
        "read_local_file_bytes: файл не найден ни в одном candidate. "
        "file_path=%r, BOT_API_DATA_MOUNT=%s (exists=%s, is_dir=%s), candidates=%s, "
        "mount_contents=%s",
        file_path, local_mount, local_mount.exists(), local_mount.is_dir(),
        [str(c) for c in candidates], mount_contents,
    )
    return None


def read_local_file_bytes_with_retry(file_path: str | None, max_retries: int = 3, delay: float = 0.5) -> bytes | None:
    """Читает файл с retry — local-bot-api может ещё сохранять файл на диск.

    getFile() возвращает file_path сразу, но сам файл может ещё записываться
    local-bot-api на диск (особенно для больших фото). Без retry read_local_file_bytes
    не найдёт файл и вернёт None.

    Делает до max_retries попыток с задержкой delay между ними.
    """
    if not file_path:
        return None
    import time

    for attempt in range(max_retries):
        data = read_local_file_bytes(file_path)
        if data:
            return data
        if attempt < max_retries - 1:
            time.sleep(delay)
    return None
