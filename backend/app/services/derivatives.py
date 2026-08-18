"""Срочный рынок: расчёт позиций по фьючерсам и опционам ФОРТС.

Терминал не выставляет заявки — он считает. Вы описываете позицию (контракт,
направление, объём, цена входа), а сервис по текущему срезу биржи показывает
результат: прибыль, точку безубыточности, требуемое обеспечение и — для
опционов — чувствительности.

Почему Блэк-76, а не Блэк-Шоулз: на ФОРТС опцион выписан на фьючерс, а не на
акцию. У фьючерса нет дивидендов и стоимости удержания, поэтому в формуле
вместо цены базового актива стоит цена фьючерса, а дисконтируется только
итог. Это же соглашение использует сама биржа, считая свои теоретические
цены, — значит и греки сойдутся с брокерским терминалом.

Опционы ФОРТС американские и маржируемые, а Блэк-76 описывает европейские.
Разница для маржируемых опционов мала (досрочное исполнение почти никогда не
выгодно, премия не уплачивается вперёд), и рынок котирует их именно так.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal, Sequence

logger = logging.getLogger(__name__)

#: Год для перевода срока в доли — календарный, как в котировках биржи
DAYS_IN_YEAR = 365.0

#: Границы поиска подразумеваемой волатильности: 1% и 1000% годовых.
#: Шире искать бессмысленно — за этими пределами цена уже не информативна
_VOL_MIN = 0.01
_VOL_MAX = 10.0
_VOL_STEPS = 60


def _norm_cdf(x: float) -> float:
    """Функция стандартного нормального распределения."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def year_fraction(expiry: date, on_date: date | None = None) -> float:
    """Срок до экспирации в годах. Прошедшая дата даёт ноль, не отрицание."""
    today = on_date or date.today()
    return max((expiry - today).days, 0) / DAYS_IN_YEAR


def _d1_d2(
    future: float, strike: float, years: float, vol: float
) -> tuple[float, float]:
    sigma = vol * math.sqrt(years)
    d1 = (math.log(future / strike) + 0.5 * vol * vol * years) / sigma
    return d1, d1 - sigma


def black76_price(
    future: float,
    strike: float,
    years: float,
    vol: float,
    rate: float,
    is_call: bool,
) -> float:
    """Цена европейского опциона на фьючерс."""
    if future <= 0 or strike <= 0:
        return 0.0

    intrinsic = max(future - strike, 0.0) if is_call else max(strike - future, 0.0)
    if years <= 0 or vol <= 0:
        # В день экспирации и при нулевой волатильности остаётся только
        # внутренняя стоимость — формула здесь делит на ноль
        return intrinsic

    discount = math.exp(-rate * years)
    d1, d2 = _d1_d2(future, strike, years, vol)
    if is_call:
        return discount * (future * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return discount * (strike * _norm_cdf(-d2) - future * _norm_cdf(-d1))


def black76_greeks(
    future: float,
    strike: float,
    years: float,
    vol: float,
    rate: float,
    is_call: bool,
) -> dict[str, float]:
    """Чувствительности цены опциона.

    Приведены к тому виду, в котором их читают на практике:

    * дельта — на сколько подорожает опцион при движении фьючерса на 1 пункт;
    * гамма — на сколько изменится сама дельта при том же движении;
    * вега — при росте волатильности на 1 процентный пункт;
    * тета — за один календарный день.
    """
    if future <= 0 or strike <= 0 or years <= 0 or vol <= 0:
        # Опцион истёк: дельта скачком равна 0 или 1, остальное вырождается
        if is_call:
            delta = 1.0 if future > strike else 0.0
        else:
            delta = -1.0 if future < strike else 0.0
        return {"delta": delta, "gamma": 0.0, "vega": 0.0, "theta": 0.0}

    discount = math.exp(-rate * years)
    d1, d2 = _d1_d2(future, strike, years, vol)
    pdf = _norm_pdf(d1)
    sqrt_years = math.sqrt(years)

    delta = discount * (_norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0)
    gamma = discount * pdf / (future * vol * sqrt_years)
    vega = discount * future * pdf * sqrt_years

    common = -discount * future * pdf * vol / (2.0 * sqrt_years)
    if is_call:
        theta = common + rate * discount * (
            future * _norm_cdf(d1) - strike * _norm_cdf(d2)
        )
    else:
        theta = common - rate * discount * (
            future * _norm_cdf(-d1) - strike * _norm_cdf(-d2)
        )

    return {
        "delta": delta,
        "gamma": gamma,
        # Вега и тета в «человеческих» единицах: пункт волатильности и день
        "vega": vega / 100.0,
        "theta": theta / DAYS_IN_YEAR,
    }


def implied_vol(
    price: float,
    future: float,
    strike: float,
    years: float,
    rate: float,
    is_call: bool,
) -> float | None:
    """Волатильность, при которой модель даёт рыночную цену.

    Биржа её не публикует, поэтому восстанавливаем из цены делением отрезка:
    цена монотонно растёт по волатильности, так что решение единственно.
    Ньютон здесь сходился бы быстрее, но у далёких страйков вега близка к
    нулю, и метод разлетается — надёжность важнее нескольких итераций.
    """
    if price <= 0 or future <= 0 or strike <= 0 or years <= 0:
        return None

    intrinsic = max(future - strike, 0.0) if is_call else max(strike - future, 0.0)
    if price < intrinsic * math.exp(-rate * years) - 1e-9:
        # Цена ниже внутренней стоимости: такого решения не существует
        return None

    low, high = _VOL_MIN, _VOL_MAX
    if black76_price(future, strike, years, high, rate, is_call) < price:
        return None

    for _ in range(_VOL_STEPS):
        middle = 0.5 * (low + high)
        if black76_price(future, strike, years, middle, rate, is_call) < price:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


# ----------------------------------------------------------------------
# Позиции
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Contract:
    """Контракт срочного рынка в том виде, в каком он нужен расчёту."""

    secid: str
    name: str
    kind: Literal["future", "option"]
    asset_code: str
    expiry: date | None
    #: Шаг цены и стоимость шага: по ним прибыль считается в рублях
    min_step: float
    step_price: float
    last: float | None
    settle_price: float | None
    #: Гарантийное обеспечение по данным биржи
    margin: float | None
    open_position: int | None
    fee: float | None
    # Только у опционов
    strike: float | None = None
    option_type: Literal["C", "P"] | None = None
    underlying: str | None = None
    underlying_price: float | None = None
    #: Волатильность биржи, % годовых. Есть только в опционной доске —
    #: если её нет, восстанавливаем из рыночной цены сами
    volatility: float | None = None


def _number(value: Any) -> float | None:
    """Биржа отдаёт отсутствующие значения нулём — отличаем их от настоящих."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def parse_future(row: dict[str, Any]) -> Contract | None:
    secid = row.get("SECID")
    if not secid:
        return None
    return Contract(
        secid=secid,
        name=row.get("SHORTNAME") or secid,
        kind="future",
        asset_code=row.get("ASSETCODE") or "",
        expiry=_as_date(row.get("LASTTRADEDATE")),
        min_step=_number(row.get("MINSTEP")) or 1.0,
        step_price=_number(row.get("STEPPRICE")) or 1.0,
        last=_positive(row.get("LAST")),
        settle_price=_positive(row.get("SETTLEPRICE"))
        or _positive(row.get("LASTSETTLEPRICE"))
        or _positive(row.get("PREVSETTLEPRICE")),
        margin=_positive(row.get("INITIALMARGIN")),
        open_position=int(_number(row.get("OPENPOSITION")) or 0),
        fee=_number(row.get("BUYSELLFEE")),
    )


def parse_option(row: dict[str, Any]) -> Contract | None:
    secid = row.get("SECID")
    option_type = row.get("OPTIONTYPE")
    if not secid or option_type not in ("C", "P"):
        return None
    return Contract(
        secid=secid,
        name=row.get("SHORTNAME") or secid,
        kind="option",
        asset_code=row.get("ASSETCODE") or "",
        expiry=_as_date(row.get("LASTTRADEDATE")),
        min_step=_number(row.get("MINSTEP")) or 1.0,
        step_price=_number(row.get("STEPPRICE")) or 1.0,
        last=_positive(row.get("LAST")),
        settle_price=_positive(row.get("SETTLEPRICE"))
        or _positive(row.get("PREVSETTLEPRICE")),
        # IMBUY — обеспечение под покупку, IMNP — под продажу непокрытого
        margin=_positive(row.get("IMBUY")) or _positive(row.get("IMNP")),
        open_position=int(_number(row.get("OPENPOSITION")) or 0),
        fee=_number(row.get("BUYSELLFEE")),
        strike=_number(row.get("STRIKE")),
        option_type=option_type,
        underlying=row.get("UNDERLYINGASSET"),
        underlying_price=_positive(row.get("UNDERLYINGSETTLEPRICE")),
    )


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def contract_price(contract: Contract) -> float | None:
    """Цена, по которой оцениваем позицию: последняя сделка, иначе расчётная.

    Вне торговой сессии сделок нет, и единственная осмысленная оценка —
    расчётная цена клиринга. Молча брать ноль нельзя: позиция обнулилась бы
    на экране каждый вечер.
    """
    return contract.last or contract.settle_price


def price_pnl(contract: Contract, entry: float, exit_price: float, qty: int) -> float:
    """Прибыль в рублях от движения цены на количество контрактов.

    На ФОРТС цена контракта — в пунктах, и рубль из них получается через шаг
    цены: сколько шагов прошла цена, столько раз начислена стоимость шага.
    Для RTS и валютных фьючерсов это не то же самое, что разница цен.
    """
    if contract.min_step <= 0:
        return 0.0
    steps = (exit_price - entry) / contract.min_step
    return steps * contract.step_price * qty


@dataclass(frozen=True)
class Leg:
    """Одна нога позиции: контракт, направление и объём."""

    contract: Contract
    #: 1 — длинная позиция (купили), -1 — короткая (продали)
    direction: int
    quantity: int
    entry_price: float

    @property
    def signed_quantity(self) -> int:
        return self.direction * self.quantity


def leg_result(
    leg: Leg, rate: float, on_date: date | None = None
) -> dict[str, Any]:
    """Текущий результат по одной ноге: прибыль, обеспечение, греки."""
    contract = leg.contract
    current = contract_price(contract)
    result: dict[str, Any] = {
        "secid": contract.secid,
        "name": contract.name,
        "kind": contract.kind,
        "direction": leg.direction,
        "quantity": leg.quantity,
        "entry_price": leg.entry_price,
        "current_price": current,
        "expiry": contract.expiry,
        "min_step": contract.min_step,
        "step_price": contract.step_price,
        "strike": contract.strike,
        "option_type": contract.option_type,
        "pnl": None,
        "margin": None,
        "fee": None,
        "greeks": None,
        "implied_vol": None,
    }

    if current is not None:
        result["pnl"] = round(
            price_pnl(contract, leg.entry_price, current, leg.signed_quantity), 2
        )
    if contract.margin is not None:
        result["margin"] = round(contract.margin * leg.quantity, 2)
    if contract.fee is not None:
        # Комиссия берётся и на входе, и на выходе
        result["fee"] = round(contract.fee * leg.quantity * 2, 2)

    if contract.kind == "option" and contract.expiry and contract.strike:
        years = year_fraction(contract.expiry, on_date)
        future = contract.underlying_price
        if future and current:
            # Волатильность биржи точнее собственного расчёта: она сглажена по
            # всей доске, а у неликвидного страйка «цена» — это случайная
            # последняя сделка, из которой выйдет что угодно
            vol = (contract.volatility / 100.0) if contract.volatility else None
            result["vol_source"] = "биржа" if vol else "расчёт по цене"
            if vol is None:
                vol = implied_vol(
                    current, future, contract.strike, years, rate,
                    contract.option_type == "C",
                )
            result["implied_vol"] = round(vol * 100, 2) if vol else None
            if vol:
                greeks = black76_greeks(
                    future, contract.strike, years, vol, rate,
                    contract.option_type == "C",
                )
                # Греки позиции: на объём и с учётом направления
                result["greeks"] = {
                    name: round(value * leg.signed_quantity, 6)
                    for name, value in greeks.items()
                }
        result["days_to_expiry"] = (
            (contract.expiry - (on_date or date.today())).days
        )
        result["underlying_price"] = future

    return result


def payoff_at(leg: Leg, underlying_price: float, rate: float = 0.0) -> float:
    """Результат ноги на экспирацию при заданной цене базового актива.

    Считается по стоимости на дату исполнения, а не по текущей рыночной
    цене: профиль выплат отвечает на вопрос «чем всё кончится», а не
    «сколько стоит сейчас».
    """
    contract = leg.contract
    if contract.kind == "future":
        return price_pnl(contract, leg.entry_price, underlying_price, leg.signed_quantity)

    if contract.strike is None:
        return 0.0
    if contract.option_type == "C":
        value = max(underlying_price - contract.strike, 0.0)
    else:
        value = max(contract.strike - underlying_price, 0.0)
    # Опцион на экспирации стоит свою внутреннюю стоимость — разница с ценой
    # входа и есть результат, пересчитанный в рубли через шаг цены
    return price_pnl(contract, leg.entry_price, value, leg.signed_quantity)


def payoff_curve(
    legs: Sequence[Leg],
    center: float,
    rate: float = 0.0,
    span_pct: float = 0.25,
    points: int = 81,
) -> list[dict[str, float]]:
    """Профиль выплат позиции вокруг текущей цены базового актива.

    В сетку явно добавляются страйки: на них у профиля излом, и на
    равномерной сетке он срезался бы наискось между соседними точками —
    ломался бы и вид графика, и расчёт точки безубыточности.
    """
    if not legs or center <= 0:
        return []

    low = center * (1 - span_pct)
    high = center * (1 + span_pct)
    step = (high - low) / max(points - 1, 1)

    grid = {round(low + step * index, 4) for index in range(points)}
    for leg in legs:
        strike = leg.contract.strike
        if strike is not None and low <= strike <= high:
            # Сам излом и точки вплотную к нему с обеих сторон: между ними
            # профиль уже линеен, и интерполяция становится точной
            grid.update({round(strike, 4), round(strike - 1e-4, 4), round(strike + 1e-4, 4)})

    curve = []
    for price in sorted(grid):
        total = sum(payoff_at(leg, price, rate) for leg in legs)
        curve.append({"price": price, "pnl": round(total, 2)})
    return curve


def breakeven_points(curve: Sequence[dict[str, float]]) -> list[float]:
    """Цены, при которых позиция выходит в ноль.

    Ищем по смене знака на профиле выплат: так находятся все точки и у
    сложных конструкций, где формулы для каждой пришлось бы выводить руками.
    """
    points: list[float] = []
    for previous, current in zip(curve, curve[1:]):
        first, second = previous["pnl"], current["pnl"]
        if first == 0.0:
            points.append(previous["price"])
            continue
        if first * second < 0:
            # Линейная интерполяция между соседними точками
            share = abs(first) / (abs(first) + abs(second))
            points.append(
                round(previous["price"] + (current["price"] - previous["price"]) * share, 4)
            )
    return points


def position_summary(
    legs: Sequence[Leg],
    rate: float,
    underlying_price: float | None = None,
    on_date: date | None = None,
) -> dict[str, Any]:
    """Свод по позиции: результат, обеспечение, греки и профиль выплат."""
    results = [leg_result(leg, rate, on_date) for leg in legs]

    def _total(field: str) -> float | None:
        values = [row[field] for row in results if row.get(field) is not None]
        return round(sum(values), 2) if values else None

    greeks_total: dict[str, float] = {}
    for row in results:
        for name, value in (row.get("greeks") or {}).items():
            greeks_total[name] = round(greeks_total.get(name, 0.0) + value, 6)

    center = underlying_price
    if center is None:
        # Ориентир — цена базового актива опциона, иначе цена самого фьючерса
        for leg in legs:
            if leg.contract.kind == "option" and leg.contract.underlying_price:
                center = leg.contract.underlying_price
                break
        else:
            for leg in legs:
                price = contract_price(leg.contract)
                if price:
                    center = price
                    break

    curve = payoff_curve(legs, center or 0.0, rate)
    return {
        "legs": results,
        "pnl": _total("pnl"),
        "margin": _total("margin"),
        "fee": _total("fee"),
        "greeks": greeks_total or None,
        "underlying_price": center,
        "payoff": curve,
        "breakeven": breakeven_points(curve),
        "rate_pct": round(rate * 100, 2),
    }


# ----------------------------------------------------------------------
# Загрузка данных биржи
# ----------------------------------------------------------------------
#: Срез живёт в памяти: дёргать биржу на каждое движение в калькуляторе
#: незачем, а данные меняются не чаще раза в несколько секунд
_CACHE_TTL_SEC = 60.0
_cache: dict[str, tuple[float, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}


def reset_cache() -> None:
    """Сбросить кэш — нужно тестам и принудительному обновлению."""
    _cache.clear()


async def _cached(key: str, loader, force: bool = False) -> Any:
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        entry = _cache.get(key)
        if entry and not force and time.monotonic() - entry[0] < _CACHE_TTL_SEC:
            return entry[1]
        value = await loader()
        _cache[key] = (time.monotonic(), value)
        return value


async def load_futures(force: bool = False) -> list[Contract]:
    """Все фьючерсы ФОРТС одним срезом — их около пятисот, это недорого."""
    async def _load() -> list[Contract]:
        from ..sources.moex import MoexSource

        async with MoexSource() as moex:
            rows = await moex.fetch_derivatives("forts", "RFUD")
        return [item for item in (parse_future(row) for row in rows) if item]

    return await _cached("futures", _load, force)


async def load_option_assets(force: bool = False) -> list[dict[str, Any]]:
    """Базовые активы опционного рынка с оборотом и открытым интересом."""
    async def _load() -> list[dict[str, Any]]:
        from ..sources.moex import MoexSource

        async with MoexSource() as moex:
            return await moex.fetch_option_assets()

    return await _cached("option-assets", _load, force)


async def load_option_contract(secid: str, force: bool = False) -> Contract | None:
    """Полные параметры опциона: шаг цены, стоимость шага, ГО, комиссия."""
    async def _load() -> Contract | None:
        from ..sources.moex import MoexSource

        async with MoexSource() as moex:
            row = await moex.fetch_derivative(secid, "options", "ROPD")
        return parse_option(row) if row else None

    return await _cached(f"option:{secid.upper()}", _load, force)


async def find_contract(secid: str) -> Contract | None:
    """Найти контракт по коду: сначала среди фьючерсов, потом опцион точечно."""
    secid = secid.upper()
    for contract in await load_futures():
        if contract.secid.upper() == secid:
            return contract
    return await load_option_contract(secid)


def _as_dict(contract: Contract) -> dict[str, Any]:
    return {
        "secid": contract.secid,
        "name": contract.name,
        "kind": contract.kind,
        "asset_code": contract.asset_code,
        "expiry": contract.expiry,
        "min_step": contract.min_step,
        "step_price": contract.step_price,
        "last": contract.last,
        "settle_price": contract.settle_price,
        "price": contract_price(contract),
        "margin": contract.margin,
        "open_position": contract.open_position,
        "fee": contract.fee,
        "strike": contract.strike,
        "option_type": contract.option_type,
        "underlying": contract.underlying,
        "underlying_price": contract.underlying_price,
    }


async def list_futures(asset: str | None = None) -> list[dict[str, Any]]:
    """Фьючерсы, самые торгуемые сверху."""
    contracts = await load_futures()
    if asset:
        contracts = [
            item for item in contracts if item.asset_code.upper() == asset.upper()
        ]
    # Сначала то, чем реально торгуют: по открытому интересу
    contracts = sorted(
        contracts,
        key=lambda item: (-(item.open_position or 0), item.expiry or date.max),
    )
    return [_as_dict(item) for item in contracts]


async def list_assets() -> list[dict[str, Any]]:
    """Базовые активы опционного рынка — из них выбирается опционная серия.

    Фьючерсы сюда не подмешиваются: биржа кодирует их иначе (SBRF против
    SBER у опциона на ту же акцию), и попытка свести всё в один список
    давала бы пары почти одинаковых строк. Фьючерсный калькулятор работает
    с плоским списком контрактов.
    """
    assets = []
    for row in await load_option_assets():
        code = row.get("asset")
        if not code:
            continue
        assets.append(
            {
                "asset": code,
                "name": row.get("shortname") or code,
                "price": _number(row.get("asset_last_price")),
                "change_pct": _number(row.get("asset_last_to_prev_price")),
                "open_position": int(_number(row.get("openposition")) or 0),
                "turnover": _number(row.get("valtoday")),
                "option_secid": row.get("option_secid"),
            }
        )
    return sorted(
        assets, key=lambda item: (-(item["open_position"] or 0), item["asset"])
    )


async def option_expiries(asset: str) -> list[dict[str, Any]]:
    """Даты экспирации по активу с пометкой типа серии."""
    from ..sources.moex import MoexSource

    titles = {"M": "месячная", "W": "недельная", "Q": "квартальная"}
    async with MoexSource() as moex:
        rows = await moex.fetch_option_expirations(asset.upper())

    today = date.today()
    result = []
    for row in rows:
        expiry = _as_date(row.get("expiration_date"))
        if expiry is None:
            continue
        series = row.get("series_type") or ""
        result.append(
            {
                "expiry": expiry,
                "series_type": series,
                "series_title": titles.get(series, series),
                "days": (expiry - today).days,
            }
        )
    return sorted(result, key=lambda item: item["expiry"])


async def option_board(
    asset: str, expiry: date | None = None
) -> dict[str, Any]:
    """Опционная доска по активу: страйки, цены и волатильность биржи.

    Волатильность и теоретическую цену считает сама биржа — их и показываем.
    Свой расчёт нужен там, где биржевых значений нет (неликвидные страйки).
    """
    from ..sources.moex import MoexSource

    async with MoexSource() as moex:
        board = await moex.fetch_option_board(asset.upper(), expiry)

    info = board.get("asset") or {}
    underlying_price = _number(info.get("UNDERLYINGSETTLEPRICE"))
    expiry_date = _as_date(info.get("LASTDELDATE"))

    def _rows(items: Sequence[dict[str, Any]], option_type: str) -> list[dict[str, Any]]:
        result = []
        for row in items:
            strike = _number(row.get("STRIKE"))
            if strike is None:
                continue
            result.append(
                {
                    "secid": row.get("SECID"),
                    "option_type": option_type,
                    "strike": strike,
                    "theor_price": _number(row.get("THEORPRICE")),
                    "volatility": _number(row.get("VOLAT")),
                    "last": _positive(row.get("LAST")),
                    "bid": _positive(row.get("BID")),
                    "offer": _positive(row.get("OFFER")),
                    "volume": int(_number(row.get("VOLTODAY")) or 0),
                    "open_position": int(_number(row.get("OPENPOSITION")) or 0),
                }
            )
        return sorted(result, key=lambda item: item["strike"])

    return {
        "asset": asset.upper(),
        "expiry": expiry_date,
        "underlying": info.get("UNDERLYINGASSET"),
        "underlying_type": info.get("UNDERLYINGTYPE"),
        "underlying_price": underlying_price,
        "central_strike": _number(info.get("CENTRALSTRIKE")),
        "days_to_expiry": (expiry_date - date.today()).days if expiry_date else None,
        "call": _rows(board.get("call") or [], "C"),
        "put": _rows(board.get("put") or [], "P"),
    }


async def build_leg(
    secid: str,
    direction: int,
    quantity: int,
    entry_price: float | None = None,
    volatility: float | None = None,
) -> Leg:
    """Собрать ногу позиции по коду контракта.

    Цена входа необязательна: без неё берётся текущая рыночная — так
    калькулятор отвечает на вопрос «что будет, если войти сейчас».
    """
    contract = await find_contract(secid)
    if contract is None:
        raise LookupError(f"Контракт {secid} не найден на срочном рынке")

    if volatility is not None:
        contract = replace(contract, volatility=volatility)

    price = entry_price if entry_price is not None else contract_price(contract)
    if price is None:
        raise LookupError(
            f"По контракту {contract.secid} нет ни сделок, ни расчётной цены — "
            "укажите цену входа вручную"
        )

    return Leg(
        contract=contract,
        direction=1 if direction >= 0 else -1,
        quantity=max(1, int(quantity)),
        entry_price=float(price),
    )


async def calculate(
    legs: Sequence[dict[str, Any]],
    rate: float,
    underlying_price: float | None = None,
) -> dict[str, Any]:
    """Посчитать позицию по описанию ног из запроса."""
    built = [
        await build_leg(
            item["secid"],
            item.get("direction", 1),
            item.get("quantity", 1),
            item.get("entry_price"),
            item.get("volatility"),
        )
        for item in legs
    ]
    return position_summary(built, rate, underlying_price)


async def contract_candles(
    secid: str, interval: int = 10, days: int = 1
) -> dict[str, Any]:
    """Ход торгов по контракту — для графика с уровнем позиции.

    Если за сегодня сделок нет (выходной, утро до открытия), отступаем на
    несколько дней назад: пустой график вместо линии цены выглядел бы
    поломкой, хотя биржа просто ещё не торговала.
    """
    from ..sources.moex import MoexSource

    contract = await find_contract(secid)
    if contract is None:
        raise LookupError(f"Контракт {secid} не найден на срочном рынке")

    market, board = (
        ("forts", "RFUD") if contract.kind == "future" else ("options", "ROPD")
    )
    async with MoexSource() as moex:
        candles = await moex.fetch_derivative_candles(
            contract.secid, market=market, board=board, interval=interval,
            start_date=date.today() - timedelta(days=max(0, days - 1)),
        )
        if not candles:
            candles = await moex.fetch_derivative_candles(
                contract.secid, market=market, board=board, interval=interval,
                start_date=date.today() - timedelta(days=7),
            )

    return {
        "secid": contract.secid,
        "name": contract.name,
        "interval": interval,
        "price": contract_price(contract),
        "candles": candles,
    }
