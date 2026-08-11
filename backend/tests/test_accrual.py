"""Тесты НКД на произвольную дату и разбора хода торгов."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, CorpAction, FxRate, Instrument
from app.services import accrual as accrual_service
from app.services import intraday as intraday_service
from app.sources.moex import _page_size_for


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        yield active


def add_bond(session, secid="AAA", **kwargs):
    kwargs.setdefault("face_value", 1000.0)
    kwargs.setdefault("face_unit", "SUR")
    instrument = Instrument(
        secid=secid, board="TQCB", engine="stock", market="bonds", kind="bond",
        short_name=secid, isin=f"RU{secid}", **kwargs,
    )
    session.add(instrument)
    session.flush()
    return instrument


def add_coupon(session, instrument, on_date, value, start=None):
    action = CorpAction(
        isin=instrument.isin, secid=instrument.secid, action_type="coupon",
        action_date=on_date, value=value, start_date=start,
    )
    session.add(action)
    session.flush()
    return action


# ----------------------------------------------------------------------
# Расчёт по справочнику
# ----------------------------------------------------------------------
def test_accrued_grows_linearly_within_period(session):
    """НКД — доля купона, пропорциональная прошедшим дням."""
    bond = add_bond(
        session, coupon_value=36.4, coupon_period=182,
        next_coupon_date=date(2026, 12, 2),
    )
    session.commit()

    # Период 2026-06-03 .. 2026-12-02, ровно половина — 91 день
    half = accrual_service.accrued_on(session, bond, date(2026, 9, 2))
    assert half["days_passed"] == 91
    assert half["days_total"] == 182
    assert half["value"] == pytest.approx(18.2)


def test_accrued_is_zero_on_period_start(session):
    bond = add_bond(
        session, coupon_value=36.4, coupon_period=182,
        next_coupon_date=date(2026, 12, 2),
    )
    session.commit()

    row = accrual_service.accrued_on(session, bond, date(2026, 6, 3))
    assert row["value"] == 0.0
    assert row["days_passed"] == 0


def test_accrued_resets_on_coupon_date(session):
    """В день выплаты купон получен, НКД обнуляется и период начинается заново.

    Иначе на дату купона показывался бы полный купон — как будто покупатель
    этого дня получит выплату, хотя она уже ушла прежнему владельцу.
    """
    bond = add_bond(
        session, coupon_value=36.4, coupon_period=182,
        next_coupon_date=date(2026, 12, 2),
    )
    session.commit()

    day_before = accrual_service.accrued_on(session, bond, date(2026, 12, 1))
    on_coupon = accrual_service.accrued_on(session, bond, date(2026, 12, 2))

    assert day_before["value"] == pytest.approx(36.4 * 181 / 182, abs=0.001)
    assert on_coupon["value"] == 0.0
    assert on_coupon["period_start"] == date(2026, 12, 2)
    assert on_coupon["period_end"] == date(2027, 6, 2)


def test_reference_steps_forward_when_stale(session):
    """Справочник мог отстать: ближайший купон уже в прошлом."""
    bond = add_bond(
        session, coupon_value=36.4, coupon_period=182,
        next_coupon_date=date(2025, 12, 2),
    )
    session.commit()

    row = accrual_service.accrued_on(session, bond, date(2026, 8, 11))
    assert row["period_start"] <= date(2026, 8, 11) < row["period_end"]


def test_date_before_known_period_is_unknown(session):
    """Справочник знает только текущий период — прошлое считать нечем."""
    bond = add_bond(
        session, coupon_value=36.4, coupon_period=182,
        next_coupon_date=date(2026, 12, 2),
    )
    session.commit()

    assert accrual_service.accrued_on(session, bond, date(2026, 6, 2)) is None


def test_share_has_no_accrued(session):
    share = Instrument(
        secid="SBER", board="TQBR", engine="stock", market="shares", kind="share",
        short_name="SBER",
    )
    session.add(share)
    session.commit()
    assert accrual_service.accrued_on(session, share, date.today()) is None


# ----------------------------------------------------------------------
# Расчёт по графику купонов
# ----------------------------------------------------------------------
def test_schedule_wins_over_reference(session):
    """График знает неравные периоды и меняющийся купон."""
    bond = add_bond(
        session, coupon_value=36.4, coupon_period=182,
        next_coupon_date=date(2026, 12, 2),
    )
    add_coupon(session, bond, date(2026, 6, 29), 38.02, start=date(2025, 12, 29))
    add_coupon(session, bond, date(2026, 12, 29), 38.23)
    session.commit()

    row = accrual_service.accrued_on(session, bond, date(2026, 8, 11))
    assert row["source"].startswith("график")
    assert row["period_start"] == date(2026, 6, 29)
    assert row["period_end"] == date(2026, 12, 29)
    assert row["days_total"] == 183
    assert row["value"] == pytest.approx(38.23 * 43 / 183, abs=0.001)


def test_first_period_uses_start_date_from_schedule(session):
    """У первого купона предыдущей даты нет — берём его start_date."""
    bond = add_bond(session)
    add_coupon(session, bond, date(2026, 9, 5), 17.05, start=date(2026, 8, 6))
    session.commit()

    row = accrual_service.accrued_on(session, bond, date(2026, 8, 21))
    assert row["period_start"] == date(2026, 8, 6)
    assert row["days_passed"] == 15


# ----------------------------------------------------------------------
# Флоатеры: ставка периода не объявлена
# ----------------------------------------------------------------------
def test_unknown_coupon_restored_from_exchange_value(session):
    """У флоатера купон в справочнике нулевой, но биржа НКД знает.

    Внутри периода НКД растёт линейно, поэтому по одной известной точке и
    границам периода считается любая другая дата.
    """
    bond = add_bond(
        session, coupon_value=0.0, coupon_period=30,
        next_coupon_date=date(2026, 8, 26),
    )
    session.commit()

    # Период 2026-07-27 .. 2026-08-26. На 12.08 прошло 16 дней
    row = accrual_service.accrued_on(
        session, bond, date(2026, 8, 11),
        exchange_value=8.0, settle_date=date(2026, 8, 12),
    )
    # Купон = 8 × 30/16 = 15, на 15-й день НКД = 7.5
    assert row["value"] == pytest.approx(7.5)
    assert row["value_basis"] == "settlement"
    assert "биржевой НКД" in row["source"]


def test_three_dates_differ_by_one_day_each(session):
    """НКД растёт по дням, и три соседние даты дают три разных числа.

    Случай из жизни: ЕвразХолдинг 003Р-01, купон выплачен 09.08, период 30
    дней, биржа публикует 1,26 на дату расчётов 12.08. Значит, накопление
    0,42 ₽ в день: 10.08 — 0,42, сегодня 11.08 — 0,84, расчёты 12.08 — 1,26.
    Складывать эти числа между собой нельзя, хотя 0,42 + 0,84 = 1,26 здесь
    совпадает случайно — просто первый и второй день дают третий.
    """
    bond = add_bond(
        session, coupon_value=0.0, coupon_period=30,
        next_coupon_date=date(2026, 9, 8),
    )
    session.commit()

    def at(day):
        row = accrual_service.accrued_on(
            session, bond, day, exchange_value=1.26, settle_date=date(2026, 8, 12),
        )
        return row["value"]

    assert at(date(2026, 8, 9)) == 0.0
    assert at(date(2026, 8, 10)) == pytest.approx(0.42)
    assert at(date(2026, 8, 11)) == pytest.approx(0.84)
    assert at(date(2026, 8, 12)) == pytest.approx(1.26)
    # Дальше сложение перестаёт совпадать: 4,20 + 4,62 ≠ 5,04
    assert at(date(2026, 8, 20)) == pytest.approx(4.62)
    assert at(date(2026, 8, 21)) == pytest.approx(5.04)


def test_discount_bond_accrues_nothing(session):
    """У бескупонного выпуска НКД равен нулю — это факт, а не пропуск.

    Прочерк на месте нуля читается как сбой расчёта, хотя купонных периодов
    у дисконтной бумаги нет вовсе и биржа тоже показывает ноль.
    """
    bond = add_bond(session, coupon_value=0.0, coupon_period=0, next_coupon_date=None)
    session.commit()

    row = accrual_service.accrued_on(session, bond, date.today(), exchange_value=0.0)
    assert row["value"] == 0.0
    assert row["days_total"] == 0
    assert "бескупонный" in row["source"]


def test_missing_coupon_data_is_not_reported_as_zero(session):
    """Нехватка данных — не то же самое, что отсутствие купона.

    Если о периодах ничего не известно и подтверждения от биржи нет, ноль
    был бы утверждением о деньгах, которого мы сделать не можем.
    """
    bond = add_bond(session, coupon_value=None, coupon_period=None, next_coupon_date=None)
    session.commit()

    assert accrual_service.accrued_on(session, bond, date.today()) is None


def test_period_start_is_zero_even_without_known_coupon(session):
    """В день начала периода накоплено ноль при любой ставке.

    Величину купона знать для этого не нужно, поэтому выпуск с необъявленной
    ставкой в этот день считается точно, а не остаётся без значения.
    """
    bond = add_bond(
        session, coupon_value=0.0, coupon_period=30,
        next_coupon_date=date(2026, 8, 12),
    )
    session.commit()

    row = accrual_service.accrued_on(
        session, bond, date(2026, 8, 12),
        exchange_value=0.0, settle_date=date(2026, 8, 12),
    )
    assert row["value"] == 0.0
    assert row["days_passed"] == 0
    assert row["estimate"] is False
    assert row["period_start"] == date(2026, 8, 12)


def test_unknown_coupon_without_exchange_value_is_unknown(session):
    """Без биржевого НКД восстанавливать нечего — честнее вернуть пусто."""
    bond = add_bond(
        session, coupon_value=0.0, coupon_period=30,
        next_coupon_date=date(2026, 8, 26),
    )
    session.commit()
    assert accrual_service.accrued_on(session, bond, date(2026, 8, 11)) is None


def test_exchange_value_outside_period_is_not_used(session):
    """Биржевой НКД другого периода для восстановления не годится."""
    bond = add_bond(
        session, coupon_value=0.0, coupon_period=30,
        next_coupon_date=date(2026, 8, 26),
    )
    session.commit()
    row = accrual_service.accrued_on(
        session, bond, date(2026, 8, 11),
        exchange_value=8.0, settle_date=date(2026, 5, 1),
    )
    assert row is None


# ----------------------------------------------------------------------
# Валюта: биржевой НКД в рублях, купон — в валюте номинала
# ----------------------------------------------------------------------
def test_foreign_bond_compared_in_rubles(session):
    """Купон в долларах против биржевого НКД в рублях — не расхождение."""
    bond = add_bond(
        session, secid="USDBOND", face_unit="USD", currency="SUR",
        coupon_value=38.23, coupon_period=183,
        next_coupon_date=date(2026, 12, 29),
    )
    session.add(FxRate(source="cbr", code="USD", rate_date=date.today(), value=80.0, nominal=1))
    session.commit()

    # 44 дня из 183 = 9.1919 USD = 735.35 ₽
    profile = accrual_service.accrual_profile(
        session, bond, exchange_value=735.35, settle_date=date(2026, 8, 12),
    )
    settlement = profile["settlement"]
    assert settlement["currency"] == "USD"
    assert settlement["value"] == pytest.approx(9.1919, abs=0.001)
    assert settlement["value_rub"] == pytest.approx(735.35, abs=0.5)
    assert profile["mismatch"] is None


def test_restored_value_not_converted_twice(session):
    """Восстановленный из биржевого НКД купон уже в рублях."""
    bond = add_bond(
        session, secid="USDFLOAT", face_unit="USD", coupon_value=0.0,
        coupon_period=30, next_coupon_date=date(2026, 8, 26),
    )
    session.add(FxRate(source="cbr", code="USD", rate_date=date.today(), value=80.0, nominal=1))
    session.commit()

    profile = accrual_service.accrual_profile(
        session, bond, exchange_value=800.0, settle_date=date(2026, 8, 12),
    )
    settlement = profile["settlement"]
    assert settlement["currency"] == "RUB"
    assert settlement["value_rub"] == pytest.approx(800.0, abs=0.5)


def test_exchange_value_wins_over_announced_coupon(session):
    """Объявленный купон — прогноз, биржевой НКД — деньги по сделке.

    У выпусков с ежедневным начислением по плавающей ставке НКД внутри
    периода растёт неровно, и линейная доля объявленного купона расходится с
    тем, что биржа реально посчитает к расчётам. Доверяем бирже.
    """
    bond = add_bond(
        session, coupon_value=36.4, coupon_period=182,
        next_coupon_date=date(2026, 12, 2),
    )
    session.commit()

    profile = accrual_service.accrual_profile(
        session, bond, exchange_value=99.0, settle_date=date(2026, 8, 12),
    )
    settlement = profile["settlement"]
    # На дату расчётов совпадает с биржей ровно, без предупреждений
    assert settlement["value"] == pytest.approx(99.0)
    assert profile["mismatch"] is None
    # И выпуск помечен как плавающий, чтобы число не приняли за точное
    assert settlement["floating"] is True
    assert settlement["estimate"] is False
    assert profile["today"]["estimate"] is True


def test_fixed_coupon_stays_exact(session):
    """Когда объявленный купон сходится с биржей, оценкой ничего не метим."""
    bond = add_bond(
        session, coupon_value=36.4, coupon_period=182,
        next_coupon_date=date(2026, 12, 2),
    )
    session.commit()

    # 70 дней из 182 по купону 36,4 — ровно 14,0
    profile = accrual_service.accrual_profile(
        session, bond, exchange_value=14.0, settle_date=date(2026, 8, 12),
    )
    settlement = profile["settlement"]
    assert settlement["floating"] is False
    assert settlement["estimate"] is False
    assert profile["today"]["estimate"] is False


def test_foreign_bond_is_not_anchored_to_rouble_value(session):
    """У валютного выпуска купон в валюте, а биржевой НКД в рублях.

    Опираться на биржевое число нельзя — оно в других деньгах, и такой
    «якорь» превратил бы доллары в рубли молча.
    """
    bond = add_bond(
        session, secid="USDBOND", face_unit="USD",
        coupon_value=38.23, coupon_period=183,
        next_coupon_date=date(2026, 12, 29),
    )
    session.add(FxRate(source="cbr", code="USD", rate_date=date.today(), value=80.0, nominal=1))
    session.commit()

    profile = accrual_service.accrual_profile(
        session, bond, exchange_value=735.35, settle_date=date(2026, 8, 12),
    )
    settlement = profile["settlement"]
    assert settlement["currency"] == "USD"
    assert settlement["value"] == pytest.approx(9.1919, abs=0.001)
    assert settlement["floating"] is False


# ----------------------------------------------------------------------
# Ход торгов
# ----------------------------------------------------------------------
def test_trade_page_size_rounds_up():
    """Биржа принимает только фиксированные размеры страницы и округляет вниз.

    Поэтому запрос надо поднимать до ближайшего разрешённого, иначе limit=50
    вернёт 10 строк.
    """
    assert _page_size_for(5) == 10
    assert _page_size_for(10) == 10
    assert _page_size_for(20) == 100
    assert _page_size_for(500) == 500
    assert _page_size_for(9000) == 1000


def test_session_stats_from_candles():
    candles = [
        {"begin": datetime(2026, 8, 11, 10), "end": datetime(2026, 8, 11, 10, 9),
         "open": 100.0, "close": 101.0, "high": 101.5, "low": 99.5,
         "volume": 100, "value": 100_000},
        {"begin": datetime(2026, 8, 11, 10, 10), "end": datetime(2026, 8, 11, 10, 19),
         "open": 101.0, "close": 102.0, "high": 102.5, "low": 100.5,
         "volume": 200, "value": 205_000},
    ]
    stats = intraday_service._session_stats(candles, [])
    assert stats["open"] == 100.0
    assert stats["last"] == 102.0
    assert stats["high"] == 102.5
    assert stats["low"] == 99.5
    assert stats["volume"] == 300
    assert stats["change_pct"] == pytest.approx(2.0)


def test_last_trade_is_fresher_than_last_candle():
    """Последняя свеча ещё не закрыта, а сделка уже прошла."""
    candles = [
        {"begin": datetime(2026, 8, 11, 10), "end": datetime(2026, 8, 11, 10, 9),
         "open": 100.0, "close": 101.0, "high": 101.0, "low": 100.0,
         "volume": 100, "value": 100_000},
    ]
    stats = intraday_service._session_stats(candles, [{"price": 103.0}])
    assert stats["last"] == 103.0
    assert stats["change_pct"] == pytest.approx(3.0)


def test_session_without_trades_is_empty():
    stats = intraday_service._session_stats([], [])
    assert stats["open"] is None
    assert stats["volume"] == 0.0


def test_weighted_price_of_bond_is_percent_of_face(session):
    """Оборот в рублях, объём в штуках — СВЦ приводим к процентам номинала."""
    bond = add_bond(session, face_value=1000.0)
    session.commit()
    candles = [{"volume": 100, "value": 54_400.0}]
    # 54 400 / 100 = 544 ₽ за бумагу при номинале 1000 = 54.4%
    assert intraday_service._weighted_price(candles, bond) == pytest.approx(54.4)


def test_trade_flow_counts_initiative():
    trades = [
        {"side": "B", "value": 750.0},
        {"side": "S", "value": 250.0},
        {"side": None, "value": 999.0},
    ]
    flow = intraday_service._trade_flow(trades)
    assert flow["buy_value"] == 750.0
    assert flow["sell_value"] == 250.0
    assert flow["buy_share_pct"] == 75.0
    assert flow["buy_trades"] == 1
    assert flow["sell_trades"] == 1
    assert flow["trades"] == 3


def test_block_trade_does_not_erase_the_other_side():
    """Одна крупная заявка не должна округляться до «сделок продажи не было».

    Доля считается по деньгам, поэтому блок на 272 млн против 18 тыс. даёт
    99,99% — но продажи были, и показывать ровно 100% нельзя.
    """
    trades = [{"side": "B", "value": 272_830_977.0}] + [
        {"side": "S", "value": 2_000.0} for _ in range(8)
    ]
    flow = intraday_service._trade_flow(trades)
    assert flow["buy_share_pct"] == 99.9
    assert flow["sell_trades"] == 8
