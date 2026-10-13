# 🚀 Инструкция: запуск всего с нуля на новой машине

> Простая пошаговая инструкция для развёртывания **TG Media Queue Bot** «с чистого листа».
> Подробные гайды по фазам: [`PHASE1_GUIDE.md`](./PHASE1_GUIDE.md) и [`PHASE2_GUIDE.md`](./PHASE2_GUIDE.md).

## Шаг 0. Установите необходимое ПО

На новой машине нужны только:
- **Git** — https://git-scm.com/downloads
- **Docker Desktop** (Windows/macOS) или **Docker Engine + Docker Compose** (Linux) — https://www.docker.com/get-started

Проверка установки:
```bash
docker --version
docker compose version
```

⚠️ **Windows**: не забудьте расшарить диск с медиа в Docker Desktop → Settings → Resources → File Sharing (в compose смонтирован `E:\Media:/app/media`; если у вас другой диск — поправьте путь в `docker-compose.yml`).

⚠️ Python локально ставить **не нужно** — всё работает внутри контейнеров.

## Шаг 1. Клонируйте репозиторий

```bash
git clone https://github.com/ChickenRamen500/tg-autopost-manager.git
cd tg-autopost-manager
```

## Шаг 2. Получите креденшалы Telegram

1. **BOT_TOKEN** — создайте бота у [@BotFather](https://t.me/BotFather) (`/newbot`).
2. **TELEGRAM_API_ID / TELEGRAM_API_HASH** — на https://my.telegram.org → «API development tools».
   Если форма отвечает голым `ERROR` — см. [`my_telegram_org_troubleshooting.md`](./my_telegram_org_troubleshooting.md).
3. **SECRET_TOKEN** — придумайте случайную длинную строку (защита REST API и Chrome-расширения), например:
   ```bash
   openssl rand -hex 32        # PowerShell: -join ((48..57)+(97..122) | Get-Random -Count 64 | % {[char]$_})
   ```
4. **ADMIN_TG_ID** и **TEST_CHAT_ID** — ваш числовой tg_id, узнайте у [@userinfobot](https://t.me/userinfobot).

## Шаг 3. Создайте `.env`

```bash
cp .env.example .env            # PowerShell: Copy-Item .env.example .env
```

Заполните в `.env` как минимум:
```
BOT_TOKEN=...
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
SECRET_TOKEN=...
ADMIN_TG_ID=...
TEST_CHAT_ID=...
```
Остальные поля можно оставить значениями по умолчанию.

## Шаг 4. Поднимите Local Bot API Server

```bash
docker compose up -d local-bot-api
docker logs -f tg-lba           # Ctrl+C когда лог перестанет расти
```
Сервер слушает порт `8081`. Healthcheck может висеть в статусе `starting` до 90 секунд (первичная синхронизация датацентров) — это нормально.

## Шаг 5. Разлогиньте бота от облачного API (разово!)

Без этого локальный сервер вернёт 401/409:
```bash
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/logOut"
# ожидаемый ответ: {"ok":true,"result":true}
```
Старые `file_id` останутся валидными.

## Шаг 6. Соберите и запустите бота

```bash
docker compose up -d --build bot
```

При первом старте приложение само:
- создаст базы `data/bot.db` (9 таблиц) и `data/tags.db`;
- назначит вас admin'ом из `ADMIN_TG_ID`;
- запустит планировщик APScheduler и polling через Local Bot API.

## Шаг 7. Проверьте, что всё работает

```bash
# Фаза 1: конфиг, здоровье local-bot-api, тест загрузки 300 МБ
docker compose exec bot python scripts/check_phase1.py --config
docker compose exec bot python scripts/check_phase1.py --health
docker compose exec bot python scripts/check_phase1.py --upload

# Фаза 2: БД, RBAC, маршрутизация, очередь
docker compose exec bot python scripts/check_phase2.py --db
```

Все проверки должны вернуть `[OK]`.

## Шаг 8. Доступ к интерфейсам

- **Веб-интерфейс**: http://localhost:8000 (или `http://<IP-машины>:8000` из локальной сети) — HTTP Basic Auth.
- **Telegram-бот**: откройте диалог с ботом и отправьте `/start` (первый пользователь с `ADMIN_TG_ID` станет admin).

⚠ **Важно при переносе на новую машину**: пока старый хост запущен и делает polling,
Telegram отдаёт обновления только одному клиенту — на новой машине бот будет молчать.
Остановите стек на старом хосте (`docker compose down`) перед запуском на новом.
Перед поллингом бот теперь сам проверяет и снимает «висячий» webhook (`getWebhookInfo` → `deleteWebhook`).

### Быстрая диагностика (PowerShell — используйте `curl.exe`, а не `curl`!)

```powershell
# 1. Жив ли local-bot-api (ответ должен прийти за <1 c):
curl.exe -s "http://localhost:8081/bot$((Get-Content .env | Select-String '^BOT_TOKEN=' ) -replace 'BOT_TOKEN=','')/getMe"
# или проще — внутри контейнера бота:
docker compose exec bot python -c "import asyncio; from app.utils.local_bot_api import get_me; print(asyncio.run(get_me()))"

# 2. Есть ли незабранные обновления (если result[] непустой, а бот молчит — был webhook):
docker compose exec bot python -c "import os,urllib.request,json;t=os.environ['BOT_TOKEN'];print(json.load(urllib.request.urlopen('http://local-bot-api:8081/bot'+t+'/getUpdates')))"

# 3. Снятие «висячего» webhook вручную (если не хотите пересобирать образ):
docker compose exec bot python -c "import os,urllib.request;t=os.environ['BOT_TOKEN'];print(urllib.request.urlopen('http://local-bot-api:8081/bot'+t+'/deleteWebhook').read())"

# 4. Перезапуск с новым кодом (снимет webhook автоматически):
docker compose up -d --build bot
docker compose logs --since 2m bot | Select-String "polling|webhook|getMe"
```

## Полезные команды

| Команда | Описание |
|---|---|
| `docker compose ps` | статус контейнеров |
| `docker compose logs -f bot` | логи бота |
| `docker compose restart bot` | перезапуск бота после правок `.env` |
| `docker compose down` | остановить всё (данные в `./data` сохранятся) |
| `docker compose down -v` | остановить **и удалить volume** `bot-api-data` (кеш файлов local-bot-api) |

## Частые проблемы

1. **`401 Unauthorized` от local-bot-api** → вы не выполнили `logOut` (Шаг 5). Выполните и перезапустите контейнеры.
2. **Бот не отвечает на `/start` (особенно после переноса на новую машину)** → самая частая причина: старый хост всё ещё запущен и «забирает» все обновления себе (Telegram отдаёт getUpdates только одному клиенту) — остановите стек на старой машине (`docker compose down`). Вторая причина: «висячий» webhook, зарегистрированный ранее — свежие версии бота снимают его автоматически при старте; вручную: `docker compose exec bot python -c "import os,urllib.request;t=os.environ['BOT_TOKEN'];print(urllib.request.urlopen('http://local-bot-api:8081/bot'+t+'/deleteWebhook').read())"`. Также проверьте `docker compose logs bot` (строки polling/webhook), `BOT_TOKEN` и `BOT_API_URL=http://local-bot-api:8081`.
3. **`check_phase1.py --health` зависает** → local-bot-api ещё синхронизирует датацентры (первые 1-2 минуты после первого запуска — норма) или недоступен из контейнера бота. Проверьте `docker logs tg-lba` и `docker compose exec bot python scripts/check_phase1.py --config`. Флаг `--upload` в скрипте называется `--big-test`.
4. **Нет доступа к веб-интерфейсу** → проверьте, что контейнер `bot` запущен и порт 8000 не занят (`docker compose ps`, `ss -ltn` / `netstat -ano`).
5. **Ошибка монтирования `E:\Media` (Windows)** → расшарьте диск в Docker Desktop или измените путь volume в `docker-compose.yml`.
6. **Форма my.telegram.org отвечает `ERROR`** → см. [`my_telegram_org_troubleshooting.md`](./my_telegram_org_troubleshooting.md).

---
*Инструкция актуальна для ТЗ v5. Полные детали архитектуры — в [`ТЗ_TechnicalDoc_v5_2026-10-11.docx`](./ТЗ_TechnicalDoc_v5_2026-10-11.docx).*
