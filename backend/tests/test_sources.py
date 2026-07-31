"""Тесты нормализации данных из внешних источников."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.sources.base import rows_to_dicts, to_date, to_datetime, to_float, to_int
from app.sources.moex import _dedupe_by_secid, _map_bar, _map_instrument, _map_quote
from app.sources.nsd import _map_amortizations, _map_coupons, upcoming_payments


class TestParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("", None),
            ("-", None),
            (12.5, 12.5),
            (7, 7.0),
            ("3.25", 3.25),
            # ЦБ РФ отдаёт десятичную запятую
            ("79,8573", 79.8573),
            ("1 234,50", 1234.5),
            ("не число", None),
            (True, None),
        ],
    )
    def test_to_float(self, value, expected):
        assert to_float(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-07-31", date(2026, 7, 31)),
            ("31.07.2026", date(2026, 7, 31)),
            ("2026-07-31 08:50:10", date(2026, 7, 31)),
            # ЦБ отдаёт ISO со смещением — раньше это ломало разбор
            ("2026-06-01T00:00:00+03:00", date(2026, 6, 1)),
            ("2026-06-01T00:00:00Z", date(2026, 6, 1)),
            # MOEX помечает отсутствующую дату нулями
            ("0000-00-00", None),
            ("", None),
            (None, None),
        ],
    )
    def test_to_date(self, value, expected):
        assert to_date(value) == expected

    def test_to_datetime_strips_timezone(self):
        """В БД хранятся наивные метки: смешивать с tz-aware нельзя."""
        parsed = to_datetime("2026-06-01T10:30:00+03:00")
        assert parsed == datetime(2026, 6, 1, 10, 30)
        assert parsed.tzinfo is None

    def test_to_int(self):
        assert to_int("42") == 42
        assert to_int(42.9) == 42
        assert to_int(None) is None

    def test_rows_to_dicts(self):
        block = {"columns": ["SECID", "LAST"], "data": [["SBER", 274.5], ["GAZP", 91.3]]}
        assert rows_to_dicts(block) == [
            {"SECID": "SBER", "LAST": 274.5},
            {"SECID": "GAZP", "LAST": 91.3},
        ]

    def test_rows_to_dicts_handles_missing_block(self):
        assert rows_to_dicts(None) == []
        assert rows_to_dicts({}) == []


class TestMoexMapping:
    SHARE_SPEC = {"engine": "stock", "market": "shares", "kind": "share"}
    BOND_SPEC = {"engine": "stock", "market": "bonds", "kind": "bond"}

    def test_dedupe_keeps_first_and_order(self):
        rows = [{"SECID": "A"}, {"SECID": "B"}, {"SECID": "A"}, {"SECID": None}]
        assert [row["SECID"] for row in _dedupe_by_secid(rows)] == ["A", "B"]

    def test_map_instrument_share(self):
        row = {
            "SECID": "SBER",
            "ISIN": "RU0009029540",
            "SHORTNAME": "Сбербанк",
            "SECNAME": "Сбербанк России ПАО ао",
            "LOTSIZE": 1,
            "FACEVALUE": 3.0,
            "LISTLEVEL": 1,
            "CURRENCYID": "SUR",
        }
        mapped = _map_instrument(row, "TQBR", self.SHARE_SPEC)
        assert mapped["secid"] == "SBER"
        assert mapped["kind"] == "share"
        assert mapped["board"] == "TQBR"
        assert mapped["isin"] == "RU0009029540"
        assert mapped["lot_size"] == 1

    def test_map_instrument_bond_dates(self):
        row = {
            "SECID": "SU26238RMFS4",
            "MATDATE": "2041-05-15",
            "OFFERDATE": "0000-00-00",
            "COUPONPERCENT": 7.1,
            "NEXTCOUPON": "2026-11-18",
        }
        mapped = _map_instrument(row, "TQOB", self.BOND_SPEC)
        assert mapped["maturity_date"] == date(2041, 5, 15)
        # Пустая оферта не должна превращаться в дату
        assert mapped["offer_date"] is None
        assert mapped["coupon_percent"] == 7.1

    def test_map_quote_computes_missing_spread_and_change(self):
        """Если биржа не прислала спред и изменение — считаем сами."""
        ts = datetime(2026, 7, 31, 12, 0)
        sec = {"PREVPRICE": 100.0}
        md = {"BID": 99.5, "OFFER": 100.5, "LAST": 101.0}
        quote = _map_quote(sec, md, {}, "share", ts)
        assert quote["spread"] == pytest.approx(1.0)
        assert quote["change_pct"] == pytest.approx(1.0)

    def test_map_quote_prefers_exchange_values(self):
        ts = datetime(2026, 7, 31, 12, 0)
        md = {"SPREAD": 0.25, "LASTCHANGEPRCNT": -2.5, "LAST": 90.0, "VOLTODAY": 100}
        quote = _map_quote({"PREVPRICE": 100.0}, md, {}, "share", ts)
        assert quote["spread"] == 0.25
        assert quote["change_pct"] == -2.5

    def test_map_quote_bond_uses_yield_block(self):
        """Доходность и спреды берём из marketdata_yields — он точнее."""
        ts = datetime(2026, 7, 31, 12, 0)
        yields = {
            "EFFECTIVEYIELD": 15.5,
            "DURATION": 2200,
            "ZSPREADBP": 180,
            "GSPREADBP": 175,
        }
        quote = _map_quote({}, {"YIELD": 9.9}, yields, "bond", ts)
        assert quote["yield_pct"] == 15.5
        assert quote["duration_days"] == 2200
        assert quote["z_spread_bp"] == 180

    def test_map_quote_index_schema(self):
        ts = datetime(2026, 7, 31, 12, 0)
        md = {"CURRENTVALUE": 2209.84, "LASTCHANGEPRC": -1.22, "VALTODAY": 5e10}
        quote = _map_quote({}, md, {}, "index", ts)
        assert quote["last"] == 2209.84
        assert quote["change_pct"] == -1.22

    def test_map_bar(self):
        row = {
            "TRADEDATE": "2026-07-20",
            "OPEN": 247.3,
            "CLOSE": 260.99,
            "VOLUME": 132103400,
            "VALUE": 33294836798.82,
            "NUMTRADES": 623452,
        }
        bar = _map_bar(row)
        assert bar["trade_date"] == date(2026, 7, 20)
        assert bar["close"] == 260.99
        assert bar["num_trades"] == 623452


class TestNsdMapping:
    def test_map_coupons(self):
        rows = [
            {
                "coupondate": "2026-12-29",
                "recorddate": "2026-12-26",
                "startdate": "2026-06-29",
                "value": 38.23,
                "value_rub": 38.23,
                "facevalue": 1000,
                "faceunit": "RUB",
                "name": "Тест",
            }
        ]
        actions = _map_coupons(rows, "RU000TEST0001", "TEST")
        assert len(actions) == 1
        assert actions[0]["action_type"] == "coupon"
        assert actions[0]["action_date"] == date(2026, 12, 29)
        assert actions[0]["source"] == "nsd"

    def test_map_skips_rows_without_date(self):
        assert _map_coupons([{"value": 10}], "ISIN", "SEC") == []
        assert _map_amortizations([{"value": 10}], "ISIN", "SEC") == []

    def test_upcoming_payments_filters_horizon(self):
        from datetime import timedelta

        today = date.today()
        actions = [
            {"action_date": today - timedelta(days=5)},   # прошедшая
            {"action_date": today + timedelta(days=10)},  # в горизонте
            {"action_date": today + timedelta(days=200)}, # за горизонтом
        ]
        result = upcoming_payments(actions, horizon_days=90)
        assert len(result) == 1
        assert result[0]["action_date"] == today + timedelta(days=10)
