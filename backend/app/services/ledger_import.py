"""Импорт выгрузки по лицевым счетам в платёжный календарь.

Раз в день казначейство выгружает из банковской системы оборотную ведомость:
лицевой счёт, входящий остаток, обороты по дебету и кредиту, исходящий
остаток. Из неё и наполняется календарь — номер счёта определяет статью,
оборот по дебету становится платежом, по кредиту поступлением.

Формат выгрузки у каждой АБС свой, поэтому колонки распознаются по заголовкам,
а не по номеру: единственное, на чём мы настаиваем, — это столбец с номером
счёта и хотя бы один столбец с суммой.

Дата, на которую грузится выгрузка, определяет колонку календаря. Мы пытаемся
вычитать её из шапки файла («по состоянию на 17.03.2026»), но последнее слово
всегда за человеком: в шапке может стоять дата печати, а не дата данных.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import LedgerRow
from .calendar_matrix import (
    CREDIT,
    DEBIT,
    ROW_BY_CODE,
    classify,
    is_technical,
    normalise_account,
)
from .importer import MAX_ROWS, parse_date, parse_number, read_table

#: Как в выгрузках называют нужные нам столбцы.
#:
#: Порядок подсказок внутри поля значения не имеет — побеждает самая длинная
#: совпавшая, поэтому «оборот по дебету» выигрывает у «дебет», а «входящий
#: остаток по дебету» не перехватывает столбец оборотов.
COLUMN_HINTS: dict[str, tuple[str, ...]] = {
    "account": (
        "лицевой счет", "лицевой счёт", "номер счета", "номер счёта",
        "счет", "счёт", "account", "balance account",
    ),
    "account_name": ("наименование", "название", "назначение", "описание", "name"),
    "currency": ("валюта", "currency", "код валюты"),
    "opening_debit": (
        "входящий остаток по дебету", "вх. остаток дебет", "остаток на начало дебет",
        "входящее сальдо дебет", "сальдо на начало дебет",
    ),
    "opening_credit": (
        "входящий остаток по кредиту", "вх. остаток кредит", "остаток на начало кредит",
        "входящее сальдо кредит", "сальдо на начало кредит",
    ),
    "opening_balance": (
        "входящий остаток", "остаток на начало", "входящее сальдо",
        "сальдо на начало", "вх. остаток", "opening",
    ),
    "debit_turnover": (
        "оборот по дебету", "обороты по дебету", "дебетовый оборот",
        "дебет", "списание", "расход", "debit",
    ),
    "credit_turnover": (
        "оборот по кредиту", "обороты по кредиту", "кредитовый оборот",
        "кредит", "зачисление", "поступление", "приход", "credit",
    ),
    "closing_debit": (
        "исходящий остаток по дебету", "исх. остаток дебет", "остаток на конец дебет",
        "исходящее сальдо дебет", "сальдо на конец дебет",
    ),
    "closing_credit": (
        "исходящий остаток по кредиту", "исх. остаток кредит", "остаток на конец кредит",
        "исходящее сальдо кредит", "сальдо на конец кредит",
    ),
    "closing_balance": (
        "исходящий остаток", "остаток на конец", "исходящее сальдо",
        "сальдо на конец", "исх. остаток", "closing",
    ),
}

#: Минимум, без которого файл не выгрузка по счетам
REQUIRED = ("account",)

#: Где в шапке файла может стоять дата данных
_DATE_PATTERN = re.compile(r"(\d{2}[.\-/]\d{2}[.\-/]\d{4}|\d{4}-\d{2}-\d{2})")

#: Номер лицевого счёта — 20 цифр. Более короткие номера тоже берём (в
#: выгрузках встречаются пятизначные балансовые счета), но пустые и явно
#: посторонние строки отсекаем
MIN_ACCOUNT_DIGITS = 3


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _detect(row: Sequence[Any]) -> dict[str, int]:
    """Сопоставить строку заголовков со столбцами выгрузки."""
    mapping: dict[str, int] = {}
    used: set[int] = set()
    # Поля с длинными подсказками разбираем первыми, иначе «дебет» заберёт
    # столбец «входящий остаток по дебету» раньше, чем до него дойдёт очередь
    fields = sorted(
        COLUMN_HINTS,
        key=lambda field: max(len(hint) for hint in COLUMN_HINTS[field]),
        reverse=True,
    )
    for field in fields:
        best_index, best_length = None, 0
        for index, cell in enumerate(row):
            if index in used:
                continue
            lowered = _norm(cell)
            if not lowered:
                continue
            for hint in COLUMN_HINTS[field]:
                if hint in lowered and len(hint) > best_length:
                    best_index, best_length = index, len(hint)
        if best_index is not None:
            mapping[field] = best_index
            used.add(best_index)
    return mapping


def _find_header(rows: Sequence[Sequence[Any]]) -> tuple[int, dict[str, int]]:
    """Найти строку заголовков: над таблицей обычно стоит шапка отчёта."""
    best_index, best_mapping, best_score = 0, {}, -1
    for index, row in enumerate(rows[:25]):
        mapping = _detect(row)
        score = len(mapping)
        # Счёт вместе с любым оборотом в подписи отчёта не встречается —
        # такое сочетание надёжно выдаёт настоящую шапку
        if "account" in mapping and (
            "debit_turnover" in mapping or "credit_turnover" in mapping
        ):
            score += 5
        if score > best_score:
            best_index, best_mapping, best_score = index, mapping, score
    return best_index, best_mapping


def detect_date(rows: Sequence[Sequence[Any]], header_index: int) -> date | None:
    """Вытащить дату данных из шапки отчёта, если она там есть."""
    for row in rows[: header_index + 1]:
        for cell in row:
            if isinstance(cell, date):
                return parse_date(cell)
            match = _DATE_PATTERN.search(str(cell or ""))
            if match:
                parsed = parse_date(match.group(1))
                if parsed:
                    return parsed
    return None


def _cell(row: Sequence[Any], mapping: dict[str, int], field: str) -> Any:
    index = mapping.get(field)
    return row[index] if index is not None and index < len(row) else None


def _signed_balance(
    row: Sequence[Any], mapping: dict[str, int], *, prefix: str
) -> float:
    """Остаток одним числом: дебетовый со знаком плюс, кредитовый — минус.

    В оборотной ведомости остаток разнесён по двум столбцам, и складывать их
    как есть нельзя — получится сумма актива и пассива. Знак сохраняет
    информацию о стороне и позволяет хранить остаток одним полем.
    """
    debit = parse_number(_cell(row, mapping, f"{prefix}_debit"))
    credit = parse_number(_cell(row, mapping, f"{prefix}_credit"))
    if debit is not None or credit is not None:
        return (debit or 0.0) - (credit or 0.0)
    return parse_number(_cell(row, mapping, f"{prefix}_balance")) or 0.0


def parse(content: bytes, filename: str, *, on_date: date | None = None) -> dict[str, Any]:
    """Разобрать выгрузку: строки счетов, дата и найденные проблемы."""
    rows = read_table(content, filename)
    if not rows:
        raise ValueError("Файл пуст")

    header_index, mapping = _find_header(rows)
    missing = [field for field in REQUIRED if field not in mapping]
    if missing:
        raise ValueError(
            "В файле не найден столбец с номером лицевого счёта. "
            "Назовите его «Лицевой счёт» или «Номер счёта»."
        )
    if "debit_turnover" not in mapping and "credit_turnover" not in mapping:
        raise ValueError(
            "В файле не найдены обороты. Нужны столбцы «Оборот по дебету» "
            "и «Оборот по кредиту»."
        )

    load_date = on_date or detect_date(rows, header_index) or date.today()

    items: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    skipped = 0

    for line, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(str(value or "").strip() for value in row):
            continue
        if len(items) >= MAX_ROWS:
            break

        account = normalise_account(_cell(row, mapping, "account"))
        if len(account) < MIN_ACCOUNT_DIGITS:
            # Итоговые строки ведомости («Итого по разделу») номера счёта не
            # имеют — их пропускаем молча, иначе каждый импорт даёт список
            # «ошибок», в котором нет ни одной настоящей
            skipped += 1
            continue

        debit = abs(parse_number(_cell(row, mapping, "debit_turnover")) or 0.0)
        credit = abs(parse_number(_cell(row, mapping, "credit_turnover")) or 0.0)

        debit_rule = classify(account, DEBIT) if debit else None
        credit_rule = classify(account, CREDIT) if credit else None

        item = {
            "line": line,
            "account": account,
            "account_name": str(_cell(row, mapping, "account_name") or "").strip() or None,
            "currency": str(_cell(row, mapping, "currency") or "").strip().upper() or "RUB",
            "opening_balance": round(_signed_balance(row, mapping, prefix="opening"), 2),
            "debit_turnover": round(debit, 2),
            "credit_turnover": round(credit, 2),
            "closing_balance": round(_signed_balance(row, mapping, prefix="closing"), 2),
            "debit_row": debit_rule.row_code if debit_rule else None,
            "debit_title": ROW_BY_CODE[debit_rule.row_code].title if debit_rule else None,
            "credit_row": credit_rule.row_code if credit_rule else None,
            "credit_title": (
                ROW_BY_CODE[credit_rule.row_code].title if credit_rule else None
            ),
            "confirm": bool(
                (debit_rule and debit_rule.confirm) or (credit_rule and credit_rule.confirm)
            ),
            "technical": is_technical(account),
        }

        # Один счёт дважды в файле — это разбитая по валютам или по разделам
        # ведомость; складываем, а не теряем вторую строку
        if account in seen:
            target = items[seen[account]]
            target["debit_turnover"] = round(target["debit_turnover"] + debit, 2)
            target["credit_turnover"] = round(target["credit_turnover"] + credit, 2)
            target["opening_balance"] = round(
                target["opening_balance"] + item["opening_balance"], 2
            )
            target["closing_balance"] = round(
                target["closing_balance"] + item["closing_balance"], 2
            )
            continue

        seen[account] = len(items)
        items.append(item)

    return {
        "load_date": load_date,
        "detected_date": detect_date(rows, header_index),
        "rows": items,
        "skipped": skipped,
        "columns": sorted(mapping),
    }


def preview(
    session: Session, content: bytes, filename: str, *, on_date: date | None = None
) -> dict[str, Any]:
    """Показать, что попадёт в календарь, до записи."""
    parsed = parse(content, filename, on_date=on_date)
    rows = parsed["rows"]

    by_article: dict[str, dict[str, Any]] = {}
    unmapped: list[dict[str, Any]] = []
    for item in rows:
        for code, amount in (
            (item["debit_row"], item["debit_turnover"]),
            (item["credit_row"], item["credit_turnover"]),
        ):
            if code is None or not amount:
                continue
            bucket = by_article.setdefault(
                code,
                {
                    "row_code": code,
                    "row_title": ROW_BY_CODE[code].title,
                    "section": ROW_BY_CODE[code].section,
                    "amount": 0.0,
                    "accounts": 0,
                },
            )
            bucket["amount"] += amount
            bucket["accounts"] += 1

        turnover = item["debit_turnover"] or item["credit_turnover"]
        if (
            turnover
            and not item["technical"]
            and item["debit_row"] is None
            and item["credit_row"] is None
        ):
            unmapped.append(item)

    warnings: list[str] = []
    if parsed["detected_date"] is None:
        warnings.append(
            "В файле не нашлась дата данных — проверьте, на какой день грузите"
        )
    if unmapped:
        total = sum(
            item["debit_turnover"] + item["credit_turnover"] for item in unmapped
        )
        warnings.append(
            f"{len(unmapped)} счетов с оборотом не попали ни в одну статью "
            f"(всего {total:,.2f} ₽) — они не отразятся в календаре".replace(",", " ")
        )
    if any(item["confirm"] for item in rows):
        warnings.append(
            "Часть счетов разнесена по правилу, которое стоит подтвердить: "
            "474 и 458 (прочее / кредиты) и 313 (депозит ЮЛ)"
        )

    existing = session.execute(
        select(LedgerRow.id).where(LedgerRow.load_date == parsed["load_date"]).limit(1)
    ).first()
    if existing:
        warnings.append(
            f"На {parsed['load_date']:%d.%m.%Y} выгрузка уже загружена — "
            "она будет заменена целиком"
        )

    return {
        **parsed,
        "articles": sorted(
            (
                {**bucket, "amount": round(bucket["amount"], 2)}
                for bucket in by_article.values()
            ),
            key=lambda bucket: bucket["row_title"],
        ),
        "unmapped": unmapped,
        "unmapped_count": len(unmapped),
        "accounts": len(rows),
        "debit_total": round(sum(item["debit_turnover"] for item in rows), 2),
        "credit_total": round(sum(item["credit_turnover"] for item in rows), 2),
        "replaces": bool(existing),
        "warnings": warnings,
    }


def apply(
    session: Session, payload: dict[str, Any], *, source_file: str | None = None
) -> dict[str, Any]:
    """Записать выгрузку на дату, заменив прежнюю целиком.

    Замена, а не дозапись: выгрузка — это полный срез дня, и добавление второй
    её копии удвоило бы обороты в календаре. Прошлые дни при этом не трогаем.
    """
    load_date = parse_date(payload.get("load_date")) or date.today()
    rows = payload.get("rows") or []

    removed = session.execute(
        delete(LedgerRow).where(LedgerRow.load_date == load_date)
    ).rowcount or 0

    written = 0
    seen: set[str] = set()
    for item in rows:
        account = normalise_account(item.get("account"))
        if len(account) < MIN_ACCOUNT_DIGITS or account in seen:
            continue
        seen.add(account)
        session.add(
            LedgerRow(
                load_date=load_date,
                account=account,
                account_name=item.get("account_name") or None,
                currency=(item.get("currency") or "RUB")[:8],
                opening_balance=parse_number(item.get("opening_balance")) or 0.0,
                debit_turnover=abs(parse_number(item.get("debit_turnover")) or 0.0),
                credit_turnover=abs(parse_number(item.get("credit_turnover")) or 0.0),
                closing_balance=parse_number(item.get("closing_balance")) or 0.0,
                source_file=source_file,
            )
        )
        written += 1

    session.commit()
    return {"load_date": load_date, "written": written, "removed": removed}


def drop(session: Session, load_date: date) -> int:
    """Убрать выгрузку за день — например, если загрузили не тот файл."""
    removed = session.execute(
        delete(LedgerRow).where(LedgerRow.load_date == load_date)
    ).rowcount or 0
    session.commit()
    return removed
