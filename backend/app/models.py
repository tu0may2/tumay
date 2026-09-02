"""ORM-модели казначейского хранилища."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
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
    #: Вид бумаги словами: ofz_bond, corporate_bond, common_share…
    #: В биржевом срезе есть только однобуквенный код, поэтому вид приходит
    #: из массового справочника бумаг и заполняется отдельным шагом сбора
    security_type: Mapped[str | None] = mapped_column(String(32), index=True)
    # Эмитент нужен для лимитов: несколько выпусков одного заёмщика
    # складываются в общий риск на него
    issuer: Mapped[str | None] = mapped_column(String(256), index=True)
    issuer_inn: Mapped[str | None] = mapped_column(String(16))

    # Только для облигаций
    maturity_date: Mapped[date | None] = mapped_column(Date)
    offer_date: Mapped[date | None] = mapped_column(Date)
    coupon_percent: Mapped[float | None] = mapped_column(Float)
    coupon_value: Mapped[float | None] = mapped_column(Float)
    coupon_period: Mapped[int | None] = mapped_column(Integer)
    next_coupon_date: Mapped[date | None] = mapped_column(Date)
    accrued_interest: Mapped[float | None] = mapped_column(Float)
    bond_type: Mapped[str | None] = mapped_column(String(64))
    bond_subtype: Mapped[str | None] = mapped_column(String(64))

    # К чему привязан купон флоатера: код базы (RREFKEYR, RUONIA, RUSFAR…)
    # и надбавка к ней в процентных пунктах. В биржевом срезе этих полей нет,
    # они приходят из карточки выпуска и заполняются сборщиком отдельно
    coupon_benchmark: Mapped[str | None] = mapped_column(String(32), index=True)
    coupon_margin: Mapped[float | None] = mapped_column(Float)
    #: Когда карточку выпуска смотрели в последний раз — чтобы не ходить
    #: за одним и тем же по кругу и видеть, что ещё не заполнено
    benchmark_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

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
    # Средневзвешенная цена предыдущего торгового дня — база расчёта
    # рыночной цены и ориентир при оценке сегодняшних уровней
    prev_wa_price: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)
    # Изменение к СВЦ предыдущего дня, %
    change_to_prev_wap_pct: Mapped[float | None] = mapped_column(Float)

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
    # Накопленный купонный доход на одну облигацию, в валюте номинала.
    # Относится к дате расчётов settle_date, а не к дате среза: в режиме T+1
    # это следующий торговый день. НКД на другие даты считает services/accrual.
    accrued_interest: Mapped[float | None] = mapped_column(Float)
    settle_date: Mapped[date | None] = mapped_column(Date)

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
    legal_close: Mapped[float | None] = mapped_column(Float)
    #: Средневзвешенная цена дня — то, что MOEX показывает как СВЦ
    wa_price: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    num_trades: Mapped[int | None] = mapped_column(Integer)

    # Облигационные поля дня (у акций остаются пустыми)
    accrued_interest: Mapped[float | None] = mapped_column(Float)
    yield_close: Mapped[float | None] = mapped_column(Float)
    yield_at_wap: Mapped[float | None] = mapped_column(Float)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    face_value: Mapped[float | None] = mapped_column(Float)
    coupon_percent: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))

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


class RateMeeting(Base):
    """Заседание Совета директоров Банка России по ключевой ставке.

    Календарь публикуется на год вперёд, поэтому по нему видно и когда
    ждать следующего решения, и чем закончились прошлые.
    """

    __tablename__ = "rate_meetings"
    __table_args__ = (
        UniqueConstraint("meeting_date", "title", name="uq_meeting_date_title"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(256))
    #: regular — плановое, extraordinary — внеочередное, other — прочие события
    #: календаря (доклад о ДКП, резюме обсуждения)
    kind: Mapped[str] = mapped_column(String(16), default="regular")
    #: Заседание с публикацией среднесрочного прогноза — «опорное»
    with_forecast: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Ссылки на пресс-релиз, прогноз и пресс-конференцию, JSON-строкой
    links: Mapped[str | None] = mapped_column(Text)
    #: Ставка, установленная на этом заседании, и её изменение в п.п.
    rate: Mapped[float | None] = mapped_column(Float)
    rate_change: Mapped[float | None] = mapped_column(Float)


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


class RatioInput(Base):
    """Балансовые составляющие нормативов ликвидности Н2, Н3, Н4.

    Терминал ведёт портфель бумаг и деньги, но не весь баланс банка:
    обязательства до востребования, капитал и долгосрочные требования он
    знать не может. Поэтому они вводятся руками и хранятся здесь — чтобы не
    вбивать их заново при каждом расчёте.

    Одна строка на дату: нормативы считаются на отчётную дату, и полезно
    видеть, как они менялись.
    """

    __tablename__ = "ratio_inputs"
    __table_args__ = (UniqueConstraint("as_of", name="uq_ratio_input_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)

    # --- Н2, мгновенная ликвидность ---
    #: Овм — обязательства до востребования
    ovm: Mapped[float | None] = mapped_column(Float)
    #: Овм* — минимальный совокупный остаток по счетам до востребования
    ovm_min: Mapped[float | None] = mapped_column(Float)

    # --- Н3, текущая ликвидность ---
    #: Овт — обязательства до востребования и сроком до 30 дней
    ovt: Mapped[float | None] = mapped_column(Float)
    #: Овт* — минимальный совокупный остаток по таким счетам
    ovt_min: Mapped[float | None] = mapped_column(Float)
    #: Лат сверх портфеля: прочие ликвидные активы до 30 дней
    lat_other: Mapped[float | None] = mapped_column(Float)
    #: Лам сверх портфеля: прочие высоколиквидные активы
    lam_other: Mapped[float | None] = mapped_column(Float)

    # --- Н4, долгосрочная ликвидность ---
    #: Крд — кредитные требования со сроком свыше 365 дней
    krd: Mapped[float | None] = mapped_column(Float)
    #: К — собственные средства (капитал)
    capital: Mapped[float | None] = mapped_column(Float)
    #: ОД — обязательства со сроком свыше 365 дней
    od: Mapped[float | None] = mapped_column(Float)
    #: О* — минимальный совокупный остаток по счетам до 365 дней
    o_min: Mapped[float | None] = mapped_column(Float)

    comment: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CbrCollateral(Base):
    """Бумага из списка принимаемых в обеспечение по кредитам Банка России.

    В обиходе — «ломбардный список». Держим отдельной таблицей, а не полями
    в справочнике инструментов: список публикуется по ISIN и не совпадает с
    тем, что торгуется на выбранных нами площадках — в нём есть выпуски,
    которых у нас нет, и наоборот. Отдельная таблица к тому же хранит дату
    публикации: ЦБ пересматривает и состав, и оценки.
    """

    __tablename__ = "cbr_collateral"

    id: Mapped[int] = mapped_column(primary_key=True)
    isin: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    reg_number: Mapped[str | None] = mapped_column(String(32))
    issuer: Mapped[str | None] = mapped_column(String(256))
    #: Цена, по которой ЦБ оценивает бумагу, в процентах от номинала
    price_pct: Mapped[float | None] = mapped_column(Float)
    #: Стоимость одной бумаги по методике ЦБ, рублей
    value_rub: Mapped[float | None] = mapped_column(Float)
    #: Поправочный коэффициент: доля стоимости, которую ЦБ примет в
    #: обеспечение. 0,98 — почти полная стоимость, 0,90 — со скидкой в 10%
    haircut: Mapped[float | None] = mapped_column(Float)
    #: Полная запись коэффициента, когда ЦБ задал разные значения под разные
    #: виды кредитов: по ней видно, откуда взялось число выше
    haircut_note: Mapped[str | None] = mapped_column(Text)
    #: ОМ — основной механизм, ДМ — дополнительный
    mechanism: Mapped[str | None] = mapped_column(String(8), index=True)
    #: Раздел списка: гособлигации, субфедеральные, корпоративные, ипотечные
    group_title: Mapped[str | None] = mapped_column(String(256))
    maturity_date: Mapped[date | None] = mapped_column(Date)
    #: На какую дату опубликован список
    as_of: Mapped[date | None] = mapped_column(Date, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


#: Виды учёта портфеля. Разница не косметическая: торговый портфель
#: переоценивается по рынку, а удерживаемый до погашения ведётся по
#: амортизированной стоимости — рыночная цена для него справочная.
ACCOUNTING_TRADING = "trading"
ACCOUNTING_HTM = "htm"


class Portfolio(Base):
    """Портфель казначейства и его вид учёта.

    До появления этой таблицы портфель был просто текстовой меткой на сделке,
    и такие «безымянные» портфели продолжают работать — они считаются
    торговыми. Запись здесь нужна, когда у портфеля есть свойство, которого
    в метке не выразишь: прежде всего вид учёта.
    """

    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: trading — торговый (переоценка по рынку),
    #: htm — до погашения (амортизированная стоимость)
    accounting_type: Mapped[str] = mapped_column(
        String(16), default=ACCOUNTING_TRADING
    )
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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
    # Курс валюты сделки к рублю на дату сделки. Нужен, чтобы отделить
    # ценовой результат от валютного: без него переоценка валютной бумаги
    # смешивает движение цены и движение курса.
    fx_rate: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Limit(Base):
    """Лимит казначейства: ограничение, которое портфель не должен нарушать."""

    __tablename__ = "limits"
    __table_args__ = (
        UniqueConstraint("portfolio", "kind", "target", name="uq_limit_scope"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio: Mapped[str] = mapped_column(String(64), default="Основной", index=True)
    #: instrument_share | issuer_share | currency_share | list_level_share
    #: | illiquid_share | duration_max | duration_min | position_value
    kind: Mapped[str] = mapped_column(String(32), index=True)
    #: К чему относится: код бумаги, имя эмитента, код валюты, уровень листинга.
    #: Пусто для лимитов на портфель целиком (дюрация, доля неликвида).
    target: Mapped[str | None] = mapped_column(String(256))
    #: Предельное значение: доля в процентах, годы дюрации или сумма в рублях
    value: Mapped[float] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SavedScreen(Base):
    """Сохранённый набор фильтров, чтобы не набирать отбор заново."""

    __tablename__ = "saved_screens"
    __table_args__ = (UniqueConstraint("view", "name", name="uq_screen_view_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    #: К какому экрану относится: bonds | instruments
    view: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(128))
    #: Значения фильтров как есть, JSON-строкой
    params: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WatchItem(Base):
    """Бумага в списке наблюдения."""

    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("secid", "watchlist", name="uq_watch_secid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    secid: Mapped[str] = mapped_column(String(64), index=True)
    watchlist: Mapped[str] = mapped_column(String(64), default="Основной", index=True)
    note: Mapped[str | None] = mapped_column(Text)
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


# ----------------------------------------------------------------------
# Денежная позиция
# ----------------------------------------------------------------------
class CashAccount(Base):
    """Счёт казначейства: рублёвый или валютный."""

    __tablename__ = "cash_accounts"
    __table_args__ = (UniqueConstraint("name", "currency", name="uq_account_name_ccy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8), default="RUB", index=True)
    portfolio: Mapped[str] = mapped_column(String(64), default="Основной", index=True)
    bank: Mapped[str | None] = mapped_column(String(128))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CashFlow(Base):
    """Движение денег по счёту.

    Плановые движения (``is_planned``) не меняют текущий остаток, но попадают
    в платёжный календарь — так виден кассовый разрыв до того, как он случится.
    """

    __tablename__ = "cash_flows"
    __table_args__ = (Index("ix_cashflow_account_date", "account_id", "flow_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("cash_accounts.id", ondelete="CASCADE"), index=True
    )
    flow_date: Mapped[date] = mapped_column(Date, index=True)
    #: Положительная сумма — поступление, отрицательная — списание
    amount: Mapped[float] = mapped_column(Float)
    #: deposit | withdrawal | trade | coupon | fee | tax | transfer | other
    kind: Mapped[str] = mapped_column(String(24), default="other", index=True)
    is_planned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LedgerRow(Base):
    """Строка выгрузки по лицевым счетам на дату — сырьё платёжного календаря.

    Храним выгрузку как есть, а статью календаря выводим при чтении по номеру
    счёта. Так исправление правила разноски чинит сразу все загруженные дни,
    не требуя перезагружать файлы: иначе ошибка в классификаторе навсегда
    застывала бы в базе, и «поправить одну статью» означало бы поднимать все
    старые выгрузки заново.
    """

    __tablename__ = "ledger_rows"
    __table_args__ = (
        UniqueConstraint("load_date", "account", name="uq_ledger_date_account"),
        Index("ix_ledger_date", "load_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Дата, на которую загружена выгрузка
    load_date: Mapped[date] = mapped_column(Date, index=True)
    #: Номер лицевого счёта, 20 знаков
    account: Mapped[str] = mapped_column(String(32), index=True)
    account_name: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")

    #: Входящий остаток: дебетовый положителен, кредитовый отрицателен
    opening_balance: Mapped[float] = mapped_column(Float, default=0.0)
    debit_turnover: Mapped[float] = mapped_column(Float, default=0.0)
    credit_turnover: Mapped[float] = mapped_column(Float, default=0.0)
    closing_balance: Mapped[float] = mapped_column(Float, default=0.0)

    source_file: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CalendarEntry(Base):
    """Сумма, вписанная в платёжный календарь руками.

    Хранится отдельно от выгрузки, а не поверх неё: выгрузка — это факт дня,
    и затирать её вводом значило бы терять то, что в действительности прошло
    по счетам. Введённое значение перекрывает вычисленное при показе, но
    исходная сумма никуда не девается — её видно в подсказке ячейки, и
    достаточно стереть ввод, чтобы вернуться к факту.

    Главное назначение — будущие дни, по которым выгрузки ещё нет: план по
    зарплате, налогам и погашениям вписывают заранее, иначе кассовый разрыв
    обнаружится в день платежа.
    """

    __tablename__ = "calendar_entries"
    __table_args__ = (
        UniqueConstraint("entry_date", "row_code", name="uq_calendar_entry"),
        Index("ix_calendar_entry_date", "entry_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    #: Код строки календаря из calendar_matrix.ROWS
    row_code: Mapped[str] = mapped_column(String(48), index=True)
    amount: Mapped[float] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Placement(Base):
    """Размещение или привлечение денег: депозит, РЕПО, обратное РЕПО."""

    __tablename__ = "placements"
    __table_args__ = (Index("ix_placement_dates", "start_date", "end_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    #: deposit | repo | reverse_repo | loan
    kind: Mapped[str] = mapped_column(String(24), index=True)
    portfolio: Mapped[str] = mapped_column(String(64), default="Основной", index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("cash_accounts.id", ondelete="SET NULL")
    )
    counterparty: Mapped[str | None] = mapped_column(String(128))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    #: Ставка годовых, %
    rate: Mapped[float] = mapped_column(Float)
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    #: Обеспечение по РЕПО, если есть
    collateral_secid: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ----------------------------------------------------------------------
# История, доступ и уведомления
# ----------------------------------------------------------------------
class PortfolioSnapshot(Base):
    """Снимок стоимости портфеля на дату — основа кривой доходности."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio", "snapshot_date", name="uq_snapshot_portfolio_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio: Mapped[str] = mapped_column(String(64), default="", index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)

    total_value: Mapped[float] = mapped_column(Float)
    total_cost: Mapped[float | None] = mapped_column(Float)
    cash_value: Mapped[float | None] = mapped_column(Float)
    price_pnl: Mapped[float | None] = mapped_column(Float)
    fx_pnl: Mapped[float | None] = mapped_column(Float)
    coupon_result: Mapped[float | None] = mapped_column(Float)
    net_pnl: Mapped[float | None] = mapped_column(Float)
    duration_years: Mapped[float | None] = mapped_column(Float)
    yield_pct: Mapped[float | None] = mapped_column(Float)
    positions: Mapped[int | None] = mapped_column(Integer)
    #: Внешние вводы и выводы за день — нужны для корректной доходности
    net_flow: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    """Пользователь терминала."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256))
    #: viewer | trader | admin
    role: Mapped[str] = mapped_column(String(16), default="viewer", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime)


class Session_(Base):
    """Активная сессия входа."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AuditRecord(Base):
    """Кто, что и когда изменил."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_login: Mapped[str | None] = mapped_column(String(64), index=True)
    #: create | update | delete | import | login
    action: Mapped[str] = mapped_column(String(24), index=True)
    entity: Mapped[str] = mapped_column(String(48), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class NotificationRule(Base):
    """Куда и о чём слать уведомления."""

    __tablename__ = "notification_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    #: Адрес вебхука: подходит Telegram, Slack, Mattermost и любой приёмник JSON
    webhook_url: Mapped[str] = mapped_column(String(512))
    #: limit_breach | offer_soon | price_move | volume_anomaly | cash_gap
    events: Mapped[str] = mapped_column(String(256), default="limit_breach")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
