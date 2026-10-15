# Кастомный образ Local Bot API Server с добавленным curl для healthcheck.
#
# Базовый образ aiogram/telegram-bot-api:latest основан на alpine и содержит
# только openssl и libstdc++ — НЕТ curl/wget-полноценного (только busybox wget с
# ограниченным функционалом). Из-за этого healthcheck через `curl -fs ...` падал
# с `command not found` (exit 127) → контейнер помечался unhealthy, хотя сам
# telegram-bot-api работал корректно.
#
# Решение: добавляем curl (alpine package ~2 МБ) поверх официального образа.
# Альтернатива — переписать healthcheck на busybox wget, но у него ограниченные
# exit codes (всё 1 = и connection refused, и HTTP 4xx) — не различить живой сервер
# от мёртвого. curl даёт точные exit codes (22 = HTTP error, 7 = connection refused).
FROM aiogram/telegram-bot-api:latest

RUN apk add --no-cache curl
