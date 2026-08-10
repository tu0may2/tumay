"""Сравнение с рыночными ориентирами.

Портфель дал 14% — это много или мало? Без ориентира ответа нет. Сравниваем с
индексами облигаций МосБиржи, у которых в истории есть и значение, и доходность,
и дюрация, поэтому сопоставима не только доходность, но и принятый риск.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Bar, Instrument
from .portfolio import compute_positions

#: Ориентиры для сравнения.
#:
#: Корпоративных индексов среди действующих нет: MOEX прекратила публиковать
#: семейство RUCB* в открытом доступе — их история обрывается на 31.05.2023.
#: Поэтому ориентиром служат государственные индексы: RGBITR учитывает
#: реинвестирование купонов и сопоставим с полной доходностью портфеля,
#: RGBI отражает только движение цен.
BENCHMARKS: tuple[tuple[str, str], ...] = (
    ("RGBITR", "Гособлигации, полная доходность (RGBITR)"),
    ("RGBI", "Гособлигации, цены (RGBI)"),
)


def _series(session: Session, secid: str, since: date) -> list[Bar]:
    return list(
        session.execute(
            select(Bar)
            .join(Instrument, Bar.instrument_id == Instrument.id)
            .where(Instrument.secid == secid, Bar.trade_date >= since)
            .order_by(Bar.trade_date)
        ).scalars()
    )


def _return_pct(bars: Sequence[Bar]) -> float | None:
    """Доходность за период по первому и последнему значению."""
    values = [bar.close for bar in bars if bar.close]
    if len(values) < 2 or not values[0]:
        return None
    return round((values[-1] / values[0] - 1) * 100, 2)


def index_summary(session: Session, *, days: int = 90) -> list[dict[str, Any]]:
    """Показатели индексов-ориентиров за период."""
    since = date.today() - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    for secid, title in BENCHMARKS:
        bars = _series(session, secid, since)
        if not bars:
            rows.append({"secid": secid, "title": title, "available": False})
            continue
        last = bars[-1]
        rows.append(
            {
                "secid": secid,
                "title": title,
                "available": True,
                "value": last.close,
                "return_pct": _return_pct(bars),
                "yield_pct": last.yield_close,
                "duration_years": (
                    round(last.duration_days / 365, 2)
                    if last.duration_days is not None
                    else None
                ),
                "from_date": bars[0].trade_date,
                "to_date": last.trade_date,
                "points": len(bars),
            }
        )
    return rows


def compare_portfolio(
    session: Session, *, portfolio: str | None = None, days: int = 90
) -> dict[str, Any]:
    """Сопоставить портфель с индексами по доходности, ставке и дюрации."""
    since = date.today() - timedelta(days=days)
    positions = [
        p for p in compute_positions(session, portfolio=portfolio) if p["quantity"] > 0
    ]

    # Доходность текущего состава за период: каждая позиция взвешивается
    # своей рыночной стоимостью. Это не полноценная доходность портфеля
    # (сделки внутри периода не учитываются), а сравнение текущего набора
    # бумаг с индексом на одном горизонте.
    total_value = sum(p["market_value_rub"] or 0 for p in positions)
    contributions: list[dict[str, Any]] = []
    covered_value = 0.0
    weighted_return = 0.0

    for position in positions:
        bars = _series(session, position["secid"], since)
        change = _return_pct(bars)
        if change is None or not position["market_value_rub"]:
            continue
        value = position["market_value_rub"]
        covered_value += value
        weighted_return += change * value
        contributions.append(
            {
                "secid": position["secid"],
                "name": position["name"],
                "return_pct": change,
                "weight_pct": round(value / total_value * 100, 2) if total_value else None,
                "contribution_pct": None,
            }
        )

    portfolio_return = round(weighted_return / covered_value, 2) if covered_value else None
    for row in contributions:
        if portfolio_return is not None and covered_value:
            share = (row["weight_pct"] or 0) / 100
            row["contribution_pct"] = round(row["return_pct"] * share, 3)
    contributions.sort(key=lambda row: row["contribution_pct"] or 0, reverse=True)

    bonds = [p for p in positions if p["duration_years"] is not None and p["market_value_rub"]]
    bond_value = sum(p["market_value_rub"] for p in bonds)
    portfolio_duration = (
        round(sum(p["duration_years"] * p["market_value_rub"] for p in bonds) / bond_value, 2)
        if bond_value
        else None
    )
    portfolio_yield = (
        round(
            sum(
                (p["yield_pct"] or 0) * p["market_value_rub"]
                for p in bonds
                if p["yield_pct"] is not None
            )
            / bond_value,
            2,
        )
        if bond_value
        else None
    )

    benchmarks = index_summary(session, days=days)
    for benchmark in benchmarks:
        if benchmark.get("available") and portfolio_return is not None:
            benchmark["excess_pct"] = round(
                portfolio_return - (benchmark["return_pct"] or 0), 2
            )

    return {
        "days": days,
        "portfolio": portfolio,
        "portfolio_return_pct": portfolio_return,
        "portfolio_yield_pct": portfolio_yield,
        "portfolio_duration_years": portfolio_duration,
        "covered_value": round(covered_value, 2),
        "total_value": round(total_value, 2),
        "coverage_pct": round(covered_value / total_value * 100, 1) if total_value else None,
        "benchmarks": benchmarks,
        "contributions": contributions[:20],
        "note": (
            "Доходность считается по текущему составу портфеля за период и не "
            "учитывает сделки внутри него. Бумаги без загруженной истории в "
            "расчёт не попадают — их доля показана отдельно. Корпоративный "
            "индекс не показан: MOEX не публикует семейство RUCB* с мая 2023 года."
        ),
    }


def spread_history(
    session: Session, secid: str, *, days: int = 365, benchmark: str | None = None
) -> dict[str, Any]:
    """История премии выпуска к индексу гособлигаций.

    Исторической кривой бескупонной доходности открытый API не отдаёт, поэтому
    ориентиром служит доходность индекса гособлигаций на ту же дату. Это премия
    к рынку ОФЗ в целом, а не к кривой на собственной дюрации выпуска, — но
    именно она показывает, дорога бумага или дешева относительно самой себя.
    """
    benchmark = benchmark or settings.benchmark_bond_index
    since = date.today() - timedelta(days=days)

    bond_bars = _series(session, secid.upper(), since)
    index_bars = {bar.trade_date: bar for bar in _series(session, benchmark, since)}

    points: list[dict[str, Any]] = []
    for bar in bond_bars:
        index_bar = index_bars.get(bar.trade_date)
        if bar.yield_close is None or index_bar is None or index_bar.yield_close is None:
            continue
        spread_bp = (bar.yield_close - index_bar.yield_close) * 100
        points.append(
            {
                "trade_date": bar.trade_date,
                "yield_pct": bar.yield_close,
                "benchmark_yield_pct": index_bar.yield_close,
                "spread_bp": round(spread_bp, 0),
                "wa_price": bar.wa_price,
            }
        )

    # Если точек нет, объясняем причину: у флоатеров доходность к погашению
    # не рассчитывается в принципе, а у остальных её может не быть просто
    # потому, что история ещё не загружена
    reason = None
    if not points:
        instrument = session.execute(
            select(Instrument).where(Instrument.secid == secid.upper()).limit(1)
        ).scalar_one_or_none()
        bond_type = (instrument.bond_type or "") if instrument else ""
        if "флоат" in bond_type.lower():
            reason = (
                "У выпусков с плавающим купоном доходность к погашению не "
                "рассчитывается: будущие купоны заранее неизвестны."
            )
        elif not bond_bars:
            reason = "История торгов по этому выпуску ещё не загружена."
        elif not index_bars:
            reason = "История индекса-ориентира ещё не загружена."
        else:
            reason = "Биржа не публикует доходность по этому выпуску."

    spreads = [point["spread_bp"] for point in points]
    stats: dict[str, Any] = {}
    if spreads:
        current = spreads[-1]
        average = sum(spreads) / len(spreads)
        stats = {
            "current_bp": current,
            "average_bp": round(average, 0),
            "min_bp": min(spreads),
            "max_bp": max(spreads),
            # Отклонение от собственной средней: положительное — бумага
            # торгуется дешевле обычного
            "deviation_bp": round(current - average, 0),
        }

    return {
        "secid": secid.upper(),
        "benchmark": benchmark,
        "days": days,
        "points": points,
        "stats": stats,
        "reason": reason,
        "note": (
            "Премия считается к доходности индекса гособлигаций на ту же дату: "
            "исторической кривой бескупонной доходности открытый API не предоставляет."
        ),
    }
