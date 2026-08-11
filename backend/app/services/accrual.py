"""НКД на произвольную дату.

Биржа отдаёт в срезе один-единственный НКД — на дату расчётов (сегодня + 1
рабочий день для режима T+1). Увидеть накопленный доход на начало торгов, на
сегодня или на любую другую дату по этому полю нельзя, а казначею это нужно:
покупка и продажа считаются по разным датам расчётов, а переоценка позиции —
по сегодняшней.

Поэтому НКД считается из графика купонов. Формула та же, по которой считает
биржа: доля купонного периода, прошедшая к дате.

    НКД = C × (дата − начало периода) / (конец периода − начало периода)

где C — величина купона за период в валюте номинала. Купонные периоды берутся
из графика (раскрытие НРД через зеркало MOEX): он учитывает и неравные
периоды, и изменение купона у выпусков с плавающей ставкой. Если графика нет,
период восстанавливается из справочника площадки по ближайшему купону и длине
периода — результат тот же для выпусков с постоянным купоном.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CorpAction, Instrument

#: Относительное расхождение с биржевым НКД, при котором стоит предупредить.
#: Биржа округляет НКД, а по валютным выпускам ещё и пересчитывает его в рубли
#: по курсу своей даты, поэтому точного совпадения до копейки не бывает.
TOLERANCE_PCT = 1.0
#: Абсолютный порог для рублёвых копеек: процент от нуля бессмыслен
TOLERANCE_ABS = 0.05

#: Насколько объявленный купон может расходиться с биржевым НКД, оставаясь
#: объяснимым округлением. Больше — значит купон был лишь прогнозом.
_ANCHOR_TOLERANCE = 0.005


def _is_rouble(instrument: Instrument) -> bool:
    """Номинал выражен в рублях — значит купон и биржевой НКД в одних деньгах."""
    return (instrument.face_unit or "SUR").upper() in ("SUR", "RUB", "RUR")


def _coupon_schedule(session: Session, instrument: Instrument) -> list[CorpAction]:
    """Купоны выпуска по возрастанию даты."""
    if not instrument.isin:
        return []
    return list(
        session.execute(
            select(CorpAction)
            .where(
                CorpAction.isin == instrument.isin,
                CorpAction.action_type == "coupon",
            )
            .order_by(CorpAction.action_date)
        ).scalars()
    )


def _period_from_schedule(
    coupons: Sequence[CorpAction], on_date: date
) -> tuple[date, date, float | None] | None:
    """Купонный период, в который попадает дата: начало, конец, величина купона."""
    for index, coupon in enumerate(coupons):
        # В день выплаты купонный период закрывается, а НКД обнуляется:
        # покупатель этого дня купон уже не получает. Поэтому дату самого
        # купона относим к следующему периоду, а не к заканчивающемуся.
        if coupon.action_date <= on_date:
            continue
        # Начало периода: дата предыдущего купона, а если её нет — поле
        # start_date самого купона (биржа отдаёт его в графике)
        if index > 0:
            start = coupons[index - 1].action_date
        elif coupon.start_date:
            start = coupon.start_date
        else:
            return None
        if start >= coupon.action_date:
            return None
        return start, coupon.action_date, coupon.value
    return None


def _period_from_reference(
    instrument: Instrument, on_date: date
) -> tuple[date, date, float | None] | None:
    """Купонный период из справочника площадки.

    Работает, пока купон постоянный: период отсчитывается от ближайшей
    купонной даты назад на длину периода.
    """
    next_coupon = instrument.next_coupon_date
    period = instrument.coupon_period
    if not next_coupon or not period or period <= 0:
        return None

    end = next_coupon
    # Если справочник отстал и ближайший купон уже прошёл, шагаем вперёд.
    # Равенство тоже шагает: в день выплаты начинается новый период.
    guard = 0
    while end <= on_date and guard < 40:
        end = end + timedelta(days=period)
        guard += 1
    start = end - timedelta(days=period)
    return start, end, instrument.coupon_value


def accrued_on(
    session: Session,
    instrument: Instrument,
    on_date: date,
    *,
    coupons: Sequence[CorpAction] | None = None,
    exchange_value: float | None = None,
    settle_date: date | None = None,
) -> dict[str, Any] | None:
    """НКД выпуска на дату.

    Возвращает и сам НКД, и то, из чего он получен: дату начала периода,
    сколько дней прошло и по какому источнику посчитано. Без этого число
    невозможно проверить, а проверять его приходится — на нём считается сумма
    сделки.

    Величина купона известна не всегда: у выпусков с плавающей ставкой ставка
    следующих периодов ещё не объявлена, и в графике на её месте пусто или
    ноль. Тогда купон восстанавливается из биржевого НКД: внутри периода он
    растёт линейно, поэтому по одной известной точке и границам периода
    считается любая другая дата. Такой НКД выражен в валюте расчётов (рублях),
    о чём говорит поле ``currency``.
    """
    if instrument.kind != "bond":
        return None

    if coupons is None:
        coupons = _coupon_schedule(session, instrument)

    period = _period_from_schedule(coupons, on_date) if coupons else None
    source = "график купонов (раскрытие НРД)"
    if period is None:
        period = _period_from_reference(instrument, on_date)
        source = "справочник MOEX"
    if period is None:
        # Купонного периода нет вовсе — так выглядят дисконтные выпуски и
        # уже погашенные бумаги. У них НКД равен нулю по определению, и это
        # утверждение, а не пропуск: прочерк вместо нуля читался бы как сбой.
        #
        # Нужно подтверждение, что периода нет, а не что данных не хватило:
        # либо биржа сама показывает ноль, либо в справочнике стоит явная
        # нулевая длина периода. Отсутствие котировки подтверждением не
        # считается — иначе купонная бумага без среза получила бы ложный ноль.
        if not coupons and (exchange_value == 0 or instrument.coupon_period == 0):
            return {
                "date": on_date,
                "value": 0.0,
                "coupon_value": 0.0,
                "period_start": None,
                "period_end": None,
                "days_passed": 0,
                "days_total": 0,
                "days_left": 0,
                "face_unit": instrument.face_unit,
                "source": "бескупонный выпуск: купонных периодов нет",
                "value_basis": "face",
                "floating": False,
                "estimate": False,
                "note": "Купона нет, поэтому накапливать нечего.",
            }
        return None

    start, end, coupon_value = period
    days_total = (end - start).days
    if days_total <= 0:
        return None
    days_passed = (on_date - start).days
    # Дата вне периода означает, что график не покрывает запрошенный день
    if days_passed < 0 or days_passed > days_total:
        return None

    currency = "face"
    implied = _implied_coupon(exchange_value, settle_date, start, end, days_total)

    # Ноль здесь означает «ставка не объявлена», а не бескупонный выпуск:
    # у настоящего дисконтного выпуска не бывает купонного периода
    if not coupon_value:
        if implied is None:
            return None
        coupon_value = implied
        source = "биржевой НКД и границы купонного периода"
        currency = "settlement"
    elif implied is not None and _is_rouble(instrument):
        # Объявленный купон — не всегда факт. У выпусков с ежедневным
        # начислением по плавающей ставке НКД внутри периода растёт неровно,
        # и линейная доля объявленного купона расходится с тем, что биржа
        # реально считает к расчётам. Биржевое значение — деньги по сделке,
        # поэтому при расхождении больше копейки верим ему, а объявленный
        # купон считаем прогнозом.
        #
        # Сравнение имеет смысл только для рублёвых выпусков: у валютных
        # купон выражен в валюте номинала, а биржевой НКД — в рублях.
        days_at_settle = (settle_date - start).days
        projected = coupon_value * days_at_settle / days_total
        if abs(projected - exchange_value) > _ANCHOR_TOLERANCE:
            coupon_value = implied
            source = "биржевой НКД и границы купонного периода"
            currency = "settlement"

    # Ставка периода считается твёрдой, когда купон объявлен и совпадает с
    # тем, что биржа считает к расчётам. Если пришлось опереться на биржевой
    # НКД, значит внутри периода ставка меняется день ото дня — точное
    # значение известно только на дату расчётов, остальное оценка.
    floating = currency == "settlement"
    # В день начала периода накоплено ноль при любой ставке, поэтому оценкой
    # такое значение не является — оно точное само по себе
    estimate = floating and on_date != settle_date and days_passed > 0

    return {
        "date": on_date,
        "value": round(coupon_value * days_passed / days_total, 4),
        "coupon_value": round(coupon_value, 4),
        "period_start": start,
        "period_end": end,
        "days_passed": days_passed,
        "days_total": days_total,
        "days_left": days_total - days_passed,
        "face_unit": instrument.face_unit,
        "source": source,
        #: face — в валюте номинала, settlement — уже в рублях расчётов
        "value_basis": currency,
        #: Ставка периода не зафиксирована: выпуск с плавающим купоном
        "floating": floating,
        #: Значение на эту дату — оценка, а не точное число биржи
        "estimate": estimate,
        "note": (
            "Плавающий купон: ставка меняется внутри периода, поэтому точное "
            "значение биржа публикует только на дату расчётов. На остальные "
            "даты показана оценка — доля периода от фактически накопленного."
            if floating else
            "Купон периода объявлен, НКД растёт равномерно — значение точное "
            "на любую дату периода."
        ),
    }


def _implied_coupon(
    exchange_value: float | None,
    settle_date: date | None,
    start: date,
    end: date,
    days_total: int,
) -> float | None:
    """Купон, восстановленный из биржевого НКД на дату расчётов."""
    if exchange_value is None or not settle_date:
        return None
    if not (start < settle_date <= end):
        return None
    days_at_settle = (settle_date - start).days
    if days_at_settle <= 0:
        return None
    return exchange_value * days_total / days_at_settle


def accrual_profile(
    session: Session,
    instrument: Instrument,
    *,
    exchange_value: float | None = None,
    settle_date: date | None = None,
    on_date: date | None = None,
) -> dict[str, Any]:
    """НКД на сегодня, на дату расчётов и на выбранную дату — вместе.

    ``exchange_value`` — НКД из биржевого среза. Он относится к дате расчётов,
    и мы показываем его рядом с расчётным: расхождение больше копеек означает,
    что график купонов устарел, и это лучше видеть, чем не видеть.
    """
    today = date.today()
    coupons = _coupon_schedule(session, instrument)

    def _at(day: date | None) -> dict[str, Any] | None:
        if day is None:
            return None
        return accrued_on(
            session, instrument, day, coupons=coupons,
            exchange_value=exchange_value, settle_date=settle_date,
        )

    # НКД по графику считается в валюте номинала, а биржа отдаёт его уже в
    # рублях расчётов. Без пересчёта валютный выпуск выглядит как ошибка в
    # графике: 9 долларов против 738 рублей.
    from .fx import FxBook, instrument_currency

    fx = FxBook(session)
    currency = instrument_currency(instrument)
    rate = fx.rate(currency) or 1.0

    def _with_rub(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        # Восстановленный из биржевого НКД купон уже выражен в рублях
        # расчётов — второй раз по курсу его пересчитывать нельзя
        in_rubles = row.get("value_basis") == "settlement"
        row["currency"] = "RUB" if in_rubles else currency
        row["value_rub"] = round(row["value"] * (1.0 if in_rubles else rate), 2)
        row["fx_rate"] = 1.0 if in_rubles else round(rate, 6)
        return row

    today_row = _with_rub(_at(today))
    settlement_row = _with_rub(_at(settle_date))
    chosen_row = (
        _with_rub(_at(on_date))
        if on_date and on_date not in (today, settle_date)
        else None
    )

    mismatch = None
    if exchange_value is not None and settlement_row is not None:
        difference = round(settlement_row["value_rub"] - exchange_value, 4)
        scale = max(abs(exchange_value), TOLERANCE_ABS)
        if abs(difference) > TOLERANCE_ABS and abs(difference) / scale * 100 > TOLERANCE_PCT:
            mismatch = {
                "difference": difference,
                "note": (
                    "Расчёт по графику купонов расходится с биржевым НКД больше "
                    f"чем на {TOLERANCE_PCT:g}%. Проверьте, не устарел ли график "
                    "купонов — обновите справочники."
                ),
            }

    return {
        "secid": instrument.secid,
        "isin": instrument.isin,
        "currency": currency,
        "today": today_row,
        "settlement": settlement_row,
        "selected": chosen_row,
        "settle_date": settle_date,
        "exchange_value": exchange_value,
        "exchange_note": (
            "Биржа отдаёт НКД на дату расчётов, а не на сегодня: в режиме T+1 "
            "это следующий торговый день. Значения на другие даты рассчитаны "
            "по графику купонов."
            + (
                f" Купон выражен в {currency}, а биржевой НКД — в рублях "
                "расчётов, поэтому рядом показан пересчёт по курсу ЦБ."
                if currency != "RUB"
                else ""
            )
        ),
        "mismatch": mismatch,
        "has_schedule": bool(coupons),
    }
