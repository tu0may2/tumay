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
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import Bar, Instrument, Quote
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
    #: Значение не из истории торгов, а рассчитывается отдельно и одинаково
    #: для всех строк бумаги — сворачивать за период его не нужно
    computed: bool = False
    #: Пояснение для интерфейса
    hint: str = ""


PARAMS: tuple[Param, ...] = (
    # Цены
    Param("wa_price", "Средневзвешенная цена", "Цены", "wa_price",
          digits=4, agg="minmax_avg", default=True),
    Param("close", "Цена закрытия", "Цены", "close", digits=4, agg="last", default=True),
    Param("open", "Цена открытия", "Цены", "open", digits=4, agg="first"),
    Param("high", "Максимум", "Цены", "high", digits=4, agg="max"),
    Param("low", "Минимум", "Цены", "low", digits=4, agg="min"),
    # Объёмы
    Param("volume", "Объём, шт", "Объёмы", "volume", digits=0, agg="sum", default=True),
    Param("turnover", "Оборот, ₽", "Объёмы", "turnover", digits=2, agg="sum", default=True),
    Param("num_trades", "Число сделок", "Объёмы", "num_trades", digits=0, agg="sum"),
    # Облигационные.
    #
    # Прежде здесь было три НКД: «на дату торгов» (как опубликовала биржа),
    # «на дату» (расчёт по графику купонов) и «на дату расчётов». Три близких
    # числа в соседних столбцах путали больше, чем помогали, — оставлен один,
    # тот, что относится к сделке: платит покупатель именно его.
    Param("accrued_settlement", "НКД на дату расчётов", "Облигации", "",
          digits=2, agg="last", computed=True, default=True,
          hint="НКД по сделке, заключённой в этот день: в режиме T+1 расчёты "
               "проходят следующим рабочим днём"),
    Param("yield_at_wap", "Доходность к СВЦ, %", "Облигации", "yield_at_wap", agg="avg"),
    Param("duration_days", "Дюрация, дней", "Облигации", "duration_days",
          digits=0, agg="last"),
    Param("face_value", "Номинал", "Облигации", "face_value", agg="last"),
    Param("coupon_base", "База купона", "Облигации", "",
          kind="text", agg="last", computed=True,
          hint="К чему привязан плавающий купон и какая надбавка: "
               "например «Ключевая ставка + 2,00%». У фиксированного купона пусто"),
    Param("coupon_benchmark_title", "Привязка к", "Облигации", "",
          kind="text", agg="last", computed=True,
          hint="Только база, без надбавки — так удобнее фильтровать в Excel"),
    Param("coupon_margin", "Надбавка к базе, п.п.", "Облигации", "",
          digits=2, agg="last", computed=True),
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
            {
                "code": param.code,
                "title": param.title,
                "default": param.default,
                "hint": param.hint,
            }
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
        # Расчётная величина одинакова для всех дней периода: сворачивать
        # её нечего, и подпись «последн.» была бы неправдой
        if param.kind == "text" or param.computed:
            columns.append(
                {
                    "code": param.code,
                    "title": param.title,
                    "kind": param.kind,
                    "digits": param.digits,
                }
            )
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


def _next_business_day(day: date) -> date:
    """Дата расчётов по сделке этого дня: режим T+1, выходные пропускаем.

    Биржевые праздники здесь не учитываются — их календарь открытый API не
    отдаёт, поэтому в праздничные дни дата расчётов может оказаться на день
    раньше фактической.
    """
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def _accrual_series(
    item: Resolved,
    dates: Sequence[date],
    codes: Sequence[str],
    bars: Sequence[dict[str, Any]] = (),
) -> dict[date, dict[str, Any]]:
    """НКД на каждую дату периода.

    Источников два, и они дополняют друг друга:

    * история торгов — там НКД датирован самим днём торгов и публикуется
      биржей, то есть за прошедшие торговые дни это точное значение;
    * расчёт по графику купонов — им закрываются выходные, праздники и
      сегодняшний день, которых в истории нет.

    Без первого источника у выпусков с плавающим купоном прошлые периоды
    оставались бы пустыми: ставка тех периодов нигде не объявлена, и
    восстановить её из одного лишь текущего среза невозможно.

    Значения отдаются в рублях расчётов — в тех же деньгах, в которых биржа
    публикует НКД в истории. Иначе у замещающего выпуска соседние колонки
    оказались бы в разных валютах: 1149 ₽ против 15 $.
    """
    wanted = {"accrued_today", "accrued_settlement"} & set(codes)
    if not wanted or item.kind != "bond" or not dates:
        return {}

    from .accrual import accrued_on
    from .fx import FxBook, instrument_currency

    with session_scope() as session:
        instrument = session.execute(
            select(Instrument).where(
                Instrument.secid == item.secid, Instrument.board == item.board
            )
        ).scalar_one_or_none()
        if instrument is None or instrument.kind != "bond":
            return {}

        # Биржевой НКД нужен выпускам с плавающим купоном: ставка периода у них
        # не объявлена, и величина купона восстанавливается из него
        quote = session.execute(
            select(Quote)
            .where(Quote.instrument_id == instrument.id)
            .order_by(Quote.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        exchange_value = quote.accrued_interest if quote else None
        settle_date = quote.settle_date if quote else None

        fx = FxBook(session)
        rate = fx.rate(instrument_currency(instrument)) or 1.0

        def _value(day: date) -> float | None:
            row = accrued_on(
                session, instrument, day,
                exchange_value=exchange_value, settle_date=settle_date,
            )
            if row is None:
                return None
            # Восстановленный из биржевого НКД купон уже в рублях расчётов
            multiplier = 1.0 if row.get("value_basis") == "settlement" else rate
            return round(row["value"] * multiplier, 2)

        # Биржевой НКД за прошедшие торговые дни — он точнее любого расчёта
        published = {
            bar["trade_date"]: bar["accrued_interest"]
            for bar in bars
            if bar.get("trade_date") and bar.get("accrued_interest") is not None
        }

        cache: dict[date, float | None] = {}

        def _cached(day: date) -> float | None:
            if day not in cache:
                value = published.get(day)
                cache[day] = round(value, 2) if value is not None else _value(day)
            return cache[day]

        # Дни без торгов внутри прошедшего купонного периода: у выпуска с
        # плавающим купоном ставка того периода нигде не объявлена, поэтому
        # расчёт их не закрывает. Но внутри периода НКД растёт линейно —
        # выходные восстанавливаются по соседним биржевым значениям
        known = sorted(published)
        for day in dates:
            if _cached(day) is not None:
                continue
            step = _daily_step(published, known, day)
            if step is None:
                continue
            base_day = max((d for d in known if d < day), default=None)
            if base_day is None:
                continue
            cache[day] = round(
                published[base_day] + step * (day - base_day).days, 2
            )

        return {
            day: {
                "accrued_today": _cached(day),
                "accrued_settlement": _cached(_next_business_day(day)),
            }
            for day in dates
        }


def _daily_step(
    published: dict[date, float], known: Sequence[date], day: date
) -> float | None:
    """Сколько НКД прибавляется за календарный день перед указанной датой.

    Считается по двум последним торговым дням до ``day``, и только если НКД
    между ними рос: падение означает выплату купона, а через границу периода
    приращение не переносится.
    """
    earlier = [item for item in known if item < day]
    if len(earlier) < 2:
        return None

    previous, last = earlier[-2], earlier[-1]
    if published[last] <= published[previous]:
        return None

    days = (last - previous).days
    return (published[last] - published[previous]) / days if days else None


def _reference_values(
    item: Resolved, dates: Sequence[date], codes: Sequence[str]
) -> dict[date, dict[str, Any]]:
    """Справочные поля выпуска, одинаковые во всех строках.

    База купона не зависит от даты, но кладётся в тот же словарь «дата →
    значения», что и НКД: так у ``build_rows`` остаётся один источник
    расчётных колонок вместо двух.
    """
    wanted = {"coupon_base", "coupon_benchmark_title", "coupon_margin"} & set(codes)
    if not wanted or item.kind != "bond" or not dates:
        return {}

    from .bonds import benchmark_title, coupon_base_title

    with session_scope() as session:
        instrument = session.execute(
            select(Instrument).where(
                Instrument.secid == item.secid, Instrument.board == item.board
            )
        ).scalar_one_or_none()
        if instrument is None:
            return {}

        values = {
            "coupon_base": coupon_base_title(
                instrument.coupon_benchmark, instrument.coupon_margin
            ),
            "coupon_benchmark_title": benchmark_title(instrument.coupon_benchmark),
            "coupon_margin": instrument.coupon_margin,
        }

    return {day: dict(values) for day in dates}


def date_range(date_from: date, date_to: date) -> list[date]:
    """Все календарные дни периода.

    Купон накапливается каждый день, поэтому в построчной выдаче нужны и
    выходные, и сегодняшний день, по которому итогов торгов ещё нет.
    """
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return [
        date_from + timedelta(days=offset)
        for offset in range((date_to - date_from).days + 1)
    ]


def build_rows(
    item: Resolved,
    bars: Sequence[dict[str, Any]],
    codes: Sequence[str],
    mode: str,
    computed: dict[date, dict[str, Any]] | None = None,
    dates: Sequence[date] | None = None,
) -> list[dict[str, Any]]:
    """Развернуть историю бумаги в строки результата.

    ``computed`` — значения, посчитанные не по истории торгов, а по графику
    купонов: словарь «дата → значения». ``dates`` задаёт полный период, чтобы
    в таблице были и дни без торгов: рыночные колонки там пустые, а НКД есть.
    """
    params = [PARAMS_BY_CODE[code] for code in codes if code in PARAMS_BY_CODE]
    computed = computed or {}

    if mode == "by_date":
        by_date = {bar["trade_date"]: bar for bar in bars}
        days = list(dates) if dates else sorted(by_date)

        rows = []
        for day in days:
            bar = by_date.get(day, {})
            row = _row_identity(item)
            row["trade_date"] = day
            #: Торгов в этот день не было — рыночные колонки пустые
            row["no_trades"] = day not in by_date
            for param in params:
                row[param.code] = (
                    computed.get(day, {}).get(param.code) if param.computed
                    else bar.get(param.field)
                )
            rows.append(row)
        return rows

    if not bars:
        return []

    # В своде расчётные величины берём на последний день периода
    last_day = max(computed) if computed else None

    row = _row_identity(item)
    row["days"] = len(bars)
    for param in params:
        if param.computed:
            row[param.code] = (
                computed.get(last_day, {}).get(param.code) if last_day else None
            )
            continue
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
    """Полный цикл: разобрать список → найти бумаги → загрузить → собрать таблицу.

    В построчной выдаче строка появляется на каждый день периода, а не только
    на дни с торгами: купон накапливается ежедневно, и НКД за выходные и за
    сегодня нужен так же, как за торговый день. Рыночные колонки в таких
    строках пустые — итогов торгов по ним нет.
    """
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
    period = date_range(date_from, date_to)

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

        # Считаем после _persist: бумага уже в справочнике, и по ней доступны
        # график купонов и последний срез
        accrual = _accrual_series(item, period, codes, bars)
        # Справочные поля кладём в тот же словарь: build_rows берёт все
        # расчётные колонки из одного места
        for day, values in _reference_values(item, period, codes).items():
            accrual.setdefault(day, {}).update(values)
        if not bars and not accrual:
            # Ни торгов, ни купонов — строки были бы пустыми
            continue

        rows.extend(build_rows(item, bars, codes, mode, accrual, dates=period))
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
        # «Торгов не было» и «итоги ещё не опубликованы» — разные вещи: за
        # сегодняшний день биржа отдаёт историю только назавтра, и назвать
        # это отсутствием торгов было бы неправдой
        warnings.append(
            "Итогов торгов за период нет: " + ", ".join(no_data)
            + ". За сегодняшний день биржа публикует их на следующий рабочий "
            "день, за выходные торгов не бывает. НКД в таких строках рассчитан."
        )
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
