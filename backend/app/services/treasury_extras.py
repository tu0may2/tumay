"""Налоги, ближайшие оферты и уведомления."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CorpAction, Instrument, NotificationRule
from .limits import check_limits
from .portfolio import compute_positions

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Налоги
# ----------------------------------------------------------------------
def after_tax_yield(
    yield_pct: float | None,
    coupon_percent: float | None = None,
    price: float | None = None,
    *,
    profit_tax: float | None = None,
    coupon_tax: float | None = None,
) -> float | None:
    """Доходность после налога.

    Доходность к погашению складывается из купонного потока и переоценки к
    номиналу. Ставки налога на купон и на прибыль могут отличаться, поэтому
    компоненты облагаются отдельно: купонная часть оценивается как текущая
    доходность купона к цене, остаток относим к приросту стоимости.
    """
    if yield_pct is None:
        return None

    profit_tax = settings.profit_tax_pct if profit_tax is None else profit_tax
    coupon_tax = settings.coupon_tax_pct if coupon_tax is None else coupon_tax

    coupon_component = 0.0
    if coupon_percent and price:
        coupon_component = min(coupon_percent / price * 100, yield_pct)
    gain_component = max(yield_pct - coupon_component, 0.0)

    net = (
        coupon_component * (1 - coupon_tax / 100)
        + gain_component * (1 - profit_tax / 100)
    )
    return round(net, 2)


def tax_settings() -> dict[str, Any]:
    return {
        "profit_tax_pct": settings.profit_tax_pct,
        "coupon_tax_pct": settings.coupon_tax_pct,
        "note": (
            "Ставки задаются переменными TREASURY_PROFIT_TAX_PCT и "
            "TREASURY_COUPON_TAX_PCT. Расчёт упрощённый: он не учитывает "
            "льготы по отдельным выпускам и особенности учётной политики."
        ),
    }


# ----------------------------------------------------------------------
# Оферты по своим бумагам
# ----------------------------------------------------------------------
def upcoming_offers(
    session: Session, *, portfolio: str | None = None, horizon_days: int = 90
) -> list[dict[str, Any]]:
    """Ближайшие оферты по бумагам в портфеле.

    Пропущенная оферта с правом предъявления означает, что позиция останется
    в портфеле на весь оставшийся срок выпуска, — поэтому предупреждение
    выделено отдельно от общего календаря.
    """
    positions = [
        p for p in compute_positions(session, portfolio=portfolio) if p["quantity"] > 0
    ]
    if not positions:
        return []

    today = date.today()
    until = today + timedelta(days=horizon_days)
    by_secid = {p["secid"]: p for p in positions}

    instruments = {
        instrument.secid: instrument
        for instrument in session.execute(
            select(Instrument).where(Instrument.secid.in_(list(by_secid)))
        ).scalars()
    }

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, date]] = set()

    # Дата оферты приходит и из справочника площадки, и из графика НРД
    for secid, instrument in instruments.items():
        if instrument.offer_date and today <= instrument.offer_date <= until:
            key = (secid, instrument.offer_date)
            seen.add(key)
            rows.append(_offer_row(by_secid[secid], instrument.offer_date, today, "справочник MOEX"))

    isins = {inst.isin for inst in instruments.values() if inst.isin}
    if isins:
        for action in session.execute(
            select(CorpAction).where(
                CorpAction.isin.in_(isins),
                CorpAction.action_type == "offer",
                CorpAction.action_date >= today,
                CorpAction.action_date <= until,
            )
        ).scalars():
            secid = next(
                (s for s, i in instruments.items() if i.isin == action.isin), None
            )
            if secid is None or (secid, action.action_date) in seen:
                continue
            seen.add((secid, action.action_date))
            row = _offer_row(by_secid[secid], action.action_date, today, "раскрытие НРД")
            row["accept_from"] = action.start_date
            row["accept_until"] = action.record_date
            rows.append(row)

    rows.sort(key=lambda row: row["offer_date"])
    return rows


def _offer_row(
    position: dict[str, Any], offer_date: date, today: date, source: str
) -> dict[str, Any]:
    days_left = (offer_date - today).days
    return {
        "secid": position["secid"],
        "name": position["name"],
        "isin": position.get("isin"),
        "quantity": position["quantity"],
        "market_value_rub": position.get("market_value_rub"),
        "offer_date": offer_date,
        "days_left": days_left,
        "source": source,
        "severity": "critical" if days_left <= 14 else "warning",
    }


# ----------------------------------------------------------------------
# Уведомления
# ----------------------------------------------------------------------
def _post_webhook(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> bool:
    """Отправить событие на вебхук.

    Подходит любой приёмник JSON: Telegram через бота, Slack, Mattermost или
    внутренний сервис. Ошибка доставки не должна ломать работу терминала.
    """
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.warning("Уведомление не доставлено на %s: %s", url, exc)
        return False


def collect_events(
    session: Session, *, portfolio: str | None = None
) -> list[dict[str, Any]]:
    """Собрать события, о которых стоит сообщить."""
    events: list[dict[str, Any]] = []

    limits = check_limits(session, portfolio=portfolio)
    for row in limits["items"]:
        if row["breached"]:
            events.append({
                "event": "limit_breach",
                "severity": "critical",
                "title": f"Нарушен лимит: {row['kind_title']} ({row['subject']})",
                "detail": f"Факт {row['actual']} при лимите {row['limit_value']}",
            })

    for offer in upcoming_offers(session, portfolio=portfolio, horizon_days=30):
        events.append({
            "event": "offer_soon",
            "severity": offer["severity"],
            "title": f"Оферта по {offer['secid']} через {offer['days_left']} дн.",
            "detail": f"{offer['name']}, дата {offer['offer_date']:%d.%m.%Y}",
        })

    from .cash import payment_calendar

    calendar = payment_calendar(session, portfolio=portfolio, horizon_days=90)
    if calendar["has_gap"]:
        events.append({
            "event": "cash_gap",
            "severity": "critical",
            "title": f"Кассовый разрыв {calendar['gap_date']:%d.%m.%Y}",
            "detail": f"Минимальный остаток {calendar['lowest_balance']:,.0f} ₽".replace(",", " "),
        })

    return events


def notify(
    session: Session, *, portfolio: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Разослать события по настроенным правилам."""
    from datetime import datetime

    events = collect_events(session, portfolio=portfolio)
    rules = list(
        session.execute(
            select(NotificationRule).where(NotificationRule.enabled.is_(True))
        ).scalars()
    )

    sent = 0
    failed = 0
    for rule in rules:
        wanted = {code.strip() for code in (rule.events or "").split(",") if code.strip()}
        matching = [event for event in events if event["event"] in wanted]
        if not matching:
            continue
        if dry_run:
            sent += len(matching)
            continue

        payload = {
            "source": "Казначейский терминал",
            "portfolio": portfolio,
            "count": len(matching),
            "events": matching,
            # Многие приёмники (Telegram, Slack) ждут текст в поле text
            "text": "\n".join(f"• {item['title']} — {item['detail']}" for item in matching),
        }
        if _post_webhook(rule.webhook_url, payload):
            rule.last_sent_at = datetime.utcnow()
            sent += len(matching)
        else:
            failed += 1

    if not dry_run:
        session.commit()

    return {
        "events_found": len(events),
        "rules": len(rules),
        "sent": sent,
        "failed": failed,
        "events": events,
        "dry_run": dry_run,
    }
