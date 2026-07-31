"""Сбор данных из внешних источников в хранилище.

Каждый запуск фиксируется в ``collection_runs``: видно, что собрано, когда и
с какой ошибкой. Сбой одного источника не останавливает остальные.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import (
    Bar,
    CollectionRun,
    CorpAction,
    CurvePoint,
    FxRate,
    Instrument,
    MacroRate,
    Quote,
)
from ..sources import CbrSource, MoexSource, NsdSource
from .analytics import latest_quote_ids

logger = logging.getLogger(__name__)

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
        """Курсы ЦБ, ключевая ставка, RUONIA и кривая бескупонной доходности."""
        total = 0
        with self._run("cbr", "reference") as counter:
            async with CbrSource() as cbr:
                fx, key_rate, ruonia = await asyncio.gather(
                    cbr.fetch_fx_rates(),
                    cbr.fetch_key_rate(),
                    cbr.fetch_ruonia(),
                    return_exceptions=True,
                )
            with session_scope() as session:
                if not isinstance(fx, Exception):
                    total += _upsert(
                        session, FxRate, fx, ("source", "code", "rate_date"), ("value",)
                    )
                macro = [
                    row
                    for series in (key_rate, ruonia)
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
            steps.append(("corp_actions", self.collect_corp_actions()))

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
    statement = (
        select(Instrument.id, Instrument.secid, Instrument.board)
        .join(Quote, Quote.instrument_id == Instrument.id)
        .where(
            Quote.id.in_(latest_quote_ids(session)),
            Quote.turnover.isnot(None),
            Instrument.kind.in_(("share", "bond")),
        )
        .order_by(Quote.turnover.desc())
        .limit(top_by_turnover)
    )
    return [tuple(row) for row in session.execute(statement).all()]


def _bond_targets(session: Session, limit: int) -> list[tuple[str, str]]:
    """Облигации с ISIN, по которым имеет смысл тянуть график выплат."""
    has_quotes = session.execute(select(func.count()).select_from(Quote)).scalar()
    statement = select(Instrument.isin, Instrument.secid).where(
        Instrument.kind == "bond", Instrument.isin.isnot(None)
    )
    if has_quotes:
        # Приоритет — торгуемым выпускам: по ним график выплат реально нужен
        statement = (
            statement.join(Quote, Quote.instrument_id == Instrument.id)
            .where(Quote.id.in_(latest_quote_ids(session)))
            .order_by(Quote.turnover.desc().nullslast())
        )
    return [
        (isin, secid)
        for isin, secid in session.execute(statement.limit(limit)).all()
        if isin
    ]


# Единственный экземпляр на приложение
collector = Collector()
