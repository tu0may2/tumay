"""Базовый HTTP-клиент для внешних источников и утилиты разбора значений."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# Значения-заглушки, которые источники отдают вместо пустого поля
_EMPTY_TOKENS = {"", "-", "0000-00-00", "null", "None"}


class SourceError(RuntimeError):
    """Ошибка получения данных из внешнего источника."""


class HttpSource:
    """Асинхронный HTTP-клиент с повторами, backoff и ограничением параллелизма."""

    #: Человекочитаемое имя источника, используется в журнале сбора
    name: str = "http"

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(settings.http_concurrency)

    async def __aenter__(self) -> "HttpSource":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=settings.http_timeout,
                follow_redirects=True,
                headers={"User-Agent": "treasury-terminal/1.0"},
            )
            self._owns_client = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise SourceError(
                f"{self.name}: клиент не инициализирован, используйте async with"
            )
        return self._client

    async def get(self, path: str, **params: Any) -> httpx.Response:
        """GET с экспоненциальным backoff. Повторяем сетевые сбои и 5xx/429."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(settings.http_retries):
            try:
                async with self._semaphore:
                    response = await self.client.get(url, params=params or None)
                if response.status_code >= 500 or response.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.StreamError) as exc:
                last_error = exc
                # На последней попытке не спим — сразу отдаём ошибку наверх
                if attempt == settings.http_retries - 1:
                    break
                delay = settings.http_backoff**attempt
                logger.warning(
                    "%s: попытка %s/%s не удалась (%s), повтор через %.1f с",
                    self.name,
                    attempt + 1,
                    settings.http_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        raise SourceError(f"{self.name}: не удалось получить {url}: {last_error}")

    async def get_json(self, path: str, **params: Any) -> dict[str, Any]:
        response = await self.get(path, **params)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{self.name}: некорректный JSON в ответе {path}") from exc


def to_float(value: Any) -> float | None:
    """Мягкое приведение к float: пустые значения дают None, а не исключение."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "")
    if text in _EMPTY_TOKENS:
        return None
    # ЦБ РФ отдаёт числа с запятой в качестве десятичного разделителя
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return int(number) if number is not None else None


def to_date(value: Any) -> date | None:
    """Разбор дат в форматах, которые встречаются у MOEX, ЦБ и НРД."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = to_datetime(value)
    return parsed.date() if parsed is not None else None


def to_datetime(value: Any) -> datetime | None:
    """Разбор даты/времени, включая ISO со смещением (ЦБ отдаёт +03:00)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip() if value is not None else ""
    if text in _EMPTY_TOKENS:
        return None

    # fromisoformat покрывает смещения и микросекунды; 'Z' он не понимает
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        # Приводим к наивному времени: смешивать tz-aware и naive в БД нельзя
        return parsed.replace(tzinfo=None)

    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def rows_to_dicts(block: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Развернуть блок MOEX ISS вида {"columns": [...], "data": [[...]]} в словари."""
    if not block:
        return []
    columns = block.get("columns") or []
    data = block.get("data") or []
    return [dict(zip(columns, row)) for row in data]
