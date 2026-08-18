"""Заседания Банка России по ключевой ставке.

Календарь сам по себе — просто список дат. Полезным его делает связка с
историей ставки: по прошедшему заседанию видно, какое решение приняли и на
сколько изменили ставку, а по будущему — сколько дней осталось и будет ли
опубликован среднесрочный прогноз.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MacroRate, RateMeeting

#: Сколько прошедших заседаний показывать. Восемь плановых в год — примерно
#: столько и укладывается в год, о котором просили
DEFAULT_HISTORY = 8

#: Решение вступает в силу не в день заседания, а через несколько дней.
#: В этом окне и ищем новое значение ставки.
_EFFECTIVE_WINDOW_DAYS = 10

KIND_TITLES = {
    "regular": "Плановое заседание",
    "extraordinary": "Внеочередное заседание",
    "other": "Событие календаря",
}


def _rate_at(rates: Sequence[tuple[date, float]], moment: date) -> float | None:
    """Ставка, действовавшая на указанный день."""
    value = None
    for rate_date, rate in rates:
        if rate_date <= moment:
            value = rate
        else:
            break
    return value


def _decision(
    rates: Sequence[tuple[date, float]], meeting_date: date
) -> tuple[float | None, float | None]:
    """Ставка после заседания и её изменение в процентных пунктах.

    Сравниваем значение накануне заседания с тем, что действует после того,
    как решение вступило в силу. Если ставку не меняли, изменение — ноль, и
    это тоже решение: «сохранили».
    """
    before = _rate_at(rates, meeting_date - timedelta(days=1))
    after = _rate_at(rates, meeting_date + timedelta(days=_EFFECTIVE_WINDOW_DAYS))
    if after is None:
        return None, None
    change = None if before is None else round(after - before, 2)
    return after, change


def _serialise(meeting: RateMeeting, today: date, rate: float | None,
               change: float | None) -> dict[str, Any]:
    days = (meeting.meeting_date - today).days
    return {
        "date": meeting.meeting_date,
        "title": meeting.title,
        "kind": meeting.kind,
        "kind_title": KIND_TITLES.get(meeting.kind, meeting.kind),
        "with_forecast": meeting.with_forecast,
        "days": days,
        "past": days < 0,
        "rate": rate,
        "rate_change": change,
        "links": json.loads(meeting.links) if meeting.links else [],
    }


def schedule(
    session: Session, history: int = DEFAULT_HISTORY
) -> dict[str, Any]:
    """Ближайшие заседания и решения последних месяцев.

    Прочие события календаря (доклад о ДКП, резюме обсуждения) в список не
    попадают: спрашивают про ставку, а не про публикации.
    """
    today = date.today()
    meetings = list(
        session.execute(
            select(RateMeeting)
            .where(RateMeeting.kind.in_(("regular", "extraordinary")))
            .order_by(RateMeeting.meeting_date)
        ).scalars()
    )

    rates = [
        (row[0], row[1])
        for row in session.execute(
            select(MacroRate.rate_date, MacroRate.value)
            .where(MacroRate.code == "KEY_RATE")
            .order_by(MacroRate.rate_date)
        ).all()
    ]

    upcoming: list[dict[str, Any]] = []
    past: list[dict[str, Any]] = []
    for meeting in meetings:
        if meeting.meeting_date >= today:
            upcoming.append(_serialise(meeting, today, None, None))
        else:
            rate, change = _decision(rates, meeting.meeting_date)
            past.append(_serialise(meeting, today, rate, change))

    current = rates[-1][1] if rates else None
    return {
        "current_rate": current,
        "current_rate_date": rates[-1][0] if rates else None,
        "next": upcoming[0] if upcoming else None,
        "upcoming": upcoming,
        # Свежие сверху: чаще смотрят последнее решение, а не позапрошлое
        "past": list(reversed(past[-history:])),
        "source": "Банк России, календарь заседаний по ключевой ставке",
    }
