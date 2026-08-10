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

#: Виды лимитов и их описание для интерфейса
LIMIT_KINDS: dict[str, dict[str, str]] = {
    "instrument_share": {
        "title": "Доля одной бумаги",
        "unit": "%",
        "target": "Код бумаги (пусто — для любой)",
        "hint": "Ограничивает вес выпуска в портфеле",
    },
    "issuer_share": {
        "title": "Доля эмитента",
        "unit": "%",
        "target": "Наименование эмитента (пусто — для любого)",
        "hint": "Несколько выпусков одного заёмщика складываются в общий риск",
    },
    "currency_share": {
        "title": "Доля валюты",
        "unit": "%",
        "target": "Код валюты, например USD",
        "hint": "Ограничивает валютную переоценку портфеля",
    },
    "list_level_share": {
        "title": "Доля уровня листинга",
        "unit": "%",
        "target": "Уровень: 1, 2 или 3",
        "hint": "Обычно ограничивают долю третьего уровня",
    },
    "illiquid_share": {
        "title": "Доля неликвида",
        "unit": "%",
        "target": "Порог ликвидности, по умолчанию 40",
        "hint": "Доля бумаг с оценкой ликвидности ниже порога",
    },
    "duration_max": {
        "title": "Дюрация не выше",
        "unit": "лет",
        "target": "",
        "hint": "Ограничивает процентный риск портфеля",
    },
    "duration_min": {
        "title": "Дюрация не ниже",
        "unit": "лет",
        "target": "",
        "hint": "Не даёт портфелю уйти в слишком короткие бумаги",
    },
    "position_value": {
        "title": "Стоимость позиции не выше",
        "unit": "₽",
        "target": "Код бумаги (пусто — для любой)",
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
