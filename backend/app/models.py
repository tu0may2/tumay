"""ORM-модели казначейского хранилища."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    """Справочник инструментов (акции, облигации, индексы, валютные пары)."""

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("secid", "board", name="uq_instrument_secid_board"),
        Index("ix_instrument_kind", "kind"),
        Index("ix_instrument_isin", "isin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    secid: Mapped[str] = mapped_column(String(64), index=True)
    board: Mapped[str] = mapped_column(String(16))
    engine: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(32))
    # share | bond | index | currency
    kind: Mapped[str] = mapped_column(String(16))

    isin: Mapped[str | None] = mapped_column(String(24))
    short_name: Mapped[str | None] = mapped_column(String(128))
    full_name: Mapped[str | None] = mapped_column(String(256))
    reg_number: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(8))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    face_value: Mapped[float | None] = mapped_column(Float)
    face_unit: Mapped[str | None] = mapped_column(String(8))
    issue_size: Mapped[float | None] = mapped_column(Float)
    list_level: Mapped[int | None] = mapped_column(Integer)
    sector: Mapped[str | None] = mapped_column(String(64))
    sec_type: Mapped[str | None] = mapped_column(String(16))

    # Только для облигаций
    maturity_date: Mapped[date | None] = mapped_column(Date)
    offer_date: Mapped[date | None] = mapped_column(Date)
    coupon_percent: Mapped[float | None] = mapped_column(Float)
    coupon_value: Mapped[float | None] = mapped_column(Float)
    coupon_period: Mapped[int | None] = mapped_column(Integer)
    next_coupon_date: Mapped[date | None] = mapped_column(Date)
    accrued_interest: Mapped[float | None] = mapped_column(Float)
    bond_type: Mapped[str | None] = mapped_column(String(64))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    quotes: Mapped[list["Quote"]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan"
    )
    bars: Mapped[list["Bar"]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        return self.short_name or self.full_name or self.secid


class Quote(Base):
    """Срез торговых данных на момент времени: цена, объёмы, спред, доходность."""

    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("instrument_id", "ts", name="uq_quote_instrument_ts"),
        Index("ix_quote_instrument_ts", "instrument_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    trade_date: Mapped[date | None] = mapped_column(Date, index=True)

    last: Mapped[float | None] = mapped_column(Float)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    prev_close: Mapped[float | None] = mapped_column(Float)
    wa_price: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)

    bid: Mapped[float | None] = mapped_column(Float)
    offer: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[float | None] = mapped_column(Float)
    bid_depth: Mapped[float | None] = mapped_column(Float)
    offer_depth: Mapped[float | None] = mapped_column(Float)

    # Объёмы: VOLTODAY (штуки) и VALTODAY (рубли оборота)
    volume: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    num_trades: Mapped[int | None] = mapped_column(Integer)
    capitalization: Mapped[float | None] = mapped_column(Float)

    # Облигационные метрики
    yield_pct: Mapped[float | None] = mapped_column(Float)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    z_spread_bp: Mapped[float | None] = mapped_column(Float)
    g_spread_bp: Mapped[float | None] = mapped_column(Float)

    trading_status: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16), default="moex")

    instrument: Mapped[Instrument] = relationship(back_populates="quotes")


class Bar(Base):
    """Дневная история торгов — база для анализа объёмов и волатильности."""

    __tablename__ = "bars"
    __table_args__ = (
        UniqueConstraint("instrument_id", "trade_date", name="uq_bar_instrument_date"),
        Index("ix_bar_instrument_date", "instrument_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True
    )
    trade_date: Mapped[date] = mapped_column(Date, index=True)

    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    wa_price: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    num_trades: Mapped[int | None] = mapped_column(Integer)

    instrument: Mapped[Instrument] = relationship(back_populates="bars")


class FxRate(Base):
    """Курсы валют: ЦБ РФ (официальные) и биржевые с MOEX."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("source", "code", "rate_date", name="uq_fx_source_code_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    nominal: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[float] = mapped_column(Float)
    rate_date: Mapped[date] = mapped_column(Date, index=True)


class MacroRate(Base):
    """Макро-ставки: ключевая ставка ЦБ, RUONIA и прочие индикаторы."""

    __tablename__ = "macro_rates"
    __table_args__ = (
        UniqueConstraint("code", "rate_date", name="uq_macro_code_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    value: Mapped[float] = mapped_column(Float)
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(16), default="cbr")


class CurvePoint(Base):
    """Точка кривой бескупонной доходности (КБД МосБиржи)."""

    __tablename__ = "curve_points"
    __table_args__ = (
        UniqueConstraint("curve_date", "period_years", name="uq_curve_date_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    curve_date: Mapped[date] = mapped_column(Date, index=True)
    period_years: Mapped[float] = mapped_column(Float)
    value: Mapped[float] = mapped_column(Float)


class CorpAction(Base):
    """Корпоративные действия и денежные потоки по бумагам (данные НРД)."""

    __tablename__ = "corp_actions"
    __table_args__ = (
        UniqueConstraint(
            "isin", "action_type", "action_date", name="uq_ca_isin_type_date"
        ),
        Index("ix_ca_isin_date", "isin", "action_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    isin: Mapped[str] = mapped_column(String(24), index=True)
    secid: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(256))
    # coupon | amortization | offer | dividend
    action_type: Mapped[str] = mapped_column(String(24), index=True)
    action_date: Mapped[date] = mapped_column(Date, index=True)
    record_date: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date | None] = mapped_column(Date)

    value: Mapped[float | None] = mapped_column(Float)
    value_rub: Mapped[float | None] = mapped_column(Float)
    value_pct: Mapped[float | None] = mapped_column(Float)
    face_value: Mapped[float | None] = mapped_column(Float)
    face_unit: Mapped[str | None] = mapped_column(String(8))
    # Первоисточник записи: nsd (через раскрытие) / moex
    source: Mapped[str] = mapped_column(String(16), default="nsd")


class Deal(Base):
    """Сделка казначейства — основа для позиций и P&L."""

    __tablename__ = "deals"
    __table_args__ = (Index("ix_deal_secid_date", "secid", "trade_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio: Mapped[str] = mapped_column(String(64), default="Основной", index=True)
    secid: Mapped[str] = mapped_column(String(64), index=True)
    # buy | sell
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    # Для облигаций — НКД на дату сделки
    accrued_interest: Mapped[float | None] = mapped_column(Float, default=0.0)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    counterparty: Mapped[str | None] = mapped_column(String(128))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CollectionRun(Base):
    """Журнал запусков сбора данных — прозрачность и диагностика источников."""

    __tablename__ = "collection_runs"
    __table_args__ = (Index("ix_run_source_started", "source", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    task: Mapped[str] = mapped_column(String(64))
    # running | success | error
    status: Mapped[str] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
