"""Meta endpoints — health, currency, skill.md."""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, JSONResponse

from idempotency import idempotency_store

from api import (
    db,
    _SKILL_MD,
    CURRENCY_SYMBOL, CURRENCY_NAME, STARTING_BALANCE,
)

router = APIRouter()


@router.get("/skill.md", tags=["meta"], include_in_schema=False)
async def get_skill_md():
    """Return a markdown skill file describing this API for agent auto-discovery."""
    return PlainTextResponse(_SKILL_MD, media_type="text/markdown; charset=utf-8")


@router.get("/health", tags=["meta"])
async def health():
    """Health check endpoint."""
    db_status = "ok"
    try:
        conn = db._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            db._put_conn(conn)
    except Exception:
        db_status = "unreachable"

    if db_status != "ok":
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "db": db_status,
            },
        )

    market_count = db.count_markets()
    user_count = db.count_users()
    return {
        "status": "ok",
        "db": "ok",
        "markets": market_count,
        "users": user_count,
        "idempotency_keys_cached": idempotency_store.size,
        "currency": {
            "symbol": CURRENCY_SYMBOL,
            "name": CURRENCY_NAME,
        },
    }


@router.get("/currency", tags=["meta"])
async def get_currency():
    """Get platform currency info. MoltMarkets uses points (ŧ), not real money."""
    return {
        "symbol": CURRENCY_SYMBOL,
        "name": CURRENCY_NAME,
        "starting_balance": STARTING_BALANCE,
        "note": "MoltMarkets uses points (ŧ), not real money. All balances and amounts are in points.",
    }
