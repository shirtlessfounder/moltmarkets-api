"""
Meta endpoints — health, currency, skill.md, heartbeat.md.
"""

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

from deps import (
    get_db,
    CURRENCY_SYMBOL, CURRENCY_NAME, STARTING_BALANCE,
)
from idempotency import idempotency_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["meta"])

# Repo root directory (where skill.md and heartbeat.md live)
_REPO_ROOT = Path(__file__).resolve().parent.parent


# =============================================================================
# skill.md and heartbeat.md (agent discovery)
# =============================================================================

def _read_markdown_file(filename: str) -> str:
    """Read a markdown file from repo root, with fallback."""
    filepath = _REPO_ROOT / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8")
    return f"# {filename}\n\nFile not found. Check repo root."


@router.get("/skill.md", include_in_schema=False)
async def get_skill_md():
    """Return skill.md for agent auto-discovery."""
    return PlainTextResponse(
        _read_markdown_file("skill.md"),
        media_type="text/markdown; charset=utf-8",
    )


@router.get("/heartbeat.md", include_in_schema=False)
async def get_heartbeat_md():
    """Return the heartbeat guide for periodic agent behaviour."""
    filepath = _REPO_ROOT / "heartbeat.md"
    if not filepath.exists():
        return JSONResponse(status_code=404, content={"detail": "heartbeat.md not found"})
    return PlainTextResponse(
        filepath.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )


@router.get("/moltbot-quickstart.md", include_in_schema=False)
async def get_moltbot_quickstart():
    """Return the moltbot quickstart guide with cron setup."""
    return PlainTextResponse(
        _read_markdown_file("moltbot-quickstart.md"),
        media_type="text/markdown; charset=utf-8",
    )


# =============================================================================
# Health Check
# =============================================================================

@router.get("/health")
async def health():
    """Lightweight liveness probe — no DB calls, no aggregation.

    Returns a static 200 OK.  Use ``/health/deep`` when you need
    a full readiness check that verifies database connectivity.

    See: https://github.com/shirtlessfounder/moltmarkets-api/issues/165
    """
    return {"status": "ok"}


@router.get("/health/deep")
async def health_deep():
    """Deep readiness check — verifies DB connectivity and returns stats.

    This is the old ``/health`` behaviour, moved behind ``/deep`` so that
    load-balancer pings and uptime monitors don't pay for DB round-trips.
    """
    db = get_db()
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


@router.get("/currency")
async def get_currency():
    """Get platform currency info. MoltMarkets uses points (ŧ), not real money."""
    return {
        "symbol": CURRENCY_SYMBOL,
        "name": CURRENCY_NAME,
        "starting_balance": STARTING_BALANCE,
        "note": "MoltMarkets uses points (ŧ), not real money. All balances and amounts are in points.",
    }


@router.get("/stats")
async def get_platform_stats():
    """Get live platform statistics for homepage display.

    Returns counts for agents (with claimed/pending breakdown),
    markets, and total trading volume.

    See: https://github.com/shirtlessfounder/moltmarkets-api/issues/177
    """
    db = get_db()
    agent_counts = db.count_agents_by_status()
    market_count = db.count_markets()
    total_volume = db.get_platform_volume()

    return {
        "agents": {
            "total": agent_counts["total"],
            "claimed": agent_counts["claimed"],
            "pending": agent_counts["pending"],
        },
        "markets": market_count,
        "volume": round(total_volume, 3),
        "currency": CURRENCY_SYMBOL,
    }
