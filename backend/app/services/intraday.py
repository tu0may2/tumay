"""Ход торгов: как идёт сессия прямо сейчас.

Собранный срез обновляется раз в несколько минут и хранит одно состояние на
момент сбора. Чтобы увидеть, как торги шли внутри дня — где прошёл объём, по
какой доходности уходили сделки, куда двигалась цена от открытия, — нужны
внутридневные данные. Биржа отдаёт их без задержки, поэтому здесь мы ходим за
ними напрямую и ничего не храним: назавтра эти данные уже в дневной истории.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import Instrument
from ..sources.moex import MoexSource
from .analytics import latest_rows

logger = logging.getLogger(__name__)

#: Шаги свечей, которые принимает биржа, в минутах
ALLOWED_INTERVALS = (1, 10, 60)


async def _load(
    board: str, secid: str, *, interval: int, trades_limit: int, on_date: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Забрать свечи и ленту одним заходом.

    Лента может быть недоступна (неторговый день, бумага без сделок), и это не
    повод ронять весь ответ: свечи сами по себе полезны.
    """
    async with MoexSource() as source:
        candles_task = source.fetch_candles(
            board, secid, interval=interval, start_date=on_date
        )
        trades_task = source.fetch_trades(board, secid, limit=trades_limit)
        candles, trades = await asyncio.gather(
            candles_task, trades_task, return_exceptions=True
        )

    warning = None
    if isinstance(candles, Exception):
        logger.warning("Свечи по %s не получены: %s", secid, candles)
        warning = "Биржа не отдала внутридневные свечи"
        candles = []
    if isinstance(trades, Exception):
        logger.warning("Лента сделок по %s не получена: %s", secid, trades)
        warning = warning or "Биржа не отдала ленту сделок"
        trades = []
    return candles, trades, warning


def _session_stats(
    candles: list[dict[str, Any]], trades: list[dict[str, Any]]
) -> dict[str, Any]:
    """Итоги сессии по самим свечам — независимо от среза."""
    if not candles:
        return {
            "open": None, "high": None, "low": None, "last": None,
            "volume": 0.0, "turnover": 0.0, "change_pct": None,
            "wa_price": None, "first_at": None, "last_at": None,
        }

    volume = sum(candle["volume"] or 0 for candle in candles)
    turnover = sum(candle["value"] or 0 for candle in candles)
    highs = [c["high"] for c in candles if c["high"] is not None]
    lows = [c["low"] for c in candles if c["low"] is not None]

    opening = candles[0]["open"]
    closing = candles[-1]["close"]
    # Последняя сделка свежее последней завершённой свечи
    if trades and trades[0].get("price") is not None:
        closing = trades[0]["price"]

    return {
        "open": opening,
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "last": closing,
        "volume": round(volume, 2),
        "turnover": round(turnover, 2),
        "change_pct": (
            round((closing / opening - 1) * 100, 2)
            if opening and closing else None
        ),
        # Средневзвешенная цена сессии: оборот, делённый на объём. Для
        # облигаций объём в штуках, а оборот в рублях, поэтому приводим
        # к процентам от номинала тем же множителем, что и биржа
        "wa_price": None,
        "first_at": candles[0]["begin"],
        "last_at": candles[-1]["end"],
    }


def _weighted_price(
    candles: list[dict[str, Any]], instrument: Instrument
) -> float | None:
    """Средневзвешенная цена сессии из оборота и объёма."""
    volume = sum(candle["volume"] or 0 for candle in candles)
    turnover = sum(candle["value"] or 0 for candle in candles)
    if not volume or not turnover:
        return None

    price_per_unit = turnover / volume
    if instrument.kind == "bond" and instrument.face_value:
        # Облигации котируются в процентах от номинала, а оборот считается
        # в деньгах, поэтому переводим обратно в проценты
        return round(price_per_unit / instrument.face_value * 100, 4)
    return round(price_per_unit, 4)


def _trade_flow(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Кто двигал цену: инициатива покупателя против инициативы продавца.

    Доля считается по деньгам, и одна крупная сделка может перевесить десятки
    мелких — поэтому рядом отдаётся и число сделок с каждой стороны. Без него
    «100% покупки» читается как «все сделки на покупку», хотя на деле это одна
    заявка блоком.
    """
    buy = sum(t["value"] or 0 for t in trades if t.get("side") == "B")
    sell = sum(t["value"] or 0 for t in trades if t.get("side") == "S")
    buy_trades = sum(1 for t in trades if t.get("side") == "B")
    sell_trades = sum(1 for t in trades if t.get("side") == "S")
    total = buy + sell

    share = None
    if total:
        share = round(buy / total * 100, 1)
        # Не выдаём чистые 100% и 0%, пока другая сторона не пуста:
        # округление не должно стирать сделки
        if share >= 100 and sell:
            share = 99.9
        elif share <= 0 and buy:
            share = 0.1

    return {
        "buy_value": round(buy, 2),
        "sell_value": round(sell, 2),
        "buy_share_pct": share,
        "buy_trades": buy_trades,
        "sell_trades": sell_trades,
        "trades": len(trades),
        "note": (
            "Доля посчитана по деньгам последних сделок ленты, а не по всей "
            "сессии: одна крупная заявка перевешивает много мелких."
            if trades else None
        ),
    }


async def trading_session(
    session: Session,
    secid: str,
    *,
    interval: int = 10,
    trades_limit: int = 50,
    on_date: date | None = None,
) -> dict[str, Any]:
    """Ход торгов по бумаге: свечи, лента, итоги сессии."""
    secid = secid.upper()
    rows = latest_rows(session, secids=(secid,))
    if not rows:
        raise LookupError(f"Инструмент {secid} не найден")

    instrument, quote = rows[0]
    if interval not in ALLOWED_INTERVALS:
        interval = 10
    on_date = on_date or date.today()

    candles, trades, warning = await _load(
        instrument.board, secid,
        interval=interval, trades_limit=trades_limit, on_date=on_date,
    )

    stats = _session_stats(candles, trades)
    stats["wa_price"] = _weighted_price(candles, instrument)

    # Срез собирается по расписанию и к моменту запроса уже отстаёт; свечи и
    # лента приходят прямо сейчас, поэтому расхождение с ним — не ошибка
    snapshot = {
        "ts": quote.ts if quote else None,
        "last": quote.last if quote else None,
        "prev_wa_price": quote.prev_wa_price if quote else None,
        "volume": quote.volume if quote else None,
        "turnover": quote.turnover if quote else None,
        "num_trades": quote.num_trades if quote else None,
    } if quote else None

    return {
        "secid": secid,
        "name": instrument.short_name or instrument.name,
        "board": instrument.board,
        "kind": instrument.kind,
        "trade_date": on_date,
        "interval_minutes": interval,
        "candles": candles,
        "trades": trades,
        "session": stats,
        "flow": _trade_flow(trades),
        "snapshot": snapshot,
        "warning": warning,
        "note": (
            "Свечи и лента сделок берутся с биржи в момент запроса, минуя "
            "собранный срез: это единственный способ увидеть текущие торги, "
            "потому что срез обновляется по расписанию."
        ),
    }
