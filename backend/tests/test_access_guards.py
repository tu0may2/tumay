"""Проверка, что в интернете терминал не окажется нараспашку.

Терминал можно выставить наружу, поэтому важно не «есть ли вход вообще», а
закрыт ли каждый маршрут. Забыть зависимость в новом обработчике легко, и он
молча окажется публичным — эти тесты за этим и следят.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.main import app

#: Точки, до которых нельзя добраться, не войдя, — они и должны быть открыты
PUBLIC = {
    ("GET", "/api/health"),
    ("GET", "/api/auth/mode"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}


def _api_routes():
    for route in app.routes:
        for candidate in _walk(route):
            if isinstance(candidate, APIRoute) and candidate.path.startswith("/api"):
                yield candidate


def _walk(node):
    yield node
    inner = getattr(node, "original_router", None)
    if inner is not None:
        for route in inner.routes:
            yield from _walk(route)


@pytest.fixture()
def guarded_client(tmp_path):
    """Клиент к приложению с включённой проверкой доступа."""
    from fastapi.testclient import TestClient

    from app.config import settings

    previous = (
        settings.auth_enabled, settings.database_url,
        settings.collect_on_startup, settings.scheduler_enabled,
        settings.admin_password,
    )
    settings.auth_enabled = True
    settings.database_url = f"sqlite:///{tmp_path / 'guards.db'}"
    settings.collect_on_startup = False
    settings.scheduler_enabled = False
    settings.admin_password = "parol-administratora"

    import importlib

    import app.db
    import app.main

    importlib.reload(app.db)
    importlib.reload(app.main)

    with TestClient(app.main.app) as client:
        yield client

    (
        settings.auth_enabled, settings.database_url,
        settings.collect_on_startup, settings.scheduler_enabled,
        settings.admin_password,
    ) = previous
    importlib.reload(app.db)
    importlib.reload(app.main)


def test_every_route_demands_login(guarded_client):
    """Без токена ни один маршрут не отдаёт данные и не меняет их."""
    opened: list[str] = []
    for route in _api_routes():
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if (method, route.path) in PUBLIC:
                continue
            # Подставляем значение вместо параметров пути
            path = route.path.replace("{secid}", "AAA")
            for name in ("account_id", "flow_id", "placement_id", "deal_id",
                         "limit_id", "screen_id", "item_id", "rule_id", "user_id"):
                path = path.replace("{" + name + "}", "1")

            response = guarded_client.request(
                method, path, json={} if method in ("POST", "PUT", "PATCH") else None
            )
            if response.status_code not in (401, 403):
                opened.append(f"{method} {route.path} -> {response.status_code}")

    assert not opened, "маршруты доступны без входа: " + "; ".join(opened)


def test_public_routes_stay_reachable(guarded_client):
    """Иначе войти будет невозможно в принципе."""
    assert guarded_client.get("/api/health").status_code == 200
    assert guarded_client.get("/api/auth/mode").status_code == 200


def test_viewer_cannot_change_anything(guarded_client):
    """Роль «просмотр» читает, но не пишет."""
    from app.db import session_scope
    from app.services.auth import create_user

    with session_scope() as session:
        create_user(session, login="nabludatel", password="parol-nabludatelya")

    token = guarded_client.post(
        "/api/auth/login",
        json={"login": "nabludatel", "password": "parol-nabludatelya"},
    ).json()["token"]
    headers = {"X-Auth-Token": token}

    assert guarded_client.get("/api/portfolio", headers=headers).status_code == 200

    for method, path in (
        ("POST", "/api/portfolio/deals"),
        ("DELETE", "/api/portfolio/deals/1"),
        ("POST", "/api/limits"),
        ("POST", "/api/watchlist"),
        ("POST", "/api/collect"),
        ("POST", "/api/cash/accounts"),
    ):
        response = guarded_client.request(
            method, path, headers=headers,
            json={} if method == "POST" else None,
        )
        assert response.status_code == 403, f"{method} {path} разрешён наблюдателю"


def test_audit_is_admin_only(guarded_client):
    from app.db import session_scope
    from app.services.auth import create_user

    with session_scope() as session:
        create_user(session, login="treider", password="parol-treidera", role="trader")

    token = guarded_client.post(
        "/api/auth/login", json={"login": "treider", "password": "parol-treidera"}
    ).json()["token"]
    headers = {"X-Auth-Token": token}

    assert guarded_client.get("/api/audit", headers=headers).status_code == 403
    assert guarded_client.get("/api/users", headers=headers).status_code == 403
