"""Рыночные данные: обзор, витрина инструментов, история, кривая, сигналы."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Bar, CorpAction, FxRate, Instrument, MacroRate, Quote
from ..services import analytics
from ..sources.moex import BOARD_SPECS

router = APIRouter(prefix="/api", tags=["Рынок"])


@router.get("/overview", summary="Сводка рынка")
def get_overview(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Ключевая ставка, курсы, индексы, обороты и кривая доходности."""
    return analytics.market_overview(session)


@router.get("/instruments", summary="Витрина инструментов")
def get_instruments(
    kind: list[str] | None = Query(None, description="share | bond | index | currency"),
    board: list[str] | None = Query(None, description="Режим торгов, например TQBR"),
    search: str | None = Query(None, description="Поиск по коду, названию или ISIN"),
    min_turnover: float | None = Query(None, ge=0, description="Минимальный оборот, руб."),
    min_liquidity: float | None = Query(None, ge=0, le=100),
    min_yield: float | None = Query(None, description="Минимальная доходность, %"),
    max_yield: float | None = Query(None, description="Максимальная доходность, %"),
    max_duration_years: float | None = Query(None, ge=0),
    sort_by: str = Query("turnover"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Отбор бумаг по ликвидности, доходности и дюрации."""
    return analytics.screener(
        session,
        kinds=kind,
        boards=board,
        search=search,
        min_turnover=min_turnover,
        min_liquidity=min_liquidity,
        min_yield=min_yield,
        max_yield=max_yield,
        max_duration_years=max_duration_years,
        sort_by=sort_by,
        descending=order == "desc",
        limit=limit,
        offset=offset,
    )


@router.get("/instruments/{secid}", summary="Карточка инструмента")
def get_instrument(
    secid: str,
    history_days: int = Query(180, ge=1, le=1825),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Всё, что известно о бумаге: параметры, рынок, история, выплаты."""
    secid = secid.upper()
    rows = analytics.latest_rows(session, secids=(secid,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"Инструмент {secid} не найден")

    instrument, quote = rows[0]
    curve = analytics.yield_curve(session)
    detail = analytics.build_row(instrument, quote, curve["points"])

    since = date.today() - timedelta(days=history_days)
    bars = session.execute(
        select(Bar)
        .where(Bar.instrument_id == instrument.id, Bar.trade_date >= since)
        .order_by(Bar.trade_date)
    ).scalars()

    intraday = session.execute(
        select(Quote.ts, Quote.last, Quote.turnover, Quote.volume)
        .where(Quote.instrument_id == instrument.id)
        .order_by(Quote.ts.desc())
        .limit(200)
    ).all()

    cashflows = []
    if instrument.isin:
        cashflows = [
            {
                "action_type": action.action_type,
                "action_date": action.action_date,
                "record_date": action.record_date,
                "value": action.value,
                "value_rub": action.value_rub,
                "value_pct": action.value_pct,
                "face_unit": action.face_unit,
                "source": action.source,
            }
            for action in session.execute(
                select(CorpAction)
                .where(CorpAction.isin == instrument.isin)
                .order_by(CorpAction.action_date)
            ).scalars()
        ]

    return {
        "instrument": detail,
        "history": [
            {
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "wa_price": bar.wa_price,
                "volume": bar.volume,
                "turnover": bar.turnover,
                "num_trades": bar.num_trades,
            }
            for bar in bars
        ],
        "intraday": [
            {"ts": ts, "last": last, "turnover": turnover, "volume": volume}
            for ts, last, turnover, volume in reversed(intraday)
        ],
        "cashflows": cashflows,
    }


@router.get("/curve", summary="Кривая бескупонной доходности")
def get_curve(session: Session = Depends(get_session)) -> dict[str, Any]:
    """КБД МосБиржи — ориентир безрисковой стоимости денег по срокам."""
    return analytics.yield_curve(session)


@router.get("/movers", summary="Лидеры роста и падения")
def get_movers(
    kind: str = Query("share"),
    limit: int = Query(5, ge=1, le=50),
    min_turnover: float = Query(1_000_000, ge=0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return analytics.movers(session, kind=kind, limit=limit, min_turnover=min_turnover)


@router.get("/anomalies", summary="Аномалии торговых объёмов")
def get_anomalies(
    lookback_days: int = Query(30, ge=7, le=365),
    z_threshold: float = Query(2.0, ge=0.5, le=10),
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Бумаги, где объём заметно отклонился от собственной нормы."""
    return analytics.volume_anomalies(
        session, lookback_days=lookback_days, z_threshold=z_threshold, limit=limit
    )


@router.get("/alerts", summary="Сигналы для казначея")
def get_alerts(
    limit: int = Query(30, ge=1, le=100), session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    return analytics.alerts(session, limit=limit)


@router.get("/calendar", summary="Календарь выплат")
def get_calendar(
    horizon_days: int = Query(90, ge=1, le=730),
    secid: list[str] | None = Query(None),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Купоны, амортизации и оферты по данным НРД."""
    return analytics.cashflow_calendar(
        session, horizon_days=horizon_days, secids=secid
    )


@router.get("/fx", summary="Курсы валют ЦБ РФ")
def get_fx(
    days: int = Query(1, ge=1, le=365),
    code: list[str] | None = Query(None),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    since = date.today() - timedelta(days=days)
    statement = (
        select(FxRate)
        .where(FxRate.source == "cbr", FxRate.rate_date >= since)
        .order_by(FxRate.rate_date.desc(), FxRate.code)
    )
    if code:
        statement = statement.where(FxRate.code.in_([c.upper() for c in code]))
    return [
        {
            "code": rate.code,
            "name": rate.name,
            "nominal": rate.nominal,
            "value": rate.value,
            "unit_value": rate.value / (rate.nominal or 1),
            "date": rate.rate_date,
        }
        for rate in session.execute(statement).scalars()
    ]


@router.get("/rates", summary="Ставки денежного рынка")
def get_rates(
    code: str = Query("KEY_RATE", description="KEY_RATE | RUONIA"),
    days: int = Query(365, ge=1, le=3650),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    since = date.today() - timedelta(days=days)
    rows = session.execute(
        select(MacroRate)
        .where(MacroRate.code == code.upper(), MacroRate.rate_date >= since)
        .order_by(MacroRate.rate_date)
    ).scalars()
    return [
        {"code": row.code, "name": row.name, "value": row.value, "date": row.rate_date}
        for row in rows
    ]


@router.get("/boards", summary="Доступные режимы торгов")
def get_boards(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Справочник площадок с числом загруженных инструментов."""
    counts = dict(
        session.execute(
            select(Instrument.board, func.count()).group_by(Instrument.board)
        ).all()
    )
    return [
        {
            "board": board,
            "title": spec["title"],
            "kind": spec["kind"],
            "instruments": counts.get(board, 0),
        }
        for board, spec in BOARD_SPECS.items()
    ]
