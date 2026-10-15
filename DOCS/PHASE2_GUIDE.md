# 🚀 Фаза 2 — Ядро: БД + приём из ЛС + очереди (ТЗ §13)

> **Статус:** реализовано. Соответствие: `DOCS/ТЗ_TechnicalDoc_v5_2026-10-11.docx`, §13 «План реализации», Фаза 2 (+§5 БД, §2.1.1 приём из ЛС, §2.2 маршрутизация, §2.5 планировщик, §2.6 RBAC, §8 TG-интерфейс, §10 миграция gbooru_tg).
>
> **Предусловие:** Фаза 1 пройдена (Local Bot API Server с `--local`, `logOut` выполнен, `getMe` → `ok:true`). См. `PHASE1_GUIDE.md`.

## Что реализовано (4 шага ТЗ §13)

| Шаг Фазы 2 (ТЗ) | Реализация |
|---|---|
| **44.** Миграция кода gbooru_tg | `app/services/parsers/gelbooru/` — `client.py` (GelbooruClient с rate limiting 8 req/s), `cache.py` (in-memory TTL кеш), `tags_db.py` (БД тегов, aiosqlite). `scripts/populate_tags.py` — bulk-наполнение |
| **45.** Схема БД (все таблицы из §5) | `app/db/schema.sql` + `app/db/database.py` (aiosqlite, WAL, FK). 10 таблиц: users, channels, queues, posts, routing_rules, signatures, duplicate_hashes, tag_subscriptions, task_queue |
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
1. `init_db()` — создаёт `data/bot.db`, применяет `app/db/schema.sql` (10 таблиц + индексы), включает WAL + foreign_keys.
2. `bootstrap_admin(ADMIN_TG_ID)` — создаёт первого admin из `.env` (роль `admin`, `is_active=1`). Без него никто не сможет управлять пользователями/каналами/очередями.
3. `tags_db.init_tags_db()` — отдельная БД `data/tags.db` для метаданных тегов Gelbooru (ТЗ §10.1).
4. `cache.start_cleanup_task()` — фоновая очистка in-memory кеша Gelbooru (каждые 10 мин).
5. `setup_scheduler_async()` + `scheduler.start()` — APScheduler регистрирует job на каждую активную очередь.
6. `dp.start_polling(bot)` — приём обновлений из ЛС через Local Bot API.

Проверка:
```bash
docker compose exec bot python scripts/check_phase2.py --db
# [OK] Все 10 таблиц из ТЗ §5 присутствуют
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

> ⚠️ **Внимание:** новая очередь, добавленная через `/add_queue`, подхватывается планировщиком **автоматически** (hot-reload) — перезапуск контейнера НЕ нужен. Команда `/reload_scheduler` принудительно перерегистрирует все очереди.

## Шаг 4. Inline-меню (ТЗ §8.3) — основной способ управления

Напишите боту `/start` или `/menu` — откроется **главное меню с кнопками**. Все действия выполняются через inline-кнопки (нажатия на сообщения), текстовые команды сохранены как fallback.

### Главное меню

```
🏠 Главное меню

[📥 Входящие]  [📋 Очереди]
[📊 Статистика]
[📢 Каналы]  [👥 Пользователи]   ← только admin
[➕ Добавить]                     ← только admin
```

### Как добавить канал (кнопками)

1. `/menu` → кнопка **➕ Добавить** → **📢 Канал**
2. Шаг 1: пришли числовой ID канала (узнать у `@userinfobot`, вида `-1002257882757`)
3. Шаг 2: пришли название (например `МойКанал`)
4. Шаг 3: пришли @username или `-`

> ⚠️ Бот должен быть **админом** в целевом канале с правом публикации.

### Как добавить очередь

1. `/menu` → **➕ Добавить** → **📋 Очередь**
2. Шаг 1: нажми кнопку канала-получателя
3. Шаг 2: название очереди
4. Шаг 3: интервал в минутах (например `60`)
5. Шаг 4: группировать альбомы? (да/нет)
6. Шаг 5: время начала и конца очереди (например `14.10.26 06:00 - 16.10.26 22:00`, или `-`)
7. Шаг 6: ежедневное окно отправки (например `06:00 - 22:00`, или `-`)
8. Шаг 7: стратегия завершения (1-4)
9. ✓ Планировщик перерегистрируется автоматически (hot-reload, без restart)

#### Время очереди (блок B)

Очередь может работать:
- **Без ограничений** (шаги 5-7 = `-`) — посты сразу, в любое время
- **С абсолютными датами** (шаг 5): `start_at` / `end_at` — когда очередь активна
- **С ежедневным окном** (шаг 6): `daily_window` — часы работы каждый день (локальное время)
- **Комбинация**: абсолютные даты + ежедневное окно внутри

Пример: очередь с 14.10.26 до 16.10.26, окно 6:00–22:00, каждые 30 мин:
- 14.10 06:00 — первый пост
- до 14.10 22:00 — 32 поста
- пауза на ночь
- 15.10 06:00 — продолжение
- 16.10 22:00 — последний пост (если постов больше, сработает стратегия)

#### Стратегии завершения (шаг 7)

Когда `end_at` достигнут, а pending посты ещё есть:
1. **continue_next_day** (по умолчанию) — продолжить в следующем `daily_window`
2. **continue_after_end** — игнорировать `end_at`, шлём по интервалу
3. **move_to_inbox** — все pending → Входящие (queue_id=NULL)
4. **delete** — удалить pending посты

В карточке очереди видно текущие настройки: «Время: старт: ... | конец: ... | окно: ... | стратегия: ...»

### Как добавить пользователя

1. `/menu` → **👥 Пользователи** → **➕ Добавить пользователя**
2. Шаг 1: пришли числовой tg_id (пользователь узнаёт у `@userinfobot`) ИЛИ перешли сообщение от него
3. Шаг 2: роль — `admin` или `moderator`

### Как поставить пост в очередь (интерактивный выбор)

**Для постов из ЛС бота и пересылок от юзеров** (блок A — интерактивный выбор):

1. Пришли боту фото/видео/GIF → пост **сразу попадает во Входящие**
2. Бот в ответ присылает: «📥 Пост #13 → Входящие. Куда отправить?» + кнопки **каналов**
3. Нажми канал → кнопки заменятся на **очереди этого канала**
4. Если очередей нет → кнопка «➕ Создать очередь» (создаст и вернёт к выбору)
5. Нажми очередь → пост перемещён, бот подтверждает

**Для пересылок из каналов** (`telegram_channel`) — работают **правила маршрутизации** (автоматически). Если правила нет — пост во Входящих, можно назначить через `/menu` → **📥 Входящие** → карточка поста.

> ℹ️ `telegram_direct` и `telegram_user` убраны из UI создания правил — посты из ЛС всегда идут через интерактивный выбор (гибче, чем одно правило на весь ЛС).

### Как создать правило маршрутизации (автоматическая сортировка)

Чтобы посты автоматически попадали в нужную очередь (без ручного «Входящие → очередь»):

1. `/menu` → **➕ Добавить** → **🔀 Правило**
2. Шаг 1: выбери тип источника кнопкой:
   - 📢 Telegram-канал (для пересланных из каналов)
   - 💬 ЛС (прямой)
   - 👤 Telegram-юзер
   - 📁 Локальная папка
   - 🖼 Gelbooru
   - ⭐ Любой (по умолчанию)
3. Шаг 2: пришли паттерн для source_id:
   - Точное совпадение: `-1002257882757` или `@anime_channel`
   - Regex (со спецсимволами): `.*rating:safe.*`
   - Любой: `.*`
4. Шаг 3: выбери целевую очередь кнопкой
5. Шаг 4: приоритет (целое число, выше = важнее)

**Пример:** правило `telegram_channel / @anime_channel → очередь #1 (priority 10)` — все пересланные из `@anime_channel` посты автоматически пойдут в очередь #1.

> Управлять правилами: `/menu` → **🔀 Маршрутизация** (список с кнопками удаления).

### Альбомы (групповая отправка)

По ТЗ §2.1.1 альбомы можно отправлять **по одному** (каждый файл отдельным сообщением) или **группой** (одним сообщением через `send_media_group`). Настройка задаётся при создании очереди:

1. `/menu` → **➕ Добавить** → **📋 Очередь**
2. На шаге 4/4 «Группировать альбомы?» ответь:
   - **«да»** — альбом из N фото отправится в канал одним сообщением (как в Telegram)
   - **«нет»** — каждое фото отдельным сообщением

В карточке очереди видно текущую настройку: «Альбомы группой: да (send_media_group)» или «нет (по одному)».

При приёме бот сохраняет `media_group_id` для каждого файла альбома. Если у очереди `group_albums=True`, scheduler собирает все посты с одинаковым `media_group_id` и отправляет их одним вызовом `send_media_group`. Если `False` — каждый пост отправляется отдельно по `file_id`.

Уведомление при приёме альбома сокращено: «📥 Пост #N → Входящие (альбом)».

### Команды (fallback, текстом)

Текстовые команды работают параллельно с кнопками для совместимости:
- `/menu` — открыть главное меню
- `/cancel` — отменить текущее действие (FSM)
- `/start`, `/help` — открыть меню + краткая справка
- `/add_to_queue <post_id> <queue_id>`, `/list_inbox`, `/reload_scheduler` и др.

## Как пользоваться: ответы на частые вопросы

> **Основной способ управления — inline-меню** (`/menu`). Текстовые команды ниже — для справки/fallback.

### 1. Как добавить пользователя?

**Способ А (рекомендуемый) — по числовому tg_id:**
1. Будущий пользователь пишет боту `@userinfobot` — тот отвечает числовым ID (например, `987654321`).
2. Admin пишет боту:
   ```
   /add_user 987654321 moderator sankta17
   ```
   (третий аргумент — имя/username, опционален)
3. Пользователь пишет боту `/start` — готово, он модератор.

**Способ Б — по @username через пересылку:**
1. Admin: `/add_user @sankta17 moderator`
2. Будущий пользователь пишет боту `/start` (или любое сообщение).
3. Admin пересылает это сообщение боту и отвечает на него: `/set_role @sankta17 moderator`
   (либо отправляет `/set_role @sankta17 moderator` ответом (reply) на пересланное сообщение).

> ⚠️ В aiogram 3.x `forward_from` недоступен (Telegram privacy) — используется `forward_origin` + `reply_to_message`.

### 2. Как добавить канал, куда будут публиковаться посты?

1. **Создайте Telegram-канал** (или используйте существующий).
2. **Добавьте бота как администратора** в этот канал с правом публикации сообщений.
   *(Без этого бот не сможет отправлять посты — будет ошибка `Forbidden: bot is not a member`)*
3. **Узнайте числовой ID канала**:
   - Перешлите любое сообщение из канала боту `@userinfobot` — он ответит ID (вида `-1002257882757`).
   - Или: канал → информация → ID виден через сторонние инструменты.
4. Admin пишет боту:
   ```
   /add_channel -1002257882757 МойКанал my_channel_username
   ```
   (`@username` опционален — нужен только для отображения; числовой ID обязателен)

5. Проверьте: `/list_channels` → видите канал #1.

### 3. Почему для активации очереди нужен перезапуск?

**Раньше** (до этого фикса) — да, планировщик регистрировал job'ы только при старте контейнера. После `/add_queue` нужно было `docker compose restart bot`.

**Теперь** — **перезапуск НЕ нужен**. При `/add_queue` и `/remove_queue` планировщик **автоматически перерегистрируется** (hot-reload). Команда `/reload_scheduler` принудительно перерегистрирует все очереди:
```
/add_queue 1 МояОчередь 60
# → ✓ Очередь добавлена. Планировщик перерегистрирован: активных очередей — 1. Перезапуск не нужен.
```

### Как связать посты с очередью?

По умолчанию посты из ЛС идут во **«Входящие»** (нет правила маршрутизации). Два способа:

**Способ А — вручную:**
```
/list_inbox                          # видите посты без очереди
/add_to_queue 3 1                    # пост #3 → очередь #1
```

**Способ Б — правило маршрутизации (автоматически, Фаза 5 через веб):**
Пока веб-интерфейса нет, правило можно добавить напрямую в БД:
```sql
INSERT INTO routing_rules (source_type, source_pattern, target_queue_id, priority)
VALUES ('telegram_channel', '@anime_channel', 1, 10);
```
После этого все пересланные из `@anime_channel` посты автоматически пойдут в очередь #1.

**Приём медиа:** просто пришлите боту фото/видео/GIF/документ (или перешлите из канала). Бот ответит, в какую очередь поставлен пост (или во «Входящие»).

## Критерии приёмки Фазы 2

- [x] Схема БД содержит все 10 таблиц из ТЗ §5 (`check_phase2.py --db`)
- [x] Приём медиа из ЛС сохраняет `tg_file_id`+`file_unique_id`+`file_size`+`mime_type` (стратегия file_id, ТЗ §2.1.1 п.4)
- [x] Маршрутизация: точное совпадение + regex + правило `any` → Входящие (ТЗ §2.2.2)
- [x] RBAC: admin ≥ moderator, гость блокируется, кроме `/start` (ТЗ §2.6)
- [x] APScheduler: interval-job на каждую активную очередь (ТЗ §2.5.1)
- [x] Стратегия отправки file_id → файл (ТЗ §2.5.1)
- [x] 429 retry + троттлинг + max 3 попытки → failed (ТЗ §2.5.2)
- [x] Миграция gbooru_tg: GelbooruClient + cache + tags_db (`aiosqlite`) (ТЗ §10)
- [x] Юнит-тесты: 61 passed (`pytest tests/unit -q`), ruff: 0 ошибок

## Тесты

```bash
pytest tests/unit -q
# 61 passed
#   test_settings.py      (4)  — Фаза 1 + адаптация под ADMIN_TG_ID
#   test_routing.py        (6)  — regex/exact/any, приоритеты
#   test_repositories.py  (8)  — CRUD users/posts/task_queue, RBAC, дедуп
#   test_scheduler.py      (4)  — стратегия file_id/файл, попытки, порядок pending
#   test_regression.py    (19)  — caption_raw, дубликат канала, lookup, add_to_queue, hot-reload, forward_origin
ruff check app/ scripts/ tests/ conftest.py   # All checks passed!
```

## Структура файлов Фазы 2

```
app/
├── main.py                      # FastAPI lifespan: init_db + bootstrap_admin + scheduler + polling
├── config/settings.py           # + ADMIN_TG_ID, GELBOORU_*, SEND_THROTTLE, MAX_SEND_ATTEMPTS
├── db/
│   ├── schema.sql               # 10 таблиц ТЗ §5 + индексы
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
- **`/health/scheduler` → пустой список:** нет активных очередей. Создайте канал + очередь (`/add_channel`, `/add_queue`) — планировщик перерегистрируется автоматически (hot-reload).
- **Бот не публикует в канал:** проверьте, что бот **добавлен как администратор** в целевой канал. Иначе `send_photo` вернёт `Forbidden: bot is not a member of the channel chat`.
- **Посты идут во «Входящие», а не в очередь:** нет правила маршрутизации. `/list_inbox` → `/add_to_queue <post_id> <queue_id>` (вручную), либо добавьте правило в `routing_rules` (см. раздел «Как связать посты с очередью»).
- **`docker compose up -d --build` → `Access is denied` на `data\bot.db`:** БД залочена работающим контейнером. Фикс: `.dockerignore` теперь исключает `data/*.db` из build context. Если ошибка повторяется — `docker compose down` перед билдом.
- **`populate_tags.py` висит:** без `GELBOORU_API_KEY`/`GELBOORU_USER_ID` публичный endpoint работает, но медленнее. Ключи опциональны для Фазы 2.

## Исправления post-release (runtime-логи)

### Раунд 1 — 3 ошибки (PR #14)

| # | Симптом в логе | Причина | Фикс |
|---|---|---|---|
| 1 | `AttributeError: 'Message' object has no attribute 'caption_raw'` при приёме любого медиа | `media.py` обращался к `message.caption_raw` — этого атрибута **нет в aiogram 3.x** (был в 2.x). Каждый приём медиа падал. | `app/bot/handlers/media.py`: оставлен только `message.caption or None`. |
| 2 | `sqlite3.IntegrityError: UNIQUE constraint failed: channels.tg_chat_id` при повторном `/add_channel` | `/add_channel` с уже существующим `tg_chat_id` падал без обработки. | `app/repositories/channels.py`: введён `DuplicateChannelError`; `app/bot/handlers/admin.py`: проверка `get_channel_by_chat_id` до вставки + catch `DuplicateChannelError` (race condition). |
| 3 | `sqlite3.OperationalError: unable to open database file` в `check_phase2.py --routing` при запущенном боте | `--routing` открывал **реальную** `bot.db`, которую уже держал uvicorn-процесс бота. На Windows + Docker volume `PRAGMA journal_mode=WAL` падала (файл `-shm` залочен). Плюс скрипт засорял БД демо-данными. | (а) `scripts/check_phase2.py --routing` переписан на **изолированную временную БД** (`tempfile.mkdtemp`) — не трогает реальную БД, работает всегда. (б) `app/db/database.py`: `PRAGMA busy_timeout=5000` + try/except вокруг `journal_mode=WAL` (fallback на default journal, если WAL не доступен). |

### Раунд 2 — UX и баги из логов 3–5

| # | Симптом | Причина | Фикс |
|---|---|---|---|
| 4 | После `/add_queue` очередь не публикует — нужен `docker compose restart bot` | Планировщик регистрировал job'ы только при старте контейнера (design-дефект). | `app/scheduler/__init__.py`: добавлен `reload_scheduler()` (hot-reload). `/add_queue` и `/remove_queue` вызывают его автоматически. Команда `/reload_scheduler` — ручная перерегистрация. **Перезапуск контейнера больше не нужен.** |
| 5 | Все пересланные посты шли во «Входящие» — `routing: source_id=None` | `_extract_source` использовал `getattr(fwd, "chat_id")` — в aiogram 3.x `forward_origin` имеет тип `MessageOriginChannel` с вложенным `chat` (не `chat_id`). | `app/bot/handlers/media.py`: переписан `_extract_source` под aiogram 3.x — обрабатывает `MessageOriginChannel`/`Chat`/`User`/`HiddenUser` через `fwd.chat.id`/`fwd.sender_user.id`. |
| 6 | `/add_user @sankta17 moderator` → «попроси его написать /start, перешли сообщение» — запутанный flow | Нельзя получить `tg_id` из `@username` через Bot API. | `app/bot/handlers/admin.py`: `/add_user` теперь принимает числовой `tg_id` напрямую: `/add_user 987654321 moderator [имя]` (рекомендуется). Пользователь узнаёт свой ID у `@userinfobot`. |
| 7 | `/set_role` не работал — `forward_from` всегда None в aiogram 3.x | `message.forward_from` заменён на `forward_origin` в 3.x. | `app/bot/handlers/admin.py`: `/set_role` использует `forward_origin.sender_user` + `reply_to_message.from_user` (два способа определения tg_id). |
| 8 | Нет команды переместить пост из «Входящих» в очередь | Только `/remove_from_queue` (обратное действие). | Добавлены `/add_to_queue <post_id> <queue_id>` и `/list_inbox` — ручная маршрутизация постов. |
| 9 | `docker compose up -d --build` → `CreateFile data\bot.db: Access is denied` | Dockerfile копировал `data/` (с залоченной БД) в образ. | Создан `.dockerignore` — `data/*.db` исключены из build context. БД создаётся при старте контейнера, в образ не попадает. |

Проверка фиксов:
```bash
pytest tests/unit -q                              # 61 passed (30 в test_regression.py)
ruff check app/ scripts/ tests/ conftest.py      # All checks passed!
# /add_queue → "Планировщик перерегистрирован: активных очередей — 1. Перезапуск не нужен."
```

### Раунд 3 — inline-меню вместо текстовых команд

**Проблема:** текстовые команды сложны в использовании (`/add_user @username moderator` → «попроси написать /start, перешли сообщение» и т.д.). Пользователь не мог добавить юзера и поставить пост в очередь.

**Фикс:** реализовано полнофункциональное **inline-меню с кнопками** (ТЗ §8.3) — основной способ управления:

| Компонент | Файл | Что делает |
|---|---|---|
| Клавиатуры | `app/bot/keyboards/builder.py` | Построители inline-кнопок для всех экранов |
| Меню + callback-хендлеры | `app/bot/handlers/menu.py` | Главное меню, очереди, Входящие, карточка поста, каналы, пользователи, статистика |
| FSM (многошаговый ввод) | `app/bot/handlers/menu.py::AddStates` | Добавление канала/очереди/пользователя в 2-3 шага |
| `/menu`, `/start` | `app/bot/handlers/menu.py` | Открывают главное меню с кнопками |
| `/cancel` | `app/bot/handlers/menu.py` | Отмена текущего FSM-действия |
| Runtime Bot | `app/bot/runtime.py` | Разрывает циклический импорт bot↔scheduler |
| MemoryStorage | `app/bot/__init__.py` | FSM-хранилище для состояний |

**Поток постановки поста в очередь (кнопками):**
1. Пришли фото → пост во Входящих
2. `/menu` → **📥 Входящие** → нажми пост
3. В карточке нажми кнопку очереди (➡ «Имя» #N) — готово

**Поток добавления пользователя (кнопками):**
1. `/menu` → **👥 Пользователи** → **➕ Добавить**
2. Перешли сообщение от юзера ИЛИ пришли числовой tg_id
3. Роль: `admin`/`moderator`

Текстовые команды (`/add_user`, `/add_channel`, `/add_queue`, `/list_inbox`, `/add_to_queue` и др.) сохранены как fallback — работают параллельно с кнопками.

Проверка:
```bash
pytest tests/unit -q   # 61 passed
ruff check             # All checks passed!
# /menu → главное меню с кнопками
```

### Раунд 4 — FSM-ловушка, routing rules UI, альбомы

**Проблема (логи 6-7):** пользователь начал «Добавить очередь», затем отправил фото (без подписи) — бот выдал «Название не может быть пустым» × 3. Команда `/cancel` не работала — перехватывалась FSM-хендлером, который ждал текст. Пользователь застрял в состоянии, не мог выйти.

| # | Симптом | Причина | Фикс |
|---|---|---|---|
| 10 | Фото во время FSM → «Название не может быть пустым» × N | FSM-хендлеры регистрировались раньше `cmd_cancel` и не фильтровали `F.text` → фото попадало в хендлер, `message.text=None` → «пустое» | `app/bot/handlers/menu.py`: (а) `cmd_cancel` зарегистрирован ПЕРВЫМ (без фильтра состояния — перехватывает /cancel всегда); (б) FSM-хендлеры получили фильтр `F.text`; (в) добавлен `fsm_non_text_in_state` — catch-all для фото/видео в FSM: «✏️ Я жду текст, /cancel для отмены» |
| 11 | `/cancel` не работал в FSM — перехватывался | `cmd_cancel` стоял после FSM-хендлеров в порядке регистрации → FSM-хендлер срабатывал первым | `cmd_cancel` перемещён в начало файла (первый хендлер роутера) |
| 12 | Нет UI для правил маршрутизации — только SQL | Правила можно было добавить только `INSERT INTO routing_rules` вручную | Добавлен UI: `/menu` → **🔀 Маршрутизация** (список + удаление) и **➕ Добавить** → **🔀 Правило** (FSM 4 шага: тип источника → паттерн → очередь → приоритет) |
| 13 | Альбом из N файлов → N длинных сообщений «Правило не найдено — назначь очередь через веб-интерфейс» | Каждое фото альбома = отдельный пост с длинным уведомлением | `app/bot/handlers/media.py`: уведомление сокращено до «📥 Пост #N → Входящие», добавлена пометка «(альбом)» при `media_group_id` |

Проверка:
```bash
pytest tests/unit -q   # 61 passed (30 в test_regression.py)
ruff check             # All checks passed!
# регрессионный тест: cmd_cancel зарегистрирован первым (test_cmd_cancel_registered_before_fsm_handlers)
```

### Раунд 5 — групповая отправка альбомов + кнопка «Удалить пост»

**Проблема (фидбек):** альбомы отправлялись в канал по одному файлу (каждое фото отдельным сообщением), без возможности групповой отправки. Дубликаты во Входящих нельзя было удалить кнопкой.

| # | Симптом | Причина | Фикс |
|---|---|---|---|
| 14 | Альбом из N файлов → N отдельных сообщений в канале | Не было настройки `group_albums` — все очереди отправляли по одному | (а) `app/db/schema.sql`: колонка `group_albums` в `queues` (0/1); (б) `app/db/database.py`: миграция `_run_migrations` добавляет колонку в существующие БД через `ALTER TABLE` (с проверкой `PRAGMA table_info`); (в) `app/scheduler/__init__.py`: `_send_album_group` — собирает посты с одинаковым `media_group_id` и отправляет через `bot.send_media_group`; (г) FSM создания очереди: шаг 4/4 «Группировать альбомы? да/нет» |
| 15 | Нельзя удалить дубликат из Входящих кнопкой | В карточке поста не было кнопки удаления | (а) `app/repositories/posts.py`: `delete_post(post_id)`; (б) `app/bot/keyboards/builder.py`: кнопка «🗑 Удалить пост» в `post_card`; (в) `app/bot/handlers/menu.py`: callback `menu:post:<id>:del` — удаляет пост и возвращается к списку (Входящие/очередь) |
| 16 | Альбомы не сохраняли `media_group_id` | В таблице `posts` не было поля | `app/db/schema.sql`: колонка `media_group_id TEXT` в `posts` + индекс `idx_posts_media_group`; миграция в `database.py`; `media.py` сохраняет `message.media_group_id`; `posts_repo.create_post` принимает `media_group_id` |

Проверка:
```bash
pytest tests/unit -q   # 61 passed (30 в test_regression.py)
ruff check             # All checks passed!
# новые тесты: test_create_post_with_media_group_id, test_find_album_posts_groups_by_media_group_id,
#              test_delete_post_removes_from_db, test_queue_group_albums_setting,
#              test_db_migration_adds_columns, test_post_card_has_delete_button
```
