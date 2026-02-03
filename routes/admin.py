"""
Admin endpoints — user management, market transitions, key regeneration,
and retroactive resolution fixes.
"""

import logging
import secrets

from fastapi import APIRouter, Header, HTTPException, Request

from auth import generate_api_key, ADMIN_SECRET
from cpmm import get_cpmm_probability
from deps import get_db
from errors import error_response, ErrorCode
from market_cache import market_cache
from models import MarketStatus, Outcome
from rate_limiter import rate_limiter
from storage import hash_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


@router.delete("/admin/users/{username}")
async def admin_delete_user(username: str, request: Request, x_admin_secret: str = Header(None)):
    """Delete a user by username (admin only). Requires X-Admin-Secret header."""
    db = get_db()
    if not ADMIN_SECRET:
        return error_response(503, "Admin endpoints disabled — ADMIN_SECRET not configured", ErrorCode.SERVICE_UNAVAILABLE)

    client_ip = request.client.host if request.client else "unknown"
    allowed, info = rate_limiter.check(f"admin:{client_ip}", max_requests=10, window_seconds=60)
    if not allowed:
        return error_response(429, f"Admin rate limit exceeded. {info['detail']}", ErrorCode.RATE_LIMITED)

    if not secrets.compare_digest(x_admin_secret or "", ADMIN_SECRET):
        return error_response(403, "Invalid admin secret", ErrorCode.FORBIDDEN)

    user = db.get_user_by_username(username)
    if not user:
        return error_response(404, f"User '{username}' not found", ErrorCode.USER_NOT_FOUND)

    db.delete_user(user["id"])

    return {"deleted": True, "username": username, "user_id": user["id"]}


@router.post("/admin/transition-markets")
async def admin_transition_markets(request: Request, x_admin_secret: str = Header(None)):
    """Batch-transition all OPEN markets past closes_at to RESOLVING.

    Requires X-Admin-Secret header.
    """
    db = get_db()
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin endpoints disabled — ADMIN_SECRET not configured")

    client_ip = request.client.host if request.client else "unknown"
    allowed, info = rate_limiter.check(f"admin:{client_ip}", max_requests=10, window_seconds=60)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Admin rate limit exceeded. {info['detail']}")

    if not secrets.compare_digest(x_admin_secret or "", ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="Invalid admin secret")

    count = db.transition_expired_markets()
    market_cache.invalidate()
    return {"transitioned": count}


@router.post("/admin/users/{username}/regenerate-key")
async def admin_regenerate_api_key(username: str, request: Request, x_admin_secret: str = Header(None)):
    """Regenerate API key for a user (admin only). Returns the new API key."""
    db = get_db()
    if not ADMIN_SECRET:
        return error_response(503, "Admin endpoints disabled — ADMIN_SECRET not configured", ErrorCode.SERVICE_UNAVAILABLE)

    client_ip = request.client.host if request.client else "unknown"
    allowed, info = rate_limiter.check(f"admin:{client_ip}", max_requests=10, window_seconds=60)
    if not allowed:
        return error_response(429, f"Admin rate limit exceeded. {info['detail']}", ErrorCode.RATE_LIMITED)

    if not secrets.compare_digest(x_admin_secret or "", ADMIN_SECRET):
        return error_response(403, "Invalid admin secret", ErrorCode.FORBIDDEN)

    user = db.get_user_by_username(username)
    if not user:
        return error_response(404, f"User '{username}' not found", ErrorCode.USER_NOT_FOUND)

    new_api_key = generate_api_key()
    key_hash = hash_api_key(new_api_key)

    db.update_user_api_key(user["id"], key_hash)

    return {
        "username": username,
        "user_id": user["id"],
        "api_key": new_api_key,
        "warning": "Save this key! It will not be shown again.",
    }


@router.post("/admin/fix-resolutions")
async def admin_fix_resolutions(
    request: Request,
    dry_run: bool = True,
    x_admin_secret: str = Header(None),
):
    """Retroactively fix all resolved markets that still have pool residuals.

    For each RESOLVED market this endpoint will:
    1. Credit the winning-side pool residual to the market creator.
    2. Set the pool to its terminal state so probability reads 1.0 or 0.0.
    3. Zero out all positions (already paid out — prevents double-counting).

    The operation is **idempotent**: markets whose winning pool is already
    ≤ 0.001ŧ are skipped (already fixed).

    Query params:
        dry_run (bool): If true (default), report what *would* change without
                        touching the database.  Set to ``false`` to apply.

    Requires ``X-Admin-Secret`` header.
    """
    db = get_db()
    if not ADMIN_SECRET:
        return error_response(
            503,
            "Admin endpoints disabled — ADMIN_SECRET not configured",
            ErrorCode.SERVICE_UNAVAILABLE,
        )

    client_ip = request.client.host if request.client else "unknown"
    allowed, info = rate_limiter.check(
        f"admin:{client_ip}", max_requests=5, window_seconds=60,
    )
    if not allowed:
        return error_response(
            429, f"Admin rate limit exceeded. {info['detail']}", ErrorCode.RATE_LIMITED,
        )

    if not secrets.compare_digest(x_admin_secret or "", ADMIN_SECRET):
        return error_response(403, "Invalid admin secret", ErrorCode.FORBIDDEN)

    # Gather all resolved markets
    all_markets = db.list_markets()
    resolved = [
        m for m in all_markets if m["status"] == MarketStatus.RESOLVED
    ]

    fixes = []
    total_residual = 0.0
    creator_payouts: dict[str, float] = {}  # creator_id → total credited

    for market in resolved:
        mid = market["id"]
        resolution = market["resolution"]
        pool = market["pool"]

        if resolution is None:
            continue  # shouldn't happen, but be safe

        winning_pool = (
            pool["YES"] if resolution == Outcome.YES else pool["NO"]
        )

        # Idempotency guard: already fixed if pool residual is negligible
        if winning_pool <= 0.001:
            continue

        creator = db.get_user(market["creator_id"])
        creator_name = creator["username"] if creator else "unknown"
        prob_before = get_cpmm_probability(pool, market["p"])

        # Determine terminal pool state
        if resolution == Outcome.YES:
            terminal_pool = {"YES": 0.0, "NO": 1.0}
            target_prob = 1.0
        else:
            terminal_pool = {"YES": 1.0, "NO": 0.0}
            target_prob = 0.0

        # Collect position info for zeroing
        positions = db.get_market_positions(mid)
        position_count = len(positions)

        fix_record = {
            "market_id": mid,
            "title": market["title"],
            "resolution": resolution.value,
            "probability_before": round(prob_before, 6),
            "probability_after": target_prob,
            "pool_before": {
                "YES": round(pool["YES"], 4),
                "NO": round(pool["NO"], 4),
            },
            "pool_after": terminal_pool,
            "winning_pool_residual": round(winning_pool, 4),
            "creator": creator_name,
            "creator_id": market["creator_id"],
            "positions_zeroed": position_count,
        }

        if not dry_run:
            # 1. Credit residual to creator
            db.update_user_balance(
                market["creator_id"], winning_pool,
                tx_type="creator_recovery", market_id=mid,
            )
            logger.info(
                "[fix-resolutions] Credited %.4fŧ to %s for market %s",
                winning_pool, creator_name, mid,
            )

            # 2. Set pool to terminal state
            db.update_market_pool(mid, terminal_pool, market["p"], 0)

            # 3. Zero out positions
            for pos in positions:
                if pos["yes_shares"] > 0:
                    db.reduce_position(mid, pos["user_id"], Outcome.YES, pos["yes_shares"])
                if pos["no_shares"] > 0:
                    db.reduce_position(mid, pos["user_id"], Outcome.NO, pos["no_shares"])

            fix_record["applied"] = True
        else:
            fix_record["applied"] = False

        fixes.append(fix_record)
        total_residual += winning_pool
        creator_payouts[market["creator_id"]] = (
            creator_payouts.get(market["creator_id"], 0) + winning_pool
        )

    if not dry_run:
        market_cache.invalidate()

    # Build creator summary
    creator_summary = []
    for cid, amount in sorted(creator_payouts.items(), key=lambda x: -x[1]):
        creator = db.get_user(cid)
        creator_summary.append({
            "creator_id": cid,
            "username": creator["username"] if creator else "unknown",
            "total_credited": round(amount, 4),
        })

    return {
        "dry_run": dry_run,
        "markets_fixed": len(fixes),
        "total_residual_distributed": round(total_residual, 4),
        "creator_summary": creator_summary,
        "fixes": fixes,
    }
