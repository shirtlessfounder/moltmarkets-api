"""
MoltMarkets API — Authentication dependencies.

Extracted from api.py as part of the Phase 3 middleware refactor (#66).
Contains FastAPI dependency functions for API key auth and admin verification.
"""

import logging
import os
import secrets
from typing import Optional

from fastapi import Header, Request

from errors import APIError, ErrorCode
from rate_limiter import rate_limiter
from storage import Storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level database reference — set by api.py at startup via init_db()
# ---------------------------------------------------------------------------
_db: Optional[Storage] = None


def init_db(db: Storage) -> None:
    """Bind the global Storage instance so auth dependencies can query users.

    Must be called once at application startup, before any request is handled.
    """
    global _db
    _db = db


# ---------------------------------------------------------------------------
# API Key Generation
# ---------------------------------------------------------------------------

def generate_api_key() -> str:
    """Generate a secure API key with ``mm_`` prefix."""
    return f"mm_{secrets.token_urlsafe(32)}"


# ---------------------------------------------------------------------------
# Auth Dependencies (FastAPI Depends)
# ---------------------------------------------------------------------------

async def get_current_user(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> dict:
    """
    Authenticate via API key. Returns demo-user for anonymous reads.

    Accepts:
    - Authorization: Bearer mm_xxx
    - X-API-Key: mm_xxx
    """
    api_key = None

    # Try Authorization header first
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    # Then X-API-Key header
    elif x_api_key:
        api_key = x_api_key

    # If we have an API key, authenticate with it
    if api_key:
        user = _db.get_user_by_api_key(api_key)
        if not user:
            raise APIError(status_code=401, message="Invalid API key", code=ErrorCode.UNAUTHORIZED)
        return user

    # X-User-ID header removed — was a security bypass that allowed unauthenticated user creation
    # All users must now register via /agents/register and claim via twitter

    # No auth provided — use demo user for anonymous reads (read-only, zero balance)
    user = _db.get_user("demo-user")
    if not user:
        user = _db.create_user("demo-user", "demo_user", balance=0.0)
    return user


async def require_auth(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> dict:
    """
    Strict authentication required. No demo-user fallback.
    Use this for all write operations (bets, markets, comments).
    """
    api_key = None

    # Try Authorization header first
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    # Then X-API-Key header
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        raise APIError(
            status_code=401,
            message="Authentication required. Provide API key via 'Authorization: Bearer mm_xxx' or 'X-API-Key: mm_xxx' header.",
            code=ErrorCode.UNAUTHORIZED,
        )

    user = _db.get_user_by_api_key(api_key)
    if not user:
        raise APIError(status_code=401, message="Invalid API key", code=ErrorCode.UNAUTHORIZED)

    return user


async def optional_auth(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> Optional[dict]:
    """Optional authentication — returns user dict or None."""
    api_key = None
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        return None

    return _db.get_user_by_api_key(api_key)


# ---------------------------------------------------------------------------
# Admin Authentication
# ---------------------------------------------------------------------------

# Admin secret for privileged operations (MUST be set via ADMIN_SECRET env var)
ADMIN_SECRET = os.getenv("ADMIN_SECRET")
if not ADMIN_SECRET:
    logger.warning("ADMIN_SECRET not set — admin endpoints will be disabled")


def verify_admin_secret(x_admin_secret: Optional[str], request: Request) -> None:
    """Verify the admin secret header with rate limiting and constant-time comparison.

    Raises :class:`APIError` on failure (503 if unconfigured, 429 if rate-limited,
    403 if the secret is invalid).
    """
    if not ADMIN_SECRET:
        raise APIError(
            status_code=503,
            message="Admin endpoints disabled — ADMIN_SECRET not configured",
            code=ErrorCode.SERVICE_UNAVAILABLE,
        )

    # Rate limit admin endpoints to mitigate brute-force attacks
    client_ip = request.client.host if request.client else "unknown"
    allowed, info = rate_limiter.check(f"admin:{client_ip}", max_requests=10, window_seconds=60)
    if not allowed:
        raise APIError(
            status_code=429,
            message=f"Admin rate limit exceeded. {info['detail']}",
            code=ErrorCode.RATE_LIMITED,
        )

    # Use constant-time comparison to prevent timing attacks (see #55)
    if not secrets.compare_digest(x_admin_secret or "", ADMIN_SECRET):
        raise APIError(status_code=403, message="Invalid admin secret", code=ErrorCode.FORBIDDEN)
