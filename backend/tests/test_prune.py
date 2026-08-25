"""Очистка устаревших срезов котировок.

Срез снимается каждые пять минут по всем бумагам — за сутки это десятки
тысяч строк. Без уборки таблица растёт без предела, а по ней проходит почти
каждая страница терминала. Здесь проверяется, что уборка удаляет только
лишнее и не задевает то, без чего бумага пропадёт с экрана.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Instrument, Quote


@pytest.fixture()
def collector(monkeypatch, tmp_path):
    """Сборщик поверх временной базы."""
    import app.db as db_module

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    from contextlib import contextmanager

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr("app.services.collector.session_scope", scope)

    from app.services.collector import Collector

    instance = Collector()
    instance._factory = factory  # для проверок в тестах
    return instance


def _fill(factory, *, instruments: int, days: int, per_day: int = 1):
    """Наполнить базу срезами за несколько дней."""
    now = datetime.utcnow()
    with factory() as session:
        ids = []
        for index in range(instruments):
            item = Instrument(
                secid=f"SEC{index}", board="TQCB", engine="stock",
                market="bonds", kind="bond",
            )
            session.add(item)
            session.flush()
            ids.append(item.id)

        for day in range(days):
            for slot in range(per_day):
                ts = now - timedelta(days=day, minutes=slot)
                for instrument_id in ids:
                    session.add(Quote(instrument_id=instrument_id, ts=ts, last=100.0))
        session.commit()
    return ids


class TestPruneQuotes:
    def test_old_snapshots_are_removed(self, collector):
        factory = collector._factory
        _fill(factory, instruments=3, days=30)

        with factory() as session:
            before = session.execute(select(func.count(Quote.id))).scalar()

        removed = collector.prune_quotes(keep_days=7)

        with factory() as session:
            after = session.execute(select(func.count(Quote.id))).scalar()
        assert removed > 0
        assert after == before - removed

    def test_recent_snapshots_survive(self, collector):
        factory = collector._factory
        _fill(factory, instruments=2, days=30)
        collector.prune_quotes(keep_days=7)

        cutoff = datetime.utcnow() - timedelta(days=7)
        with factory() as session:
            stale = session.execute(
                select(func.count(Quote.id)).where(Quote.ts < cutoff)
            ).scalar()
            fresh = session.execute(
                select(func.count(Quote.id)).where(Quote.ts >= cutoff)
            ).scalar()
        assert stale == 0
        assert fresh > 0

    def test_every_instrument_keeps_at_least_one_quote(self, collector):
        """По неликвидной бумаге срез может быть единственным и старым.

        Удалить его — значит убрать бумагу из всех витрин: они строятся по
        последней известной котировке.
        """
        factory = collector._factory
        with factory() as session:
            item = Instrument(
                secid="DEAD", board="TQCB", engine="stock", market="bonds", kind="bond"
            )
            session.add(item)
            session.flush()
            # Единственный срез, снятый год назад
            session.add(
                Quote(
                    instrument_id=item.id,
                    ts=datetime.utcnow() - timedelta(days=365),
                    last=99.0,
                )
            )
            session.commit()
            instrument_id = item.id

        collector.prune_quotes(keep_days=7)

        with factory() as session:
            left = session.execute(
                select(func.count(Quote.id)).where(Quote.instrument_id == instrument_id)
            ).scalar()
        assert left == 1

    def test_nothing_to_remove_is_not_an_error(self, collector):
        _fill(collector._factory, instruments=2, days=2)
        assert collector.prune_quotes(keep_days=7) == 0

    def test_retention_can_be_switched_off(self, collector):
        _fill(collector._factory, instruments=2, days=30)
        assert collector.prune_quotes(keep_days=0) == 0

    def test_run_is_capped_so_it_does_not_stall_the_server(self, collector, monkeypatch):
        """Первая уборка на запущенной базе не должна идти одним заходом.

        Большое удаление на медленном диске держит долгую транзакцию, и
        терминал в это время не отвечает. Остаток убирается в следующем цикле.
        """
        from app.config import settings

        monkeypatch.setattr(settings, "quote_prune_max_rows", 20)
        monkeypatch.setattr(settings, "quote_prune_batch", 5)

        factory = collector._factory
        _fill(factory, instruments=5, days=30)

        first = collector.prune_quotes(keep_days=7)
        assert first == 20

        # Следующий заход продолжает с того же места
        second = collector.prune_quotes(keep_days=7)
        assert second == 20

    def test_deletes_in_batches_not_one_transaction(self, collector, monkeypatch):
        """Порции — не деталь реализации, а способ не блокировать чтение."""
        from app.config import settings

        monkeypatch.setattr(settings, "quote_prune_batch", 7)
        monkeypatch.setattr(settings, "quote_prune_max_rows", 1000)

        commits = {"count": 0}
        original = collector.prune_quotes

        factory = collector._factory
        _fill(factory, instruments=3, days=30)

        # Считаем транзакции по числу открытий сессии
        import app.services.collector as module

        real_scope = module.session_scope

        from contextlib import contextmanager

        @contextmanager
        def counting_scope():
            commits["count"] += 1
            with real_scope() as session:
                yield session

        monkeypatch.setattr(module, "session_scope", counting_scope)
        removed = original(keep_days=7)

        assert removed > 7
        # Порция по семь строк — значит заходов было несколько
        assert commits["count"] > 1


class TestTradingHours:
    """Ночью и в выходные цены не меняются, а снимки копятся.

    Это больше половины строк в таблице котировок, и каждая из них
    замедляет отбор «последняя котировка по каждой бумаге», через который
    проходит почти каждая страница терминала.
    """

    def test_weekday_daytime_is_trading(self):
        from app.services.collector import is_trading_time

        # Вторник, 12:00 МСК — это 09:00 UTC
        assert is_trading_time(datetime(2026, 8, 25, 9, 0)) is True

    def test_night_is_not_trading(self):
        from app.services.collector import is_trading_time

        # Вторник, 03:00 МСК — это 00:00 UTC
        assert is_trading_time(datetime(2026, 8, 25, 0, 0)) is False

    def test_weekend_is_not_trading(self):
        from app.services.collector import is_trading_time

        # Суббота, полдень
        assert is_trading_time(datetime(2026, 8, 29, 9, 0)) is False
        # Воскресенье
        assert is_trading_time(datetime(2026, 8, 30, 9, 0)) is False

    def test_evening_session_is_still_trading(self):
        from app.services.collector import is_trading_time

        # Вторник, 22:00 МСК — вечерняя сессия идёт
        assert is_trading_time(datetime(2026, 8, 25, 19, 0)) is True

    def test_collection_is_skipped_outside_trading_hours(self, collector, monkeypatch):
        """Планировщик не должен снимать срез в выходной."""
        import asyncio

        from app.config import settings

        monkeypatch.setattr(settings, "collect_in_trading_hours_only", True)
        monkeypatch.setattr(
            "app.services.collector.is_trading_time", lambda *a, **k: False
        )

        # Если проверка не сработает, коллектор пойдёт в сеть и тест упадёт
        assert asyncio.run(collector.collect_quotes()) == 0

    def test_manual_collection_ignores_the_clock(self, collector, monkeypatch):
        """Кнопка «Собрать данные» должна работать и в выходной.

        Человек нажал осознанно — отказывать ему из-за расписания неверно.
        """
        import asyncio

        from app.config import settings

        monkeypatch.setattr(settings, "collect_in_trading_hours_only", True)
        monkeypatch.setattr(
            "app.services.collector.is_trading_time", lambda *a, **k: False
        )

        called = {"fetched": False}

        class FakeMoex:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def fetch_boards(self, boards):
                called["fetched"] = True
                return []

        monkeypatch.setattr("app.services.collector.MoexSource", FakeMoex)
        asyncio.run(collector.collect_quotes(force=True))
        assert called["fetched"], "принудительный сбор не дошёл до биржи"


class TestPruneCapacity:
    def test_cap_outpaces_daily_growth(self):
        """Потолок уборки должен перекрывать суточный прирост.

        Четыре тысячи бумаг со срезом раз в пять минут дают около миллиона
        строк в сутки. Если за час убирать меньше, чем набегает, таблица
        растёт, сколько её ни чисти, — так она и доросла до семи миллионов.
        """
        from app.config import Settings

        defaults = Settings()
        instruments = 4000
        snapshots_per_day = 24 * 60 / 5
        daily_growth = instruments * snapshots_per_day

        # Уборка идёт раз в час
        runs_per_day = 24 * 3600 / defaults.housekeeping_interval_sec
        capacity = defaults.quote_prune_max_rows * runs_per_day
        assert capacity > daily_growth, (
            f"уборка убирает {capacity:,.0f} строк в сутки при приросте "
            f"{daily_growth:,.0f} — отставание навсегда"
        )
