# 🚀 Фаза 2 — Ядро: БД + приём из ЛС + очереди (ТЗ §13)

> **Статус:** реализовано. Соответствие: `DOCS/ТЗ_TechnicalDoc_v5_2026-10-11.docx`, §13 «План реализации», Фаза 2 (+§5 БД, §2.1.1 приём из ЛС, §2.2 маршрутизация, §2.5 планировщик, §2.6 RBAC, §8 TG-интерфейс, §10 миграция gbooru_tg).
>
> **Предусловие:** Фаза 1 пройдена (Local Bot API Server с `--local`, `logOut` выполнен, `getMe` → `ok:true`). См. `PHASE1_GUIDE.md`.

## Что реализовано (4 шага ТЗ §13)

| Шаг Фазы 2 (ТЗ) | Реализация |
|---|---|
| **44.** Миграция кода gbooru_tg | `app/services/parsers/gelbooru/` — `client.py` (GelbooruClient с rate limiting 8 req/s), `cache.py` (in-memory TTL кеш), `tags_db.py` (БД тегов, aiosqlite). `scripts/populate_tags.py` — bulk-наполнение |
| **45.** Схема БД (все таблицы из §5) | `app/db/schema.sql` + `app/db/database.py` (aiosqlite, WAL, FK). 9 таблиц: users, channels, queues, posts, routing_rules, signatures, duplicate_hashes, tag_subscriptions, task_queue |
| **46.** Приём медиа из ЛС: file_id, метаданные, маршрутизация | `app/bot/handlers/media.py` — photo/video/gif/document, форварды (source_type=telegram_channel), сохранение tg_file_id+file_unique_id+size+mime, авто-маршрутизация, дедуп по file_unique_id |
| **47.** APScheduler + отправка из очередей (file_id → файл) | `app/scheduler/__init__.py` — interval-job на каждую активную очередь, стратегия file_id→файл, 429 retry, троттлинг 1.1с, max 3 попытки → failed |

Плюс: RBAC-промежуточный слой (`app/bot/middleware/access.py`), админ-команды (`app/bot/handlers/admin.py`), первичный admin из `.env` (`ADMIN_TG_ID`), 3 health-эндпоинта.

---

## Шаг 0. Предусловия

1. Фаза 1 выполнена: контейнеры `local-bot-api` (порт 8081, `--local`) и `bot` поднимаются, `check_phase1.py --health` → `[OK]`.
2. В `.env` заполнены: `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `SECRET_TOKEN`.
3. Узнайте свой `tg_id` (например, у `@userinfobot`) и впишите в `ADMIN_TG_ID` — это первичный администратор.

## Шаг 1. Инициализация БД (п.45)

```bash
docker compose up -d bot            # контейнер поднимется, lifespan инициализирует БД автоматически
# или вручную:
docker compose exec bot python scripts/init_db.py
```

Что произойдёт при первом старте (lifespan `app/main.py`):
1. `init_db()` — создаёт `data/bot.db`, применяет `app/db/schema.sql` (9 таблиц + индексы), включает WAL + foreign_keys.
2. `bootstrap_admin(ADMIN_TG_ID)` — создаёт первого admin из `.env` (роль `admin`, `is_active=1`). Без него никто не сможет управлять пользователями/каналами/очередями.
3. `tags_db.init_tags_db()` — отдельная БД `data/tags.db` для метаданных тегов Gelbooru (ТЗ §10.1).
4. `cache.start_cleanup_task()` — фоновая очистка in-memory кеша Gelbooru (каждые 10 мин).
5. `setup_scheduler_async()` + `scheduler.start()` — APScheduler регистрирует job на каждую активную очередь.
6. `dp.start_polling(bot)` — приём обновлений из ЛС через Local Bot API.

Проверка:
```bash
docker compose exec bot python scripts/check_phase2.py --db
# [OK] Все 9 таблиц из ТЗ §5 присутствуют
# [OK] admin в БД: tg_id=... role=admin
# [OK] RBAC: admin >= moderator, moderator не проходит admin-проверку
```

## Шаг 2. Маршрутизация (п.46, ТЗ §2.2.2)

При поступлении медиа бот:
1. Извлекает `tg_file_id`, `file_unique_id`, `file_size`, `mime_type`, `caption`, метаданные источника (`forward_from_chat` → `source_type=telegram_channel`).
2. Проверяет дубликат по `file_unique_id` (ТЗ §2.4.3 — точный уровень): если найден → пост в «Входящие» со статусом `possible_duplicate`.
3. Применяет правила маршрутизации (`app/services/routing/service.py`):
   - правила отсортированы по `priority DESC`;
   - `source_type` матчится точно или `any` (любой);
   - `source_pattern` — regex (если содержит спецсимволы `.+*?[](){}|^$\`) или точное совпадение;
   - проверка по `source_id` **и** по строке тегов (для booru-правил вида `.*rating:safe.*`);
   - первое совпавшее правило (наивысший приоритет) → `target_queue_id`;
   - нет правила → пост во «Входящие» (`queue_id=NULL`).

Проверка демо-маршрутизации:
```bash
docker compose exec bot python scripts/check_phase2.py --routing
# [OK] Точное совпадение telegram_channel/@anime_channel → queue 1
# [OK] Regex по тегам gelbooru (.*rating:safe.*) → queue 1
# [OK] Нет правила для x.com → Входящие (None)
```

Создать правило вручную (примеры ТЗ §5.2):
```bash
# через бота (admin):
/add_channel -1001234567890 Anime_Channel @anime_channel
/add_queue 1 "Anime Safe" 120
# затем правило — пока только через БД/веб (Фаза 5). Например, в sqlite:
# INSERT INTO routing_rules (source_type, source_pattern, target_queue_id, priority)
#   VALUES ('telegram_channel', '@anime_channel', 1, 10);
```

## Шаг 3. Планировщик и постинг (п.47, ТЗ §2.5)

`app/scheduler/__init__.py`:
- Для каждой **активной** очереди (`is_active=1`, канал активен) регистрируется `interval`-job с `id=queue_<id>`, `minutes=interval_minutes`.
- `send_post_from_queue(queue_id)`:
  1. Берёт следующий `pending` пост (`ORDER BY scheduled_time ASC NULLS FIRST, created_at ASC`).
  2. **Стратегия отправки (ТЗ §2.5.1):**
     - если есть `tg_file_id` и `was_processed=False` → отправка по `file_id` (без чтения файла, без лимита размера);
     - иначе → локальный файл через Local Bot API Server (чтение в потоке `asyncio.to_thread`, до 2 ГБ).
  3. Успех → `status='sent'`, `sent_at=now`.
  4. Ошибка (ТЗ §2.5.2):
     - 429 Too Many Requests → ждём `retry_after`, повтор;
     - троттлинг 1.1 сек между отправками в одну очередь (`SEND_THROTTLE_SECONDS`);
     - max 3 попытки (`MAX_SEND_ATTEMPTS`) → `status='failed'`, `last_error` сохранён.

Проверка списка job'ов:
```bash
curl http://localhost:8000/health/scheduler
# {"status":"ok","jobs":[{"id":"queue_1","next_run":"..."}]}
# или:
docker compose exec bot python scripts/check_phase2.py --scheduler
```

> ⚠️ Новая очередь, добавленная через `/add_queue`, подхватится планировщиком **после перезапуска** контейнера (`docker compose restart bot`). Динамическая перерегистрация — Фаза 6.

## Шаг 4. Telegram-команды (ТЗ §8)

Напишите боту `/start` (с `tg_id == ADMIN_TG_ID`) — станет admin автоматически.

**Модератор + Admin:**
- `/list_queues` — список очередей
- `/queue <queue_id>` — посты очереди
- `/remove_from_queue <post_id>` — перенести пост во «Входящие»
- `/stats` — статистика (постов/очередей/пользователей)

**Только Admin:**
- `/add_user @username <role>` → `/set_role @username <role>` (переслав сообщение юзера) — добавление пользователя
- `/remove_user @username`, `/list_users`
- `/add_channel <tg_chat_id> <name> [@username]`, `/list_channels`, `/remove_channel <channel_id>`
- `/add_queue <channel_id> <name> <interval_min>`, `/remove_queue <queue_id>`
- `/subscribe gelbooru <tag1> <tag2> ...`, `/list_subscriptions` (автопарсинг — Фаза 3)

**Приём медиа:** просто пришлите боту фото/видео/GIF/документ (или перешлите из канала). Бот ответит, в какую очередь поставлен пост (или во «Входящие»).

## Критерии приёмки Фазы 2

- [x] Схема БД содержит все 9 таблиц из ТЗ §5 (`check_phase2.py --db`)
- [x] Приём медиа из ЛС сохраняет `tg_file_id`+`file_unique_id`+`file_size`+`mime_type` (стратегия file_id, ТЗ §2.1.1 п.4)
- [x] Маршрутизация: точное совпадение + regex + правило `any` → Входящие (ТЗ §2.2.2)
- [x] RBAC: admin ≥ moderator, гость блокируется, кроме `/start` (ТЗ §2.6)
- [x] APScheduler: interval-job на каждую активную очередь (ТЗ §2.5.1)
- [x] Стратегия отправки file_id → файл (ТЗ §2.5.1)
- [x] 429 retry + троттлинг + max 3 попытки → failed (ТЗ §2.5.2)
- [x] Миграция gbooru_tg: GelbooruClient + cache + tags_db (`aiosqlite`) (ТЗ §10)
- [x] Юнит-тесты: 25 passed (`pytest tests/unit -q`), ruff: 0 ошибок

## Тесты

```bash
pytest tests/unit -q
# 25 passed
#   test_settings.py      (4)  — Фаза 1 + адаптация под ADMIN_TG_ID
#   test_routing.py        (6)  — regex/exact/any, приоритеты
#   test_repositories.py  (8)  — CRUD users/posts/task_queue, RBAC, дедуп
#   test_scheduler.py      (4)  — стратегия file_id/файл, попытки, порядок pending
#   test_regression.py     (3)  — caption_raw, дубликат канала, lookup канала
ruff check app/ scripts/ tests/ conftest.py   # All checks passed!
```

## Структура файлов Фазы 2

```
app/
├── main.py                      # FastAPI lifespan: init_db + bootstrap_admin + scheduler + polling
├── config/settings.py           # + ADMIN_TG_ID, GELBOORU_*, SEND_THROTTLE, MAX_SEND_ATTEMPTS
├── db/
│   ├── schema.sql               # 9 таблиц ТЗ §5 + индексы
│   ├── database.py              # aiosqlite singleton, WAL, FK, bootstrap_admin
│   └── __init__.py
├── repositories/                # CRUD-слой (без бизнес-логики)
│   ├── users.py                 # + RBAC check_access (admin >= moderator)
│   ├── channels.py
│   ├── queues.py
│   ├── posts.py                 # file_id-поля, жизненный цикл, дедуп по file_unique_id
│   ├── routing.py
│   ├── signatures.py
│   ├── duplicates.py            # pHash с расстоянием Хэмминга
│   ├── subscriptions.py
│   └── task_queue.py            # take_next через UPDATE...RETURNING
├── services/
│   ├── routing/service.py       # алгоритм §2.2.2 (regex/exact/any, приоритеты)
│   └── parsers/gelbooru/        # миграция gbooru_tg (ТЗ §10)
│       ├── client.py            # GelbooruClient (rate limit 8 req/s), конфиг из settings
│       ├── cache.py             # in-memory TTL кеш (10 мин / 1 час)
│       └── tags_db.py           # БД тегов (aiosqlite), категоризация, autocomplete
├── bot/
│   ├── __init__.py              # Dispatcher + AccessMiddleware + роутеры
│   ├── middleware/access.py    # RBAC: admin/moderator, bootstrap admin, гость → /start only
│   ├── handlers/
│   │   ├── start.py             # /start, /help
│   │   ├── media.py             # приём медиа, file_id, маршрутизация, дедуп
│   │   └── admin.py             # команды §8.1/§8.2
│   └── keyboards/
└── scheduler/__init__.py        # APScheduler: interval-jobs очередей, стратегия file_id→файл

scripts/
├── init_db.py                   # инициализация БД + bootstrap admin
├── check_phase2.py              # --db / --routing / --scheduler
└── populate_tags.py             # bulk-наполнение tags.db из Gelbooru API

tests/unit/                      # 22 теста (routing, repositories, scheduler, settings)
```

## Расширения схемы сверх ТЗ §5 (обоснование)

| Поле | Где | Зачем |
|---|---|---|
| `posts.send_attempts INTEGER` | posts | Учёт попыток отправки (ТЗ §2.5.2 п.4: «макс. 3 попытки → failed»). В §5 нет, но §2.5.2 требует. |
| `posts.last_error TEXT` | posts | Диагностика последней ошибки отправки (ТЗ §2.5.2 «уведомление админа»). |

Эти поля не нарушают логику §5 — они дополняют её для реализации §2.5.2.

## Что дальше (Фаза 3 — Worker + внешние источники)

- Персистентная `task_queue` уже есть (репозиторий готов) — worker будет брать задачи `download`/`process`/`repost`.
- Скачивание с заголовками: Gelbooru (Referer+UA), Pixiv (Referer), Pikabu — `app/services/parsers/`.
- yt-dlp с cookies для X.com.
- Полный дедуп: `file_unique_id`, `source_id`, `pHash` (репозиторий `duplicates.py` готов с Хэммингом).
- Автопарсинг подписок: `subscriptions.list_due_subscriptions()` готов, нужен polling-воркер.

## Возможные проблемы

- **Бот не отвечает на /start:** проверьте, что `ADMIN_TG_ID` совпадает с вашим `tg_id`, и что `logOut` выполнен (Фаза 1). Без admin в БД middleware пропустит только `/start`/`/help`.
- **`/health/scheduler` → пустой список:** нет активных очередей. Создайте канал + очередь (`/add_channel`, `/add_queue`), затем `docker compose restart bot`.
- **Новая очередь не публикует:** планировщик регистрирует job'ы только при старте. После `/add_queue` — `docker compose restart bot`.
- **`populate_tags.py` висит:** без `GELBOORU_API_KEY`/`GELBOORU_USER_ID` публичный endpoint работает, но медленнее. Ключи опциональны для Фазы 2.

## Исправления post-release (runtime-логи)

После запуска Фазы 2 в реальном окружении обнаружены и исправлены 3 ошибки:

| # | Симптом в логе | Причина | Фикс |
|---|---|---|---|
| 1 | `AttributeError: 'Message' object has no attribute 'caption_raw'` при приёме любого медиа | `media.py` обращался к `message.caption_raw` — этого атрибута **нет в aiogram 3.x** (был в 2.x). Каждый приём медиа падал. | `app/bot/handlers/media.py`: оставлен только `message.caption or None`. |
| 2 | `sqlite3.IntegrityError: UNIQUE constraint failed: channels.tg_chat_id` при повторном `/add_channel` | `/add_channel` с уже существующим `tg_chat_id` падал без обработки. | `app/repositories/channels.py`: введён `DuplicateChannelError`; `app/bot/handlers/admin.py`: проверка `get_channel_by_chat_id` до вставки + catch `DuplicateChannelError` (race condition). |
| 3 | `sqlite3.OperationalError: unable to open database file` в `check_phase2.py --routing` при запущенном боте | `--routing` открывал **реальную** `bot.db`, которую уже держал uvicorn-процесс бота. На Windows + Docker volume `PRAGMA journal_mode=WAL` падала (файл `-shm` залочен). Плюс скрипт засорял БД демо-данными. | (а) `scripts/check_phase2.py --routing` переписан на **изолированную временную БД** (`tempfile.mkdtemp`) — не трогает реальную БД, работает всегда. (б) `app/db/database.py`: `PRAGMA busy_timeout=5000` + try/except вокруг `journal_mode=WAL` (fallback на default journal, если WAL не доступен). |

Проверка фиксов:
```bash
pytest tests/unit -q                              # 25 passed (3 новых в test_regression.py)
ruff check app/ scripts/ tests/ conftest.py      # All checks passed!
docker compose exec bot python scripts/check_phase2.py --routing
# теперь работает даже при запущенном боте (временная БД)
```
