"""Переоценка портфеля: накопленная, дневная и амортизированная стоимость.

Три разных вопроса, которые обычно смешивают в одну колонку «прибыль»:

* **накопленная переоценка** — сколько бумага прибавила с момента покупки.
  Отвечает на «сколько мы на ней заработали»;
* **дневная переоценка** — насколько изменилась оценка со вчерашнего дня.
  Это то, что попадает в учёт за день по средневзвешенной цене (СВЦ)
  предыдущего торгового дня;
* **амортизированная стоимость** — для портфеля до погашения, где рыночная
  цена не влияет на учёт: премия или дисконт разносятся равномерно до
  погашения, а рынок показывается справочно.

Ведём именно СВЦ, а не цену последней сделки: одна случайная сделка в конце
дня двигает last, но не средневзвешенную, а переоценка по случайной цене —
это переоценка по шуму.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ACCOUNTING_HTM, Instrument, Quote
from .analytics import latest_quote_map
from .fx import FxBook, instrument_currency
from .portfolio import accounting_types, compute_positions, price_multiplier

#: Номинал в процентах — к нему стремится цена облигации при погашении
_PAR_PCT = 100.0


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def amortized_price(
    *,
    purchase_price: float,
    maturity: date | None,
    purchased_on: date | None,
    today: date,
) -> float | None:
    """Цена облигации по амортизированной стоимости, линейный метод.

    Купили с дисконтом — стоимость растёт к номиналу, с премией — снижается.
    Линейный метод, а не эффективная ставка: для портфеля казначейства с
    бумагами на два-три года разница в пределах десятых долей процента, а
    считается и проверяется линейный метод несопоставимо проще. Если
    понадобится ЭСП, менять придётся только эту функцию.
    """
    if maturity is None or purchased_on is None:
        return None
    total_days = (maturity - purchased_on).days
    if total_days <= 0:
        return _PAR_PCT
    passed = (today - purchased_on).days
    # За границами срока держим номинал: после погашения амортизировать нечего,
    # до даты покупки — ещё нечего
    if passed <= 0:
        return purchase_price
    if passed >= total_days:
        return _PAR_PCT
    return purchase_price + (_PAR_PCT - purchase_price) * passed / total_days


def revaluate(
    session: Session,
    *,
    portfolio: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Блок переоценки по позициям портфеля.

    Если портфель не задан, считаем по всем сразу, но вид учёта у каждой
    позиции остаётся своим: смешивать торговый портфель с портфелем до
    погашения в одной сумме переоценки нельзя.
    """
    positions = compute_positions(session, portfolio=portfolio, method=method)
    positions = [p for p in positions if (p["quantity"] or 0) > 0]
    if not positions:
        return {
            "as_of": date.today(),
            "items": [],
            "totals": _empty_totals(),
            "by_portfolio": [],
        }

    secids = [p["secid"] for p in positions]
    instruments = {
        instrument.secid: instrument
        for instrument in session.execute(
            select(Instrument).where(Instrument.secid.in_(secids))
        ).scalars()
    }
    latest = latest_quote_map(session)
    quotes = {
        instrument.secid: quote
        for instrument, quote in session.execute(
            select(Instrument, Quote)
            .join(latest, latest.c.instrument_id == Instrument.id)
            .join(Quote, Quote.id == latest.c.quote_id)
            .where(Instrument.secid.in_(secids))
        ).all()
    }

    accounting = accounting_types(session)
    fx = FxBook(session)
    today = date.today()

    items = [
        _describe_position(
            position,
            instrument=instruments.get(position["secid"]),
            quote=quotes.get(position["secid"]),
            accounting=accounting,
            fx=fx,
            today=today,
        )
        for position in positions
    ]
    items.sort(key=lambda item: abs(item["total_reval_rub"] or 0), reverse=True)

    return {
        "as_of": today,
        "quote_date": max(
            (q.ts for q in quotes.values() if q is not None), default=None
        ),
        "items": items,
        "totals": _totals(items),
        "by_portfolio": _by_portfolio(items),
    }


def _describe_position(
    position: dict[str, Any],
    *,
    instrument: Instrument | None,
    quote: Quote | None,
    accounting: dict[str, str],
    fx: FxBook,
    today: date,
) -> dict[str, Any]:
    """Разложить одну позицию на балансовую, вчерашнюю и текущую оценку."""
    quantity = position["quantity"] or 0.0
    multiplier = price_multiplier(instrument)
    currency = instrument_currency(instrument)
    rate = fx.rate(currency) or 1.0

    # Портфель у позиции может быть не один, если бумага куплена и в торговый,
    # и в инвестиционный. Вид учёта тогда берём по первому — а признак
    # смешения выносим наружу, чтобы это не выглядело точным числом
    portfolios = position.get("portfolios") or []
    portfolio_name = portfolios[0] if portfolios else ""
    accounting_type = accounting.get(portfolio_name, "trading")
    is_htm = accounting_type == ACCOUNTING_HTM

    # СВЦ: сегодняшняя и предыдущего дня. Если биржа сегодня СВЦ не дала
    # (бумага не торговалась), опираемся на цену закрытия — иначе позиция
    # молча выпала бы из переоценки
    wa_price = None
    prev_wa_price = None
    if quote is not None:
        wa_price = quote.wa_price or quote.last or quote.prev_close
        prev_wa_price = quote.prev_wa_price or quote.prev_close

    book_price = position["avg_price"]
    book_value_rub = position["cost_rub"]

    def value(price: float | None) -> float | None:
        if price is None:
            return None
        return quantity * price * multiplier * rate

    market_value_rub = value(wa_price)
    prev_value_rub = value(prev_wa_price)

    daily_reval_rub = (
        market_value_rub - prev_value_rub
        if market_value_rub is not None and prev_value_rub is not None
        else None
    )
    total_reval_rub = (
        market_value_rub - book_value_rub if market_value_rub is not None else None
    )

    # Амортизированная стоимость — только для облигаций портфеля до погашения
    amort_price = None
    amortized_value_rub = None
    if is_htm and instrument is not None and instrument.kind == "bond":
        amort_price = amortized_price(
            purchase_price=book_price,
            maturity=instrument.maturity_date,
            purchased_on=position.get("first_trade_date"),
            today=today,
        )
        amortized_value_rub = value(amort_price)

    # Учётная стоимость: у торгового портфеля это рынок, у портфеля до
    # погашения — амортизированная стоимость. Именно она идёт в баланс
    carrying_value_rub = (
        (amortized_value_rub if amortized_value_rub is not None else book_value_rub)
        if is_htm
        else market_value_rub
    )

    return {
        "secid": position["secid"],
        "name": position["name"],
        "isin": position["isin"],
        "kind": position["kind"],
        "currency": currency,
        "portfolio": portfolio_name,
        "portfolios": portfolios,
        # Одна бумага в двух портфелях с разным учётом — числа ниже смешаны,
        # поэтому таблица показывает такую строку с пометкой
        "mixed_portfolios": len(portfolios) > 1,
        "accounting_type": accounting_type,
        "quantity": quantity,

        "book_price": _round(book_price, 4),
        "book_value_rub": _round(book_value_rub),

        "prev_wa_price": _round(prev_wa_price, 4),
        "prev_value_rub": _round(prev_value_rub),

        "wa_price": _round(wa_price, 4),
        "market_value_rub": _round(market_value_rub),

        "daily_reval_rub": _round(daily_reval_rub),
        "daily_reval_pct": (
            round(daily_reval_rub / prev_value_rub * 100, 3)
            if daily_reval_rub is not None and prev_value_rub
            else None
        ),
        "total_reval_rub": _round(total_reval_rub),
        "total_reval_pct": (
            round(total_reval_rub / book_value_rub * 100, 2)
            if total_reval_rub is not None and book_value_rub
            else None
        ),

        "amortized_price": _round(amort_price, 4),
        "amortized_value_rub": _round(amortized_value_rub),
        "carrying_value_rub": _round(carrying_value_rub),
        # У портфеля до погашения рыночная переоценка в учёт не идёт —
        # показываем её, но помечаем, чтобы её не приняли за результат
        "market_is_reference": is_htm,
        # Амортизация считается от даты первой покупки: при нескольких
        # докупках это приближение, и честнее сказать об этом в таблице
        "amortization_approximate": (
            amort_price is not None and (position.get("deals") or 0) > 1
        ),
        "accrued_now_rub": position.get("accrued_now_rub"),
        "maturity_date": position.get("maturity_date"),
    }


def _empty_totals() -> dict[str, Any]:
    return {
        "book_value_rub": 0.0,
        "prev_value_rub": 0.0,
        "market_value_rub": 0.0,
        "carrying_value_rub": 0.0,
        "daily_reval_rub": 0.0,
        "total_reval_rub": 0.0,
        "accrued_now_rub": 0.0,
        "positions": 0,
    }


def _totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Итоги. Дневная и накопленная переоценка суммируются только там, где
    они идут в учёт: у портфеля до погашения рыночная переоценка справочная,
    и включать её в общую сумму — значит показать результат, которого нет."""
    totals = _empty_totals()
    for item in items:
        totals["book_value_rub"] += item["book_value_rub"] or 0
        totals["prev_value_rub"] += item["prev_value_rub"] or 0
        totals["market_value_rub"] += item["market_value_rub"] or 0
        totals["carrying_value_rub"] += item["carrying_value_rub"] or 0
        totals["accrued_now_rub"] += item["accrued_now_rub"] or 0
        if not item["market_is_reference"]:
            totals["daily_reval_rub"] += item["daily_reval_rub"] or 0
            totals["total_reval_rub"] += item["total_reval_rub"] or 0
    totals["positions"] = len(items)
    return {
        key: (round(value, 2) if isinstance(value, float) else value)
        for key, value in totals.items()
    }


def _by_portfolio(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Итоги в разрезе портфелей — торговый и до погашения раздельно."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["portfolio"], []).append(item)

    rows = []
    for name, group in grouped.items():
        totals = _totals(group)
        totals["portfolio"] = name
        totals["accounting_type"] = group[0]["accounting_type"]
        rows.append(totals)
    rows.sort(key=lambda row: row["carrying_value_rub"], reverse=True)
    return rows


#: Колонки выгрузки переоценки в Excel
REVALUATION_COLUMNS: tuple[dict[str, Any], ...] = (
    {"code": "portfolio", "title": "Портфель", "kind": "text"},
    {"code": "accounting_title", "title": "Вид учёта", "kind": "text"},
    {"code": "secid", "title": "Код", "kind": "text"},
    {"code": "isin", "title": "ISIN", "kind": "text"},
    {"code": "name", "title": "Бумага", "kind": "text"},
    {"code": "quantity", "title": "Количество", "kind": "number", "digits": 0},
    {"code": "book_price", "title": "Цена приобретения", "kind": "number", "digits": 4},
    {"code": "book_value_rub", "title": "Балансовая стоимость, ₽", "kind": "number", "digits": 2},
    {"code": "prev_wa_price", "title": "СВЦ пред. дня", "kind": "number", "digits": 4},
    {"code": "prev_value_rub", "title": "Оценка на пред. день, ₽", "kind": "number", "digits": 2},
    {"code": "wa_price", "title": "СВЦ текущая", "kind": "number", "digits": 4},
    {"code": "market_value_rub", "title": "Рыночная стоимость, ₽", "kind": "number", "digits": 2},
    {"code": "daily_reval_rub", "title": "Переоценка за день, ₽", "kind": "number", "digits": 2},
    {"code": "total_reval_rub", "title": "Переоценка накопленная, ₽", "kind": "number", "digits": 2},
    {"code": "amortized_price", "title": "Амортизированная цена", "kind": "number", "digits": 4},
    {"code": "amortized_value_rub", "title": "Амортизированная стоимость, ₽", "kind": "number", "digits": 2},
    {"code": "carrying_value_rub", "title": "Учётная стоимость, ₽", "kind": "number", "digits": 2},
    {"code": "accrued_now_rub", "title": "НКД, ₽", "kind": "number", "digits": 2},
    {"code": "maturity_date", "title": "Погашение", "kind": "date"},
    {"code": "note", "title": "Примечание", "kind": "text"},
)

ACCOUNTING_TITLES = {
    "trading": "Торговый",
    ACCOUNTING_HTM: "До погашения",
}


def rows_for_export(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Дополнить строки текстовыми пояснениями: в Excel подсказку не наведёшь."""
    rows = []
    for item in items:
        notes = []
        if item["market_is_reference"]:
            notes.append("рыночная оценка справочно, в учёт не идёт")
        if item["amortization_approximate"]:
            notes.append("амортизация от даты первой покупки")
        if item["mixed_portfolios"]:
            notes.append("бумага в нескольких портфелях")
        rows.append(
            {
                **item,
                "accounting_title": ACCOUNTING_TITLES.get(
                    item["accounting_type"], item["accounting_type"]
                ),
                "note": "; ".join(notes) or None,
            }
        )
    return rows
