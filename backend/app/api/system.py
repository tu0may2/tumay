"""Доступ, история портфеля, отчёт, налоги и уведомления."""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import AuditRecord, NotificationRule, User
from ..schemas import (
    LoginRequest,
    NotificationRuleCreate,
    NotificationRuleRead,
    PasswordChange,
    RoleSave,
    UserCreate,
    UserRead,
    UserRoleChange,
)
from ..services import access as access_service
from ..services import auth as auth_service
from ..services import ratelimit
from ..services import history as history_service
from ..services import report as report_service
from ..services import treasury_extras as extras_service
from ..services.auth import audit, require_admin, require_viewer

router = APIRouter(prefix="/api", tags=["Система"])


# ----------------------------------------------------------------------
# Вход и пользователи
# ----------------------------------------------------------------------
@router.get("/auth/mode", summary="Нужен ли вход")
def auth_mode(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Публичная точка: интерфейс должен узнать про вход до логина."""
    return {
        "auth_enabled": settings.auth_enabled,
        "roles": [
            {"code": code, "title": auth_service.ROLE_TITLES[code]}
            for code in auth_service.ROLES
        ],
        "note": (
            "Вход включается переменной TREASURY_AUTH_ENABLED=true. "
            "При выключенной проверке терминал работает без пароля — "
            "так задумано для запуска на одной машине."
        ),
    }


@router.post("/auth/login", summary="Войти")
def login(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if not settings.auth_enabled:
        return {
            "token": None,
            "login": "local",
            "role": "admin",
            "detail": "Проверка входа выключена",
        }

    # Считаем попытки и по адресу, и по логину: первое мешает перебирать
    # пароли с одной машины, второе — распределённому перебору одной
    # учётной записи с разных адресов
    keys = (
        f"ip:{ratelimit.client_key(request)}",
        f"login:{payload.login.strip().lower()}",
    )
    for key in keys:
        ratelimit.check(key)

    try:
        result = auth_service.login(session, payload.login, payload.password)
    except HTTPException as exc:
        if exc.status_code == 401:
            for key in keys:
                ratelimit.register_failure(key)
        raise

    for key in keys:
        ratelimit.register_success(key)
    return result


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Выйти")
def logout(
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
    session: Session = Depends(get_session),
) -> None:
    if x_auth_token:
        auth_service.logout(session, x_auth_token)


@router.get("/auth/me", summary="Текущий пользователь")
def me(user: dict = Depends(require_viewer)) -> dict[str, Any]:
    """Кто вошёл, с какими правами и какие вкладки ему показывать."""
    return {
        **user,
        "role_title": user.get("role_title")
        or auth_service.ROLE_TITLES.get(user["role"], user["role"]),
        "level": auth_service.user_level(user),
        "tabs": user.get("tabs") or list(access_service.TAB_CODES),
    }


@router.post("/auth/password", summary="Сменить пароль")
def change_password(
    payload: PasswordChange,
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> dict[str, Any]:
    """Свой пароль меняет любой вошедший, чужой — только администратор."""
    target_login = (payload.login or user["login"]).strip().lower()
    if target_login != user["login"] and user["role"] != "admin":
        raise HTTPException(
            status_code=403, detail="Чужой пароль может менять только администратор"
        )

    target = session.execute(
        select(User).where(User.login == target_login)
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    target.password_hash = auth_service.hash_password(payload.password)
    # Старые сессии больше не действуют: сменивший пароль ожидает,
    # что прежний доступ закрылся
    auth_service.drop_sessions(session, target.id)
    audit(session, user, action="update", entity="user", entity_id=target.id,
          detail=f"смена пароля {target.login}")
    return {"login": target.login, "detail": "Пароль изменён"}


@router.get("/users", response_model=list[UserRead], summary="Пользователи")
def list_users(
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> list[User]:
    return list(session.execute(select(User).order_by(User.login)).scalars())


@router.post(
    "/users",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Завести пользователя",
)
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> User:
    existing = session.execute(
        select(User).where(User.login == payload.login.strip().lower())
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Такой логин уже занят")

    created = auth_service.create_user(
        session,
        login=payload.login,
        password=payload.password,
        role=payload.role,
        full_name=payload.full_name,
    )
    audit(session, user, action="create", entity="user", entity_id=created.id,
          detail=f"{created.login}, роль {created.role}")
    return created


@router.delete(
    "/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Отключить доступ"
)
def disable_user(
    user_id: int,
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> None:
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target.login == user.get("login"):
        raise HTTPException(status_code=422, detail="Нельзя отключить себя")

    # Учётную запись не удаляем: на неё ссылается журнал изменений
    target.active = False
    session.commit()
    audit(session, user, action="disable", entity="user", entity_id=user_id,
          detail=target.login)


@router.patch("/users/{user_id}/role", response_model=UserRead, summary="Сменить роль")
def change_role(
    user_id: int,
    payload: UserRoleChange,
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> User:
    """Перевести учётную запись на другую роль."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if access_service.role_by_name(session, payload.role) is None:
        raise HTTPException(status_code=422, detail="Роль не найдена")
    if target.login == user.get("login") and payload.role != target.role:
        # Снять роль с самого себя — верный способ потерять вход в настройки
        raise HTTPException(
            status_code=422, detail="Свою роль менять нельзя — попросите другого админа"
        )

    was, target.role = target.role, payload.role
    # Права меняются сразу, а не после того, как человек сам перезайдёт
    auth_service.drop_sessions(session, target.id)
    session.commit()
    session.refresh(target)
    audit(session, user, action="update", entity="user", entity_id=user_id,
          detail=f"{target.login}: роль {was} → {payload.role}")
    return target


@router.post("/users/{user_id}/enable", response_model=UserRead, summary="Вернуть доступ")
def enable_user(
    user_id: int,
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> User:
    """Включить отключённую учётную запись обратно."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    target.active = True
    session.commit()
    session.refresh(target)
    audit(session, user, action="enable", entity="user", entity_id=user_id,
          detail=target.login)
    return target


# ----------------------------------------------------------------------
# Роли и состав вкладок
# ----------------------------------------------------------------------
@router.get("/roles", summary="Роли и их вкладки")
def list_roles(
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Роли, доступные вкладки и справочник вкладок для выбора."""
    return {
        "roles": access_service.list_roles(session),
        "tabs": access_service.tabs_catalog(),
        "levels": [
            {"code": code, "title": access_service.LEVEL_TITLES[code]}
            for code in access_service.LEVELS
        ],
    }


@router.put("/roles", summary="Завести или изменить роль")
def save_role(
    payload: RoleSave,
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Записать роль. Состав вкладок администратора не меняется."""
    try:
        role = access_service.save_role(
            session,
            name=payload.name,
            title=payload.title,
            level=payload.level,
            tabs=payload.tabs,
            comment=payload.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Вкладки меняются сразу у всех, кто на этой роли: иначе человек до
    # следующего входа продолжал бы работать по прежним правам
    for record in session.execute(
        select(User).where(User.role == role.name)
    ).scalars():
        if record.login != user.get("login"):
            auth_service.drop_sessions(session, record.id)

    audit(session, user, action="update", entity="role", entity_id=role.id,
          detail=f"{role.name}: {role.level}, вкладок {len(payload.tabs)}")
    return {"roles": access_service.list_roles(session)}


@router.delete("/roles/{name}", summary="Удалить роль")
def delete_role(
    name: str,
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> dict[str, Any]:
    try:
        access_service.delete_role(session, name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(session, user, action="delete", entity="role", detail=name)
    return {"roles": access_service.list_roles(session)}


@router.get("/audit", summary="Журнал изменений")
def audit_log(
    limit: int = Query(200, ge=1, le=2000),
    entity: str | None = Query(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> list[dict[str, Any]]:
    statement = select(AuditRecord).order_by(AuditRecord.created_at.desc())
    if entity:
        statement = statement.where(AuditRecord.entity == entity)
    return [
        {
            "id": record.id,
            "created_at": record.created_at,
            "user_login": record.user_login,
            "action": record.action,
            "entity": record.entity,
            "entity_id": record.entity_id,
            "detail": record.detail,
        }
        for record in session.execute(statement.limit(limit)).scalars()
    ]


# ----------------------------------------------------------------------
# История стоимости портфеля
# ----------------------------------------------------------------------
@router.get("/history/portfolio", summary="История стоимости портфеля")
def portfolio_history(
    name: str | None = Query(None),
    days: int = Query(365, ge=7, le=3650),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> dict[str, Any]:
    return history_service.portfolio_history(session, portfolio=name, days=days)


@router.post("/history/snapshot", summary="Снять состояние портфеля")
def take_snapshot(
    name: str | None = Query(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> dict[str, Any]:
    """Зафиксировать стоимость на сегодня — обычно это делает расписание.

    Задним числом снимок сделать нельзя: оценка считается по текущим ценам,
    и запись такой суммы на прошлую дату нарисовала бы историю, которой не было.
    """
    return history_service.take_snapshot(session, portfolio=name)


# ----------------------------------------------------------------------
# Оферты, налоги, уведомления
# ----------------------------------------------------------------------
@router.get("/offers", summary="Ближайшие оферты по своим бумагам")
def offers(
    name: str | None = Query(None),
    horizon_days: int = Query(90, ge=1, le=730),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return extras_service.upcoming_offers(
        session, portfolio=name, horizon_days=horizon_days
    )


@router.get("/taxes", summary="Ставки налога для расчётов")
def taxes(user: dict = Depends(require_viewer)) -> dict[str, Any]:
    return extras_service.tax_settings()


@router.get("/events", summary="События, требующие внимания")
def events(
    name: str | None = Query(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return extras_service.collect_events(session, portfolio=name)


@router.get(
    "/notifications",
    response_model=list[NotificationRuleRead],
    summary="Правила уведомлений",
)
def list_rules(
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> list[NotificationRule]:
    return list(
        session.execute(select(NotificationRule).order_by(NotificationRule.name)).scalars()
    )


@router.post(
    "/notifications",
    response_model=NotificationRuleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить правило",
)
def create_rule(
    payload: NotificationRuleCreate,
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> NotificationRule:
    rule = NotificationRule(
        name=payload.name,
        webhook_url=payload.webhook_url,
        events=",".join(payload.events),
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    audit(session, user, action="create", entity="notification_rule",
          entity_id=rule.id, detail=rule.name)
    return rule


@router.delete(
    "/notifications/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить правило",
)
def delete_rule(
    rule_id: int,
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> None:
    rule = session.get(NotificationRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило не найдено")
    session.delete(rule)
    session.commit()
    audit(session, user, action="delete", entity="notification_rule", entity_id=rule_id)


@router.post("/notifications/send", summary="Разослать уведомления")
def send_notifications(
    name: str | None = Query(None),
    dry_run: bool = Query(True, description="Только показать, что будет отправлено"),
    session: Session = Depends(get_session),
    user: dict = Depends(require_admin),
) -> dict[str, Any]:
    return extras_service.notify(session, portfolio=name, dry_run=dry_run)


# ----------------------------------------------------------------------
# Отчёт для инвесткомитета
# ----------------------------------------------------------------------
@router.get("/report", summary="Отчёт для инвесткомитета (.xlsx)")
def report(
    name: str | None = Query(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> Response:
    """Книга Excel: сводка, позиции, лимиты, риск, деньги, оферты, ориентиры."""
    payload = report_service.build_report(session, portfolio=name)
    filename = f"Отчёт по портфелю {name or 'все'} {date.today():%d.%m.%Y}.xlsx"
    return Response(
        content=payload,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )
