"""Отбор по виду бумаги: ОФЗ, корпоративные, биржевые, привилегированные и т.д.

Биржевой срез несёт только однобуквенный код вида, по которому нельзя
построить осмысленный фильтр — вид приходит из массового справочника и
хранится в ``Instrument.security_type``. Здесь проверяется отбор по нему в
витрине инструментов и в анализе облигаций, а также каталог видов для
выпадающего списка.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Instrument, Quote
from app.services import analytics
from app.services import bonds as bonds_service
from app.services.security_types import security_type_title, sort_key


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        yield active


def add_instrument(session, secid, kind="bond", security_type=None, **kwargs):
    instrument = Instrument(
        secid=secid,
        board=kwargs.pop("board", "TQOB"),
        engine="stock",
        market="bonds" if kind == "bond" else "shares",
        kind=kind,
        short_name=kwargs.pop("short_name", secid),
        security_type=security_type,
        **kwargs,
    )
    session.add(instrument)
    session.flush()
    session.add(Quote(instrument_id=instrument.id, ts=datetime(2026, 7, 31, 12, 0), last=100.0))
    session.commit()
    return instrument


class TestSecurityTypeTitle:
    def test_known_code_translated(self):
        assert security_type_title("ofz_bond") == "ОФЗ (гособлигация)"

    def test_unknown_code_shown_as_is(self):
        assert security_type_title("something_new") == "something_new"

    def test_empty_code_is_none(self):
        assert security_type_title(None) is None

    def test_sort_puts_ofz_before_corporate(self):
        codes = ["exchange_bond", "corporate_bond", "ofz_bond"]
        assert sorted(codes, key=sort_key) == ["ofz_bond", "corporate_bond", "exchange_bond"]

    def test_unknown_codes_sort_to_the_end(self):
        codes = ["zzz_unknown", "ofz_bond"]
        assert sorted(codes, key=sort_key) == ["ofz_bond", "zzz_unknown"]


class TestScreenerBySecurityType:
    def test_filters_instruments_by_security_type(self, session):
        add_instrument(session, "SU26238", security_type="ofz_bond")
        add_instrument(session, "RU000A1", security_type="corporate_bond")

        result = analytics.screener(session, security_types=["ofz_bond"])
        secids = {row["secid"] for row in result["items"]}
        assert secids == {"SU26238"}

    def test_without_filter_returns_everything(self, session):
        add_instrument(session, "SU26238", security_type="ofz_bond")
        add_instrument(session, "RU000A1", security_type="corporate_bond")

        result = analytics.screener(session)
        assert result["total"] == 2

    def test_row_carries_security_type_title(self, session):
        add_instrument(session, "SU26238", security_type="ofz_bond")
        result = analytics.screener(session)
        assert result["items"][0]["security_type_title"] == "ОФЗ (гособлигация)"

    def test_instrument_without_security_type_excluded_when_filtering(self, session):
        add_instrument(session, "NOTYPE", security_type=None)
        result = analytics.screener(session, security_types=["ofz_bond"])
        assert result["total"] == 0


class TestSecurityTypeCatalog:
    def test_groups_by_kind_with_counts(self, session):
        add_instrument(session, "SU26238", kind="bond", security_type="ofz_bond")
        add_instrument(session, "SU26239", kind="bond", security_type="ofz_bond")
        add_instrument(session, "RU000A1", kind="bond", security_type="corporate_bond")
        add_instrument(session, "SBER", kind="share", security_type="common_share")

        catalog = analytics.security_type_catalog(session)
        assert set(catalog.keys()) == {"bond", "share"}
        bond_codes = {item["code"]: item["count"] for item in catalog["bond"]}
        assert bond_codes == {"ofz_bond": 2, "corporate_bond": 1}

    def test_can_be_scoped_to_one_kind(self, session):
        add_instrument(session, "SU26238", kind="bond", security_type="ofz_bond")
        add_instrument(session, "SBER", kind="share", security_type="common_share")

        catalog = analytics.security_type_catalog(session, kinds=("bond",))
        assert set(catalog.keys()) == {"bond"}

    def test_instruments_without_security_type_are_skipped(self, session):
        add_instrument(session, "NOTYPE", kind="bond", security_type=None)
        catalog = analytics.security_type_catalog(session)
        assert catalog == {}

    def test_items_are_ordered_ofz_first(self, session):
        add_instrument(session, "A", kind="bond", security_type="exchange_bond")
        add_instrument(session, "B", kind="bond", security_type="ofz_bond")

        catalog = analytics.security_type_catalog(session, kinds=("bond",))
        codes = [item["code"] for item in catalog["bond"]]
        assert codes == ["ofz_bond", "exchange_bond"]


class TestBondsAnalyseBySecurityType:
    def test_filters_bonds_by_security_type(self, session):
        add_instrument(session, "SU26238", security_type="ofz_bond")
        add_instrument(session, "RU000A1", security_type="corporate_bond")

        result = bonds_service.analyse(session, security_types=["corporate_bond"])
        secids = {row["secid"] for row in result["items"]}
        assert secids == {"RU000A1"}

    def test_export_column_present(self):
        codes = [column["code"] for column in bonds_service.ANALYSIS_COLUMNS]
        assert "security_type_title" in codes
