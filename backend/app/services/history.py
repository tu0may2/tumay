"""История стоимости портфеля и доходность во времени.

Без ежедневных снимков доходность можно оценить лишь по текущему составу —
это приближение, которое не видит ни сделок внутри периода, ни просадки.
Снимок фиксирует состояние на дату, и уже по ряду снимков считается настоящая
кривая доходности.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Deal, PortfolioSnapshot
from .cash import cash_position
from .portfolio import portfolio_summary


def take_snapshot(
    session: Session, *, portfolio: str | None = None, on_date: date | None = None
) -> dict[str, Any]:
    """Зафиксировать состояние портфеля на дату."""
    on_date = on_date or date.today()
    summary = portfolio_summary(session, portfolio=portfolio)
    cash = cash_position(session, portfolio=portfolio)

    # Внешние вводы и выводы за день: без них доходность спутается с
    # пополнением счёта — рост от новых денег не является заработком
    statement = select(Deal).where(Deal.trade_date == on_date)
    if portfolio:
        statement = statement.where(Deal.portfolio == portfolio)
    net_flow = 0.0

    key = portfolio or ""
    existing = session.execute(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.portfolio == key,
            PortfolioSnapshot.snapshot_date == on_date,
        )
    ).scalar_one_or_none()

    values = {
        "total_value": summary["total_value"] + cash["total_liquidity_rub"],
        "total_cost": summary["total_cost"],
        "cash_value": cash["total_liquidity_rub"],
        "price_pnl": summary["price_pnl"],
        "fx_pnl": summary["fx_pnl"],
        "coupon_result": summary["coupon_result"],
        "net_pnl": summary["net_pnl"],
        "duration_years": summary["weighted_duration_years"],
        "yield_pct": summary["weighted_yield_pct"],
        "positions": summary["positions_open"],
        "net_flow": net_flow,
    }

    if existing is not None:
        for field, value in values.items():
            setattr(existing, field, value)
        snapshot = existing
    else:
        snapshot = PortfolioSnapshot(portfolio=key, snapshot_date=on_date, **values)
        session.add(snapshot)

    session.commit()
    return {"snapshot_date": on_date, "portfolio": portfolio, **values}


def _drawdown(values: Sequence[float]) -> tuple[float | None, int | None]:
    """Максимальная просадка ряда и её глубина в точках."""
    if len(values) < 2:
        return None, None
    peak = values[0]
    worst = 0.0
    worst_index = None
    for index, value in enumerate(values):
        peak = max(peak, value)
        if peak > 0:
            drop = (value / peak - 1) * 100
            if drop < worst:
                worst, worst_index = drop, index
    return round(worst, 2), worst_index


def portfolio_history(
    session: Session, *, portfolio: str | None = None, days: int = 365
) -> dict[str, Any]:
    """Ряд снимков с доходностью и просадкой."""
    since = date.today() - timedelta(days=days)
    key = portfolio or ""

    snapshots = list(
        session.execute(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.portfolio == key,
                PortfolioSnapshot.snapshot_date >= since,
            )
            .order_by(PortfolioSnapshot.snapshot_date)
        ).scalars()
    )

    points = [
        {
            "date": snapshot.snapshot_date,
            "total_value": snapshot.total_value,
            "cash_value": snapshot.cash_value,
            "net_pnl": snapshot.net_pnl,
            "duration_years": snapshot.duration_years,
            "yield_pct": snapshot.yield_pct,
            "positions": snapshot.positions,
        }
        for snapshot in snapshots
    ]

    values = [point["total_value"] for point in points if point["total_value"]]
    total_return = (
        round((values[-1] / values[0] - 1) * 100, 2)
        if len(values) >= 2 and values[0]
        else None
    )
    max_drawdown, drawdown_index = _drawdown(values)

    return {
        "portfolio": portfolio,
        "days": days,
        "points": points,
        "snapshots": len(points),
        "first_date": points[0]["date"] if points else None,
        "last_date": points[-1]["date"] if points else None,
        "total_return_pct": total_return,
        "max_drawdown_pct": max_drawdown,
        "max_drawdown_date": (
            points[drawdown_index]["date"] if drawdown_index is not None else None
        ),
        "note": (
            "Снимок делается раз в сутки при работающем приложении. "
            "Доходность считается по стоимости портфеля вместе с денежной "
            "позицией и не корректируется на вводы и выводы средств."
            if points
            else "Снимков пока нет: они накапливаются со дня запуска."
        ),
    }
