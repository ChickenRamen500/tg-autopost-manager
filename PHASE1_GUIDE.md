# 🚀 Фаза 1 — Фундамент: Local Bot API Server + миграция (ТЗ §13)

## Шаг 0. Получите api_id / api_hash (п.1 ТЗ §13, §3.3)
1. Зайдите на https://my.telegram.org под своим аккаунтом;
2. «API development tools» → создать приложение → скопируйте `api_id` и `api_hash`.

## Шаг 1. Заполните .env
```bash
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
```
Заполните: `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `SECRET_TOKEN`,
`TEST_CHAT_ID` (для теста 300 МБ; узнать свой chat id: напишите боту @userinfobot).

Проверка конфигурации:
```bash
python scripts/check_phase1.py --config
```

## Шаг 2. Поднимите local-bot-api (п.2)
⚠️ Для Windows путь `D:\Media:/app/media` в docker-compose требует Docker Desktop
с включённым File Sharing для диска D. Если диск другой — поправьте volume.

```bash
docker compose up -d local-bot-api
docker compose logs local-bot-api          # сервер слушает :8081
```
Ключевой флаг `--local` уже в compose-команде: upload до 2 ГБ, download без лимита,
`getFile` возвращает локальный путь. Мониторьте рост volume `bot-api-data` (кеш файлов).

## Шаг 3. Миграция бота с облачного API (п.3, разовая!)
```bash
python scripts/check_phase1.py --logout
# или вручную:
curl https://api.telegram.org/bot<TOKEN>/logOut   # {"ok":true,"result":true}
```
Без logOut локальный сервер вернёт 401/409. Старые file_id остаются валидными.

## Шаг 4. Проверка (п.4)
```bash
# healthcheck: getMe через localhost:8081
python scripts/check_phase1.py --health
curl http://localhost:8081/bot<TOKEN>/getMe

# главный тест: отправка файла 300 МБ + getFile обратно (облако так не умеет)
python scripts/check_phase1.py --big-test
python scripts/check_phase1.py --big-test --size 500   # можно и больше, до 2000 МБ
```
В чате должна появиться документ-файл ~300 МБ и сообщение
«✅ Фаза 1: upload>50МБ и getFile>20МБ прошли». В логах — абсолютный `file_path` из getFile.

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
