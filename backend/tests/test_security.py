"""Защита терминала, выставленного в интернет.

Проверяем не наличие настроек, а поведение: что перебор пароля упирается в
отказ, что заголовки доходят до браузера и что чужая страница не может
обратиться к API. Настройку легко потерять при правке, поведение — заметно.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.services import ratelimit


@pytest.fixture(autouse=True)
def clean_limits():
    """Счётчики попыток живут в памяти процесса — разделяем тесты."""
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture()
def client(tmp_path):
    """Терминал с включённым входом, как на общем сервере."""
    from app.config import settings
    import app.db
    import app.main

    previous = (
        settings.auth_enabled, settings.database_url,
        settings.collect_on_startup, settings.scheduler_enabled,
        settings.admin_password, settings.extra_passwords,
    )
    settings.auth_enabled = True
    settings.database_url = f"sqlite:///{tmp_path / 'sec.db'}"
    settings.collect_on_startup = False
    settings.scheduler_enabled = False
    settings.admin_password = "ochen-dlinnyi-parol"
    settings.extra_passwords = ""

    importlib.reload(app.db)
    importlib.reload(app.main)

    with TestClient(app.main.app) as active:
        yield active

    (
        settings.auth_enabled, settings.database_url,
        settings.collect_on_startup, settings.scheduler_enabled,
        settings.admin_password, settings.extra_passwords,
    ) = previous
    importlib.reload(app.db)
    importlib.reload(app.main)


class TestBruteForce:
    """Подбор пароля должен упираться в отказ, а не идти бесконечно."""

    def test_repeated_failures_lock_the_login(self, client):
        for _ in range(ratelimit.MAX_ATTEMPTS):
            client.post("/api/auth/login", json={"login": "admin", "password": "нет"})

        response = client.post(
            "/api/auth/login", json={"login": "admin", "password": "нет"}
        )
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    def test_lock_survives_correct_password(self, client):
        """Иначе перебор можно продолжать, подмешивая верный пароль."""
        for _ in range(ratelimit.MAX_ATTEMPTS):
            client.post("/api/auth/login", json={"login": "admin", "password": "нет"})

        response = client.post(
            "/api/auth/login",
            json={"login": "admin", "password": "ochen-dlinnyi-parol"},
        )
        assert response.status_code == 429

    def test_successful_login_clears_the_counter(self, client):
        """Пара опечаток не должна копиться до блокировки."""
        for _ in range(3):
            client.post("/api/auth/login", json={"login": "admin", "password": "нет"})

        ok = client.post(
            "/api/auth/login",
            json={"login": "admin", "password": "ochen-dlinnyi-parol"},
        )
        assert ok.status_code == 200

        # Счётчик обнулён: снова доступен полный лимит попыток
        for _ in range(ratelimit.MAX_ATTEMPTS - 1):
            response = client.post(
                "/api/auth/login", json={"login": "admin", "password": "нет"}
            )
            assert response.status_code == 401

    def test_login_is_counted_separately_from_address(self, client):
        """Распределённый перебор одной записи тоже должен упираться в отказ.

        Адрес злоумышленник меняет свободно, а логин цели — нет.
        """
        for index in range(ratelimit.MAX_ATTEMPTS):
            client.post(
                "/api/auth/login",
                json={"login": "admin", "password": "нет"},
                headers={"X-Forwarded-For": f"10.0.0.{index}"},
            )

        response = client.post(
            "/api/auth/login",
            json={"login": "admin", "password": "нет"},
            headers={"X-Forwarded-For": "10.0.0.200"},
        )
        assert response.status_code == 429

    def test_other_login_is_not_affected(self, client):
        """Блокировка одной записи не должна закрывать вход остальным."""
        for index in range(ratelimit.MAX_ATTEMPTS):
            client.post(
                "/api/auth/login",
                json={"login": "chuzhoi", "password": "нет"},
                headers={"X-Forwarded-For": f"10.0.0.{index}"},
            )

        response = client.post(
            "/api/auth/login",
            json={"login": "admin", "password": "ochen-dlinnyi-parol"},
            headers={"X-Forwarded-For": "10.0.0.250"},
        )
        assert response.status_code == 200


class TestSecurityHeaders:
    def test_page_cannot_be_framed(self, client):
        """Иначе поверх терминала рисуют слой и заставляют нажимать не то."""
        response = client.get("/api/health")
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]

    def test_content_type_is_not_guessed(self, client):
        response = client.get("/api/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_scripts_are_restricted_to_own_origin(self, client):
        """Строгое правило закрывает главный путь эксплуатации XSS."""
        policy = client.get("/api/health").headers["Content-Security-Policy"]
        assert "script-src 'self'" in policy
        assert "'unsafe-eval'" not in policy
        # Инлайновых скриптов в разметке нет, послабления быть не должно
        assert "script-src 'self' 'unsafe-inline'" not in policy

    def test_hsts_only_over_https(self, client):
        """На голом http заголовок закрыл бы доступ до появления сертификата."""
        plain = client.get("/api/health")
        assert "Strict-Transport-Security" not in plain.headers

        secure = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
        assert "Strict-Transport-Security" in secure.headers


class TestCors:
    def test_foreign_origin_is_not_allowed_by_default(self, client):
        """Раньше стояло allow_origins=['*'] — API был открыт любой странице."""
        response = client.get(
            "/api/health", headers={"Origin": "https://zloi-sait.example"}
        )
        assert "access-control-allow-origin" not in {
            key.lower() for key in response.headers
        }


class TestSessionHygiene:
    def test_expired_sessions_are_purged(self, tmp_path):
        """Просроченная строка — рабочий ключ, если база утечёт."""
        from sqlalchemy import create_engine, func, select
        from sqlalchemy.orm import sessionmaker

        from app.models import Base, Session_, User
        from app.services.auth import purge_expired_sessions

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        with factory() as session:
            user = User(login="a", password_hash="x", role="admin")
            session.add(user)
            session.flush()
            session.add(Session_(
                token="staryi", user_id=user.id,
                expires_at=datetime.utcnow() - timedelta(days=2),
            ))
            session.add(Session_(
                token="zhivoi", user_id=user.id,
                expires_at=datetime.utcnow() + timedelta(hours=5),
            ))
            session.commit()

            assert purge_expired_sessions(session) == 1
            left = session.execute(select(Session_.token)).scalars().all()
            assert left == ["zhivoi"]


class TestScrapedLinks:
    """Ссылки с чужих страниц попадают в href на нашей."""

    def test_javascript_scheme_is_neutralised(self):
        from app.sources.cbr import _absolute_url

        url = _absolute_url("javascript:alert(1)")
        assert not url.lower().startswith("javascript:")

    def test_data_scheme_is_neutralised(self):
        from app.sources.cbr import _absolute_url

        url = _absolute_url("data:text/html,<script>alert(1)</script>")
        assert not url.lower().startswith("data:")

    def test_ordinary_links_still_work(self):
        from app.sources.cbr import _absolute_url

        assert _absolute_url("https://www.cbr.ru/press/") == "https://www.cbr.ru/press/"
        assert _absolute_url("/press/pr/").endswith("/press/pr/")


class TestApiDocs:
    """Карта API — подсказка для того, кто ищет, что можно нажать."""

    def test_docs_require_admin_when_login_is_on(self, client):
        for path in ("/docs", "/openapi.json", "/redoc"):
            response = client.get(path)
            assert response.status_code in (401, 404), f"{path} открыт без входа"

    def test_admin_still_can_read_docs(self, client):
        token = client.post(
            "/api/auth/login",
            json={"login": "admin", "password": "ochen-dlinnyi-parol"},
        ).json()["token"]

        response = client.get("/openapi.json", headers={"X-Auth-Token": token})
        assert response.status_code == 200
        assert "paths" in response.json()


class TestAdminPasswordLogging:
    """Пароль из настроек не должен оседать в системном журнале."""

    def test_password_from_settings_is_not_logged(self, tmp_path, caplog):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.config import settings
        from app.models import Base
        from app.services.auth import ensure_admin

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        previous = settings.admin_password
        settings.admin_password = "sekretnyi-parol-iz-nastroek"
        try:
            with caplog.at_level("INFO"), factory() as session:
                ensure_admin(session)
            assert "sekretnyi-parol-iz-nastroek" not in caplog.text
        finally:
            settings.admin_password = previous

    def test_generated_password_is_shown_once(self, tmp_path, caplog):
        """Сгенерированный пароль иначе узнать неоткуда — его печатаем."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.config import settings
        from app.models import Base
        from app.services.auth import ensure_admin

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        previous = settings.admin_password
        settings.admin_password = ""
        try:
            with caplog.at_level("WARNING"), factory() as session:
                password = ensure_admin(session)
            assert password and password in caplog.text
        finally:
            settings.admin_password = previous
