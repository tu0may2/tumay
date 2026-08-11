"""Развёрнутый анализ облигаций.

Собирает по каждому выпуску всё, что даёт открытый рынок: доходность, дюрацию,
премию к безрисковой кривой, параметры купона, наличие оферты и амортизации,
ликвидность. Кредитные рейтинги агентств (АКРА, Эксперт РА) в открытом доступе
машиночитаемо не публикуются, поэтому их здесь нет — вместо рейтинга даётся
рыночная оценка риска, honestly помеченная как расчётная.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CorpAction, Instrument
from .analytics import (
    build_row,
    curve_yield_at,
    latest_rows,
    yield_curve,
)
from .accrual import accrued_on
from .treasury_extras import after_tax_yield

#: Типы купона
COUPON_FIXED = "fixed"
COUPON_FLOAT = "float"
COUPON_STRUCTURED = "structured"
COUPON_LINKER = "linker"
COUPON_DISCOUNT = "discount"
COUPON_UNKNOWN = "unknown"

COUPON_TITLES = {
    COUPON_FIXED: "Фиксированный",
    COUPON_FLOAT: "Плавающий",
    COUPON_STRUCTURED: "Структурная",
    COUPON_LINKER: "Индексируемый номинал",
    COUPON_DISCOUNT: "Дисконтная",
    COUPON_UNKNOWN: "Не определён",
}

#: Классификация по типу выпуска из справочника MOEX. Это поле заполнено у всех
#: бумаг, тогда как график выплат НРД подгружается лишь по части выпусков,
#: поэтому справочник здесь — основной источник, а график только уточняет.
_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("флоат", COUPON_FLOAT),
    ("структурн", COUPON_STRUCTURED),
    ("линкер", COUPON_LINKER),
    ("индексируем", COUPON_LINKER),
    ("дисконт", COUPON_DISCOUNT),
    ("фикс", COUPON_FIXED),
    ("амортизируем", COUPON_FIXED),
    ("валютные", COUPON_FIXED),
    ("конвертируем", COUPON_FIXED),
)


def _coupon_type_from_moex(bond_type: str | None) -> str:
    if not bond_type:
        return COUPON_UNKNOWN
    lowered = bond_type.lower()
    for marker, code in _TYPE_MARKERS:
        if marker in lowered:
            return code
    return COUPON_UNKNOWN


def _coupon_profile(
    actions: Sequence[CorpAction], instrument: Instrument
) -> dict[str, Any]:
    """Тип купона, амортизация и оферта по справочнику MOEX и графику НРД."""
    bond_type = instrument.bond_type
    coupon_type = _coupon_type_from_moex(bond_type)

    coupons = sorted(
        (a for a in actions if a.action_type == "coupon"),
        key=lambda a: a.action_date,
    )
    amortizations = [a for a in actions if a.action_type == "amortization"]
    offers = [a for a in actions if a.action_type == "offer"]
    today = date.today()

    # Если справочник молчит, пробуем определить тип по будущим купонам
    if coupon_type == COUPON_UNKNOWN:
        future = [a for a in coupons if a.action_date >= today and a.value is not None]
        if len(future) >= 2:
            values = {round(a.value, 6) for a in future}
            coupon_type = COUPON_FIXED if len(values) == 1 else COUPON_FLOAT
        elif coupons:
            coupon_type = COUPON_FIXED

    # Амортизация видна из типа выпуска; в графике погашение тоже приходит
    # как амортизация, поэтому реальная амортизация — это два транша и более
    has_amortization = bool(bond_type and "амортизируем" in bond_type.lower())
    if len(amortizations) > 1:
        has_amortization = True

    next_coupon = next((a for a in coupons if a.action_date >= today), None)

    return {
        "coupon_type": coupon_type,
        "coupon_type_title": COUPON_TITLES[coupon_type],
        "has_amortization": has_amortization,
        "amortization_count": max(0, len(amortizations) - 1),
        "has_offer": bool(offers),
        "next_coupon_date": (
            next_coupon.action_date if next_coupon else instrument.next_coupon_date
        ),
        "next_coupon_value": (
            next_coupon.value if next_coupon else instrument.coupon_value
        ),
        "schedule_known": bool(coupons),
    }


def _risk_score(row: dict[str, Any]) -> dict[str, Any]:
    """Рыночная оценка риска выпуска — не рейтинг агентства.

    Строится из наблюдаемых величин: премии к безрисковой кривой, уровня
    листинга и ликвидности. Чем выше балл, тем выше наблюдаемый риск.
    """
    points = 0.0
    reasons: list[str] = []

    spread = row.get("spread_to_curve_bp")
    if spread is not None:
        if spread > 1500:
            points += 55
            reasons.append("премия к КБД выше 1500 бп")
        elif spread > 800:
            points += 38
            reasons.append("премия к КБД 800–1500 бп")
        elif spread > 400:
            points += 22
            reasons.append("премия к КБД 400–800 бп")
        elif spread > 150:
            points += 10
        else:
            points += 3

    level = row.get("list_level")
    if level == 3:
        points += 22
        reasons.append("третий уровень листинга")
    elif level == 2:
        points += 10
    elif level == 1:
        points += 2

    liquidity = row.get("liquidity_score")
    if liquidity is not None:
        if liquidity < 20:
            points += 20
            reasons.append("низкая ликвидность")
        elif liquidity < 45:
            points += 10
    else:
        points += 12
        reasons.append("нет данных о торгах")

    score = round(min(points, 100), 1)
    if score >= 60:
        band = "высокий"
    elif score >= 35:
        band = "повышенный"
    elif score >= 15:
        band = "умеренный"
    else:
        band = "низкий"

    return {
        "risk_score": score,
        "risk_band": band,
        "risk_reasons": reasons,
    }


def _years_to(target: date | None) -> float | None:
    if target is None:
        return None
    return round((target - date.today()).days / 365, 2)


def analyse(
    session: Session,
    *,
    search: str | None = None,
    min_yield: float | None = None,
    max_yield: float | None = None,
    min_duration_years: float | None = None,
    max_duration_years: float | None = None,
    maturity_from: date | None = None,
    maturity_to: date | None = None,
    min_turnover: float | None = None,
    min_liquidity: float | None = None,
    list_levels: Sequence[int] | None = None,
    currencies: Sequence[str] | None = None,
    coupon_types: Sequence[str] | None = None,
    has_offer: bool | None = None,
    has_amortization: bool | None = None,
    max_risk_score: float | None = None,
    sort_by: str = "yield_pct",
    descending: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Отобрать и разметить облигации по заданным условиям."""
    curve = yield_curve(session)
    rows_raw = latest_rows(session, kinds=("bond",))

    today = date.today()

    # График выплат тянем одним запросом на все выпуски сразу
    isins = {inst.isin for inst, _ in rows_raw if inst.isin}
    actions_by_isin: dict[str, list[CorpAction]] = defaultdict(list)
    if isins:
        for action in session.execute(
            select(CorpAction).where(CorpAction.isin.in_(isins))
        ).scalars():
            actions_by_isin[action.isin].append(action)

    # Для НКД нужны только купоны и обязательно по возрастанию даты: расчёт
    # берёт границы периода из соседних записей
    coupons_by_isin: dict[str, list[CorpAction]] = {
        isin: sorted(
            (a for a in actions if a.action_type == "coupon"),
            key=lambda a: a.action_date,
        )
        for isin, actions in actions_by_isin.items()
    }

    from .fx import FxBook, instrument_currency

    fx = FxBook(session)
    rows: list[dict[str, Any]] = []
    for instrument, quote in rows_raw:
        row = build_row(
            instrument, quote, curve["points"],
            fx_rate=fx.rate(instrument_currency(instrument)) or 1.0,
        )
        profile = _coupon_profile(
            actions_by_isin.get(instrument.isin or "", []), instrument
        )
        # Оферта известна и из справочника площадки, и из графика НРД
        profile["has_offer"] = profile["has_offer"] or instrument.offer_date is not None
        row.update(profile)
        row["years_to_maturity"] = _years_to(instrument.maturity_date)
        row["years_to_offer"] = _years_to(instrument.offer_date)
        row.update(_risk_score(row))

        # Текущая доходность купона к цене — простой ориентир денежного потока
        price = row.get("last") or row.get("wa_price")
        if instrument.coupon_percent and price:
            row["current_yield_pct"] = round(
                instrument.coupon_percent / price * 100, 2
            )
        else:
            row["current_yield_pct"] = None

        # Сравнивать выпуски правильнее по доходности, которая останется
        # после налога: купон и переоценка облагаются по разным ставкам
        row["after_tax_yield_pct"] = after_tax_yield(
            row.get("yield_pct"), instrument.coupon_percent, price
        )

        # НКД из среза относится к дате расчётов. На сегодня его считаем сами
        row["settle_date"] = quote.settle_date if quote else None
        accrued_today = accrued_on(
            session, instrument, today,
            coupons=coupons_by_isin.get(instrument.isin or "", []),
            # У флоатеров ставка периода не раскрыта — тогда НКД
            # восстанавливается из биржевого значения
            exchange_value=quote.accrued_interest if quote else None,
            settle_date=quote.settle_date if quote else None,
        )
        row["accrued_today"] = accrued_today["value"] if accrued_today else None
        row["accrued_days_passed"] = (
            accrued_today["days_passed"] if accrued_today else None
        )
        row["accrued_days_left"] = accrued_today["days_left"] if accrued_today else None
        # Пометка для таблицы: у флоатера НКД на дату, кроме расчётной, — оценка
        row["accrued_estimate"] = bool(accrued_today and accrued_today["estimate"])
        row["accrued_floating"] = bool(accrued_today and accrued_today["floating"])
        # Отдельным текстом для выгрузки: в Excel подсказку не наведёшь
        row["accrued_basis"] = (
            None if accrued_today is None
            else "оценка: плавающий купон" if accrued_today["estimate"]
            else "точно"
        )

        rows.append(row)

    def _keep(row: dict[str, Any]) -> bool:
        if search:
            needle = search.strip().lower()
            haystack = " ".join(
                str(row.get(key) or "").lower()
                for key in ("secid", "isin", "name", "full_name")
            )
            if needle not in haystack:
                return False
        yield_pct = row.get("yield_pct")
        if min_yield is not None and (yield_pct is None or yield_pct < min_yield):
            return False
        if max_yield is not None and (yield_pct is None or yield_pct > max_yield):
            return False
        duration = row.get("duration_years")
        if min_duration_years is not None and (duration is None or duration < min_duration_years):
            return False
        if max_duration_years is not None and (duration is None or duration > max_duration_years):
            return False
        maturity = row.get("maturity_date")
        if maturity_from is not None and (maturity is None or maturity < maturity_from):
            return False
        if maturity_to is not None and (maturity is None or maturity > maturity_to):
            return False
        if min_turnover is not None and (row.get("turnover") or 0) < min_turnover:
            return False
        if min_liquidity is not None and (row.get("liquidity_score") or 0) < min_liquidity:
            return False
        if list_levels and row.get("list_level") not in list_levels:
            return False
        if currencies and (row.get("currency") or "").upper() not in currencies:
            return False
        if coupon_types and row.get("coupon_type") not in coupon_types:
            return False
        if has_offer is not None and bool(row.get("has_offer")) is not has_offer:
            return False
        if has_amortization is not None and bool(row.get("has_amortization")) is not has_amortization:
            return False
        if max_risk_score is not None and (row.get("risk_score") or 0) > max_risk_score:
            return False
        return True

    rows = [row for row in rows if _keep(row)]
    total = len(rows)

    present = [row for row in rows if row.get(sort_by) is not None]
    missing = [row for row in rows if row.get(sort_by) is None]
    present.sort(key=lambda row: row[sort_by], reverse=descending)
    rows = present + missing

    return {
        "total": total,
        "curve_date": curve["curve_date"],
        "items": rows[offset : offset + limit],
    }


#: Колонки выгрузки анализа облигаций в Excel
ANALYSIS_COLUMNS: tuple[dict[str, Any], ...] = (
    {"code": "secid", "title": "Код", "kind": "text"},
    {"code": "isin", "title": "ISIN", "kind": "text"},
    {"code": "name", "title": "Выпуск", "kind": "text"},
    {"code": "full_name", "title": "Полное наименование", "kind": "text"},
    {"code": "maturity_date", "title": "Погашение", "kind": "date"},
    {"code": "years_to_maturity", "title": "Лет до погашения", "kind": "number", "digits": 2},
    {"code": "offer_date", "title": "Оферта", "kind": "date"},
    {"code": "last", "title": "Цена, %", "kind": "number", "digits": 4},
    {"code": "prev_wa_price", "title": "СВЦ пред. дня, %", "kind": "number", "digits": 4},
    {"code": "accrued_interest", "title": "НКД на расчёты", "kind": "number", "digits": 2},
    {"code": "accrued_today", "title": "НКД на сегодня", "kind": "number", "digits": 4},
    {"code": "accrued_days_passed", "title": "Дней купона прошло", "kind": "number", "digits": 0},
    {"code": "accrued_days_left", "title": "Дней до купона", "kind": "number", "digits": 0},
    {"code": "dirty_price", "title": "Полная цена, %", "kind": "number", "digits": 4},
    {"code": "settlement_amount", "title": "К оплате за бумагу", "kind": "number", "digits": 2},
    {"code": "yield_pct", "title": "Доходность, %", "kind": "number", "digits": 2},
    {"code": "current_yield_pct", "title": "Текущая доходность, %", "kind": "number", "digits": 2},
    {"code": "after_tax_yield_pct", "title": "Доходность после налога, %", "kind": "number", "digits": 2},
    {"code": "curve_yield_pct", "title": "КБД на дюрации, %", "kind": "number", "digits": 2},
    {"code": "spread_to_curve_bp", "title": "Премия к КБД, бп", "kind": "number", "digits": 0},
    {"code": "z_spread_bp", "title": "Z-спред, бп", "kind": "number", "digits": 0},
    {"code": "duration_years", "title": "Дюрация, лет", "kind": "number", "digits": 2},
    {"code": "coupon_percent", "title": "Купон, %", "kind": "number", "digits": 2},
    {"code": "coupon_type_title", "title": "Тип купона", "kind": "text"},
    {"code": "coupon_period", "title": "Период купона, дней", "kind": "number", "digits": 0},
    {"code": "next_coupon_date", "title": "Ближайший купон", "kind": "date"},
    {"code": "has_amortization", "title": "Амортизация", "kind": "text"},
    {"code": "has_offer", "title": "Оферта есть", "kind": "text"},
    {"code": "face_value", "title": "Номинал", "kind": "number", "digits": 2},
    {"code": "currency", "title": "Валюта", "kind": "text"},
    {"code": "turnover", "title": "Оборот, ₽", "kind": "number", "digits": 2},
    {"code": "volume", "title": "Объём, шт", "kind": "number", "digits": 0},
    {"code": "num_trades", "title": "Сделок", "kind": "number", "digits": 0},
    {"code": "liquidity_score", "title": "Ликвидность, 0–100", "kind": "number", "digits": 0},
    {"code": "list_level", "title": "Уровень листинга", "kind": "number", "digits": 0},
    {"code": "risk_score", "title": "Оценка риска (расчётная)", "kind": "number", "digits": 1},
    {"code": "risk_band", "title": "Уровень риска", "kind": "text"},
    {"code": "bond_type", "title": "Тип выпуска", "kind": "text"},
)


def rows_for_export(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Подготовить строки анализа к записи в файл."""
    prepared = []
    for item in items:
        row = dict(item)
        # В таблице Excel «да/нет» читается лучше, чем true/false
        row["has_amortization"] = "да" if item.get("has_amortization") else "нет"
        row["has_offer"] = "да" if item.get("has_offer") else "нет"
        prepared.append(row)
    return prepared
