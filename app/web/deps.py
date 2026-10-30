"""Auth для веб-интерфейса (ТЗ §7): HTTP Basic Auth с SECRET_TOKEN.

Если SECRET_TOKEN не задан — веб-интерфейс открыт (локальная разработка).
Логин любой, пароль = SECRET_TOKEN. Это проще для пользователя чем отдельный web_password.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config.settings import settings

security = HTTPBasic(auto_error=False)


async def verify_web_user(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    """Возвращает username при успехе. 401 при неверном пароле
    (или если token задан, но credentials нет).
    """
    if not settings.secret_token:
        return "dev"  # auth отключён
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )
    # Пароль должен совпадать с SECRET_TOKEN (constant-time). Логин любой.
    ok = secrets.compare_digest(credentials.password, settings.secret_token)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
