"""
In-memory cache for the /markets endpoint.

Provides a simple TTL-based cache with invalidation on mutations,
ETag generation, and Last-Modified tracking.

Design decisions:
- Dict + timestamp (not lru_cache) so we can invalidate on mutations
- Short TTL (5s default) — markets change with every bet, stale data
  is worse than slow data for a trading platform
- ETag = md5 of serialized response — allows HTTP 304 Not Modified
- Thread-safe via the GIL for dict reads/writes (sufficient for our
  concurrency model; FastAPI runs in a single process with async)

See issue #45 for context.
"""

import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Default cache TTL in seconds. Short because markets are live-trading
# instruments where probability/volume change with every bet.
DEFAULT_TTL_SECONDS = 5


class MarketListCache:
    """In-memory cache for the market list endpoint.

    Stores the serialized market summaries along with metadata for
    HTTP caching headers (ETag, Last-Modified).

    Usage:
        cache = MarketListCache(ttl_seconds=5)

        # On read:
        hit = cache.get("open")
        if hit:
            return hit["data"]  # cached response

        # On miss: build response, then store
        data = build_market_list(...)
        cache.set("open", data)

        # On mutation (bet, create, resolve):
        cache.invalidate()
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS):
        self._ttl = ttl_seconds
        # Keyed by status filter string (e.g., "open", "all", "resolved")
        self._entries: Dict[str, dict] = {}
        # Global last-modified timestamp — updated on any mutation
        self._last_modified: datetime = datetime.now(timezone.utc)
        # Monotonic counter incremented on every invalidation.
        # Used as a cheap ETag component so we don't have to hash the data.
        self._generation: int = 0

    @property
    def last_modified(self) -> datetime:
        return self._last_modified

    @property
    def generation(self) -> int:
        return self._generation

    def get(self, status_filter: str) -> Optional[dict]:
        """Return cached entry if it exists and hasn't expired.

        Returns dict with keys: data (list), etag (str), stored_at (float)
        or None on miss/expiry.
        """
        entry = self._entries.get(status_filter)
        if entry is None:
            return None

        age = time.monotonic() - entry["stored_at"]
        if age > self._ttl:
            # Expired — remove and return miss
            del self._entries[status_filter]
            return None

        return entry

    def set(self, status_filter: str, data: Any) -> dict:
        """Store a cache entry and compute its ETag.

        Accepts either a plain list or a paginated response object (with a
        ``.data`` attribute).  The full object is stored so cache hits can
        return it directly; the ETag is computed from the inner list length.

        Returns the entry dict (with etag) so callers can use it
        immediately for response headers.
        """
        etag = self._compute_etag(data, status_filter)
        entry = {
            "data": data,
            "etag": etag,
            "stored_at": time.monotonic(),
        }
        self._entries[status_filter] = entry
        return entry

    def invalidate(self) -> None:
        """Clear all cached entries. Called on any market mutation.

        Also bumps the generation counter and last-modified timestamp
        so subsequent responses get fresh ETags and Last-Modified headers.
        """
        self._entries.clear()
        self._generation += 1
        self._last_modified = datetime.now(timezone.utc)

    def _compute_etag(self, data: Any, status_filter: str) -> str:
        """Compute a weak ETag for the response data.

        Uses generation + status_filter + data length as a fast fingerprint.
        We don't hash the full payload (expensive for large lists) — the
        generation counter already guarantees uniqueness after invalidation.

        Handles both plain lists and paginated response objects (with a
        ``.data`` attribute containing the inner list).
        """
        # Support paginated wrapper objects (PaginatedMarketSummary, etc.)
        inner = data.data if hasattr(data, "data") else data
        fingerprint = f"{self._generation}:{status_filter}:{len(inner)}"
        digest = hashlib.md5(fingerprint.encode()).hexdigest()[:16]
        return f'W/"{digest}"'


# Singleton cache instance — imported by api.py
market_cache = MarketListCache(ttl_seconds=DEFAULT_TTL_SECONDS)
