"""Точка входа приложения казначейского терминала."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import api_router
from .config import settings
from .db import init_db, session_scope
from .services.auth import ensure_admin
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

    if settings.auth_enabled:
        # Пароль администратора печатается в журнал ровно один раз — при
        # создании учётной записи; хранится только его хеш
        with session_scope() as session:
            ensure_admin(session)

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
    # При включённом входе встроенные адреса документации выключаем и отдаём
    # их сами, администратору. Иначе полная карта API — адреса, параметры,
    # схемы — доступна любому, кто открыл терминал, ещё до входа: подсказка
    # для того, кто ищет, что тут можно нажать.
    docs_url=None if settings.auth_enabled else "/docs",
    redoc_url=None if settings.auth_enabled else "/redoc",
    openapi_url=None if settings.auth_enabled else "/openapi.json",
)

if settings.auth_enabled:
    from fastapi import Depends
    from fastapi.openapi.docs import get_swagger_ui_html

    from .services.auth import require_admin

    @app.get("/openapi.json", include_in_schema=False)
    def protected_openapi(user: dict = Depends(require_admin)) -> dict:
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    def protected_docs(user: dict = Depends(require_admin)):
        return get_swagger_ui_html(openapi_url="/openapi.json", title=settings.app_name)

#: Заголовки, которые браузер понимает как ограничения. Ставим их в
#: приложении, а не только в nginx: терминал запускают и без прокси —
#: локально через туннель, — и защита не должна зависеть от того, кто
#: его сегодня публикует.
_SECURITY_HEADERS = {
    # Терминал не должен открываться внутри чужого фрейма: иначе поверх него
    # рисуют невидимый слой и заставляют нажимать не то, что видит человек
    "X-Frame-Options": "DENY",
    # Не угадывать тип содержимого: выгрузка .csv не должна быть истолкована
    # как HTML и выполнена
    "X-Content-Type-Options": "nosniff",
    # Не утекать адресом терминала на сторонние сайты по ссылкам
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # Скрипты только свои: инлайновых скриптов и обработчиков в разметке нет,
    # поэтому строгое правило ничего не ломает и закрывает главный путь
    # эксплуатации XSS, если он где-то всё же найдётся. Стили инлайновые
    # есть (атрибуты style), поэтому для них послабление.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    ),
}


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # HSTS имеет смысл только поверх HTTPS: на голом http он лишний, а
    # выставленный по ошибке заголовок закроет доступ к терминалу, пока
    # сертификат не появится
    if request.url.scheme == "https" or request.headers.get(
        "x-forwarded-proto"
    ) == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


# Интерфейс живёт на том же адресе, что и API, поэтому кросс-доменные
# запросы терминалу не нужны вовсе. Раньше здесь стояло allow_origins=["*"],
# то есть любая посторонняя страница могла обращаться к API от имени
# браузера посетителя. Список задаётся настройкой на случай, если интерфейс
# когда-нибудь вынесут на отдельный адрес.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origin_list),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["X-Auth-Token", "Content-Type"],
    )

app.include_router(api_router)

def _asset_version(static_dir: Path) -> str:
    """Отпечаток статики: меняется при любой правке скриптов или стилей.

    Подставляется в адреса ассетов, иначе браузер после обновления версии
    продолжает выполнять закэшированный скрипт: разметка новая, обработчики
    старые — и кнопки «не работают».
    """
    digest = hashlib.sha256()
    for path in sorted(static_dir.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest.update(path.name.encode())
            digest.update(str(stat.st_mtime_ns).encode())
            digest.update(str(stat.st_size).encode())
    return digest.hexdigest()[:12]


# Веб-интерфейс отдаётся тем же приложением — отдельный сервер не нужен
_frontend = settings.frontend_dir
if _frontend.is_dir():
    app.mount(
        "/static", StaticFiles(directory=_frontend / "static"), name="static"
    )

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        # Версию считаем на каждый запрос: при разработке файлы меняются,
        # а страница открывается редко — стоимость незаметна
        version = _asset_version(_frontend / "static")
        html = (_frontend / "index.html").read_text(encoding="utf-8")
        html = html.replace("__ASSET_VERSION__", version)
        return HTMLResponse(
            html,
            # Саму страницу браузер обязан перепроверять, иначе он не узнает
            # о новой версии ассетов
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

else:  # pragma: no cover — на случай запуска без собранного фронтенда
    logger.warning("Каталог фронтенда не найден: %s", _frontend)
