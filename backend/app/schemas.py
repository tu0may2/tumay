"""Схемы запросов и ответов API."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DealCreate(BaseModel):
    """Регистрация сделки казначейства."""

    secid: str = Field(..., min_length=1, max_length=64, description="Код инструмента")
    side: Literal["buy", "sell"] = Field(..., description="Направление сделки")
    quantity: float = Field(..., gt=0, description="Количество бумаг")
    price: float = Field(..., gt=0, description="Цена: руб. для акций, % от номинала для облигаций")
    trade_date: date = Field(default_factory=date.today)
    portfolio: str = Field("Основной", min_length=1, max_length=64)
    accrued_interest: float = Field(0.0, ge=0, description="НКД на одну облигацию")
    fee: float = Field(0.0, ge=0, description="Комиссия по сделке")
    counterparty: str | None = Field(None, max_length=128)
    comment: str | None = None

    @field_validator("secid")
    @classmethod
    def _normalize_secid(cls, value: str) -> str:
        return value.strip().upper()


class DealRead(BaseModel):
    """Сделка в ответе API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio: str
    secid: str
    side: str
    quantity: float
    price: float
    accrued_interest: float | None
    fee: float
    trade_date: date
    counterparty: str | None
    comment: str | None


class CollectRequest(BaseModel):
    """Ручной запуск сбора данных."""

    with_history: bool = Field(True, description="Догружать историю торгов")


class HealthResponse(BaseModel):
    status: str
    instruments: int
    quotes: int
    last_collection: str | None


class LimitCreate(BaseModel):
    """Установка лимита казначейства."""

    kind: str = Field(..., description="Вид лимита из /api/limits/kinds")
    value: float = Field(..., gt=0, description="Предельное значение")
    target: str | None = Field(None, max_length=256, description="К чему относится")
    portfolio: str = Field("Основной", min_length=1, max_length=64)
    comment: str | None = None


class LimitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio: str
    kind: str
    target: str | None
    value: float
    comment: str | None
    enabled: bool


class TradePreview(BaseModel):
    """Гипотетическая сделка для проверки лимитов."""

    secid: str = Field(..., min_length=1, max_length=64)
    quantity: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    portfolio: str | None = None

    @field_validator("secid")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class SavedScreenCreate(BaseModel):
    """Сохранение набора фильтров."""

    view: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=128)
    params: dict = Field(default_factory=dict)


class SavedScreenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    view: str
    name: str
    params: str


class WatchItemCreate(BaseModel):
    """Добавление бумаги в список наблюдения."""

    secid: str = Field(..., min_length=1, max_length=64)
    watchlist: str = Field("Основной", min_length=1, max_length=64)
    note: str | None = None

    @field_validator("secid")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class WatchItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    secid: str
    watchlist: str
    note: str | None
