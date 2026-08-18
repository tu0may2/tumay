"""Заседания ЦБ по ключевой ставке: разбор календаря и связка с решениями."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, MacroRate, RateMeeting
from app.services import keyrate
from app.sources.cbr import CbrSource


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        yield active


def _meeting(session, day: date, **overrides):
    row = RateMeeting(
        meeting_date=day,
        title=overrides.get("title", "Заседание Совета директоров по ключевой ставке"),
        kind=overrides.get("kind", "regular"),
        with_forecast=overrides.get("with_forecast", False),
        links=overrides.get("links"),
    )
    session.add(row)
    session.commit()
    return row


def _rates(session, points):
    """points — [(дата, ставка)]."""
    for day, value in points:
        session.add(
            MacroRate(code="KEY_RATE", name="Ключевая", value=value, rate_date=day)
        )
    session.commit()


class TestSchedule:
    def test_next_meeting_is_the_closest_future_one(self, session):
        today = date.today()
        _meeting(session, today + timedelta(days=40))
        _meeting(session, today + timedelta(days=10))
        _meeting(session, today - timedelta(days=5))

        result = keyrate.schedule(session)
        assert result["next"]["date"] == today + timedelta(days=10)
        assert result["next"]["days"] == 10
        assert result["next"]["past"] is False

    def test_upcoming_and_past_do_not_mix(self, session):
        today = date.today()
        _meeting(session, today + timedelta(days=5))
        _meeting(session, today - timedelta(days=5))
        _meeting(session, today - timedelta(days=60))

        result = keyrate.schedule(session)
        assert [m["date"] for m in result["upcoming"]] == [today + timedelta(days=5)]
        assert len(result["past"]) == 2

    def test_today_counts_as_upcoming(self, session):
        """В день заседания решение ещё впереди — прятать его в историю рано."""
        today = date.today()
        _meeting(session, today)

        result = keyrate.schedule(session)
        assert result["next"]["days"] == 0
        assert result["past"] == []

    def test_past_meetings_come_newest_first(self, session):
        today = date.today()
        for offset in (10, 60, 120):
            _meeting(session, today - timedelta(days=offset))

        past = keyrate.schedule(session)["past"]
        assert [m["date"] for m in past] == sorted(
            (m["date"] for m in past), reverse=True
        )

    def test_history_is_limited(self, session):
        today = date.today()
        for offset in range(1, 15):
            _meeting(session, today - timedelta(days=offset * 30))

        assert len(keyrate.schedule(session, history=4)["past"]) == 4

    def test_other_calendar_events_are_not_meetings(self, session):
        """Доклад о ДКП и резюме обсуждения — не заседания по ставке."""
        today = date.today()
        _meeting(session, today + timedelta(days=3), kind="other",
                 title="Доклад о денежно-кредитной политике")
        _meeting(session, today + timedelta(days=7))

        result = keyrate.schedule(session)
        assert len(result["upcoming"]) == 1
        assert result["next"]["date"] == today + timedelta(days=7)

    def test_extraordinary_meeting_is_marked(self, session):
        today = date.today()
        _meeting(session, today + timedelta(days=2), kind="extraordinary",
                 title="Внеочередное заседание Совета директоров по ключевой ставке")

        assert keyrate.schedule(session)["next"]["kind_title"] == "Внеочередное заседание"

    def test_links_are_returned_as_a_list(self, session):
        today = date.today()
        _meeting(
            session, today + timedelta(days=2),
            links=json.dumps([{"title": "Пресс-релиз", "url": "https://cbr.ru/x"}]),
        )
        links = keyrate.schedule(session)["next"]["links"]
        assert links == [{"title": "Пресс-релиз", "url": "https://cbr.ru/x"}]

    def test_empty_calendar_does_not_break(self, session):
        result = keyrate.schedule(session)
        assert result["next"] is None
        assert result["upcoming"] == [] and result["past"] == []


class TestDecisions:
    """Решение восстанавливается по истории ставки вокруг даты заседания."""

    def test_rate_cut_is_recognised(self, session):
        today = date.today()
        meeting = today - timedelta(days=30)
        _meeting(session, meeting)
        _rates(session, [
            (meeting - timedelta(days=1), 16.5),
            (meeting + timedelta(days=3), 16.0),
            (today, 16.0),
        ])

        decision = keyrate.schedule(session)["past"][0]
        assert decision["rate"] == 16.0
        assert decision["rate_change"] == -0.5

    def test_rate_hold_gives_zero_change(self, session):
        """Сохранить ставку — тоже решение, и его нельзя показать пустым."""
        today = date.today()
        meeting = today - timedelta(days=20)
        _meeting(session, meeting)
        _rates(session, [
            (meeting - timedelta(days=1), 14.0),
            (meeting + timedelta(days=3), 14.0),
        ])

        decision = keyrate.schedule(session)["past"][0]
        assert decision["rate"] == 14.0
        assert decision["rate_change"] == 0

    def test_rate_hike_is_recognised(self, session):
        today = date.today()
        meeting = today - timedelta(days=20)
        _meeting(session, meeting)
        _rates(session, [
            (meeting - timedelta(days=1), 12.0),
            (meeting + timedelta(days=2), 13.0),
        ])

        assert keyrate.schedule(session)["past"][0]["rate_change"] == 1.0

    def test_future_meeting_has_no_decision(self, session):
        _meeting(session, date.today() + timedelta(days=5))
        _rates(session, [(date.today(), 14.0)])

        assert keyrate.schedule(session)["next"]["rate"] is None

    def test_current_rate_is_the_latest_known(self, session):
        _rates(session, [
            (date.today() - timedelta(days=40), 15.0),
            (date.today() - timedelta(days=2), 14.0),
        ])
        assert keyrate.schedule(session)["current_rate"] == 14.0


class TestCalendarParsing:
    """Разбор страницы календаря ЦБ — единственный источник расписания."""

    PAGE = """
      <div class="calendar-main-events">
        <div class="main-events_day">
          <div class="date col-md-5">13&nbsp;февраля 2026 года</div>
          <div class="main-events">
            <div class="main-event">
              <div class="title"><span>Заседание Совета директоров Банка России
                по&nbsp;ключевой ставке</span>
                <div class="icon_wrapper"><div class="icon-important"></div></div>
              </div>
              <div class="info"><a href="/press/pr/?file=13022026.htm">Пресс-релиз</a>
                <a href="/Content/forecast.pdf">Среднесрочный прогноз</a></div>
            </div>
          </div>
        </div>
        <div class="main-events_day">
          <div class="date col-md-5">15&nbsp;августа 2023 года</div>
          <div class="main-events">
            <div class="main-event">
              <div class="title"><span>Внеочередное заседание Совета директоров
                Банка России по&nbsp;ключевой ставке</span>
                <div class="icon_wrapper"><div></div></div>
              </div>
              <div class="info"><a href="/press/pr/?file=15082023.htm">Пресс-релиз</a></div>
            </div>
          </div>
        </div>
        <div class="main-events_day">
          <div class="date col-md-5">20&nbsp;марта 2026 года</div>
          <div class="main-events">
            <div class="main-event">
              <div class="title"><span>Доклад о&nbsp;денежно-кредитной политике</span>
                <div class="icon_wrapper"><div></div></div>
              </div>
              <div class="info"></div>
            </div>
          </div>
        </div>
      </div>
    """

    async def _parse(self, monkeypatch, page: str):
        class FakeResponse:
            text = page

        async def fake_get(self, path, **params):
            return FakeResponse()

        monkeypatch.setattr(CbrSource, "get", fake_get)
        async with CbrSource() as cbr:
            return await cbr.fetch_rate_calendar()

    @pytest.mark.asyncio
    async def test_dates_and_kinds(self, monkeypatch):
        events = await self._parse(monkeypatch, self.PAGE)
        by_date = {event["meeting_date"]: event for event in events}

        assert by_date[date(2026, 2, 13)]["kind"] == "regular"
        assert by_date[date(2023, 8, 15)]["kind"] == "extraordinary"
        # Доклад о ДКП — событие календаря, но не заседание по ставке
        assert by_date[date(2026, 3, 20)]["kind"] == "other"

    @pytest.mark.asyncio
    async def test_forecast_meeting_is_flagged(self, monkeypatch):
        events = await self._parse(monkeypatch, self.PAGE)
        by_date = {event["meeting_date"]: event for event in events}

        assert by_date[date(2026, 2, 13)]["with_forecast"] is True
        assert by_date[date(2023, 8, 15)]["with_forecast"] is False

    @pytest.mark.asyncio
    async def test_links_become_absolute(self, monkeypatch):
        events = await self._parse(monkeypatch, self.PAGE)
        links = {event["meeting_date"]: event["links"] for event in events}

        urls = [link["url"] for link in links[date(2026, 2, 13)]]
        assert all(url.startswith("https://") for url in urls)
        assert any("13022026" in url for url in urls)

    @pytest.mark.asyncio
    async def test_events_are_sorted_by_date(self, monkeypatch):
        events = await self._parse(monkeypatch, self.PAGE)
        dates = [event["meeting_date"] for event in events]
        assert dates == sorted(dates)

    @pytest.mark.asyncio
    async def test_unavailable_page_does_not_break_collection(self, monkeypatch):
        async def fake_get(self, path, **params):
            raise RuntimeError("ЦБ недоступен")

        monkeypatch.setattr(CbrSource, "get", fake_get)
        async with CbrSource() as cbr:
            assert await cbr.fetch_rate_calendar() == []
