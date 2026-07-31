"""HTTP-интерфейс казначейского терминала."""
from fastapi import APIRouter

from . import admin, market, portfolio

api_router = APIRouter()
api_router.include_router(market.router)
api_router.include_router(portfolio.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
