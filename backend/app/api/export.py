"""Запрос данных по списку бумаг и выгрузка в Excel/CSV."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from ..services import export as export_service
from ..services.tabular import to_csv, to_xlsx

router = APIRouter(prefix="/api/export", tags=["Выгрузка"])


class ExportQuery(BaseModel):
    """Запрос данных по списку бумаг за период."""

    securities: str = Field(
        ...,
        description="Список ISIN или тикеров: из Excel, через перевод строки или запятую",
    )
    date_from: date = Field(default_factory=lambda: date.today() - timedelta(days=30))
    date_to: date = Field(default_factory=date.today)
    parameters: list[str] = Field(
        default_factory=lambda: [
            param.code for param in export_service.PARAMS if param.default
        ],
        description="Коды параметров из /api/export/parameters",
    )
    mode: Literal["by_date", "summary"] = "by_date"

    @field_validator("parameters")
    @classmethod
    def _known_parameters(cls, value: list[str]) -> list[str]:
        unknown = [code for code in value if code not in export_service.PARAMS_BY_CODE]
        if unknown:
            raise ValueError(f"Неизвестные параметры: {', '.join(unknown)}")
        # Сохраняем порядок каталога — колонки не должны прыгать между запросами
        order = {param.code: index for index, param in enumerate(export_service.PARAMS)}
        return sorted(set(value), key=lambda code: order[code])


@router.get("/parameters", summary="Доступные параметры выгрузки")
def get_parameters() -> dict[str, Any]:
    """Каталог параметров для панели выбора."""
    return {
        "groups": export_service.parameter_catalog(),
        "identity": list(export_service.IDENTITY_COLUMNS),
        "max_securities": export_service.MAX_SECURITIES,
    }


@router.post("/preview", summary="Получить таблицу по списку бумаг")
async def preview(query: ExportQuery) -> dict[str, Any]:
    """Собрать таблицу: строки — бумаги (и даты), столбцы — выбранные параметры."""
    result = await export_service.run_query(
        query.securities,
        query.date_from,
        query.date_to,
        query.parameters,
        query.mode,
    )
    return result


@router.post("/download", summary="Скачать таблицу файлом")
async def download(query: ExportQuery, fmt: Literal["xlsx", "csv"] = "xlsx") -> Response:
    """Та же таблица, но файлом для Excel."""
    result = await export_service.run_query(
        query.securities,
        query.date_from,
        query.date_to,
        query.parameters,
        query.mode,
    )
    if not result["rows"]:
        raise HTTPException(
            status_code=404,
            detail="По заданным бумагам и периоду данных нет — выгружать нечего",
        )

    mode_title = "по датам" if query.mode == "by_date" else "свод"
    stem = (
        f"Выгрузка {mode_title} "
        f"{query.date_from:%d.%m.%Y}-{query.date_to:%d.%m.%Y}"
    )

    if fmt == "csv":
        content = to_csv(result["columns"], result["rows"])
        media_type = "text/csv; charset=utf-8"
        filename = f"{stem}.csv"
    else:
        content = to_xlsx(
            result["columns"],
            result["rows"],
            sheet_title="Данные",
            meta=[
                ("Период", f"{query.date_from:%d.%m.%Y} — {query.date_to:%d.%m.%Y}"),
                ("Форма", "Построчно по датам" if query.mode == "by_date" else "Свод за период"),
                ("Бумаг", str(len(result["found"]))),
                ("Источник", "Московская биржа (ISS)"),
                ("Сформировано", date.today().strftime("%d.%m.%Y")),
            ],
        )
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        filename = f"{stem}.xlsx"

    # Кириллица в имени файла требует filename* (RFC 5987)
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )
