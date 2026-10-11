# Pull Request: Фаза 1 — Фундамент (Local Bot API Server + каркас проекта)

**Branch:** `feature/phase1-foundation` → **Base:** `main`
**Commits:** `b71fe04`, `ea7c215`, `35b7d71`
**Соответствие ТЗ:** `/DOCS/ТЗ_TechnicalDoc_v5_2026-10-11.docx`, §13 «План реализации», Фаза 1 (+§3.1–3.3, §4.1)

---

## 📋 Описание

Первая фаза проекта **TG Media Queue Bot**: поднят фундамент — Local Bot API Server (`--local`, лимиты до 2 ГБ), конфигурация, подключение aiogram 3.x через локальный сервер, механизм миграции с облачного API (`logOut`) и скрипт проверки всех четырёх шагов Фазы 1. Также зафиксирована базовая структура проекта под все 6 фаз реализации.

## ✅ Что сделано

### 1. Базовая структура проекта (`b71fe04`)
- Каркас `app/` (bot, db, services, api, web, scheduler, worker) согласно §4;
- `docker-compose.yml` по §3.2: bot (лимит RAM 2G) + local-bot-api (`tdlib/telegram-bot-api:latest --local`, порт 8081, volume `D:\Media:/app/media`);
- `.env.example`, `requirements.txt`, `pyproject.toml`, `.gitignore`;
- `PROJECT_STRUCTURE.md` — привязка папок к разделам ТЗ и фазам 1–6.

### 2. Фаза 1 «Фундамент» (`ea7c215`)
| Шаг Фазы 1 (ТЗ) | Реализация |
|---|---|
| п.1 Поднять Local Bot API Server | `docker/local-bot-api.md` + healthcheck в compose |
| п.1 Конфигурация | `app/config/settings.py` — dataclass `Settings` из `.env` + `validate()` |
| п.2 Dockerfile для бота | `docker/Dockerfile` (python:3.11-slim + ffmpeg) |
| п.3 Подключить aiogram к `BOT_API_URL` | `app/utils/local_bot_api.py::create_bot()` — `AiohttpSession(TelegramAPIServer.from_base(...))` |
| п.3 Миграция со старого сервера | `logout_from_cloud()` — разовый `logOut` |
| п.4 Healthcheck | `get_me()`, эндпоинты `/health` и `/health/bot-api` в `app/main.py` |
| п.4 Тест big upload/download | `test_big_upload_download()` (300 МБ → send_document → getFile) |
| п.4 CLI-проверка | `scripts/check_phase1.py --config/--logout/--health/--big-test` |
| Гайд по запуску | `PHASE1_GUIDE.md` |

### 3. CI-стабилизация (`35b7d71`)
- `tests/conftest.py` — изоляция от сторонних pytest-плагинов.

## 🧪 Тесты
```
$ pytest tests/unit -q
4 passed ✅   (валидация Settings, константы лимитов 50/20/2000 МБ по ТЗ, генерация тестового файла)
$ docker compose config   # валиден
```
Проверки Фазы 1 на боевой машине (нужен Docker + реальный токен) описаны в `PHASE1_GUIDE.md`:
`check_phase1.py --logout → --health → --big-test`.

## 🔑 Ключевые файлы
- `app/config/settings.py` — вся конфигурация (§3.1)
- `app/utils/local_bot_api.py` — ядро взаимодействия с Local Bot API (§3.3)
- `scripts/check_phase1.py` — чек-лист приёмки Фазы 1
- `docker-compose.yml`, `docker/Dockerfile`, `docker/local-bot-api.md`
- `PHASE1_GUIDE.md`, `PROJECT_STRUCTURE.md`

## ⚠️ Заметки для ревьюера
- После `logOut` старые `file_id` остаются валидны на локальном сервере (учтено в архитектуре §5).
- Volume `D:\Media` требует File Sharing в Docker Desktop (Windows) — см. гайд.
- Секреты не коммитятся: только `.env.example` (.gitignore исключает `.env`).
- Ветки `main` и история содержат только этот стек изменений; конфликтов с `main` нет (fast-forward).

## 📌 Дальше
Фаза 2 (Ядро): схема БД §5, приём медиа из ЛС с сохранением file_id, маршрутизация, APScheduler.
