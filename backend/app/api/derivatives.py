"""Срочный рынок: контракты, опционная доска и расчёт позиции."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import MacroRate
from ..services import derivatives as service

router = APIRouter(prefix="/api/derivatives", tags=["Срочный рынок"])


def risk_free_rate(session: Session) -> float:
    """Безрисковая ставка для модели — последняя ключевая ставка ЦБ.

    Она же используется на вкладке облигаций, поэтому расчёты по разным
    инструментам опираются на одно и то же значение.
    """
    value = session.execute(
        select(MacroRate.value)
        .where(MacroRate.code == "KEY_RATE")
        .order_by(MacroRate.rate_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (value or 0.0) / 100.0


class LegRequest(BaseModel):
    """Одна нога позиции."""

    secid: str = Field(..., description="Код контракта, например SRU6 или SR270CH6")
    direction: int = Field(1, description="1 — купили, -1 — продали")
    quantity: int = Field(1, ge=1, le=1_000_000)
    entry_price: float | None = Field(
        None, description="Цена входа; без неё берётся текущая рыночная"
    )
    volatility: float | None = Field(
        None, description="Волатильность биржи, % годовых — из опционной доски"
    )


class PositionRequest(BaseModel):
    legs: list[LegRequest] = Field(..., min_length=1, max_length=20)
    underlying_price: float | None = Field(
        None, description="Центр профиля выплат; по умолчанию текущая цена актива"
    )


@router.get("/assets", summary="Базовые активы опционного рынка")
async def get_assets() -> list[dict[str, Any]]:
    """Активы, по которым торгуются опционы, — самые ликвидные сверху."""
    return await service.list_assets()


@router.get("/futures", summary="Фьючерсы ФОРТС")
async def get_futures(
    asset: str | None = Query(None, description="Код базового актива, например SBRF"),
    search: str | None = Query(None, description="Поиск по коду или названию"),
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    contracts = await service.list_futures(asset)
    if search:
        needle = search.strip().lower()
        contracts = [
            item
            for item in contracts
            if needle in item["secid"].lower() or needle in (item["name"] or "").lower()
        ]
    return contracts[:limit]


@router.get("/expiries/{asset}", summary="Даты экспирации опционов")
async def get_expiries(asset: str) -> list[dict[str, Any]]:
    try:
        return await service.option_expiries(asset)
    except Exception as error:  # noqa: BLE001 — биржа могла не отдать серию
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/board/{asset}", summary="Опционная доска")
async def get_board(
    asset: str,
    expiry: date | None = Query(None, description="Дата экспирации серии"),
) -> dict[str, Any]:
    """Страйки серии с ценами и волатильностью биржи."""
    try:
        return await service.option_board(asset, expiry)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/contract/{secid}", summary="Параметры контракта")
async def get_contract(secid: str) -> dict[str, Any]:
    contract = await service.find_contract(secid)
    if contract is None:
        raise HTTPException(
            status_code=404, detail=f"Контракт {secid} не найден на срочном рынке"
        )
    return service._as_dict(contract)


@router.post("/position", summary="Расчёт позиции")
async def calculate_position(
    payload: PositionRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Результат позиции: прибыль, обеспечение, греки и профиль выплат."""
    try:
        return await service.calculate(
            [leg.model_dump() for leg in payload.legs],
            risk_free_rate(session),
            payload.underlying_price,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/candles/{secid}", summary="Ход торгов по контракту")
async def get_candles(
    secid: str,
    interval: int = Query(10, description="Шаг свечи в минутах: 1, 10 или 60"),
    days: int = Query(1, ge=1, le=30),
) -> dict[str, Any]:
    """Свечи для графика цены с уровнем позиции."""
    try:
        return await service.contract_candles(secid, interval, days)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
