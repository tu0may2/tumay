"""Аналитика для принятия решений: ликвидность, доходности, аномалии, сигналы."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..models import Bar, CorpAction, CurvePoint, FxRate, Instrument, MacroRate, Quote

#: Веса компонентов оценки ликвидности
_W_TURNOVER, _W_TRADES, _W_SPREAD = 0.45, 0.30, 0.25
#: Оборот 10 тыс. руб. → 0 баллов, 10 млрд руб. → 100 баллов
_TURNOVER_FLOOR_LOG, _TURNOVER_CEIL_LOG = 4.0, 10.0
#: 1 сделка → 0 баллов, 100 тыс. сделок → 100 баллов
_TRADES_CEIL_LOG = 5.0
#: Относительный спред 1% и шире → 0 баллов
_SPREAD_ZERO_PCT = 1.0
#: Минимальный уровень шума объёма (доля среднего) при нулевой дисперсии
_MIN_VOLUME_NOISE = 0.1
#: Потолок z-оценки, чтобы выбросы не ломали сортировку сигналов
_MAX_Z_SCORE = 99.0


@dataclass(slots=True)
class Alert:
    """Сигнал для казначея."""

    severity: str  # info | warning | critical
    category: str
    secid: str | None
    title: str
    detail: str
    value: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "secid": self.secid,
            "title": self.title,
            "detail": self.detail,
            "value": self.value,
        }


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# ----------------------------------------------------------------------
# Доступ к последнему срезу
# ----------------------------------------------------------------------
def latest_quote_ids(session: Session) -> Select:
    """Подзапрос: id последней котировки по каждому инструменту.

    Площадки снимаются разными запросами, поэтому единого «последнего ts» нет —
    берём максимум по каждому инструменту отдельно.
    """
    latest_ts = (
        select(Quote.instrument_id, func.max(Quote.ts).label("ts"))
        .group_by(Quote.instrument_id)
        .subquery()
    )
    return (
        select(func.max(Quote.id))
        .join(
            latest_ts,
            (Quote.instrument_id == latest_ts.c.instrument_id) & (Quote.ts == latest_ts.c.ts),
        )
        .group_by(Quote.instrument_id)
    )


def latest_rows(
    session: Session,
    *,
    kinds: Sequence[str] | None = None,
    boards: Sequence[str] | None = None,
    secids: Sequence[str] | None = None,
) -> list[tuple[Instrument, Quote]]:
    """Последний срез: пары (инструмент, котировка)."""
    statement = (
        select(Instrument, Quote)
        .join(Quote, Quote.instrument_id == Instrument.id)
        .where(Quote.id.in_(latest_quote_ids(session)))
    )
    if kinds:
        statement = statement.where(Instrument.kind.in_(list(kinds)))
    if boards:
        statement = statement.where(Instrument.board.in_(list(boards)))
    if secids:
        statement = statement.where(Instrument.secid.in_(list(secids)))
    return [tuple(row) for row in session.execute(statement).all()]


# ----------------------------------------------------------------------
# Ликвидность
# ----------------------------------------------------------------------
def relative_spread_pct(quote: Quote) -> float | None:
    """Спред в процентах от цены — сопоставимая мера издержек входа/выхода."""
    price = quote.last or quote.wa_price or quote.prev_close
    if not price or quote.spread is None:
        return None
    return quote.spread / price * 100


def liquidity_score(quote: Quote) -> float | None:
    """Оценка ликвидности 0–100: оборот, число сделок и спред.

    Нужна, чтобы казначей заранее видел, какой объём бумага «переварит»
    без заметного проскальзывания.
    """
    components: list[tuple[float, float]] = []

    if quote.turnover and quote.turnover > 0:
        score = (
            (math.log10(quote.turnover) - _TURNOVER_FLOOR_LOG)
            / (_TURNOVER_CEIL_LOG - _TURNOVER_FLOOR_LOG)
            * 100
        )
        components.append((_clamp(score), _W_TURNOVER))

    if quote.num_trades and quote.num_trades > 0:
        score = math.log10(quote.num_trades) / _TRADES_CEIL_LOG * 100
        components.append((_clamp(score), _W_TRADES))

    spread_pct = relative_spread_pct(quote)
    if spread_pct is not None:
        score = 100 - (spread_pct / _SPREAD_ZERO_PCT) * 100
        components.append((_clamp(score), _W_SPREAD))

    if not components:
        return None

    total_weight = sum(weight for _, weight in components)
    return round(sum(score * weight for score, weight in components) / total_weight, 1)


# ----------------------------------------------------------------------
# Кривая бескупонной доходности
# ----------------------------------------------------------------------
def yield_curve(session: Session) -> dict[str, Any]:
    """Актуальная КБД: срок в годах → доходность, %."""
    curve_date = session.execute(select(func.max(CurvePoint.curve_date))).scalar()
    if curve_date is None:
        return {"curve_date": None, "points": []}

    rows = session.execute(
        select(CurvePoint.period_years, CurvePoint.value)
        .where(CurvePoint.curve_date == curve_date)
        .order_by(CurvePoint.period_years)
    ).all()
    return {
        "curve_date": curve_date,
        "points": [{"period_years": p, "value": v} for p, v in rows],
    }


def curve_yield_at(points: Sequence[dict[str, Any]], years: float) -> float | None:
    """Линейная интерполяция доходности КБД на произвольный срок."""
    if not points:
        return None
    if years <= points[0]["period_years"]:
        return points[0]["value"]
    if years >= points[-1]["period_years"]:
        return points[-1]["value"]

    for left, right in zip(points, points[1:]):
        if left["period_years"] <= years <= right["period_years"]:
            span = right["period_years"] - left["period_years"]
            if span == 0:
                return left["value"]
            weight = (years - left["period_years"]) / span
            return left["value"] + weight * (right["value"] - left["value"])
    return None


# ----------------------------------------------------------------------
# Витрина инструментов
# ----------------------------------------------------------------------
def build_row(
    instrument: Instrument,
    quote: Quote,
    curve_points: Sequence[dict] | None = None,
    fx_rate: float = 1.0,
) -> dict[str, Any]:
    """Развернуть пару инструмент+котировка в строку витрины с метриками.

    ``fx_rate`` — курс валюты номинала к рублю: нужен, чтобы у валютных
    выпусков сумма к оплате считалась в одной валюте с НКД.
    """
    row: dict[str, Any] = {
        "secid": instrument.secid,
        "isin": instrument.isin,
        "board": instrument.board,
        "kind": instrument.kind,
        "name": instrument.display_name,
        "full_name": instrument.full_name,
        "currency": instrument.currency,
        "sector": instrument.sector,
        "list_level": instrument.list_level,
        "lot_size": instrument.lot_size,
        "face_value": instrument.face_value,
        "ts": quote.ts,
        "last": quote.last,
        "open": quote.open,
        "high": quote.high,
        "low": quote.low,
        "prev_close": quote.prev_close,
        "wa_price": quote.wa_price,
        "prev_wa_price": quote.prev_wa_price,
        "change_pct": quote.change_pct,
        "change_to_prev_wap_pct": _round(quote.change_to_prev_wap_pct, 2),
        "bid": quote.bid,
        "offer": quote.offer,
        "spread": quote.spread,
        "spread_pct": _round(relative_spread_pct(quote), 4),
        "volume": quote.volume,
        "turnover": quote.turnover,
        "num_trades": quote.num_trades,
        "capitalization": quote.capitalization,
        "liquidity_score": liquidity_score(quote),
        "trading_status": quote.trading_status,
        "face_unit": instrument.face_unit,
        "fx_rate": fx_rate,
    }

    if instrument.kind == "bond":
        duration_years = (
            quote.duration_days / 365 if quote.duration_days is not None else None
        )
        # НКД берём из среза, а если биржа его не прислала — из справочника
        accrued = (
            quote.accrued_interest
            if quote.accrued_interest is not None
            else instrument.accrued_interest
        )
        # Полная («грязная») цена — то, что фактически платит покупатель.
        # Цена задана в процентах от номинала, а номинал может быть валютным;
        # НКД биржа отдаёт уже в валюте расчётов. Поэтому чистую часть сначала
        # приводим к рублям по курсу и только потом складываем с НКД.
        dirty_price = None
        settlement_amount = None
        price = quote.last or quote.wa_price or quote.prev_close
        if price is not None and instrument.face_value:
            face_rub = instrument.face_value * fx_rate
            clean_amount_rub = price / 100 * face_rub
            settlement_amount = clean_amount_rub + (accrued or 0)
            dirty_price = settlement_amount / face_rub * 100 if face_rub else None
        benchmark = (
            curve_yield_at(curve_points, duration_years)
            if curve_points and duration_years is not None
            else None
        )
        row.update(
            {
                "yield_pct": quote.yield_pct,
                "duration_days": quote.duration_days,
                "duration_years": _round(duration_years, 2),
                "z_spread_bp": quote.z_spread_bp,
                "g_spread_bp": quote.g_spread_bp,
                "maturity_date": instrument.maturity_date,
                "offer_date": instrument.offer_date,
                "coupon_percent": instrument.coupon_percent,
                "coupon_value": instrument.coupon_value,
                "coupon_period": instrument.coupon_period,
                "next_coupon_date": instrument.next_coupon_date,
                "accrued_interest": _round(accrued, 2),
                "dirty_price": _round(dirty_price, 4),
                "settlement_amount": _round(settlement_amount, 2),
                "bond_type": instrument.bond_type,
                "curve_yield_pct": _round(benchmark, 2),
                # Премия к безрисковой кривой — главный ориентир relative value
                "spread_to_curve_bp": _round(
                    (quote.yield_pct - benchmark) * 100
                    if quote.yield_pct is not None and benchmark is not None
                    else None,
                    0,
                ),
            }
        )
    return row


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def screener(
    session: Session,
    *,
    kinds: Sequence[str] | None = None,
    boards: Sequence[str] | None = None,
    search: str | None = None,
    min_turnover: float | None = None,
    min_liquidity: float | None = None,
    min_yield: float | None = None,
    max_yield: float | None = None,
    max_duration_years: float | None = None,
    sort_by: str = "turnover",
    descending: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Отбор инструментов по торговым и кредитным критериям."""
    curve = yield_curve(session)
    curve_points = curve["points"]

    from .fx import FxBook, instrument_currency

    fx = FxBook(session)
    rows = [
        build_row(
            instrument, quote, curve_points,
            fx_rate=fx.rate(instrument_currency(instrument)) or 1.0,
        )
        for instrument, quote in latest_rows(session, kinds=kinds, boards=boards)
    ]

    if search:
        needle = search.strip().lower()
        rows = [
            row
            for row in rows
            if needle in (row["secid"] or "").lower()
            or needle in (row["name"] or "").lower()
            or needle in (row["isin"] or "").lower()
        ]

    def _keep(row: dict[str, Any]) -> bool:
        if min_turnover is not None and (row["turnover"] or 0) < min_turnover:
            return False
        if min_liquidity is not None and (row["liquidity_score"] or 0) < min_liquidity:
            return False
        if min_yield is not None and (row.get("yield_pct") or 0) < min_yield:
            return False
        if max_yield is not None and (row.get("yield_pct") or 0) > max_yield:
            return False
        if max_duration_years is not None:
            duration = row.get("duration_years")
            if duration is None or duration > max_duration_years:
                return False
        return True

    rows = [row for row in rows if _keep(row)]
    total = len(rows)

    # Строки без значения сортируемого поля всегда уходят вниз — независимо от
    # направления сортировки. Иначе при сортировке по убыванию наверх всплывают
    # бумаги без доходности или оборота.
    present = [row for row in rows if row.get(sort_by) is not None]
    missing = [row for row in rows if row.get(sort_by) is None]
    present.sort(key=lambda row: row[sort_by], reverse=descending)
    rows = present + missing
    return {
        "total": total,
        "curve_date": curve["curve_date"],
        "items": rows[offset : offset + limit],
    }


# ----------------------------------------------------------------------
# Обзор рынка
# ----------------------------------------------------------------------
def market_overview(session: Session) -> dict[str, Any]:
    """Сводка для верхней панели терминала."""
    rows = latest_rows(session)

    def _latest_macro(code: str) -> dict[str, Any] | None:
        row = session.execute(
            select(MacroRate.value, MacroRate.rate_date, MacroRate.name)
            .where(MacroRate.code == code)
            .order_by(MacroRate.rate_date.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return {"value": row[0], "date": row[1], "name": row[2]}

    fx_date = session.execute(
        select(func.max(FxRate.rate_date)).where(FxRate.source == "cbr")
    ).scalar()
    fx_rows = (
        session.execute(
            select(FxRate.code, FxRate.value, FxRate.nominal, FxRate.name).where(
                FxRate.source == "cbr",
                FxRate.rate_date == fx_date,
                FxRate.code.in_(("USD", "EUR", "CNY")),
            )
        ).all()
        if fx_date
        else []
    )

    indices = [
        {
            "secid": instrument.secid,
            "name": instrument.display_name,
            "value": quote.last,
            "change_pct": quote.change_pct,
        }
        for instrument, quote in rows
        if instrument.kind == "index" and instrument.secid in ("IMOEX", "RGBI", "RTSI", "MOEXBC")
    ]

    traded = [(inst, q) for inst, q in rows if (q.turnover or 0) > 0]
    total_turnover = sum(q.turnover or 0 for _, q in traded)

    return {
        "updated_at": max((q.ts for _, q in rows), default=None),
        "key_rate": _latest_macro("KEY_RATE"),
        "ruonia": _latest_macro("RUONIA"),
        "fx": [
            {
                "code": code,
                "value": value / (nominal or 1),
                "name": name,
                "date": fx_date,
            }
            for code, value, nominal, name in fx_rows
        ],
        "indices": sorted(indices, key=lambda item: item["secid"]),
        "instruments_total": len(rows),
        "instruments_traded": len(traded),
        "total_turnover": total_turnover,
        "curve": yield_curve(session),
    }


def movers(
    session: Session, *, kind: str = "share", limit: int = 5, min_turnover: float = 1_000_000
) -> dict[str, list[dict[str, Any]]]:
    """Лидеры роста и падения среди ликвидных бумаг."""
    rows = [
        build_row(instrument, quote)
        for instrument, quote in latest_rows(session, kinds=(kind,))
        if quote.change_pct is not None and (quote.turnover or 0) >= min_turnover
    ]
    rows.sort(key=lambda row: row["change_pct"], reverse=True)
    return {
        "gainers": rows[:limit],
        "losers": list(reversed(rows[-limit:])) if len(rows) >= limit else [],
    }


# ----------------------------------------------------------------------
# Аномалии объёма
# ----------------------------------------------------------------------
def volume_anomalies(
    session: Session, *, lookback_days: int = 30, z_threshold: float = 2.0, limit: int = 20
) -> list[dict[str, Any]]:
    """Бумаги с необычным объёмом торгов относительно собственной истории.

    Всплеск объёма часто опережает движение цены — повод посмотреть на бумагу
    до того, как она попадёт в лидеры роста.
    """
    since = date.today() - timedelta(days=lookback_days)
    history = session.execute(
        select(Bar.instrument_id, Bar.trade_date, Bar.volume, Bar.turnover)
        .where(Bar.trade_date >= since, Bar.volume.isnot(None))
        .order_by(Bar.instrument_id, Bar.trade_date)
    ).all()

    by_instrument: dict[int, list[tuple[date, float, float | None]]] = {}
    for instrument_id, trade_date, volume, turnover in history:
        by_instrument.setdefault(instrument_id, []).append((trade_date, volume, turnover))

    instruments = {
        instrument.id: instrument
        for instrument in session.execute(
            select(Instrument).where(Instrument.id.in_(by_instrument.keys()))
        ).scalars()
    }

    findings: list[dict[str, Any]] = []
    for instrument_id, series in by_instrument.items():
        # Последний бар сравниваем с распределением предыдущих
        if len(series) < 6:
            continue
        *baseline, latest = series
        volumes = [volume for _, volume, _ in baseline if volume and volume > 0]
        if len(volumes) < 5:
            continue

        mean = statistics.fmean(volumes)
        if mean == 0:
            continue

        # Ровный ряд даёт нулевую дисперсию, и тогда любой всплеск давал бы
        # деление на ноль — такие бумаги молча выпадали из анализа. Задаём
        # минимальный уровень «шума» как долю среднего объёма.
        stdev = max(statistics.pstdev(volumes), mean * _MIN_VOLUME_NOISE)

        latest_date, latest_volume, latest_turnover = latest
        z_score = (latest_volume - mean) / stdev
        if abs(z_score) < z_threshold:
            continue
        # Ограничиваем шкалу, чтобы редкие выбросы не ломали сортировку
        z_score = max(-_MAX_Z_SCORE, min(_MAX_Z_SCORE, z_score))

        instrument = instruments.get(instrument_id)
        if instrument is None:
            continue
        findings.append(
            {
                "secid": instrument.secid,
                "name": instrument.display_name,
                "kind": instrument.kind,
                "trade_date": latest_date,
                "volume": latest_volume,
                "turnover": latest_turnover,
                "avg_volume": round(mean, 2),
                "z_score": round(z_score, 2),
                "ratio_to_avg": round(latest_volume / mean, 2),
            }
        )

    findings.sort(key=lambda item: abs(item["z_score"]), reverse=True)
    return findings[:limit]


# ----------------------------------------------------------------------
# Календарь выплат
# ----------------------------------------------------------------------
def cashflow_calendar(
    session: Session, *, horizon_days: int = 90, secids: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """Ближайшие купоны, амортизации и оферты — вход для плана ликвидности."""
    today = date.today()
    statement = (
        select(CorpAction)
        .where(
            CorpAction.action_date >= today,
            CorpAction.action_date <= today + timedelta(days=horizon_days),
        )
        .order_by(CorpAction.action_date)
    )
    if secids:
        statement = statement.where(CorpAction.secid.in_(list(secids)))

    return [
        {
            "isin": action.isin,
            "secid": action.secid,
            "name": action.name,
            "action_type": action.action_type,
            "action_date": action.action_date,
            "record_date": action.record_date,
            "value": action.value,
            "value_rub": action.value_rub,
            "face_unit": action.face_unit,
            "days_left": (action.action_date - today).days,
            "source": action.source,
        }
        for action in session.execute(statement).scalars()
    ]


# ----------------------------------------------------------------------
# Сигналы
# ----------------------------------------------------------------------
def alerts(session: Session, *, limit: int = 30) -> list[dict[str, Any]]:
    """Свести наблюдения в список сигналов, отсортированный по важности."""
    found: list[Alert] = []

    # 1. Всплески объёма
    for anomaly in volume_anomalies(session, limit=8):
        direction = "выше" if anomaly["z_score"] > 0 else "ниже"
        found.append(
            Alert(
                severity="warning" if abs(anomaly["z_score"]) >= 3 else "info",
                category="volume",
                secid=anomaly["secid"],
                title=f"{anomaly['name']}: объём {direction} нормы в {anomaly['ratio_to_avg']}x",
                detail=(
                    f"Объём {anomaly['volume']:,.0f} против среднего "
                    f"{anomaly['avg_volume']:,.0f} за период (z={anomaly['z_score']})"
                ).replace(",", " "),
                value=anomaly["z_score"],
            )
        )

    # 2. Широкий спред у бумаг, которыми реально торгуют
    for instrument, quote in latest_rows(session, kinds=("share", "bond")):
        spread_pct = relative_spread_pct(quote)
        if spread_pct is None or (quote.turnover or 0) < 5_000_000:
            continue
        if spread_pct >= 1.0:
            found.append(
                Alert(
                    severity="warning",
                    category="liquidity",
                    secid=instrument.secid,
                    title=f"{instrument.display_name}: широкий спред {spread_pct:.2f}%",
                    detail=(
                        "Издержки входа и выхода повышены — дробите заявку "
                        "или используйте лимитные ордера"
                    ),
                    value=round(spread_pct, 2),
                )
            )

    # 3. Резкие движения цены
    for instrument, quote in latest_rows(session, kinds=("share",)):
        if quote.change_pct is None or (quote.turnover or 0) < 10_000_000:
            continue
        if abs(quote.change_pct) >= 5:
            found.append(
                Alert(
                    severity="critical" if abs(quote.change_pct) >= 10 else "warning",
                    category="price",
                    secid=instrument.secid,
                    title=f"{instrument.display_name}: {quote.change_pct:+.2f}% за сессию",
                    detail=f"Цена {quote.last}, оборот {(quote.turnover or 0) / 1e6:.1f} млн руб.",
                    value=round(quote.change_pct, 2),
                )
            )

    # 4. Ближайшие выплаты
    for payment in cashflow_calendar(session, horizon_days=14)[:8]:
        found.append(
            Alert(
                severity="info",
                category="cashflow",
                secid=payment["secid"],
                title=(
                    f"{payment['name'] or payment['secid']}: "
                    f"{_ACTION_TITLES.get(payment['action_type'], payment['action_type'])} "
                    f"через {payment['days_left']} дн."
                ),
                detail=(
                    f"Дата {payment['action_date']:%d.%m.%Y}, "
                    f"выплата {payment['value']} {payment['face_unit'] or ''}".strip()
                ),
                value=float(payment["days_left"]),
            )
        )

    order = {"critical": 0, "warning": 1, "info": 2}
    found.sort(key=lambda alert: (order.get(alert.severity, 3), -abs(alert.value or 0)))
    return [alert.as_dict() for alert in found[:limit]]


_ACTION_TITLES = {
    "coupon": "купон",
    "amortization": "амортизация",
    "offer": "оферта",
}
