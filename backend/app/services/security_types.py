"""Виды бумаг: ОФЗ, корпоративные, биржевые, привилегированные акции и прочее.

В биржевом срезе вид бумаги закодирован одной буквой, по которой ничего не
отберёшь, поэтому вид приходит из массового справочника MOEX и хранится в
``Instrument.security_type`` системным именем (``ofz_bond``, ``common_share``).
Здесь лежит перевод этих имён на русский и порядок показа в фильтре.

Названия дублируются из справочника биржи намеренно: фильтр должен работать
и на свежей базе, где шаг сбора видов ещё не отработал, и когда биржа
недоступна. Реальные названия при сборе перекрывают эти — если биржа
переименует вид, терминал подхватит новое имя сам.
"""
from __future__ import annotations

#: Системное имя вида → название по-русски. Пополняется при сборе.
SECURITY_TYPE_TITLES: dict[str, str] = {
    # Облигации
    "ofz_bond": "ОФЗ (гособлигация)",
    "state_bond": "Государственная облигация",
    "cb_bond": "Облигация Банка России",
    "subfederal_bond": "Региональная облигация",
    "municipal_bond": "Муниципальная облигация",
    "corporate_bond": "Корпоративная облигация",
    "exchange_bond": "Биржевая облигация",
    "non_exchange_bond": "Коммерческая облигация",
    "ifi_bond": "Облигация международной финансовой организации",
    "euro_bond": "Еврооблигация",
    "stock_mortgage": "Ипотечный сертификат",
    # Долевые
    "common_share": "Акция обыкновенная",
    "preferred_share": "Акция привилегированная",
    "depositary_receipt": "Депозитарная расписка",
    "etf_ppif": "ETF",
    "exchange_ppif": "Пай биржевого ПИФа",
    "public_ppif": "Пай открытого ПИФа",
    "interval_ppif": "Пай интервального ПИФа",
    "private_ppif": "Пай закрытого ПИФа",
    # Индексы и валюта
    "stock_index": "Индекс фондового рынка",
    "stock_index_eq": "Индекс акций",
    "stock_index_fi": "Индекс облигаций",
    "stock_index_mx": "Индекс составной",
    "stock_index_ot": "Прочие индексы",
    "rts_index": "Индекс РТС",
    "currency": "Валюта",
    "currency_fixing": "Валютный фиксинг",
    "currency_wap": "Средневзвешенный курс",
    "gold_metal": "Металл золото",
    "silver_metal": "Металл серебро",
}

#: Порядок в выпадающем списке. Внутри вида бумаги упорядочены не по алфавиту,
#: а по тому, как о них думает казначей: сначала суверенный риск, затем
#: субфедеральный, затем корпоративный. Алфавит поставил бы биржевые
#: облигации перед ОФЗ, что для отбора неудобно.
SECURITY_TYPE_ORDER: tuple[str, ...] = (
    "ofz_bond",
    "state_bond",
    "cb_bond",
    "subfederal_bond",
    "municipal_bond",
    "ifi_bond",
    "corporate_bond",
    "exchange_bond",
    "non_exchange_bond",
    "euro_bond",
    "stock_mortgage",
    "common_share",
    "preferred_share",
    "depositary_receipt",
    "etf_ppif",
    "exchange_ppif",
    "public_ppif",
    "interval_ppif",
    "private_ppif",
)

_ORDER_INDEX = {code: index for index, code in enumerate(SECURITY_TYPE_ORDER)}


def security_type_title(code: str | None) -> str | None:
    """Название вида. Незнакомый код показываем как есть, а не прячем."""
    if not code:
        return None
    return SECURITY_TYPE_TITLES.get(code, code)


def sort_key(code: str) -> tuple[int, str]:
    """Ключ сортировки: известные виды в заданном порядке, прочие — в конец."""
    return (_ORDER_INDEX.get(code, len(_ORDER_INDEX)), security_type_title(code) or code)
