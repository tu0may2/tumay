"""Фоновое обновление данных по расписанию."""
from __future__ import annotations

import asyncio
import logging

from ..config import settings
from .collector import collector

logger = logging.getLogger(__name__)


class Scheduler:
    """Два независимых цикла: частый — котировки, редкий — справочники."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    async def _loop(self, name: str, interval: int, action) -> None:
        """Повторять action раз в interval секунд, переживая ошибки."""
        while not self._stopping.is_set():
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — цикл не должен умирать
                logger.error("Планировщик %s: ошибка (%s)", name, exc)

            try:
                # Ожидание, прерываемое остановкой приложения
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _refresh_reference(self) -> None:
        await collector.collect_reference()
        await collector.collect_history()
        await collector.collect_corp_actions()

    async def _housekeeping(self) -> None:
        """Снимок стоимости за сегодня и рассылка событий.

        Снимок перезаписывает запись за текущую дату, поэтому цикл может
        отрабатывать сколько угодно раз за день: в истории останется одна
        точка на дату — последняя известная оценка.
        """
        # Работа с БД синхронная: уносим её из цикла событий
        await asyncio.to_thread(self._housekeeping_sync)

    def _housekeeping_sync(self) -> None:
        from ..db import session_scope
        from .collector import Collector
        from .history import take_snapshot
        from .portfolio import portfolio_names
        from .treasury_extras import notify

        # Чистим срезы до снимков: дальше по циклу таблица уже не мешает
        try:
            Collector().prune_quotes()
        except Exception as exc:  # noqa: BLE001 — уборка не должна ронять цикл
            logger.warning("Очистка котировок не выполнена: %s", exc)

        with session_scope() as session:
            if settings.snapshots_enabled:
                # Снимок по каждому портфелю и общий по всем сразу
                for name in [None, *portfolio_names(session)]:
                    take_snapshot(session, portfolio=name)

            result = notify(session)
            if result["sent"]:
                logger.info(
                    "Уведомления: отправлено %s событий, ошибок %s",
                    result["sent"],
                    result["failed"],
                )

    def start(self) -> None:
        if self._tasks:
            return
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(
                self._loop("quotes", settings.quotes_interval_sec, collector.collect_quotes),
                name="treasury-quotes",
            ),
            asyncio.create_task(
                self._loop(
                    "reference", settings.reference_interval_sec, self._refresh_reference
                ),
                name="treasury-reference",
            ),
            asyncio.create_task(
                self._loop(
                    "housekeeping", settings.housekeeping_interval_sec, self._housekeeping
                ),
                name="treasury-housekeeping",
            ),
        ]
        logger.info(
            "Планировщик запущен: котировки раз в %s с, справочники раз в %s с, "
            "снимки и уведомления раз в %s с",
            settings.quotes_interval_sec,
            settings.reference_interval_sec,
            settings.housekeeping_interval_sec,
        )

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("Планировщик остановлен")


scheduler = Scheduler()
