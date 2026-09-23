"""Лимиты казначейства и контроль их соблюдения.

Лимит отвечает на вопрос «можно ли столько купить». Проверка работает и по
факту (что нарушено сейчас), и до сделки — чтобы увидеть нарушение заранее,
а не в отчёте на следующий день.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Instrument, Limit
from .fx import instrument_currency, is_rub
from .portfolio import compute_positions, price_multiplier

#: Чем заполняется поле «к чему относится» у каждого вида лимита.
#: Интерфейс выбирает по этому признаку управляющий элемент: список бумаг,
#: список эмитентов, список валют, уровень листинга, число или ничего.
#: Свободный ввод одним полем на все виды — источник ошибок: в лимит на
#: эмитента попадал ISIN, и лимит молча не срабатывал ни на одной позиции.
TARGET_INSTRUMENT = "instrument"
TARGET_ISSUER = "issuer"
TARGET_CURRENCY = "currency"
TARGET_LIST_LEVEL = "list_level"
TARGET_NUMBER = "number"
TARGET_NONE = "none"

#: Виды лимитов и их описание для интерфейса
LIMIT_KINDS: dict[str, dict[str, str]] = {
    "instrument_share": {
        "title": "Доля одной бумаги",
        "unit": "%",
        "target": "Код бумаги (пусто — для любой)",
        "target_type": TARGET_INSTRUMENT,
        "target_label": "Бумага",
        "hint": "Ограничивает вес выпуска в портфеле",
    },
    "issuer_share": {
        "title": "Доля эмитента",
        "unit": "%",
        "target": "Наименование эмитента (пусто — для любого)",
        "target_type": TARGET_ISSUER,
        "target_label": "Эмитент",
        "hint": "Несколько выпусков одного заёмщика складываются в общий риск",
    },
    "currency_share": {
        "title": "Доля валюты",
        "unit": "%",
        "target": "Код валюты, например USD",
        "target_type": TARGET_CURRENCY,
        "target_label": "Валюта",
        "hint": "Ограничивает валютную переоценку портфеля",
    },
    "list_level_share": {
        "title": "Доля уровня листинга",
        "unit": "%",
        "target": "Уровень: 1, 2 или 3",
        "target_type": TARGET_LIST_LEVEL,
        "target_label": "Уровень листинга",
        "hint": "Обычно ограничивают долю третьего уровня",
    },
    "illiquid_share": {
        "title": "Доля неликвида",
        "unit": "%",
        "target": "Порог ликвидности, по умолчанию 40",
        "target_type": TARGET_NUMBER,
        "target_label": "Порог ликвидности",
        "hint": "Доля бумаг с оценкой ликвидности ниже порога",
    },
    "duration_max": {
        "title": "Дюрация не выше",
        "unit": "лет",
        "target": "",
        "target_type": TARGET_NONE,
        "target_label": "",
        "hint": "Ограничивает процентный риск портфеля",
    },
    "duration_min": {
        "title": "Дюрация не ниже",
        "unit": "лет",
        "target": "",
        "target_type": TARGET_NONE,
        "target_label": "",
        "hint": "Не даёт портфелю уйти в слишком короткие бумаги",
    },
    "position_value": {
        "title": "Стоимость позиции не выше",
        "unit": "₽",
        "target": "Код бумаги (пусто — для любой)",
        "target_type": TARGET_INSTRUMENT,
        "target_label": "Бумага",
        "hint": "Абсолютное ограничение вложения в один выпуск",
    },
}

#: Порог ликвидности по умолчанию для лимита на неликвид
DEFAULT_ILLIQUID_THRESHOLD = 40.0


@dataclass(slots=True)
class Usage:
    """Фактическое значение по лимиту."""

    subject: str
    value: float
    detail: str = ""


def _positions_value(positions: Sequence[dict[str, Any]]) -> float:
    return sum(p["market_value_rub"] or 0 for p in positions)


def _usages(
    limit: Limit, positions: Sequence[dict[str, Any]], total: float
) -> list[Usage]:
    """Что фактически получилось по данному виду лимита."""
    if not positions:
        return []

    def share(value: float) -> float:
        return value / total * 100 if total else 0.0

    kind = limit.kind
    target = (limit.target or "").strip()

    if kind in ("instrument_share", "position_value"):
        rows = [p for p in positions if not target or p["secid"] == target.upper()]
        return [
            Usage(
                subject=p["secid"],
                value=share(p["market_value_rub"] or 0)
                if kind == "instrument_share"
                else (p["market_value_rub"] or 0),
                detail=p["name"] or "",
            )
            for p in rows
        ]

    if kind == "issuer_share":
        grouped: dict[str, float] = {}
        for position in positions:
            issuer = position.get("issuer") or "не определён"
            if target and issuer.lower() != target.lower():
                continue
            grouped[issuer] = grouped.get(issuer, 0) + (position["market_value_rub"] or 0)
        return [Usage(subject=name, value=share(value)) for name, value in grouped.items()]

    if kind == "currency_share":
        grouped = {}
        for position in positions:
            code = position.get("currency") or "RUB"
            if target and code.upper() != target.upper():
                continue
            grouped[code] = grouped.get(code, 0) + (position["market_value_rub"] or 0)
        return [Usage(subject=code, value=share(value)) for code, value in grouped.items()]

    if kind == "list_level_share":
        grouped = {}
        for position in positions:
            level = position.get("list_level")
            if level is None:
                continue
            if target and str(level) != target:
                continue
            grouped[str(level)] = grouped.get(str(level), 0) + (position["market_value_rub"] or 0)
        return [
            Usage(subject=f"уровень {level}", value=share(value))
            for level, value in grouped.items()
        ]

    if kind == "illiquid_share":
        try:
            threshold = float(target) if target else DEFAULT_ILLIQUID_THRESHOLD
        except ValueError:
            threshold = DEFAULT_ILLIQUID_THRESHOLD
        illiquid = sum(
            p["market_value_rub"] or 0
            for p in positions
            if (p.get("liquidity_score") or 0) < threshold
        )
        return [Usage(subject=f"ликвидность ниже {threshold:.0f}", value=share(illiquid))]

    if kind in ("duration_max", "duration_min"):
        bonds = [p for p in positions if p["duration_years"] is not None and p["market_value_rub"]]
        bond_value = sum(p["market_value_rub"] for p in bonds)
        if not bond_value:
            return []
        duration = sum(p["duration_years"] * p["market_value_rub"] for p in bonds) / bond_value
        return [Usage(subject="портфель", value=duration)]

    return []


def _is_breached(kind: str, actual: float, limit_value: float) -> bool:
    if kind == "duration_min":
        return actual < limit_value
    return actual > limit_value


# ----------------------------------------------------------------------
# Справочник значений для формы лимита
# ----------------------------------------------------------------------
#: Сколько вариантов отдавать сверх того, что есть в портфеле. Выбор из
#: полного справочника биржи — это десятки тысяч строк, в списке они
#: бесполезны, а страницу тормозят.
TARGET_LIMIT = 400

#: Валюты, которые предлагаем всегда, даже если таких бумаг в портфеле нет:
#: лимит обычно ставят заранее, до первой валютной покупки
COMMON_CURRENCIES = ("RUB", "USD", "EUR", "CNY")


def target_options(
    session: Session, *, portfolio: str | None = None
) -> dict[str, Any]:
    """Чем можно заполнить поле «к чему относится» — по каждому виду лимита.

    Бумаги и эмитенты, которые есть в портфеле, идут первыми и помечены:
    лимит чаще ставят на то, что уже куплено, а искать это в общем списке
    справочника неудобно. Остальные варианты остаются доступны — лимит
    полезно выставить и заранее, до первой покупки.
    """
    positions = compute_positions(session, portfolio=portfolio)
    held_secids = {p["secid"] for p in positions if p["quantity"] > 0}

    # Один и тот же выпуск торгуется на нескольких досках, и в справочнике на
    # каждую доску своя запись. В выпадающем списке это выглядело бы как два
    # одинаковых пункта, поэтому оставляем по одной записи на код бумаги —
    # ту, у которой заполнен эмитент: она пришла из полного справочника
    instruments: dict[str, Instrument] = {}
    for instrument in session.execute(
        select(Instrument)
        .where(Instrument.kind.in_(("bond", "share")))
        .order_by(Instrument.secid)
    ).scalars():
        current = instruments.get(instrument.secid)
        if current is None or (not current.issuer and instrument.issuer):
            instruments[instrument.secid] = instrument
    ordered = list(instruments.values())

    # Эмитентов своих бумаг берём по справочнику, а не только из позиций:
    # у записи с другой доски эмитент может быть не заполнен, и тогда
    # собственный эмитент не помечался бы как «в портфеле»
    held_issuers = {
        (instruments[secid].issuer or "").strip()
        for secid in held_secids
        if secid in instruments and (instruments[secid].issuer or "").strip()
    }

    def instrument_option(instrument: Instrument) -> dict[str, Any]:
        return {
            "value": instrument.secid,
            "title": instrument.display_name or instrument.secid,
            "issuer": instrument.issuer,
            "isin": instrument.isin,
            "in_portfolio": instrument.secid in held_secids,
        }

    held = [instrument_option(i) for i in ordered if i.secid in held_secids]
    rest = [
        instrument_option(i) for i in ordered if i.secid not in held_secids
    ][: max(TARGET_LIMIT - len(held), 0)]

    issuers: dict[str, dict[str, Any]] = {}
    for instrument in ordered:
        name = (instrument.issuer or "").strip()
        if not name:
            continue
        entry = issuers.setdefault(
            name,
            {
                "value": name,
                "title": name,
                "issues": 0,
                "in_portfolio": name in held_issuers,
            },
        )
        entry["issues"] += 1
    issuer_list = sorted(
        issuers.values(), key=lambda row: (not row["in_portfolio"], row["title"].lower())
    )[:TARGET_LIMIT]

    currencies = {code: {"value": code, "title": code} for code in COMMON_CURRENCIES}
    for instrument in ordered:
        code = (instrument.face_unit or instrument.currency or "").strip().upper()
        if not code or is_rub(code):
            continue
        currencies.setdefault(code, {"value": code, "title": code})

    levels = sorted(
        {
            instrument.list_level
            for instrument in ordered
            if instrument.list_level is not None
        }
    ) or [1, 2, 3]

    return {
        "portfolio": portfolio,
        "instruments": held + rest,
        "issuers": issuer_list,
        "currencies": list(currencies.values()),
        "list_levels": [
            {"value": str(level), "title": f"{level} уровень"} for level in levels
        ],
        "illiquid_default": DEFAULT_ILLIQUID_THRESHOLD,
    }


def check_limits(
    session: Session,
    *,
    portfolio: str | None = None,
    extra_positions: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Проверить все лимиты портфеля.

    ``extra_positions`` позволяет подмешать гипотетическую сделку и увидеть,
    к чему она приведёт, ещё до её совершения.
    """
    statement = select(Limit).where(Limit.enabled.is_(True))
    if portfolio:
        statement = statement.where(Limit.portfolio == portfolio)
    limits = list(session.execute(statement).scalars())

    positions = [
        p for p in compute_positions(session, portfolio=portfolio) if p["quantity"] > 0
    ]
    if extra_positions:
        positions = _merge_positions(positions, extra_positions)

    # Ликвидность нужна для лимита на неликвид — подтягиваем из витрины
    _attach_liquidity(session, positions)
    total = _positions_value(positions)

    results: list[dict[str, Any]] = []
    for limit in limits:
        meta = LIMIT_KINDS.get(limit.kind, {})
        for usage in _usages(limit, positions, total):
            breached = _is_breached(limit.kind, usage.value, limit.value)
            results.append(
                {
                    "limit_id": limit.id,
                    "kind": limit.kind,
                    "kind_title": meta.get("title", limit.kind),
                    "unit": meta.get("unit", ""),
                    # Лимит действует в пределах одного портфеля, и в таблице
                    # это должно быть видно: без имени портфеля непонятно, к
                    # чему относится строка, когда открыты все портфели сразу
                    "portfolio": limit.portfolio,
                    "target": limit.target,
                    "subject": usage.subject,
                    "detail": usage.detail,
                    "limit_value": limit.value,
                    "actual": round(usage.value, 2),
                    "utilisation_pct": (
                        round(usage.value / limit.value * 100, 1) if limit.value else None
                    ),
                    "breached": breached,
                    "headroom": round(limit.value - usage.value, 2),
                    "comment": limit.comment,
                }
            )

    # Нарушения — наверх, дальше по заполненности лимита
    results.sort(key=lambda row: (not row["breached"], -(row["utilisation_pct"] or 0)))
    return {
        "portfolio": portfolio,
        "total_value": round(total, 2),
        "limits_total": len(limits),
        "breached": sum(1 for row in results if row["breached"]),
        "items": results,
    }


def _attach_liquidity(session: Session, positions: Sequence[dict[str, Any]]) -> None:
    """Добавить в позиции оценку ликвидности из последнего среза."""
    if not positions:
        return
    from .analytics import latest_rows, liquidity_score

    secids = [p["secid"] for p in positions]
    scores = {
        instrument.secid: liquidity_score(quote)
        for instrument, quote in latest_rows(session, secids=secids)
    }
    for position in positions:
        position["liquidity_score"] = scores.get(position["secid"])


def _merge_positions(
    current: Sequence[dict[str, Any]], extra: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Сложить текущие позиции с гипотетическими."""
    merged = {p["secid"]: dict(p) for p in current}
    for addition in extra:
        secid = addition["secid"]
        if secid in merged:
            position = merged[secid]
            position["quantity"] = (position["quantity"] or 0) + addition["quantity"]
            position["market_value_rub"] = (position["market_value_rub"] or 0) + (
                addition["market_value_rub"] or 0
            )
        else:
            merged[secid] = dict(addition)
    return list(merged.values())


def preview_deal(
    session: Session,
    *,
    secid: str,
    quantity: float,
    price: float,
    portfolio: str | None = None,
) -> dict[str, Any]:
    """Как изменится соблюдение лимитов, если сделку совершить."""
    secid = secid.upper()
    instrument = session.execute(
        select(Instrument).where(Instrument.secid == secid).limit(1)
    ).scalar_one_or_none()

    multiplier = price_multiplier(instrument)
    currency = instrument_currency(instrument)
    from .fx import FxBook

    rate = FxBook(session).rate(currency) or 1.0
    value_rub = quantity * price * multiplier * rate

    hypothetical = [
        {
            "secid": secid,
            "name": instrument.display_name if instrument else secid,
            "issuer": instrument.issuer if instrument else None,
            "currency": currency,
            "list_level": instrument.list_level if instrument else None,
            "quantity": quantity,
            "market_value_rub": value_rub,
            "duration_years": None,
            "yield_pct": None,
        }
    ]

    before = check_limits(session, portfolio=portfolio)
    after = check_limits(session, portfolio=portfolio, extra_positions=hypothetical)

    breached_before = {
        (row["kind"], row["subject"]) for row in before["items"] if row["breached"]
    }
    new_breaches = [
        row
        for row in after["items"]
        if row["breached"] and (row["kind"], row["subject"]) not in breached_before
    ]

    return {
        "secid": secid,
        "quantity": quantity,
        "price": price,
        "currency": currency,
        "value_rub": round(value_rub, 2),
        "value_share_pct": (
            round(value_rub / after["total_value"] * 100, 2) if after["total_value"] else None
        ),
        "breached_before": before["breached"],
        "breached_after": after["breached"],
        "new_breaches": new_breaches,
        "allowed": not new_breaches,
        "items": after["items"],
    }
