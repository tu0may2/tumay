"""Портфель казначейства: позиции, финансовый результат, риск-метрики."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Deal, Instrument, Quote
from .analytics import latest_quote_ids


@dataclass(slots=True)
class _Running:
    """Состояние позиции при проходе по сделкам методом средней цены."""

    quantity: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    last_trade_date: date | None = None
    deals: int = 0
    accrued_paid: float = 0.0
    tags: set[str] = field(default_factory=set)


def _price_multiplier(instrument: Instrument | None) -> float:
    """Во что превращается 1 пункт цены за 1 бумагу.

    Облигации котируются в процентах от номинала, акции — в рублях за штуку.
    """
    if instrument is None:
        return 1.0
    if instrument.kind == "bond" and instrument.face_value:
        return instrument.face_value / 100.0
    return 1.0


def compute_positions(
    session: Session, *, portfolio: str | None = None
) -> list[dict[str, Any]]:
    """Свернуть сделки в позиции и оценить их по последнему рынку."""
    statement = select(Deal).order_by(Deal.trade_date, Deal.id)
    if portfolio:
        statement = statement.where(Deal.portfolio == portfolio)
    deals = list(session.execute(statement).scalars())
    if not deals:
        return []

    running: dict[str, _Running] = {}
    for deal in deals:
        state = running.setdefault(deal.secid, _Running())
        state.deals += 1
        state.last_trade_date = deal.trade_date
        state.fees += deal.fee or 0.0
        state.tags.add(deal.portfolio)

        quantity = abs(deal.quantity)
        if deal.side == "buy":
            total_cost = state.avg_price * state.quantity + deal.price * quantity
            state.quantity += quantity
            state.avg_price = total_cost / state.quantity if state.quantity else 0.0
            state.accrued_paid += (deal.accrued_interest or 0.0) * quantity
        else:
            # Продажа не меняет среднюю цену остатка, но фиксирует результат
            sold = min(quantity, state.quantity) if state.quantity > 0 else quantity
            state.realized_pnl += (deal.price - state.avg_price) * sold
            state.quantity -= quantity
            if state.quantity <= 1e-9:
                state.quantity = 0.0
                state.avg_price = 0.0

    secids = list(running.keys())
    instruments = {
        instrument.secid: instrument
        for instrument in session.execute(
            select(Instrument).where(Instrument.secid.in_(secids))
        ).scalars()
    }
    quotes = {
        instrument.secid: quote
        for instrument, quote in session.execute(
            select(Instrument, Quote)
            .join(Quote, Quote.instrument_id == Instrument.id)
            .where(Quote.id.in_(latest_quote_ids(session)), Instrument.secid.in_(secids))
        ).all()
    }

    positions: list[dict[str, Any]] = []
    for secid, state in running.items():
        instrument = instruments.get(secid)
        quote = quotes.get(secid)
        multiplier = _price_multiplier(instrument)

        price = quote.last or quote.wa_price or quote.prev_close if quote else None
        market_value = (
            state.quantity * price * multiplier if price is not None else None
        )
        cost_basis = state.quantity * state.avg_price * multiplier
        unrealized = (
            market_value - cost_basis if market_value is not None else None
        )

        positions.append(
            {
                "secid": secid,
                "name": instrument.display_name if instrument else secid,
                "kind": instrument.kind if instrument else None,
                "isin": instrument.isin if instrument else None,
                "currency": instrument.currency if instrument else None,
                "portfolios": sorted(state.tags),
                "quantity": round(state.quantity, 6),
                "avg_price": round(state.avg_price, 6),
                "last_price": price,
                "face_value": instrument.face_value if instrument else None,
                "cost_basis": round(cost_basis, 2),
                "market_value": round(market_value, 2) if market_value is not None else None,
                "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
                "unrealized_pnl_pct": (
                    round(unrealized / cost_basis * 100, 2)
                    if unrealized is not None and cost_basis
                    else None
                ),
                "realized_pnl": round(state.realized_pnl * multiplier, 2),
                "fees": round(state.fees, 2),
                "accrued_paid": round(state.accrued_paid, 2),
                "deals": state.deals,
                "last_trade_date": state.last_trade_date,
                # Риск-метрики облигаций
                "yield_pct": quote.yield_pct if quote else None,
                "duration_days": quote.duration_days if quote else None,
                "duration_years": (
                    round(quote.duration_days / 365, 2)
                    if quote and quote.duration_days is not None
                    else None
                ),
                "maturity_date": instrument.maturity_date if instrument else None,
                "change_pct": quote.change_pct if quote else None,
            }
        )

    positions.sort(key=lambda item: item["market_value"] or 0, reverse=True)
    return positions


def portfolio_summary(
    session: Session, *, portfolio: str | None = None
) -> dict[str, Any]:
    """Итоги портфеля: стоимость, результат, дюрация, концентрация."""
    positions = compute_positions(session, portfolio=portfolio)
    open_positions = [p for p in positions if p["quantity"] > 0]

    total_value = sum(p["market_value"] or 0 for p in open_positions)
    total_cost = sum(p["cost_basis"] or 0 for p in open_positions)
    unrealized = sum(p["unrealized_pnl"] or 0 for p in open_positions)
    realized = sum(p["realized_pnl"] or 0 for p in positions)
    fees = sum(p["fees"] or 0 for p in positions)

    # Доли позиций нужны и для дюрации, и для оценки концентрации
    for position in open_positions:
        position["weight_pct"] = (
            round((position["market_value"] or 0) / total_value * 100, 2)
            if total_value
            else None
        )

    bond_positions = [
        p for p in open_positions if p["duration_years"] is not None and p["market_value"]
    ]
    bond_value = sum(p["market_value"] for p in bond_positions)
    weighted_duration = (
        round(
            sum(p["duration_years"] * p["market_value"] for p in bond_positions) / bond_value,
            2,
        )
        if bond_value
        else None
    )
    weighted_yield = (
        round(
            sum(
                (p["yield_pct"] or 0) * p["market_value"]
                for p in bond_positions
                if p["yield_pct"] is not None
            )
            / bond_value,
            2,
        )
        if bond_value
        else None
    )

    weights = [(p["weight_pct"] or 0) / 100 for p in open_positions]
    # Индекс Херфиндаля–Хиршмана: 1 — всё в одной бумаге, ~0 — равномерно
    hhi = round(sum(w**2 for w in weights), 4) if weights else None
    top_weights = sorted((p["weight_pct"] or 0) for p in open_positions)
    top5 = round(sum(top_weights[-5:]), 2) if open_positions else None

    by_kind: dict[str, float] = {}
    for position in open_positions:
        kind = position["kind"] or "прочее"
        by_kind[kind] = by_kind.get(kind, 0) + (position["market_value"] or 0)

    return {
        "portfolio": portfolio,
        "positions_open": len(open_positions),
        "positions_total": len(positions),
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "unrealized_pnl": round(unrealized, 2),
        "unrealized_pnl_pct": round(unrealized / total_cost * 100, 2) if total_cost else None,
        "realized_pnl": round(realized, 2),
        "fees": round(fees, 2),
        "net_pnl": round(unrealized + realized - fees, 2),
        "weighted_duration_years": weighted_duration,
        "weighted_yield_pct": weighted_yield,
        "concentration_hhi": hhi,
        "top5_weight_pct": top5,
        "allocation": [
            {"kind": kind, "value": round(value, 2),
             "share_pct": round(value / total_value * 100, 2) if total_value else None}
            for kind, value in sorted(by_kind.items(), key=lambda item: -item[1])
        ],
        "positions": open_positions,
    }


def portfolio_names(session: Session) -> list[str]:
    """Список портфелей, по которым есть сделки."""
    rows = session.execute(select(Deal.portfolio).distinct().order_by(Deal.portfolio)).all()
    return [row[0] for row in rows]


def rate_sensitivity(
    session: Session, *, portfolio: str | None = None, shift_bp: Sequence[int] = (25, 50, 100, 200)
) -> dict[str, Any]:
    """Оценка переоценки облигационной части при параллельном сдвиге ставок.

    Приближение первого порядка: ΔV ≈ −D × Δy × V. Для решения «удлинять или
    сокращать дюрацию» этого достаточно.
    """
    summary = portfolio_summary(session, portfolio=portfolio)
    bond_positions = [
        p for p in summary["positions"] if p["duration_years"] is not None and p["market_value"]
    ]
    bond_value = sum(p["market_value"] for p in bond_positions)
    duration = summary["weighted_duration_years"]

    scenarios = []
    for shift in shift_bp:
        for sign in (1, -1):
            delta_y = sign * shift / 10_000
            impact = -(duration or 0) * delta_y * bond_value
            scenarios.append(
                {
                    "shift_bp": sign * shift,
                    "impact_rub": round(impact, 2),
                    "impact_pct": round(impact / bond_value * 100, 3) if bond_value else None,
                }
            )
    scenarios.sort(key=lambda item: item["shift_bp"])

    return {
        "bond_value": round(bond_value, 2),
        "weighted_duration_years": duration,
        "scenarios": scenarios,
    }
