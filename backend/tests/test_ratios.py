"""Нормативы ликвидности, обеспечение Банка России и график выплат.

Проверяем то, где ошибка дороже всего: формулы нормативов (их сверяют с
отчётностью), отличие залоговой бумаги от незалоговой в расчёте ликвидности
и форму «пилы» НКД — по ней читают, сколько переплатишь при покупке.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CbrCollateral, CorpAction, Deal, Instrument, Quote
from app.services import collateral as collateral_service
from app.services import payments as payments_service
from app.services import ratios as ratios_service


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        yield active


def add_bond(session, secid="SU26245RMFS9", *, isin=None, face=1000.0, period=182):
    instrument = Instrument(
        secid=secid, board="TQOB", engine="stock", market="bonds", kind="bond",
        short_name=secid, isin=isin or f"RU000{secid[:9]}", face_value=face,
        face_unit="SUR", currency="SUR", coupon_period=period,
    )
    session.add(instrument)
    session.flush()
    return instrument


def add_collateral(session, isin, *, haircut=0.98, value_rub=900.0, price_pct=90.0):
    row = CbrCollateral(
        isin=isin, issuer="МИНФИН", price_pct=price_pct, value_rub=value_rub,
        haircut=haircut, mechanism="ОМ", group_title="Гособлигации",
        as_of=date.today(),
    )
    session.add(row)
    session.flush()
    return row


# ----------------------------------------------------------------------
# Формулы нормативов
# ----------------------------------------------------------------------
class TestFormulas:
    """Формулы 199-И. Числа подобраны так, чтобы результат считался в уме."""

    def test_h2_instant_liquidity(self):
        # Лам 300, Овм 2000, Овм* 1000 → 300 / (2000 − 500) = 20%
        ratios = ratios_service.compute(
            {"ovm": 2000, "ovm_min": 1000}, lam_portfolio=300
        )
        h2 = ratios[0]
        assert h2.code == "Н2"
        assert h2.value == pytest.approx(20.0)
        assert h2.compliant is True  # предел 15%

    def test_h2_breach_is_detected(self):
        ratios = ratios_service.compute(
            {"ovm": 2000, "ovm_min": 0}, lam_portfolio=200
        )
        h2 = ratios[0]
        assert h2.value == pytest.approx(10.0)
        assert h2.compliant is False
        assert h2.cushion == pytest.approx(-5.0)

    def test_h3_current_liquidity(self):
        # Лат 600, Овт 2000, Овт* 800 → 600 / (2000 − 400) = 37,5%
        ratios = ratios_service.compute(
            {"ovt": 2000, "ovt_min": 800}, lam_portfolio=600
        )
        h3 = ratios[1]
        assert h3.value == pytest.approx(37.5)
        assert h3.compliant is False  # предел 50%

    def test_h4_is_a_ceiling_not_a_floor(self):
        """У Н4 предел сверху: превышение — нарушение, а не запас."""
        # Крд 1000, К 400, ОД 400, О* 400 → 1000 / (400+400+200) = 100%
        ratios = ratios_service.compute(
            {"krd": 1000, "capital": 400, "od": 400, "o_min": 400}
        )
        h4 = ratios[2]
        assert h4.direction == "maximum"
        assert h4.value == pytest.approx(100.0)
        assert h4.compliant is True

        breached = ratios_service.compute(
            {"krd": 1500, "capital": 400, "od": 400, "o_min": 400}
        )[2]
        assert breached.value == pytest.approx(150.0)
        assert breached.compliant is False

    def test_zero_denominator_is_undefined_not_zero(self):
        """Без обязательств норматив не считается.

        Ноль означал бы нарушение там, где его нет.
        """
        ratios = ratios_service.compute({"ovm": 0}, lam_portfolio=500)
        assert ratios[0].value is None
        assert ratios[0].compliant is None

    def test_portfolio_assets_add_to_manual_ones(self):
        """Лам = посчитанное терминалом плюс введённое руками."""
        ratios = ratios_service.compute(
            {"ovm": 1000, "lam_other": 100}, lam_portfolio=400
        )
        assert ratios[0].numerator == pytest.approx(500)


# ----------------------------------------------------------------------
# Обеспечение
# ----------------------------------------------------------------------
class TestCollateral:
    def test_pledge_value_applies_haircut(self):
        """Под бумагу дают не полную оценку, а с поправочным коэффициентом."""
        value = collateral_service.pledge_value(
            100, 1000.0, {"value_rub": 900.0, "haircut": 0.9}
        )
        assert value == pytest.approx(81_000)  # 100 × 900 × 0,9

    def test_pledge_value_falls_back_to_price_and_face(self):
        value = collateral_service.pledge_value(
            10, 1000.0, {"price_pct": 80.0, "haircut": 1.0}
        )
        assert value == pytest.approx(8_000)

    def test_unknown_haircut_gives_no_number(self):
        """Считать неизвестный коэффициент единицей — завысить ликвидность."""
        assert collateral_service.pledge_value(
            10, 1000.0, {"value_rub": 900.0, "haircut": None}
        ) is None

    def test_security_outside_the_list_is_not_pledgeable(self):
        assert collateral_service.pledge_value(10, 1000.0, None) is None

    def test_portfolio_splits_eligible_and_not(self, session):
        eligible = add_bond(session, "OFZ1", isin="RU000OFZ0001")
        plain = add_bond(session, "CORP1", isin="RU000CORP001")
        add_collateral(session, "RU000OFZ0001", haircut=0.98, value_rub=900.0)

        for instrument in (eligible, plain):
            session.add(Quote(
                instrument_id=instrument.id, ts=datetime.utcnow(),
                last=90.0, wa_price=90.0,
            ))
            session.add(Deal(
                portfolio="Основной", secid=instrument.secid, side="buy",
                quantity=1000, price=88.0, trade_date=date.today() - timedelta(days=30),
            ))
        session.commit()

        result = collateral_service.portfolio_collateral(session)
        assert result["eligible_positions"] == 1
        assert result["total_positions"] == 2
        # 1000 бумаг × 900 ₽ × 0,98
        assert result["pledgeable_rub"] == pytest.approx(882_000)


# ----------------------------------------------------------------------
# Пересчёт под сделку
# ----------------------------------------------------------------------
class TestSimulation:
    """Ради этого расчёт и нужен: видно цену решения до сделки."""

    def _prepare(self, session):
        bond = add_bond(session, "OFZ1", isin="RU000OFZ0001")
        add_collateral(session, "RU000OFZ0001", haircut=0.9, value_rub=1000.0)
        session.add(Quote(
            instrument_id=bond.id, ts=datetime.utcnow(),
            last=100.0, wa_price=100.0,
        ))
        session.add(Deal(
            portfolio="Основной", secid="OFZ1", side="buy", quantity=10_000,
            price=100.0, trade_date=date.today() - timedelta(days=30),
        ))
        ratios_service.save_inputs(session, {
            "ovm": 50_000_000, "ovt": 60_000_000,
            "krd": 10_000_000, "capital": 20_000_000,
        })
        session.commit()

    def test_eligible_purchase_costs_less_liquidity(self, session):
        """Залоговая бумага возвращает часть денег: ЦБ даст их под залог."""
        self._prepare(session)

        eligible = ratios_service.simulate(session, amount_rub=1_000_000, eligible=True)
        plain = ratios_service.simulate(session, amount_rub=1_000_000, eligible=False)

        assert eligible["liquidity_delta_rub"] > plain["liquidity_delta_rub"]
        # Незалоговая выводит всю сумму из ликвидности
        assert plain["liquidity_delta_rub"] == pytest.approx(-1_000_000)

    def test_impossible_purchase_is_flagged_not_shown_negative(self, session):
        """Отрицательных ликвидных активов не бывает — сделка неисполнима."""
        self._prepare(session)

        result = ratios_service.simulate(
            session, amount_rub=999_000_000_000, eligible=False
        )
        assert result["insufficient"] is True
        assert result["shortfall_rub"] > 0
        for ratio in result["after"]["ratios"]:
            if ratio["value"] is not None:
                assert ratio["value"] >= 0

    def test_changes_point_at_new_breaches(self, session):
        """Не сличать два списка глазами, а сразу видеть, что сломается."""
        self._prepare(session)
        result = ratios_service.simulate(
            session, amount_rub=10_000_000, eligible=False
        )
        codes = {change["code"] for change in result["changes"]}
        assert codes == {"Н2", "Н3", "Н4"}
        # Н4 от сделки с бумагами не зависит
        h4 = next(c for c in result["changes"] if c["code"] == "Н4")
        assert h4["delta"] in (0, None) or h4["delta"] == pytest.approx(0)


# ----------------------------------------------------------------------
# График выплат
# ----------------------------------------------------------------------
class TestPaymentSchedule:
    def _with_coupons(self, session, count=4, value=50.0, period=182):
        bond = add_bond(session, period=period)
        start = date.today() - timedelta(days=period)
        for index in range(count):
            session.add(CorpAction(
                isin=bond.isin, secid=bond.secid, action_type="coupon",
                action_date=start + timedelta(days=period * index),
                value=value, face_unit="SUR", source="nsd",
            ))
        session.commit()
        return bond

    def test_payments_are_scaled_by_quantity(self, session):
        bond = self._with_coupons(session, count=3, value=50.0)
        schedule = payments_service.payment_schedule(session, bond, quantity=200)
        assert schedule["payments"][0]["amount"] == pytest.approx(10_000)

    def test_accrual_resets_at_every_payment(self, session):
        """Пила: НКД растёт внутри периода и обнуляется в день выплаты."""
        bond = self._with_coupons(session, count=4, value=50.0)
        curve = payments_service.payment_schedule(session, bond)["accrual"]

        # Падения — соседние точки с одной датой и снижением значения
        drops = [
            (left, right)
            for left, right in zip(curve, curve[1:])
            if left["date"] == right["date"] and right["value"] < left["value"]
        ]
        assert drops, "на графике не видно ни одного обнуления НКД"
        for left, right in drops:
            assert right["value"] == 0.0

    def test_accrual_grows_monotonically_inside_a_period(self, session):
        bond = self._with_coupons(session, count=3, value=50.0)
        curve = payments_service.payment_schedule(session, bond)["accrual"]

        for left, right in zip(curve, curve[1:]):
            if left["date"] < right["date"]:
                assert right["value"] >= left["value"], "НКД убывает внутри периода"

    def test_accrual_reaches_the_full_coupon(self, session):
        bond = self._with_coupons(session, count=3, value=50.0)
        curve = payments_service.payment_schedule(session, bond)["accrual"]
        assert max(point["value"] for point in curve) == pytest.approx(50.0)

    def test_offers_are_not_counted_as_money(self, session):
        """Оферта — право предъявить бумагу, денег в этот день может не быть."""
        bond = self._with_coupons(session, count=2)
        session.add(CorpAction(
            isin=bond.isin, secid=bond.secid, action_type="offer",
            action_date=date.today() + timedelta(days=90), source="nsd",
        ))
        session.commit()

        schedule = payments_service.payment_schedule(session, bond)
        assert all(item["kind"] != "offer" for item in schedule["payments"])
        assert len(schedule["offers"]) == 1

    def test_totals_split_past_and_future(self, session):
        bond = self._with_coupons(session, count=4, value=50.0)
        totals = payments_service.payment_schedule(session, bond, quantity=10)["totals"]
        assert totals["payments"] == 4
        assert totals["upcoming"] < totals["payments"]
        assert totals["next_date"] is not None
        assert totals["average_coupon"] == pytest.approx(500)

    def test_no_schedule_is_not_an_error(self, session):
        bond = add_bond(session, "NOCOUPON")
        schedule = payments_service.payment_schedule(session, bond)
        assert schedule["payments"] == []
        assert schedule["accrual"] == []

    def test_single_coupon_uses_declared_period(self, session):
        """Начало первого периода из графика НРД не узнать — берём из справочника."""
        bond = self._with_coupons(session, count=1, value=50.0, period=91)
        curve = payments_service.payment_schedule(session, bond)["accrual"]
        assert curve, "по единственному купону линия не построена"
        span = (curve[-1]["date"] - curve[0]["date"]).days
        assert span == pytest.approx(91, abs=1)


class TestInputsStorage:
    def test_inputs_are_kept_between_sessions(self, session):
        ratios_service.save_inputs(session, {"ovm": 1_000, "capital": 2_000})
        loaded = ratios_service.load_inputs(session)
        assert loaded["ovm"] == 1_000
        assert loaded["capital"] == 2_000

    def test_same_date_is_overwritten_not_duplicated(self, session):
        today = date.today()
        ratios_service.save_inputs(session, {"as_of": today, "ovm": 1_000})
        ratios_service.save_inputs(session, {"as_of": today, "ovm": 2_000})
        assert ratios_service.load_inputs(session, today)["ovm"] == 2_000

    def test_missing_inputs_leave_ratio_uncomputed(self, session):
        """Пустая форма — не нарушение, а «данных пока нет»."""
        report = ratios_service.report(session)
        assert set(report["incomplete"]) == {"Н2", "Н3", "Н4"}
        assert report["breaches"] == []
