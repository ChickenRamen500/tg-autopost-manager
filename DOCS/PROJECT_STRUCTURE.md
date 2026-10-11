# 📂 Структура проекта TG Media Queue Bot

Базовая структура папок, составлена на основе ТЗ `DOCS/ТЗ_TechnicalDoc_v5_2026-10-11.docx`
(разделы 3–4 «Инфраструктура/Архитектура», 6 «REST API», 7 «Веб-интерфейс»,
8 «Telegram-интерфейс», 9 «Chrome-расширение», 12 «Технологический стек»).

```
tg-media-queue-bot/
├── app/                        # Backend (Python 3.11+, FastAPI + aiogram 3.x)
│   ├── main.py                 # Точка входа: запуск FastAPI + бота (заглушка)
│   ├── config/                 # Настройки из .env (BOT_TOKEN, SECRET_TOKEN, BOT_API_URL и т.д.)
│   ├── bot/                    # Telegram-интерфейс (ТЗ §8)
│   │   ├── handlers/           # Хендлеры ЛС: приём медиа, модерация, подписки Gelbooru/Danbooru
│   │   └── keyboards/          # Inline-кнопки модерации и управления
│   ├── db/                     # SQLite (aiosqlite), схема из ТЗ §5
│   │   └── models/             # users, channels, queues, posts, signatures,
│   │                           # routing_rules, duplicate_hashes, task_queue
│   ├── repositories/           # Слой доступа к данным (CRUD-запросы к моделям)
│   ├── services/               # Бизнес-логика (ТЗ §2)
│   │   ├── routing/            # Правила маршрутизации (regex/точное совпадение → очередь)
│   │   ├── caption/            # Конвейер обработки подписей (фильтры, случайные фразы)
│   │   ├── dedup/              # Двухуровневые дубликаты: file_unique_id/source_id + pHash
│   │   ├── media/              # ffmpeg-кроп видео (-preset ultrafast), Pillow/imagehash
│   │   └── parsers/            # Источники контента (ТЗ §2.1)
│   │       ├── gelbooru/       # API + Referer/UA, реюз GelbooruClient из gbooru_tg
│   │       ├── danbooru/
│   │       ├── xcom/           # yt-dlp + cookies
│   │       ├── pikabu/
│   │       └── pixiv/          # опционально pixivy
│   ├── api/                    # REST API (ТЗ §6): quick_add, posts, queues, files, duplicates
│   │   └── routes/
│   ├── web/                    # Веб-интерфейс (ТЗ §7): Jinja2 + Bootstrap 5 + vanilla JS
│   │   ├── templates/pages/    # dashboard, queue, local_files, duplicates, routing, users
│   │   └── static/{css,js,img}/
│   ├── scheduler/              # APScheduler: публикация постов из очередей по расписанию (ТЗ §2.5)
│   ├── worker/                 # Обработчик task_queue: new → downloading → pending (ТЗ §4.2.2)
│   └── utils/                  # Вспомогательные функции (log, file_id-стратегия и пр.)
├── chrome-extension/           # Расширение Manifest V3 (ТЗ §9)
│   └── icons/                  # manifest.json, content.js, background.js,
│                               # site-config.js, options.*, popup.* — добавляются на Фазе 4
├── data/                       # Runtime-данные (в git не попадают, volume в Docker)
│   ├── db/                     # bot.db (sqlite:///./data/bot.db)
│   ├── downloads/              # Временная загрузка; автоочистка каждые 24 ч (ТЗ §3.1)
│   └── media/                  # Хранилище медиа (/app/media, дополнительный диск)
├── docker/                     # Dockerfile бота, конфиги local-bot-api (aiogram/telegram-bot-api --local)
├── scripts/                    # Служебные скрипты (миграции, инициализация БД, очистка)
├── tests/
│   ├── unit/
│   └── integration/
├── DOCS/                       # Техническая документация:
│   ├── ТЗ_TechnicalDoc_v5_2026-10-11.docx   # исходное ТЗ
│   ├── PROJECT_STRUCTURE.md    # этот файл
│   ├── PHASE1_GUIDE.md         # пошаговый гайд Фазы 1
│   ├── my_telegram_org_troubleshooting.md  # решение ERROR на my.telegram.org
│   └── PULL_REQUEST.md         # описание PR Фазы 1 (историческое)
├── .env.example                # Шаблон переменных окружения
├── docker-compose.yml          # Сервисы bot + local-bot-api, лимиты памяти (ТЗ §3.2)
├── requirements.txt            # fastapi, aiogram, aiosqlite, apscheduler, yt-dlp, Pillow, imagehash...
├── pyproject.toml
└── README.md
```

## Соответствие фазам реализации (ТЗ §13)

| Фаза | Статус | Что заполняется |
|------|--------|-----------------|
| 1. Фундамент | ✅ готово | `docker/`, `docker-compose.yml`, `.env.example`, `app/config/`, `app/utils/local_bot_api.py`, `scripts/check_phase1.py` |
| 2. Ядро | ✅ готово | `app/db/{schema.sql,database.py}`, `app/repositories/*`, `app/bot/{middleware,handlers}/*`, `app/scheduler/`, `app/services/routing/`, `app/services/parsers/gelbooru/` (миграция gbooru_tg), `scripts/{init_db,check_phase2,populate_tags}.py`, `tests/unit/{test_routing,test_repositories,test_scheduler}.py` |
| 3. Worker + источники | ⬜ план | `app/worker/` (task_queue готов в репозитории), доп. парсеры `app/services/parsers/{xcom,pikabu,pixiv,danbooru}/`, `app/services/dedup/` (репозиторий `duplicates.py` готов) |
| 4. API + расширение | ⬜ план | `app/api/routes/`, `chrome-extension/` |
| 5. Обработка + веб | ⬜ план | `app/services/caption/`, `app/services/media/` (кроп ffmpeg), `app/web/` (Jinja2+Bootstrap) |
| 6. Полировка | ⬜ план | `scripts/` (автоочистка downloads), доп. `tests/`, retry/уведомления |

> **Фаза 2 реализована** — см. `DOCS/PHASE2_GUIDE.md`. 22 юнит-теста проходят, ruff: 0 ошибок.
> Готовая инфраструктура для Фазы 3: персистентная `task_queue` (`take_next` через UPDATE…RETURNING), репозиторий дубликатов с расстоянием Хэмминга для pHash, `subscriptions.list_due_subscriptions()` для автопарсинга.
