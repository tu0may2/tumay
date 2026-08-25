"""Нормативы ликвидности Н2, Н3, Н4 и залоговый потенциал портфеля."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..schemas import RatioInputSave
from ..services import collateral as collateral_service
from ..services import ratios as ratios_service
from ..services.auth import audit, require_trader

router = APIRouter(prefix="/api/ratios", tags=["Нормативы"])


@router.get("", summary="Нормативы Н2, Н3, Н4")
def get_ratios(
    portfolio: str | None = Query(None, description="Портфель; пусто — все сразу"),
    on_date: date | None = Query(None, description="Отчётная дата"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Расчёт с разложением: формула, числитель, знаменатель, запас до предела.

    Ликвидные активы терминал считает сам — деньги на счетах плюс бумаги,
    принимаемые ЦБ в обеспечение, по залоговой стоимости. Остальные части
    берутся из введённых балансовых данных.
    """
    return ratios_service.report(session, portfolio=portfolio, on_date=on_date)


@router.put("/inputs", summary="Сохранить балансовые данные")
def save_inputs(
    payload: RatioInputSave,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> dict[str, Any]:
    """Записать обязательства, капитал и требования на отчётную дату."""
    record = ratios_service.save_inputs(session, payload.model_dump(exclude_none=False))
    audit(
        session, user, action="update", entity="ratio_input",
        entity_id=str(record.as_of), detail="балансовые данные нормативов",
    )
    return ratios_service.load_inputs(session, record.as_of)


@router.get("/simulate", summary="Пересчёт нормативов под сделку")
def simulate(
    amount_rub: float = Query(..., description="Сумма покупки; продажа — со знаком минус"),
    eligible: bool = Query(True, description="Бумага принимается ЦБ в обеспечение"),
    portfolio: str | None = Query(None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Что станет с нормативами, если купить бумагу на заданную сумму.

    Разница между залоговой и незалоговой бумагой здесь и видна: под первую
    ЦБ вернёт деньги в тот же день, вторая выводит их из ликвидности целиком.
    """
    return ratios_service.simulate(
        session, amount_rub=amount_rub, eligible=eligible, portfolio=portfolio
    )


@router.get("/collateral", summary="Залоговый потенциал портфеля")
def portfolio_collateral(
    portfolio: str | None = Query(None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Сколько денег можно поднять под портфель, не продавая бумаги."""
    return collateral_service.portfolio_collateral(session, portfolio=portfolio)
