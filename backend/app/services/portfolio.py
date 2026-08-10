"""Портфель казначейства: позиции, финансовый результат, риск-метрики.

Учёт ведётся лотами. Это нужно сразу для трёх вещей:

* **ФИФО** — при продаже списывается самый ранний лот, как того требует
  налоговый учёт; метод средней цены доступен переключателем.
* **Валютная переоценка** — каждый лот помнит курс на дату покупки, поэтому
  результат раскладывается на ценовой и валютный. Без этого портфель с
  замещающими облигациями показывает смесь движения цены и движения курса.
* **Купонный доход** — сделки и купонные выплаты проходят одной лентой
  событий, поэтому купон начисляется на то количество бумаг, которое
  фактически было в позиции на дату выплаты.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Bar, CorpAction, Deal, Instrument, Quote
from .analytics import latest_quote_ids
from .fx import BASE_CURRENCY, FxBook, coupon_to_rub, instrument_currency, is_rub

#: Методы учёта себестоимости
FIFO = "fifo"
AVERAGE = "average"

#: Доля дневного оборота, которую считаем безопасной при выходе из позиции
DEFAULT_PARTICIPATION = 0.3


@dataclass(slots=True)
class Lot:
    """Партия бумаг с собственной ценой и курсом покупки."""

    quantity: float
    price: float
    fx_rate: float
    trade_date: date
    accrued: float = 0.0


@dataclass(slots=True)
class PositionState:
    """Накопленное состояние позиции при проходе по ленте событий."""

    lots: deque[Lot] = field(default_factory=deque)
    realized_price_rub: float = 0.0
    realized_fx_rub: float = 0.0
    coupon_income_rub: float = 0.0
    amortization_rub: float = 0.0
    accrued_paid_rub: float = 0.0
    accrued_received_rub: float = 0.0
    fees_rub: float = 0.0
    deals: int = 0
    first_trade_date: date | None = None
    last_trade_date: date | None = None
    portfolios: set[str] = field(default_factory=set)

    @property
    def quantity(self) -> float:
        return sum(lot.quantity for lot in self.lots)

    @property
    def avg_price(self) -> float:
        total = self.quantity
        if total <= 0:
            return 0.0
        return sum(lot.quantity * lot.price for lot in self.lots) / total

    @property
    def avg_fx(self) -> float:
        total = self.quantity
        if total <= 0:
            return 1.0
        return sum(lot.quantity * lot.fx_rate for lot in self.lots) / total


def price_multiplier(instrument: Instrument | None) -> float:
    """Во что превращается один пункт цены за одну бумагу.

    Облигации котируются в процентах от номинала, акции — в деньгах за штуку.
    """
    if instrument is None:
        return 1.0
    if instrument.kind == "bond" and instrument.face_value:
        return instrument.face_value / 100.0
    return 1.0


# ----------------------------------------------------------------------
# Лента событий
# ----------------------------------------------------------------------
def _events(
    deals: Sequence[Deal], actions: Sequence[CorpAction]
) -> list[tuple[date, int, Any]]:
    """Сделки и выплаты в хронологическом порядке.

    Сделка в день выплаты обрабатывается после неё: купон получает тот, кто
    владел бумагой на дату фиксации, а она предшествует расчётам по сделке.
    """
    events: list[tuple[date, int, Any]] = []
    for action in actions:
        if action.action_type in ("coupon", "amortization"):
            events.append((action.action_date, 0, action))
    for deal in deals:
        events.append((deal.trade_date, 1, deal))
    return sorted(events, key=lambda item: (item[0], item[1]))


def _consume(state: PositionState, quantity: float) -> list[tuple[Lot, float]]:
    """Списать количество из лотов по порядку и вернуть, что из какого взято."""
    taken: list[tuple[Lot, float]] = []
    remaining = quantity
    while remaining > 1e-9 and state.lots:
        lot = state.lots[0]
        used = min(lot.quantity, remaining)
        taken.append((lot, used))
        lot.quantity -= used
        remaining -= used
        if lot.quantity <= 1e-9:
            state.lots.popleft()
    return taken


def _merge_to_average(state: PositionState) -> None:
    """Свернуть лоты в один со средневзвешенными ценой и курсом."""
    quantity = state.quantity
    if quantity <= 0:
        state.lots.clear()
        return
    merged = Lot(
        quantity=quantity,
        price=state.avg_price,
        fx_rate=state.avg_fx,
        trade_date=state.lots[0].trade_date,
        accrued=sum(lot.accrued * lot.quantity for lot in state.lots) / quantity,
    )
    state.lots.clear()
    state.lots.append(merged)


def _apply_deal(
    state: PositionState,
    deal: Deal,
    *,
    multiplier: float,
    currency: str,
    fx: FxBook,
    method: str,
) -> None:
    rate = deal.fx_rate or fx.rate(currency, deal.trade_date) or 1.0
    quantity = abs(deal.quantity)

    state.deals += 1
    state.portfolios.add(deal.portfolio)
    state.last_trade_date = deal.trade_date
    if state.first_trade_date is None:
        state.first_trade_date = deal.trade_date
    # Комиссия всегда в рублях расчётов
    state.fees_rub += deal.fee or 0.0

    accrued = deal.accrued_interest or 0.0

    if deal.side == "buy":
        state.lots.append(
            Lot(quantity=quantity, price=deal.price, fx_rate=rate,
                trade_date=deal.trade_date, accrued=accrued)
        )
        # НКД биржа даёт сразу в валюте расчётов — на курс не умножаем
        state.accrued_paid_rub += accrued * quantity
        if method == AVERAGE:
            _merge_to_average(state)
        return

    # Продажа: НКД покупатель платит нам (тоже в рублях)
    state.accrued_received_rub += accrued * quantity
    for lot, used in _consume(state, quantity):
        # Ценовой результат считаем по курсу на дату продажи,
        # валютный — как переоценку вложенной суммы за время владения
        state.realized_price_rub += used * (deal.price - lot.price) * multiplier * rate
        state.realized_fx_rub += used * lot.price * multiplier * (rate - lot.fx_rate)


def _apply_payment(
    state: PositionState,
    action: CorpAction,
    *,
    currency: str,
    fx: FxBook,
    today: date,
) -> None:
    """Начислить купон или амортизацию на количество бумаг в позиции.

    Учитываются только состоявшиеся выплаты: график НРД содержит и будущие
    купоны до самого погашения, а они ещё не получены и в финансовый результат
    попадать не должны — для них есть календарь поступлений.
    """
    if action.action_date > today:
        return

    quantity = state.quantity
    if quantity <= 0 or action.value is None:
        return

    per_bond_rub = coupon_to_rub(
        action.value, action.value_rub, currency, action.action_date, fx
    )
    if per_bond_rub is None:
        return
    amount_rub = per_bond_rub * quantity

    if action.action_type == "coupon":
        state.coupon_income_rub += amount_rub
    else:
        state.amortization_rub += amount_rub


# ----------------------------------------------------------------------
# Позиции
# ----------------------------------------------------------------------
def compute_positions(
    session: Session,
    *,
    portfolio: str | None = None,
    method: str | None = None,
) -> list[dict[str, Any]]:
    """Свернуть сделки в позиции, оценить их и разложить результат."""
    method = method or settings.cost_method
    statement = select(Deal).order_by(Deal.trade_date, Deal.id)
    if portfolio:
        statement = statement.where(Deal.portfolio == portfolio)
    deals = list(session.execute(statement).scalars())
    if not deals:
        return []

    secids = sorted({deal.secid for deal in deals})
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

    isins = {inst.isin for inst in instruments.values() if inst.isin}
    actions_by_isin: dict[str, list[CorpAction]] = {}
    if isins:
        for action in session.execute(
            select(CorpAction).where(CorpAction.isin.in_(isins))
        ).scalars():
            actions_by_isin.setdefault(action.isin, []).append(action)

    fx = FxBook(session)
    volumes = _average_volumes(session, secids)
    today = date.today()

    by_secid: dict[str, list[Deal]] = {}
    for deal in deals:
        by_secid.setdefault(deal.secid, []).append(deal)

    positions: list[dict[str, Any]] = []
    for secid, secid_deals in by_secid.items():
        instrument = instruments.get(secid)
        quote = quotes.get(secid)
        multiplier = price_multiplier(instrument)
        currency = instrument_currency(instrument)

        state = PositionState()
        actions = actions_by_isin.get(instrument.isin or "", []) if instrument else []
        for _, _, event in _events(secid_deals, actions):
            if isinstance(event, Deal):
                _apply_deal(
                    state, event, multiplier=multiplier, currency=currency,
                    fx=fx, method=method,
                )
            else:
                _apply_payment(state, event, currency=currency, fx=fx, today=today)

        positions.append(
            _describe(
                secid=secid,
                state=state,
                instrument=instrument,
                quote=quote,
                multiplier=multiplier,
                currency=currency,
                fx=fx,
                avg_volume=volumes.get(secid),
            )
        )

    positions.sort(key=lambda item: item["market_value_rub"] or 0, reverse=True)
    return positions


def _describe(
    *,
    secid: str,
    state: PositionState,
    instrument: Instrument | None,
    quote: Quote | None,
    multiplier: float,
    currency: str,
    fx: FxBook,
    avg_volume: float | None,
) -> dict[str, Any]:
    """Собрать строку позиции с оценкой и разложением результата."""
    quantity = state.quantity
    price = None
    if quote is not None:
        price = quote.last or quote.wa_price or quote.prev_close
    rate_now = fx.rate(currency) or 1.0

    market_value_ccy = quantity * price * multiplier if price is not None else None
    market_value_rub = market_value_ccy * rate_now if market_value_ccy is not None else None
    # Себестоимость в рублях — по курсам, действовавшим на даты покупок
    cost_rub = sum(
        lot.quantity * lot.price * multiplier * lot.fx_rate for lot in state.lots
    )

    price_pnl_rub = fx_pnl_rub = None
    if price is not None:
        price_pnl_rub = sum(
            lot.quantity * (price - lot.price) * multiplier * rate_now
            for lot in state.lots
        )
        fx_pnl_rub = sum(
            lot.quantity * lot.price * multiplier * (rate_now - lot.fx_rate)
            for lot in state.lots
        )

    accrued_now = None
    accrued_rub = 0.0
    if instrument is not None and instrument.kind == "bond":
        accrued_now = (
            quote.accrued_interest
            if quote is not None and quote.accrued_interest is not None
            else instrument.accrued_interest
        )
        if accrued_now is not None:
            # НКД уже рублёвый
            accrued_rub = accrued_now * quantity

    # Купонный доход: полученные купоны плюс накопленный НКД,
    # минус НКД, уплаченный при покупке
    coupon_result_rub = (
        state.coupon_income_rub
        + state.accrued_received_rub
        + accrued_rub
        - state.accrued_paid_rub
    )

    unrealized_rub = (price_pnl_rub or 0) + (fx_pnl_rub or 0) if price is not None else None
    total_rub = (
        (unrealized_rub or 0)
        + state.realized_price_rub
        + state.realized_fx_rub
        + coupon_result_rub
        - state.fees_rub
    )

    duration_years = (
        quote.duration_days / 365
        if quote is not None and quote.duration_days is not None
        else None
    )

    return {
        "secid": secid,
        "name": instrument.display_name if instrument else secid,
        "kind": instrument.kind if instrument else None,
        "isin": instrument.isin if instrument else None,
        "issuer": instrument.issuer if instrument else None,
        "currency": currency,
        "fx_rate": round(rate_now, 6),
        "portfolios": sorted(state.portfolios),
        "quantity": round(quantity, 6),
        "avg_price": round(state.avg_price, 6),
        "avg_fx_rate": round(state.avg_fx, 6),
        "last_price": price,
        "face_value": instrument.face_value if instrument else None,
        "lots": len(state.lots),

        "market_value_ccy": _round(market_value_ccy, 2),
        "market_value_rub": _round(market_value_rub, 2),
        "cost_rub": round(cost_rub, 2),

        "price_pnl_rub": _round(price_pnl_rub, 2),
        "fx_pnl_rub": _round(fx_pnl_rub, 2),
        "unrealized_pnl_rub": _round(unrealized_rub, 2),
        "unrealized_pnl_pct": (
            round(unrealized_rub / cost_rub * 100, 2)
            if unrealized_rub is not None and cost_rub
            else None
        ),
        "realized_price_pnl_rub": round(state.realized_price_rub, 2),
        "realized_fx_pnl_rub": round(state.realized_fx_rub, 2),

        "coupon_income_rub": round(state.coupon_income_rub, 2),
        "amortization_rub": round(state.amortization_rub, 2),
        "accrued_now": _round(accrued_now, 2),
        "accrued_now_rub": round(accrued_rub, 2),
        "accrued_paid_rub": round(state.accrued_paid_rub, 2),
        "coupon_result_rub": round(coupon_result_rub, 2),

        "fees_rub": round(state.fees_rub, 2),
        "total_pnl_rub": round(total_rub, 2),

        "deals": state.deals,
        "first_trade_date": state.first_trade_date,
        "last_trade_date": state.last_trade_date,

        "yield_pct": quote.yield_pct if quote else None,
        "duration_days": quote.duration_days if quote else None,
        "duration_years": _round(duration_years, 2),
        "maturity_date": instrument.maturity_date if instrument else None,
        "list_level": instrument.list_level if instrument else None,
        "change_pct": quote.change_pct if quote else None,
        "avg_daily_volume": _round(avg_volume, 0),
        "days_to_exit": _days_to_exit(quantity, avg_volume),
    }


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _average_volumes(
    session: Session, secids: Sequence[str], *, lookback_days: int = 30
) -> dict[str, float]:
    """Средний дневной объём торгов — основа оценки срока выхода."""
    since = date.today() - timedelta(days=lookback_days)
    rows = session.execute(
        select(Instrument.secid, func.avg(Bar.volume))
        .join(Bar, Bar.instrument_id == Instrument.id)
        .where(
            Instrument.secid.in_(list(secids)),
            Bar.trade_date >= since,
            Bar.volume.isnot(None),
            Bar.volume > 0,
        )
        .group_by(Instrument.secid)
    ).all()
    return {secid: value for secid, value in rows if value}


def _days_to_exit(
    quantity: float, avg_volume: float | None, participation: float = DEFAULT_PARTICIPATION
) -> float | None:
    """За сколько торговых дней позиция выходит в рынок.

    Оценка грубая: полного стакана открытый API не отдаёт, поэтому считаем по
    среднему дневному объёму и допустимой доле участия в нём.
    """
    if not quantity or not avg_volume:
        return None
    capacity = avg_volume * participation
    if capacity <= 0:
        return None
    return round(quantity / capacity, 1)


# ----------------------------------------------------------------------
# Сводка
# ----------------------------------------------------------------------
def portfolio_summary(
    session: Session, *, portfolio: str | None = None, method: str | None = None
) -> dict[str, Any]:
    """Итоги портфеля: стоимость, результат, дюрация, концентрация, валюты."""
    positions = compute_positions(session, portfolio=portfolio, method=method)
    open_positions = [p for p in positions if p["quantity"] > 0]

    total_value = sum(p["market_value_rub"] or 0 for p in open_positions)
    total_cost = sum(p["cost_rub"] or 0 for p in open_positions)

    price_pnl = sum(p["price_pnl_rub"] or 0 for p in open_positions)
    fx_pnl = sum(p["fx_pnl_rub"] or 0 for p in open_positions)
    unrealized = price_pnl + fx_pnl
    realized = sum(
        (p["realized_price_pnl_rub"] or 0) + (p["realized_fx_pnl_rub"] or 0)
        for p in positions
    )
    coupon_result = sum(p["coupon_result_rub"] or 0 for p in positions)
    coupons_received = sum(p["coupon_income_rub"] or 0 for p in positions)
    fees = sum(p["fees_rub"] or 0 for p in positions)

    for position in open_positions:
        position["weight_pct"] = (
            round((position["market_value_rub"] or 0) / total_value * 100, 2)
            if total_value
            else None
        )

    bond_positions = [
        p for p in open_positions
        if p["duration_years"] is not None and p["market_value_rub"]
    ]
    bond_value = sum(p["market_value_rub"] for p in bond_positions)
    weighted_duration = (
        round(
            sum(p["duration_years"] * p["market_value_rub"] for p in bond_positions)
            / bond_value,
            2,
        )
        if bond_value
        else None
    )
    weighted_yield = (
        round(
            sum(
                (p["yield_pct"] or 0) * p["market_value_rub"]
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
    hhi = round(sum(weight**2 for weight in weights), 4) if weights else None
    top_weights = sorted((p["weight_pct"] or 0) for p in open_positions)
    top5 = round(sum(top_weights[-5:]), 2) if open_positions else None

    return {
        "portfolio": portfolio,
        "cost_method": method or settings.cost_method,
        "base_currency": BASE_CURRENCY,
        "positions_open": len(open_positions),
        "positions_total": len(positions),

        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),

        "price_pnl": round(price_pnl, 2),
        "fx_pnl": round(fx_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "unrealized_pnl_pct": round(unrealized / total_cost * 100, 2) if total_cost else None,
        "realized_pnl": round(realized, 2),
        "coupon_result": round(coupon_result, 2),
        "coupons_received": round(coupons_received, 2),
        "fees": round(fees, 2),
        "net_pnl": round(unrealized + realized + coupon_result - fees, 2),

        "weighted_duration_years": weighted_duration,
        "weighted_yield_pct": weighted_yield,
        "concentration_hhi": hhi,
        "top5_weight_pct": top5,

        "allocation": _breakdown(open_positions, "kind", total_value, _KIND_TITLES),
        "allocation_currency": _breakdown(open_positions, "currency", total_value),
        "allocation_issuer": _breakdown(open_positions, "issuer", total_value, limit=10),
        "positions": open_positions,
    }


_KIND_TITLES = {
    "share": "Акции",
    "bond": "Облигации",
    "index": "Индексы",
    "currency": "Валюта",
}


def _breakdown(
    positions: Sequence[dict[str, Any]],
    key: str,
    total: float,
    titles: dict[str, str] | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Разложение стоимости портфеля по признаку."""
    grouped: dict[str, float] = {}
    for position in positions:
        name = position.get(key) or "не указано"
        grouped[name] = grouped.get(name, 0) + (position["market_value_rub"] or 0)

    rows = [
        {
            "key": name,
            "title": (titles or {}).get(name, name),
            "value": round(value, 2),
            "share_pct": round(value / total * 100, 2) if total else None,
        }
        for name, value in sorted(grouped.items(), key=lambda item: -item[1])
    ]
    return rows[:limit] if limit else rows


def portfolio_names(session: Session) -> list[str]:
    rows = session.execute(select(Deal.portfolio).distinct().order_by(Deal.portfolio)).all()
    return [row[0] for row in rows]
