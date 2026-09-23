"""Тесты нормализации данных из внешних источников."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.sources.base import rows_to_dicts, to_date, to_datetime, to_float, to_int
from app.sources.moex import _dedupe_by_secid, _map_bar, _map_instrument, _map_quote
from app.sources.nsd import (
    _dedupe,
    _map_amortizations,
    _map_coupons,
    upcoming_payments,
)


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


class TestBondizationSchedule:
    """График выплат: полнота и различение погашения от амортизации."""

    def test_maturity_marker_is_carried_over(self):
        rows = [
            {
                "amortdate": "2037-08-12",
                "valueprc": 100,
                "value": 1000,
                "value_rub": 1000,
                "facevalue": 1000,
                "faceunit": "RUB",
                "data_source": "maturity",
            }
        ]
        action = _map_amortizations(rows, "RU000TEST0001", "TEST")[0]
        assert action["data_source"] == "maturity"
        assert action["action_type"] == "amortization"

    def test_partial_amortization_has_no_marker(self):
        rows = [{"amortdate": "2030-01-15", "valueprc": 25, "value": 250}]
        assert _map_amortizations(rows, "ISIN", "SEC")[0]["data_source"] is None

    def test_repeated_rows_are_dropped(self):
        """Биржа отдаёт погашение в блоке амортизаций дважды."""
        row = {"amortdate": "2037-08-12", "valueprc": 100, "value": 1000,
               "data_source": "maturity"}
        actions = _dedupe(_map_amortizations([row, dict(row)], "ISIN", "SEC"))
        assert len(actions) == 1

    def test_different_dates_survive_dedupe(self):
        rows = [
            {"amortdate": "2030-01-15", "value": 250},
            {"amortdate": "2031-01-15", "value": 250},
        ]
        assert len(_dedupe(_map_amortizations(rows, "ISIN", "SEC"))) == 2

    @pytest.mark.asyncio
    async def test_schedule_is_read_page_by_page(self):
        """С одной страницей дальние купоны выпуска молча терялись.

        ISS применяет ``limit`` к каждому блоку графика, поэтому у выпуска с
        частым купоном ответ обрезался на сотой выплате — и обрезался именно
        с хвоста, где альтернативных данных нет.
        """
        from app.sources.moex import MoexSource

        page_size = MoexSource.BONDIZATION_PAGE
        total = page_size + 7
        requested: list[int] = []

        async def fake_get_json(path, **params):
            start = params.get("start", 0)
            requested.append(start)
            rows = [
                [f"2030-01-{(index % 28) + 1:02d}"]
                for index in range(start, min(start + page_size, total))
            ]
            return {
                "coupons": {"columns": ["coupondate"], "data": rows},
                "amortizations": {"columns": ["amortdate"], "data": []},
                "offers": {"columns": ["offerdate"], "data": []},
            }

        source = MoexSource()
        source.get_json = fake_get_json  # type: ignore[method-assign]
        payload = await source.fetch_bondization("RU000TEST0001")

        assert len(payload["coupons"]) == total
        assert requested == [0, page_size]

    @pytest.mark.asyncio
    async def test_single_page_costs_one_request(self):
        from app.sources.moex import MoexSource

        requested: list[int] = []

        async def fake_get_json(path, **params):
            requested.append(params.get("start", 0))
            return {
                "coupons": {"columns": ["coupondate"], "data": [["2030-01-15"]]},
                "amortizations": None,
                "offers": None,
            }

        source = MoexSource()
        source.get_json = fake_get_json  # type: ignore[method-assign]
        payload = await source.fetch_bondization("RU000TEST0001")

        assert requested == [0]
        assert len(payload["coupons"]) == 1
        assert payload["offers"] == []


class TestScheduleLookupKey:
    """График выплат спрашивается по коду бумаги, а не по ISIN.

    Биржа на запрос по ISIN отвечает не ошибкой, а пустым графиком. У
    корпоративных выпусков код и ISIN совпадают, поэтому по ним всё работало;
    у ОФЗ они разные, и график государственных бумаг не загружался вообще —
    в календаре поступлений по ним не было ни купонов, ни погашения.
    """

    @staticmethod
    def _source(schedules):
        from app.sources.moex import MoexSource
        from app.sources.nsd import NsdSource

        asked: list[str] = []

        async def fake_bondization(code, **kwargs):
            asked.append(code)
            rows = schedules.get(code, [])
            return {
                "coupons": [{"coupondate": row, "value": 10.0} for row in rows],
                "amortizations": [],
                "offers": [],
            }

        moex = MoexSource()
        moex.fetch_bondization = fake_bondization  # type: ignore[method-assign]
        return NsdSource(moex), asked

    @pytest.mark.asyncio
    async def test_government_bond_is_asked_by_its_secid(self):
        nsd, asked = self._source({"SU26238RMFS4": ["2030-05-15"]})
        actions = await nsd.fetch_cashflows("RU000A1038V6", "SU26238RMFS4")

        assert asked == ["SU26238RMFS4"]
        assert len(actions) == 1
        # Хранится запись всё равно под ISIN — это её ключ
        assert actions[0]["isin"] == "RU000A1038V6"
        assert actions[0]["secid"] == "SU26238RMFS4"

    @pytest.mark.asyncio
    async def test_isin_is_tried_when_the_secid_gives_nothing(self):
        nsd, asked = self._source({"RU000A105SG2": ["2030-05-15"]})
        actions = await nsd.fetch_cashflows("RU000A105SG2", "SOMEOTHER")

        assert asked == ["SOMEOTHER", "RU000A105SG2"]
        assert len(actions) == 1

    @pytest.mark.asyncio
    async def test_isin_alone_still_works(self):
        nsd, asked = self._source({"RU000A105SG2": ["2030-05-15"]})
        actions = await nsd.fetch_cashflows("RU000A105SG2")

        assert asked == ["RU000A105SG2"]
        assert len(actions) == 1

    @pytest.mark.asyncio
    async def test_bond_without_schedule_gives_empty_result(self):
        nsd, asked = self._source({})
        assert await nsd.fetch_cashflows("RU000A1038V6", "SU26238RMFS4") == []
        assert asked == ["SU26238RMFS4", "RU000A1038V6"]

    @pytest.mark.asyncio
    async def test_broken_request_does_not_stop_the_fallback(self):
        from app.sources.moex import MoexSource
        from app.sources.nsd import NsdSource

        asked: list[str] = []

        async def fake_bondization(code, **kwargs):
            asked.append(code)
            if code == "SU26238RMFS4":
                raise RuntimeError("биржа недоступна")
            return {
                "coupons": [{"coupondate": "2030-05-15", "value": 10.0}],
                "amortizations": [],
                "offers": [],
            }

        moex = MoexSource()
        moex.fetch_bondization = fake_bondization  # type: ignore[method-assign]
        actions = await NsdSource(moex).fetch_cashflows("RU000A1038V6", "SU26238RMFS4")

        assert asked == ["SU26238RMFS4", "RU000A1038V6"]
        assert len(actions) == 1
