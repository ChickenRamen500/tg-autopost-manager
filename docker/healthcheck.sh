#!/bin/sh
# Healthcheck без утечки SECRET_TOKEN в `ps`/`docker inspect`/cmdline.
# Заголовок X-Secret-Token подставляется curl из окружения контейнера,
# а не появляется как аргумент командной строки.
# Exit 0 = healthy, exit 1 = unhealthy.
curl -fs -H "X-Secret-Token: ${SECRET_TOKEN}" http://localhost:8000/health || exit 1
