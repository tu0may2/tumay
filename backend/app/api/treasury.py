"""Лимиты, денежные потоки, бенчмарк, сохранённые отборы и наблюдение."""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Instrument, Limit, SavedScreen, WatchItem
from ..schemas import (
    LimitCreate,
    LimitRead,
    SavedScreenCreate,
    SavedScreenRead,
    TradePreview,
    WatchItemCreate,
    WatchItemRead,
)
from ..services import benchmark as benchmark_service
from ..services import limits as limits_service
from ..services import risk as risk_service

router = APIRouter(prefix="/api", tags=["Казначейство"])


# ----------------------------------------------------------------------
# Лимиты
# ----------------------------------------------------------------------
@router.get("/limits/kinds", summary="Виды лимитов")
def limit_kinds() -> list[dict[str, Any]]:
    """Справочник видов лимитов для формы."""
    return [
        {"kind": kind, **meta} for kind, meta in limits_service.LIMIT_KINDS.items()
    ]


@router.get("/limits", response_model=list[LimitRead], summary="Список лимитов")
def list_limits(
    portfolio: str | None = Query(None), session: Session = Depends(get_session)
) -> list[Limit]:
    statement = select(Limit).order_by(Limit.kind, Limit.target)
    if portfolio:
        statement = statement.where(Limit.portfolio == portfolio)
    return list(session.execute(statement).scalars())


@router.post(
    "/limits",
    response_model=LimitRead,
    status_code=status.HTTP_201_CREATED,
    summary="Установить лимит",
)
def create_limit(payload: LimitCreate, session: Session = Depends(get_session)) -> Limit:
    if payload.kind not in limits_service.LIMIT_KINDS:
        raise HTTPException(status_code=422, detail=f"Неизвестный вид лимита: {payload.kind}")

    existing = session.execute(
        select(Limit).where(
            Limit.portfolio == payload.portfolio,
            Limit.kind == payload.kind,
            Limit.target == (payload.target or None),
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Повторная установка того же лимита обновляет значение, а не плодит дубли
        existing.value = payload.value
        existing.comment = payload.comment
        existing.enabled = True
        session.commit()
        session.refresh(existing)
        return existing

    limit = Limit(**payload.model_dump())
    session.add(limit)
    session.commit()
    session.refresh(limit)
    return limit


@router.delete(
    "/limits/{limit_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Снять лимит"
)
def delete_limit(limit_id: int, session: Session = Depends(get_session)) -> None:
    limit = session.get(Limit, limit_id)
    if limit is None:
        raise HTTPException(status_code=404, detail="Лимит не найден")
    session.delete(limit)
    session.commit()


@router.get("/limits/check", summary="Соблюдение лимитов")
def check_limits(
    portfolio: str | None = Query(None), session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Что нарушено сейчас и насколько заполнены остальные лимиты."""
    return limits_service.check_limits(session, portfolio=portfolio)


@router.post("/limits/preview", summary="Проверка сделки до совершения")
def preview_trade(
    payload: TradePreview, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Покажет, выведет ли сделка портфель за лимиты."""
    return limits_service.preview_deal(
        session,
        secid=payload.secid,
        quantity=payload.quantity,
        price=payload.price,
        portfolio=payload.portfolio,
    )


# ----------------------------------------------------------------------
# Денежные потоки и риск
# ----------------------------------------------------------------------
@router.get("/portfolio/cashflow", summary="Календарь потоков по портфелю")
def portfolio_cashflow(
    name: str | None = Query(None, description="Имя портфеля"),
    horizon_days: int = Query(365, ge=1, le=3650),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Купоны, амортизации и погашения по своим позициям в рублях."""
    return risk_service.portfolio_cashflow(
        session, portfolio=name, horizon_days=horizon_days
    )


# ----------------------------------------------------------------------
# Бенчмарк
# ----------------------------------------------------------------------
@router.get("/benchmark", summary="Сравнение портфеля с индексами")
def benchmark(
    name: str | None = Query(None),
    days: int = Query(90, ge=7, le=1825),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return benchmark_service.compare_portfolio(session, portfolio=name, days=days)


@router.get("/instruments/{secid}/spread-history", summary="История премии выпуска")
def spread_history(
    secid: str,
    days: int = Query(365, ge=30, le=1825),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Премия бумаги к индексу гособлигаций во времени."""
    return benchmark_service.spread_history(session, secid, days=days)


# ----------------------------------------------------------------------
# Сохранённые отборы
# ----------------------------------------------------------------------
@router.get("/screens", response_model=list[SavedScreenRead], summary="Сохранённые отборы")
def list_screens(
    view: str | None = Query(None), session: Session = Depends(get_session)
) -> list[SavedScreen]:
    statement = select(SavedScreen).order_by(SavedScreen.view, SavedScreen.name)
    if view:
        statement = statement.where(SavedScreen.view == view)
    return list(session.execute(statement).scalars())


@router.post(
    "/screens",
    response_model=SavedScreenRead,
    status_code=status.HTTP_201_CREATED,
    summary="Сохранить отбор",
)
def create_screen(
    payload: SavedScreenCreate, session: Session = Depends(get_session)
) -> SavedScreen:
    existing = session.execute(
        select(SavedScreen).where(
            SavedScreen.view == payload.view, SavedScreen.name == payload.name
        )
    ).scalar_one_or_none()

    params = json.dumps(payload.params, ensure_ascii=False)
    if existing is not None:
        existing.params = params
        session.commit()
        session.refresh(existing)
        return existing

    screen = SavedScreen(view=payload.view, name=payload.name, params=params)
    session.add(screen)
    session.commit()
    session.refresh(screen)
    return screen


@router.delete(
    "/screens/{screen_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить отбор",
)
def delete_screen(screen_id: int, session: Session = Depends(get_session)) -> None:
    screen = session.get(SavedScreen, screen_id)
    if screen is None:
        raise HTTPException(status_code=404, detail="Отбор не найден")
    session.delete(screen)
    session.commit()


# ----------------------------------------------------------------------
# Список наблюдения
# ----------------------------------------------------------------------
@router.get("/watchlist", summary="Список наблюдения")
def get_watchlist(
    name: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Отслеживаемые бумаги с текущими рыночными данными."""
    from ..services.analytics import build_row, latest_rows, yield_curve

    statement = select(WatchItem).order_by(WatchItem.created_at)
    if name:
        statement = statement.where(WatchItem.watchlist == name)
    items = list(session.execute(statement).scalars())
    if not items:
        return []

    curve = yield_curve(session)
    rows = {
        instrument.secid: build_row(instrument, quote, curve["points"])
        for instrument, quote in latest_rows(
            session, secids=[item.secid for item in items]
        )
    }
    return [
        {
            "id": item.id,
            "watchlist": item.watchlist,
            "note": item.note,
            **(rows.get(item.secid) or {"secid": item.secid, "name": item.secid}),
        }
        for item in items
    ]


@router.post(
    "/watchlist",
    response_model=WatchItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить в наблюдение",
)
def add_watch(
    payload: WatchItemCreate, session: Session = Depends(get_session)
) -> WatchItem:
    known = session.execute(
        select(Instrument.id).where(Instrument.secid == payload.secid).limit(1)
    ).first()
    if known is None:
        raise HTTPException(
            status_code=422,
            detail=f"Инструмент {payload.secid} не найден в справочнике",
        )

    existing = session.execute(
        select(WatchItem).where(
            WatchItem.secid == payload.secid, WatchItem.watchlist == payload.watchlist
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.note = payload.note
        session.commit()
        session.refresh(existing)
        return existing

    item = WatchItem(**payload.model_dump())
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.delete(
    "/watchlist/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Убрать из наблюдения",
)
def remove_watch(item_id: int, session: Session = Depends(get_session)) -> None:
    item = session.get(WatchItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    session.delete(item)
    session.commit()
