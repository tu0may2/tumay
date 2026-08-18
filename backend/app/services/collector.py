"""Сбор данных из внешних источников в хранилище.

Каждый запуск фиксируется в ``collection_runs``: видно, что собрано, когда и
с какой ошибкой. Сбой одного источника не останавливает остальные.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import (
    Bar,
    Deal,
    CollectionRun,
    CorpAction,
    CurvePoint,
    FxRate,
    Instrument,
    MacroRate,
    Quote,
    RateMeeting,
    WatchItem,
)
from ..sources import CbrSource, MoexSource, NsdSource
from ..sources.base import rows_to_dicts
from .analytics import latest_quote_map

logger = logging.getLogger(__name__)

#: Типы выпусков, где купон известен из среза и карточка не нужна
_FIXED_COUPON_TYPES = ("Фикс с известным купоном", "Дисконтная облигация")


def _to_float(value: Any) -> float | None:
    """Мягкое приведение: у карточки биржи поля бывают пустыми строками."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


#: Поля справочника, которые обновляем при каждом сборе
_INSTRUMENT_FIELDS = (
    "isin",
    "short_name",
    "full_name",
    "reg_number",
    "currency",
    "lot_size",
    "face_value",
    "face_unit",
    "issue_size",
    "list_level",
    "sector",
    "sec_type",
    "maturity_date",
    "offer_date",
    "coupon_percent",
    "coupon_value",
    "coupon_period",
    "next_coupon_date",
    "accrued_interest",
    "bond_type",
)


# ----------------------------------------------------------------------
# Универсальный upsert
# ----------------------------------------------------------------------
def _upsert(
    session: Session,
    model: type,
    rows: Sequence[dict[str, Any]],
    index_elements: Sequence[str],
    update_columns: Sequence[str] | None = None,
) -> int:
    """Вставка с разрешением конфликта по уникальному ключу.

    На SQLite и PostgreSQL используем ``ON CONFLICT``; на прочих диалектах
    откатываемся на построчную вставку с пропуском дублей.
    """
    if not rows:
        return 0

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "sqlite":
        statement = sqlite_insert(model)
    elif dialect == "postgresql":
        statement = pg_insert(model)
    else:
        return _upsert_fallback(session, model, rows)

    if update_columns:
        statement = statement.on_conflict_do_update(
            index_elements=list(index_elements),
            set_={col: getattr(statement.excluded, col) for col in update_columns},
        )
    else:
        statement = statement.on_conflict_do_nothing(index_elements=list(index_elements))

    # Порциями, чтобы не упереться в лимит переменных SQLite (999 по умолчанию)
    written = 0
    for chunk in _chunks(rows, 200):
        session.execute(statement, chunk)
        written += len(chunk)
    return written


def _upsert_fallback(session: Session, model: type, rows: Sequence[dict]) -> int:
    written = 0
    for row in rows:
        try:
            with session.begin_nested():
                session.execute(model.__table__.insert().values(**row))
            written += 1
        except Exception:  # noqa: BLE001 — дубликат ключа, строку пропускаем
            continue
    return written


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ----------------------------------------------------------------------
# Сборщик
# ----------------------------------------------------------------------
class Collector:
    """Оркестратор сбора: источники → нормализация → хранилище."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @contextmanager
    def _run(self, source: str, task: str) -> Iterator[dict[str, int]]:
        """Записать запуск в журнал и зафиксировать итог."""
        started = datetime.utcnow()
        counter = {"rows": 0}
        with session_scope() as session:
            run = CollectionRun(
                source=source, task=task, status="running", started_at=started
            )
            session.add(run)
            session.flush()
            run_id = run.id

        status, error = "success", None
        try:
            yield counter
        except Exception as exc:  # noqa: BLE001 — журналируем и пробрасываем
            status, error = "error", f"{type(exc).__name__}: {exc}"
            logger.exception("Сбор %s/%s завершился ошибкой", source, task)
            raise
        finally:
            finished = datetime.utcnow()
            with session_scope() as session:
                run = session.get(CollectionRun, run_id)
                if run is not None:
                    run.status = status
                    run.finished_at = finished
                    run.duration_sec = (finished - started).total_seconds()
                    run.rows = counter["rows"]
                    run.error = error

    # ------------------------------------------------------------------
    async def collect_quotes(self, boards: Sequence[str] | None = None) -> int:
        """Снять текущий рыночный срез по площадкам и сохранить его."""
        boards = list(boards or _default_boards())
        with self._run("moex", "quotes") as counter:
            async with MoexSource() as moex:
                rows = await moex.fetch_boards(boards)
            counter["rows"] = _store_snapshot(rows)
            return counter["rows"]

    async def collect_reference(self) -> int:
        """Курсы ЦБ, ключевая ставка, RUONIA и кривая бескупонной доходности.

        Ряды тянем сразу за период ``macro_history_days``: графики обзора
        рынка строятся по истории, а она иначе накапливалась бы по одному дню
        за запуск и первый год выглядела бы пустой.
        """
        total = 0
        depth = timedelta(days=settings.macro_history_days)
        since = date.today() - depth

        with self._run("cbr", "reference") as counter:
            async with CbrSource() as cbr:
                fx, key_rate, ruonia, ruonia_details, currency_ids = await asyncio.gather(
                    cbr.fetch_fx_rates(),
                    cbr.fetch_key_rate(since),
                    cbr.fetch_ruonia(since),
                    cbr.fetch_ruonia_details(since),
                    cbr.fetch_currency_ids(),
                    return_exceptions=True,
                )

                fx_history: list[dict[str, Any]] = []
                if not isinstance(currency_ids, Exception):
                    histories = await asyncio.gather(
                        *(
                            cbr.fetch_fx_history(code, currency_ids[code], since)
                            for code in settings.fx_history_codes
                            if code in currency_ids
                        ),
                        return_exceptions=True,
                    )
                    fx_history = [
                        row
                        for outcome in histories
                        if not isinstance(outcome, Exception)
                        for row in outcome
                    ]

            with session_scope() as session:
                rates = [
                    row
                    for series in (fx, fx_history)
                    if not isinstance(series, Exception)
                    for row in series
                ]
                total += _upsert(
                    session, FxRate, rates, ("source", "code", "rate_date"), ("value",)
                )
                # Подробности RUONIA идут последними: там та же ставка, что и в
                # SOAP-ряду, и при совпадении дат побеждает более полный источник
                macro = [
                    row
                    for series in (key_rate, ruonia, ruonia_details)
                    if not isinstance(series, Exception)
                    for row in series
                ]
                total += _upsert(
                    session, MacroRate, macro, ("code", "rate_date"), ("value",)
                )
            counter["rows"] = total

        with self._run("moex", "curve") as counter:
            async with MoexSource() as moex:
                curve = await moex.fetch_yield_curve()
            points = [
                {
                    "curve_date": curve["curve_date"],
                    "period_years": point["period_years"],
                    "value": point["value"],
                }
                for point in curve["points"]
            ]
            with session_scope() as session:
                counter["rows"] = _upsert(
                    session, CurvePoint, points, ("curve_date", "period_years"), ("value",)
                )
            total += counter["rows"]

        return total

    async def collect_history(
        self,
        secids: Sequence[str] | None = None,
        *,
        days: int | None = None,
        top_by_turnover: int = 60,
    ) -> int:
        """История торгов по списку бумаг либо по самым ликвидным из среза."""
        days = days or settings.history_depth_days
        start_date = date.today() - timedelta(days=days)
        end_date = date.today()

        with session_scope() as session:
            targets = _history_targets(session, secids, top_by_turnover)

        if not targets:
            logger.info("История: нет подходящих инструментов для загрузки")
            return 0

        with self._run("moex", "history") as counter:
            written = 0
            async with MoexSource() as moex:
                semaphore = asyncio.Semaphore(settings.http_concurrency)

                async def _one(instrument_id: int, secid: str, board: str):
                    async with semaphore:
                        return instrument_id, await moex.fetch_history(
                            board, secid, start_date, end_date
                        )

                results = await asyncio.gather(
                    *(_one(*target) for target in targets), return_exceptions=True
                )

            with session_scope() as session:
                for outcome in results:
                    if isinstance(outcome, Exception):
                        logger.warning("История: ошибка загрузки (%s)", outcome)
                        continue
                    instrument_id, bars = outcome
                    rows = [{"instrument_id": instrument_id, **bar} for bar in bars]
                    written += _upsert(
                        session,
                        Bar,
                        rows,
                        ("instrument_id", "trade_date"),
                        ("open", "high", "low", "close", "wa_price", "volume",
                         "turnover", "num_trades"),
                    )
            counter["rows"] = written
            return written

    async def collect_corp_actions(self, limit: int = 40) -> int:
        """Купоны, амортизации и оферты (данные НРД) по облигациям в обращении."""
        with session_scope() as session:
            securities = _bond_targets(session, limit)

        if not securities:
            return 0

        with self._run("nsd", "corp_actions") as counter:
            async with MoexSource() as moex:
                nsd = NsdSource(moex)
                actions = await nsd.fetch_cashflows_bulk(
                    securities, concurrency=settings.http_concurrency
                )
            with session_scope() as session:
                counter["rows"] = _upsert(
                    session,
                    CorpAction,
                    actions,
                    ("isin", "action_type", "action_date"),
                    ("value", "value_rub", "value_pct", "record_date", "name"),
                )
            return counter["rows"]

    async def collect_benchmarks(self) -> int:
        """История индексов-ориентиров: нужна для сравнения портфеля."""
        from .benchmark import BENCHMARKS

        start_date = date.today() - timedelta(days=settings.history_depth_days)
        end_date = date.today()

        with self._run("moex", "benchmarks") as counter:
            written = 0
            async with MoexSource() as moex:
                for secid, _ in BENCHMARKS:
                    try:
                        bars = await moex.fetch_history(
                            settings.index_board, secid, start_date, end_date
                        )
                    except Exception as exc:  # noqa: BLE001 — индекс не критичен
                        logger.warning("Индекс %s недоступен: %s", secid, exc)
                        continue
                    if not bars:
                        continue

                    with session_scope() as session:
                        instrument = session.execute(
                            select(Instrument).where(
                                Instrument.secid == secid,
                                Instrument.board == settings.index_board,
                            )
                        ).scalar_one_or_none()
                        if instrument is None:
                            instrument = Instrument(
                                secid=secid,
                                board=settings.index_board,
                                engine="stock",
                                market="index",
                                kind="index",
                                short_name=secid,
                            )
                            session.add(instrument)
                            session.flush()

                        rows = [{"instrument_id": instrument.id, **bar} for bar in bars]
                        written += _upsert(
                            session,
                            Bar,
                            rows,
                            ("instrument_id", "trade_date"),
                            ("close", "wa_price", "volume", "turnover", "yield_close",
                             "duration_days"),
                        )
            counter["rows"] = written
            return written

    async def collect_issuers(self, limit: int = 60) -> int:
        """Эмитенты бумаг из портфеля — основа лимита на заёмщика.

        Справочник эмитентов отдаётся по одной бумаге за запрос, поэтому
        заполняем его точечно: только для того, что реально в портфеле.
        """
        with session_scope() as session:
            secids = [
                row[0]
                for row in session.execute(
                    select(Deal.secid).distinct()
                ).all()
            ]
            if not secids:
                return 0
            pending = [
                row[0]
                for row in session.execute(
                    select(Instrument.secid).where(
                        Instrument.secid.in_(secids), Instrument.issuer.is_(None)
                    )
                ).all()
            ][:limit]

        if not pending:
            return 0

        with self._run("moex", "issuers") as counter:
            found: dict[str, tuple[str, str | None]] = {}
            async with MoexSource() as moex:
                semaphore = asyncio.Semaphore(settings.http_concurrency)

                async def _one(secid: str) -> None:
                    async with semaphore:
                        try:
                            payload = await moex.get_json(
                                "/securities.json",
                                **{"iss.meta": "off", "q": secid, "limit": 5,
                                   "iss.only": "securities"},
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Эмитент %s не получен: %s", secid, exc)
                            return
                        for row in rows_to_dicts(payload.get("securities")):
                            if (row.get("secid") or "").upper() == secid.upper():
                                title = row.get("emitent_title")
                                if title:
                                    found[secid] = (title, row.get("emitent_inn"))
                                return

                await asyncio.gather(*(_one(secid) for secid in pending))

            with session_scope() as session:
                for secid, (title, inn) in found.items():
                    for instrument in session.execute(
                        select(Instrument).where(Instrument.secid == secid)
                    ).scalars():
                        instrument.issuer = title
                        instrument.issuer_inn = inn
            counter["rows"] = len(found)
            return len(found)

    async def collect_rate_calendar(self) -> int:
        """Календарь заседаний ЦБ по ключевой ставке.

        Публикуется на год вперёд и меняется редко, поэтому обновляется
        вместе с остальными справочниками, а не по отдельному расписанию.
        """
        with self._run("cbr", "rate_calendar") as counter:
            async with CbrSource() as cbr:
                events = await cbr.fetch_rate_calendar()

            if not events:
                return 0

            rows = [
                {
                    "meeting_date": event["meeting_date"],
                    "title": event["title"],
                    "kind": event["kind"],
                    "with_forecast": event["with_forecast"],
                    "links": json.dumps(event["links"], ensure_ascii=False),
                }
                for event in events
            ]
            with session_scope() as session:
                counter["rows"] = _upsert(
                    session, RateMeeting, rows,
                    ("meeting_date", "title"),
                    ("kind", "with_forecast", "links"),
                )
            return counter["rows"]

    async def collect_coupon_benchmarks(self, limit: int | None = None) -> int:
        """К чему привязан купон флоатеров — по одной карточке за запрос.

        В биржевом срезе базы купона нет, а карточка отдаётся по одной
        бумаге, поэтому справочник заполняется порциями: за один заход
        берём ограниченное число выпусков, начиная с портфельных и
        оборотистых. Через несколько циклов сбора рынок закрыт целиком,
        а биржу мы при этом не заваливаем.

        Повторно карточку не запрашиваем: база купона задана при выпуске и
        не меняется. Помечаем дату проверки, чтобы не ходить по кругу за
        теми, у кого базы нет вовсе (у фиксированного купона её и не будет).
        """
        limit = limit or settings.benchmark_batch_size
        with session_scope() as session:
            pending = [
                row[0]
                for row in session.execute(
                    select(Instrument.secid)
                    .outerjoin(Quote, Quote.instrument_id == Instrument.id)
                    .where(
                        Instrument.kind == "bond",
                        Instrument.benchmark_checked_at.is_(None),
                        # Фиксированный купон известен из среза — карточка не нужна
                        Instrument.bond_type.notin_(_FIXED_COUPON_TYPES),
                    )
                    .group_by(Instrument.secid)
                    # Сначала то, чем торгуют: по этим выпускам данные нужнее
                    .order_by(func.max(Quote.turnover).desc().nullslast())
                    .limit(limit)
                ).all()
            ]

        if not pending:
            return 0

        with self._run("moex", "benchmarks") as counter:
            cards: dict[str, dict[str, Any]] = {}
            async with MoexSource() as moex:
                semaphore = asyncio.Semaphore(settings.http_concurrency)

                async def _one(secid: str) -> None:
                    async with semaphore:
                        try:
                            cards[secid] = await moex.fetch_security_card(secid)
                        except Exception as exc:  # noqa: BLE001 — один выпуск не важен
                            logger.warning("Карточка %s не получена: %s", secid, exc)

                await asyncio.gather(*(_one(secid) for secid in pending))

            checked = datetime.utcnow()
            filled = 0
            with session_scope() as session:
                for secid in pending:
                    card = cards.get(secid)
                    instruments = list(
                        session.execute(
                            select(Instrument).where(Instrument.secid == secid)
                        ).scalars()
                    )
                    for instrument in instruments:
                        if card is None:
                            # Не достучались — оставляем неотмеченным,
                            # чтобы попробовать в следующий раз
                            continue
                        benchmark = (card.get("COUPON_BENCHMARK") or "").strip() or None
                        instrument.coupon_benchmark = benchmark
                        instrument.coupon_margin = _to_float(
                            card.get("COUPON_BENCHMARK_SPREAD")
                        )
                        instrument.bond_subtype = card.get("BOND_SUBTYPE") or None
                        instrument.benchmark_checked_at = checked
                    if card is not None and instruments:
                        filled += 1

            counter["rows"] = filled
            return filled

    def prune_quotes(self, keep_days: int | None = None) -> int:
        """Убрать внутридневные срезы старше срока хранения.

        Срез снимается каждые пять минут по всем бумагам — это около сорока
        тысяч строк в день. Таблица растёт без предела, а почти каждая
        страница терминала считает по ней «последнюю котировку по каждой
        бумаге», то есть проходит её целиком: чем она больше, тем медленнее
        работает всё.

        Дневная история при этом не теряется — она хранится в таблице баров
        отдельно и живёт своей жизнью. Удаляются только промежуточные срезы
        внутри дня, и то лишь давние.
        """
        # Ноль — осмысленное значение «не удалять ничего», поэтому подстановка
        # умолчания только при None: keep_days or ... подменил бы ноль семёркой
        if keep_days is None:
            keep_days = settings.quote_retention_days
        if keep_days <= 0:
            return 0

        cutoff = datetime.utcnow() - timedelta(days=keep_days)
        batch = settings.quote_prune_batch
        limit = settings.quote_prune_max_rows
        removed = 0

        # Удаляем порциями с отдельной транзакцией на каждую. Одним запросом
        # это выглядит короче, но на сервере с медленным диском удаление
        # сотен тысяч строк держит долгую транзакцию и разрастается журнал —
        # терминал в это время не отвечает. Порциями диск успевает разгрестись
        # между заходами, а незавершённая уборка просто продолжится в
        # следующем цикле обслуживания.
        while removed < limit:
            with session_scope() as session:
                # Последний срез по каждой бумаге не трогаем ни при каких
                # условиях: по неликвидным выпускам он может быть
                # единственным, и без него бумага пропадёт из витрин.
                #
                # Свежесть определяем по метке времени, а не по номеру записи:
                # они совпадают, только пока строки пишутся строго по порядку,
                # а после ручной загрузки истории или переноса базы — уже нет
                latest = (
                    select(
                        Quote.instrument_id.label("instrument_id"),
                        func.max(Quote.ts).label("ts"),
                    )
                    .group_by(Quote.instrument_id)
                    .subquery()
                )
                stale = [
                    row[0]
                    for row in session.execute(
                        select(Quote.id)
                        .join(latest, latest.c.instrument_id == Quote.instrument_id)
                        .where(Quote.ts < cutoff, Quote.ts < latest.c.ts)
                        .limit(min(batch, limit - removed))
                    ).all()
                ]
                if not stale:
                    break
                session.execute(delete(Quote).where(Quote.id.in_(stale)))
                removed += len(stale)

        if removed:
            logger.info("Очистка: удалено %s устаревших срезов котировок", removed)
        return removed

    async def collect_all(self, *, with_history: bool = True) -> dict[str, int]:
        """Полный цикл сбора. Ошибка одного шага не отменяет остальные."""
        async with self._lock:
            summary: dict[str, int] = {}
            steps: list[tuple[str, Any]] = [
                ("quotes", self.collect_quotes()),
                ("reference", self.collect_reference()),
            ]
            if with_history:
                steps.append(("history", self.collect_history()))
                steps.append(("benchmarks", self.collect_benchmarks()))
            steps.append(("corp_actions", self.collect_corp_actions()))
            steps.append(("issuers", self.collect_issuers()))
            # База купона флоатеров: добирается порциями, полное покрытие
            # набирается за несколько циклов
            steps.append(("coupon_benchmarks", self.collect_coupon_benchmarks()))
            steps.append(("rate_calendar", self.collect_rate_calendar()))

            for name, coro in steps:
                try:
                    summary[name] = await coro
                except Exception as exc:  # noqa: BLE001 — шаг уже в журнале
                    logger.error("Шаг сбора %s не выполнен: %s", name, exc)
                    summary[name] = 0
            return summary


# ----------------------------------------------------------------------
# Запись среза
# ----------------------------------------------------------------------
def _store_snapshot(rows: Sequence[dict[str, Any]]) -> int:
    """Обновить справочник инструментов и добавить котировки."""
    if not rows:
        return 0

    with session_scope() as session:
        existing = {
            (inst.secid, inst.board): inst
            for inst in session.execute(select(Instrument)).scalars()
        }

        new_instruments: list[Instrument] = []
        for row in rows:
            data = row["instrument"]
            key = (data["secid"], data["board"])
            current = existing.get(key)
            if current is None:
                instrument = Instrument(**data)
                session.add(instrument)
                existing[key] = instrument
                new_instruments.append(instrument)
            else:
                for field in _INSTRUMENT_FIELDS:
                    value = data.get(field)
                    if value is not None:
                        setattr(current, field, value)

        # id новых записей нужны для привязки котировок
        session.flush()

        quotes: list[dict[str, Any]] = []
        for row in rows:
            data = row["instrument"]
            instrument = existing[(data["secid"], data["board"])]
            quote = row["quote"]
            # Пустые срезы (бумага без торгов и без цены) не храним
            if quote.get("last") is None and quote.get("turnover") is None:
                continue
            quotes.append({"instrument_id": instrument.id, **quote})

        return _upsert(session, Quote, quotes, ("instrument_id", "ts"))


def _default_boards() -> list[str]:
    return [
        settings.shares_board,
        *settings.bonds_boards,
        settings.index_board,
        settings.fx_board,
    ]


def _history_targets(
    session: Session, secids: Sequence[str] | None, top_by_turnover: int
) -> list[tuple[int, str, str]]:
    """Выбрать инструменты для загрузки истории."""
    if secids:
        statement = select(
            Instrument.id, Instrument.secid, Instrument.board
        ).where(Instrument.secid.in_(list(secids)))
        return [tuple(row) for row in session.execute(statement).all()]

    # Иначе — самые оборотистые бумаги последнего среза.
    # Площадки снимаются параллельно и получают разные ts, поэтому единого
    # «последнего ts» нет: берём последнюю котировку по каждому инструменту.
    #
    # Акции и облигации отбираем отдельными квотами: по обороту акции
    # вытесняют облигации почти полностью, и тогда по облигациям не с чем
    # считать историю премии и доходности.
    latest = latest_quote_map(session)

    def _top(kind: str, limit: int) -> list[tuple[int, str, str]]:
        statement = (
            select(Instrument.id, Instrument.secid, Instrument.board)
            .join(latest, latest.c.instrument_id == Instrument.id)
            .join(Quote, Quote.id == latest.c.quote_id)
            .where(
                Quote.turnover.isnot(None),
                Quote.turnover > 0,
                Instrument.kind == kind,
            )
            .order_by(Quote.turnover.desc())
            .limit(limit)
        )
        return [tuple(row) for row in session.execute(statement).all()]

    half = max(1, top_by_turnover // 2)
    targets = _top("share", half) + _top("bond", top_by_turnover - half)

    # Свои бумаги и наблюдаемые нужны всегда, даже если они неликвидны
    watched = session.execute(
        select(Instrument.id, Instrument.secid, Instrument.board).where(
            Instrument.secid.in_(
                select(Deal.secid).distinct().union(select(WatchItem.secid).distinct())
            )
        )
    ).all()

    # У индексов нет оборота, поэтому в отбор по ликвидности они не попадают —
    # а без их истории не построить график «Индекс МосБиржи» в обзоре рынка
    indices = session.execute(
        select(Instrument.id, Instrument.secid, Instrument.board).where(
            Instrument.secid.in_(list(settings.tracked_indices))
        )
    ).all()

    seen: set[int] = set()
    result: list[tuple[int, str, str]] = []
    for row in [tuple(item) for item in watched + indices] + targets:
        if row[0] in seen:
            continue
        seen.add(row[0])
        result.append(row)
    return result


def _bond_targets(session: Session, limit: int) -> list[tuple[str, str]]:
    """Облигации с ISIN, по которым имеет смысл тянуть график выплат.

    Бумаги из портфеля берём всегда: без их графика не посчитать ни купонный
    доход, ни календарь поступлений, а по обороту они могут не попасть в
    список ликвидных.
    """
    owned = [
        (isin, secid)
        for isin, secid in session.execute(
            select(Instrument.isin, Instrument.secid)
            .where(
                Instrument.kind == "bond",
                Instrument.isin.isnot(None),
                Instrument.secid.in_(select(Deal.secid).distinct()),
            )
            .distinct()
        ).all()
        if isin
    ]

    has_quotes = session.execute(select(func.count()).select_from(Quote)).scalar()
    statement = select(Instrument.isin, Instrument.secid).where(
        Instrument.kind == "bond", Instrument.isin.isnot(None)
    )
    if has_quotes:
        # Приоритет — торгуемым выпускам: по ним график выплат реально нужен
        latest = latest_quote_map(session)
        statement = (
            statement.join(latest, latest.c.instrument_id == Instrument.id)
            .join(Quote, Quote.id == latest.c.quote_id)
            .order_by(Quote.turnover.desc().nullslast())
        )
    liquid = [
        (isin, secid)
        for isin, secid in session.execute(statement.limit(limit)).all()
        if isin
    ]
    # Свои бумаги первыми, дальше ликвидные, без повторов
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for isin, secid in owned + liquid:
        if isin in seen:
            continue
        seen.add(isin)
        result.append((isin, secid))
    return result


# Единственный экземпляр на приложение
collector = Collector()
