"""Коннектор к MOEX ISS — открытому API Московской биржи.

Отдаёт цены, торговые объёмы, спреды, доходности и дюрации облигаций,
кривую бескупонной доходности и историю торгов. Авторизация не требуется.
Документация: https://iss.moex.com/iss/reference/
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from ..config import settings
from .base import HttpSource, SourceError, rows_to_dicts, to_date, to_float, to_int

logger = logging.getLogger(__name__)

#: Описание торговых площадок, которые собирает казначейство
BOARD_SPECS: dict[str, dict[str, str]] = {
    "TQBR": {"engine": "stock", "market": "shares", "kind": "share", "title": "Акции"},
    "TQOB": {"engine": "stock", "market": "bonds", "kind": "bond", "title": "ОФЗ"},
    "TQCB": {
        "engine": "stock",
        "market": "bonds",
        "kind": "bond",
        "title": "Корпоративные облигации",
    },
    "SNDX": {"engine": "stock", "market": "index", "kind": "index", "title": "Индексы"},
    "CETS": {
        "engine": "currency",
        "market": "selt",
        "kind": "currency",
        "title": "Валютный рынок",
    },
}

#: Размер страницы для эндпоинтов, которые действительно листаются (история)
_PAGE_SIZE = 100
#: Предохранитель от бесконечного цикла пагинации
_MAX_PAGES = 40


class MoexSource(HttpSource):
    """Клиент MOEX ISS."""

    name = "moex"

    def __init__(self, **kwargs: Any):
        super().__init__(settings.moex_base_url, **kwargs)

    # ------------------------------------------------------------------
    # Торговый срез по площадке
    # ------------------------------------------------------------------
    async def fetch_board(self, board: str) -> list[dict[str, Any]]:
        """Снять срез по площадке: справочник + рыночные данные одним запросом.

        Возвращает список словарей ``{"instrument": {...}, "quote": {...}}``.
        """
        spec = BOARD_SPECS.get(board)
        if spec is None:
            raise SourceError(f"moex: неизвестная площадка {board}")

        path = (
            f"/engines/{spec['engine']}/markets/{spec['market']}"
            f"/boards/{board}/securities.json"
        )
        # Этот эндпоинт игнорирует start/limit и всегда отдаёт доску целиком,
        # поэтому листать нечего — берём одним запросом и страхуемся от дублей.
        payload = await self.get_json(
            path,
            **{
                "iss.meta": "off",
                "iss.only": "securities,marketdata,marketdata_yields",
            },
        )
        securities = _dedupe_by_secid(rows_to_dicts(payload.get("securities")))
        market = rows_to_dicts(payload.get("marketdata"))
        yields = rows_to_dicts(payload.get("marketdata_yields"))

        if settings.max_instruments_per_board:
            securities = securities[: settings.max_instruments_per_board]

        market_by_id = {row.get("SECID"): row for row in market if row.get("SECID")}
        yields_by_id = {row.get("SECID"): row for row in yields if row.get("SECID")}

        ts = datetime.utcnow().replace(microsecond=0)
        result: list[dict[str, Any]] = []
        for sec in securities:
            secid = sec.get("SECID")
            if not secid:
                continue
            md = market_by_id.get(secid, {})
            yd = yields_by_id.get(secid, {})
            result.append(
                {
                    "instrument": _map_instrument(sec, board, spec),
                    "quote": _map_quote(sec, md, yd, spec["kind"], ts),
                }
            )
        return result

    async def fetch_boards(self, boards: list[str]) -> list[dict[str, Any]]:
        """Снять срезы по нескольким площадкам параллельно."""
        results = await asyncio.gather(
            *(self.fetch_board(board) for board in boards), return_exceptions=True
        )
        merged: list[dict[str, Any]] = []
        for board, outcome in zip(boards, results):
            if isinstance(outcome, Exception):
                logger.error("moex: не удалось снять срез по %s: %s", board, outcome)
                continue
            merged.extend(outcome)
        return merged

    # ------------------------------------------------------------------
    # История торгов
    # ------------------------------------------------------------------
    async def fetch_history(
        self, board: str, secid: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        """Дневная история торгов по бумаге: цены и объёмы."""
        spec = BOARD_SPECS.get(board)
        if spec is None:
            raise SourceError(f"moex: неизвестная площадка {board}")

        path = (
            f"/history/engines/{spec['engine']}/markets/{spec['market']}"
            f"/boards/{board}/securities/{secid}.json"
        )
        bars: list[dict[str, Any]] = []
        seen_dates: set[date] = set()
        start = 0
        for _ in range(_MAX_PAGES):
            payload = await self.get_json(
                path,
                **{
                    "iss.meta": "off",
                    "iss.only": "history",
                    "from": start_date.isoformat(),
                    "till": end_date.isoformat(),
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
                if bar["trade_date"] is None or bar["trade_date"] in seen_dates:
                    continue
                seen_dates.add(bar["trade_date"])
                bars.append(bar)
                fresh += 1

            # Страница без новых дат означает, что пагинация не поддерживается
            if fresh == 0 or len(page) < _PAGE_SIZE:
                break
            start += len(page)

        return sorted(bars, key=lambda bar: bar["trade_date"])

    # ------------------------------------------------------------------
    # Кривая бескупонной доходности
    # ------------------------------------------------------------------
    async def fetch_yield_curve(self) -> dict[str, Any]:
        """КБД МосБиржи: доходность по срокам — ориентир стоимости денег."""
        payload = await self.get_json(
            "/engines/stock/zcyc.json",
            **{"iss.meta": "off", "iss.only": "yearyields"},
        )
        rows = rows_to_dicts(payload.get("yearyields"))
        points = []
        curve_date: date | None = None
        for row in rows:
            period = to_float(row.get("period"))
            value = to_float(row.get("value"))
            if period is None or value is None:
                continue
            curve_date = curve_date or to_date(row.get("tradedate"))
            points.append({"period_years": period, "value": value})
        return {"curve_date": curve_date or date.today(), "points": points}

    # ------------------------------------------------------------------
    # Купоны и амортизации (данные эмиссии, источник — раскрытие НРД)
    # ------------------------------------------------------------------
    async def fetch_bondization(self, isin: str) -> dict[str, list[dict[str, Any]]]:
        """График купонов, амортизаций и оферт по облигации."""
        payload = await self.get_json(
            f"/securities/{isin}/bondization.json",
            **{"iss.meta": "off", "limit": 100},
        )
        return {
            "coupons": rows_to_dicts(payload.get("coupons")),
            "amortizations": rows_to_dicts(payload.get("amortizations")),
            "offers": rows_to_dicts(payload.get("offers")),
        }

    async def fetch_index_composition(self, index_id: str = "IMOEX") -> list[dict]:
        """Состав и веса индекса — ориентир для лимитов концентрации."""
        payload = await self.get_json(
            f"/statistics/engines/stock/markets/index/analytics/{index_id}.json",
            **{"iss.meta": "off", "iss.only": "analytics", "limit": 100},
        )
        return rows_to_dicts(payload.get("analytics"))


# ----------------------------------------------------------------------
# Преобразование строк ISS в поля моделей
# ----------------------------------------------------------------------
def _dedupe_by_secid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Оставить по одной строке на бумагу, сохранив порядок выдачи."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        secid = row.get("SECID")
        if not secid or secid in seen:
            continue
        seen.add(secid)
        unique.append(row)
    return unique


def _map_instrument(sec: dict[str, Any], board: str, spec: dict[str, str]) -> dict:
    """Справочные поля бумаги."""
    return {
        "secid": sec.get("SECID"),
        "board": board,
        "engine": spec["engine"],
        "market": spec["market"],
        "kind": spec["kind"],
        "isin": sec.get("ISIN") or None,
        "short_name": sec.get("SHORTNAME"),
        "full_name": sec.get("SECNAME") or sec.get("LATNAME"),
        "reg_number": sec.get("REGNUMBER"),
        "currency": sec.get("CURRENCYID") or sec.get("FACEUNIT"),
        "lot_size": to_int(sec.get("LOTSIZE")),
        "face_value": to_float(sec.get("FACEVALUE")),
        "face_unit": sec.get("FACEUNIT"),
        "issue_size": to_float(sec.get("ISSUESIZE")),
        "list_level": to_int(sec.get("LISTLEVEL")),
        "sector": sec.get("SECTORID"),
        "sec_type": sec.get("SECTYPE"),
        "maturity_date": to_date(sec.get("MATDATE")),
        "offer_date": to_date(sec.get("OFFERDATE")),
        "coupon_percent": to_float(sec.get("COUPONPERCENT")),
        "coupon_value": to_float(sec.get("COUPONVALUE")),
        "coupon_period": to_int(sec.get("COUPONPERIOD")),
        "next_coupon_date": to_date(sec.get("NEXTCOUPON")),
        "accrued_interest": to_float(sec.get("ACCRUEDINT")),
        "bond_type": sec.get("BONDTYPE"),
    }


def _map_quote(
    sec: dict[str, Any],
    md: dict[str, Any],
    yd: dict[str, Any],
    kind: str,
    ts: datetime,
) -> dict:
    """Рыночные поля: цена, объём, спред, доходность."""
    if kind == "index":
        # У индексов своя схема: значение вместо цены, оборот по корзине
        last = to_float(md.get("CURRENTVALUE")) or to_float(md.get("LASTVALUE"))
        return {
            "ts": ts,
            "trade_date": to_date(md.get("TRADEDATE")) or date.today(),
            "last": last,
            "open": to_float(md.get("OPENVALUE")),
            "high": to_float(md.get("HIGH")),
            "low": to_float(md.get("LOW")),
            "prev_close": to_float(md.get("LASTVALUE")),
            "change_pct": to_float(md.get("LASTCHANGEPRC")),
            "turnover": to_float(md.get("VALTODAY")),
            "source": "moex",
        }

    prev_close = to_float(sec.get("PREVPRICE")) or to_float(sec.get("PREVLEGALCLOSEPRICE"))
    last = to_float(md.get("LAST")) or to_float(md.get("LCURRENTPRICE")) or prev_close
    bid = to_float(md.get("BID"))
    offer = to_float(md.get("OFFER"))

    spread = to_float(md.get("SPREAD"))
    if spread is None and bid is not None and offer is not None:
        spread = offer - bid

    change_pct = to_float(md.get("LASTCHANGEPRCNT"))
    if change_pct is None and last is not None and prev_close:
        change_pct = (last / prev_close - 1) * 100

    # СВЦ предыдущего дня: биржа отдаёт её в справочном блоке
    prev_wa_price = to_float(sec.get("PREVWAPRICE"))
    change_to_prev_wap = to_float(md.get("WAPTOPREVWAPRICEPRCNT"))
    if change_to_prev_wap is None and last is not None and prev_wa_price:
        change_to_prev_wap = (last / prev_wa_price - 1) * 100

    quote = {
        "ts": ts,
        "trade_date": to_date(md.get("SYSTIME")) or date.today(),
        "last": last,
        "open": to_float(md.get("OPEN")),
        "high": to_float(md.get("HIGH")),
        "low": to_float(md.get("LOW")),
        "prev_close": prev_close,
        "wa_price": to_float(md.get("WAPRICE")) or prev_wa_price,
        "prev_wa_price": prev_wa_price,
        "change_pct": change_pct,
        "change_to_prev_wap_pct": change_to_prev_wap,
        "bid": bid,
        "offer": offer,
        "spread": spread,
        "bid_depth": to_float(md.get("BIDDEPTHT")),
        "offer_depth": to_float(md.get("OFFERDEPTHT")),
        "volume": to_float(md.get("VOLTODAY")),
        "turnover": to_float(md.get("VALTODAY_RUR")) or to_float(md.get("VALTODAY")),
        "num_trades": to_int(md.get("NUMTRADES")),
        "capitalization": to_float(md.get("ISSUECAPITALIZATION")),
        "trading_status": md.get("TRADINGSTATUS"),
        "source": "moex",
    }

    if kind == "bond":
        # Блок marketdata_yields точнее: там расчётная доходность и спреды к КБД
        quote["yield_pct"] = (
            to_float(yd.get("EFFECTIVEYIELD"))
            or to_float(md.get("YIELD"))
            or to_float(sec.get("YIELDATPREVWAPRICE"))
        )
        quote["duration_days"] = to_int(yd.get("DURATION")) or to_int(md.get("DURATION"))
        quote["z_spread_bp"] = to_float(yd.get("ZSPREADBP"))
        quote["g_spread_bp"] = to_float(yd.get("GSPREADBP"))
        # НКД на одну бумагу: покупатель платит его сверх цены
        quote["accrued_interest"] = to_float(sec.get("ACCRUEDINT"))

    return quote


def _map_bar(row: dict[str, Any]) -> dict:
    """Дневная свеча из блока history.

    Для облигаций MOEX дополнительно отдаёт НКД (``ACCINT``), доходности и
    дюрацию на дату — их и сохраняем, у акций эти поля останутся пустыми.
    """
    return {
        "trade_date": to_date(row.get("TRADEDATE")),
        "open": to_float(row.get("OPEN")),
        "high": to_float(row.get("HIGH")),
        "low": to_float(row.get("LOW")),
        "close": to_float(row.get("CLOSE")) or to_float(row.get("LEGALCLOSEPRICE")),
        "legal_close": to_float(row.get("LEGALCLOSEPRICE")),
        "wa_price": to_float(row.get("WAPRICE")),
        "volume": to_float(row.get("VOLUME")),
        "turnover": to_float(row.get("VALUE")),
        "num_trades": to_int(row.get("NUMTRADES")),
        "accrued_interest": to_float(row.get("ACCINT")),
        # У индексов доходность приходит в поле YIELD, у облигаций — YIELDCLOSE
        "yield_close": to_float(row.get("YIELDCLOSE")) or to_float(row.get("YIELD")),
        "yield_at_wap": to_float(row.get("YIELDATWAP")),
        "duration_days": to_int(row.get("DURATION")),
        "face_value": to_float(row.get("FACEVALUE")),
        "coupon_percent": to_float(row.get("COUPONPERCENT")),
        "currency": row.get("CURRENCYID") or row.get("FACEUNIT"),
    }
