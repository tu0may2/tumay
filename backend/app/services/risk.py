"""Риск-метрики портфеля: переоценка при движении ставок и денежные потоки."""
from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy.orm import Session

from .portfolio import compute_positions

#: Сдвиги ставок для сценариев, базисные пункты
DEFAULT_SHIFTS = (25, 50, 100, 200, 300)

#: Граница между «коротким» и «длинным» концом кривой, лет
CURVE_PIVOT_YEARS = 5.0


def modified_duration(duration_years: float | None, yield_pct: float | None) -> float | None:
    """Модифицированная дюрация — чувствительность цены к доходности.

    MOEX отдаёт дюрацию Маколея; для переоценки нужна модифицированная.
    """
    if duration_years is None:
        return None
    rate = (yield_pct or 0) / 100
    return duration_years / (1 + rate)


def approximate_convexity(
    duration_years: float | None, yield_pct: float | None
) -> float | None:
    """Оценка выпуклости по дюрации.

    Точная выпуклость требует полного графика платежей по каждому выпуску;
    для облигации с погашением в конце срока хорошо работает приближение
    ``C ≈ D · (D + 1) / (1 + y)²``. Используется только как поправка второго
    порядка, поэтому погрешность приближения на итог влияет слабо.
    """
    if duration_years is None:
        return None
    rate = (yield_pct or 0) / 100
    return duration_years * (duration_years + 1) / ((1 + rate) ** 2)


def _revalue(
    positions: Sequence[dict[str, Any]], shift_bp: float, *, tilt: str = "parallel"
) -> float:
    """Изменение стоимости облигаций при заданном движении кривой.

    ``tilt`` задаёт форму сдвига: параллельный, кривая круче (длинный конец
    растёт сильнее) или положе (короткий конец растёт сильнее).
    """
    total = 0.0
    for position in positions:
        value = position.get("market_value_rub")
        duration = position.get("duration_years")
        if not value or duration is None:
            continue

        delta_y = shift_bp / 10_000
        if tilt == "steepening":
            # Длинные ставки растут, короткие почти не двигаются
            delta_y *= min(duration / CURVE_PIVOT_YEARS, 2.0)
        elif tilt == "flattening":
            # Короткие ставки растут, длинные отстают
            delta_y *= max(0.2, min(CURVE_PIVOT_YEARS / max(duration, 0.25), 2.0))

        mod_duration = modified_duration(duration, position.get("yield_pct")) or 0
        convexity = approximate_convexity(duration, position.get("yield_pct")) or 0
        # Второй порядок: на сдвигах от 200 бп линейная оценка заметно врёт
        change = -mod_duration * delta_y + 0.5 * convexity * delta_y**2
        total += value * change
    return total


def rate_sensitivity(
    session: Session,
    *,
    portfolio: str | None = None,
    shift_bp: Sequence[int] = DEFAULT_SHIFTS,
) -> dict[str, Any]:
    """Переоценка облигационной части при движении ставок."""
    positions = [
        p
        for p in compute_positions(session, portfolio=portfolio)
        if p["quantity"] > 0 and p["duration_years"] is not None and p["market_value_rub"]
    ]
    bond_value = sum(p["market_value_rub"] for p in positions)

    weighted_duration = weighted_mod = weighted_convexity = None
    if bond_value:
        weighted_duration = round(
            sum(p["duration_years"] * p["market_value_rub"] for p in positions) / bond_value, 2
        )
        weighted_mod = round(
            sum(
                (modified_duration(p["duration_years"], p["yield_pct"]) or 0)
                * p["market_value_rub"]
                for p in positions
            )
            / bond_value,
            2,
        )
        weighted_convexity = round(
            sum(
                (approximate_convexity(p["duration_years"], p["yield_pct"]) or 0)
                * p["market_value_rub"]
                for p in positions
            )
            / bond_value,
            1,
        )

    def _scenarios(tilt: str) -> list[dict[str, Any]]:
        rows = []
        for shift in shift_bp:
            for sign in (1, -1):
                impact = _revalue(positions, sign * shift, tilt=tilt)
                linear = -(weighted_mod or 0) * (sign * shift / 10_000) * bond_value
                rows.append(
                    {
                        "shift_bp": sign * shift,
                        "impact_rub": round(impact, 2),
                        "impact_pct": round(impact / bond_value * 100, 3) if bond_value else None,
                        # Показываем, сколько добавила поправка на выпуклость
                        "convexity_effect_rub": round(impact - linear, 2),
                    }
                )
        return sorted(rows, key=lambda row: row["shift_bp"])

    return {
        "bond_value": round(bond_value, 2),
        "weighted_duration_years": weighted_duration,
        "weighted_modified_duration": weighted_mod,
        "weighted_convexity": weighted_convexity,
        "scenarios": _scenarios("parallel"),
        "scenarios_steepening": _scenarios("steepening"),
        "scenarios_flattening": _scenarios("flattening"),
        "note": (
            "Переоценка второго порядка: ΔV/V ≈ −D_мод × Δy + ½ × C × Δy². "
            "Выпуклость оценена по дюрации."
        ),
    }


# ----------------------------------------------------------------------
# Денежные потоки портфеля
# ----------------------------------------------------------------------
def portfolio_cashflow(
    session: Session,
    *,
    portfolio: str | None = None,
    horizon_days: int = 365,
) -> dict[str, Any]:
    """Будущие купоны, амортизации и погашения по своим позициям, в рублях.

    Прямой вход в план ликвидности: видно, сколько и когда придёт денег.

    Расчёт живёт в :mod:`.coupons` — там же, где полный календарь поступлений
    с прошлыми выплатами и флоатерами. Здесь остался только вход без прошлого:
    отчёт и сводка спрашивают именно «сколько придёт впереди».
    """
    from .coupons import receipts_calendar

    return receipts_calendar(
        session, portfolio=portfolio, horizon_days=horizon_days, past_days=0
    )


