"""Тесты денежной позиции, импорта, истории, доступа и налогов."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    CashAccount,
    CashFlow,
    CorpAction,
    Deal,
    FxRate,
    Instrument,
    Placement,
    Quote,
    User,
)
from app.services import auth as auth_service
from app.services import cash as cash_service
from app.services import history as history_service
from app.services import importer as importer_service
from app.services import treasury_extras as extras_service


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        yield active


def add_bond(session, secid, **kwargs):
    instrument = Instrument(
        secid=secid, board="TQCB", engine="stock", market="bonds", kind="bond",
        short_name=secid, isin=f"RU{secid}", face_value=1000.0, face_unit="SUR",
        **kwargs,
    )
    session.add(instrument)
    session.flush()
    return instrument


def add_account(session, name="Расчётный", currency="RUB", portfolio="Основной"):
    account = CashAccount(name=name, currency=currency, portfolio=portfolio)
    session.add(account)
    session.flush()
    return account


def add_flow(session, account, amount, on_date=None, *, planned=False, kind="deposit"):
    flow = CashFlow(
        account_id=account.id,
        flow_date=on_date or date.today(),
        amount=amount,
        kind=kind,
        is_planned=planned,
    )
    session.add(flow)
    session.flush()
    return flow


# ----------------------------------------------------------------------
# Проценты по размещениям
# ----------------------------------------------------------------------
def test_accrued_interest_counts_actual_days():
    """Простые проценты по фактическому числу дней, база 365."""
    today = date.today()
    placement = Placement(
        kind="deposit", amount=1_000_000, currency="RUB", rate=18.25,
        start_date=today - timedelta(days=73), end_date=today + timedelta(days=30),
    )
    # 1 000 000 × 18,25% × 73/365 = 36 500
    assert cash_service.accrued_interest(placement, today) == pytest.approx(36_500, abs=1)


def test_accrued_interest_stops_at_maturity():
    """После окончания срока проценты больше не набегают."""
    end = date.today() - timedelta(days=10)
    placement = Placement(
        kind="deposit", amount=1_000_000, currency="RUB", rate=18.25,
        start_date=end - timedelta(days=365), end_date=end,
    )
    assert cash_service.accrued_interest(placement) == pytest.approx(182_500, abs=2)


def test_placement_before_start_has_no_interest():
    start = date.today() + timedelta(days=5)
    placement = Placement(
        kind="deposit", amount=1_000_000, currency="RUB", rate=18.0,
        start_date=start, end_date=start + timedelta(days=90),
    )
    assert cash_service.accrued_interest(placement) == 0.0


# ----------------------------------------------------------------------
# Денежная позиция
# ----------------------------------------------------------------------
def test_position_separates_placed_and_borrowed(session):
    """Размещённое и привлечённое нельзя показывать одним сальдо."""
    account = add_account(session)
    add_flow(session, account, 10_000_000)
    today = date.today()
    session.add_all([
        Placement(kind="deposit", amount=5_000_000, currency="RUB", rate=18.0,
                  start_date=today - timedelta(days=30), end_date=today + timedelta(days=60),
                  portfolio="Основной"),
        Placement(kind="repo", amount=3_000_000, currency="RUB", rate=17.0,
                  start_date=today - timedelta(days=10), end_date=today + timedelta(days=20),
                  portfolio="Основной"),
    ])
    session.commit()

    position = cash_service.cash_position(session, portfolio="Основной")
    assert position["total_cash_rub"] == 10_000_000
    assert position["placed_out_rub"] == 5_000_000
    assert position["borrowed_rub"] == 3_000_000
    assert position["placed_rub"] == 2_000_000
    # Ставка считается только по размещённому, привлечение её не размывает
    assert position["weighted_placement_rate"] == 18.0


def test_planned_flow_not_in_balance(session):
    """Плановое движение — ещё не деньги на счёте."""
    account = add_account(session)
    add_flow(session, account, 1_000_000)
    add_flow(session, account, 500_000, planned=True)
    session.commit()

    position = cash_service.cash_position(session)
    assert position["total_cash_rub"] == 1_000_000


def test_foreign_account_converted_to_rubles(session):
    account = add_account(session, name="Валютный", currency="USD")
    add_flow(session, account, 1_000)
    session.add(
        FxRate(source="cbr", code="USD", rate_date=date.today(), value=90.0, nominal=1)
    )
    session.commit()

    position = cash_service.cash_position(session)
    assert position["total_cash_rub"] == 90_000
    assert position["accounts"][0]["balance"] == 1_000


# ----------------------------------------------------------------------
# Платёжный календарь
# ----------------------------------------------------------------------
def test_settled_flow_not_counted_twice(session):
    """Состоявшееся движение уже в остатке и в календарь не попадает.

    Иначе сегодняшнее пополнение показывалось бы дважды и остаток удваивался.
    """
    account = add_account(session)
    add_flow(session, account, 12_000_000, date.today())
    session.commit()

    calendar = cash_service.payment_calendar(session, portfolio="Основной")
    assert calendar["opening_balance"] == 12_000_000
    assert calendar["closing_balance"] == 12_000_000
    assert all(event["source"] != "manual" for event in calendar["events"])


def test_planned_flow_today_stays_in_calendar(session):
    """Плановое движение в остаток не вошло — значит, оно впереди."""
    account = add_account(session)
    add_flow(session, account, 1_000_000)
    add_flow(session, account, -400_000, date.today(), planned=True, kind="tax")
    session.commit()

    calendar = cash_service.payment_calendar(session, portfolio="Основной")
    assert calendar["opening_balance"] == 1_000_000
    assert calendar["closing_balance"] == 600_000


def test_calendar_finds_cash_gap(session):
    """Разрыв — первый день, когда накопленный остаток уходит в минус."""
    account = add_account(session)
    add_flow(session, account, 1_000_000)
    gap_day = date.today() + timedelta(days=20)
    add_flow(session, account, -2_500_000, gap_day, planned=True, kind="withdrawal")
    session.commit()

    calendar = cash_service.payment_calendar(session, portfolio="Основной")
    assert calendar["has_gap"] is True
    assert calendar["gap_date"] == gap_day
    assert calendar["lowest_balance"] == -1_500_000


def test_placement_return_appears_in_calendar(session):
    """Возврат депозита с процентами — будущий приток."""
    add_account(session)
    today = date.today()
    session.add(Placement(
        kind="deposit", amount=1_000_000, currency="RUB", rate=18.25,
        start_date=today - timedelta(days=100), end_date=today + timedelta(days=265),
        portfolio="Основной",
    ))
    session.commit()

    calendar = cash_service.payment_calendar(
        session, portfolio="Основной", horizon_days=365
    )
    returns = [e for e in calendar["events"] if e["kind"] == "placement_return"]
    assert len(returns) == 1
    # 1 000 000 + проценты за 365 дней под 18,25% = 1 182 500
    assert returns[0]["amount"] == pytest.approx(1_182_500, abs=2)


def test_repo_return_is_outflow(session):
    """По привлечению возвращаем мы — это отток."""
    add_account(session)
    today = date.today()
    session.add(Placement(
        kind="repo", amount=1_000_000, currency="RUB", rate=17.0,
        start_date=today - timedelta(days=5), end_date=today + timedelta(days=25),
        portfolio="Основной",
    ))
    session.commit()

    calendar = cash_service.payment_calendar(session, portfolio="Основной")
    returns = [e for e in calendar["events"] if e["kind"] == "placement_return"]
    assert len(returns) == 1
    assert returns[0]["amount"] < 0


# ----------------------------------------------------------------------
# Импорт сделок
# ----------------------------------------------------------------------
def _workbook_bytes(rows) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_header_found_below_report_title(session):
    """Шапку отчёта нельзя принять за строку заголовков.

    Слово «счёт» в подписи «Выписка по счёту депо» само по себе распознаётся
    как колонка портфеля и раньше перебивало настоящий заголовок.
    """
    add_bond(session, "AAA")
    session.commit()

    content = _workbook_bytes([
        ["Выписка по счёту депо в НРД на 11.08.2026"],
        [],
        ["Код выпуска", "Остаток, шт"],
        ["AAA", 250],
    ])
    result = importer_service.reconcile(session, content, "v.xlsx")
    assert result["errors"] == []
    assert result["differences"][0]["statement_quantity"] == 250


def test_preview_marks_unknown_instrument(session):
    add_bond(session, "AAA")
    session.commit()

    content = _workbook_bytes([
        ["Отчёт брокера"],
        [],
        ["Дата сделки", "Код бумаги", "Операция", "Кол-во, шт", "Цена, %", "НКД", "Комиссия"],
        ["03.07.2026", "AAA", "Покупка", 250, 98.4, 11.2, 45],
        ["04.07.2026", "НЕТУ", "Продажа", 10, 100, 0, 0],
    ])
    result = importer_service.preview_import(session, content, "deals.xlsx")

    assert result["valid"] == 1
    assert result["invalid"] == 1
    assert result["deals"][0]["side"] == "buy"
    assert result["deals"][0]["trade_date"] == date(2026, 7, 3)
    assert result["deals"][1]["problems"]


def test_apply_import_accepts_json_types(session):
    """Строки возвращаются из предпросмотра через JSON: дата приходит текстом."""
    add_bond(session, "AAA")
    session.commit()

    result = importer_service.apply_import(session, [
        {"secid": "AAA", "side": "buy", "quantity": "100", "price": "99,5",
         "trade_date": "2026-07-03", "fee": "45", "portfolio": "Основной"},
    ])
    assert result["created"] == 1
    deal = session.query(Deal).one()
    assert deal.trade_date == date(2026, 7, 3)
    assert deal.price == 99.5


def test_apply_import_skips_unknown_instrument(session):
    result = importer_service.apply_import(session, [
        {"secid": "НЕТУ", "quantity": 10, "price": 100, "trade_date": "2026-07-03"},
    ])
    assert result["created"] == 0
    assert result["skipped_count"] == 1


def test_negative_quantity_means_sale(session):
    """Некоторые отчёты не пишут направление, а кодируют его знаком."""
    add_bond(session, "AAA")
    session.commit()

    result = importer_service.apply_import(session, [
        {"secid": "AAA", "quantity": -50, "price": 100, "trade_date": "2026-07-03"},
    ])
    assert result["created"] == 1
    deal = session.query(Deal).one()
    assert deal.side == "sell"
    assert deal.quantity == 50


def test_number_parsing_handles_russian_format():
    assert importer_service.parse_number("1 234,56") == 1234.56
    assert importer_service.parse_number("1\xa0234,56") == 1234.56
    assert importer_service.parse_number("") is None
    assert importer_service.parse_number("—") is None


# ----------------------------------------------------------------------
# История стоимости
# ----------------------------------------------------------------------
def test_snapshot_is_rewritten_for_same_day(session):
    """За один день остаётся одна точка — последняя известная оценка."""
    account = add_account(session)
    add_flow(session, account, 1_000_000)
    session.commit()

    history_service.take_snapshot(session, portfolio="Основной")
    add_flow(session, account, 500_000)
    session.commit()
    history_service.take_snapshot(session, portfolio="Основной")

    result = history_service.portfolio_history(session, portfolio="Основной")
    assert result["snapshots"] == 1
    assert result["points"][0]["total_value"] == 1_500_000


def test_drawdown_measured_from_peak():
    values = [100.0, 120.0, 90.0, 110.0]
    depth, index = history_service._drawdown(values)
    assert depth == -25.0
    assert index == 2


def test_history_without_snapshots_says_so(session):
    result = history_service.portfolio_history(session)
    assert result["snapshots"] == 0
    assert "накапливаются" in result["note"]


# ----------------------------------------------------------------------
# Доступ и роли
# ----------------------------------------------------------------------
def test_password_hash_is_salted_and_verifiable():
    first = auth_service.hash_password("parol")
    second = auth_service.hash_password("parol")
    # Соль случайная: одинаковые пароли дают разные хеши
    assert first != second
    assert auth_service.verify_password("parol", first)
    assert not auth_service.verify_password("drugoy", first)
    assert not auth_service.verify_password("parol", "мусор")


def test_login_rejects_wrong_password(session):
    from fastapi import HTTPException

    auth_service.create_user(session, login="ivan", password="parol-ivana")
    with pytest.raises(HTTPException) as info:
        auth_service.login(session, "ivan", "ne-tot")
    assert info.value.status_code == 401


def test_login_issues_token(session):
    auth_service.create_user(session, login="ivan", password="parol-ivana", role="trader")
    result = auth_service.login(session, "IVAN", "parol-ivana")
    assert result["role"] == "trader"
    assert len(result["token"]) > 20


def test_disabled_user_cannot_log_in(session):
    from fastapi import HTTPException

    user = auth_service.create_user(session, login="ivan", password="parol-ivana")
    user.active = False
    session.commit()
    with pytest.raises(HTTPException):
        auth_service.login(session, "ivan", "parol-ivana")


def test_admin_created_only_once(session):
    from app.config import settings

    settings.admin_password = "vremennyy-parol"
    try:
        assert auth_service.ensure_admin(session) is not None
        assert auth_service.ensure_admin(session) is None
        assert session.query(User).count() == 1
    finally:
        settings.admin_password = ""


def test_audit_records_action(session):
    auth_service.audit(
        session, {"login": "ivan"}, action="create", entity="deal", entity_id=7,
        detail="AAA 100 шт",
    )
    from app.models import AuditRecord

    record = session.query(AuditRecord).one()
    assert record.user_login == "ivan"
    assert record.entity_id == "7"


# ----------------------------------------------------------------------
# Налоги и оферты
# ----------------------------------------------------------------------
def test_after_tax_yield_taxes_parts_separately():
    """Купон и прирост стоимости облагаются по своим ставкам."""
    # Купонная часть 10% годовых, всего доходность 15%: 5% — прирост
    result = extras_service.after_tax_yield(
        15.0, coupon_percent=100.0, price=1000.0, profit_tax=20.0, coupon_tax=30.0
    )
    # 10 × 0,7 + 5 × 0,8 = 11
    assert result == pytest.approx(11.0)


def test_after_tax_yield_without_coupon_uses_profit_tax():
    assert extras_service.after_tax_yield(10.0, profit_tax=25.0, coupon_tax=13.0) == 7.5


def test_after_tax_yield_of_unknown_is_unknown():
    assert extras_service.after_tax_yield(None) is None


def test_upcoming_offer_found_for_own_bond(session):
    today = date.today()
    offer_day = today + timedelta(days=20)
    bond = add_bond(session, "AAA", offer_date=offer_day)
    session.add(Quote(
        instrument_id=bond.id, ts=datetime.utcnow(), last=100.0, accrued_interest=0.0,
    ))
    session.add(Deal(
        secid="AAA", side="buy", quantity=10, price=100.0,
        trade_date=today - timedelta(days=30), portfolio="Основной",
    ))
    session.commit()

    offers = extras_service.upcoming_offers(session, portfolio="Основной")
    assert len(offers) == 1
    assert offers[0]["days_left"] == 20
    assert offers[0]["severity"] == "warning"


def test_offer_within_two_weeks_is_critical(session):
    today = date.today()
    bond = add_bond(session, "AAA", offer_date=today + timedelta(days=5))
    session.add(Quote(instrument_id=bond.id, ts=datetime.utcnow(), last=100.0))
    session.add(Deal(
        secid="AAA", side="buy", quantity=10, price=100.0,
        trade_date=today - timedelta(days=30), portfolio="Основной",
    ))
    session.commit()

    offers = extras_service.upcoming_offers(session, portfolio="Основной")
    assert offers[0]["severity"] == "critical"


def test_offer_from_nsd_schedule_is_not_duplicated(session):
    """Оферта известна и из справочника площадки, и из графика НРД."""
    today = date.today()
    offer_day = today + timedelta(days=30)
    bond = add_bond(session, "AAA", offer_date=offer_day)
    session.add(Quote(instrument_id=bond.id, ts=datetime.utcnow(), last=100.0))
    session.add(CorpAction(
        isin=bond.isin, secid="AAA", action_type="offer", action_date=offer_day,
    ))
    session.add(Deal(
        secid="AAA", side="buy", quantity=10, price=100.0,
        trade_date=today - timedelta(days=30), portfolio="Основной",
    ))
    session.commit()

    offers = extras_service.upcoming_offers(session, portfolio="Основной")
    assert len(offers) == 1


def test_events_report_cash_gap(session):
    account = add_account(session)
    add_flow(session, account, 100_000)
    add_flow(
        session, account, -1_000_000, date.today() + timedelta(days=10),
        planned=True, kind="withdrawal",
    )
    session.commit()

    events = extras_service.collect_events(session, portfolio="Основной")
    assert any(event["event"] == "cash_gap" for event in events)
