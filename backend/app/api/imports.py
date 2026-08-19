"""Импорт сделок из файла и сверка позиций с выпиской."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_session
from ..schemas import ImportApply, PortfolioImportApply
from ..services import importer as importer_service
from ..services import portfolio_import as portfolio_import_service
from ..services.auth import audit, require_trader

router = APIRouter(prefix="/api/import", tags=["Импорт"])

#: Больше брокерский отчёт за квартал обычно не весит; ограничение защищает
#: память сервера от случайно выбранного архива
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Файл пуст")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ",
        )
    return content


@router.post("/preview", summary="Разобрать файл со сделками")
async def preview(
    file: UploadFile = File(..., description="Отчёт брокера: .xlsx или .csv"),
    portfolio: str | None = Form(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> dict[str, Any]:
    """Показать, что будет загружено, до записи в журнал.

    Колонки распознаются по заголовкам, поэтому предпросмотр обязателен:
    в нём видно, какое поле файла во что превратилось и где не хватило данных.
    """
    content = await _read_upload(file)
    try:
        return importer_service.preview_import(
            session, content, file.filename or "", portfolio=portfolio
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/apply", summary="Записать разобранные сделки")
def apply(
    payload: ImportApply,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> dict[str, Any]:
    """Записать в журнал строки, подтверждённые в предпросмотре."""
    result = importer_service.apply_import(session, payload.deals)
    audit(
        session,
        user,
        action="import",
        entity="deal",
        detail=f"загружено {result['created']}, пропущено {result['skipped_count']}",
    )
    return result


@router.post("/reconcile", summary="Сверка с выпиской депозитария")
async def reconcile(
    file: UploadFile = File(..., description="Выписка: код бумаги и количество"),
    portfolio: str | None = Form(None),
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> dict[str, Any]:
    """Сопоставить позиции терминала с внешней выпиской."""
    content = await _read_upload(file)
    try:
        return importer_service.reconcile(
            session, content, file.filename or "", portfolio=portfolio
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/portfolio/template", summary="Скачать шаблон книги портфеля")
def portfolio_template() -> Response:
    """Пустая книга с листами «Портфели», «Остатки» и «Сделки».

    Отдаём образец файлом, а не описанием в документации: заполнить готовые
    столбцы можно сразу, а сверять свою таблицу с текстом инструкции — долго
    и с ошибками.
    """
    content = portfolio_import_service.build_template()
    filename = "Шаблон портфеля.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/portfolio/preview", summary="Разобрать книгу портфеля")
async def preview_portfolio(
    file: UploadFile = File(..., description="Книга Excel с составом портфеля"),
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> dict[str, Any]:
    """Показать содержимое книги до записи, вместе с предупреждениями.

    Предупреждения здесь важнее ошибок: главный риск импорта остатков — не
    отказ разбора, а молчаливое задвоение позиции поверх уже загруженных
    сделок.
    """
    content = await _read_upload(file)
    try:
        return portfolio_import_service.preview(session, content, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/portfolio/apply", summary="Записать книгу портфеля")
def apply_portfolio(
    payload: PortfolioImportApply,
    session: Session = Depends(get_session),
    user: dict = Depends(require_trader),
) -> dict[str, Any]:
    """Записать портфели, остатки и сделки, подтверждённые в предпросмотре."""
    result = portfolio_import_service.apply(
        session,
        payload.model_dump(),
        replace_existing=payload.replace_existing,
    )
    audit(
        session,
        user,
        action="import",
        entity="portfolio",
        detail=(
            f"портфелей {result['portfolios']}, остатков {result['holdings']}, "
            f"сделок {result['deals']}, удалено прежних {result['removed']}"
        ),
    )
    return result


@router.get("/columns", summary="Какие заголовки распознаются")
def known_columns() -> list[dict[str, Any]]:
    """Подсказка для пользователя: как назвать колонки, чтобы файл распознался."""
    titles = {
        "secid": "Код бумаги",
        "isin": "ISIN",
        "side": "Направление",
        "quantity": "Количество",
        "price": "Цена",
        "accrued_interest": "НКД",
        "fee": "Комиссия",
        "trade_date": "Дата сделки",
        "portfolio": "Портфель",
        "counterparty": "Контрагент",
    }
    required = {"secid", "quantity", "price"}
    return [
        {
            "code": code,
            "title": titles.get(code, code),
            "required": code in required,
            "hints": list(hints),
        }
        for code, hints in importer_service.COLUMN_HINTS.items()
    ]
