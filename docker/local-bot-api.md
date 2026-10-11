# Local Bot API Server — заметки по эксплуатации (ТЗ §3.3)

Запускается образом `aiogram/telegram-bot-api` из `docker-compose.yml`,
дополнительных конфигов не требует — все параметры передаются флагами:

- `--api-id/--api-hash` — из `.env` (получить на https://my.telegram.org → «API development tools»);
- `--local` — **ключевой флаг**: upload до 2000 МБ, download без лимита,
  getFile возвращает абсолютный локальный `file_path`;
- `--dir=/var/lib/telegram-bot-api` — кеш скачанных файлов в volume `bot-api-data`
  (мониторить рост диска!).

## Разовая миграция бота (обязательно перед первым запуском через localhost:8081)

```bash
curl https://api.telegram.org/bot<TOKEN>/logOut
# ожидаемый ответ: {"ok":true,"result":true}
```

Без logOut локальный сервер вернёт 401/409. Старые file_id остаются валидными.

## Проверка связности

```bash
curl http://localhost:8081/bot<TOKEN>/getMe
python scripts/check_phase1.py --health
python scripts/check_phase1.py --big-test   # тест 300 МБ из п.4 Фазы 1
```
