"""Денежная позиция казначейства.

Казначейство управляет не только бумагами, но и деньгами: сколько свободно
сегодня, что придёт завтра, куда положить остаток. Здесь собираются остатки по
счетам, размещения (депозиты и РЕПО) и платёжный календарь, в котором видно
кассовый разрыв до того, как он случится.

Расчёты по сделкам с бумагами выводятся из журнала сделок, а не заводятся
вторым разом руками: покупка списывает деньги, продажа зачисляет.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CashAccount, CashFlow, CorpAction, Deal, Instrument, Placement
from .fx import BASE_CURRENCY, FxBook, coupon_to_rub, instrument_currency
from .portfolio import compute_positions, price_multiplier

#: Виды движений и их русские названия
FLOW_TITLES = {
    "deposit": "Пополнение",
    "withdrawal": "Вывод",
    "trade": "Расчёты по сделке",
    "coupon": "Купон",
    "fee": "Комиссия",
    "tax": "Налог",
    "transfer": "Перевод",
    "maturity": "Погашение",
    "placement": "Размещение",
    "placement_return": "Возврат размещения",
    "other": "Прочее",
}

PLACEMENT_TITLES = {
    "deposit": "Депозит",
    "repo": "РЕПО (привлечение)",
    "reverse_repo": "Обратное РЕПО (размещение)",
    "loan": "Кредит",
}

#: Размещения, по которым деньги уходят от нас и потом возвращаются с процентом
PLACEMENT_OUTFLOW = {"deposit", "reverse_repo"}


def accrued_interest(placement: Placement, on_date: date | None = None) -> float:
    """Проценты, накопленные по размещению к дате.

    Считаем простыми процентами по фактическому числу дней — так считают
    короткие депозиты и РЕПО.
    """
    on_date = on_date or date.today()
    end = min(on_date, placement.end_date)
    days = (end - placement.start_date).days
    if days <= 0:
        return 0.0
    return placement.amount * placement.rate / 100 * days / 365


def placement_total(placement: Placement) -> float:
    """Сумма к возврату в конце срока: тело плюс проценты за весь срок."""
    days = (placement.end_date - placement.start_date).days
    return placement.amount * (1 + placement.rate / 100 * days / 365)


def _deal_settlements(
    session: Session, portfolio: str | None
) -> list[dict[str, Any]]:
    """Расчёты по сделкам с бумагами — выводим из журнала, а не дублируем."""
    statement = select(Deal).order_by(Deal.trade_date)
    if portfolio:
        statement = statement.where(Deal.portfolio == portfolio)
    deals = list(session.execute(statement).scalars())
    if not deals:
        return []

    instruments = {
        instrument.secid: instrument
        for instrument in session.execute(
            select(Instrument).where(
                Instrument.secid.in_({deal.secid for deal in deals})
            )
        ).scalars()
    }

    fx = FxBook(session)
    rows: list[dict[str, Any]] = []
    for deal in deals:
        instrument = instruments.get(deal.secid)
        multiplier = price_multiplier(instrument)
        currency = instrument_currency(instrument)
        rate = deal.fx_rate or fx.rate(currency, deal.trade_date) or 1.0

        # Цена в валюте номинала, НКД биржа даёт уже в рублях
        clean_rub = abs(deal.quantity) * deal.price * multiplier * rate
        accrued_rub = abs(deal.quantity) * (deal.accrued_interest or 0.0)
        amount = clean_rub + accrued_rub

        rows.append(
            {
                "flow_date": deal.trade_date,
                # Покупка уводит деньги, продажа приводит
                "amount": -(amount + (deal.fee or 0)) if deal.side == "buy"
                else amount - (deal.fee or 0),
                "kind": "trade",
                "currency": BASE_CURRENCY,
                "comment": (
                    f"{'Покупка' if deal.side == 'buy' else 'Продажа'} "
                    f"{deal.secid} × {abs(deal.quantity):g}"
                ),
                "source": "deal",
                "is_planned": False,
            }
        )
    return rows


def _placement_flows(
    session: Session, portfolio: str | None, *, until: date
) -> list[dict[str, Any]]:
    """Движения по депозитам и РЕПО: размещение и возврат с процентами."""
    statement = select(Placement)
    if portfolio:
        statement = statement.where(Placement.portfolio == portfolio)
    placements = list(session.execute(statement).scalars())

    fx = FxBook(session)
    rows: list[dict[str, Any]] = []
    for placement in placements:
        rate = fx.rate(placement.currency, placement.start_date) or 1.0
        outflow = placement.kind in PLACEMENT_OUTFLOW
        title = PLACEMENT_TITLES.get(placement.kind, placement.kind)

        rows.append(
            {
                "flow_date": placement.start_date,
                "amount": (-placement.amount if outflow else placement.amount) * rate,
                "kind": "placement",
                "currency": placement.currency,
                "comment": f"{title}: размещение, {placement.counterparty or '—'}",
                "source": "placement",
                "is_planned": placement.start_date > date.today(),
            }
        )

        if placement.end_date <= until:
            back = placement_total(placement) * (
                fx.rate(placement.currency, placement.end_date) or rate
            )
            rows.append(
                {
                    "flow_date": placement.end_date,
                    "amount": back if outflow else -back,
                    "kind": "placement_return",
                    "currency": placement.currency,
                    "comment": (
                        f"{title}: возврат с процентами под {placement.rate:g}% годовых"
                    ),
                    "source": "placement",
                    "is_planned": placement.end_date > date.today(),
                }
            )
    return rows


def _coupon_flows(
    session: Session, portfolio: str | None, *, until: date
) -> list[dict[str, Any]]:
    """Будущие купоны и погашения по своим позициям."""
    positions = [
        p for p in compute_positions(session, portfolio=portfolio) if p["quantity"] > 0
    ]
    by_isin = {p["isin"]: p for p in positions if p.get("isin")}
    if not by_isin:
        return []

    today = date.today()
    instruments = {
        instrument.isin: instrument
        for instrument in session.execute(
            select(Instrument).where(Instrument.isin.in_(list(by_isin)))
        ).scalars()
        if instrument.isin
    }

    actions = session.execute(
        select(CorpAction).where(
            CorpAction.isin.in_(list(by_isin)),
            CorpAction.action_date >= today,
            CorpAction.action_date <= until,
            CorpAction.action_type.in_(("coupon", "amortization")),
        )
    ).scalars()

    fx = FxBook(session)
    rows: list[dict[str, Any]] = []
    for action in actions:
        position = by_isin.get(action.isin)
        if position is None or action.value is None:
            continue
        currency = instrument_currency(instruments.get(action.isin))
        per_bond = coupon_to_rub(
            action.value, action.value_rub, currency, action.action_date, fx
        )
        if per_bond is None:
            continue
        rows.append(
            {
                "flow_date": action.action_date,
                "amount": per_bond * position["quantity"],
                "kind": "coupon",
                "currency": BASE_CURRENCY,
                "comment": (
                    f"{'Купон' if action.action_type == 'coupon' else 'Погашение'} "
                    f"{position['secid']}"
                ),
                "source": "security",
                "is_planned": True,
            }
        )
    return rows


def _manual_flows(session: Session, portfolio: str | None) -> list[dict[str, Any]]:
    """Движения, заведённые вручную: пополнения, налоги, плановые выплаты."""
    statement = (
        select(CashFlow, CashAccount)
        .join(CashAccount, CashFlow.account_id == CashAccount.id)
        .order_by(CashFlow.flow_date)
    )
    if portfolio:
        statement = statement.where(CashAccount.portfolio == portfolio)

    fx = FxBook(session)
    rows: list[dict[str, Any]] = []
    for flow, account in session.execute(statement).all():
        rate = fx.rate(account.currency, flow.flow_date) or 1.0
        rows.append(
            {
                "flow_date": flow.flow_date,
                "amount": flow.amount * rate,
                "amount_ccy": flow.amount,
                "kind": flow.kind,
                "currency": account.currency,
                "account": account.name,
                "comment": flow.comment,
                "source": "manual",
                "is_planned": flow.is_planned,
                "id": flow.id,
            }
        )
    return rows


# ----------------------------------------------------------------------
# Остатки
# ----------------------------------------------------------------------
def cash_position(
    session: Session, *, portfolio: str | None = None
) -> dict[str, Any]:
    """Текущие остатки: по счетам, по валютам и в размещениях."""
    fx = FxBook(session)
    today = date.today()

    accounts_statement = select(CashAccount)
    if portfolio:
        accounts_statement = accounts_statement.where(CashAccount.portfolio == portfolio)
    accounts = list(session.execute(accounts_statement).scalars())

    # Остаток счёта — сумма состоявшихся движений по нему
    balances: dict[int, float] = defaultdict(float)
    flow_statement = select(CashFlow).where(
        CashFlow.is_planned.is_(False), CashFlow.flow_date <= today
    )
    for flow in session.execute(flow_statement).scalars():
        balances[flow.account_id] += flow.amount

    account_rows = []
    total_rub = 0.0
    by_currency: dict[str, float] = defaultdict(float)
    for account in accounts:
        balance = balances.get(account.id, 0.0)
        rate = fx.rate(account.currency) or 1.0
        balance_rub = balance * rate
        total_rub += balance_rub
        by_currency[account.currency] += balance
        account_rows.append(
            {
                "id": account.id,
                "name": account.name,
                "bank": account.bank,
                "currency": account.currency,
                "portfolio": account.portfolio,
                "balance": round(balance, 2),
                "balance_rub": round(balance_rub, 2),
                "fx_rate": round(rate, 6),
            }
        )

    # Действующие размещения: тело и накопленные проценты
    placement_statement = select(Placement).where(Placement.closed.is_(False))
    if portfolio:
        placement_statement = placement_statement.where(Placement.portfolio == portfolio)

    # Размещённое и привлечённое считаем порознь: сальдо скрывает и объём
    # свободных средств, и размер обязательства к возврату
    placed_out_rub = borrowed_rub = accrued_rub = 0.0
    placement_rows = []
    for placement in session.execute(placement_statement).scalars():
        rate = fx.rate(placement.currency) or 1.0
        interest = accrued_interest(placement)
        active = placement.start_date <= today <= placement.end_date
        outflow = placement.kind in PLACEMENT_OUTFLOW
        if active:
            if outflow:
                placed_out_rub += placement.amount * rate
            else:
                borrowed_rub += placement.amount * rate
            accrued_rub += interest * rate * (1 if outflow else -1)

        placement_rows.append(
            {
                "id": placement.id,
                "kind": placement.kind,
                "kind_title": PLACEMENT_TITLES.get(placement.kind, placement.kind),
                "counterparty": placement.counterparty,
                "amount": placement.amount,
                "amount_rub": round(placement.amount * rate, 2),
                "currency": placement.currency,
                "rate": placement.rate,
                "start_date": placement.start_date,
                "end_date": placement.end_date,
                "days_left": (placement.end_date - today).days,
                "days_total": (placement.end_date - placement.start_date).days,
                "accrued_interest": round(interest, 2),
                "total_at_maturity": round(placement_total(placement), 2),
                "collateral_secid": placement.collateral_secid,
                "active": active,
                "closed": placement.closed,
            }
        )

    placement_rows.sort(key=lambda row: row["end_date"])

    # Средневзвешенная ставка размещений — с чем сравнивать доходность бумаг
    active_placements = [
        row for row in placement_rows
        if row["active"] and row["kind"] in PLACEMENT_OUTFLOW
    ]
    placed_amount = sum(row["amount_rub"] for row in active_placements)
    weighted_rate = (
        round(
            sum(row["rate"] * row["amount_rub"] for row in active_placements)
            / placed_amount,
            2,
        )
        if placed_amount
        else None
    )

    placed_rub = placed_out_rub - borrowed_rub
    return {
        "portfolio": portfolio,
        "total_cash_rub": round(total_rub, 2),
        #: Сальдо размещений: положительное — мы нетто-кредитор
        "placed_rub": round(placed_rub, 2),
        "placed_out_rub": round(placed_out_rub, 2),
        "borrowed_rub": round(borrowed_rub, 2),
        "accrued_interest_rub": round(accrued_rub, 2),
        "available_rub": round(total_rub, 2),
        "total_liquidity_rub": round(total_rub + placed_rub + accrued_rub, 2),
        "weighted_placement_rate": weighted_rate,
        "accounts": account_rows,
        "by_currency": [
            {
                "currency": code,
                "balance": round(value, 2),
                "balance_rub": round(value * (fx.rate(code) or 1.0), 2),
            }
            for code, value in sorted(by_currency.items())
        ],
        "placements": placement_rows,
    }


# ----------------------------------------------------------------------
# Платёжный календарь
# ----------------------------------------------------------------------
def payment_calendar(
    session: Session,
    *,
    portfolio: str | None = None,
    horizon_days: int = 180,
) -> dict[str, Any]:
    """Все ожидаемые движения денег на горизонте, с накопленным остатком.

    Отрицательный накопленный остаток — кассовый разрыв: видно, в какой день
    денег не хватит, и сколько нужно найти.
    """
    today = date.today()
    until = today + timedelta(days=horizon_days)

    def _ahead(row: dict[str, Any]) -> bool:
        """Осталось ли движение в будущем.

        Остаток счёта уже включает все состоявшиеся движения с датой по
        сегодня. Если показать их ещё и в календаре, деньги удвоятся, поэтому
        сегодняшнее состоявшееся движение из календаря убираем: оно в остатке.
        """
        if row["flow_date"] > until:
            return False
        if row.get("source") == "manual" and not row.get("is_planned"):
            return row["flow_date"] > today
        return row["flow_date"] >= today

    events = [
        row
        for row in (
            _manual_flows(session, portfolio)
            + _placement_flows(session, portfolio, until=until)
            + _coupon_flows(session, portfolio, until=until)
        )
        if _ahead(row)
    ]
    events.sort(key=lambda row: row["flow_date"])

    position = cash_position(session, portfolio=portfolio)
    running = position["total_cash_rub"]

    rows: list[dict[str, Any]] = []
    gap_date: date | None = None
    lowest = running
    for event in events:
        running += event["amount"]
        if running < lowest:
            lowest = running
        if running < 0 and gap_date is None:
            gap_date = event["flow_date"]
        rows.append(
            {
                **event,
                "amount": round(event["amount"], 2),
                "kind_title": FLOW_TITLES.get(event["kind"], event["kind"]),
                "days_left": (event["flow_date"] - today).days,
                "balance_after": round(running, 2),
            }
        )

    by_month: dict[str, dict[str, float]] = {}
    for row in rows:
        key = row["flow_date"].strftime("%Y-%m")
        bucket = by_month.setdefault(key, {"month": key, "inflow": 0.0, "outflow": 0.0})
        if row["amount"] >= 0:
            bucket["inflow"] += row["amount"]
        else:
            bucket["outflow"] += row["amount"]

    return {
        "horizon_days": horizon_days,
        "opening_balance": position["total_cash_rub"],
        "closing_balance": round(running, 2),
        "lowest_balance": round(lowest, 2),
        "gap_date": gap_date,
        "has_gap": gap_date is not None,
        "inflow": round(sum(r["amount"] for r in rows if r["amount"] > 0), 2),
        "outflow": round(sum(r["amount"] for r in rows if r["amount"] < 0), 2),
        "events": rows,
        "by_month": [
            {
                "month": bucket["month"],
                "inflow": round(bucket["inflow"], 2),
                "outflow": round(bucket["outflow"], 2),
                "net": round(bucket["inflow"] + bucket["outflow"], 2),
            }
            for bucket in sorted(by_month.values(), key=lambda item: item["month"])
        ],
        "note": (
            "Расчёты по сделкам с бумагами в календарь не включаются: они уже "
            "прошли и учтены в остатке. Показаны будущие купоны, погашения, "
            "возвраты размещений и плановые движения."
        ),
    }


def settlement_history(
    session: Session, *, portfolio: str | None = None, days: int = 90
) -> list[dict[str, Any]]:
    """Состоявшиеся движения денег за период — для сверки остатка."""
    since = date.today() - timedelta(days=days)
    rows = [
        row
        for row in (
            _manual_flows(session, portfolio) + _deal_settlements(session, portfolio)
        )
        if row["flow_date"] >= since and not row.get("is_planned")
    ]
    rows.sort(key=lambda row: row["flow_date"], reverse=True)
    for row in rows:
        row["amount"] = round(row["amount"], 2)
        row["kind_title"] = FLOW_TITLES.get(row["kind"], row["kind"])
    return rows
