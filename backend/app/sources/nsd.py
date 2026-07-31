"""Коннектор к данным НРД (Национальный расчётный депозитарий).

О происхождении данных — важно для доверия к цифрам в терминале:

НРД не публикует бесплатный machine-readable API: прямые сервисы (Ценовой центр
НРД, ленты корпоративных действий, справочник ISIN) отдаются по договору. При
этом сведения о выпуске, которые эмитент раскрывает через НРД как расчётный
депозитарий, — график купонов, амортизаций и оферт — доступны публично через
зеркало MOEX ISS (эндпоинт ``bondization``, поле ``data_source``). Этот класс
нормализует их в корпоративные действия казначейства.

Если у организации есть договор с НРД, коммерческие ленты подключаются здесь же:
задайте ``TREASURY_NSD_API_KEY`` и переопределите :meth:`NsdSource.fetch_feed` —
остальной код (сборщик, хранилище, аналитика) менять не потребуется.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from typing import Any

from .base import to_date, to_float
from .moex import MoexSource

logger = logging.getLogger(__name__)

#: Типы денежных потоков, которые ведём в терминале
COUPON = "coupon"
AMORTIZATION = "amortization"
OFFER = "offer"


class NsdSource:
    """Корпоративные действия и денежные потоки по облигациям."""

    name = "nsd"

    def __init__(self, moex: MoexSource, *, api_key: str | None = None):
        self._moex = moex
        self._api_key = api_key or os.getenv("TREASURY_NSD_API_KEY")

    @property
    def has_contract_access(self) -> bool:
        """Настроен ли доступ к коммерческим лентам НРД."""
        return bool(self._api_key)

    async def fetch_cashflows(
        self, isin: str, secid: str | None = None
    ) -> list[dict[str, Any]]:
        """Купоны, амортизации и оферты по одной бумаге."""
        try:
            payload = await self._moex.fetch_bondization(isin)
        except Exception as exc:  # noqa: BLE001 — одна бумага не должна ронять сбор
            logger.warning("nsd: не удалось получить график по %s: %s", isin, exc)
            return []

        actions: list[dict[str, Any]] = []
        actions.extend(_map_coupons(payload.get("coupons", []), isin, secid))
        actions.extend(_map_amortizations(payload.get("amortizations", []), isin, secid))
        actions.extend(_map_offers(payload.get("offers", []), isin, secid))
        return actions

    async def fetch_cashflows_bulk(
        self, securities: list[tuple[str, str]], *, concurrency: int = 4
    ) -> list[dict[str, Any]]:
        """Графики по списку ``(isin, secid)`` с ограничением параллелизма."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _one(isin: str, secid: str) -> list[dict[str, Any]]:
            async with semaphore:
                return await self.fetch_cashflows(isin, secid)

        results = await asyncio.gather(
            *(_one(isin, secid) for isin, secid in securities),
            return_exceptions=True,
        )
        collected: list[dict[str, Any]] = []
        for outcome in results:
            if isinstance(outcome, Exception):
                logger.warning("nsd: ошибка при массовой загрузке: %s", outcome)
                continue
            collected.extend(outcome)
        return collected

    async def fetch_feed(self, feed: str, **params: Any) -> list[dict[str, Any]]:
        """Точка расширения для коммерческих лент НРД (нужен договор).

        Без ключа возвращает пустой список и пишет предупреждение — терминал
        продолжает работать на открытых источниках.
        """
        if not self.has_contract_access:
            logger.info(
                "nsd: лента %r недоступна — не задан TREASURY_NSD_API_KEY "
                "(работаем на открытых источниках)",
                feed,
            )
            return []
        raise NotImplementedError(
            "Подключите здесь клиент коммерческой ленты НРД согласно вашему договору"
        )


def _base_action(isin: str, secid: str | None, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "isin": isin,
        "secid": secid or row.get("secid"),
        "name": row.get("name"),
        "face_value": to_float(row.get("facevalue")),
        "face_unit": row.get("faceunit"),
        "source": "nsd",
    }


def _map_coupons(
    rows: list[dict[str, Any]], isin: str, secid: str | None
) -> list[dict[str, Any]]:
    actions = []
    for row in rows:
        action_date = to_date(row.get("coupondate"))
        if action_date is None:
            continue
        action = _base_action(isin, secid, row)
        action.update(
            {
                "action_type": COUPON,
                "action_date": action_date,
                "record_date": to_date(row.get("recorddate")),
                "start_date": to_date(row.get("startdate")),
                "value": to_float(row.get("value")),
                "value_rub": to_float(row.get("value_rub")),
                "value_pct": to_float(row.get("valueprc")),
            }
        )
        actions.append(action)
    return actions


def _map_amortizations(
    rows: list[dict[str, Any]], isin: str, secid: str | None
) -> list[dict[str, Any]]:
    actions = []
    for row in rows:
        action_date = to_date(row.get("amortdate"))
        if action_date is None:
            continue
        action = _base_action(isin, secid, row)
        action.update(
            {
                "action_type": AMORTIZATION,
                "action_date": action_date,
                "value": to_float(row.get("value")),
                "value_rub": to_float(row.get("value_rub")),
                "value_pct": to_float(row.get("valueprc")),
            }
        )
        actions.append(action)
    return actions


def _map_offers(
    rows: list[dict[str, Any]], isin: str, secid: str | None
) -> list[dict[str, Any]]:
    actions = []
    for row in rows:
        action_date = to_date(row.get("offerdate"))
        if action_date is None:
            continue
        action = _base_action(isin, secid, row)
        action.update(
            {
                "action_type": OFFER,
                "action_date": action_date,
                "start_date": to_date(row.get("offerdatestart")),
                "record_date": to_date(row.get("offerdateend")),
                "value": to_float(row.get("value")),
                "value_pct": to_float(row.get("price")),
            }
        )
        actions.append(action)
    return actions


def upcoming_payments(
    actions: list[dict[str, Any]], *, horizon_days: int = 90
) -> list[dict[str, Any]]:
    """Отфильтровать ближайшие выплаты — вход для планирования ликвидности."""
    today = date.today()
    result = []
    for action in actions:
        action_date = action.get("action_date")
        if action_date is None or action_date < today:
            continue
        if (action_date - today).days > horizon_days:
            continue
        result.append(action)
    return sorted(result, key=lambda item: item["action_date"])
