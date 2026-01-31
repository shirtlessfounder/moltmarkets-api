"""
In-memory cache for market history and bets endpoints.

Unlike the market list cache, this is keyed by market_id since each
market has its own history. Invalidation happens per-market when a
new bet is placed.

Cache is safe because history is append-only — past bets never change.
We invalidate on new bets to ensure fresh data.
"""

import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Longer TTL than market list — history only changes on new bets,
# and we explicitly invalidate when that happens.
DEFAULT_TTL_SECONDS = 60


class HistoryCache:
    """Per-market cache for history and bets endpoints.
    
    Usage:
        cache = HistoryCache()
        
        # On read:
        hit = cache.get("history", market_id)
        if hit:
            return hit  # cached response
        
        # On miss: build response, then store
        data = build_history(...)
        cache.set("history", market_id, data)
        
        # On bet placed:
        cache.invalidate(market_id)
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS):
        self._ttl = ttl_seconds
        # Nested dict: endpoint -> market_id -> {data, stored_at}
        self._entries: Dict[str, Dict[str, dict]] = {
            "history": {},
            "bets": {},
        }
        self._hits = 0
        self._misses = 0

    def get(self, endpoint: str, market_id: str) -> Optional[Any]:
        """Return cached data if it exists and hasn't expired."""
        endpoint_cache = self._entries.get(endpoint, {})
        entry = endpoint_cache.get(market_id)
        
        if entry is None:
            self._misses += 1
            return None
        
        age = time.monotonic() - entry["stored_at"]
        if age > self._ttl:
            # Expired — remove and return miss
            del endpoint_cache[market_id]
            self._misses += 1
            return None
        
        self._hits += 1
        return entry["data"]

    def set(self, endpoint: str, market_id: str, data: Any) -> None:
        """Store a cache entry."""
        if endpoint not in self._entries:
            self._entries[endpoint] = {}
        
        self._entries[endpoint][market_id] = {
            "data": data,
            "stored_at": time.monotonic(),
        }

    def invalidate(self, market_id: str) -> None:
        """Clear cached entries for a specific market.
        
        Called when a bet is placed on this market.
        """
        for endpoint_cache in self._entries.values():
            if market_id in endpoint_cache:
                del endpoint_cache[market_id]
        logger.debug("Invalidated history cache for market %s", market_id)

    def invalidate_all(self) -> None:
        """Clear all cached entries (e.g., on server restart)."""
        for endpoint_cache in self._entries.values():
            endpoint_cache.clear()

    def stats(self) -> dict:
        """Return cache statistics."""
        total_entries = sum(len(ec) for ec in self._entries.values())
        total_requests = self._hits + self._misses
        hit_rate = self._hits / total_requests if total_requests > 0 else 0
        return {
            "entries": total_entries,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.1%}",
        }


# Singleton instance
history_cache = HistoryCache(ttl_seconds=DEFAULT_TTL_SECONDS)
