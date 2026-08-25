"""Нормативы ликвидности Банка России: Н2, Н3, Н4.

Инструкция Банка России № 199-И. Формулы:

* **Н2** (мгновенная) = Лам / (Овм − 0,5 × Овм*) × 100%, минимум **15%**
* **Н3** (текущая) = Лат / (Овт − 0,5 × Овт*) × 100%, минимум **50%**
* **Н4** (долгосрочная) = Крд / (К + ОД + 0,5 × О*) × 100%, максимум **120%**

Терминал не ведёт баланс банка и не может посчитать их целиком: обязательства
до востребования, капитал и долгосрочные требования он не знает. Поэтому
разделение честное — знаменатель вводится руками, а числитель по ликвидным
активам терминал собирает сам: деньги на счетах плюс бумаги, которые ЦБ
принимает в обеспечение, по той стоимости, которую ЦБ под них даёт.

Именно поэтому ломбардный список важен для нормативов: залоговая бумага
превращается в деньги в тот же день и идёт в Лам, а незалоговая — нет. Купив
на свободные деньги бумагу вне списка, банк ухудшает Н2, даже если бумага
надёжная и доходная. Ради этого здесь и сделан пересчёт: видно, во что
обойдётся сделка ещё до того, как она заключена.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import RatioInput

#: Предельные значения по 199-И
LIMIT_H2 = 15.0
LIMIT_H3 = 50.0
LIMIT_H4 = 120.0


@dataclass(slots=True)
class Ratio:
    """Один норматив со всем, что нужно для проверки расчёта."""

    code: str
    title: str
    value: float | None
    limit: float
    #: minimum — норматив должен быть не ниже предела, maximum — не выше
    direction: str
    numerator: float
    denominator: float
    numerator_title: str
    denominator_title: str
    formula: str

    @property
    def compliant(self) -> bool | None:
        if self.value is None:
            return None
        return self.value >= self.limit if self.direction == "minimum" else self.value <= self.limit

    @property
    def cushion(self) -> float | None:
        """Запас до предела в процентных пунктах. Отрицательный — нарушение."""
        if self.value is None:
            return None
        return (
            self.value - self.limit
            if self.direction == "minimum"
            else self.limit - self.value
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "value": round(self.value, 2) if self.value is not None else None,
            "limit": self.limit,
            "direction": self.direction,
            "compliant": self.compliant,
            "cushion": round(self.cushion, 2) if self.cushion is not None else None,
            "numerator": round(self.numerator, 2),
            "denominator": round(self.denominator, 2),
            "numerator_title": self.numerator_title,
            "denominator_title": self.denominator_title,
            "formula": self.formula,
        }


def _ratio(numerator: float, denominator: float) -> float | None:
    """Отношение в процентах. Нулевой знаменатель — не ноль, а «не считается».

    У банка без обязательств до востребования норматив не определён; выдать
    ноль означало бы показать нарушение там, где его нет.
    """
    if not denominator:
        return None
    return numerator / denominator * 100


def liquid_assets(
    session: Session,
    *,
    portfolio: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Ликвидные активы, которые терминал знает сам.

    Деньги на счетах — это Лам без оговорок. Бумаги считаем по залоговой
    стоимости: продавать их не требуется, ЦБ даёт деньги под залог в тот же
    день, но даёт не полную оценку, а с поправочным коэффициентом.
    """
    from .cash import cash_position
    from .collateral import portfolio_collateral

    cash = cash_position(session, portfolio=portfolio)
    collateral = portfolio_collateral(session, portfolio=portfolio, method=method)

    # Берём именно остаток на счетах, а не total_liquidity_rub: тот включает
    # размещения, а депозит на срок — не высоколиквидный актив, его нельзя
    # обратить в деньги сегодня, и в Лам ему не место
    money = float(cash.get("total_cash_rub") or 0.0)
    pledgeable = float(collateral.get("pledgeable_rub") or 0.0)

    return {
        "cash_rub": round(money, 2),
        "pledgeable_rub": round(pledgeable, 2),
        "total_rub": round(money + pledgeable, 2),
        "eligible_positions": collateral.get("eligible_positions", 0),
        "total_positions": collateral.get("total_positions", 0),
        "collateral_as_of": collateral.get("as_of"),
    }


def compute(
    inputs: dict[str, Any],
    *,
    lam_portfolio: float = 0.0,
    lat_portfolio: float | None = None,
) -> list[Ratio]:
    """Посчитать Н2, Н3 и Н4 по введённым данным.

    ``lam_portfolio`` — ликвидные активы, посчитанные терминалом (деньги и
    залоговые бумаги). ``lat_portfolio`` по умолчанию равен им же: всё, что
    доступно мгновенно, доступно и в пределах тридцати дней.
    """
    if lat_portfolio is None:
        lat_portfolio = lam_portfolio

    value = lambda key: float(inputs.get(key) or 0.0)  # noqa: E731

    lam = lam_portfolio + value("lam_other")
    lat = lat_portfolio + value("lat_other")

    ovm_net = value("ovm") - 0.5 * value("ovm_min")
    ovt_net = value("ovt") - 0.5 * value("ovt_min")
    h4_denominator = value("capital") + value("od") + 0.5 * value("o_min")

    return [
        Ratio(
            code="Н2",
            title="Мгновенная ликвидность",
            value=_ratio(lam, ovm_net),
            limit=LIMIT_H2,
            direction="minimum",
            numerator=lam,
            denominator=ovm_net,
            numerator_title="Лам — высоколиквидные активы",
            denominator_title="Овм − 0,5 × Овм*",
            formula="Н2 = Лам / (Овм − 0,5 × Овм*) × 100%",
        ),
        Ratio(
            code="Н3",
            title="Текущая ликвидность",
            value=_ratio(lat, ovt_net),
            limit=LIMIT_H3,
            direction="minimum",
            numerator=lat,
            denominator=ovt_net,
            numerator_title="Лат — ликвидные активы до 30 дней",
            denominator_title="Овт − 0,5 × Овт*",
            formula="Н3 = Лат / (Овт − 0,5 × Овт*) × 100%",
        ),
        Ratio(
            code="Н4",
            title="Долгосрочная ликвидность",
            value=_ratio(value("krd"), h4_denominator),
            limit=LIMIT_H4,
            direction="maximum",
            numerator=value("krd"),
            denominator=h4_denominator,
            numerator_title="Крд — требования свыше 365 дней",
            denominator_title="К + ОД + 0,5 × О*",
            formula="Н4 = Крд / (К + ОД + 0,5 × О*) × 100%",
        ),
    ]


#: Поля, которые вводит человек: код, название, к какому нормативу относится
INPUT_FIELDS: tuple[dict[str, str], ...] = (
    {"code": "ovm", "title": "Овм — обязательства до востребования", "ratio": "Н2"},
    {"code": "ovm_min", "title": "Овм* — минимальный остаток по ним", "ratio": "Н2"},
    {"code": "lam_other", "title": "Лам сверх портфеля — прочие высоколиквидные активы", "ratio": "Н2"},
    {"code": "ovt", "title": "Овт — обязательства до 30 дней", "ratio": "Н3"},
    {"code": "ovt_min", "title": "Овт* — минимальный остаток по ним", "ratio": "Н3"},
    {"code": "lat_other", "title": "Лат сверх портфеля — прочие ликвидные активы", "ratio": "Н3"},
    {"code": "krd", "title": "Крд — кредитные требования свыше 365 дней", "ratio": "Н4"},
    {"code": "capital", "title": "К — собственные средства (капитал)", "ratio": "Н4"},
    {"code": "od", "title": "ОД — обязательства свыше 365 дней", "ratio": "Н4"},
    {"code": "o_min", "title": "О* — минимальный остаток по счетам до 365 дней", "ratio": "Н4"},
)

_INPUT_CODES = tuple(field["code"] for field in INPUT_FIELDS)


def load_inputs(session: Session, on_date: date | None = None) -> dict[str, Any]:
    """Последние введённые данные — или пустые, если их ещё не вводили."""
    statement = select(RatioInput).order_by(RatioInput.as_of.desc())
    if on_date is not None:
        statement = select(RatioInput).where(RatioInput.as_of == on_date)

    record = session.execute(statement.limit(1)).scalar_one_or_none()
    if record is None:
        return {"as_of": on_date or date.today(), **{code: None for code in _INPUT_CODES}}

    return {
        "as_of": record.as_of,
        "comment": record.comment,
        **{code: getattr(record, code) for code in _INPUT_CODES},
    }


def save_inputs(session: Session, payload: dict[str, Any]) -> RatioInput:
    """Сохранить балансовые данные на дату, перезаписав прежние за тот же день."""
    on_date = payload.get("as_of") or date.today()
    record = session.execute(
        select(RatioInput).where(RatioInput.as_of == on_date)
    ).scalar_one_or_none()
    if record is None:
        record = RatioInput(as_of=on_date)
        session.add(record)

    for code in _INPUT_CODES:
        if code in payload:
            setattr(record, code, payload[code])
    if "comment" in payload:
        record.comment = payload["comment"]

    session.commit()
    session.refresh(record)
    return record


def report(
    session: Session,
    *,
    portfolio: str | None = None,
    method: str | None = None,
    on_date: date | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Нормативы с разложением: что посчитано, что введено, где запас.

    ``overrides`` подменяют сохранённые значения, не записывая их: на этом
    построен пересчёт «что будет, если».
    """
    inputs = load_inputs(session, on_date)
    if overrides:
        inputs = {**inputs, **{k: v for k, v in overrides.items() if v is not None}}

    assets = liquid_assets(session, portfolio=portfolio, method=method)
    ratios = compute(inputs, lam_portfolio=assets["total_rub"])

    return {
        "as_of": inputs.get("as_of"),
        "inputs": inputs,
        "fields": list(INPUT_FIELDS),
        "assets": assets,
        "ratios": [ratio.as_dict() for ratio in ratios],
        "breaches": [
            ratio.code for ratio in ratios if ratio.compliant is False
        ],
        "incomplete": [
            ratio.code for ratio in ratios if ratio.value is None
        ],
    }


def simulate(
    session: Session,
    *,
    amount_rub: float,
    eligible: bool,
    portfolio: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Как сделка изменит нормативы.

    Покупка на ``amount_rub``: деньги уходят со счёта, а взамен приходит
    бумага. Если она в списке обеспечения, часть суммы возвращается в
    ликвидные активы — та, что ЦБ даст под залог; если нет, деньги уходят из
    ликвидности целиком. Продажа — отрицательная сумма.

    Поправочный коэффициент берём как средний по списку: конкретный выпуск
    ещё не выбран, а порядок величины он показывает верно.
    """
    from .collateral import collateral_map

    before = report(session, portfolio=portfolio, method=method)

    haircuts = [
        terms["haircut"]
        for terms in collateral_map(session).values()
        if terms.get("haircut")
    ]
    average_haircut = sum(haircuts) / len(haircuts) if haircuts else 0.0

    # Сколько ликвидности останется от потраченных денег
    returned = amount_rub * average_haircut if eligible else 0.0
    delta = returned - amount_rub

    assets = dict(before["assets"])
    raw_total = assets["total_rub"] + delta
    # Отрицательных ликвидных активов не бывает: если сделка съедает больше,
    # чем есть, она попросту неисполнима. Показать Н2 со знаком минус значило
    # бы обсуждать величину, которой не существует, — вместо этого доводим до
    # нуля и говорим, сколько денег не хватает
    shortfall = max(0.0, -raw_total)
    assets["total_rub"] = round(max(0.0, raw_total), 2)

    ratios = compute(before["inputs"], lam_portfolio=assets["total_rub"])
    after = {
        "assets": assets,
        "ratios": [ratio.as_dict() for ratio in ratios],
        "breaches": [ratio.code for ratio in ratios if ratio.compliant is False],
    }

    return {
        "amount_rub": amount_rub,
        "eligible": eligible,
        "average_haircut": round(average_haircut, 4) if average_haircut else None,
        "liquidity_delta_rub": round(delta, 2),
        # Сделка неисполнима: ликвидных активов на неё не хватает
        "insufficient": shortfall > 0,
        "shortfall_rub": round(shortfall, 2) if shortfall else None,
        "before": before,
        "after": after,
        # Что именно изменилось — чтобы не сличать два списка глазами
        "changes": [
            {
                "code": old["code"],
                "before": old["value"],
                "after": new["value"],
                "delta": (
                    round(new["value"] - old["value"], 2)
                    if old["value"] is not None and new["value"] is not None
                    else None
                ),
                "breaks": old["compliant"] is not False and new["compliant"] is False,
            }
            for old, new in zip(before["ratios"], after["ratios"])
        ],
    }
