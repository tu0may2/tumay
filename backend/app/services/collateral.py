"""Обеспечение по кредитам Банка России — «ломбардный список».

Для казначейства этот список отвечает на вопрос, который не виден ни по
доходности, ни по обороту: что из портфеля можно превратить в деньги, не
продавая. Заложить бумагу в ЦБ быстрее и дешевле, чем продать её на рынке, —
особенно тогда, когда деньги нужны срочно, а рынок как раз просел.

Одного факта «бумага в списке» мало. ЦБ оценивает её по своей цене и
применяет поправочный коэффициент: под выпуск с коэффициентом 0,9 дадут лишь
девять десятых оценки. Поэтому везде, где показываем участие в списке,
показываем и то, сколько денег под бумагу реально дадут.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CbrCollateral

#: Механизмы кредитования ЦБ
MECHANISM_TITLES = {
    "ОМ": "Основной механизм",
    "ДМ": "Дополнительный механизм",
}


def collateral_map(session: Session, isins: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """Справочник «ISIN → условия обеспечения».

    Отдаём словарём, а не соединением в запросе: список короткий (сотни
    строк), нужен сразу по всем бумагам витрины, и словарь избавляет от
    join'а в каждом отборе.
    """
    statement = select(CbrCollateral)
    if isins:
        statement = statement.where(CbrCollateral.isin.in_(list(isins)))

    return {
        row.isin: {
            "eligible": True,
            "haircut": row.haircut,
            "price_pct": row.price_pct,
            "value_rub": row.value_rub,
            "mechanism": row.mechanism,
            "mechanism_title": MECHANISM_TITLES.get(row.mechanism, row.mechanism),
            "group": row.group_title,
            "as_of": row.as_of,
        }
        for row in session.execute(statement).scalars()
    }


def as_of(session: Session) -> date | None:
    """На какую дату загружен список."""
    return session.execute(select(func.max(CbrCollateral.as_of))).scalar()


def pledge_value(
    quantity: float,
    face_value: float | None,
    terms: dict[str, Any] | None,
) -> float | None:
    """Сколько денег дадут под позицию, рублей.

    Считаем по оценке ЦБ, а не по рыночной цене: в залог бумагу принимают
    именно по ней. Если ЦБ дал готовую стоимость одной бумаги — берём её,
    иначе восстанавливаем из цены в процентах и номинала.
    """
    if not terms or not quantity:
        return None

    per_unit = terms.get("value_rub")
    if per_unit is None:
        price_pct = terms.get("price_pct")
        if price_pct is None or not face_value:
            return None
        per_unit = price_pct / 100 * face_value

    haircut = terms.get("haircut")
    if haircut is None:
        # Коэффициент у части выпусков не опубликован. Считать его единицей
        # значило бы завысить доступные деньги, поэтому суммы не даём
        return None
    return quantity * per_unit * haircut


def describe(terms: dict[str, Any] | None) -> dict[str, Any]:
    """Поля обеспечения для строки витрины — единообразно во всех таблицах."""
    if not terms:
        return {
            "cbr_eligible": False,
            "cbr_haircut": None,
            "cbr_price_pct": None,
            "cbr_mechanism": None,
            "cbr_mechanism_title": None,
        }
    return {
        "cbr_eligible": True,
        "cbr_haircut": terms.get("haircut"),
        "cbr_price_pct": terms.get("price_pct"),
        "cbr_mechanism": terms.get("mechanism"),
        "cbr_mechanism_title": terms.get("mechanism_title"),
    }


def portfolio_collateral(
    session: Session,
    *,
    portfolio: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Сколько денег можно поднять под портфель, не продавая бумаги.

    Это же число — вход для норматива мгновенной ликвидности: залоговая
    бумага и деньги под неё доступны в тот же день.
    """
    from .portfolio import compute_positions

    positions = [
        position
        for position in compute_positions(session, portfolio=portfolio, method=method)
        if (position["quantity"] or 0) > 0
    ]
    if not positions:
        return {
            "as_of": as_of(session),
            "items": [],
            "pledgeable_rub": 0.0,
            "market_value_rub": 0.0,
            "eligible_positions": 0,
            "total_positions": 0,
        }

    terms_by_isin = collateral_map(
        session, [p["isin"] for p in positions if p.get("isin")]
    )

    items: list[dict[str, Any]] = []
    pledgeable = 0.0
    market_total = 0.0
    eligible = 0

    for position in positions:
        terms = terms_by_isin.get(position.get("isin") or "")
        money = pledge_value(position["quantity"], position.get("face_value"), terms)
        market = position.get("market_value_rub") or 0.0
        market_total += market
        if terms:
            eligible += 1
            pledgeable += money or 0.0

        items.append(
            {
                "secid": position["secid"],
                "isin": position.get("isin"),
                "name": position["name"],
                "quantity": position["quantity"],
                "market_value_rub": market,
                "pledge_value_rub": round(money, 2) if money is not None else None,
                # Во сколько обходится залог вместо продажи: доля рыночной
                # стоимости, которую даст ЦБ
                "coverage_pct": (
                    round(money / market * 100, 1)
                    if money is not None and market
                    else None
                ),
                **describe(terms),
                "cbr_group": terms.get("group") if terms else None,
            }
        )

    items.sort(key=lambda item: item["pledge_value_rub"] or 0, reverse=True)
    return {
        "as_of": as_of(session),
        "items": items,
        "pledgeable_rub": round(pledgeable, 2),
        "market_value_rub": round(market_total, 2),
        "eligible_positions": eligible,
        "total_positions": len(positions),
        # Какую часть портфеля можно заложить — грубая мера запаса ликвидности
        "eligible_share_pct": (
            round(
                sum(
                    item["market_value_rub"]
                    for item in items
                    if item["cbr_eligible"]
                )
                / market_total
                * 100,
                1,
            )
            if market_total
            else None
        ),
    }
