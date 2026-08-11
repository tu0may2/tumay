"""Управление сбором данных и состояние системы."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import CollectionRun, Instrument, Quote
from ..schemas import CollectRequest, HealthResponse
from ..services.auth import require_trader, require_viewer
from ..services.collector import collector

router = APIRouter(prefix="/api", tags=["Служебное"])


@router.get("/health", response_model=HealthResponse, summary="Состояние сервиса")
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    instruments = session.execute(select(func.count()).select_from(Instrument)).scalar() or 0
    quotes = session.execute(select(func.count()).select_from(Quote)).scalar() or 0
    last_run = session.execute(
        select(CollectionRun.finished_at)
        .where(CollectionRun.status == "success")
        .order_by(CollectionRun.finished_at.desc())
        .limit(1)
    ).scalar()
    return {
        "status": "ok" if instruments else "empty",
        "instruments": instruments,
        "quotes": quotes,
        "last_collection": last_run.isoformat() if last_run else None,
    }


@router.post("/collect", summary="Запустить сбор данных")
async def run_collection(
    payload: CollectRequest,
    background: BackgroundTasks,
    user: dict = Depends(require_trader),
) -> dict[str, str]:
    """Поставить полный цикл сбора в фон — ответ приходит сразу."""
    background.add_task(collector.collect_all, with_history=payload.with_history)
    return {
        "status": "accepted",
        "detail": "Сбор запущен в фоне, следите за /api/collect/runs",
    }


@router.get("/collect/runs", summary="Журнал сбора данных")
def collection_runs(
    limit: int = Query(30, ge=1, le=200),
    session: Session = Depends(get_session),
    user: dict = Depends(require_viewer),
) -> list[dict[str, Any]]:
    """Что и когда собиралось, сколько строк и с какой ошибкой."""
    runs = session.execute(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(limit)
    ).scalars()
    return [
        {
            "id": run.id,
            "source": run.source,
            "task": run.task,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_sec": round(run.duration_sec, 2) if run.duration_sec else None,
            "rows": run.rows,
            "error": run.error,
        }
        for run in runs
    ]


@router.get("/sources", summary="Используемые источники данных")
def sources(user: dict = Depends(require_viewer)) -> list[dict[str, Any]]:
    """Открытые источники и что именно берётся из каждого."""
    return [
        {
            "code": "moex",
            "name": "Московская биржа (ISS)",
            "url": "https://iss.moex.com/iss/reference/",
            "access": "открытый, без авторизации",
            "provides": [
                "цены и котировки bid/ask",
                "торговые объёмы и число сделок",
                "доходности, дюрация, G- и Z-спреды облигаций",
                "история торгов",
                "кривая бескупонной доходности",
            ],
        },
        {
            "code": "cbr",
            "name": "Банк России",
            "url": "https://www.cbr.ru/development/SXML/",
            "access": "открытый, без авторизации",
            "provides": ["официальные курсы валют", "ключевая ставка", "RUONIA"],
        },
        {
            "code": "nsd",
            "name": "НРД (Национальный расчётный депозитарий)",
            "url": "https://www.nsd.ru/",
            "access": "открытые сведения о выпусках; ленты по договору",
            "provides": [
                "график купонов",
                "амортизации",
                "оферты",
                "даты фиксации реестра",
            ],
            "note": (
                "НРД не публикует бесплатный API. Сведения о выпуске, раскрываемые "
                "через НРД, берутся из открытого зеркала MOEX ISS. Коммерческие ленты "
                "подключаются переменной TREASURY_NSD_API_KEY."
            ),
        },
    ]
