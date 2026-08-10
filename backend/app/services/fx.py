"""Приведение сумм к рублю.

Портфель казначейства держит и рублёвые, и валютные выпуски (замещающие
облигации, юаневые ОФЗ). Складывать их напрямую нельзя — сначала переоценка
по курсу. Курс берём официальный, от Банка России.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FxRate

#: Обозначения рубля, встречающиеся в данных биржи
RUB_CODES = {"SUR", "RUB", "RUR", ""}

#: Валюта расчётов терминала
BASE_CURRENCY = "RUB"


def is_rub(code: str | None) -> bool:
    return (code or "").upper() in RUB_CODES


class FxBook:
    """Кэш курсов: даты сделок повторяются, ходить в БД на каждую не нужно."""

    def __init__(self, session: Session):
        self._session = session
        self._by_code: dict[str, list[tuple[date, float]]] = {}
        self._loaded: set[str] = set()

    def _load(self, code: str) -> list[tuple[date, float]]:
        """Ряд курсов валюты по возрастанию даты."""
        if code in self._loaded:
            return self._by_code.get(code, [])

        rows = self._session.execute(
            select(FxRate.rate_date, FxRate.value, FxRate.nominal)
            .where(FxRate.source == "cbr", FxRate.code == code)
            .order_by(FxRate.rate_date)
        ).all()
        # Курс ЦБ даётся за номинал (например, 100 иен) — приводим к единице
        series = [(row[0], row[1] / (row[2] or 1)) for row in rows]
        self._by_code[code] = series
        self._loaded.add(code)
        return series

    def rate(self, code: str | None, on_date: date | None = None) -> float | None:
        """Курс валюты к рублю на дату.

        Берём последний известный курс на эту дату или раньше: биржа торгует
        в выходные и праздники, когда ЦБ курс не публикует.
        """
        if is_rub(code):
            return 1.0

        series = self._load((code or "").upper())
        if not series:
            return None
        if on_date is None:
            return series[-1][1]

        found: float | None = None
        for rate_date, value in series:
            if rate_date <= on_date:
                found = value
            else:
                break
        # Если сделка старше первого известного курса — берём самый ранний
        return found if found is not None else series[0][1]

    def to_rub(
        self, amount: float | None, code: str | None, on_date: date | None = None
    ) -> float | None:
        if amount is None:
            return None
        rate = self.rate(code, on_date)
        return amount * rate if rate is not None else None

    def known_currencies(self) -> list[str]:
        codes = self._session.execute(
            select(FxRate.code).where(FxRate.source == "cbr").distinct()
        ).scalars()
        return sorted({code for code in codes})


#: Соглашение MOEX о единицах измерения, проверенное на живых данных:
#:
#: * цена бумаги — проценты от номинала, номинал в ``FACEUNIT``;
#: * НКД (``ACCRUEDINT``/``ACCINT``) — уже в валюте расчётов, то есть в рублях;
#: * купон из графика выплат (``CorpAction.value``) — в валюте номинала,
#:   а ``value_rub`` — тот же купон в рублях.
#:
#: Проверка: у замещающей облигации с номиналом 1000 USD и купоном 3,25%
#: годовых купон за период равен ≈16 USD, а НКД в срезе достигает 1231 —
#: это рубли, а не доллары.
ACCRUED_IS_IN_SETTLEMENT_CURRENCY = True


def coupon_to_rub(
    value: float | None, value_rub: float | None, currency: str, on_date, fx: "FxBook"
) -> float | None:
    """Купон в рублях: биржа обычно сама даёт рублёвый эквивалент."""
    if value_rub is not None:
        return value_rub
    if value is None:
        return None
    rate = fx.rate(currency, on_date)
    return value * rate if rate is not None else None


def instrument_currency(instrument: Any) -> str:
    """Валюта, в которой считается стоимость бумаги.

    У облигации номинал может быть в валюте, а торговаться она может за рубли —
    для оценки позиции важна валюта номинала.
    """
    if instrument is None:
        return BASE_CURRENCY
    code = getattr(instrument, "face_unit", None) or getattr(instrument, "currency", None)
    if is_rub(code):
        return BASE_CURRENCY
    return (code or BASE_CURRENCY).upper()
