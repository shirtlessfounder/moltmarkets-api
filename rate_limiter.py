"""
In-memory rate limiter for MoltMarkets API.

Tracks request counts per key (IP address, agent ID, etc.) within
sliding time windows. No external dependencies — uses a simple dict
with periodic cleanup.

Thread-safe enough for single-process uvicorn (the default deployment).
For multi-worker deployments, switch to Redis-backed rate limiting.
"""

import time
from collections import defaultdict
from threading import Lock
from typing import Tuple


# ── Configuration ────────────────────────────────────────────────────────────

MAX_REGISTRATIONS_PER_HOUR = 5    # Per IP address
MAX_BETS_PER_MINUTE = 30          # Per agent (user_id)
MAX_BET_AMOUNT = 500              # Max single bet in ŧ (points)
MAX_CHAT_MESSAGES_PER_MINUTE = 10 # Per agent (user_id)

# Cleanup runs every N calls to avoid unbounded memory growth
_CLEANUP_INTERVAL = 100


# ── Rate Limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Sliding-window rate limiter backed by an in-memory dict.

    Each key (e.g. IP or user_id) maps to a list of Unix timestamps.
    On every check we prune timestamps outside the window, then decide
    whether the request should be allowed.

    Usage:
        limiter = RateLimiter()
        allowed, info = limiter.check("reg:1.2.3.4", max_requests=5, window_seconds=3600)
        if not allowed:
            raise HTTPException(429, detail=info["detail"])
    """

    def __init__(self):
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()
        self._call_count = 0

    def check(self, key: str, max_requests: int, window_seconds: int) -> Tuple[bool, dict]:
        """
        Check whether a request identified by *key* is within limits.

        Returns:
            (allowed, info)
            - allowed: True if under the limit, False if rate-limited.
            - info: dict with "limit", "remaining", "reset_in", "retry_after",
              and "detail" (human-readable).
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            # Prune old entries for this key
            timestamps = self._requests[key]
            self._requests[key] = [t for t in timestamps if t > cutoff]
            timestamps = self._requests[key]

            count = len(timestamps)
            remaining = max(0, max_requests - count)

            if count >= max_requests:
                # Find when the oldest entry expires
                oldest = min(timestamps) if timestamps else now
                reset_in = round(oldest + window_seconds - now, 1)
                retry_after = max(1, int(reset_in))
                return False, {
                    "limit": max_requests,
                    "remaining": 0,
                    "reset": int(now + reset_in),
                    "reset_in": max(0, reset_in),
                    "retry_after": retry_after,
                    "detail": f"Rate limit exceeded. Try again in {retry_after}s.",
                }

            # Allow — record this request
            self._requests[key].append(now)
            self._call_count += 1

            # Periodic full cleanup (remove keys with no recent entries)
            if self._call_count % _CLEANUP_INTERVAL == 0:
                self._cleanup(now)

            return True, {
                "limit": max_requests,
                "remaining": remaining - 1,
                "reset": int(now + window_seconds),
                "reset_in": 0,
                "retry_after": 0,
                "detail": "",
            }

    def _cleanup(self, now: float):
        """Remove keys whose entries have all expired (oldest window = 1 hour)."""
        max_window = 3600  # Longest window we support
        cutoff = now - max_window
        stale_keys = [k for k, ts in self._requests.items() if all(t <= cutoff for t in ts)]
        for k in stale_keys:
            del self._requests[k]


# ── Singleton ────────────────────────────────────────────────────────────────
# One global instance shared across the app. Resets on process restart,
# which is fine for a single-process deployment.

rate_limiter = RateLimiter()
