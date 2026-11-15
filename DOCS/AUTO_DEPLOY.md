# 🚀 Автодеплой и операционные уведомления (вне ТЗ v5)

> Этот документ описывает функциональность, которая **не была заложена в ТЗ
> `ТЗ_TechnicalDoc_v5_2026-10-11.docx`**, но появилась в проекте по мере развития:
> CI/CD автодеплой (GitHub Actions → webhook → watcher на хосте) и связанные
> с ним уведомления админа. Всё остальное ТЗ покрывает в §3, §6, §7.

## 1. Архитектура автодеплоя

```
git push origin main
        │
        ▼
GitHub Actions  .github/workflows/auto-deploy.yml
  • подключается к Tailscale (secrets.TS_AUTHKEY)
  • POST http://DEPLOY_HOST:DEPLOY_PORT/webhook/deploy
    заголовок X-Deploy-Token: DEPLOY_TOKEN
        │
        ▼
app/main.py  POST /webhook/deploy  (в контейнере bot)
  • сверяет X-Deploy-Token с DEPLOY_TOKEN (compare_digest);
    если DEPLOY_TOKEN пуст → 404 (автодеплой отключён)
  • пишет файл-триггер data/deploy.trigger:
        <commit SHA payload.after>
        <ref payload.ref>
        │   (data/ — volume, виден хосту)
        ▼
Хост (Windows): scripts/watch_deploy.ps1  — watcher
  • каждые 3 сек проверяет data/deploy.trigger
  • нашёл → удаляет триггер, git fetch + reset --hard origin/main,
    docker compose down && up --build -d, логирует в data/deploy.log
  • регистрация автозапуска: powershell -File watch_deploy.ps1 -Register
    (Scheduled Task "TgAutoDeployWatcher")
        │
        ▼
Контейнер bot пересобран → FastAPI lifespan стартует заново
  └─► app/services/deploy_notify.py:
      • «🚀 Бот перезапущен после автодеплоя» (см. §2)
      • фоновый наблюдатель ERROR из лога контейнера (см. §3)
```

Аналог для Linux-хоста: `scripts/auto_deploy.sh` (тот же цикл git+compose,
вызывается вручную или из systemd timer; webhook-вариант «webhook запускает
скрипт внутри контейнера» НЕ используется — в образе нет git/docker).

### Секреты GitHub (Settings → Secrets and variables → Actions)

| Secret | Значение |
|---|---|
| `TS_AUTHKEY` | authkey Tailscale для раннера Actions |
| `DEPLOY_HOST` | IP хоста в сети Tailscale |
| `DEPLOY_PORT` | проброшенный порт бота (8000) |
| `DEPLOY_TOKEN` | тот же токен, что `DEPLOY_TOKEN` в `.env` (`openssl rand -hex 32`) |

## 2. Уведомление «бот перезапущен после автодеплоя»

Реализация: `app/services/deploy_notify.py::notify_deploy_restart()`,
вызывается из lifespan (`app/main.py`, блок 8) через 5 сек после старта polling.

Логика:
1. Если `data/deploy.trigger` существует и создан **не старше**
   `DEPLOY_RESTART_WINDOW_SECONDS` (по умолчанию 900 сек = запас на git pull +
   docker build) — старт классифицируется как перезапуск после автодеплоя.
2. Отправляется сообщение «🚀 Бот перезапущен после автодеплоя» с short SHA
   коммита, ref и возрастом триггера — через `notifications.notify_admin()`
   (ЛС админа `ADMIN_TG_ID` + дубль в служебный канал, если задан).
3. Анти-спам: SHA записывается в `data/deploy.notified` (история ~20 коммитов);
   повторный старт с тем же коммитом не уведомляет (защита от case «watcher
   умер, trigger пережил рестарт, старый код снова его видит»).
4. Любой сбой (нет прав на файлы, упавшая отправка) только логируется —
   старт приложения никогда не прерывается из-за уведомления.

Настройки (`.env`, см. `.env.example`):

| Переменная | Default | Назначение |
|---|---|---|
| `DEPLOY_TOKEN` | пусто (отключено) | общий секрет webhook'а GitHub Actions ↔ бот |
| `DEPLOY_RESTART_WINDOW_SECONDS` | 900 | окно, в котором старт считается «после деплоя» |

## 3. Уведомления об ошибках из лога контейнера

Реализация: `app/services/deploy_notify.py::start_log_error_watcher()` —
фоновая asyncio-задача, стартует в lifespan, останавливается при shutdown.

Как это работает:
1. Источник — **собственный stdout контейнера**. В Docker он доступен процессу
   через `/proc/1/log` (FIFO, куда entrypoint образа python направляет stdout;
   читается даже от непривилегированного `appuser`). Fallback `/dev/stdout` —
   для запуска на хосте с перенаправлением в файл. Ни одного пути нет →
   наблюдатель молча отключается (логируется INFO).
2. Каждые `LOG_ERRORS_SCAN_INTERVAL_SECONDS` (default 120) сканер читает новые
   байты с сохранённого offset и выбирает строки с `[ERROR]` / `[CRITICAL]`
   (формат логгера `main.py`) и заголовки traceback'ов.
3. Найденные строки отправляются **одним дайджестом** «⚠️ Ошибки в логе
   контейнера» в ЛС админу (+служебный канал) — максимум
   `LOG_ERRORS_MAX_LINES_PER_REPORT` строк (default 10), каждая усекается до
   300 символов, всё сообщение — до лимита Telegram.
4. Обработка ротации json-file логов Docker (`max-size: 10m`): если файл стал
   меньше offset — читаем заново с нуля. Неполная последняя строка дочитывается
   на следующем цикле без дублей.
5. Ошибка одна — сообщение одно; ошибки массовые группируются в дайджест
   (интервал скана = естественный anti-spam throttle).

| Переменная | Default | Назначение |
|---|---|---|
| `LOG_ERRORS_SCAN_INTERVAL_SECONDS` | 120 | период скана; 0 — полностью отключить |
| `LOG_ERRORS_MAX_LINES_PER_REPORT` | 10 | макс. строк в одном сообщении |

> Примечание: `docker logs tg-bot` остаётся первоисточником — дайджест содержит
> ссылки на него при обрезке («…и ещё N строк (см. docker logs tg-bot)»).

## 4. Healthcheck и наблюдаемость (сопутствующее, тоже вне ТЗ)

- `docker/Dockerfile`: `HEALTHCHECK` → `docker/healthcheck.sh` (curl `/health`,
  токен берётся из env, не светится в cmdline).
- Эндпоинты: `/health` (без auth), `/health/bot-api`, `/health/scheduler`,
  `/health/worker` (за `X-Secret-Token`).
- Watcher пишет собственный лог `data/deploy.log` (не путать с логом контейнера).

## 5. Тесты

`tests/unit/test_deploy_notify.py` — 12 тестов: детект свежего/устаревшего
trigger, dedup по commit, отсутствие trigger, устойчивость к падению отправки,
парсинг trigger-файла, матчинг ERROR-строк, инкрементальный скан с offset,
ротация/усечение файла, неполная последняя строка.
