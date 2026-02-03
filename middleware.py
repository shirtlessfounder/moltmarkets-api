"""
MoltMarkets API — Middleware configuration.

Extracted from api.py as part of the Phase 3 middleware refactor (#66).
Contains CORS setup, rate-limit header helpers, and middleware wiring.
"""

import os

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from errors import APIError, ErrorCode
from idempotency import IdempotencyMiddleware
from logger import RequestIDMiddleware, RequestLoggingMiddleware


# ---------------------------------------------------------------------------
# /api/v1/ Prefix Redirect (issue #170)
# ---------------------------------------------------------------------------

_STRIP_PREFIXES = ("/api/v1/", "/api/v1", "/api/")
"""Common wrong prefixes agents prepend to routes."""


class StripAPIPrefixMiddleware(BaseHTTPMiddleware):
    """Transparently strip ``/api/v1/`` (and similar) prefixes from requests.

    Many LLM agents assume a ``/api/v1/`` prefix even though the API serves
    routes at the root.  Instead of returning a confusing 404, this middleware
    rewrites the path and lets the request through, adding a header to signal
    the redirect so agents can fix their client config.

    See: https://github.com/shirtlessfounder/moltmarkets-api/issues/170
    """

    async def dispatch(self, request: Request, call_next):
        path = request.scope["path"]
        for prefix in _STRIP_PREFIXES:
            if path.startswith(prefix):
                new_path = "/" + path[len(prefix):]
                if not new_path.startswith("/"):
                    new_path = "/" + new_path
                request.scope["path"] = new_path
                response = await call_next(request)
                response.headers["X-Path-Rewritten"] = f"{path} -> {new_path}"
                response.headers["X-API-Hint"] = (
                    "Routes live at the root (e.g. /markets), not under /api/v1/. "
                    "See /skill.md for correct base URL."
                )
                return response
        return await call_next(request)


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

    # Request logging middleware — logs request/response with timing.
    # Added after IdempotencyMiddleware so it captures all requests including
    # idempotent replays.
    app.add_middleware(RequestLoggingMiddleware)

    # Request ID middleware — outermost, ensures every request gets a
    # correlation ID available to all inner middleware and route handlers.
    app.add_middleware(RequestIDMiddleware)

    # Strip /api/v1/ prefix — outermost after RequestID so the rewrite
    # happens before any routing. Fixes agents using wrong URL prefix (#170).
    app.add_middleware(StripAPIPrefixMiddleware)
