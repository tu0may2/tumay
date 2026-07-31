"""Точка входа приложения казначейского терминала."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import api_router
from .config import settings
from .db import init_db
from .services.collector import collector
from .services.scheduler import scheduler

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Подготовить БД, при необходимости собрать данные и включить расписание."""
    init_db()
    logger.info("Хранилище готово: %s", settings.database_url)

    if settings.collect_on_startup:
        # В фоне: терминал должен открываться сразу, не дожидаясь биржи
        asyncio.create_task(_initial_collection())

    if settings.scheduler_enabled:
        scheduler.start()

    try:
        yield
    finally:
        await scheduler.stop()


async def _initial_collection() -> None:
    try:
        summary = await collector.collect_all()
        logger.info("Стартовый сбор завершён: %s", summary)
    except Exception as exc:  # noqa: BLE001 — терминал работает и на старых данных
        logger.error("Стартовый сбор не удался: %s", exc)


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description=(
        "Сбор рыночных данных из открытых источников (MOEX ISS, Банк России, НРД) "
        "и аналитика для решений казначейства."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

# Веб-интерфейс отдаётся тем же приложением — отдельный сервер не нужен
_frontend = settings.frontend_dir
if _frontend.is_dir():
    app.mount(
        "/static", StaticFiles(directory=_frontend / "static"), name="static"
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_frontend / "index.html")

else:  # pragma: no cover — на случай запуска без собранного фронтенда
    logger.warning("Каталог фронтенда не найден: %s", _frontend)
