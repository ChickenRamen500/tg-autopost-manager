"""Настройки приложения (ТЗ §3.2, §3.3).

Все значения читаются из переменных окружения / .env-файла.
Пример наполнения — в `.env.example`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта: .../app/config/settings.py -> project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    """Парсит int из env с обработкой невалидных значений.

    Если переменная задана, но не парсится как int (например MAX_SEND_ATTEMPTS=abc),
    логируем warning и возвращаем default — приложение продолжает работать,
    а не падает на старте.
    """
    raw = _get(name, str(default))
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logging.getLogger("config").warning(
            "Невалидное значение %s=%r, использую default=%d", name, raw, default
        )
        return default


@dataclass(frozen=True)
class Settings:
    # --- Telegram ---
    bot_token: str = field(default_factory=lambda: _get("BOT_TOKEN"))
    telegram_api_id: int = field(default_factory=lambda: _get_int("TELEGRAM_API_ID", 0))
    telegram_api_hash: str = field(default_factory=lambda: _get("TELEGRAM_API_HASH"))

    # --- Local Bot API Server (ТЗ §3.3) ---
    # Внутри docker-сети: http://local-bot-api:8081; на хосте для тестов: http://localhost:8081
    bot_api_url: str = field(default_factory=lambda: _get("BOT_API_URL", "http://local-bot-api:8081"))

    # --- Безопасность (REST API + Chrome-расширение, ТЗ §6, §9) ---
    secret_token: str = field(default_factory=lambda: _get("SECRET_TOKEN"))
    # Лимит размера файла для POST /api/quick_add_upload (ТЗ §6.1.3, по умолчанию 200 МБ)
    max_upload_mb: int = field(default_factory=lambda: _get_int("MAX_UPLOAD_MB", 200))

    # --- База данных (ТЗ §5) ---
    database_url: str = field(default_factory=lambda: _get("DATABASE_URL", "sqlite:///./data/bot.db"))

    # --- Фаза 2: первичный admin (bootstrap RBAC, ТЗ §2.6) ---
    # tg_id владельца-админа. На первом /start он автоматически становится admin в БД,
    # иначе нельзя добавить пользователей (команда /add_user требует прав admin).
    admin_tg_id: int = field(default_factory=lambda: _get_int("ADMIN_TG_ID", 0))

    # --- Фаза 2: миграция кода gbooru_tg (ТЗ §10) ---
    # Опциональные ключи Gelbooru API (нужны для автопарсинга подписок — Фаза 3).
    # На Фазе 2 GelbooruClient переносится как библиотека, ключи не обязательны для импорта.
    gelbooru_api_key: str = field(default_factory=lambda: _get("GELBOORU_API_KEY", ""))
    gelbooru_user_id: str = field(default_factory=lambda: _get("GELBOORU_USER_ID", ""))

    # --- Фаза 2: планировщик (ТЗ §2.5) ---
    # Минимальная пауза между отправками в одну очередь (защита от 429, ТЗ §2.5.2 п.3)
    send_throttle_seconds: float = field(default_factory=lambda: float(_get("SEND_THROTTLE_SECONDS", "1.1") or 1.1))
    # Лимит попыток отправки поста до перевода в failed (ТЗ §2.5.2 п.4)
    max_send_attempts: int = field(default_factory=lambda: _get_int("MAX_SEND_ATTEMPTS", 3))
    # Часовой пояс для daily_window очередей (Asia/Yekaterinburg — TZ пользователя)
    timezone: str = field(default_factory=lambda: _get("TIMEZONE", "Asia/Yekaterinburg"))

    # --- Фаза 3: worker (ТЗ §4.2.2 — обработка task_queue) ---
    # Интервал опроса task_queue на новые queued-задачи, сек. Короткий интервал = быстрая
    # реакция, но больший расход CPU/БД; длинный = экономия, но задержка обработки.
    worker_poll_interval_seconds: float = field(
        default_factory=lambda: float(_get("WORKER_POLL_INTERVAL_SECONDS", "2.0") or 2.0)
    )
    # Лимит попыток выполнения задачи (download) до перевода в status='error'.
    # task_queue.attempts инкрементируется в take_next(); по достижении лимита задача
    # помечается error и больше не возвращается в очередь (требует ручного requeue).
    worker_max_attempts: int = field(default_factory=lambda: _get_int("WORKER_MAX_ATTEMPTS", 3))
    # Зависшие in_progress задачи старше этого количества минут считаются «перевисшими»
    # (worker упал / рестарт контейнера / OOM-kill) и сбрасываются обратно в queued.
    worker_stale_timeout_minutes: int = field(default_factory=lambda: _get_int("WORKER_STALE_TIMEOUT_MINUTES", 30))
    # Таймаут HTTP-скачивания внешних файлов (Gelbooru/Pikabu/...), сек. Большие файлы
    # на медленном канале могут требовать больше — но бесконечно ждать нельзя (worker
    # блокируется на одной задаче). По умолчанию 120 сек — баланс для изображений/коротких видео.
    worker_download_timeout_seconds: int = field(default_factory=lambda: _get_int("WORKER_DOWNLOAD_TIMEOUT_SECONDS", 120))
    # Период запуска cleanup-процедуры (reset_stale_in_progress), сек.
    worker_cleanup_interval_seconds: int = field(default_factory=lambda: _get_int("WORKER_CLEANUP_INTERVAL_SECONDS", 300))

    # --- Фаза 3: pHash-дедупликация (ТЗ §2.4.3) ---
    # Включить визуальную дедупликацию (imagehash.phash + Hamming distance).
    # False → посты никогда не помечаются possible_duplicate по pHash (только по file_unique_id).
    phash_enabled: bool = field(default_factory=lambda: _get("PHASH_ENABLED", "1").strip() not in ("0", "false", "no", "off"))
    # Порог расстояния Хэмминга: два изображения считаются дубликатами если
    # distance ≤ phash_max_distance. 0 = точное совпадение, 5 = «похожие» (рекомендация
    # imagehash для устойчивости к ресайзу/перекомпрессии). Выше 8 — много false positives.
    phash_max_distance: int = field(default_factory=lambda: _get_int("PHASH_MAX_DISTANCE", 5))
    # Скачивать thumbnail фото из ЛС для pHash-проверки при приёме (ТЗ §2.1.1: «файл
    # НЕ скачивается при приёме» — thumbnail это минимальное исключение ради дедупликации).
    # False → pHash при приёме не вычисляется, только после явной обработки (worker).
    phash_check_on_receive: bool = field(default_factory=lambda: _get("PHASH_CHECK_ON_RECEIVE", "1").strip() not in ("0", "false", "no", "off"))

    # --- Фаза 3: подписки Gelbooru (ТЗ §2.1.4) ---
    # Включить автопарсинг подписок (tag_subscriptions). False → worker НЕ опрашивает
    # gelbooru, подписки существуют в БД но не проверяются (для экономии трафика/CPU).
    gelbooru_subs_enabled: bool = field(default_factory=lambda: _get("GELBOORU_SUBS_ENABLED", "1").strip() not in ("0", "false", "no", "off"))
    # Интервал опроса due-подписок worker'ом, сек. Не путать с check_interval_hours
    # отдельной подписки — этот интервал определяет как часто worker вообще запускает
    # цикл проверки (внутри цикла каждая подписка проверяется только если подошёл её
    # check_interval_hours). 60 сек = баланс реактивности и нагрузки.
    gelbooru_subs_poll_interval_seconds: int = field(default_factory=lambda: _get_int("GELBOORU_SUBS_POLL_INTERVAL_SECONDS", 60))
    # Лимит постов в одном запросе к gelbooru API (max 100, gelbooru ограничивает).
    # Больше = больше постов за раз, но дольше запрос и больше расход кеша.
    gelbooru_search_limit: int = field(default_factory=lambda: _get_int("GELBOORU_SEARCH_LIMIT", 50))

    # --- Фаза 3: внешние парсеры (ТЗ §2.1.4) ---
    # Danbooru: опциональная аутентификация (без неё rate limit 1 req/s)
    danbooru_login: str = field(default_factory=lambda: _get("DANBOORU_LOGIN", ""))
    danbooru_api_key: str = field(default_factory=lambda: _get("DANBOORU_API_KEY", ""))
    # X.com: путь к cookies-файлу (Netscape format, export из браузера).
    # Без cookies большинство твитов недоступно (X требует логин).
    xcom_cookies_path: str = field(default_factory=lambda: _get("XCOM_COOKIES_PATH", ""))
    # Pixiv: опциональная аутентификация через PHPSESSID (для доступа к R-18 и поиску)
    pixiv_session_id: str = field(default_factory=lambda: _get("PIXIV_SESSION_ID", ""))

    # --- Веб-интерфейс (ТЗ §7) ---
    web_port: int = field(default_factory=lambda: _get_int("WEB_PORT", 8000))

    # --- Пути ---
    data_dir: Path = BASE_DIR / "data"
    db_path: Path = data_dir / "bot.db"
    downloads_dir: Path = data_dir / "downloads"   # автоочистка каждые 24 ч (ТЗ §3.1)
    media_dir: Path = Path(_get("MEDIA_DIR", str(data_dir / "media")))  # в Docker: /app/media

    def validate(self) -> list[str]:
        """Возвращает список критических проблем конфигурации.

        Покрывает как обязательные значения (BOT_TOKEN, TELEGRAM_API_ID, SECRET_TOKEN,
        ADMIN_TG_ID), так и диапазоны/форматы числовых и строковых полей
        (web_port, timezone, send_throttle_seconds, max_send_attempts, bot_api_url).
        """
        problems: list[str] = []
        if not self.bot_token:
            problems.append("BOT_TOKEN не задан — бот не сможет работать ни с облачным, ни с локальным API")
        if not self.telegram_api_id or not self.telegram_api_hash:
            problems.append(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы — local-bot-api не поднимется с флагом --local "
                "(получить на https://my.telegram.org, ТЗ §3.3)"
            )
        if not self.secret_token:
            problems.append("SECRET_TOKEN не задан — REST API и Chrome-расширение будут недоступны (Фаза 4)")
        if not self.admin_tg_id:
            problems.append(
                "ADMIN_TG_ID не задан — никто не сможет стать admin при первом /start "
                "(управление пользователями/каналами/очередями будет недоступно, ТЗ §2.6)"
            )

        # --- Диапазоны числовых полей ---
        if not (1 <= self.web_port <= 65535):
            problems.append(f"WEB_PORT={self.web_port} вне диапазона 1-65535")
        if self.send_throttle_seconds <= 0:
            problems.append(
                f"SEND_THROTTLE_SECONDS={self.send_throttle_seconds} должен быть > 0 "
                "(защита от 429 не работает с неположительной паузой)"
            )
        if self.max_send_attempts <= 0:
            problems.append(
                f"MAX_SEND_ATTEMPTS={self.max_send_attempts} должен быть > 0 "
                "(иначе посты никогда не перейдут в failed и будут крутиться в очереди бесконечно)"
            )

        # --- Фаза 3: worker ---
        if self.worker_poll_interval_seconds <= 0:
            problems.append(
                f"WORKER_POLL_INTERVAL_SECONDS={self.worker_poll_interval_seconds} должен быть > 0 "
                "(неположительный интервал = busy-loop, грузит CPU/БД)"
            )
        if self.worker_max_attempts <= 0:
            problems.append(
                f"WORKER_MAX_ATTEMPTS={self.worker_max_attempts} должен быть > 0 "
                "(иначе задачи никогда не перейдут в error и будут ретраиться бесконечно)"
            )
        if self.worker_stale_timeout_minutes < 0:
            problems.append(
                f"WORKER_STALE_TIMEOUT_MINUTES={self.worker_stale_timeout_minutes} должен быть >= 0"
            )
        if self.worker_download_timeout_seconds <= 0:
            problems.append(
                f"WORKER_DOWNLOAD_TIMEOUT_SECONDS={self.worker_download_timeout_seconds} должен быть > 0"
            )
        if self.worker_cleanup_interval_seconds <= 0:
            problems.append(
                f"WORKER_CLEANUP_INTERVAL_SECONDS={self.worker_cleanup_interval_seconds} должен быть > 0"
            )

        # --- Фаза 3: pHash ---
        if self.phash_max_distance < 0:
            problems.append(
                f"PHASH_MAX_DISTANCE={self.phash_max_distance} должен быть >= 0 "
                "(отрицательное расстояние Хэмминга не имеет смысла)"
            )
        if self.phash_max_distance > 32:
            problems.append(
                f"PHASH_MAX_DISTANCE={self.phash_max_distance} слишком большой (>32) — "
                "практически все изображения будут считаться дубликатами (64-битный pHash)"
            )

        # --- Фаза 3: подписки Gelbooru ---
        if self.gelbooru_subs_poll_interval_seconds <= 0:
            problems.append(
                f"GELBOORU_SUBS_POLL_INTERVAL_SECONDS={self.gelbooru_subs_poll_interval_seconds} должен быть > 0"
            )
        if not (1 <= self.gelbooru_search_limit <= 100):
            problems.append(
                f"GELBOORU_SEARCH_LIMIT={self.gelbooru_search_limit} вне диапазона 1-100 (лимит gelbooru API)"
            )

        # --- Часовой пояс: должен существовать в tzdata ---
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception as e:  # noqa: BLE001 — ZoneInfoNotFoundError -> KeyError; OSError на битой tzdata
            problems.append(
                f"TIMEZONE={self.timezone!r} не распознан ({type(e).__name__}: {e}); "
                "см. список IANA tz (https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)"
            )

        # --- bot_api_url: должен быть http(s)://... ---
        url = self.bot_api_url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            problems.append(
                f"BOT_API_URL={self.bot_api_url!r} должен начинаться с http:// или https://"
            )

        return problems


settings = Settings()
