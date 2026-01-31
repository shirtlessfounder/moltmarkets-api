"""
Route modules for MoltMarkets API.

Each module defines an APIRouter that is mounted by api.py via app.include_router().
"""

from routes.markets import router as markets_router
from routes.trading import router as trading_router
from routes.agents import router as agents_router
from routes.chat import router as chat_router
from routes.admin import router as admin_router
from routes.meta import router as meta_router

__all__ = [
    "markets_router",
    "trading_router",
    "agents_router",
    "chat_router",
    "admin_router",
    "meta_router",
]
