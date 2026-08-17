"""Временные ряды для графиков обзора рынка.

Каждый график описан один раз: какие показатели у него есть, какие включены
по умолчанию, в чём измеряются и как рисуются. Фронтенд строит переключатели
показателей и период по этому же описанию, а выгрузка в Excel — по тем же
данным, что видны на экране.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Bar, FxRate, Instrument, MacroRate

#: Период графика по умолчанию — 12 месяцев
DEFAULT_PERIOD_DAYS = 365


@dataclass(frozen=True)
class Metric:
    """Один показатель на графике."""

    code: str
    title: str
    #: line — линия по левой оси, bar — столбцы по правой
    kind: str = "line"
    unit: str = "%"
    digits: int = 2
    default: bool = False

    @property
    def axis(self) -> str:
        """Столбцы всегда уходят на правую ось: у них своя размерность."""
        return "right" if self.kind == "bar" else "left"


@dataclass(frozen=True)
class Chart:
    """Описание графика: заголовок, пояснение и набор показателей."""

    code: str
    title: str
    note: str
    metrics: tuple[Metric, ...]

    @property
    def defaults(self) -> tuple[str, ...]:
        return tuple(metric.code for metric in self.metrics if metric.default)


CHARTS: tuple[Chart, ...] = (
    Chart(
        code="rates",
        title="Ключевая ставка и RUONIA",
        note=(
            "Стоимость фондирования: ориентир для решения «размещать или занимать». "
            "Разброс ставок и число участников показывают, насколько RUONIA "
            "репрезентативна в конкретный день."
        ),
        metrics=(
            Metric("KEY_RATE", "Ключевая ставка", unit="%", default=True),
            Metric("RUONIA", "RUONIA", unit="%", default=True),
            Metric("RUONIA_MIN", "Минимальная ставка", unit="%"),
            Metric("RUONIA_P25", "25-й процентиль", unit="%"),
            Metric("RUONIA_P75", "75-й процентиль", unit="%"),
            Metric("RUONIA_MAX", "Максимальная ставка", unit="%"),
            Metric("RUONIA_VOLUME", "Объём сделок", kind="bar", unit="млрд ₽"),
            Metric("RUONIA_DEALS", "Количество сделок", kind="bar", unit="ед.", digits=0),
            Metric(
                "RUONIA_PARTICIPANTS",
                "Участников со сделками",
                kind="bar",
                unit="ед.",
                digits=0,
            ),
        ),
    ),
    Chart(
        code="imoex",
        title="Индекс МосБиржи",
        note=(
            "Главный индикатор рынка акций. Оборот показывает, "
            "подкреплено ли движение индекса деньгами."
        ),
        metrics=(
            Metric("close", "Значение на закрытие", unit="пунктов", default=True),
            Metric("open", "Значение на открытие", unit="пунктов"),
            Metric("high", "Максимум дня", unit="пунктов"),
            Metric("low", "Минимум дня", unit="пунктов"),
            Metric("turnover", "Оборот", kind="bar", unit="₽", digits=0),
        ),
    ),
    Chart(
        code="usd",
        title="USD / RUB",
        note="Официальный курс Банка России на дату.",
        metrics=(Metric("rate", "Курс ЦБ", unit="₽", digits=4, default=True),),
    ),
    Chart(
        code="eur",
        title="EUR / RUB",
        note="Официальный курс Банка России на дату.",
        metrics=(Metric("rate", "Курс ЦБ", unit="₽", digits=4, default=True),),
    ),
    Chart(
        code="cny",
        title="CNY / RUB",
        note="Официальный курс Банка России на дату.",
        metrics=(Metric("rate", "Курс ЦБ", unit="₽", digits=4, default=True),),
    ),
)

CHARTS_BY_CODE = {chart.code: chart for chart in CHARTS}

#: Какой инструмент и какая валюта стоят за графиком
_INDEX_SECID = {"imoex": "IMOEX"}
_FX_CODE = {"usd": "USD", "eur": "EUR", "cny": "CNY"}


def catalog() -> list[dict[str, Any]]:
    """Описание всех графиков для панели управления во фронтенде."""
    return [
        {
            "code": chart.code,
            "title": chart.title,
            "note": chart.note,
            "default_period_days": DEFAULT_PERIOD_DAYS,
            "metrics": [
                {
                    "code": metric.code,
                    "title": metric.title,
                    "kind": metric.kind,
                    "axis": metric.axis,
                    "unit": metric.unit,
                    "digits": metric.digits,
                    "default": metric.default,
                }
                for metric in chart.metrics
            ],
        }
        for chart in CHARTS
    ]


def resolve_period(
    date_from: date | None, date_to: date | None
) -> tuple[date, date]:
    """Границы периода: по умолчанию последние 12 месяцев."""
    end = date_to or date.today()
    start = date_from or (end - timedelta(days=DEFAULT_PERIOD_DAYS))
    if start > end:
        start, end = end, start
    return start, end


def _macro_points(
    session: Session, code: str, start: date, end: date
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(MacroRate.rate_date, MacroRate.value)
        .where(
            MacroRate.code == code,
            MacroRate.rate_date >= start,
            MacroRate.rate_date <= end,
        )
        .order_by(MacroRate.rate_date)
    ).all()
    return [{"date": row[0], "value": row[1]} for row in rows]


def _bar_points(
    session: Session, secid: str, field: str, start: date, end: date
) -> list[dict[str, Any]]:
    column = getattr(Bar, field)
    rows = session.execute(
        select(Bar.trade_date, column)
        .join(Instrument, Instrument.id == Bar.instrument_id)
        .where(
            Instrument.secid == secid,
            Bar.trade_date >= start,
            Bar.trade_date <= end,
            column.isnot(None),
        )
        .order_by(Bar.trade_date)
    ).all()
    return [{"date": row[0], "value": row[1]} for row in rows]


def _fx_points(
    session: Session, code: str, start: date, end: date
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(FxRate.rate_date, FxRate.value, FxRate.nominal)
        .where(
            FxRate.source == "cbr",
            FxRate.code == code,
            FxRate.rate_date >= start,
            FxRate.rate_date <= end,
        )
        .order_by(FxRate.rate_date)
    ).all()
    # Часть валют ЦБ котирует за 10 или 100 единиц — приводим к одной
    return [
        {"date": row[0], "value": row[1] / (row[2] or 1)}
        for row in rows
    ]


def _points_for(
    session: Session, chart: Chart, metric: Metric, start: date, end: date
) -> list[dict[str, Any]]:
    if chart.code == "rates":
        return _macro_points(session, metric.code, start, end)
    if chart.code in _INDEX_SECID:
        return _bar_points(session, _INDEX_SECID[chart.code], metric.code, start, end)
    if chart.code in _FX_CODE:
        return _fx_points(session, _FX_CODE[chart.code], start, end)
    return []


def build(
    session: Session,
    chart_code: str,
    metrics: Sequence[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    """Собрать данные графика за период по выбранным показателям."""
    chart = CHARTS_BY_CODE.get(chart_code)
    if chart is None:
        raise LookupError(f"Неизвестный график: {chart_code}")

    start, end = resolve_period(date_from, date_to)
    wanted = set(metrics) if metrics else set(chart.defaults)
    selected = [metric for metric in chart.metrics if metric.code in wanted]
    if not selected:
        selected = [metric for metric in chart.metrics if metric.default]

    return {
        "chart": chart.code,
        "title": chart.title,
        "note": chart.note,
        "date_from": start,
        "date_to": end,
        "series": [
            {
                "code": metric.code,
                "title": metric.title,
                "kind": metric.kind,
                "axis": metric.axis,
                "unit": metric.unit,
                "digits": metric.digits,
                "points": _points_for(session, chart, metric, start, end),
            }
            for metric in selected
        ],
    }


def to_table(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Развернуть ряды в таблицу «дата — показатели» для выгрузки в Excel."""
    columns: list[dict[str, Any]] = [
        {"code": "date", "title": "Дата", "kind": "date"}
    ]
    by_date: dict[date, dict[str, Any]] = {}

    for serie in data["series"]:
        title = serie["title"]
        if serie["unit"]:
            title = f"{title}, {serie['unit']}"
        columns.append(
            {
                "code": serie["code"],
                "title": title,
                "kind": "number",
                "digits": serie["digits"],
            }
        )
        for point in serie["points"]:
            by_date.setdefault(point["date"], {"date": point["date"]})[
                serie["code"]
            ] = point["value"]

    rows = [by_date[key] for key in sorted(by_date)]
    return columns, rows
