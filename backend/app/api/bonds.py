"""Развёрнутый анализ облигаций и его выгрузка."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_session
from ..services import bonds as bonds_service
from ..services.tabular import to_csv, to_xlsx

router = APIRouter(prefix="/api/bonds", tags=["Облигации"])


def _filters(
    search: str | None = Query(None, description="Код, ISIN или наименование"),
    min_yield: float | None = Query(None, description="Доходность от, %"),
    max_yield: float | None = Query(None, description="Доходность до, %"),
    min_duration_years: float | None = Query(None, ge=0),
    max_duration_years: float | None = Query(None, ge=0),
    maturity_from: date | None = Query(None, description="Погашение не раньше"),
    maturity_to: date | None = Query(None, description="Погашение не позже"),
    min_turnover: float | None = Query(None, ge=0, description="Оборот от, ₽"),
    min_liquidity: float | None = Query(None, ge=0, le=100),
    list_level: list[int] | None = Query(None, description="Уровни листинга: 1, 2, 3"),
    currency: list[str] | None = Query(None, description="Валюта: SUR, USD, CNY…"),
    coupon_type: list[str] | None = Query(None, description="fixed | float | unknown"),
    has_offer: bool | None = Query(None),
    has_amortization: bool | None = Query(None),
    max_risk_score: float | None = Query(None, ge=0, le=100),
    sort_by: str = Query("yield_pct"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    """Общий набор фильтров для показа и выгрузки — чтобы файл совпадал с экраном."""
    return {
        "search": search,
        "min_yield": min_yield,
        "max_yield": max_yield,
        "min_duration_years": min_duration_years,
        "max_duration_years": max_duration_years,
        "maturity_from": maturity_from,
        "maturity_to": maturity_to,
        "min_turnover": min_turnover,
        "min_liquidity": min_liquidity,
        "list_levels": list_level,
        "currencies": [item.upper() for item in currency] if currency else None,
        "coupon_types": coupon_type,
        "has_offer": has_offer,
        "has_amortization": has_amortization,
        "max_risk_score": max_risk_score,
        "sort_by": sort_by,
        "descending": order == "desc",
    }


@router.get("/analysis", summary="Анализ облигаций")
def analysis(
    filters: dict[str, Any] = Depends(_filters),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Полная витрина по облигациям: доходность, премия к КБД, купон, риск."""
    return bonds_service.analyse(session, limit=limit, offset=offset, **filters)


@router.get("/analysis/download", summary="Выгрузить анализ облигаций")
def download_analysis(
    filters: dict[str, Any] = Depends(_filters),
    fmt: Literal["xlsx", "csv"] = Query("xlsx"),
    limit: int = Query(1000, ge=1, le=5000),
    session: Session = Depends(get_session),
) -> Response:
    """Тот же отбор, что на экране, но файлом для Excel."""
    result = bonds_service.analyse(session, limit=limit, offset=0, **filters)
    if not result["items"]:
        raise HTTPException(
            status_code=404, detail="Под заданные условия не подошёл ни один выпуск"
        )

    columns = list(bonds_service.ANALYSIS_COLUMNS)
    rows = bonds_service.rows_for_export(result["items"])
    stem = f"Анализ облигаций {date.today():%d.%m.%Y}"

    if fmt == "csv":
        content = to_csv(columns, rows)
        media_type = "text/csv; charset=utf-8"
        filename = f"{stem}.csv"
    else:
        content = to_xlsx(
            columns,
            rows,
            sheet_title="Облигации",
            meta=[
                ("Выпусков", str(len(rows))),
                ("КБД на дату", str(result["curve_date"] or "—")),
                ("Источники", "MOEX ISS, Банк России, НРД"),
                ("Сформировано", date.today().strftime("%d.%m.%Y")),
                ("Примечание", "Оценка риска расчётная, не рейтинг агентства"),
            ],
        )
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{stem}.xlsx"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/filters", summary="Справочники для фильтров")
def filter_options(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Значения, которые реально встречаются в загруженных данных."""
    from sqlalchemy import select

    from ..models import Instrument

    currencies = session.execute(
        select(Instrument.currency)
        .where(Instrument.kind == "bond", Instrument.currency.isnot(None))
        .distinct()
    ).scalars()
    bond_types = session.execute(
        select(Instrument.bond_type)
        .where(Instrument.kind == "bond", Instrument.bond_type.isnot(None))
        .distinct()
    ).scalars()

    return {
        "currencies": sorted({c for c in currencies if c}),
        "bond_types": sorted({b for b in bond_types if b}),
        "coupon_types": [
            {"code": code, "title": title}
            for code, title in bonds_service.COUPON_TITLES.items()
        ],
        "list_levels": [1, 2, 3],
    }
