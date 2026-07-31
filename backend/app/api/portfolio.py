"""Портфель и сделки казначейства."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Deal, Instrument
from ..schemas import DealCreate, DealRead
from ..services import portfolio as portfolio_service

router = APIRouter(prefix="/api/portfolio", tags=["Портфель"])


@router.get("", summary="Сводка по портфелю")
def get_portfolio(
    name: str | None = Query(None, description="Имя портфеля; пусто — все сразу"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Стоимость, финансовый результат, дюрация и концентрация."""
    return portfolio_service.portfolio_summary(session, portfolio=name)


@router.get("/names", summary="Список портфелей")
def get_portfolio_names(session: Session = Depends(get_session)) -> list[str]:
    return portfolio_service.portfolio_names(session)


@router.get("/sensitivity", summary="Чувствительность к ставке")
def get_sensitivity(
    name: str | None = Query(None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Переоценка облигаций при параллельном сдвиге кривой."""
    return portfolio_service.rate_sensitivity(session, portfolio=name)


@router.get("/deals", response_model=list[DealRead], summary="Журнал сделок")
def list_deals(
    name: str | None = Query(None),
    secid: str | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> list[Deal]:
    statement = select(Deal).order_by(Deal.trade_date.desc(), Deal.id.desc())
    if name:
        statement = statement.where(Deal.portfolio == name)
    if secid:
        statement = statement.where(Deal.secid == secid.upper())
    return list(session.execute(statement.limit(limit)).scalars())


@router.post(
    "/deals",
    response_model=DealRead,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать сделку",
)
def create_deal(payload: DealCreate, session: Session = Depends(get_session)) -> Deal:
    """Добавить сделку. Инструмент должен быть в справочнике."""
    known = session.execute(
        select(Instrument.id).where(Instrument.secid == payload.secid).limit(1)
    ).first()
    if known is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Инструмент {payload.secid} не найден в справочнике. "
                "Запустите сбор данных или проверьте код бумаги."
            ),
        )

    deal = Deal(**payload.model_dump())
    session.add(deal)
    session.commit()
    session.refresh(deal)
    return deal


@router.delete(
    "/deals/{deal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить сделку",
)
def delete_deal(deal_id: int, session: Session = Depends(get_session)) -> None:
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    session.delete(deal)
    session.commit()
