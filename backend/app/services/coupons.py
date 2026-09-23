"""Календарь поступлений по портфелю: купоны, амортизации, погашение.

Почему это отдельный модуль, а не пара запросов в риск-метриках.

Календарь поступлений — это не «список будущих купонов». У него три разных
режима, и каждый требует своей логики:

1. **Фиксированный купон в будущем.** Биржа знает сумму заранее, считать
   нечего: сумма на бумагу × количество.

2. **Плавающий купон в будущем.** Биржа отдаёт дату, но сумму присылает
   пустой — ставка ещё не зафиксирована. Такую выплату нельзя ни выбросить
   (дата известна и нужна в плане ликвидности), ни показать нулём (это ложь).
   Поэтому строка остаётся с пустой суммой и пометкой, а рядом — оценка по
   последнему известному купону этого же выпуска, помеченная как оценка и
   не попадающая в «объявлено».

3. **Уже выплаченные купоны.** Их нельзя считать по текущему портфелю:
   выплату получил тот, кто держал бумагу на дату фиксации. Если выпуск
   куплен в марте, январский купон нам не приходил, а если продан в июне —
   июльский тоже. Поэтому количество берётся не из позиции, а из истории
   сделок на дату фиксации.

Из этого же следует ответ на вопрос «что делать с проданными бумагами»:
никакого отдельного правила не нужно. Количество на дату выплаты само
отсекает и купоны до покупки, и купоны после продажи, а за период владения
оставляет ровно те деньги, которые действительно пришли на счёт.
"""
from __future__ import annotations

import bisect
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CorpAction, Deal, Instrument
from .fx import FxBook, coupon_to_rub, instrument_currency

#: Виды выплат, которые приносят деньги на счёт. Оферта сюда не входит:
#: это право предъявить бумагу, а не выплата, и попадёт она в поступления
#: только если предъявление действительно состоялось — то есть сделкой.
CASH_ACTION_TYPES = ("coupon", "amortization")

#: Значение ``data_source`` у биржи, которым помечено погашение. Тип действия
#: у погашения и частичной амортизации один и тот же, различить их можно
#: только так.
MATURITY_MARKER = "maturity"

#: Как называются виды выплат в интерфейсе и в выгрузке
ACTION_TITLES = {
    "coupon": "Купон",
    "amortization": "Амортизация",
    "maturity": "Погашение",
}

#: Состояние выплаты
#: paid       — день прошёл, сумма известна: деньги получены
#: announced  — день впереди, сумма известна
#: awaiting   — день наступил или прошёл, а суммы от биржи всё ещё нет
#: unknown    — день впереди, ставка не зафиксирована (флоатер)
STATUS_TITLES = {
    "paid": "Выплачен",
    "announced": "Объявлен",
    "awaiting": "Ждём сумму",
    "unknown": "Ставка не определена",
}


# ----------------------------------------------------------------------
# Количество бумаг на произвольную дату
# ----------------------------------------------------------------------
class Holdings:
    """Сколько бумаг было в портфеле на любую дату в прошлом.

    Сделки свёрнуты в накопленный остаток по каждому коду: дальше ответ на
    вопрос «сколько было на дату X» — это двоичный поиск, а не обход всех
    сделок заново на каждую из сотен выплат.
    """

    __slots__ = ("_dates", "_totals")

    def __init__(self, deals: Iterable[Deal]):
        moves: dict[str, dict[date, float]] = {}
        for deal in deals:
            sign = -1.0 if deal.side == "sell" else 1.0
            per_date = moves.setdefault(deal.secid, {})
            per_date[deal.trade_date] = (
                per_date.get(deal.trade_date, 0.0) + sign * deal.quantity
            )

        self._dates: dict[str, list[date]] = {}
        self._totals: dict[str, list[float]] = {}
        for secid, per_date in moves.items():
            running = 0.0
            dates: list[date] = []
            totals: list[float] = []
            for moment in sorted(per_date):
                running += per_date[moment]
                dates.append(moment)
                totals.append(running)
            self._dates[secid] = dates
            self._totals[secid] = totals

    @property
    def secids(self) -> list[str]:
        """Все коды, которые когда-либо были в портфеле."""
        return sorted(self._dates)

    def quantity_on(self, secid: str, on: date) -> float:
        """Остаток по бумаге на конец дня ``on``.

        Сделка в день выплаты учитывается: право на купон определяется
        остатком на дату фиксации, а расчёты по сделке этого дня к ней уже
        относятся.
        """
        dates = self._dates.get(secid)
        if not dates:
            return 0.0
        index = bisect.bisect_right(dates, on)
        if index == 0:
            return 0.0
        return self._totals[secid][index - 1]

    def held_ever(self, secid: str) -> bool:
        return secid in self._dates


# ----------------------------------------------------------------------
# Календарь
# ----------------------------------------------------------------------
def receipts_calendar(
    session: Session,
    *,
    portfolio: str | None = None,
    horizon_days: int = 365,
    past_days: int = 365,
    today: date | None = None,
) -> dict[str, Any]:
    """Поступления по бумагам портфеля: прошлые, объявленные и ожидаемые.

    ``past_days`` — насколько глубоко показывать уже прошедшие выплаты,
    ``horizon_days`` — насколько далеко вперёд. Ноль в ``past_days`` даёт
    календарь только будущего.
    """
    today = today or date.today()
    since = today - timedelta(days=max(past_days, 0))
    until = today + timedelta(days=max(horizon_days, 0))

    statement = select(Deal).order_by(Deal.trade_date, Deal.id)
    if portfolio:
        statement = statement.where(Deal.portfolio == portfolio)
    holdings = Holdings(session.execute(statement).scalars())

    secids = holdings.secids
    if not secids:
        return _empty(horizon_days, past_days)

    instruments = {
        instrument.secid: instrument
        for instrument in session.execute(
            select(Instrument).where(Instrument.secid.in_(secids))
        ).scalars()
    }
    # График выплат биржа ведёт по ISIN, портфель — по коду бумаги
    isin_to_secid: dict[str, str] = {
        instrument.isin: secid
        for secid, instrument in instruments.items()
        if instrument.isin
    }
    if not isin_to_secid:
        return _empty(horizon_days, past_days, missing=_bonds_without_isin(instruments))

    actions = list(
        session.execute(
            select(CorpAction)
            .where(
                CorpAction.isin.in_(sorted(isin_to_secid)),
                CorpAction.action_date >= since,
                CorpAction.action_date <= until,
                CorpAction.action_type.in_(CASH_ACTION_TYPES),
            )
            .order_by(CorpAction.action_date)
        ).scalars()
    )

    fallback = _last_known_value(session, sorted(isin_to_secid), today)
    fx = FxBook(session)
    events: list[dict[str, Any]] = []

    for action in actions:
        secid = isin_to_secid.get(action.isin)
        if secid is None:
            continue
        # Право на выплату даёт остаток на дату фиксации; её биржа присылает
        # не всегда, тогда ориентируемся на день выплаты
        record_date = action.record_date or action.action_date
        quantity = holdings.quantity_on(secid, record_date)
        if quantity <= 0:
            continue

        instrument = instruments.get(secid)
        currency = instrument_currency(instrument)
        kind = _action_kind(action)

        per_bond = coupon_to_rub(
            action.value, action.value_rub, currency, action.action_date, fx
        )
        is_past = action.action_date <= today
        known = per_bond is not None

        estimate_per_bond = None
        if not known:
            estimate_per_bond = coupon_to_rub(
                fallback.get(action.isin), None, currency, action.action_date, fx
            )

        if known:
            status = "paid" if is_past else "announced"
        else:
            status = "awaiting" if is_past else "unknown"

        events.append(
            {
                "action_date": action.action_date,
                "record_date": action.record_date,
                "days_left": (action.action_date - today).days,
                "secid": secid,
                "isin": action.isin,
                "name": (instrument.display_name if instrument else None) or action.name,
                "action_type": kind,
                "action_title": ACTION_TITLES.get(kind, kind),
                "status": status,
                "status_title": STATUS_TITLES[status],
                "is_past": is_past,
                "quantity": quantity,
                "value_per_bond": action.value,
                "value_pct": action.value_pct,
                "currency": currency,
                "amount_ccy": (
                    round(action.value * quantity, 2) if action.value is not None else None
                ),
                "amount_rub": round(per_bond * quantity, 2) if known else None,
                "amount_estimate_rub": (
                    round(estimate_per_bond * quantity, 2)
                    if estimate_per_bond is not None
                    else None
                ),
                "source": action.source,
            }
        )

    events.sort(key=lambda row: (row["action_date"], row["secid"], row["action_type"]))
    return _totals(
        events,
        horizon_days=horizon_days,
        past_days=past_days,
        missing=_missing_schedules(instruments, holdings, isin_to_secid, actions),
    )


def portfolio_isins(session: Session, *, portfolio: str | None = None) -> list[str]:
    """ISIN облигаций, которые сейчас есть в портфеле.

    Именно по ним имеет смысл обновлять график выплат по кнопке: проданные
    выпуски новых выплат нам уже не принесут, а их прошлые купоны в базе и так
    лежат.
    """
    statement = select(Deal).order_by(Deal.trade_date, Deal.id)
    if portfolio:
        statement = statement.where(Deal.portfolio == portfolio)
    holdings = Holdings(session.execute(statement).scalars())
    secids = holdings.secids
    if not secids:
        return []

    rows = session.execute(
        select(Instrument.secid, Instrument.isin).where(
            Instrument.secid.in_(secids), Instrument.isin.isnot(None)
        )
    ).all()
    return sorted(
        {isin for secid, isin in rows if isin and holdings.quantity_on(secid, date.max) > 0}
    )


def isins_without_schedule(session: Session, secids: Sequence[str]) -> list[str]:
    """ISIN бумаг, по которым в базе ещё нет ни одной выплаты.

    Вход для догрузки графика сразу после сделки: спрашивать биржу заново по
    выпуску, график которого уже лежит, незачем.
    """
    if not secids:
        return []
    isins = {
        isin
        for isin, in session.execute(
            select(Instrument.isin).where(
                Instrument.secid.in_(list(secids)),
                Instrument.isin.isnot(None),
                Instrument.kind == "bond",
            )
        ).all()
        if isin
    }
    if not isins:
        return []
    known = {
        isin
        for isin, in session.execute(
            select(CorpAction.isin).where(CorpAction.isin.in_(sorted(isins))).distinct()
        ).all()
    }
    return sorted(isins - known)


def _action_kind(action: CorpAction) -> str:
    """Купон, амортизация или погашение."""
    if action.action_type != "amortization":
        return action.action_type
    if (action.data_source or "").lower() == MATURITY_MARKER:
        return "maturity"
    # Признака от биржи может не быть у записей, загруженных раньше: выплата
    # всего номинала — это погашение, как бы она ни называлась в графике
    if action.value_pct is not None and action.value_pct >= 100:
        return "maturity"
    return "amortization"


def _last_known_value(
    session: Session, isins: Sequence[str], today: date
) -> dict[str, float]:
    """Последний известный купон по каждому выпуску — база для оценки.

    Для флоатера это ближайший ориентир: ставка меняется вслед за ключевой,
    но не в разы. Оценка нужна, чтобы календарь не выглядел пустым там, где
    выплата точно будет, и всегда помечается как оценка.
    """
    if not isins:
        return {}
    rows = session.execute(
        select(CorpAction.isin, CorpAction.value)
        .where(
            CorpAction.isin.in_(list(isins)),
            CorpAction.action_type == "coupon",
            CorpAction.value.isnot(None),
            CorpAction.action_date <= today,
        )
        .order_by(CorpAction.isin, CorpAction.action_date)
    ).all()
    # Строки отсортированы по дате, поэтому последняя запись по выпуску и
    # оказывается самой свежей
    return {isin: value for isin, value in rows if value is not None}


def _bonds_without_isin(instruments: dict[str, Instrument]) -> list[dict[str, Any]]:
    return [
        {"secid": secid, "reason": "нет ISIN в справочнике"}
        for secid, instrument in sorted(instruments.items())
        if instrument.kind == "bond" and not instrument.isin
    ]


def _missing_schedules(
    instruments: dict[str, Instrument],
    holdings: Holdings,
    isin_to_secid: dict[str, str],
    actions: Sequence[CorpAction],
) -> list[dict[str, Any]]:
    """Облигации в портфеле, по которым графика выплат нет.

    Без этого списка пустой календарь по только что купленной бумаге выглядит
    как «выплат не будет», а не как «график ещё не загружен».
    """
    with_schedule = {action.isin for action in actions}
    missing = _bonds_without_isin(instruments)
    for isin, secid in sorted(isin_to_secid.items(), key=lambda item: item[1]):
        instrument = instruments.get(secid)
        if instrument is None or instrument.kind != "bond":
            continue
        if holdings.quantity_on(secid, date.max) <= 0:
            continue
        if isin in with_schedule:
            continue
        missing.append({"secid": secid, "isin": isin, "reason": "график не загружен"})
    return missing


def _empty(
    horizon_days: int, past_days: int, missing: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "horizon_days": horizon_days,
        "past_days": past_days,
        "total_rub": 0,
        "coupon_rub": 0,
        "amortization_rub": 0,
        "maturity_rub": 0,
        "received_rub": 0,
        "estimated_rub": 0,
        "unknown_count": 0,
        "events": [],
        "by_month": [],
        "missing": missing or [],
        "note": NOTE,
    }


NOTE = (
    "Купоны, амортизации и погашение по бумагам портфеля. Количество берётся "
    "на дату фиксации, поэтому выплаты до покупки и после продажи в календарь "
    "не попадают. Плавающий купон, ставка которого ещё не объявлена, показан "
    "датой без суммы; рядом — оценка по последнему известному купону выпуска, "
    "она не входит в объявленные суммы."
)


def _totals(
    events: list[dict[str, Any]],
    *,
    horizon_days: int,
    past_days: int,
    missing: list[dict[str, Any]],
) -> dict[str, Any]:
    future = [row for row in events if not row["is_past"]]
    past = [row for row in events if row["is_past"]]

    def total(rows: Sequence[dict[str, Any]], field: str = "amount_rub") -> float:
        return round(sum(row[field] or 0 for row in rows), 2)

    by_month: dict[str, dict[str, Any]] = {}
    for event in events:
        key = event["action_date"].strftime("%Y-%m")
        bucket = by_month.setdefault(
            key,
            {
                "month": key,
                "coupon_rub": 0.0,
                "amortization_rub": 0.0,
                "maturity_rub": 0.0,
                "estimated_rub": 0.0,
                "total_rub": 0.0,
                "is_past": True,
            },
        )
        amount = event["amount_rub"]
        if amount is None:
            bucket["estimated_rub"] += event["amount_estimate_rub"] or 0
        else:
            bucket[f"{event['action_type']}_rub"] += amount
            bucket["total_rub"] += amount
        if not event["is_past"]:
            bucket["is_past"] = False

    months = [
        {
            key: (round(value, 2) if isinstance(value, float) else value)
            for key, value in bucket.items()
        }
        for bucket in sorted(by_month.values(), key=lambda item: item["month"])
    ]

    return {
        "horizon_days": horizon_days,
        "past_days": past_days,
        # total_rub — объявленные поступления впереди: именно это число
        # человек сравнивает с планом ликвидности
        "total_rub": total(future),
        "coupon_rub": total([r for r in future if r["action_type"] == "coupon"]),
        "amortization_rub": total(
            [r for r in future if r["action_type"] == "amortization"]
        ),
        "maturity_rub": total([r for r in future if r["action_type"] == "maturity"]),
        "received_rub": total(past),
        "estimated_rub": total(
            [r for r in future if r["amount_rub"] is None], "amount_estimate_rub"
        ),
        "unknown_count": sum(1 for r in future if r["amount_rub"] is None),
        "events": events,
        "by_month": months,
        "missing": missing,
        "note": NOTE,
    }
