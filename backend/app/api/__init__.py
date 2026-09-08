"""HTTP-интерфейс казначейского терминала.

Проверка доступа навешивается на роутеры целиком, а не на отдельные ручки.
Так безопаснее: забыть зависимость в одном новом обработчике легко, и он
молча окажется открытым, а терминал может стоять в интернете.

При выключенной проверке (``TREASURY_AUTH_ENABLED=false``) зависимость
пропускает всех, поэтому запуск на своей машине не меняется.

Снаружи остаются только те точки, без которых нельзя войти:
``/api/auth/mode``, ``/api/auth/login``, ``/api/auth/logout`` — они объявлены
в ``system`` и защиты не требуют по существу, — и ``/api/health``, по которому
удобно проверять, что сервис жив.
"""
from fastapi import APIRouter, Depends

from ..services.auth import require_section
from . import admin, bonds, cash, export, imports, market, portfolio, ratios, system, treasury

#: Минимум для любого обращения: вход плюс право на раздел, к которому
#: относится путь. Уровень прав на запись (сделки, настройки) запрашивается
#: отдельно в самих обработчиках.
_authenticated = [Depends(require_section)]

api_router = APIRouter()
api_router.include_router(market.router, dependencies=_authenticated)
api_router.include_router(bonds.router, dependencies=_authenticated)
api_router.include_router(portfolio.router, dependencies=_authenticated)
api_router.include_router(cash.router, dependencies=_authenticated)
api_router.include_router(export.router, dependencies=_authenticated)
api_router.include_router(imports.router, dependencies=_authenticated)
api_router.include_router(treasury.router, dependencies=_authenticated)
api_router.include_router(ratios.router, dependencies=_authenticated)
# Вход и состояние сервиса. Проверка раздела пропускает /api/auth и
# /api/health до всякой авторизации — иначе войти было бы нечем, — а роль
# для записи по-прежнему запрашивается в самих обработчиках
api_router.include_router(system.router, dependencies=_authenticated)
api_router.include_router(admin.router, dependencies=_authenticated)

__all__ = ["api_router"]
