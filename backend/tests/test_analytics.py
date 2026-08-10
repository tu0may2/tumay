"""Тесты аналитики и портфельных расчётов."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Bar, Base, CurvePoint, Deal, Instrument, Quote
from app.services import analytics
from app.services import portfolio as portfolio_service


@pytest.fixture()
def session():
    """Изолированная БД в памяти на каждый тест."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        yield active


def add_instrument(session, secid, kind="share", **kwargs):
    instrument = Instrument(
        secid=secid,
        board=kwargs.pop("board", "TQBR"),
        engine="stock",
        market="shares" if kind == "share" else "bonds",
        kind=kind,
        short_name=kwargs.pop("short_name", secid),
        **kwargs,
    )
    session.add(instrument)
    session.flush()
    return instrument


def add_quote(session, instrument, ts=None, **kwargs):
    quote = Quote(
        instrument_id=instrument.id,
        ts=ts or datetime(2026, 7, 31, 12, 0),
        **kwargs,
    )
    session.add(quote)
    session.flush()
    return quote


class TestLiquidity:
    def test_relative_spread(self):
        quote = Quote(last=100.0, spread=0.5)
        assert analytics.relative_spread_pct(quote) == pytest.approx(0.5)

    def test_relative_spread_without_price(self):
        assert analytics.relative_spread_pct(Quote(spread=0.5)) is None

    def test_liquid_beats_illiquid(self):
        liquid = Quote(last=100, spread=0.01, turnover=5e9, num_trades=50000)
        illiquid = Quote(last=100, spread=2.0, turnover=50000, num_trades=3)
        assert analytics.liquidity_score(liquid) > 80
        assert analytics.liquidity_score(illiquid) < 25

    def test_score_is_bounded(self):
        extreme = Quote(last=100, spread=0.0, turnover=1e15, num_trades=10**9)
        assert 0 <= analytics.liquidity_score(extreme) <= 100

    def test_no_data_gives_none(self):
        assert analytics.liquidity_score(Quote()) is None


class TestYieldCurve:
    POINTS = [
        {"period_years": 0.25, "value": 13.0},
        {"period_years": 1.0, "value": 14.0},
        {"period_years": 5.0, "value": 15.0},
    ]

    def test_interpolates_between_points(self):
        # Середина между 1 и 5 лет: (14 + 15) / 2
        assert analytics.curve_yield_at(self.POINTS, 3.0) == pytest.approx(14.5)

    def test_exact_point(self):
        assert analytics.curve_yield_at(self.POINTS, 1.0) == pytest.approx(14.0)

    def test_clamps_outside_range(self):
        assert analytics.curve_yield_at(self.POINTS, 0.01) == 13.0
        assert analytics.curve_yield_at(self.POINTS, 30.0) == 15.0

    def test_empty_curve(self):
        assert analytics.curve_yield_at([], 5.0) is None

    def test_reads_latest_curve_from_db(self, session):
        session.add_all([
            CurvePoint(curve_date=date(2026, 7, 29), period_years=1.0, value=10.0),
            CurvePoint(curve_date=date(2026, 7, 30), period_years=1.0, value=14.0),
        ])
        session.flush()
        curve = analytics.yield_curve(session)
        assert curve["curve_date"] == date(2026, 7, 30)
        assert curve["points"] == [{"period_years": 1.0, "value": 14.0}]


class TestLatestRows:
    def test_picks_latest_quote_per_instrument(self, session):
        """Площадки снимаются в разное время — нужен максимум по каждой бумаге."""
        share = add_instrument(session, "SBER", kind="share")
        bond = add_instrument(session, "SU26238RMFS4", kind="bond", board="TQOB")

        add_quote(session, share, ts=datetime(2026, 7, 31, 12, 0, 0), last=100)
        add_quote(session, share, ts=datetime(2026, 7, 31, 12, 5, 0), last=101)
        # Облигационная доска снялась на секунду позже акций
        add_quote(session, bond, ts=datetime(2026, 7, 31, 12, 5, 1), last=95)

        rows = analytics.latest_rows(session)
        assert len(rows) == 2
        prices = {instrument.secid: quote.last for instrument, quote in rows}
        assert prices == {"SBER": 101, "SU26238RMFS4": 95}

    def test_filters_by_kind(self, session):
        share = add_instrument(session, "SBER", kind="share")
        bond = add_instrument(session, "OFZ", kind="bond", board="TQOB")
        add_quote(session, share, last=100)
        add_quote(session, bond, last=95)

        rows = analytics.latest_rows(session, kinds=("bond",))
        assert [instrument.secid for instrument, _ in rows] == ["OFZ"]


class TestScreener:
    def test_bond_row_gets_spread_to_curve(self, session):
        session.add(CurvePoint(curve_date=date(2026, 7, 30), period_years=2.0, value=14.0))
        bond = add_instrument(session, "BOND1", kind="bond", board="TQCB")
        # Дюрация 730 дней = 2 года, доходность 16% против кривой 14% → 200 бп
        add_quote(session, bond, last=98, yield_pct=16.0, duration_days=730, turnover=1e7)
        session.flush()

        result = analytics.screener(session, kinds=("bond",))
        row = result["items"][0]
        assert row["duration_years"] == pytest.approx(2.0)
        assert row["curve_yield_pct"] == pytest.approx(14.0)
        assert row["spread_to_curve_bp"] == pytest.approx(200)

    def test_filters_by_turnover(self, session):
        big = add_instrument(session, "BIG")
        small = add_instrument(session, "SMALL")
        add_quote(session, big, last=100, turnover=1e9)
        add_quote(session, small, last=100, turnover=1000)

        result = analytics.screener(session, min_turnover=1e6)
        assert [row["secid"] for row in result["items"]] == ["BIG"]

    def test_search_matches_name_and_isin(self, session):
        instrument = add_instrument(session, "SBER", short_name="Сбербанк", isin="RU0009029540")
        add_quote(session, instrument, last=100, turnover=1e6)

        assert analytics.screener(session, search="сбер")["total"] == 1
        assert analytics.screener(session, search="RU00090295")["total"] == 1
        assert analytics.screener(session, search="газпром")["total"] == 0

    def test_sorting_puts_nulls_last(self, session):
        with_value = add_instrument(session, "A")
        without = add_instrument(session, "B")
        add_quote(session, with_value, last=100, turnover=500)
        add_quote(session, without, last=100)

        items = analytics.screener(session, sort_by="turnover")["items"]
        assert items[0]["secid"] == "A"


class TestVolumeAnomalies:
    def test_detects_spike(self, session):
        instrument = add_instrument(session, "SPIKE")
        base = date.today() - timedelta(days=20)
        # Ровный объём, затем десятикратный всплеск в последний день
        for offset in range(10):
            session.add(Bar(
                instrument_id=instrument.id,
                trade_date=base + timedelta(days=offset),
                close=100,
                volume=1000,
            ))
        session.add(Bar(
            instrument_id=instrument.id,
            trade_date=base + timedelta(days=10),
            close=100,
            volume=10000,
        ))
        session.flush()

        found = analytics.volume_anomalies(session, lookback_days=60, z_threshold=2.0)
        assert len(found) == 1
        assert found[0]["secid"] == "SPIKE"
        assert found[0]["ratio_to_avg"] == pytest.approx(10.0)

    def test_ignores_stable_volume(self, session):
        instrument = add_instrument(session, "CALM")
        base = date.today() - timedelta(days=20)
        for offset in range(11):
            session.add(Bar(
                instrument_id=instrument.id,
                trade_date=base + timedelta(days=offset),
                close=100,
                volume=1000 + offset,
            ))
        session.flush()
        assert analytics.volume_anomalies(session, lookback_days=60) == []

    def test_requires_minimum_history(self, session):
        instrument = add_instrument(session, "SHORT")
        base = date.today() - timedelta(days=5)
        for offset in range(3):
            session.add(Bar(
                instrument_id=instrument.id,
                trade_date=base + timedelta(days=offset),
                close=100,
                volume=1000 * (offset + 1),
            ))
        session.flush()
        assert analytics.volume_anomalies(session) == []


class TestPortfolio:
    def test_average_cost_and_realized_pnl(self, session):
        instrument = add_instrument(session, "SBER")
        add_quote(session, instrument, last=274.45)
        session.add_all([
            Deal(secid="SBER", side="buy", quantity=10000, price=250.0,
                 trade_date=date(2026, 7, 1), fee=1250),
            Deal(secid="SBER", side="sell", quantity=3000, price=270.0,
                 trade_date=date(2026, 7, 20), fee=400),
        ])
        session.flush()

        positions = portfolio_service.compute_positions(session)
        assert len(positions) == 1
        position = positions[0]
        assert position["quantity"] == pytest.approx(7000)
        assert position["avg_price"] == pytest.approx(250.0)
        # Реализовано: (270 - 250) * 3000
        # Поля с суффиксом _rub: портфель мультивалютный, всё приведено к рублю
        assert position["realized_price_pnl_rub"] == pytest.approx(60000)
        # Нереализовано: (274.45 - 250) * 7000
        assert position["unrealized_pnl_rub"] == pytest.approx(171150)

    def test_bond_priced_as_percent_of_face(self, session):
        bond = add_instrument(session, "OFZ", kind="bond", board="TQOB", face_value=1000.0)
        add_quote(session, bond, last=54.372, duration_days=2632, yield_pct=15.31)
        session.add(Deal(secid="OFZ", side="buy", quantity=5000, price=62.5,
                         trade_date=date(2026, 7, 10)))
        session.flush()

        position = portfolio_service.compute_positions(session)[0]
        # 5000 бумаг × номинал 1000 × 54.372%
        assert position["market_value_rub"] == pytest.approx(2718600)
        assert position["cost_rub"] == pytest.approx(3125000)
        assert position["unrealized_pnl_rub"] == pytest.approx(-406400)

    def test_summary_weights_and_concentration(self, session):
        for secid, price in (("A", 100.0), ("B", 100.0)):
            instrument = add_instrument(session, secid)
            add_quote(session, instrument, last=price)
            session.add(Deal(secid=secid, side="buy", quantity=100, price=100.0,
                             trade_date=date(2026, 7, 1)))
        session.flush()

        summary = portfolio_service.portfolio_summary(session)
        assert summary["total_value"] == pytest.approx(20000)
        assert summary["positions_open"] == 2
        # Две равные позиции: HHI = 0.5² + 0.5²
        assert summary["concentration_hhi"] == pytest.approx(0.5)

    def test_closed_position_excluded_from_value(self, session):
        instrument = add_instrument(session, "SBER")
        add_quote(session, instrument, last=270.0)
        session.add_all([
            Deal(secid="SBER", side="buy", quantity=100, price=250.0, trade_date=date(2026, 7, 1)),
            Deal(secid="SBER", side="sell", quantity=100, price=260.0, trade_date=date(2026, 7, 5)),
        ])
        session.flush()

        summary = portfolio_service.portfolio_summary(session)
        assert summary["positions_open"] == 0
        assert summary["total_value"] == 0
        assert summary["realized_pnl"] == pytest.approx(1000)

    def test_rate_sensitivity_signs(self, session):
        bond = add_instrument(session, "OFZ", kind="bond", board="TQOB", face_value=1000.0)
        add_quote(session, bond, last=100.0, duration_days=3650)
        session.add(Deal(secid="OFZ", side="buy", quantity=1000, price=100.0,
                         trade_date=date(2026, 7, 1)))
        session.flush()

        from app.services import risk as risk_service

        result = risk_service.rate_sensitivity(session, shift_bp=(100,))
        assert result["weighted_duration_years"] == pytest.approx(10.0)
        up = next(s for s in result["scenarios"] if s["shift_bp"] == 100)
        down = next(s for s in result["scenarios"] if s["shift_bp"] == -100)
        # Рост ставок удешевляет облигации, падение — удорожает
        assert up["impact_rub"] < 0
        assert down["impact_rub"] > 0
        # Дюрация 10 лет × 1% ≈ 10% стоимости; поправка на выпуклость
        # смягчает падение, поэтому допуск шире
        assert up["impact_rub"] == pytest.approx(-100000, rel=0.10)

    def test_empty_portfolio(self, session):
        summary = portfolio_service.portfolio_summary(session)
        assert summary["total_value"] == 0
        assert summary["positions"] == []
