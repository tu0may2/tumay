"""Срочный рынок: модель опционов, расчёт позиций и профиль выплат.

Модель проверяется не «примерно похоже», а через тождества, которые обязаны
выполняться точно: паритет пут-колл, восстановление волатильности из цены,
соотношения греков. Если формулу однажды перепишут с опечаткой, эти равенства
сломаются, а глазами такое не ловится.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.services import derivatives as d


def _contract(**overrides) -> d.Contract:
    base = dict(
        secid="SR270CH6",
        name="Call 270",
        kind="option",
        asset_code="SBRF",
        expiry=date.today() + timedelta(days=30),
        min_step=1.0,
        step_price=1.0,
        last=100.0,
        settle_price=100.0,
        margin=500.0,
        open_position=1000,
        fee=1.0,
        strike=270.0,
        option_type="C",
        underlying="SBER",
        underlying_price=272.0,
    )
    base.update(overrides)
    return d.Contract(**base)


class TestBlack76:
    F, K, T, VOL, RATE = 3286.0, 3250.0, 30 / 365, 0.35, 0.14

    def test_put_call_parity(self):
        """C − P = disc·(F − K). Нарушение означает ошибку в формуле."""
        call = d.black76_price(self.F, self.K, self.T, self.VOL, self.RATE, True)
        put = d.black76_price(self.F, self.K, self.T, self.VOL, self.RATE, False)
        expected = math.exp(-self.RATE * self.T) * (self.F - self.K)
        assert call - put == pytest.approx(expected, abs=1e-9)

    def test_price_grows_with_volatility(self):
        low = d.black76_price(self.F, self.K, self.T, 0.20, self.RATE, True)
        high = d.black76_price(self.F, self.K, self.T, 0.60, self.RATE, True)
        assert high > low

    def test_at_expiry_only_intrinsic_value_remains(self):
        call = d.black76_price(3300.0, 3250.0, 0.0, self.VOL, self.RATE, True)
        put = d.black76_price(3300.0, 3250.0, 0.0, self.VOL, self.RATE, False)
        assert call == pytest.approx(50.0)
        assert put == pytest.approx(0.0)

    def test_zero_volatility_does_not_divide_by_zero(self):
        assert d.black76_price(3300.0, 3250.0, self.T, 0.0, self.RATE, True) == 50.0


class TestGreeks:
    F, K, T, VOL, RATE = 3286.0, 3250.0, 30 / 365, 0.35, 0.14

    def test_call_and_put_delta_differ_by_discount(self):
        call = d.black76_greeks(self.F, self.K, self.T, self.VOL, self.RATE, True)
        put = d.black76_greeks(self.F, self.K, self.T, self.VOL, self.RATE, False)
        expected = math.exp(-self.RATE * self.T)
        assert call["delta"] - put["delta"] == pytest.approx(expected, abs=1e-9)

    def test_gamma_and_vega_are_the_same_for_call_and_put(self):
        call = d.black76_greeks(self.F, self.K, self.T, self.VOL, self.RATE, True)
        put = d.black76_greeks(self.F, self.K, self.T, self.VOL, self.RATE, False)
        assert call["gamma"] == pytest.approx(put["gamma"])
        assert call["vega"] == pytest.approx(put["vega"])

    def test_time_works_against_the_buyer(self):
        """Тета отрицательна: купленный опцион дешевеет каждый день."""
        for is_call in (True, False):
            greeks = d.black76_greeks(self.F, self.K, self.T, self.VOL, self.RATE, is_call)
            assert greeks["theta"] < 0

    def test_delta_matches_numeric_derivative(self):
        """Дельта — это производная цены по цене базового актива."""
        step = 0.01
        up = d.black76_price(self.F + step, self.K, self.T, self.VOL, self.RATE, True)
        down = d.black76_price(self.F - step, self.K, self.T, self.VOL, self.RATE, True)
        numeric = (up - down) / (2 * step)
        analytic = d.black76_greeks(self.F, self.K, self.T, self.VOL, self.RATE, True)["delta"]
        assert analytic == pytest.approx(numeric, abs=1e-6)

    def test_expired_option_has_binary_delta(self):
        greeks = d.black76_greeks(3300.0, 3250.0, 0.0, self.VOL, self.RATE, True)
        assert greeks == {"delta": 1.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}


class TestImpliedVol:
    F, K, T, RATE = 3286.0, 3250.0, 30 / 365, 0.14

    @pytest.mark.parametrize("vol", [0.15, 0.35, 0.80, 1.5])
    @pytest.mark.parametrize("is_call", [True, False])
    def test_recovers_the_volatility_it_was_priced_with(self, vol, is_call):
        price = d.black76_price(self.F, self.K, self.T, vol, self.RATE, is_call)
        assert d.implied_vol(price, self.F, self.K, self.T, self.RATE, is_call) == pytest.approx(
            vol, abs=1e-4
        )

    def test_price_below_intrinsic_has_no_solution(self):
        """Такой цены на рынке быть не может — честнее вернуть пустоту."""
        assert d.implied_vol(1.0, 3300.0, 3250.0, self.T, self.RATE, True) is None

    def test_expired_option_gives_nothing(self):
        assert d.implied_vol(50.0, self.F, self.K, 0.0, self.RATE, True) is None


class TestFuturesPnl:
    """На ФОРТС рубли считаются через шаг цены, а не как разница котировок."""

    def test_step_price_converts_points_to_roubles(self):
        # RTS: шаг 10 пунктов стоит 14 ₽ — движение на 100 пунктов даёт 140 ₽
        rts = _contract(
            kind="future", strike=None, option_type=None,
            min_step=10.0, step_price=14.0, last=100_000.0, settle_price=100_000.0,
        )
        assert d.price_pnl(rts, 100_000.0, 100_100.0, 1) == pytest.approx(140.0)

    def test_short_position_earns_on_the_way_down(self):
        future = _contract(kind="future", strike=None, option_type=None)
        assert d.price_pnl(future, 100.0, 90.0, -5) == pytest.approx(50.0)

    def test_quantity_scales_the_result(self):
        future = _contract(kind="future", strike=None, option_type=None)
        one = d.price_pnl(future, 100.0, 110.0, 1)
        ten = d.price_pnl(future, 100.0, 110.0, 10)
        assert ten == pytest.approx(one * 10)


class TestPayoff:
    def test_long_call_breaks_even_at_strike_plus_premium(self):
        leg = d.Leg(
            contract=_contract(min_step=0.01, step_price=0.01, strike=290.0),
            direction=1, quantity=1, entry_price=0.08,
        )
        curve = d.payoff_curve([leg], 271.94)
        assert d.breakeven_points(curve) == pytest.approx([290.08], abs=0.01)

    def test_short_put_breaks_even_at_strike_minus_premium(self):
        leg = d.Leg(
            contract=_contract(
                min_step=0.01, step_price=0.01, strike=260.0, option_type="P"
            ),
            direction=-1, quantity=1, entry_price=0.5,
        )
        curve = d.payoff_curve([leg], 271.94)
        assert d.breakeven_points(curve) == pytest.approx([259.5], abs=0.01)

    def test_strike_is_a_point_on_the_grid(self):
        """На страйке у профиля излом — сетка обязана в него попадать."""
        leg = d.Leg(
            contract=_contract(min_step=0.01, step_price=0.01, strike=280.0),
            direction=1, quantity=1, entry_price=1.0,
        )
        prices = [point["price"] for point in d.payoff_curve([leg], 272.0)]
        assert 280.0 in prices

    def test_long_call_cannot_lose_more_than_the_premium(self):
        leg = d.Leg(
            contract=_contract(min_step=0.01, step_price=0.01, strike=290.0),
            direction=1, quantity=3, entry_price=2.0,
        )
        curve = d.payoff_curve([leg], 271.94)
        assert min(point["pnl"] for point in curve) == pytest.approx(-6.0, abs=0.01)

    def test_spread_has_two_breakeven_points(self):
        """Проданный стрэнгл: убыток по краям, прибыль в середине."""
        call = d.Leg(
            contract=_contract(min_step=0.01, step_price=0.01, strike=290.0),
            direction=-1, quantity=1, entry_price=2.0,
        )
        put = d.Leg(
            contract=_contract(
                min_step=0.01, step_price=0.01, strike=250.0, option_type="P"
            ),
            direction=-1, quantity=1, entry_price=2.0,
        )
        points = d.breakeven_points(d.payoff_curve([call, put], 271.0))
        assert len(points) == 2
        assert points[0] == pytest.approx(246.0, abs=0.05)
        assert points[1] == pytest.approx(294.0, abs=0.05)

    def test_empty_position_has_no_curve(self):
        assert d.payoff_curve([], 100.0) == []


class TestPositionSummary:
    RATE = 0.14

    def test_totals_add_up_across_legs(self):
        first = d.Leg(contract=_contract(), direction=1, quantity=2, entry_price=90.0)
        second = d.Leg(
            contract=_contract(secid="SR280CH6", strike=280.0),
            direction=1, quantity=3, entry_price=50.0,
        )
        summary = d.position_summary([first, second], self.RATE)

        assert summary["pnl"] == pytest.approx(
            sum(leg["pnl"] for leg in summary["legs"])
        )
        assert summary["margin"] == pytest.approx(500.0 * 2 + 500.0 * 3)

    def test_exchange_volatility_is_preferred_over_own_calculation(self):
        leg = d.Leg(
            contract=_contract(volatility=42.0), direction=1, quantity=1, entry_price=100.0
        )
        result = d.leg_result(leg, self.RATE)
        assert result["implied_vol"] == pytest.approx(42.0)
        assert result["vol_source"] == "биржа"

    def test_own_calculation_when_exchange_is_silent(self):
        leg = d.Leg(contract=_contract(), direction=1, quantity=1, entry_price=100.0)
        result = d.leg_result(leg, self.RATE)
        assert result["vol_source"] == "расчёт по цене"
        assert result["implied_vol"] is not None

    def test_short_position_flips_the_sign_of_greeks(self):
        long = d.leg_result(
            d.Leg(contract=_contract(volatility=40.0), direction=1, quantity=1,
                  entry_price=100.0),
            self.RATE,
        )
        short = d.leg_result(
            d.Leg(contract=_contract(volatility=40.0), direction=-1, quantity=1,
                  entry_price=100.0),
            self.RATE,
        )
        assert short["greeks"]["delta"] == pytest.approx(-long["greeks"]["delta"])
        assert short["greeks"]["theta"] == pytest.approx(-long["greeks"]["theta"])

    def test_futures_leg_has_no_greeks(self):
        leg = d.Leg(
            contract=_contract(kind="future", strike=None, option_type=None),
            direction=1, quantity=1, entry_price=100.0,
        )
        assert d.leg_result(leg, self.RATE)["greeks"] is None

    def test_contract_falls_back_to_settlement_price(self):
        """Вне сессии сделок нет — позиция не должна обнуляться на экране."""
        contract = _contract(last=None, settle_price=123.0)
        assert d.contract_price(contract) == 123.0


class TestParsing:
    def test_zero_from_exchange_is_absence_not_a_price(self):
        """Биржа пишет ноль вместо пустого поля — принять его за цену нельзя."""
        contract = d.parse_future(
            {"SECID": "SRU6", "SHORTNAME": "SBRF-9.26", "ASSETCODE": "SBRF",
             "LASTTRADEDATE": "2026-09-17", "MINSTEP": 1.0, "STEPPRICE": 1.0,
             "LAST": 0, "SETTLEPRICE": 0, "PREVSETTLEPRICE": 27600.0,
             "INITIALMARGIN": 4869.84, "OPENPOSITION": 1056862, "BUYSELLFEE": 5.43}
        )
        assert contract.last is None
        assert contract.settle_price == 27600.0
        assert contract.expiry == date(2026, 9, 17)

    def test_option_without_type_is_skipped(self):
        assert d.parse_option({"SECID": "BROKEN", "STRIKE": 100.0}) is None

    def test_row_without_code_is_skipped(self):
        assert d.parse_future({"SHORTNAME": "без кода"}) is None
