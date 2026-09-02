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


class DealBulkCreate(BaseModel):
    """Пакетное добавление сделок из витрины бумаг."""

    deals: list[DealCreate] = Field(..., min_length=1, max_length=200)


class DealBulkResult(BaseModel):
    """Итог пакетного добавления: что прошло, что нет."""

    created: list[DealRead]
    errors: list[dict]
    created_count: int
    error_count: int


# ----------------------------------------------------------------------
# Деньги
# ----------------------------------------------------------------------
class CashAccountCreate(BaseModel):
    """Счёт казначейства."""

    name: str = Field(..., min_length=1, max_length=128)
    currency: str = Field("RUB", min_length=3, max_length=8)
    portfolio: str = Field("Основной", min_length=1, max_length=64)
    bank: str | None = Field(None, max_length=128)
    comment: str | None = None

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class CashAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    currency: str
    portfolio: str
    bank: str | None


class CashFlowCreate(BaseModel):
    """Движение по счёту. Отрицательная сумма — списание."""

    account_id: int
    amount: float = Field(..., description="Положительная — приход, отрицательная — расход")
    flow_date: date = Field(default_factory=date.today)
    kind: Literal[
        "deposit", "withdrawal", "trade", "coupon", "fee", "tax", "transfer", "other"
    ] = "other"
    is_planned: bool = False
    comment: str | None = None


class CashFlowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    flow_date: date
    amount: float
    kind: str
    is_planned: bool
    comment: str | None


class PlacementCreate(BaseModel):
    """Депозит, РЕПО или кредит."""

    kind: Literal["deposit", "repo", "reverse_repo", "loan"]
    amount: float = Field(..., gt=0)
    rate: float = Field(..., ge=0, description="Ставка годовых, %")
    start_date: date
    end_date: date
    currency: str = Field("RUB", min_length=3, max_length=8)
    portfolio: str = Field("Основной", min_length=1, max_length=64)
    account_id: int | None = None
    counterparty: str | None = Field(None, max_length=128)
    collateral_secid: str | None = Field(None, max_length=64)
    comment: str | None = None

    @field_validator("currency")
    @classmethod
    def _upper_ccy(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("end_date")
    @classmethod
    def _after_start(cls, value: date, info) -> date:
        start = info.data.get("start_date")
        if start and value <= start:
            raise ValueError("Дата окончания должна быть позже даты начала")
        return value


class PlacementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    amount: float
    rate: float
    currency: str
    start_date: date
    end_date: date
    counterparty: str | None
    closed: bool


# ----------------------------------------------------------------------
# Импорт, доступ, уведомления
# ----------------------------------------------------------------------
class ImportApply(BaseModel):
    """Подтверждение импорта разобранных сделок."""

    deals: list[dict] = Field(..., min_length=1, max_length=5000)


class PortfolioImportApply(BaseModel):
    """Подтверждение импорта книги портфеля.

    Все три списка необязательны: книга может описывать только состав, только
    журнал операций или только виды учёта портфелей.
    """

    portfolios: list[dict] = Field(default_factory=list, max_length=200)
    holdings: list[dict] = Field(default_factory=list, max_length=5000)
    deals: list[dict] = Field(default_factory=list, max_length=5000)
    #: Стереть прежние сделки затронутых портфелей перед записью — режим
    #: «файл содержит полный текущий состав». Без него повторная загрузка
    #: тех же остатков удвоит позицию
    replace_existing: bool = False


class CalendarCellSave(BaseModel):
    """Сумма, вписанная в ячейку платёжного календаря.

    ``amount = null`` стирает ввод и возвращает ячейку к тому, что дала
    выгрузка. Ноль этого не делает: пустая ячейка и ноль — разные
    утверждения, и «оборота не было» надо уметь сказать явно.
    """

    entry_date: date
    row_code: str = Field(..., min_length=1, max_length=48)
    amount: float | None = None
    comment: str | None = Field(None, max_length=500)


class LedgerImportApply(BaseModel):
    """Подтверждение загрузки выгрузки по лицевым счетам на дату.

    Дата обязательна: именно она определяет, в какой день платёжного календаря
    лягут обороты, и угадывать её на этом шаге уже поздно — в предпросмотре
    человек её видел и мог поправить.
    """

    load_date: date
    rows: list[dict] = Field(..., min_length=1, max_length=5000)
    source_file: str | None = Field(None, max_length=255)


class RatioInputSave(BaseModel):
    """Балансовые составляющие нормативов ликвидности.

    Все поля необязательны: часть данных может появиться позже, а норматив,
    для которого чего-то не хватает, честнее показать нерассчитанным, чем
    подставить ноль и получить мнимое нарушение.
    """

    as_of: date | None = None
    ovm: float | None = Field(None, ge=0)
    ovm_min: float | None = Field(None, ge=0)
    ovt: float | None = Field(None, ge=0)
    ovt_min: float | None = Field(None, ge=0)
    lam_other: float | None = Field(None, ge=0)
    lat_other: float | None = Field(None, ge=0)
    krd: float | None = Field(None, ge=0)
    capital: float | None = Field(None, ge=0)
    od: float | None = Field(None, ge=0)
    o_min: float | None = Field(None, ge=0)
    comment: str | None = Field(None, max_length=500)


class PortfolioAccounting(BaseModel):
    """Вид учёта портфеля."""

    name: str = Field(..., min_length=1, max_length=64)
    accounting_type: Literal["trading", "htm"]


class LoginRequest(BaseModel):
    login: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)


class UserCreate(BaseModel):
    login: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6)
    role: Literal["viewer", "trader", "admin"] = "viewer"
    full_name: str | None = Field(None, max_length=128)


class PasswordChange(BaseModel):
    """Смена пароля: себе — без логина, чужому — с логином (только админ)."""

    password: str = Field(..., min_length=6, description="Новый пароль")
    login: str | None = Field(
        None, max_length=64, description="Чей пароль менять; по умолчанию свой"
    )


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    login: str
    full_name: str | None
    role: str
    active: bool


class NotificationRuleCreate(BaseModel):
    """Правило доставки уведомлений."""

    name: str = Field(..., min_length=1, max_length=128)
    webhook_url: str = Field(..., min_length=8, max_length=512)
    events: list[
        Literal["limit_breach", "offer_soon", "cash_gap", "price_move", "volume_anomaly"]
    ] = Field(default_factory=lambda: ["limit_breach"])


class NotificationRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    webhook_url: str
    events: str
    enabled: bool
