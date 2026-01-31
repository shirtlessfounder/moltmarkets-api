"""
MoltMarkets Storage — market CRUD operations.
"""

import warnings
from datetime import datetime, timezone
from typing import Dict, List, Optional

from models import MarketStatus, Outcome
from storage.types import MarketDict, MarketWithCreatorDict


class MarketStorageMixin:
    """Mixin providing all market-related storage methods."""

    # --- Markets ---

    def get_market(self, market_id: str) -> Optional[MarketDict]:
        if self._use_memory:
            return self._markets.get(market_id)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM markets WHERE id = %s", (market_id,))
                row = cur.fetchone()
                return self._row_to_market(row)
        finally:
            self._put_conn(conn)

    def list_markets(self) -> List[MarketDict]:
        if self._use_memory:
            return list(self._markets.values())

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM markets ORDER BY created_at DESC")
                rows = cur.fetchall()
                return [self._row_to_market(row) for row in rows]
        finally:
            self._put_conn(conn)

    def list_markets_with_creators(self) -> List[MarketWithCreatorDict]:
        """List all markets with creator usernames in a single JOIN query.

        Eliminates N+1: previously list_markets + N × get_user calls.
        Now: 1 query total.
        """
        if self._use_memory:
            results: List[MarketWithCreatorDict] = []
            for m in self._markets.values():
                market = MarketWithCreatorDict(**m)
                creator = self._users.get(m["creator_id"])
                market["creator_username"] = creator["username"] if creator else None
                results.append(market)
            return results

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.*, u.username AS creator_username
                    FROM markets m
                    LEFT JOIN users u ON m.creator_id = u.id
                    ORDER BY m.created_at DESC
                """)
                rows = cur.fetchall()
                results: List[MarketWithCreatorDict] = []
                for row in rows:
                    market = MarketWithCreatorDict(**self._row_to_market(row))
                    market["creator_username"] = row.get("creator_username")
                    results.append(market)
                return results
        finally:
            self._put_conn(conn)

    def count_markets(self) -> int:
        """Count total markets without loading all rows.

        O(1) via COUNT(*) instead of O(N) full-table load.
        Replaces ``len(db.markets)`` / ``len(db.list_markets())`` in hot paths.

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if self._use_memory:
            return len(self._markets)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM markets")
                return cur.fetchone()["cnt"]
        finally:
            self._put_conn(conn)

    def get_markets_by_ids(self, market_ids: set) -> Dict[str, MarketDict]:
        """Get specific markets by their IDs in a single query.

        Returns a dict of {market_id: market_dict} for only the requested IDs.
        O(K) where K = len(market_ids), instead of O(N) for the full table.

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if not market_ids:
            return {}

        if self._use_memory:
            return {mid: self._markets[mid] for mid in market_ids if mid in self._markets}

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM markets WHERE id = ANY(%s)",
                    (list(market_ids),),
                )
                rows = cur.fetchall()
                return {row["id"]: self._row_to_market(row) for row in rows}
        finally:
            self._put_conn(conn)

    def get_markets_by_creator(self, creator_id: str) -> List[MarketDict]:
        """Get all markets created by a specific user.

        Uses idx_markets_creator index for O(1) lookup.

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if self._use_memory:
            return [m for m in self._markets.values() if m.get("creator_id") == creator_id]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM markets WHERE creator_id = %s ORDER BY created_at DESC",
                    (creator_id,),
                )
                rows = cur.fetchall()
                return [self._row_to_market(row) for row in rows]
        finally:
            self._put_conn(conn)

    @property
    def markets(self) -> Dict[str, MarketDict]:
        """Get all markets as dict.

        .. deprecated::
            Loads the **entire** markets table into memory — O(N) on every call.
            Use targeted methods instead:
            - ``count_markets()`` for counts
            - ``get_market(id)`` for single lookups
            - ``list_markets()`` for ordered listing
            - ``get_markets_by_ids(ids)`` for batch lookups
            - ``get_markets_by_creator(uid)`` for creator filtering

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        warnings.warn(
            "db.markets loads the entire markets table into memory. "
            "Use count_markets(), get_market(), get_markets_by_ids(), or "
            "get_markets_by_creator() instead. "
            "See #54: https://github.com/shirtlessfounder/moltmarkets-api/issues/54",
            DeprecationWarning,
            stacklevel=2,
        )
        if self._use_memory:
            return self._markets

        markets_list = self.list_markets()
        return {m["id"]: m for m in markets_list}

    def create_market(self, market_id: str, creator_id: str, title: str,
                      description: str, closes_at: datetime,
                      initial_liquidity: float) -> MarketDict:
        if self._use_memory:
            pool = {"YES": initial_liquidity, "NO": initial_liquidity}
            market = MarketDict(
                id=market_id,
                title=title,
                description=description,
                status=MarketStatus.OPEN,
                closes_at=closes_at,
                created_at=datetime.now(timezone.utc),
                resolved_at=None,
                resolution=None,
                total_volume=0.0,
                creator_id=creator_id,
                pool=pool,
                p=0.5,
                version=1,
                committee=None,
                resolution_deadline=None,
            )
            self._markets[market_id] = market
            self._positions[market_id] = {}
            self._users[creator_id]["markets_created"] += 1
            return market

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO markets (id, title, description, closes_at, creator_id, pool_yes, pool_no, p)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (market_id, title, description, closes_at, creator_id, initial_liquidity, initial_liquidity, 0.5))
                row = cur.fetchone()

                # Increment user's markets_created
                cur.execute("UPDATE users SET markets_created = markets_created + 1 WHERE id = %s", (creator_id,))
                conn.commit()
                return self._row_to_market(row)
        finally:
            self._put_conn(conn)

    def update_market_pool(self, market_id: str, new_pool: dict, new_p: float, volume_delta: float) -> None:
        if self._use_memory:
            market = self._markets[market_id]
            market["pool"] = new_pool
            market["p"] = new_p
            market["total_volume"] += volume_delta
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE markets
                    SET pool_yes = %s, pool_no = %s, p = %s, total_volume = total_volume + %s
                    WHERE id = %s
                """, (new_pool["YES"], new_pool["NO"], new_p, volume_delta, market_id))
                conn.commit()
        finally:
            self._put_conn(conn)

    def update_market_pool_versioned(
        self,
        market_id: str,
        new_pool: dict,
        new_p: float,
        volume_delta: float,
        expected_version: int,
    ) -> Optional[int]:
        """Compare-and-swap pool update with optimistic locking.

        Atomically sets new pool values **only** when the current row
        version matches ``expected_version``.  On success the version is
        incremented and the new version number is returned.  On conflict
        (another writer incremented the version first) returns ``None``
        so the caller can retry with fresh state.
        """
        if self._use_memory:
            market = self._markets.get(market_id)
            if not market or market.get("version", 1) != expected_version:
                return None
            market["pool"] = new_pool
            market["p"] = new_p
            market["total_volume"] += volume_delta
            market["version"] = expected_version + 1
            return expected_version + 1

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE markets
                    SET pool_yes = %s,
                        pool_no  = %s,
                        p        = %s,
                        total_volume = total_volume + %s,
                        version  = version + 1
                    WHERE id = %s AND version = %s
                    RETURNING version
                """, (
                    new_pool["YES"], new_pool["NO"], new_p,
                    volume_delta, market_id, expected_version,
                ))
                row = cur.fetchone()
                conn.commit()
                return int(row["version"]) if row else None
        finally:
            self._put_conn(conn)

    def resolve_market_versioned(
        self,
        market_id: str,
        outcome: Outcome,
        expected_version: int,
    ) -> Optional[int]:
        """Resolve a market with optimistic locking.

        Returns the new version on success, ``None`` on version conflict.
        """
        now = datetime.now(timezone.utc)

        if self._use_memory:
            market = self._markets.get(market_id)
            if not market or market.get("version", 1) != expected_version:
                return None
            market["status"] = MarketStatus.RESOLVED
            market["resolution"] = outcome
            market["resolved_at"] = now
            market["version"] = expected_version + 1
            return expected_version + 1

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE markets
                    SET status      = %s,
                        resolution  = %s,
                        resolved_at = %s,
                        version     = version + 1
                    WHERE id = %s AND version = %s
                    RETURNING version
                """, (
                    MarketStatus.RESOLVED.value, outcome.value, now,
                    market_id, expected_version,
                ))
                row = cur.fetchone()
                conn.commit()
                return int(row["version"]) if row else None
        finally:
            self._put_conn(conn)

    def update_market_status(self, market_id: str, status: MarketStatus) -> None:
        """Update a market's status (e.g. OPEN → RESOLVING)."""
        if self._use_memory:
            market = self._markets[market_id]
            market["status"] = status
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE markets SET status = %s WHERE id = %s",
                    (status.value, market_id),
                )
                conn.commit()
        finally:
            self._put_conn(conn)

    def transition_expired_markets(self) -> int:
        """Batch-transition all OPEN markets past closes_at → RESOLVING.

        Returns the number of rows updated.  Uses a single UPDATE so the
        cost is O(1) DB round-trips regardless of how many markets expire.
        """
        now = datetime.now(timezone.utc)
        if self._use_memory:
            count = 0
            for m in self._markets.values():
                if m["status"] == MarketStatus.OPEN and m["closes_at"] <= now:
                    m["status"] = MarketStatus.RESOLVING
                    count += 1
            return count

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE markets SET status = %s "
                    "WHERE status = %s AND closes_at <= %s",
                    (MarketStatus.RESOLVING.value, MarketStatus.OPEN.value, now),
                )
                conn.commit()
                return cur.rowcount
        finally:
            self._put_conn(conn)

    def resolve_market(self, market_id: str, outcome: Outcome) -> None:
        if self._use_memory:
            market = self._markets[market_id]
            market["status"] = MarketStatus.RESOLVED
            market["resolution"] = outcome
            market["resolved_at"] = datetime.now(timezone.utc)
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE markets
                    SET status = %s, resolution = %s, resolved_at = %s
                    WHERE id = %s
                """, (MarketStatus.RESOLVED.value, outcome.value, datetime.now(timezone.utc), market_id))
                conn.commit()
        finally:
            self._put_conn(conn)
