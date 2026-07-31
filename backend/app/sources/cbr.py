"""Коннектор к открытым данным Банка России.

Официальные курсы валют (XML_daily), ключевая ставка и RUONIA (DailyInfoWebServ).
Ключевая ставка — базовый ориентир для оценки стоимости фондирования.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any
from xml.etree import ElementTree

from ..config import settings
from .base import HttpSource, SourceError, to_date, to_float, to_int

logger = logging.getLogger(__name__)

_SOAP_PATH = "/DailyInfoWebServ/DailyInfo.asmx"
_SOAP_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Body><{method} xmlns=\"http://web.cbr.ru/\">"
    "<fromDate>{from_date}</fromDate><ToDate>{to_date}</ToDate>"
    "</{method}></soap:Body></soap:Envelope>"
)


def _localname(tag: str) -> str:
    """Имя тега без пространства имён."""
    return tag.rsplit("}", 1)[-1]


def _find_text(node: ElementTree.Element, name: str) -> str | None:
    for child in node:
        if _localname(child.tag) == name:
            return child.text
    return None


class CbrSource(HttpSource):
    """Клиент открытых данных ЦБ РФ."""

    name = "cbr"

    def __init__(self, **kwargs: Any):
        super().__init__(settings.cbr_base_url, **kwargs)

    async def fetch_fx_rates(self, on_date: date | None = None) -> list[dict[str, Any]]:
        """Официальные курсы валют на дату."""
        params: dict[str, Any] = {}
        if on_date is not None:
            params["date_req"] = on_date.strftime("%d/%m/%Y")

        response = await self.get("/scripts/XML_daily.asp", **params)
        # ЦБ отдаёт windows-1251, поэтому разбираем байты, а не response.text
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise SourceError(f"cbr: не удалось разобрать XML курсов: {exc}") from exc

        rate_date = to_date(root.attrib.get("Date")) or on_date or date.today()
        rates: list[dict[str, Any]] = []
        for node in root:
            if _localname(node.tag) != "Valute":
                continue
            code = _find_text(node, "CharCode")
            value = to_float(_find_text(node, "Value"))
            if not code or value is None:
                continue
            rates.append(
                {
                    "source": "cbr",
                    "code": code,
                    "name": _find_text(node, "Name"),
                    "nominal": to_int(_find_text(node, "Nominal")) or 1,
                    "value": value,
                    "rate_date": rate_date,
                }
            )
        return rates

    async def _fetch_soap_series(
        self, method: str, from_date: date, to_date_: date, item_tag: str
    ) -> list[ElementTree.Element]:
        """Общий вызов SOAP-метода DailyInfoWebServ, возвращает узлы ряда."""
        body = _SOAP_TEMPLATE.format(
            method=method,
            from_date=from_date.isoformat(),
            to_date=to_date_.isoformat(),
        )
        url = f"{self.base_url}{_SOAP_PATH}"
        response = await self.client.post(
            url,
            content=body.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"http://web.cbr.ru/{method}",
            },
            timeout=settings.http_timeout,
        )
        response.raise_for_status()
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise SourceError(f"cbr: некорректный SOAP-ответ {method}: {exc}") from exc

        return [node for node in root.iter() if _localname(node.tag) == item_tag]

    async def fetch_key_rate(
        self, from_date: date | None = None, to_date_: date | None = None
    ) -> list[dict[str, Any]]:
        """Ключевая ставка ЦБ за период."""
        to_date_ = to_date_ or date.today()
        from_date = from_date or (to_date_ - timedelta(days=365))

        nodes = await self._fetch_soap_series("KeyRate", from_date, to_date_, "KR")
        series: list[dict[str, Any]] = []
        for node in nodes:
            rate_date = to_date(_find_text(node, "DT"))
            value = to_float(_find_text(node, "Rate"))
            if rate_date is None or value is None:
                continue
            series.append(
                {
                    "code": "KEY_RATE",
                    "name": "Ключевая ставка Банка России",
                    "value": value,
                    "rate_date": rate_date,
                    "source": "cbr",
                }
            )
        # ЦБ отдаёт ряд от новых к старым — приводим к возрастанию по дате
        return sorted(series, key=lambda item: item["rate_date"])

    async def fetch_ruonia(
        self, from_date: date | None = None, to_date_: date | None = None
    ) -> list[dict[str, Any]]:
        """RUONIA — индикативная ставка овернайт межбанковского рынка."""
        to_date_ = to_date_ or date.today()
        from_date = from_date or (to_date_ - timedelta(days=90))

        try:
            nodes = await self._fetch_soap_series("Ruonia", from_date, to_date_, "ro")
        except Exception as exc:  # noqa: BLE001 — RUONIA не критична для работы
            logger.warning("cbr: RUONIA недоступна (%s), пропускаем", exc)
            return []

        series: list[dict[str, Any]] = []
        for node in nodes:
            rate_date = to_date(_find_text(node, "D0"))
            value = to_float(_find_text(node, "ruo"))
            if rate_date is None or value is None:
                continue
            series.append(
                {
                    "code": "RUONIA",
                    "name": "RUONIA",
                    "value": value,
                    "rate_date": rate_date,
                    "source": "cbr",
                }
            )
        return sorted(series, key=lambda item: item["rate_date"])
