"""Денежная позиция: счета, движения, размещения, платёжный календарь."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import CashAccount, CashFlow, Placement
from ..schemas import (
    CashAccountCreate,
    CashAccountRead,
    CashFlowCreate,
    CashFlowRead,
    PlacementCreate,
    PlacementRead,
)
from ..services import cash as cash_service
from ..services.tabular import to_csv, to_xlsx
from ..services.auth import audit, require_trader, require_viewer

router = APIRouter(prefix="/api/cash", tags=["Деньги"])


@router.get("/position", summary="Денежная позиция")
def position(
    portfolio: str | None = Query(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> dict[str, Any]:
    """Остатки по счетам и валютам, действующие размещения."""
    return cash_service.cash_position(session, portfolio=portfolio)


@router.get("/calendar", summary="Платёжный календарь")
def calendar(
    portfolio: str | None = Query(None),
    horizon_days: int = Query(180, ge=1, le=1095),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> dict[str, Any]:
    """Ожидаемые движения денег с накопленным остатком и кассовым разрывом."""
    return cash_service.payment_calendar(
        session, portfolio=portfolio, horizon_days=horizon_days
    )


@router.get("/calendar/download", summary="Выгрузить платёжный календарь")
def download_calendar(
    portfolio: str | None = Query(None),
    horizon_days: int = Query(180, ge=1, le=1095),
    fmt: Literal["xlsx", "csv"] = Query("xlsx"),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> Response:
    """Тот же календарь, что на экране, но файлом для Excel."""
    result = cash_service.payment_calendar(
        session, portfolio=portfolio, horizon_days=horizon_days
    )
    if not result["events"]:
        raise HTTPException(
            status_code=404, detail="На этом горизонте движений не запланировано"
        )

    columns = list(cash_service.CALENDAR_COLUMNS)
    rows = cash_service.calendar_rows_for_export(result["events"])
    stem = f"Платёжный календарь {date.today():%d.%m.%Y}"

    if fmt == "csv":
        content = to_csv(columns, rows)
        media_type = "text/csv; charset=utf-8"
        filename = f"{stem}.csv"
    else:
        money = lambda value: f"{value:,.2f}".replace(",", " ")  # noqa: E731
        content = to_xlsx(
            columns,
            rows,
            sheet_title="Календарь",
            meta=[
                ("Портфель", portfolio or "все"),
                ("Горизонт, дней", str(horizon_days)),
                ("Остаток на начало, ₽", money(result["opening_balance"])),
                ("Остаток на конец, ₽", money(result["closing_balance"])),
                ("Минимальный остаток, ₽", money(result["lowest_balance"])),
                (
                    "Кассовый разрыв",
                    f"{result['gap_date']:%d.%m.%Y}" if result["gap_date"] else "нет",
                ),
                ("Сформировано", date.today().strftime("%d.%m.%Y")),
            ],
        )
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{stem}.xlsx"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/history", summary="Состоявшиеся движения")
def history(
    portfolio: str | None = Query(None),
    days: int = Query(90, ge=1, le=1095),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return cash_service.settlement_history(session, portfolio=portfolio, days=days)


# ----------------------------------------------------------------------
# Счета
# ----------------------------------------------------------------------
@router.get("/accounts", response_model=list[CashAccountRead], summary="Счета")
def list_accounts(
    portfolio: str | None = Query(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> list[CashAccount]:
    statement = select(CashAccount).order_by(CashAccount.name)
    if portfolio:
        statement = statement.where(CashAccount.portfolio == portfolio)
    return list(session.execute(statement).scalars())


@router.post(
    "/accounts",
    response_model=CashAccountRead,
    status_code=status.HTTP_201_CREATED,
    summary="Открыть счёт",
)
def create_account(
    payload: CashAccountCreate,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> CashAccount:
    account = CashAccount(**payload.model_dump())
    session.add(account)
    session.commit()
    session.refresh(account)
    audit(session, user, action="create", entity="cash_account",
          entity_id=account.id, detail=f"{account.name} {account.currency}")
    return account


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить счёт",
)
def delete_account(
    account_id: int,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> None:
    account = session.get(CashAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    session.delete(account)
    session.commit()
    audit(session, user, action="delete", entity="cash_account", entity_id=account_id)


# ----------------------------------------------------------------------
# Движения
# ----------------------------------------------------------------------
@router.get("/flows", response_model=list[CashFlowRead], summary="Движения по счетам")
def list_flows(
    account_id: int | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> list[CashFlow]:
    statement = select(CashFlow).order_by(CashFlow.flow_date.desc(), CashFlow.id.desc())
    if account_id:
        statement = statement.where(CashFlow.account_id == account_id)
    return list(session.execute(statement.limit(limit)).scalars())


@router.post(
    "/flows",
    response_model=CashFlowRead,
    status_code=status.HTTP_201_CREATED,
    summary="Завести движение",
)
def create_flow(
    payload: CashFlowCreate,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> CashFlow:
    if session.get(CashAccount, payload.account_id) is None:
        raise HTTPException(status_code=422, detail="Счёт не найден")

    flow = CashFlow(**payload.model_dump())
    session.add(flow)
    session.commit()
    session.refresh(flow)
    audit(session, user, action="create", entity="cash_flow", entity_id=flow.id,
          detail=f"{flow.kind} {flow.amount:+.2f}")
    return flow


@router.delete(
    "/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить движение"
)
def delete_flow(
    flow_id: int,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> None:
    flow = session.get(CashFlow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Движение не найдено")
    session.delete(flow)
    session.commit()
    audit(session, user, action="delete", entity="cash_flow", entity_id=flow_id)


# ----------------------------------------------------------------------
# Размещения
# ----------------------------------------------------------------------
@router.get("/placements", response_model=list[PlacementRead], summary="Размещения")
def list_placements(
    portfolio: str | None = Query(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> list[Placement]:
    statement = select(Placement).order_by(Placement.end_date)
    if portfolio:
        statement = statement.where(Placement.portfolio == portfolio)
    return list(session.execute(statement).scalars())


@router.post(
    "/placements",
    response_model=PlacementRead,
    status_code=status.HTTP_201_CREATED,
    summary="Разместить или привлечь",
)
def create_placement(
    payload: PlacementCreate,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> Placement:
    if payload.account_id and session.get(CashAccount, payload.account_id) is None:
        raise HTTPException(status_code=422, detail="Счёт не найден")

    placement = Placement(**payload.model_dump())
    session.add(placement)
    session.commit()
    session.refresh(placement)
    audit(session, user, action="create", entity="placement", entity_id=placement.id,
          detail=f"{placement.kind} {placement.amount:.2f} под {placement.rate}%")
    return placement


@router.delete(
    "/placements/{placement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить размещение",
)
def delete_placement(
    placement_id: int,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> None:
    placement = session.get(Placement, placement_id)
    if placement is None:
        raise HTTPException(status_code=404, detail="Размещение не найдено")
    session.delete(placement)
    session.commit()
    audit(session, user, action="delete", entity="placement", entity_id=placement_id)
