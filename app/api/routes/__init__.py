"""REST API routes (Фаза 4 — Chrome-расширение, ТЗ §6, §9).

На Фазе 2 все endpoints (/health, /health/bot-api, /health/scheduler) написаны
прямо в app/main.py без версионирования.

План на Фазу 4 — вынести в APIRouter с префиксом /v1:
  POST /v1/posts            — приём медиа из Chrome-расширения (ТЗ §6.1)
  GET  /v1/posts/{id}       — статус поста
  GET  /v1/queues           — список очередей (для UI выбора)
  POST /v1/queues/{id}/posts — постановка поста в очередь
  GET  /v1/settings         — настройки пользователя (timezone, thresholds)
  WS   /v1/events           — stream событий (пост отправлен / failed)

Авторизация: X-Secret-Token (как в /health*) — extesion хранит токен после OAuth.

Пока файл пустой — реализация в Фазе 4.
"""
