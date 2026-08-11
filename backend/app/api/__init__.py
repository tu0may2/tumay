"""HTTP-интерфейс казначейского терминала."""
from fastapi import APIRouter

from . import admin, bonds, cash, export, imports, market, portfolio, system, treasury

api_router = APIRouter()
api_router.include_router(market.router)
api_router.include_router(bonds.router)
api_router.include_router(portfolio.router)
api_router.include_router(cash.router)
api_router.include_router(export.router)
api_router.include_router(imports.router)
api_router.include_router(treasury.router)
api_router.include_router(system.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
