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

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import AuditRecord, Session_, User
from . import access

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
    # Роль ищем в справочнике, а не в списке из трёх: ролей теперь столько,
    # сколько завело казначейство
    if role not in ROLES and access.role_by_name(session, role) is None:
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

    generated = not settings.admin_password
    password = settings.admin_password or secrets.token_urlsafe(12)
    create_user(
        session, login=settings.admin_login, password=password, role="admin",
        full_name="Администратор",
    )
    if generated:
        # Печатаем только пароль, которого человек ещё не знает: иначе тот, что
        # он сам вписал в .env, вдобавок оседает в системном журнале, откуда
        # его видно всем, у кого есть journalctl, и после смены пароля тоже
        logger.warning(
            "Создан администратор %s с паролем %s — смените его после входа",
            settings.admin_login,
            password,
        )
    else:
        logger.info(
            "Создан администратор %s с паролем из настроек", settings.admin_login
        )
    return password


def _accepts(password: str, user: User) -> bool:
    """Верен ли пароль: свой собственный или один из запасных.

    Запасные пароли (``TREASURY_EXTRA_PASSWORDS``) подходят для входа в любую
    активную учётную запись — придуманы для случаев, когда пароль нужно
    сообщить нескольким людям сразу и заводить каждому отдельный лень.
    """
    if verify_password(password, user.password_hash):
        return True
    return any(
        hmac.compare_digest(password, extra) for extra in settings.extra_password_list
    )


def login(session: Session, login_name: str, password: str) -> dict[str, Any]:
    """Проверить пару логин-пароль и выдать токен сессии."""
    user = session.execute(
        select(User).where(User.login == login_name.strip().lower())
    ).scalar_one_or_none()

    if user is None or not user.active or not _accepts(password, user):
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

    resolved = access.resolve(session, user.role)
    return {
        "token": token,
        "login": user.login,
        "full_name": user.full_name,
        "role": user.role,
        "role_title": resolved["title"],
        "level": resolved["level"],
        #: Какие вкладки показывать — интерфейс рисует только их
        "tabs": resolved["tabs"],
        "expires_hours": settings.session_hours,
    }


def logout(session: Session, token: str) -> None:
    record = session.execute(
        select(Session_).where(Session_.token == token)
    ).scalar_one_or_none()
    if record is not None:
        session.delete(record)
        session.commit()


def purge_expired_sessions(session: Session) -> int:
    """Удалить просроченные сессии.

    Раньше строка исчезала только при обращении с её же токеном — то есть у
    брошенного входа не исчезала никогда. Пока строка жива, токен в ней
    остаётся действующим ключом на случай утечки базы, да и таблица растёт.
    """
    from sqlalchemy import delete

    result = session.execute(
        delete(Session_).where(Session_.expires_at < datetime.utcnow())
    )
    session.commit()
    return result.rowcount or 0


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
        return {
            "login": "local",
            "role": "admin",
            "level": "admin",
            "full_name": "Локальный запуск",
            "tabs": list(access.TAB_CODES),
        }

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

    # Уровень прав и состав вкладок берём из роли: имя роли само по себе
    # больше ничего не значит, ролей может быть сколько угодно
    resolved = access.resolve(session, user.role)
    return {
        "login": user.login,
        "role": user.role,
        "full_name": user.full_name,
        "level": resolved["level"],
        "tabs": resolved["tabs"],
        "role_title": resolved["title"],
    }


def user_level(user: dict[str, Any]) -> str:
    """Уровень прав на запись. Роль без уровня считаем просмотром."""
    level = user.get("level") or user.get("role")
    return level if level in ROLES else "viewer"


def require_role(minimum: str):
    """Зависимость: требовать уровень прав не ниже указанного."""
    threshold = ROLES.index(minimum)

    def _check(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Требуется вход в систему",
            )
        if ROLES.index(user_level(user)) < threshold:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Недостаточно прав: нужна роль «{ROLE_TITLES[minimum]}»",
            )
        return user

    return _check


def require_section(
    request: Request, user: dict[str, Any] | None = Depends(current_user)
) -> dict[str, Any] | None:
    """Зависимость: вход плюс право на раздел, к которому относится путь.

    Спрятать вкладку в интерфейсе — не защита: адрес запроса виден в любой
    вкладке разработчика, и «этому человеку облигации не видны» должно
    означать именно это. Поэтому скрытый раздел закрывается и здесь.
    """
    path = request.url.path
    # Вход и проверка живости — до всякой авторизации, иначе войти нечем
    if path.startswith(access.PUBLIC):
        return user

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется вход в систему",
        )

    tabs = user.get("tabs")
    if tabs is None or access.path_allowed(path, tabs):
        return user

    sections = access.section_of(path) or frozenset()
    titles = ", ".join(
        f"«{access.TAB_TITLES[code]}»" for code in access.TAB_CODES if code in sections
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"Раздел не открыт для вашей роли: {titles}"
            if titles
            else "Раздел не открыт для вашей роли"
        ),
    )


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
