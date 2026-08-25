"""График выплат по выпуску: купоны и накопление НКД.

Таблица выплат отвечает на вопрос «когда и сколько», но не показывает того,
что видно только на картинке: как НКД растёт внутри периода и обнуляется в
день выплаты. Эта «пила» — то, за что покупатель доплачивает продавцу, и
именно её форма объясняет, почему цена бумаги в день купона падает на его
величину, хотя ничего плохого не произошло.

Поэтому отдаём два ряда сразу: столбцы выплат (купон, амортизация,
погашение) и линию НКД по дням. Столбцы отвечают на «сколько денег придёт»,
линия — на «сколько я переплачу, если куплю сегодня».
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CorpAction, Instrument

#: Как называются виды выплат в графике
ACTION_TITLES = {
    "coupon": "Купон",
    "amortization": "Амортизация",
    "offer": "Оферта",
    "maturity": "Погашение",
}

#: Сколько точек рисуем внутри одного купонного периода. Накопление линейное,
#: поэтому хватило бы двух, но с промежуточными точками линия не ломается
#: на графиках с редкой сеткой и по ней удобнее наводить курсор
_POINTS_PER_PERIOD = 6


def payment_schedule(
    session: Session,
    instrument: Instrument,
    *,
    quantity: float = 1.0,
) -> dict[str, Any]:
    """Выплаты и накопление НКД по выпуску.

    ``quantity`` — на сколько бумаг считать. По умолчанию на одну: карточка
    выпуска показывает параметры бумаги, а не позиции.
    """
    if not instrument.isin:
        return _empty()

    actions = list(
        session.execute(
            select(CorpAction)
            .where(CorpAction.isin == instrument.isin)
            .order_by(CorpAction.action_date)
        ).scalars()
    )
    if not actions:
        return _empty()

    today = date.today()
    payments = [
        {
            "date": action.action_date,
            "kind": action.action_type,
            "title": ACTION_TITLES.get(action.action_type, action.action_type),
            "value": action.value,
            "value_rub": action.value_rub,
            "value_pct": action.value_pct,
            "amount": (action.value or 0) * quantity,
            "face_unit": action.face_unit,
            "past": action.action_date < today,
            "days_left": (action.action_date - today).days,
        }
        for action in actions
        # Оферта — не выплата, а право предъявить бумагу: денег в этот день
        # может не быть вовсе, и в графике поступлений ей не место
        if action.action_type != "offer"
    ]

    coupons = [item for item in payments if item["kind"] == "coupon"]
    upcoming = [item for item in payments if not item["past"]]

    return {
        "payments": payments,
        "accrual": _accrual_curve(coupons, instrument),
        "offers": [
            {
                "date": action.action_date,
                "title": ACTION_TITLES["offer"],
                "past": action.action_date < today,
            }
            for action in actions
            if action.action_type == "offer"
        ],
        "totals": {
            "payments": len(payments),
            "upcoming": len(upcoming),
            "upcoming_amount": round(sum(item["amount"] for item in upcoming), 2),
            "paid_amount": round(
                sum(item["amount"] for item in payments if item["past"]), 2
            ),
            "next_date": upcoming[0]["date"] if upcoming else None,
            "next_amount": round(upcoming[0]["amount"], 2) if upcoming else None,
            "average_coupon": (
                round(sum(item["amount"] for item in coupons) / len(coupons), 2)
                if coupons
                else None
            ),
        },
        "quantity": quantity,
    }


def _accrual_curve(
    coupons: list[dict[str, Any]], instrument: Instrument
) -> list[dict[str, Any]]:
    """Линия НКД по дням: растёт внутри периода, обнуляется в день выплаты.

    Начало первого периода из графика НРД не узнать — там только даты выплат,
    поэтому первый купон рисуем от даты, отстоящей на длину периода назад.
    Если длина периода неизвестна, первый купон пропускаем: рисовать линию от
    выдуманной даты хуже, чем не рисовать её вовсе.
    """
    if len(coupons) < 2 and not instrument.coupon_period:
        return []

    points: list[dict[str, Any]] = []
    previous_date: date | None = None

    for index, coupon in enumerate(coupons):
        end = coupon["date"]
        value = coupon["amount"]
        if value is None:
            continue

        if previous_date is not None:
            start = previous_date
        elif instrument.coupon_period:
            start = end - timedelta(days=int(instrument.coupon_period))
        else:
            previous_date = end
            continue

        span = (end - start).days
        if span <= 0:
            previous_date = end
            continue

        for step in range(_POINTS_PER_PERIOD):
            moment = start + timedelta(days=span * step // _POINTS_PER_PERIOD)
            points.append(
                {
                    "date": moment,
                    "value": round(value * ((moment - start).days / span), 4),
                }
            )
        # День выплаты: НКД равен полному купону. Обнуление рисует следующий
        # период — он начинается в этот же день с нуля. Явный ноль ставим
        # только после последнего купона, иначе точка задвоится
        points.append({"date": end, "value": round(value, 4)})
        if index == len(coupons) - 1:
            points.append({"date": end, "value": 0.0})
        previous_date = end

    return points


def _empty() -> dict[str, Any]:
    return {
        "payments": [],
        "accrual": [],
        "offers": [],
        "totals": {
            "payments": 0,
            "upcoming": 0,
            "upcoming_amount": 0.0,
            "paid_amount": 0.0,
            "next_date": None,
            "next_amount": None,
            "average_coupon": None,
        },
        "quantity": 1.0,
    }
