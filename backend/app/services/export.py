"""Выгрузка данных по произвольному списку бумаг за период.

Пользователь вставляет список бумаг из Excel (ISIN или тикеры), задаёт период
и набор параметров — сервис находит бумаги на MOEX, догружает историю торгов и
собирает таблицу в одной из двух форм: построчно по датам (как таблица на сайте
MOEX) либо сводом за период.
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import Bar, Instrument
from ..sources import MoexSource
from ..sources.base import rows_to_dicts, to_date, to_float, to_int
from ..sources.moex import BOARD_SPECS
from .collector import _upsert

logger = logging.getLogger(__name__)

#: Разделители, которые встречаются при вставке из Excel и Word
_SPLIT_RE = re.compile(r"[\s,;|]+")
#: ISIN: 2 буквы страны, 9 буквенно-цифровых, контрольная цифра
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
#: Максимум бумаг за один запрос — защита от случайной вставки всего портфеля
MAX_SECURITIES = 300


# ----------------------------------------------------------------------
# Каталог параметров
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Param:
    """Параметр, который можно запросить по бумаге."""

    code: str
    title: str
    group: str
    #: Поле модели Bar, из которого берётся значение
    field: str
    #: number | date | text
    kind: str = "number"
    digits: int = 2
    #: Как сворачивать за период: avg | sum | last | minmax_avg
    agg: str = "avg"
    #: Показывать по умолчанию
    default: bool = False


PARAMS: tuple[Param, ...] = (
    # Цены
    Param("wa_price", "Средневзвешенная цена", "Цены", "wa_price",
          digits=4, agg="minmax_avg", default=True),
    Param("close", "Цена закрытия", "Цены", "close", digits=4, agg="last", default=True),
    Param("legal_close", "Офиц. цена закрытия", "Цены", "legal_close", digits=4, agg="last"),
    Param("open", "Цена открытия", "Цены", "open", digits=4, agg="first"),
    Param("high", "Максимум", "Цены", "high", digits=4, agg="max"),
    Param("low", "Минимум", "Цены", "low", digits=4, agg="min"),
    # Объёмы
    Param("volume", "Объём, шт", "Объёмы", "volume", digits=0, agg="sum", default=True),
    Param("turnover", "Оборот, ₽", "Объёмы", "turnover", digits=2, agg="sum", default=True),
    Param("num_trades", "Число сделок", "Объёмы", "num_trades", digits=0, agg="sum"),
    # Облигационные
    Param("accrued_interest", "НКД", "Облигации", "accrued_interest",
          digits=2, agg="last", default=True),
    Param("yield_close", "Доходность к закрытию, %", "Облигации", "yield_close", agg="avg"),
    Param("yield_at_wap", "Доходность к СВЦ, %", "Облигации", "yield_at_wap", agg="avg"),
    Param("duration_days", "Дюрация, дней", "Облигации", "duration_days",
          digits=0, agg="last"),
    Param("coupon_percent", "Купон, %", "Облигации", "coupon_percent", agg="last"),
    Param("face_value", "Номинал", "Облигации", "face_value", agg="last"),
    # Справочные
    Param("currency", "Валюта торгов", "Справка", "currency", kind="text", agg="last",
          default=True),
)

PARAMS_BY_CODE = {param.code: param for param in PARAMS}
#: Колонки, которые есть всегда — по ним бумага опознаётся
IDENTITY_COLUMNS = (
    {"code": "secid", "title": "Код бумаги", "kind": "text"},
    {"code": "isin", "title": "ISIN", "kind": "text"},
    {"code": "name", "title": "Наименование", "kind": "text"},
)


def parameter_catalog() -> list[dict[str, Any]]:
    """Список параметров для интерфейса, сгруппированный по разделам."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for param in PARAMS:
        groups.setdefault(param.group, []).append(
            {"code": param.code, "title": param.title, "default": param.default}
        )
    return [{"group": group, "items": items} for group, items in groups.items()]


# ----------------------------------------------------------------------
# Разбор списка бумаг
# ----------------------------------------------------------------------
def parse_identifiers(raw: str) -> list[str]:
    """Разобрать вставленный список: строки, табы, запятые, точки с запятой.

    Excel при копировании столбца отдаёт значения через перевод строки, при
    копировании строки — через табуляцию; поддерживаем оба случая.
    """
    if not raw:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for chunk in _SPLIT_RE.split(raw.strip().upper()):
        token = chunk.strip().strip('"\'')
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result[:MAX_SECURITIES]


def looks_like_isin(value: str) -> bool:
    return bool(_ISIN_RE.match(value))


# ----------------------------------------------------------------------
# Поиск бумаги на бирже
# ----------------------------------------------------------------------
@dataclass
class Resolved:
    """Найденная на бирже бумага."""

    query: str
    secid: str
    isin: str | None
    name: str
    board: str
    engine: str
    market: str
    kind: str
    currency: str | None = None
    face_value: float | None = None
    maturity_date: date | None = None


#: Соответствие типа бумаги MOEX нашему виду инструмента
_TYPE_TO_KIND = {
    "corporate_bond": "bond",
    "ofz_bond": "bond",
    "subfederal_bond": "bond",
    "municipal_bond": "bond",
    "exchange_bond": "bond",
    "euro_bond": "bond",
    "common_share": "share",
    "preferred_share": "share",
    "depositary_receipt": "share",
    "etf_ppif": "share",
    "public_ppif": "share",
    "private_ppif": "share",
    "stock_index": "index",
    "currency": "currency",
}


async def resolve_securities(
    moex: MoexSource, identifiers: Sequence[str]
) -> tuple[list[Resolved], list[str]]:
    """Найти бумаги по ISIN или тикеру. Возвращает найденные и ненайденные."""
    semaphore = asyncio.Semaphore(settings.http_concurrency)

    async def _one(identifier: str) -> Resolved | None:
        async with semaphore:
            return await _resolve_one(moex, identifier)

    outcomes = await asyncio.gather(
        *(_one(identifier) for identifier in identifiers), return_exceptions=True
    )

    found: list[Resolved] = []
    missing: list[str] = []
    for identifier, outcome in zip(identifiers, outcomes):
        if isinstance(outcome, Exception):
            logger.warning("Выгрузка: ошибка поиска %s: %s", identifier, outcome)
            missing.append(identifier)
        elif outcome is None:
            missing.append(identifier)
        else:
            found.append(outcome)
    return found, missing


async def _resolve_one(moex: MoexSource, identifier: str) -> Resolved | None:
    """Определить основную площадку и параметры бумаги."""
    payload = await moex.get_json(
        "/securities.json",
        **{"iss.meta": "off", "q": identifier, "limit": 10, "iss.only": "securities"},
    )
    candidates = rows_to_dicts(payload.get("securities"))
    if not candidates:
        return None

    # Точное совпадение по ISIN или тикеру важнее порядка выдачи поиска
    def _score(row: dict[str, Any]) -> tuple[int, int]:
        exact = (row.get("isin") or "").upper() == identifier or (
            row.get("secid") or ""
        ).upper() == identifier
        return (0 if exact else 1, 0 if row.get("is_traded") else 1)

    candidates.sort(key=_score)
    best = candidates[0]
    if _score(best)[0] != 0:
        # Нестрогое совпадение принимаем, только если запрос не похож на ISIN
        if looks_like_isin(identifier):
            return None

    secid = best.get("secid")
    if not secid:
        return None

    board = best.get("primary_boardid") or best.get("marketprice_boardid")
    engine, market = "stock", "bonds" if "bond" in (best.get("type") or "") else "shares"

    # Уточняем площадку и рынок в карточке бумаги — там они авторитетны
    detail = await moex.get_json(
        f"/securities/{secid}.json", **{"iss.meta": "off", "iss.only": "boards"}
    )
    boards = rows_to_dicts(detail.get("boards"))
    primary = next(
        (row for row in boards if row.get("is_primary") and row.get("is_traded")), None
    ) or next((row for row in boards if row.get("is_primary")), None)
    if primary:
        board = primary.get("boardid") or board
        engine = primary.get("engine") or engine
        market = primary.get("market") or market

    if not board:
        return None

    kind = _TYPE_TO_KIND.get(best.get("type") or "")
    if kind is None:
        kind = BOARD_SPECS.get(board, {}).get("kind", "share")

    return Resolved(
        query=identifier,
        secid=secid,
        isin=best.get("isin") or None,
        name=best.get("shortname") or best.get("name") or secid,
        board=board,
        engine=engine,
        market=market,
        kind=kind,
    )


# ----------------------------------------------------------------------
# Загрузка истории
# ----------------------------------------------------------------------
async def _fetch_history(
    moex: MoexSource, item: Resolved, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    """История по бумаге за период с её основной площадки."""
    path = (
        f"/history/engines/{item.engine}/markets/{item.market}"
        f"/boards/{item.board}/securities/{item.secid}.json"
    )
    from ..sources.moex import _map_bar, _PAGE_SIZE, _MAX_PAGES

    bars: list[dict[str, Any]] = []
    seen: set[date] = set()
    start = 0
    for _ in range(_MAX_PAGES):
        payload = await moex.get_json(
            path,
            **{
                "iss.meta": "off",
                "iss.only": "history",
                "from": date_from.isoformat(),
                "till": date_to.isoformat(),
                "start": start,
                "limit": _PAGE_SIZE,
            },
        )
        page = rows_to_dicts(payload.get("history"))
        if not page:
            break
        fresh = 0
        for row in page:
            bar = _map_bar(row)
            if bar["trade_date"] is None or bar["trade_date"] in seen:
                continue
            seen.add(bar["trade_date"])
            bars.append(bar)
            fresh += 1
        if fresh == 0 or len(page) < _PAGE_SIZE:
            break
        start += len(page)

    return sorted(bars, key=lambda bar: bar["trade_date"])


def _persist(item: Resolved, bars: Sequence[dict[str, Any]]) -> None:
    """Сохранить бумагу и её историю — следующий запрос будет быстрее."""
    if not bars:
        return
    with session_scope() as session:
        instrument = session.execute(
            select(Instrument).where(
                Instrument.secid == item.secid, Instrument.board == item.board
            )
        ).scalar_one_or_none()

        if instrument is None:
            instrument = Instrument(
                secid=item.secid,
                board=item.board,
                engine=item.engine,
                market=item.market,
                kind=item.kind,
                isin=item.isin,
                short_name=item.name,
            )
            session.add(instrument)
            session.flush()

        rows = [{"instrument_id": instrument.id, **bar} for bar in bars]
        _upsert(
            session,
            Bar,
            rows,
            ("instrument_id", "trade_date"),
            ("open", "high", "low", "close", "legal_close", "wa_price", "volume",
             "turnover", "num_trades", "accrued_interest", "yield_close",
             "yield_at_wap", "duration_days", "face_value", "coupon_percent",
             "currency"),
        )


# ----------------------------------------------------------------------
# Сборка таблицы
# ----------------------------------------------------------------------
_AGG_TITLES = {
    "avg": "средн.",
    "sum": "сумма",
    "last": "на конец",
    "first": "на начало",
    "min": "мин",
    "max": "макс",
}


def _aggregate(values: list[float], how: str) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    if how == "sum":
        return sum(clean)
    if how == "avg":
        return statistics.fmean(clean)
    if how == "min":
        return min(clean)
    if how == "max":
        return max(clean)
    if how == "first":
        return clean[0]
    return clean[-1]


def build_columns(codes: Sequence[str], mode: str) -> list[dict[str, Any]]:
    """Описание колонок результата для интерфейса и файла."""
    columns: list[dict[str, Any]] = [dict(col) for col in IDENTITY_COLUMNS]
    if mode == "by_date":
        columns.append({"code": "trade_date", "title": "Дата", "kind": "date"})
        for code in codes:
            param = PARAMS_BY_CODE.get(code)
            if param is None:
                continue
            columns.append(
                {
                    "code": param.code,
                    "title": param.title,
                    "kind": param.kind,
                    "digits": param.digits,
                }
            )
        return columns

    columns.append({"code": "days", "title": "Дней с торгами", "kind": "number", "digits": 0})
    for code in codes:
        param = PARAMS_BY_CODE.get(code)
        if param is None:
            continue
        if param.kind == "text":
            columns.append({"code": param.code, "title": param.title, "kind": "text"})
            continue
        if param.agg == "minmax_avg":
            for suffix, label in (("avg", "средн."), ("min", "мин"), ("max", "макс")):
                columns.append(
                    {
                        "code": f"{param.code}_{suffix}",
                        "title": f"{param.title}, {label}",
                        "kind": "number",
                        "digits": param.digits,
                    }
                )
        else:
            columns.append(
                {
                    "code": param.code,
                    "title": f"{param.title}, {_AGG_TITLES.get(param.agg, '')}".strip(", "),
                    "kind": "number",
                    "digits": param.digits,
                }
            )
    return columns


def _row_identity(item: Resolved) -> dict[str, Any]:
    return {"secid": item.secid, "isin": item.isin, "name": item.name}


def build_rows(
    item: Resolved, bars: Sequence[dict[str, Any]], codes: Sequence[str], mode: str
) -> list[dict[str, Any]]:
    """Развернуть историю бумаги в строки результата."""
    params = [PARAMS_BY_CODE[code] for code in codes if code in PARAMS_BY_CODE]

    if mode == "by_date":
        rows = []
        for bar in bars:
            row = _row_identity(item)
            row["trade_date"] = bar["trade_date"]
            for param in params:
                row[param.code] = bar.get(param.field)
            rows.append(row)
        return rows

    if not bars:
        return []

    row = _row_identity(item)
    row["days"] = len(bars)
    for param in params:
        values = [bar.get(param.field) for bar in bars]
        if param.kind == "text":
            text_values = [value for value in values if value]
            row[param.code] = text_values[-1] if text_values else None
            continue
        numbers = [value for value in values if isinstance(value, (int, float))]
        if param.agg == "minmax_avg":
            row[f"{param.code}_avg"] = _aggregate(numbers, "avg")
            row[f"{param.code}_min"] = _aggregate(numbers, "min")
            row[f"{param.code}_max"] = _aggregate(numbers, "max")
        else:
            row[param.code] = _aggregate(numbers, param.agg)
    return [row]


async def run_query(
    raw_identifiers: str,
    date_from: date,
    date_to: date,
    codes: Sequence[str],
    mode: str = "by_date",
) -> dict[str, Any]:
    """Полный цикл: разобрать список → найти бумаги → загрузить → собрать таблицу."""
    identifiers = parse_identifiers(raw_identifiers)
    if not identifiers:
        return {
            "columns": build_columns(codes, mode),
            "rows": [],
            "found": [],
            "missing": [],
            "warnings": ["Список бумаг пуст"],
        }

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    warnings: list[str] = []
    async with MoexSource() as moex:
        resolved, missing = await resolve_securities(moex, identifiers)

        semaphore = asyncio.Semaphore(settings.http_concurrency)

        async def _load(item: Resolved):
            async with semaphore:
                return item, await _fetch_history(moex, item, date_from, date_to)

        outcomes = await asyncio.gather(
            *(_load(item) for item in resolved), return_exceptions=True
        )

    rows: list[dict[str, Any]] = []
    found: list[dict[str, Any]] = []
    no_data: list[str] = []
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            logger.warning("Выгрузка: ошибка загрузки истории: %s", outcome)
            continue
        item, bars = outcome
        _persist(item, bars)
        if not bars:
            no_data.append(item.query)
            continue
        rows.extend(build_rows(item, bars, codes, mode))
        found.append(
            {
                "query": item.query,
                "secid": item.secid,
                "isin": item.isin,
                "name": item.name,
                "board": item.board,
                "kind": item.kind,
                "days": len(bars),
            }
        )

    if missing:
        warnings.append("Не найдены на бирже: " + ", ".join(missing))
    if no_data:
        warnings.append("Нет торгов за период: " + ", ".join(no_data))
    if len(identifiers) == MAX_SECURITIES:
        warnings.append(f"Список обрезан до {MAX_SECURITIES} бумаг")

    # По датам сортируем внутри бумаги, свод — по названию
    if mode == "by_date":
        rows.sort(key=lambda row: (row["secid"], row["trade_date"]))
    else:
        rows.sort(key=lambda row: row["secid"])

    return {
        "columns": build_columns(codes, mode),
        "rows": rows,
        "found": found,
        "missing": missing,
        "warnings": warnings,
        "date_from": date_from,
        "date_to": date_to,
        "mode": mode,
    }
