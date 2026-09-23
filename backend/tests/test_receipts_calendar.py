"""Календарь поступлений: купоны, погашение, флоатеры, прошлые выплаты.

Отдельный файл, потому что проверять здесь надо не «функция вернула список», а
поведение календаря во времени: бумагу купили — выплаты появились, продали —
будущие ушли, а прошлые остались за период владения; у флоатера сначала есть
только дата, потом биржа объявляет сумму, и она должна встать на место.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CorpAction, Deal, Instrument
from app.services import coupons as coupons_service
from app.services import risk as risk_service

TODAY = date(2026, 9, 23)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        yield active


def add_bond(session, secid, *, isin=None, face_unit="SUR", kind="bond"):
    instrument = Instrument(
        secid=secid,
        board="TQCB",
        engine="stock",
        market="bonds",
        kind=kind,
        short_name=secid,
        isin=isin or f"RU000{secid}",
        face_value=1000.0,
        face_unit=face_unit,
    )
    session.add(instrument)
    session.flush()
    return instrument


def add_deal(session, secid, *, side="buy", quantity=100.0, on=TODAY, portfolio="Основной"):
    deal = Deal(
        portfolio=portfolio,
        secid=secid,
        side=side,
        quantity=quantity,
        price=100.0,
        trade_date=on,
    )
    session.add(deal)
    session.flush()
    return deal


def add_action(
    session,
    isin,
    *,
    on,
    action_type="coupon",
    value=30.0,
    record_date=None,
    data_source=None,
    value_pct=None,
):
    action = CorpAction(
        isin=isin,
        secid=None,
        name="тестовый выпуск",
        action_type=action_type,
        action_date=on,
        record_date=record_date,
        value=value,
        value_pct=value_pct,
        data_source=data_source,
        source="nsd",
    )
    session.add(action)
    session.flush()
    return action


def calendar(session, **kwargs):
    kwargs.setdefault("today", TODAY)
    return coupons_service.receipts_calendar(session, **kwargs)


def dates(result):
    return [row["action_date"] for row in result["events"]]


# ----------------------------------------------------------------------
# Остаток бумаг на дату
# ----------------------------------------------------------------------
class TestHoldings:
    def test_empty_without_deals(self, session):
        holdings = coupons_service.Holdings([])
        assert holdings.secids == []
        assert holdings.quantity_on("ANY", TODAY) == 0

    def test_nothing_before_first_deal(self, session):
        add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY)
        holdings = coupons_service.Holdings(session.query(Deal).all())
        assert holdings.quantity_on("AAA", TODAY - timedelta(days=1)) == 0
        assert holdings.quantity_on("AAA", TODAY) == 100

    def test_sale_reduces_balance_from_its_date(self, session):
        add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=100, on=TODAY - timedelta(days=30))
        add_deal(session, "AAA", side="sell", quantity=40, on=TODAY - timedelta(days=10))
        holdings = coupons_service.Holdings(session.query(Deal).all())
        assert holdings.quantity_on("AAA", TODAY - timedelta(days=11)) == 100
        assert holdings.quantity_on("AAA", TODAY - timedelta(days=10)) == 60
        assert holdings.quantity_on("AAA", TODAY) == 60

    def test_several_deals_on_one_day_are_netted(self, session):
        add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=100, on=TODAY)
        add_deal(session, "AAA", quantity=50, on=TODAY)
        add_deal(session, "AAA", side="sell", quantity=30, on=TODAY)
        holdings = coupons_service.Holdings(session.query(Deal).all())
        assert holdings.quantity_on("AAA", TODAY) == 120


# ----------------------------------------------------------------------
# Основное поведение календаря
# ----------------------------------------------------------------------
class TestFutureCoupons:
    def test_empty_portfolio_gives_empty_calendar(self, session):
        result = calendar(session)
        assert result["events"] == []
        assert result["total_rub"] == 0
        assert result["received_rub"] == 0

    def test_future_coupon_is_multiplied_by_quantity(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=100, on=TODAY - timedelta(days=5))
        add_action(session, bond.isin, on=TODAY + timedelta(days=30), value=30.0)

        result = calendar(session)
        assert len(result["events"]) == 1
        event = result["events"][0]
        assert event["amount_rub"] == 3000.0
        assert event["quantity"] == 100
        assert event["status"] == "announced"
        assert result["total_rub"] == 3000.0

    def test_coupon_beyond_horizon_is_not_shown(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY)
        add_action(session, bond.isin, on=TODAY + timedelta(days=400))
        assert calendar(session, horizon_days=365)["events"] == []
        assert len(calendar(session, horizon_days=500)["events"]) == 1

    def test_security_outside_portfolio_is_ignored(self, session):
        outsider = add_bond(session, "ZZZ")
        add_action(session, outsider.isin, on=TODAY + timedelta(days=30))
        assert calendar(session)["events"] == []

    def test_other_portfolio_is_not_counted(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY, portfolio="Дочерний")
        add_action(session, bond.isin, on=TODAY + timedelta(days=30))

        assert calendar(session, portfolio="Основной")["events"] == []
        assert len(calendar(session, portfolio="Дочерний")["events"]) == 1
        # Без имени портфеля считаем все сразу
        assert len(calendar(session)["events"]) == 1


class TestRetrospective:
    def test_paid_coupon_is_shown_and_counted_separately(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=10, on=TODAY - timedelta(days=200))
        add_action(session, bond.isin, on=TODAY - timedelta(days=100), value=25.0)

        result = calendar(session)
        assert len(result["events"]) == 1
        event = result["events"][0]
        assert event["is_past"] is True
        assert event["status"] == "paid"
        assert event["amount_rub"] == 250.0
        # Прошлое не попадает в «объявлено впереди»
        assert result["received_rub"] == 250.0
        assert result["total_rub"] == 0

    def test_coupon_before_purchase_is_not_ours(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY - timedelta(days=30))
        add_action(session, bond.isin, on=TODAY - timedelta(days=100))
        assert calendar(session)["events"] == []

    def test_past_days_zero_hides_history(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY - timedelta(days=200))
        add_action(session, bond.isin, on=TODAY - timedelta(days=100))
        add_action(session, bond.isin, on=TODAY + timedelta(days=100))

        result = calendar(session, past_days=0)
        assert dates(result) == [TODAY + timedelta(days=100)]

    def test_payment_today_counts_as_received(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=1, on=TODAY - timedelta(days=10))
        add_action(session, bond.isin, on=TODAY, value=12.0)

        event = calendar(session)["events"][0]
        assert event["is_past"] is True
        assert event["status"] == "paid"

    def test_record_date_decides_entitlement(self, session):
        """Купон получает тот, кто держал бумагу на дату фиксации.

        Бумага продана между фиксацией и выплатой: деньги всё равно наши.
        """
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=50, on=TODAY - timedelta(days=60))
        add_deal(session, "AAA", side="sell", quantity=50, on=TODAY - timedelta(days=20))
        add_action(
            session,
            bond.isin,
            on=TODAY - timedelta(days=15),
            record_date=TODAY - timedelta(days=25),
            value=20.0,
        )

        result = calendar(session)
        assert len(result["events"]) == 1
        assert result["events"][0]["amount_rub"] == 1000.0


class TestFloatingCoupon:
    def test_future_coupon_without_amount_keeps_its_date(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=100, on=TODAY)
        add_action(session, bond.isin, on=TODAY + timedelta(days=90), value=None)

        result = calendar(session)
        assert len(result["events"]) == 1
        event = result["events"][0]
        assert event["action_date"] == TODAY + timedelta(days=90)
        assert event["amount_rub"] is None
        assert event["status"] == "unknown"
        assert result["unknown_count"] == 1
        # Неизвестная сумма не попадает в объявленные поступления
        assert result["total_rub"] == 0

    def test_estimate_comes_from_last_known_coupon(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=100, on=TODAY - timedelta(days=200))
        add_action(session, bond.isin, on=TODAY - timedelta(days=100), value=41.0)
        add_action(session, bond.isin, on=TODAY + timedelta(days=90), value=None)

        result = calendar(session)
        future = [row for row in result["events"] if not row["is_past"]][0]
        assert future["amount_estimate_rub"] == 4100.0
        assert result["estimated_rub"] == 4100.0
        assert result["total_rub"] == 0

    def test_amount_appears_once_the_exchange_publishes_it(self, session):
        """Главный сценарий из задачи: наступил день — сумма встала на место."""
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=100, on=TODAY - timedelta(days=30))
        action = add_action(session, bond.isin, on=TODAY + timedelta(days=10), value=None)

        before = calendar(session)["events"][0]
        assert before["amount_rub"] is None
        assert before["status"] == "unknown"

        # Биржа объявила ставку — сборщик перезаписал value
        action.value = 37.5
        session.flush()

        after = calendar(session)["events"][0]
        assert after["amount_rub"] == 3750.0
        assert after["status"] == "announced"

    def test_past_payment_without_amount_is_marked_awaiting(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY - timedelta(days=60))
        add_action(session, bond.isin, on=TODAY - timedelta(days=1), value=None)

        event = calendar(session)["events"][0]
        assert event["status"] == "awaiting"
        assert event["amount_rub"] is None


class TestMaturity:
    def test_maturity_marker_from_exchange(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=10, on=TODAY)
        add_action(
            session,
            bond.isin,
            on=TODAY + timedelta(days=200),
            action_type="amortization",
            value=1000.0,
            value_pct=100,
            data_source="maturity",
        )

        result = calendar(session)
        assert result["events"][0]["action_type"] == "maturity"
        assert result["events"][0]["action_title"] == "Погашение"
        assert result["maturity_rub"] == 10000.0
        assert result["amortization_rub"] == 0

    def test_partial_amortization_stays_amortization(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=10, on=TODAY)
        add_action(
            session,
            bond.isin,
            on=TODAY + timedelta(days=200),
            action_type="amortization",
            value=250.0,
            value_pct=25,
        )

        result = calendar(session)
        assert result["events"][0]["action_type"] == "amortization"
        assert result["amortization_rub"] == 2500.0
        assert result["maturity_rub"] == 0

    def test_full_repayment_without_marker_is_still_maturity(self, session):
        """Записи, загруженные прежней версией, признака от биржи не имеют."""
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=10, on=TODAY)
        add_action(
            session,
            bond.isin,
            on=TODAY + timedelta(days=200),
            action_type="amortization",
            value=1000.0,
            value_pct=100,
            data_source=None,
        )
        assert calendar(session)["events"][0]["action_type"] == "maturity"


# ----------------------------------------------------------------------
# Реакция на изменение состава портфеля
# ----------------------------------------------------------------------
class TestPortfolioChanges:
    def test_new_security_brings_its_payments(self, session):
        first = add_bond(session, "AAA")
        second = add_bond(session, "BBB")
        add_action(session, first.isin, on=TODAY + timedelta(days=30))
        add_action(session, second.isin, on=TODAY + timedelta(days=45))

        add_deal(session, "AAA", on=TODAY)
        assert [row["secid"] for row in calendar(session)["events"]] == ["AAA"]

        add_deal(session, "BBB", on=TODAY)
        assert [row["secid"] for row in calendar(session)["events"]] == ["AAA", "BBB"]

    def test_full_sale_removes_future_payments_but_keeps_past(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=100, on=TODAY - timedelta(days=200))
        add_action(session, bond.isin, on=TODAY - timedelta(days=100), value=30.0)
        add_action(session, bond.isin, on=TODAY + timedelta(days=100), value=30.0)

        assert len(calendar(session)["events"]) == 2

        add_deal(session, "AAA", side="sell", quantity=100, on=TODAY)
        after = calendar(session)
        assert dates(after) == [TODAY - timedelta(days=100)]
        assert after["received_rub"] == 3000.0
        assert after["total_rub"] == 0

    def test_partial_sale_scales_future_payments(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=100, on=TODAY - timedelta(days=10))
        add_action(session, bond.isin, on=TODAY + timedelta(days=30), value=30.0)
        assert calendar(session)["total_rub"] == 3000.0

        add_deal(session, "AAA", side="sell", quantity=70, on=TODAY)
        assert calendar(session)["total_rub"] == 900.0

    def test_deleting_the_deal_empties_the_calendar(self, session):
        bond = add_bond(session, "AAA")
        deal = add_deal(session, "AAA", on=TODAY - timedelta(days=10))
        add_action(session, bond.isin, on=TODAY + timedelta(days=30))
        assert len(calendar(session)["events"]) == 1

        session.delete(deal)
        session.flush()
        assert calendar(session)["events"] == []

    def test_buying_back_restores_future_payments(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=50, on=TODAY - timedelta(days=100))
        add_deal(session, "AAA", side="sell", quantity=50, on=TODAY - timedelta(days=50))
        add_action(session, bond.isin, on=TODAY + timedelta(days=30), value=10.0)
        assert calendar(session)["total_rub"] == 0

        add_deal(session, "AAA", quantity=20, on=TODAY)
        assert calendar(session)["total_rub"] == 200.0


class TestMissingSchedules:
    def test_bond_without_schedule_is_reported(self, session):
        add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY)
        result = calendar(session)
        assert result["events"] == []
        assert [row["secid"] for row in result["missing"]] == ["AAA"]

    def test_bond_with_schedule_is_not_reported(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY)
        add_action(session, bond.isin, on=TODAY + timedelta(days=30))
        assert calendar(session)["missing"] == []

    def test_bond_without_isin_is_reported(self, session):
        instrument = add_bond(session, "AAA")
        instrument.isin = None
        session.flush()
        add_deal(session, "AAA", on=TODAY)
        result = calendar(session)
        assert result["missing"] == [{"secid": "AAA", "reason": "нет ISIN в справочнике"}]

    def test_sold_out_bond_is_not_reported_as_missing(self, session):
        add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=10, on=TODAY - timedelta(days=30))
        add_deal(session, "AAA", side="sell", quantity=10, on=TODAY - timedelta(days=5))
        assert calendar(session)["missing"] == []

    def test_share_without_schedule_is_not_reported(self, session):
        add_bond(session, "SBER", kind="share")
        add_deal(session, "SBER", on=TODAY)
        assert calendar(session)["missing"] == []


class TestRefreshTargets:
    def test_portfolio_isins_lists_only_what_is_held(self, session):
        held = add_bond(session, "AAA")
        add_bond(session, "BBB")
        add_deal(session, "AAA", quantity=10, on=TODAY)
        add_deal(session, "BBB", quantity=10, on=TODAY - timedelta(days=30))
        add_deal(session, "BBB", side="sell", quantity=10, on=TODAY - timedelta(days=1))

        assert coupons_service.portfolio_isins(session) == [held.isin]

    def test_isins_without_schedule_skips_loaded_ones(self, session):
        loaded = add_bond(session, "AAA")
        fresh = add_bond(session, "BBB")
        add_action(session, loaded.isin, on=TODAY + timedelta(days=30))

        assert coupons_service.isins_without_schedule(session, ["AAA", "BBB"]) == [
            fresh.isin
        ]

    def test_empty_input_asks_for_nothing(self, session):
        assert coupons_service.isins_without_schedule(session, []) == []
        assert coupons_service.portfolio_isins(session) == []


class TestMonthlyBuckets:
    def test_months_split_by_kind(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=10, on=TODAY - timedelta(days=5))
        add_action(session, bond.isin, on=date(2026, 10, 15), value=30.0)
        add_action(
            session,
            bond.isin,
            on=date(2026, 10, 20),
            action_type="amortization",
            value=1000.0,
            value_pct=100,
            data_source="maturity",
        )

        months = {row["month"]: row for row in calendar(session)["by_month"]}
        october = months["2026-10"]
        assert october["coupon_rub"] == 300.0
        assert october["maturity_rub"] == 10000.0
        assert october["total_rub"] == 10300.0
        assert october["is_past"] is False

    def test_estimate_goes_to_its_own_bucket(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=10, on=TODAY - timedelta(days=200))
        add_action(session, bond.isin, on=TODAY - timedelta(days=90), value=20.0)
        add_action(session, bond.isin, on=date(2026, 12, 10), value=None)

        months = {row["month"]: row for row in calendar(session)["by_month"]}
        december = months["2026-12"]
        assert december["total_rub"] == 0
        assert december["estimated_rub"] == 200.0

    def test_past_month_is_marked(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=1, on=date(2026, 1, 10))
        add_action(session, bond.isin, on=date(2026, 3, 15), value=10.0)

        months = {row["month"]: row for row in calendar(session)["by_month"]}
        assert months["2026-03"]["is_past"] is True


class TestBackwardCompatibility:
    def test_risk_helper_still_returns_only_the_future(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", quantity=10, on=date(2026, 1, 10))
        add_action(session, bond.isin, on=date(2026, 3, 15), value=10.0)
        add_action(session, bond.isin, on=date(2099, 3, 15), value=10.0)

        result = risk_service.portfolio_cashflow(session, horizon_days=365)
        assert result["events"] == []
        assert set(result) >= {"total_rub", "coupon_rub", "amortization_rub",
                               "events", "by_month", "horizon_days"}

    def test_offers_are_not_receipts(self, session):
        bond = add_bond(session, "AAA")
        add_deal(session, "AAA", on=TODAY)
        add_action(
            session, bond.isin, on=TODAY + timedelta(days=30), action_type="offer"
        )
        assert calendar(session)["events"] == []
