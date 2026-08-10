"""Тесты портфельного учёта, лимитов, риска и бенчмарка."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Bar,
    Base,
    CorpAction,
    Deal,
    FxRate,
    Instrument,
    Limit,
    Quote,
)
from app.services import benchmark as benchmark_service
from app.services import limits as limits_service
from app.services import portfolio as portfolio_service
from app.services import risk as risk_service
from app.services.fx import FxBook, instrument_currency, is_rub


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        yield active


def add_bond(session, secid, *, face=1000.0, face_unit="SUR", **kwargs):
    instrument = Instrument(
        secid=secid, board="TQCB", engine="stock", market="bonds", kind="bond",
        short_name=secid, isin=f"RU{secid}", face_value=face, face_unit=face_unit,
        **kwargs,
    )
    session.add(instrument)
    session.flush()
    return instrument


def add_share(session, secid, **kwargs):
    instrument = Instrument(
        secid=secid, board="TQBR", engine="stock", market="shares", kind="share",
        short_name=secid, **kwargs,
    )
    session.add(instrument)
    session.flush()
    return instrument


def add_quote(session, instrument, **kwargs):
    quote = Quote(
        instrument_id=instrument.id, ts=datetime(2026, 8, 10, 12, 0), **kwargs
    )
    session.add(quote)
    session.flush()
    return quote


def add_fx(session, code, value, on_date):
    session.add(
        FxRate(source="cbr", code=code, nominal=1, value=value, rate_date=on_date)
    )
    session.flush()


class TestFxBook:
    def test_rouble_needs_no_conversion(self, session):
        book = FxBook(session)
        for code in ("SUR", "RUB", "RUR", None):
            assert book.rate(code) == 1.0

    def test_takes_last_known_rate_before_date(self, session):
        add_fx(session, "USD", 80.0, date(2026, 3, 10))
        add_fx(session, "USD", 82.0, date(2026, 8, 10))
        book = FxBook(session)
        # Биржа торгует и в выходные, когда ЦБ курс не публикует
        assert book.rate("USD", date(2026, 5, 1)) == 80.0
        assert book.rate("USD", date(2026, 8, 10)) == 82.0
        assert book.rate("USD") == 82.0

    def test_rate_before_first_known(self, session):
        add_fx(session, "USD", 80.0, date(2026, 3, 10))
        assert FxBook(session).rate("USD", date(2020, 1, 1)) == 80.0

    def test_nominal_is_applied(self, session):
        session.add(
            FxRate(source="cbr", code="JPY", nominal=100, value=55.0,
                   rate_date=date(2026, 8, 10))
        )
        session.flush()
        assert FxBook(session).rate("JPY") == pytest.approx(0.55)

    def test_unknown_currency(self, session):
        assert FxBook(session).rate("XYZ") is None

    def test_instrument_currency_uses_face_unit(self, session):
        bond = add_bond(session, "USDBOND", face_unit="USD", currency="SUR")
        # Оценка позиции идёт по валюте номинала, а не по валюте торгов
        assert instrument_currency(bond) == "USD"
        assert instrument_currency(add_bond(session, "RUBBOND")) == "RUB"


class TestCostMethods:
    """ФИФО против средней цены на одинаковом наборе сделок."""

    def _setup(self, session):
        instrument = add_share(session, "SBER")
        add_quote(session, instrument, last=300.0)
        session.add_all([
            Deal(secid="SBER", side="buy", quantity=100, price=100.0,
                 trade_date=date(2026, 1, 10)),
            Deal(secid="SBER", side="buy", quantity=100, price=200.0,
                 trade_date=date(2026, 2, 10)),
            Deal(secid="SBER", side="sell", quantity=100, price=250.0,
                 trade_date=date(2026, 3, 10)),
        ])
        session.flush()

    def test_fifo_sells_oldest_lot(self, session):
        self._setup(session)
        position = portfolio_service.compute_positions(session, method="fifo")[0]
        # Продан первый лот по 100 → результат (250 − 100) × 100
        assert position["realized_price_pnl_rub"] == pytest.approx(15000)
        # Остаётся второй лот по 200
        assert position["avg_price"] == pytest.approx(200.0)
        assert position["quantity"] == pytest.approx(100)

    def test_average_uses_mean_cost(self, session):
        self._setup(session)
        position = portfolio_service.compute_positions(session, method="average")[0]
        # Средняя цена 150 → результат (250 − 150) × 100
        assert position["realized_price_pnl_rub"] == pytest.approx(10000)
        assert position["avg_price"] == pytest.approx(150.0)

    def test_fifo_consumes_across_lots(self, session):
        instrument = add_share(session, "GAZP")
        add_quote(session, instrument, last=100.0)
        session.add_all([
            Deal(secid="GAZP", side="buy", quantity=50, price=100.0,
                 trade_date=date(2026, 1, 1)),
            Deal(secid="GAZP", side="buy", quantity=50, price=200.0,
                 trade_date=date(2026, 2, 1)),
            Deal(secid="GAZP", side="sell", quantity=75, price=300.0,
                 trade_date=date(2026, 3, 1)),
        ])
        session.flush()
        position = portfolio_service.compute_positions(session, method="fifo")[0]
        # 50 бумаг из лота по 100 и 25 из лота по 200
        expected = 50 * (300 - 100) + 25 * (300 - 200)
        assert position["realized_price_pnl_rub"] == pytest.approx(expected)
        assert position["quantity"] == pytest.approx(25)


class TestMultiCurrency:
    def test_splits_price_and_fx_result(self, session):
        bond = add_bond(session, "ZO", face=1000.0, face_unit="USD")
        add_quote(session, bond, last=90.0)
        add_fx(session, "USD", 80.0, date(2026, 3, 15))
        add_fx(session, "USD", 100.0, date(2026, 8, 10))
        session.add(
            Deal(secid="ZO", side="buy", quantity=10, price=80.0,
                 trade_date=date(2026, 3, 15))
        )
        session.flush()

        position = portfolio_service.compute_positions(session)[0]
        assert position["currency"] == "USD"
        # Ценовой: 10 × (90 − 80)% × 1000 USD × курс 100 = 100 000 ₽
        assert position["price_pnl_rub"] == pytest.approx(100_000)
        # Валютный: вложено 10 × 80% × 1000 USD = 8000 USD, курс вырос на 20
        assert position["fx_pnl_rub"] == pytest.approx(160_000)
        # Сумма компонентов равна общему нереализованному результату
        assert position["unrealized_pnl_rub"] == pytest.approx(260_000)
        # Оценка: 10 × 90% × 1000 × 100
        assert position["market_value_rub"] == pytest.approx(900_000)

    def test_rouble_bond_has_no_fx_component(self, session):
        bond = add_bond(session, "OFZ")
        add_quote(session, bond, last=60.0)
        session.add(
            Deal(secid="OFZ", side="buy", quantity=10, price=50.0,
                 trade_date=date(2026, 3, 1))
        )
        session.flush()
        position = portfolio_service.compute_positions(session)[0]
        assert position["fx_pnl_rub"] == pytest.approx(0)
        assert position["price_pnl_rub"] == pytest.approx(10 * 0.10 * 1000)

    def test_summary_totals_split(self, session):
        bond = add_bond(session, "ZO", face_unit="USD")
        add_quote(session, bond, last=100.0)
        add_fx(session, "USD", 80.0, date(2026, 3, 1))
        add_fx(session, "USD", 90.0, date(2026, 8, 10))
        session.add(
            Deal(secid="ZO", side="buy", quantity=10, price=100.0,
                 trade_date=date(2026, 3, 1))
        )
        session.flush()

        summary = portfolio_service.portfolio_summary(session)
        assert summary["price_pnl"] == pytest.approx(0)
        assert summary["fx_pnl"] == pytest.approx(10 * 1000 * 10)
        currencies = {row["key"]: row["share_pct"] for row in summary["allocation_currency"]}
        assert currencies["USD"] == pytest.approx(100.0)


class TestCouponIncome:
    def test_coupon_counted_on_held_quantity(self, session):
        bond = add_bond(session, "CPN")
        add_quote(session, bond, last=100.0)
        session.add_all([
            Deal(secid="CPN", side="buy", quantity=100, price=100.0,
                 trade_date=date(2026, 1, 1)),
            # Купон 40 ₽ на бумагу после покупки
            CorpAction(isin="RUCPN", action_type="coupon", value=40.0,
                       action_date=date(2026, 3, 1)),
        ])
        session.flush()

        position = portfolio_service.compute_positions(session)[0]
        assert position["coupon_income_rub"] == pytest.approx(4000)

    def test_coupon_before_purchase_ignored(self, session):
        bond = add_bond(session, "CPN")
        add_quote(session, bond, last=100.0)
        session.add_all([
            CorpAction(isin="RUCPN", action_type="coupon", value=40.0,
                       action_date=date(2026, 1, 1)),
            Deal(secid="CPN", side="buy", quantity=100, price=100.0,
                 trade_date=date(2026, 6, 1)),
        ])
        session.flush()
        position = portfolio_service.compute_positions(session)[0]
        assert position["coupon_income_rub"] == pytest.approx(0)

    def test_accrued_interest_is_not_converted(self, session):
        """НКД биржа отдаёт в рублях — повторно умножать на курс нельзя."""
        bond = add_bond(session, "ZO", face_unit="USD")
        add_fx(session, "USD", 80.0, date(2026, 8, 10))
        add_quote(session, bond, last=100.0, accrued_interest=1000.0)
        session.add(
            Deal(secid="ZO", side="buy", quantity=10, price=100.0,
                 accrued_interest=900.0, trade_date=date(2026, 8, 1))
        )
        session.flush()

        position = portfolio_service.compute_positions(session)[0]
        # 10 бумаг × 900 ₽ НКД, без умножения на 80
        assert position["accrued_paid_rub"] == pytest.approx(9000)
        assert position["accrued_now_rub"] == pytest.approx(10_000)
        # Купонный итог: текущий НКД минус уплаченный
        assert position["coupon_result_rub"] == pytest.approx(1000)

    def test_coupon_uses_exchange_rouble_value(self, session):
        bond = add_bond(session, "ZO", face_unit="USD")
        add_fx(session, "USD", 80.0, date(2026, 8, 10))
        add_quote(session, bond, last=100.0)
        session.add_all([
            Deal(secid="ZO", side="buy", quantity=10, price=100.0,
                 trade_date=date(2026, 1, 1)),
            # Купон 16 USD, биржа сразу даёт рублёвый эквивалент
            CorpAction(isin="RUZO", action_type="coupon", value=16.0,
                       value_rub=1300.0, action_date=date(2026, 3, 1)),
        ])
        session.flush()
        position = portfolio_service.compute_positions(session)[0]
        assert position["coupon_income_rub"] == pytest.approx(13_000)


class TestRisk:
    def test_modified_duration(self):
        assert risk_service.modified_duration(10.0, 15.0) == pytest.approx(10 / 1.15)
        assert risk_service.modified_duration(None, 15.0) is None

    def test_convexity_positive(self):
        convexity = risk_service.approximate_convexity(10.0, 15.0)
        assert convexity > 0

    def test_convexity_helps_in_both_directions(self, session):
        bond = add_bond(session, "LONG")
        add_quote(session, bond, last=100.0, duration_days=3650, yield_pct=10.0)
        session.add(
            Deal(secid="LONG", side="buy", quantity=1000, price=100.0,
                 trade_date=date(2026, 1, 1))
        )
        session.flush()

        result = risk_service.rate_sensitivity(session, shift_bp=(300,))
        up = next(s for s in result["scenarios"] if s["shift_bp"] == 300)
        down = next(s for s in result["scenarios"] if s["shift_bp"] == -300)
        assert up["impact_rub"] < 0 and down["impact_rub"] > 0
        # Выпуклость добавляет стоимость при движении в любую сторону
        assert up["convexity_effect_rub"] > 0
        assert down["convexity_effect_rub"] > 0
        # Рост ставок бьёт слабее, чем помогает их падение
        assert abs(up["impact_rub"]) < abs(down["impact_rub"])

    def test_curve_tilts_differ(self, session):
        short = add_bond(session, "SHORT")
        long = add_bond(session, "LONG")
        add_quote(session, short, last=100.0, duration_days=365, yield_pct=15.0)
        add_quote(session, long, last=100.0, duration_days=3650, yield_pct=15.0)
        session.add_all([
            Deal(secid="SHORT", side="buy", quantity=1000, price=100.0,
                 trade_date=date(2026, 1, 1)),
            Deal(secid="LONG", side="buy", quantity=1000, price=100.0,
                 trade_date=date(2026, 1, 1)),
        ])
        session.flush()

        result = risk_service.rate_sensitivity(session, shift_bp=(100,))
        parallel = next(s for s in result["scenarios"] if s["shift_bp"] == 100)
        steep = next(s for s in result["scenarios_steepening"] if s["shift_bp"] == 100)
        # При росте длинных ставок длинная бумага теряет больше
        assert steep["impact_rub"] < parallel["impact_rub"]

    def test_empty_portfolio(self, session):
        result = risk_service.rate_sensitivity(session)
        assert result["bond_value"] == 0
        assert result["weighted_duration_years"] is None


class TestCashflow:
    def test_future_payments_in_roubles(self, session):
        bond = add_bond(session, "CPN")
        add_quote(session, bond, last=100.0)
        soon = date.today() + timedelta(days=30)
        session.add_all([
            Deal(secid="CPN", side="buy", quantity=100, price=100.0,
                 trade_date=date(2026, 1, 1)),
            CorpAction(isin="RUCPN", action_type="coupon", value=40.0, action_date=soon),
            # Прошедшая выплата в календарь не попадает
            CorpAction(isin="RUCPN", action_type="coupon", value=40.0,
                       action_date=date(2020, 1, 1)),
        ])
        session.flush()

        result = risk_service.portfolio_cashflow(session, horizon_days=90)
        assert len(result["events"]) == 1
        assert result["total_rub"] == pytest.approx(4000)
        assert result["by_month"][0]["coupon_rub"] == pytest.approx(4000)

    def test_beyond_horizon_excluded(self, session):
        bond = add_bond(session, "CPN")
        add_quote(session, bond, last=100.0)
        session.add_all([
            Deal(secid="CPN", side="buy", quantity=100, price=100.0,
                 trade_date=date(2026, 1, 1)),
            CorpAction(isin="RUCPN", action_type="coupon", value=40.0,
                       action_date=date.today() + timedelta(days=200)),
        ])
        session.flush()
        assert risk_service.portfolio_cashflow(session, horizon_days=90)["events"] == []

    def test_empty_portfolio(self, session):
        assert risk_service.portfolio_cashflow(session)["total_rub"] == 0


class TestLimits:
    def _portfolio(self, session):
        for secid, price, issuer in (
            ("A", 100.0, "Эмитент 1"),
            ("B", 100.0, "Эмитент 1"),
            ("C", 100.0, "Эмитент 2"),
        ):
            instrument = add_share(session, secid, issuer=issuer, list_level=1)
            add_quote(session, instrument, last=price, turnover=1e8, num_trades=1000)
            session.add(
                Deal(secid=secid, side="buy", quantity=100, price=price,
                     trade_date=date(2026, 1, 1))
            )
        session.flush()

    def test_instrument_share_breach(self, session):
        self._portfolio(session)
        session.add(Limit(portfolio="Основной", kind="instrument_share", value=30.0))
        session.flush()

        result = limits_service.check_limits(session)
        # Каждая бумага занимает треть портфеля при лимите 30%
        assert result["breached"] == 3
        assert all(row["actual"] == pytest.approx(33.33, abs=0.01) for row in result["items"])

    def test_issuer_share_aggregates(self, session):
        self._portfolio(session)
        session.add(Limit(portfolio="Основной", kind="issuer_share", value=50.0))
        session.flush()

        result = limits_service.check_limits(session)
        first = next(row for row in result["items"] if row["subject"] == "Эмитент 1")
        # Два выпуска одного заёмщика складываются
        assert first["actual"] == pytest.approx(66.67, abs=0.01)
        assert first["breached"] is True

    def test_duration_min_breaches_downward(self, session):
        bond = add_bond(session, "SHORT")
        add_quote(session, bond, last=100.0, duration_days=182)
        session.add_all([
            Deal(secid="SHORT", side="buy", quantity=100, price=100.0,
                 trade_date=date(2026, 1, 1)),
            Limit(portfolio="Основной", kind="duration_min", value=3.0),
        ])
        session.flush()

        row = limits_service.check_limits(session)["items"][0]
        assert row["breached"] is True

    def test_no_breach_when_within(self, session):
        self._portfolio(session)
        session.add(Limit(portfolio="Основной", kind="instrument_share", value=50.0))
        session.flush()
        assert limits_service.check_limits(session)["breached"] == 0

    def test_preview_detects_new_breach(self, session):
        self._portfolio(session)
        session.add(Limit(portfolio="Основной", kind="instrument_share", value=40.0))
        session.flush()

        # Докупка сделает долю A заметно выше лимита
        result = limits_service.preview_deal(
            session, secid="A", quantity=500, price=100.0
        )
        assert result["allowed"] is False
        assert result["new_breaches"]

    def test_preview_allows_small_trade(self, session):
        self._portfolio(session)
        session.add(Limit(portfolio="Основной", kind="instrument_share", value=90.0))
        session.flush()
        result = limits_service.preview_deal(session, secid="A", quantity=1, price=100.0)
        assert result["allowed"] is True

    def test_limits_without_positions(self, session):
        session.add(Limit(portfolio="Основной", kind="instrument_share", value=10.0))
        session.flush()
        assert limits_service.check_limits(session)["items"] == []


class TestBenchmark:
    def _index(self, session, secid, values):
        instrument = Instrument(
            secid=secid, board="SNDX", engine="stock", market="index", kind="index",
            short_name=secid,
        )
        session.add(instrument)
        session.flush()
        start = date.today() - timedelta(days=len(values))
        for offset, (close, yield_pct) in enumerate(values):
            session.add(Bar(
                instrument_id=instrument.id,
                trade_date=start + timedelta(days=offset),
                close=close, yield_close=yield_pct, duration_days=1800,
            ))
        session.flush()

    def test_index_return(self, session):
        self._index(session, "RGBITR", [(100.0, 15.0), (110.0, 14.0)])
        rows = benchmark_service.index_summary(session, days=30)
        rgbitr = next(row for row in rows if row["secid"] == "RGBITR")
        assert rgbitr["return_pct"] == pytest.approx(10.0)
        assert rgbitr["duration_years"] == pytest.approx(4.93, abs=0.01)

    def test_missing_index_marked_unavailable(self, session):
        rows = benchmark_service.index_summary(session, days=30)
        assert all(row["available"] is False for row in rows)

    def test_spread_history(self, session):
        self._index(session, "RGBITR", [(100.0, 15.0), (100.0, 15.0)])
        bond = add_bond(session, "CORP")
        start = date.today() - timedelta(days=2)
        for offset, yield_pct in enumerate((18.0, 20.0)):
            session.add(Bar(
                instrument_id=bond.id,
                trade_date=start + timedelta(days=offset),
                close=100.0, yield_close=yield_pct,
            ))
        session.flush()

        result = benchmark_service.spread_history(session, "CORP", days=30)
        assert len(result["points"]) == 2
        # 18% против 15% = 300 бп, затем 20% против 15% = 500 бп
        assert result["points"][0]["spread_bp"] == pytest.approx(300)
        assert result["stats"]["current_bp"] == pytest.approx(500)
        assert result["stats"]["average_bp"] == pytest.approx(400)
        # Премия выше средней — бумага дешевле обычного
        assert result["stats"]["deviation_bp"] == pytest.approx(100)

    def test_spread_history_without_data(self, session):
        result = benchmark_service.spread_history(session, "NOSUCH", days=30)
        assert result["points"] == []
        assert result["stats"] == {}


class TestCapacity:
    def test_days_to_exit(self):
        # 30 000 бумаг при среднем обороте 10 000 и участии 30% — 10 дней
        assert portfolio_service._days_to_exit(30_000, 10_000) == pytest.approx(10.0)

    def test_without_volume(self):
        assert portfolio_service._days_to_exit(1000, None) is None
        assert portfolio_service._days_to_exit(0, 1000) is None


class TestFuturePaymentsExcluded:
    """График НРД содержит выплаты до погашения — будущие в результат не идут."""

    def test_future_coupon_not_counted_as_received(self, session):
        bond = add_bond(session, "FUT")
        add_quote(session, bond, last=100.0)
        session.add_all([
            Deal(secid="FUT", side="buy", quantity=100, price=100.0,
                 trade_date=date.today() - timedelta(days=30)),
            # Купон уже прошёл — считается
            CorpAction(isin="RUFUT", action_type="coupon", value=40.0,
                       action_date=date.today() - timedelta(days=10)),
            # Эти ещё не наступили — не считаются
            CorpAction(isin="RUFUT", action_type="coupon", value=40.0,
                       action_date=date.today() + timedelta(days=180)),
            CorpAction(isin="RUFUT", action_type="coupon", value=40.0,
                       action_date=date.today() + timedelta(days=360)),
        ])
        session.flush()

        position = portfolio_service.compute_positions(session)[0]
        assert position["coupon_income_rub"] == pytest.approx(4000)

    def test_future_amortization_not_counted(self, session):
        bond = add_bond(session, "AMR")
        add_quote(session, bond, last=100.0)
        session.add_all([
            Deal(secid="AMR", side="buy", quantity=100, price=100.0,
                 trade_date=date.today() - timedelta(days=30)),
            CorpAction(isin="RUAMR", action_type="amortization", value=1000.0,
                       action_date=date.today() + timedelta(days=100)),
        ])
        session.flush()
        position = portfolio_service.compute_positions(session)[0]
        assert position["amortization_rub"] == pytest.approx(0)

    def test_future_payments_still_in_cashflow(self, session):
        """Будущие выплаты исключены из P&L, но остаются в календаре."""
        bond = add_bond(session, "FUT")
        add_quote(session, bond, last=100.0)
        session.add_all([
            Deal(secid="FUT", side="buy", quantity=100, price=100.0,
                 trade_date=date.today() - timedelta(days=30)),
            CorpAction(isin="RUFUT", action_type="coupon", value=40.0,
                       action_date=date.today() + timedelta(days=60)),
        ])
        session.flush()

        assert portfolio_service.compute_positions(session)[0]["coupon_income_rub"] == 0
        assert risk_service.portfolio_cashflow(session)["total_rub"] == pytest.approx(4000)
