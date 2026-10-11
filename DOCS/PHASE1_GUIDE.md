# 🚀 Фаза 1 — Фундамент: Local Bot API Server + миграция (ТЗ §13)

> ⚠️ **Windows:** команда `python` в PowerShell часто ссылается на алиас Microsoft Store
> и молча ничего не делает. Все скрипты проверки запускайте **внутри контейнера bot**:
> `docker compose exec bot python scripts/check_phase1.py ...`
> (либо используйте `py` / `python3`, если Python установлен в PATH).

## Шаг 0. Получите api_id / api_hash (п.1 ТЗ §13, §3.3)
1. Зайдите на https://my.telegram.org под своим аккаунтом;
2. «API development tools» → создать приложение → скопируйте `api_id` и `api_hash`.
   Если форма отвечает голым `ERROR`: IP должен быть из страны аккаунта (смените VPN),
   App title — UpperCamelCase без дефисов (`TgAutopostManagerMeme`), Short name = title,
   платформа Web, URL пустой. Подробности: `DOCS/my_telegram_org_troubleshooting.md`.

## Шаг 1. Заполните .env
```bash
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
```
Заполните: `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `SECRET_TOKEN`,
`TEST_CHAT_ID` (для теста 300 МБ; узнать свой chat id: напишите боту @userinfobot).

Проверка конфигурации:
```bash
docker compose exec bot python scripts/check_phase1.py --config   # или локально: py scripts\...
```

## Шаг 2. Поднимите local-bot-api (п.2)
⚠️ Для Windows путь `D:\Media:/app/media` в docker-compose требует Docker Desktop
с включённым File Sharing для диска D. Если диск другой — поправьте volume.

```bash
docker compose up -d local-bot-api
docker logs -f tg-lba                      # контейнер назван tg-lba (compose: container_name)
# Сервер слушает :8081; healthcheck может висеть "starting" до 90s (первичная синхронизация DC)
```
Ключевой флаг `--local` уже в compose-команде: upload до 2 ГБ, download без лимита,
`getFile` возвращает локальный путь. Мониторьте рост volume `bot-api-data` (кеш файлов).

## Шаг 3. Миграция бота с облачного API (п.3, разовая!)
```bash
docker compose exec bot python scripts/check_phase1.py --logout
# или вручную:
curl https://api.telegram.org/bot<TOKEN>/logOut   # {"ok":true,"result":true}
```
Без logOut локальный сервер вернёт 401/409. Старые file_id остаются валидными.

## Шаг 4. Проверка (п.4)
```bash
# healthcheck: getMe через localhost:8081
docker compose exec bot python scripts/check_phase1.py --health
curl.exe http://localhost:8081/bot<TOKEN>/getMe

# главный тест: отправка файла 300 МБ + getFile обратно (облако так не умеет)
docker compose exec bot python scripts/check_phase1.py --big-test
docker compose exec bot python scripts/check_phase1.py --big-test --size 500  # до 2000 МБ
```
В чате должен появиться документ-файл ~300 МБ и сообщение
«✅ Фаза 1: upload>50МБ и getFile>20МБ прошли». В логах — абсолютный `file_path` из getFile.

⚠️ Файлы >20 МБ **нельзя** скачать по облачной ссылке `api.telegram.org/file/bot...` —
она вернёт `Bad Request: file is too big`. Это ожидаемо и означает, что всё работает:
при `--local` файл уже лежит на диске сервера, а `getFile` возвращает путь внутри
контейнера `tg-lba`. Скрипт читает его напрямую через read-only монтирование
volume `bot-api-data:/app/bot-api-data` в контейнере `bot` (см. docker-compose.yml).

## Критерии приёмки Фазы 1
- [x] `getMe` через `http://localhost:8081` → `ok:true`
- [x] `send_document` 300 МБ проходит (в облаке — ошибка file_too_big на 50 МБ)
- [x] `getFile` для файла >20 МБ возвращает `file_path` (локальный абсолютный путь)
- [x] `restart: always` — контейнеры перезапускаются автоматически (§3.4)

## Что дальше (Фаза 2 — Ядро)
`docker compose up -d bot` поднимет контейнер бота (FastAPI `/health`, `/health/bot-api`).
Далее: схема БД (§5), приём медиа из ЛС с сохранением file_id, APScheduler-публикация.

Полезно: `pip install -r requirements.txt` (на хосте для скриптов проверки);
юнит-тесты: `pytest tests/unit/test_settings.py`.
