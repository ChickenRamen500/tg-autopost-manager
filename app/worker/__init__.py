"""Worker для автопарсинга подписок (Фаза 3, ТЗ §2.5 / §10).

На Фазе 2 autopostring живёт в app.scheduler (APScheduler внутри uvicorn-процесса):
  - send_post_from_queue — отправка следующего pending-поста по интервалу.

На Фазе 3 сюда переедет отдельный процесс для автопарсинга теговых подписок
(tag_subscriptions в БД). Worker будет:
  1. Опрашивать подписки, у которых наступило check_at (ТЗ §2.5.3).
  2. Дёргать GelbooruClient.search_posts через task_queue.
  3. Скачивать новые посты, создавать posts с status='new', ставить task_type='download'.

Пока этот модуль пустой — реализация в будущей Фазе 3.
"""
