"""Тесты HTTP-слоя на изолированной БД."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import app
from app.models import Base, CurvePoint, FxRate, Instrument, MacroRate, Quote


@pytest.fixture()
def client(monkeypatch):
    """Клиент API поверх БД в памяти, без обращений к биржам."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        _seed(session)
        session.commit()

    def override_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_session
    # Отключаем стартовый сбор и планировщик: тест не ходит в сеть
    monkeypatch.setattr("app.main.settings.collect_on_startup", False)
    monkeypatch.setattr("app.main.settings.scheduler_enabled", False)

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _seed(session):
    share = Instrument(
        secid="SBER", board="TQBR", engine="stock", market="shares", kind="share",
        short_name="Сбербанк", isin="RU0009029540", lot_size=1, list_level=1,
    )
    bond = Instrument(
        secid="OFZ26238", board="TQOB", engine="stock", market="bonds", kind="bond",
        short_name="ОФЗ 26238", isin="RU000A1038V6", face_value=1000.0,
        maturity_date=date(2041, 5, 15), coupon_percent=7.1,
    )
    session.add_all([share, bond])
    session.flush()

    session.add_all([
        Quote(instrument_id=share.id, ts=datetime(2026, 7, 31, 12, 0), last=274.45,
              prev_close=274.5, change_pct=-0.02, turnover=9.5e8, volume=3.4e6,
              num_trades=10773, spread=0.02, bid=274.4, offer=274.42),
        Quote(instrument_id=bond.id, ts=datetime(2026, 7, 31, 12, 0, 1), last=54.37,
              yield_pct=15.31, duration_days=2632, turnover=5.2e7, num_trades=900,
              spread=0.05, bid=54.3, offer=54.35),
        CurvePoint(curve_date=date(2026, 7, 30), period_years=7.0, value=15.5),
        FxRate(source="cbr", code="USD", name="Доллар США", nominal=1,
               value=79.8573, rate_date=date(2026, 7, 31)),
        MacroRate(code="KEY_RATE", name="Ключевая ставка", value=14.0,
                  rate_date=date(2026, 7, 31)),
    ])


class TestMarketEndpoints:
    def test_health(self, client):
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["instruments"] == 2

    def test_overview(self, client):
        payload = client.get("/api/overview").json()
        assert payload["key_rate"]["value"] == 14.0
        assert payload["instruments_total"] == 2
        assert any(item["code"] == "USD" for item in payload["fx"])
        assert payload["curve"]["points"]

    def test_instruments_filtered_by_kind(self, client):
        payload = client.get("/api/instruments", params={"kind": "bond"}).json()
        assert payload["total"] == 1
        assert payload["items"][0]["secid"] == "OFZ26238"

    def test_bond_row_has_spread_to_curve(self, client):
        payload = client.get("/api/instruments", params={"kind": "bond"}).json()
        row = payload["items"][0]
        # Дюрация 2632 дня ≈ 7.2 года, кривая 15.5% → премия около -19 бп
        assert row["duration_years"] == pytest.approx(7.21, abs=0.01)
        assert row["spread_to_curve_bp"] == pytest.approx(-19, abs=1)

    def test_instrument_detail(self, client):
        payload = client.get("/api/instruments/SBER").json()
        assert payload["instrument"]["secid"] == "SBER"
        assert "history" in payload and "cashflows" in payload

    def test_instrument_detail_is_case_insensitive(self, client):
        assert client.get("/api/instruments/sber").status_code == 200

    def test_unknown_instrument_returns_404(self, client):
        response = client.get("/api/instruments/NOSUCH")
        assert response.status_code == 404

    def test_search_filter(self, client):
        assert client.get("/api/instruments", params={"search": "сбербанк"}).json()["total"] == 1
        assert client.get("/api/instruments", params={"search": "лукойл"}).json()["total"] == 0

    def test_curve_and_rates(self, client):
        assert client.get("/api/curve").json()["points"]
        assert client.get("/api/rates", params={"code": "KEY_RATE"}).json()[0]["value"] == 14.0

    def test_sources_documents_nsd(self, client):
        codes = {item["code"] for item in client.get("/api/sources").json()}
        assert codes == {"moex", "cbr", "nsd"}


class TestPortfolioEndpoints:
    def test_empty_portfolio(self, client):
        payload = client.get("/api/portfolio").json()
        assert payload["total_value"] == 0
        assert payload["positions"] == []

    def test_deal_lifecycle(self, client):
        created = client.post("/api/portfolio/deals", json={
            "secid": "SBER", "side": "buy", "quantity": 1000,
            "price": 250.0, "trade_date": "2026-07-15", "fee": 125,
        })
        assert created.status_code == 201
        deal_id = created.json()["id"]

        summary = client.get("/api/portfolio").json()
        assert summary["positions_open"] == 1
        # (274.45 - 250) * 1000
        assert summary["unrealized_pnl"] == pytest.approx(24450)

        assert client.delete(f"/api/portfolio/deals/{deal_id}").status_code == 204
        assert client.get("/api/portfolio").json()["positions_open"] == 0

    def test_secid_is_normalized(self, client):
        response = client.post("/api/portfolio/deals", json={
            "secid": "sber", "side": "buy", "quantity": 10,
            "price": 250.0, "trade_date": "2026-07-15",
        })
        assert response.status_code == 201
        assert response.json()["secid"] == "SBER"

    def test_unknown_instrument_rejected(self, client):
        response = client.post("/api/portfolio/deals", json={
            "secid": "NOSUCH", "side": "buy", "quantity": 10,
            "price": 100.0, "trade_date": "2026-07-15",
        })
        assert response.status_code == 422
        assert "не найден" in response.json()["detail"]

    def test_invalid_quantity_rejected(self, client):
        response = client.post("/api/portfolio/deals", json={
            "secid": "SBER", "side": "buy", "quantity": -10,
            "price": 250.0, "trade_date": "2026-07-15",
        })
        assert response.status_code == 422

    def test_invalid_side_rejected(self, client):
        response = client.post("/api/portfolio/deals", json={
            "secid": "SBER", "side": "hold", "quantity": 10,
            "price": 250.0, "trade_date": "2026-07-15",
        })
        assert response.status_code == 422

    def test_delete_missing_deal(self, client):
        assert client.delete("/api/portfolio/deals/9999").status_code == 404

    def test_bond_sensitivity(self, client):
        client.post("/api/portfolio/deals", json={
            "secid": "OFZ26238", "side": "buy", "quantity": 1000,
            "price": 54.37, "trade_date": "2026-07-15",
        })
        payload = client.get("/api/portfolio/sensitivity").json()
        assert payload["weighted_duration_years"] == pytest.approx(7.21, abs=0.01)
        up = next(s for s in payload["scenarios"] if s["shift_bp"] == 100)
        assert up["impact_rub"] < 0


class TestFrontend:
    def test_index_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Казначейский терминал" in response.text

    def test_static_assets(self, client):
        for path in ("/static/css/app.css", "/static/js/app.js", "/static/js/charts.js"):
            assert client.get(path).status_code == 200


class TestBulkDeals:
    """Пакетное добавление сделок из витрины бумаг."""

    def test_adds_several_at_once(self, client):
        response = client.post("/api/portfolio/deals/bulk", json={
            "deals": [
                {"secid": "SBER", "side": "buy", "quantity": 100, "price": 274.0,
                 "trade_date": "2026-08-10"},
                {"secid": "OFZ26238", "side": "buy", "quantity": 50, "price": 54.0,
                 "accrued_interest": 13.4, "trade_date": "2026-08-10"},
            ]
        })
        assert response.status_code == 201
        payload = response.json()
        assert payload["created_count"] == 2
        assert payload["error_count"] == 0
        assert client.get("/api/portfolio").json()["positions_open"] == 2

    def test_unknown_instrument_does_not_block_others(self, client):
        response = client.post("/api/portfolio/deals/bulk", json={
            "deals": [
                {"secid": "SBER", "side": "buy", "quantity": 10, "price": 274.0,
                 "trade_date": "2026-08-10"},
                {"secid": "НЕТТАКОЙ", "side": "buy", "quantity": 10, "price": 100.0,
                 "trade_date": "2026-08-10"},
            ]
        })
        payload = response.json()
        # Хорошая строка сохраняется, по плохой возвращается причина
        assert payload["created_count"] == 1
        assert payload["error_count"] == 1
        assert payload["errors"][0]["secid"] == "НЕТТАКОЙ"

    def test_secid_normalised(self, client):
        response = client.post("/api/portfolio/deals/bulk", json={
            "deals": [{"secid": "sber", "side": "buy", "quantity": 10,
                       "price": 274.0, "trade_date": "2026-08-10"}]
        })
        assert response.json()["created"][0]["secid"] == "SBER"

    def test_rejects_empty_list(self, client):
        assert client.post("/api/portfolio/deals/bulk", json={"deals": []}).status_code == 422

    def test_rejects_bad_quantity(self, client):
        response = client.post("/api/portfolio/deals/bulk", json={
            "deals": [{"secid": "SBER", "side": "buy", "quantity": -5,
                       "price": 274.0, "trade_date": "2026-08-10"}]
        })
        assert response.status_code == 422
