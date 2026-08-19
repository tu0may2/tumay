"""Портфель и сделки казначейства."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Deal, Instrument
from ..schemas import (
    DealBulkCreate,
    DealBulkResult,
    DealCreate,
    DealRead,
    PortfolioAccounting,
)
from ..services.auth import audit, require_trader
from ..services import portfolio as portfolio_service
from ..services import revaluation as revaluation_service
from ..services import risk as risk_service
from ..services.tabular import to_csv, to_xlsx

router = APIRouter(prefix="/api/portfolio", tags=["Портфель"])


@router.get("", summary="Сводка по портфелю")
def get_portfolio(
    name: str | None = Query(None, description="Имя портфеля; пусто — все сразу"),
    method: str | None = Query(None, description="fifo или average"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Стоимость, результат с разложением, дюрация, концентрация и валюты."""
    return portfolio_service.portfolio_summary(session, portfolio=name, method=method)


@router.get("/names", summary="Список портфелей")
def get_portfolio_names(session: Session = Depends(get_session)) -> list[str]:
    return portfolio_service.portfolio_names(session)


@router.get("/accounting", summary="Виды учёта портфелей")
def get_accounting(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Какой портфель как учитывается: торговый или до погашения."""
    titles = {"trading": "Торговый", "htm": "До погашения"}
    return [
        {"name": name, "accounting_type": kind, "title": titles.get(kind, kind)}
        for name, kind in sorted(portfolio_service.accounting_types(session).items())
    ]


@router.put("/accounting", summary="Задать вид учёта портфеля")
def set_accounting(
    payload: PortfolioAccounting,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> dict[str, Any]:
    """Сменить вид учёта. От него зависит, идёт ли переоценка по рынку."""
    try:
        record = portfolio_service.set_accounting_type(
            session, payload.name, payload.accounting_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    audit(
        session,
        user,
        action="update",
        entity="portfolio",
        entity_id=payload.name,
        detail=f"вид учёта: {payload.accounting_type}",
    )
    return {"name": record.name, "accounting_type": record.accounting_type}


@router.get("/revaluation", summary="Переоценка портфеля")
def get_revaluation(
    name: str | None = Query(None, description="Имя портфеля; пусто — все сразу"),
    method: str | None = Query(None, description="fifo или average"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Переоценка: накопленная, за день по СВЦ и амортизированная стоимость."""
    return revaluation_service.revaluate(session, portfolio=name, method=method)


@router.get("/revaluation/download", summary="Выгрузить переоценку")
def download_revaluation(
    name: str | None = Query(None),
    method: str | None = Query(None),
    fmt: Literal["xlsx", "csv"] = Query("xlsx"),
    session: Session = Depends(get_session),
) -> Response:
    """Тот же блок переоценки, но файлом для Excel."""
    result = revaluation_service.revaluate(session, portfolio=name, method=method)
    if not result["items"]:
        raise HTTPException(status_code=404, detail="В портфеле нет позиций")

    columns = list(revaluation_service.REVALUATION_COLUMNS)
    rows = revaluation_service.rows_for_export(result["items"])
    stem = f"Переоценка {date.today():%d.%m.%Y}"

    if fmt == "csv":
        content = to_csv(columns, rows)
        media_type = "text/csv; charset=utf-8"
        filename = f"{stem}.csv"
    else:
        totals = result["totals"]
        content = to_xlsx(
            columns,
            rows,
            sheet_title="Переоценка",
            meta=[
                ("Портфель", name or "все"),
                ("Позиций", str(totals["positions"])),
                ("Учётная стоимость, ₽", f"{totals['carrying_value_rub']:,.2f}".replace(",", " ")),
                ("Переоценка за день, ₽", f"{totals['daily_reval_rub']:,.2f}".replace(",", " ")),
                ("Переоценка накопленная, ₽", f"{totals['total_reval_rub']:,.2f}".replace(",", " ")),
                ("Сформировано", date.today().strftime("%d.%m.%Y")),
                (
                    "Примечание",
                    "По портфелю до погашения рыночная переоценка справочная "
                    "и в итоги не включена",
                ),
            ],
        )
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{stem}.xlsx"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/sensitivity", summary="Чувствительность к ставке")
def get_sensitivity(
    name: str | None = Query(None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Переоценка облигаций при параллельном сдвиге кривой."""
    return risk_service.rate_sensitivity(session, portfolio=name)


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
def create_deal(
    payload: DealCreate,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> Deal:
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


@router.post(
    "/deals/bulk",
    response_model=DealBulkResult,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить несколько сделок сразу",
)
def create_deals_bulk(
    payload: DealBulkCreate,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> dict[str, Any]:
    """Завести пачку сделок из витрины бумаг.

    Одна плохая строка не отменяет остальные: что удалось — сохраняется,
    по остальным возвращается причина отказа.
    """
    known = {
        row[0]
        for row in session.execute(
            select(Instrument.secid).where(
                Instrument.secid.in_([deal.secid for deal in payload.deals])
            )
        ).all()
    }

    created: list[Deal] = []
    errors: list[dict[str, Any]] = []
    for item in payload.deals:
        if item.secid not in known:
            errors.append({
                "secid": item.secid,
                "detail": "Инструмент не найден в справочнике",
            })
            continue
        deal = Deal(**item.model_dump())
        session.add(deal)
        created.append(deal)

    session.commit()
    for deal in created:
        session.refresh(deal)

    return {
        "created": created,
        "errors": errors,
        "created_count": len(created),
        "error_count": len(errors),
    }


@router.delete(
    "/deals/{deal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить сделку",
)
def delete_deal(
    deal_id: int,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> None:
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    session.delete(deal)
    session.commit()
