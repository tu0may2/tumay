"""Доступ, роли и журнал изменений.

Терминал хранит финансовые данные и журнал сделок, поэтому на общем сервере
нужен вход и роли. Для запуска на одной машине проверка отключается настройкой
``TREASURY_AUTH_ENABLED=false`` — тогда всё работает как раньше.

Пароли хранятся как PBKDF2-хеш со случайной солью: библиотек для этого не
требуется, всё есть в стандартной библиотеке.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import AuditRecord, Session_, User

logger = logging.getLogger(__name__)

#: Роли по возрастанию прав
ROLES = ("viewer", "trader", "admin")
ROLE_TITLES = {
    "viewer": "Просмотр",
    "trader": "Сделки и лимиты",
    "admin": "Администратор",
}

_ITERATIONS = 240_000


def hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-хеш вида ``salt$hash``."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS
    )
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    # Сравнение постоянного времени: не даём подобрать хеш по задержке ответа
    return hmac.compare_digest(hash_password(password, salt), stored)


def create_user(
    session: Session, *, login: str, password: str, role: str = "viewer",
    full_name: str | None = None,
) -> User:
    if role not in ROLES:
        raise ValueError(f"Неизвестная роль: {role}")
    user = User(
        login=login.strip().lower(),
        full_name=full_name,
        password_hash=hash_password(password),
        role=role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def ensure_admin(session: Session) -> str | None:
    """Создать администратора при первом запуске.

    Пароль печатается в журнал ровно один раз — при создании; хранится он
    только в виде хеша, восстановить его нельзя.
    """
    if session.execute(select(User.id).limit(1)).first() is not None:
        return None

    password = settings.admin_password or secrets.token_urlsafe(12)
    create_user(
        session, login=settings.admin_login, password=password, role="admin",
        full_name="Администратор",
    )
    logger.warning(
        "Создан администратор %s с паролем %s — смените его после входа",
        settings.admin_login,
        password,
    )
    return password


def login(session: Session, login_name: str, password: str) -> dict[str, Any]:
    """Проверить пару логин-пароль и выдать токен сессии."""
    user = session.execute(
        select(User).where(User.login == login_name.strip().lower())
    ).scalar_one_or_none()

    if user is None or not user.active or not verify_password(password, user.password_hash):
        # Не уточняем, что именно неверно: это подсказка для подбора
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = secrets.token_urlsafe(32)
    session.add(
        Session_(
            token=token,
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(hours=settings.session_hours),
        )
    )
    user.last_login = datetime.utcnow()
    session.add(
        AuditRecord(user_login=user.login, action="login", entity="session")
    )
    session.commit()

    return {
        "token": token,
        "login": user.login,
        "full_name": user.full_name,
        "role": user.role,
        "expires_hours": settings.session_hours,
    }


def logout(session: Session, token: str) -> None:
    record = session.execute(
        select(Session_).where(Session_.token == token)
    ).scalar_one_or_none()
    if record is not None:
        session.delete(record)
        session.commit()


def drop_sessions(session: Session, user_id: int) -> int:
    """Закрыть все входы пользователя — после смены пароля старые недействительны."""
    records = list(
        session.execute(
            select(Session_).where(Session_.user_id == user_id)
        ).scalars()
    )
    for record in records:
        session.delete(record)
    session.commit()
    return len(records)


def current_user(
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
    session: Session = Depends(get_session),
) -> dict[str, Any] | None:
    """Пользователь текущего запроса.

    При отключённой проверке возвращаем условного администратора, чтобы
    остальной код не ветвился.
    """
    if not settings.auth_enabled:
        return {"login": "local", "role": "admin", "full_name": "Локальный запуск"}

    if not x_auth_token:
        return None

    record = session.execute(
        select(Session_).where(Session_.token == x_auth_token)
    ).scalar_one_or_none()
    if record is None:
        return None
    if record.expires_at < datetime.utcnow():
        session.delete(record)
        session.commit()
        return None

    user = session.get(User, record.user_id)
    if user is None or not user.active:
        return None
    return {"login": user.login, "role": user.role, "full_name": user.full_name}


def require_role(minimum: str):
    """Зависимость: требовать роль не ниже указанной."""
    threshold = ROLES.index(minimum)

    def _check(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Требуется вход в систему",
            )
        if ROLES.index(user["role"]) < threshold:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Недостаточно прав: нужна роль «{ROLE_TITLES[minimum]}»",
            )
        return user

    return _check


require_viewer = require_role("viewer")
require_trader = require_role("trader")
require_admin = require_role("admin")


def audit(
    session: Session,
    user: dict[str, Any] | None,
    *,
    action: str,
    entity: str,
    entity_id: str | int | None = None,
    detail: str | None = None,
) -> None:
    """Записать изменение в журнал. Ошибка записи не должна ронять операцию."""
    try:
        session.add(
            AuditRecord(
                user_login=(user or {}).get("login"),
                action=action,
                entity=entity,
                entity_id=str(entity_id) if entity_id is not None else None,
                detail=detail,
            )
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 — журнал не критичен для операции
        logger.warning("Не удалось записать в журнал изменений: %s", exc)
        session.rollback()
