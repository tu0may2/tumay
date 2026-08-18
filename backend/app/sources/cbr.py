"""Коннектор к открытым данным Банка России.

Официальные курсы валют (XML_daily), ключевая ставка и RUONIA (DailyInfoWebServ).
Ключевая ставка — базовый ориентир для оценки стоимости фондирования.
"""
from __future__ import annotations

import logging
import re
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


#: Метрики RUONIA со страницы динамики ЦБ: код, название, номер столбца таблицы.
#: Столбец 0 — дата ставки, поэтому нумерация начинается с единицы.
RUONIA_METRICS: tuple[tuple[str, str, int], ...] = (
    ("RUONIA", "RUONIA, % годовых", 1),
    ("RUONIA_VOLUME", "Объём сделок RUONIA, млрд ₽", 2),
    ("RUONIA_DEALS", "Количество сделок RUONIA, ед.", 3),
    ("RUONIA_PARTICIPANTS", "Участников RUONIA со сделками, ед.", 4),
    ("RUONIA_MIN", "Минимальная ставка RUONIA, % годовых", 5),
    ("RUONIA_P25", "25-й процентиль ставок RUONIA, % годовых", 6),
    ("RUONIA_P75", "75-й процентиль ставок RUONIA, % годовых", 7),
    ("RUONIA_MAX", "Максимальная ставка RUONIA, % годовых", 8),
)

_TABLE_RE = re.compile(r'<table[^>]*class="data"[^>]*>(.*?)</table>', re.S)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


#: Разбор календаря заседаний: страница ЦБ размечена блоками «день → события»
_SPACES_RE = re.compile(r"\s+")
_DAY_RE = re.compile(
    r'<div class="main-events_day">(.*?)(?=<div class="main-events_day">'
    r'|<div class="calendar-main-events">|\Z)'
)
_DATE_RE = re.compile(r'<div class="date[^"]*">(.*?)</div>')
_TITLE_RE = re.compile(r'<div class="title">(.*?)(?:<div class="icon_wrapper|</div>)', re.S)
_LINK_RE = re.compile(r'<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)

_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}


def _parse_russian_date(text: str) -> date | None:
    """«13 февраля 2026 года» → дата."""
    match = re.match(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text.strip().lower())
    if match is None:
        return None
    month = _RU_MONTHS.get(match.group(2))
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _absolute_url(href: str) -> str:
    """Ссылки на странице относительные — приводим к полным."""
    if href.startswith("http"):
        return href
    return f"{settings.cbr_base_url.rstrip('/')}/{href.lstrip('/')}"


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

    async def fetch_ruonia_details(
        self, from_date: date | None = None, to_date_: date | None = None
    ) -> list[dict[str, Any]]:
        """Полный набор показателей RUONIA со страницы динамики ЦБ.

        SOAP-метод отдаёт только ставку и объём. Публичная страница динамики
        даёт вдобавок число сделок, число участников и разброс ставок
        (минимум, 25-й и 75-й процентили, максимум) — то, по чему видно,
        насколько ставка репрезентативна.
        """
        to_date_ = to_date_ or date.today()
        from_date = from_date or (to_date_ - timedelta(days=365))

        try:
            response = await self.get(
                "/hd_base/ruonia/dynamics/",
                **{
                    "UniDbQuery.Posted": "True",
                    "UniDbQuery.From": from_date.strftime("%d.%m.%Y"),
                    "UniDbQuery.To": to_date_.strftime("%d.%m.%Y"),
                },
            )
        except Exception as exc:  # noqa: BLE001 — показатели не критичны для работы
            logger.warning("cbr: динамика RUONIA недоступна (%s), пропускаем", exc)
            return []

        table = _TABLE_RE.search(response.text)
        if table is None:
            logger.warning("cbr: на странице динамики RUONIA нет таблицы данных")
            return []

        series: list[dict[str, Any]] = []
        for row in _ROW_RE.findall(table.group(1)):
            cells = [
                _TAG_RE.sub("", cell).replace("\xa0", " ").strip()
                for cell in _CELL_RE.findall(row)
            ]
            if not cells:
                continue
            rate_date = to_date(cells[0])
            if rate_date is None:  # строка шапки
                continue
            for code, name, index in RUONIA_METRICS:
                if index >= len(cells):
                    continue
                value = to_float(cells[index])
                if value is None:
                    continue
                series.append(
                    {
                        "code": code,
                        "name": name,
                        "value": value,
                        "rate_date": rate_date,
                        "source": "cbr",
                    }
                )
        return sorted(series, key=lambda item: (item["rate_date"], item["code"]))

    async def fetch_rate_calendar(self) -> list[dict[str, Any]]:
        """Календарь заседаний Совета директоров по ключевой ставке.

        Машиночитаемого канала у ЦБ для него нет, поэтому разбираем страницу
        календаря: она размечена одинаково много лет и содержит и прошедшие
        заседания, и запланированные на год вперёд.

        Каждое событие несёт дату, название, признак «с публикацией
        среднесрочного прогноза» (в вёрстке — иконка important; такие
        заседания называют опорными) и ссылки на пресс-релиз, прогноз и
        пресс-конференцию.
        """
        try:
            response = await self.get("/dkp/cal_mp/")
        except Exception as exc:  # noqa: BLE001 — календарь не критичен
            logger.warning("cbr: календарь заседаний недоступен (%s)", exc)
            return []

        text = _SPACES_RE.sub(" ", response.text).replace("&nbsp;", " ")
        events: list[dict[str, Any]] = []
        for block in _DAY_RE.findall(text):
            match = _DATE_RE.search(block)
            if match is None:
                continue
            meeting_date = _parse_russian_date(_TAG_RE.sub("", match.group(1)).strip())
            if meeting_date is None:
                continue

            title = ""
            title_match = _TITLE_RE.search(block)
            if title_match:
                title = _TAG_RE.sub("", title_match.group(1)).strip()
            if not title:
                continue

            links = [
                {"title": _TAG_RE.sub("", label).strip(), "url": _absolute_url(href)}
                for href, label in _LINK_RE.findall(block)
                if _TAG_RE.sub("", label).strip()
            ]

            lowered = title.lower()
            if "заседание" in lowered and "ключевой ставке" in lowered:
                kind = "extraordinary" if "внеочередное" in lowered else "regular"
            else:
                # Доклад о ДКП, резюме обсуждения и прочие события календаря
                kind = "other"

            events.append(
                {
                    "meeting_date": meeting_date,
                    "title": title,
                    "kind": kind,
                    "with_forecast": "icon-important" in block,
                    "links": links,
                }
            )

        # Одна дата может нести несколько событий — дубли снимаем по паре
        seen: set[tuple[date, str]] = set()
        unique: list[dict[str, Any]] = []
        for event in sorted(events, key=lambda item: item["meeting_date"]):
            key = (event["meeting_date"], event["title"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(event)
        return unique

    async def fetch_currency_ids(self) -> dict[str, str]:
        """Соответствие буквенного кода валюты внутреннему коду ЦБ (R01235 и т.п.).

        Внутренний код нужен для запроса истории курса: XML_dynamic принимает
        только его, а не ISO-код.
        """
        response = await self.get("/scripts/XML_daily.asp")
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise SourceError(f"cbr: не удалось разобрать XML курсов: {exc}") from exc

        mapping: dict[str, str] = {}
        for node in root:
            if _localname(node.tag) != "Valute":
                continue
            code = _find_text(node, "CharCode")
            internal = node.attrib.get("ID")
            if code and internal:
                mapping[code] = internal
        return mapping

    async def fetch_fx_history(
        self,
        code: str,
        internal_id: str,
        from_date: date | None = None,
        to_date_: date | None = None,
    ) -> list[dict[str, Any]]:
        """История официального курса валюты за период."""
        to_date_ = to_date_ or date.today()
        from_date = from_date or (to_date_ - timedelta(days=365))

        response = await self.get(
            "/scripts/XML_dynamic.asp",
            date_req1=from_date.strftime("%d/%m/%Y"),
            date_req2=to_date_.strftime("%d/%m/%Y"),
            VAL_NM_RQ=internal_id,
        )
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise SourceError(
                f"cbr: не удалось разобрать историю курса {code}: {exc}"
            ) from exc

        series: list[dict[str, Any]] = []
        for node in root:
            if _localname(node.tag) != "Record":
                continue
            rate_date = to_date(node.attrib.get("Date"))
            value = to_float(_find_text(node, "Value"))
            if rate_date is None or value is None:
                continue
            series.append(
                {
                    "source": "cbr",
                    "code": code,
                    "name": None,
                    "nominal": to_int(_find_text(node, "Nominal")) or 1,
                    "value": value,
                    "rate_date": rate_date,
                }
            )
        return sorted(series, key=lambda item: item["rate_date"])
