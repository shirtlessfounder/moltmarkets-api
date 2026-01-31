"""
Admin endpoints — user management, market transitions, key regeneration.
"""

import logging
import secrets

from fastapi import APIRouter, Header, HTTPException, Request

from auth import generate_api_key, ADMIN_SECRET
from deps import get_db
from errors import error_response, ErrorCode
from market_cache import market_cache
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
