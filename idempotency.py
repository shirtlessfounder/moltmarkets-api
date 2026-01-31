"""
Idempotency key support for MoltMarkets API.

Prevents double-spending from network retries by caching responses
for POST requests that include an X-Idempotency-Key header.

How it works:
  1. Client sends POST with `X-Idempotency-Key: <unique-key>` header
  2. If key is new → request executes normally, response is cached
  3. If key was seen before → cached response is returned (no re-execution)
  4. If a request with the same key is already in-flight → 409 Conflict

Storage: in-memory dict  {scoped_key: {status_code, body, headers, timestamp}}
TTL: 24 hours (configurable via IDEMPOTENCY_TTL_SECONDS env var)
Scoping: keys are scoped per-user via API key hash prefix, so different
         users can use the same key string without collision.

Thread-safe for single-process uvicorn (same as rate_limiter.py).
"""

import hashlib
import os
import time
from threading import Lock
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

# ── Configuration ────────────────────────────────────────────────────────────

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
IDEMPOTENCY_TTL_SECONDS = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "86400"))  # 24h
MAX_KEY_LENGTH = 256
_CLEANUP_INTERVAL = 100  # Run cleanup every N lookups
_IN_PROGRESS_TIMEOUT = 300  # 5 min — stale in-progress entries are cleaned up


# ── Idempotency Store ───────────────────────────────────────────────────────

class IdempotencyStore:
    """
    In-memory store for idempotency keys and their cached responses.

    Each entry is either:
      - in-progress: {"in_progress": True, "timestamp": ...}
      - completed:   {"status_code": ..., "body": ..., "headers": ..., "timestamp": ...}

    Entries expire after TTL seconds.  Stale in-progress entries (older than
    _IN_PROGRESS_TIMEOUT) are cleaned up to handle crashed requests.
    """

    def __init__(self, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS):
        self._store: dict[str, dict] = {}
        self._lock = Lock()
        self._ttl = ttl_seconds
        self._call_count = 0

    def get(self, key: str) -> Optional[dict]:
        """Look up a cached response.  Returns None if not found or expired."""
        with self._lock:
            self._call_count += 1
            if self._call_count % _CLEANUP_INTERVAL == 0:
                self._cleanup()

            entry = self._store.get(key)
            if entry is None:
                return None

            age = time.time() - entry["timestamp"]

            # Completed entries: expire after TTL
            if not entry.get("in_progress") and age > self._ttl:
                del self._store[key]
                return None

            # In-progress entries: expire after timeout (crashed request)
            if entry.get("in_progress") and age > _IN_PROGRESS_TIMEOUT:
                del self._store[key]
                return None

            return entry

    def set(self, key: str, status_code: int, body: bytes, headers: dict):
        """Cache a completed response for an idempotency key."""
        with self._lock:
            self._store[key] = {
                "status_code": status_code,
                "body": body,
                "headers": headers,
                "timestamp": time.time(),
            }

    def mark_in_progress(self, key: str) -> bool:
        """
        Atomically mark a key as in-progress.

        Returns True if the key was successfully marked (new request).
        Returns False if the key already exists (duplicate or in-progress).
        """
        with self._lock:
            existing = self._store.get(key)
            if existing is not None:
                age = time.time() - existing["timestamp"]
                # Stale in-progress entry — allow retry
                if existing.get("in_progress") and age > _IN_PROGRESS_TIMEOUT:
                    pass  # Fall through to overwrite
                else:
                    return False
            self._store[key] = {"in_progress": True, "timestamp": time.time()}
            return True

    def remove(self, key: str):
        """Remove a key (used when request fails with 5xx and shouldn't be cached)."""
        with self._lock:
            self._store.pop(key, None)

    def _cleanup(self):
        """Remove expired entries."""
        now = time.time()
        expired = [
            k for k, v in self._store.items()
            if (not v.get("in_progress") and now - v["timestamp"] > self._ttl)
            or (v.get("in_progress") and now - v["timestamp"] > _IN_PROGRESS_TIMEOUT)
        ]
        for k in expired:
            del self._store[k]

    @property
    def size(self) -> int:
        """Number of entries in the store (for health/monitoring)."""
        return len(self._store)


# ── Singleton ────────────────────────────────────────────────────────────────

idempotency_store = IdempotencyStore()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_user_scope(request: Request) -> str:
    """
    Extract a user-scoping prefix from the request's auth headers.

    Uses first 16 hex chars of the SHA-256 hash of the API key so that
    different users with the same idempotency key string don't collide,
    and we never store the raw key.
    """
    auth = request.headers.get("authorization", "")
    api_key = request.headers.get("x-api-key", "")

    if auth.startswith("Bearer "):
        token = auth[7:]
    elif api_key:
        token = api_key
    else:
        return "anon"

    return hashlib.sha256(token.encode()).hexdigest()[:16]


# ── Middleware ────────────────────────────────────────────────────────────────

class IdempotencyMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that intercepts POST requests carrying an
    X-Idempotency-Key header and enforces at-most-once execution.

    Only POST requests are affected.  GET, PUT, DELETE, etc. pass through
    unchanged.  The header is entirely optional — clients that don't send
    it get normal (non-idempotent) behaviour.

    Responses with status 5xx are NOT cached so the client can safely retry
    on transient server errors.
    """

    async def dispatch(self, request: Request, call_next):
        # Only intercept POST requests
        if request.method != "POST":
            return await call_next(request)

        # Check for idempotency key header
        raw_key = request.headers.get(IDEMPOTENCY_HEADER)
        if not raw_key:
            return await call_next(request)

        # ── Validate key ──
        if len(raw_key) > MAX_KEY_LENGTH:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        f"X-Idempotency-Key exceeds maximum length "
                        f"of {MAX_KEY_LENGTH} characters"
                    )
                },
            )

        # Scope the key to the authenticated user
        user_scope = _extract_user_scope(request)
        scoped_key = f"{user_scope}:{raw_key}"

        # ── Check cache for completed response ──
        cached = idempotency_store.get(scoped_key)
        if cached and not cached.get("in_progress"):
            return Response(
                content=cached["body"],
                status_code=cached["status_code"],
                headers={**cached["headers"], "X-Idempotency-Replayed": "true"},
            )

        # ── Try to mark as in-progress (atomic) ──
        if not idempotency_store.mark_in_progress(scoped_key):
            # Key exists — either completed (race) or still in-flight
            cached = idempotency_store.get(scoped_key)
            if cached and not cached.get("in_progress"):
                return Response(
                    content=cached["body"],
                    status_code=cached["status_code"],
                    headers={**cached["headers"], "X-Idempotency-Replayed": "true"},
                )
            # Still in-progress from a concurrent request → 409
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        "A request with this idempotency key is already "
                        "being processed. Please wait and retry."
                    )
                },
            )

        # ── Execute the actual request ──
        try:
            response = await call_next(request)
        except Exception:
            # Request crashed — remove in-progress marker so client can retry
            idempotency_store.remove(scoped_key)
            raise

        # ── Capture response body ──
        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        # ── Cache decision ──
        if response.status_code >= 500:
            # Don't cache server errors — allow retries
            idempotency_store.remove(scoped_key)
        else:
            # Cache successful and client-error responses
            response_headers = dict(response.headers)
            # Remove hop-by-hop / streaming headers that shouldn't be replayed
            response_headers.pop("transfer-encoding", None)
            idempotency_store.set(
                scoped_key,
                status_code=response.status_code,
                body=body,
                headers=response_headers,
            )

        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
        )
