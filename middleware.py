"""
MoltMarkets API — Middleware configuration.

Extracted from api.py as part of the Phase 3 middleware refactor (#66).
Contains CORS setup, rate-limit header helpers, and middleware wiring.
"""

import os

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from errors import APIError, ErrorCode
from idempotency import IdempotencyMiddleware


# ---------------------------------------------------------------------------
# Rate-Limit Header Helpers
# ---------------------------------------------------------------------------

def set_rate_limit_headers(response: Response, info: dict) -> None:
    """Inject standard rate-limit headers into a FastAPI Response.

    Headers follow the draft IETF RateLimit header spec and the widely-adopted
    X-RateLimit-* convention so agent HTTP clients can self-throttle.
    """
    response.headers["X-RateLimit-Limit"] = str(info.get("limit", ""))
    response.headers["X-RateLimit-Remaining"] = str(info.get("remaining", ""))
    response.headers["X-RateLimit-Reset"] = str(info.get("reset", ""))


def raise_rate_limited(detail: str, info: dict) -> None:
    """Raise an APIError with 429 status and Retry-After header guidance.

    The ``info`` dict comes from ``rate_limiter.check()`` and contains the
    ``retry_after`` value in seconds.
    """
    raise APIError(
        status_code=429,
        message=detail,
        code=ErrorCode.RATE_LIMITED,
        detail={"retry_after": info.get("retry_after", 60)},
        headers={
            "Retry-After": str(info.get("retry_after", 60)),
            "X-RateLimit-Limit": str(info.get("limit", "")),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(info.get("reset", "")),
        },
    )


# ---------------------------------------------------------------------------
# CORS Configuration
# ---------------------------------------------------------------------------

_DEFAULT_ORIGINS = [
    "https://moltmarkets.com",
    "http://localhost:3000",
]


def get_cors_config() -> dict:
    """Build CORS middleware kwargs from environment variables.

    Behaviour:
    - ``DEBUG=1``: allow all origins (wildcard), all methods, all headers.
    - ``CORS_ORIGINS`` env var set: use those origins (comma-separated).
    - Default: ``moltmarkets.com`` + ``localhost:3000``.
    """
    debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
    cors_origins_env = os.getenv("CORS_ORIGINS")

    if debug:
        allowed_origins = ["*"]
    elif cors_origins_env:
        allowed_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    else:
        allowed_origins = _DEFAULT_ORIGINS

    allowed_methods = ["*"] if debug else ["GET", "POST", "OPTIONS"]
    allowed_headers = (
        ["*"]
        if debug
        else ["Authorization", "Content-Type", "X-Idempotency-Key", "X-API-Key"]
    )

    return {
        "allow_origins": allowed_origins,
        "allow_credentials": not debug,  # credentials incompatible with wildcard origins
        "allow_methods": allowed_methods,
        "allow_headers": allowed_headers,
    }


# ---------------------------------------------------------------------------
# Middleware Wiring
# ---------------------------------------------------------------------------

def configure_middleware(app: FastAPI) -> None:
    """Attach CORS and idempotency middleware to the FastAPI application.

    Order matters: IdempotencyMiddleware is added **after** CORSMiddleware so
    CORS headers are applied even to cached/replayed responses.
    (Starlette middleware ordering: last added = outermost = runs first.)
    """
    cors_config = get_cors_config()
    app.add_middleware(CORSMiddleware, **cors_config)

    # Idempotency middleware — must be added AFTER CORSMiddleware so CORS
    # headers are applied even to cached/replayed responses.
    app.add_middleware(IdempotencyMiddleware)
