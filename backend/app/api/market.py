"""Рыночные данные: обзор, витрина инструментов, история, кривая, сигналы."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Bar, CorpAction, FxRate, Instrument, MacroRate, Quote
from ..services import accrual, analytics, collateral, intraday, keyrate, payments, series
from ..services.tabular import to_csv, to_xlsx
from ..sources.moex import BOARD_SPECS

router = APIRouter(prefix="/api", tags=["Рынок"])


@router.get("/overview", summary="Сводка рынка")
def get_overview(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Ключевая ставка, курсы, индексы, обороты и кривая доходности."""
    return analytics.market_overview(session)


@router.get("/instruments", summary="Витрина инструментов")
def get_instruments(
    kind: list[str] | None = Query(None, description="share | bond | index | currency"),
    board: list[str] | None = Query(None, description="Режим торгов, например TQBR"),
    security_type: list[str] | None = Query(
        None, description="Вид бумаги: ofz_bond, corporate_bond, common_share…"
    ),
    search: str | None = Query(None, description="Поиск по коду, названию или ISIN"),
    min_turnover: float | None = Query(None, ge=0, description="Минимальный оборот, руб."),
    min_liquidity: float | None = Query(None, ge=0, le=100),
    min_yield: float | None = Query(None, description="Минимальная доходность, %"),
    max_yield: float | None = Query(None, description="Максимальная доходность, %"),
    max_duration_years: float | None = Query(None, ge=0),
    sort_by: str = Query("turnover"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Отбор бумаг по ликвидности, доходности и дюрации."""
    return analytics.screener(
        session,
        kinds=kind,
        boards=board,
        security_types=security_type,
        search=search,
        min_turnover=min_turnover,
        min_liquidity=min_liquidity,
        min_yield=min_yield,
        max_yield=max_yield,
        max_duration_years=max_duration_years,
        sort_by=sort_by,
        descending=order == "desc",
        limit=limit,
        offset=offset,
    )


@router.get("/instruments/security-types", summary="Виды бумаг для фильтра")
def get_security_types(
    kind: list[str] | None = Query(None, description="share | bond | index | currency"),
    session: Session = Depends(get_session),
) -> dict[str, list[dict[str, Any]]]:
    """Вложенный список видов бумаг, сгруппированный по классу инструмента.

    Отдаёт только виды, реально встретившиеся в загруженных данных — иначе
    выбор в фильтре часто вёл бы в пустую таблицу.

    Маршрут объявлен раньше ``/instruments/{secid}`` намеренно: FastAPI
    сопоставляет пути по порядку регистрации, и с обратным порядком запрос
    сюда подхватывался бы как карточка инструмента с secid="security-types".
    """
    return analytics.security_type_catalog(session, kinds=kind)


@router.get("/instruments/{secid}", summary="Карточка инструмента")
def get_instrument(
    secid: str,
    history_days: int = Query(180, ge=1, le=1825),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Всё, что известно о бумаге: параметры, рынок, история, выплаты."""
    secid = secid.upper()
    rows = analytics.latest_rows(session, secids=(secid,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"Инструмент {secid} не найден")

    instrument, quote = rows[0]
    curve = analytics.yield_curve(session)
    detail = analytics.build_row(instrument, quote, curve["points"])

    since = date.today() - timedelta(days=history_days)
    bars = session.execute(
        select(Bar)
        .where(Bar.instrument_id == instrument.id, Bar.trade_date >= since)
        .order_by(Bar.trade_date)
    ).scalars()

    intraday = session.execute(
        select(Quote.ts, Quote.last, Quote.turnover, Quote.volume)
        .where(Quote.instrument_id == instrument.id)
        .order_by(Quote.ts.desc())
        .limit(200)
    ).all()

    cashflows = []
    if instrument.isin:
        cashflows = [
            {
                "action_type": action.action_type,
                "action_date": action.action_date,
                "record_date": action.record_date,
                "value": action.value,
                "value_rub": action.value_rub,
                "value_pct": action.value_pct,
                "face_unit": action.face_unit,
                "source": action.source,
            }
            for action in session.execute(
                select(CorpAction)
                .where(CorpAction.isin == instrument.isin)
                .order_by(CorpAction.action_date)
            ).scalars()
        ]

    return {
        "instrument": detail,
        "history": [
            {
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "wa_price": bar.wa_price,
                "volume": bar.volume,
                "turnover": bar.turnover,
                "num_trades": bar.num_trades,
            }
            for bar in bars
        ],
        "intraday": [
            {"ts": ts, "last": last, "turnover": turnover, "volume": volume}
            for ts, last, turnover, volume in reversed(intraday)
        ],
        "cashflows": cashflows,
        # НКД на сегодня и на дату расчётов: в срезе биржа даёт только второй
        "accrual": accrual.accrual_profile(
            session,
            instrument,
            exchange_value=quote.accrued_interest if quote else None,
            settle_date=(quote.settle_date if quote else None),
        ) if instrument.kind == "bond" else None,
    }


@router.get("/instruments/{secid}/payments", summary="График выплат по выпуску")
def get_payments(
    secid: str,
    quantity: float = Query(1.0, gt=0, description="На сколько бумаг считать"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Купоны, амортизации и погашение плюс линия накопления НКД."""
    instrument = session.execute(
        select(Instrument).where(Instrument.secid == secid.upper()).limit(1)
    ).scalar_one_or_none()
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Инструмент {secid} не найден")
    return payments.payment_schedule(session, instrument, quantity=quantity)


@router.get("/collateral", summary="Список обеспечения Банка России")
def get_collateral(
    search: str | None = Query(None, description="Код, ISIN или эмитент"),
    mechanism: str | None = Query(None, description="ОМ или ДМ"),
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Бумаги, которые ЦБ принимает в залог, с поправочными коэффициентами."""
    from ..models import CbrCollateral

    statement = select(CbrCollateral).order_by(CbrCollateral.group_title, CbrCollateral.isin)
    if mechanism:
        statement = statement.where(CbrCollateral.mechanism == mechanism.upper())
    if search:
        needle = f"%{search.strip().lower()}%"
        statement = statement.where(
            func.lower(CbrCollateral.isin).like(needle)
            | func.lower(CbrCollateral.issuer).like(needle)
            | func.lower(CbrCollateral.reg_number).like(needle)
        )

    rows = list(session.execute(statement.limit(limit)).scalars())

    # Код бумаги нужен, чтобы по строке списка открывалась её карточка.
    # Список ЦБ ведётся по ISIN и шире того, что торгуется на наших
    # площадках, поэтому код находится не для каждой строки
    secid_by_isin = dict(
        session.execute(
            select(Instrument.isin, Instrument.secid).where(
                Instrument.isin.in_([row.isin for row in rows])
            )
        ).all()
    )

    return {
        "as_of": collateral.as_of(session),
        "total": session.execute(select(func.count(CbrCollateral.id))).scalar(),
        "items": [
            {
                "isin": row.isin,
                "secid": secid_by_isin.get(row.isin),
                "reg_number": row.reg_number,
                "issuer": row.issuer,
                "price_pct": row.price_pct,
                "value_rub": row.value_rub,
                "haircut": row.haircut,
                "mechanism": row.mechanism,
                "mechanism_title": collateral.MECHANISM_TITLES.get(
                    row.mechanism, row.mechanism
                ),
                "group": row.group_title,
                "maturity_date": row.maturity_date,
            }
            for row in rows
        ],
    }


@router.get("/instruments/{secid}/accrued", summary="НКД на дату")
def get_accrued(
    secid: str,
    on_date: date | None = Query(None, description="Дата, по умолчанию сегодня"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Накопленный купонный доход на выбранную дату.

    Биржевой срез содержит НКД только на дату расчётов. Здесь он считается по
    графику купонов на любой день — на начало торгов, на сегодня, на дату
    будущей сделки.
    """
    secid = secid.upper()
    rows = analytics.latest_rows(session, secids=(secid,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"Инструмент {secid} не найден")

    instrument, quote = rows[0]
    if instrument.kind != "bond":
        raise HTTPException(
            status_code=422, detail="НКД считается только по облигациям"
        )

    profile = accrual.accrual_profile(
        session,
        instrument,
        exchange_value=quote.accrued_interest if quote else None,
        settle_date=quote.settle_date if quote else None,
        on_date=on_date,
    )
    if profile["today"] is None and profile["selected"] is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Не хватает данных о купонах: у выпуска нет ни графика, "
                "ни величины купона в справочнике"
            ),
        )
    return profile


@router.get("/instruments/{secid}/intraday", summary="Ход торгов")
async def get_intraday(
    secid: str,
    interval: int = Query(10, description="Шаг свечи в минутах: 1, 10 или 60"),
    trades: int = Query(50, ge=1, le=500, description="Сколько сделок ленты вернуть"),
    on_date: date | None = Query(None, description="Дата сессии, по умолчанию сегодня"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Свечи, лента сделок и итоги текущей сессии.

    Данные берутся с биржи в момент запроса: собранный срез обновляется по
    расписанию и текущие торги показать не может.
    """
    try:
        return await intraday.trading_session(
            session, secid, interval=interval, trades_limit=trades, on_date=on_date
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/curve", summary="Кривая бескупонной доходности")
def get_curve(session: Session = Depends(get_session)) -> dict[str, Any]:
    """КБД МосБиржи — ориентир безрисковой стоимости денег по срокам."""
    return analytics.yield_curve(session)


@router.get("/movers", summary="Лидеры роста и падения")
def get_movers(
    kind: str = Query("share"),
    limit: int = Query(5, ge=1, le=50),
    min_turnover: float = Query(1_000_000, ge=0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return analytics.movers(session, kind=kind, limit=limit, min_turnover=min_turnover)


@router.get("/anomalies", summary="Аномалии торговых объёмов")
def get_anomalies(
    lookback_days: int = Query(30, ge=7, le=365),
    z_threshold: float = Query(2.0, ge=0.5, le=10),
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Бумаги, где объём заметно отклонился от собственной нормы."""
    return analytics.volume_anomalies(
        session, lookback_days=lookback_days, z_threshold=z_threshold, limit=limit
    )


@router.get("/alerts", summary="Сигналы для казначея")
def get_alerts(
    limit: int = Query(30, ge=1, le=100), session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    return analytics.alerts(session, limit=limit)


@router.get("/calendar", summary="Календарь выплат")
def get_calendar(
    horizon_days: int = Query(90, ge=1, le=730),
    secid: list[str] | None = Query(None),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Купоны, амортизации и оферты по данным НРД."""
    return analytics.cashflow_calendar(
        session, horizon_days=horizon_days, secids=secid
    )


@router.get("/fx", summary="Курсы валют ЦБ РФ")
def get_fx(
    days: int = Query(1, ge=1, le=365),
    code: list[str] | None = Query(None),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    since = date.today() - timedelta(days=days)
    statement = (
        select(FxRate)
        .where(FxRate.source == "cbr", FxRate.rate_date >= since)
        .order_by(FxRate.rate_date.desc(), FxRate.code)
    )
    if code:
        statement = statement.where(FxRate.code.in_([c.upper() for c in code]))
    return [
        {
            "code": rate.code,
            "name": rate.name,
            "nominal": rate.nominal,
            "value": rate.value,
            "unit_value": rate.value / (rate.nominal or 1),
            "date": rate.rate_date,
        }
        for rate in session.execute(statement).scalars()
    ]


@router.get("/rates", summary="Ставки денежного рынка")
def get_rates(
    code: str = Query("KEY_RATE", description="KEY_RATE | RUONIA"),
    days: int = Query(365, ge=1, le=3650),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    since = date.today() - timedelta(days=days)
    rows = session.execute(
        select(MacroRate)
        .where(MacroRate.code == code.upper(), MacroRate.rate_date >= since)
        .order_by(MacroRate.rate_date)
    ).scalars()
    return [
        {"code": row.code, "name": row.name, "value": row.value, "date": row.rate_date}
        for row in rows
    ]


@router.get("/series/catalog", summary="Каталог графиков обзора рынка")
def get_series_catalog() -> list[dict[str, Any]]:
    """Какие графики есть и какие показатели можно на них включить."""
    return series.catalog()


@router.get("/series/{chart}", summary="Данные графика за период")
def get_series(
    chart: str,
    metric: list[str] | None = Query(None, description="Коды показателей графика"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Ряды выбранных показателей. По умолчанию — за последние 12 месяцев."""
    try:
        return series.build(session, chart, metric, date_from, date_to)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/series/{chart}/download", summary="Выгрузить данные графика")
def download_series(
    chart: str,
    metric: list[str] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    fmt: Literal["xlsx", "csv"] = Query("xlsx"),
    session: Session = Depends(get_session),
) -> Response:
    """Те же ряды, что на графике, но файлом для Excel."""
    try:
        data = series.build(session, chart, metric, date_from, date_to)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    columns, rows = series.to_table(data)
    if not rows:
        raise HTTPException(
            status_code=404, detail="За выбранный период данных нет — выгружать нечего"
        )

    stem = (
        f"{data['title']} {data['date_from']:%d.%m.%Y}-{data['date_to']:%d.%m.%Y}"
    ).replace("/", "-")

    if fmt == "csv":
        content = to_csv(columns, rows)
        media_type = "text/csv; charset=utf-8"
        filename = f"{stem}.csv"
    else:
        content = to_xlsx(
            columns,
            rows,
            sheet_title=data["title"],
            meta=[
                (
                    "Период",
                    f"{data['date_from']:%d.%m.%Y} — {data['date_to']:%d.%m.%Y}",
                ),
                ("Источник", "Банк России, Московская биржа"),
                ("Сформировано", date.today().strftime("%d.%m.%Y")),
            ],
        )
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        filename = f"{stem}.xlsx"

    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("/rates/calendar", summary="Заседания ЦБ по ключевой ставке")
def get_rate_calendar(
    history: int = Query(
        keyrate.DEFAULT_HISTORY, ge=1, le=40,
        description="Сколько прошедших заседаний показать",
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Ближайшее заседание, расписание на год вперёд и прошлые решения."""
    return keyrate.schedule(session, history)


@router.get("/boards", summary="Доступные режимы торгов")
def get_boards(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Справочник площадок с числом загруженных инструментов."""
    counts = dict(
        session.execute(
            select(Instrument.board, func.count()).group_by(Instrument.board)
        ).all()
    )
    return [
        {
            "board": board,
            "title": spec["title"],
            "kind": spec["kind"],
            "instruments": counts.get(board, 0),
        }
        for board, spec in BOARD_SPECS.items()
    ]


