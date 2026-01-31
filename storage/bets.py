"""
MoltMarkets Storage — bet CRUD and leaderboard operations.
"""

import warnings
from datetime import datetime, timezone
from typing import Dict, List

from models import MarketStatus, Outcome
from storage.types import BetDict, BetWithUsernameDict, LeaderboardEntryDict, ReputationDataDict


class BetStorageMixin:
    """Mixin providing all bet-related storage methods and leaderboard."""

    # --- Bets ---

    def create_bet(self, bet_id: str, market_id: str, user_id: str,
                   outcome: Outcome, amount: float, shares: float,
                   prob_before: float, prob_after: float) -> BetDict:
        avg_price = amount / shares if shares > 0 else 0

        if self._use_memory:
            bet = BetDict(
                id=bet_id,
                market_id=market_id,
                user_id=user_id,
                outcome=outcome,
                amount=amount,
                shares=shares,
                avg_price=avg_price,
                probability_before=prob_before,
                probability_after=prob_after,
                created_at=datetime.now(timezone.utc),
            )
            self._bets[bet_id] = bet
            self._users[user_id]["total_bets"] += 1
            return bet

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bets (id, market_id, user_id, outcome, amount, shares, avg_price, probability_before, probability_after)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (bet_id, market_id, user_id, outcome.value, amount, shares, avg_price, prob_before, prob_after))
                row = cur.fetchone()

                # Increment user's total_bets
                cur.execute("UPDATE users SET total_bets = total_bets + 1 WHERE id = %s", (user_id,))
                conn.commit()
                return self._row_to_bet(row)
        finally:
            self._put_conn(conn)

    def get_bets_for_market(self, market_id: str) -> List[BetDict]:
        """Get all bets for a market."""
        if self._use_memory:
            return [b for b in self._bets.values() if b["market_id"] == market_id]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bets WHERE market_id = %s ORDER BY created_at", (market_id,))
                rows = cur.fetchall()
                return [self._row_to_bet(row) for row in rows]
        finally:
            self._put_conn(conn)

    def get_bets_for_market_with_users(self, market_id: str) -> List[BetWithUsernameDict]:
        """Get all bets for a market with user info in a single JOIN query.

        Eliminates N+1: previously get_bets_for_market + N × get_user calls.
        Now: 1 query total.
        """
        if self._use_memory:
            results: List[BetWithUsernameDict] = []
            for b in self._bets.values():
                if b["market_id"] == market_id:
                    bet = BetWithUsernameDict(**b)
                    user = self._users.get(b["user_id"])
                    bet["username"] = user["username"] if user else "unknown"
                    results.append(bet)
            return results

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT b.*, u.username
                    FROM bets b
                    LEFT JOIN users u ON b.user_id = u.id
                    WHERE b.market_id = %s
                    ORDER BY b.created_at
                """, (market_id,))
                rows = cur.fetchall()
                results: List[BetWithUsernameDict] = []
                for row in rows:
                    bet = BetWithUsernameDict(**self._row_to_bet(row))
                    bet["username"] = row.get("username") or "unknown"
                    results.append(bet)
                return results
        finally:
            self._put_conn(conn)

    def get_bets_for_user(self, user_id: str) -> List[BetDict]:
        """Get all bets for a user."""
        if self._use_memory:
            return [b for b in self._bets.values() if b["user_id"] == user_id]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bets WHERE user_id = %s ORDER BY created_at", (user_id,))
                rows = cur.fetchall()
                return [self._row_to_bet(row) for row in rows]
        finally:
            self._put_conn(conn)

    def get_bets_on_markets(self, market_ids: set) -> List[BetDict]:
        """Get all bets placed on specific markets.

        Used by reputation creation-score to count bets on a user's
        created markets without loading the entire bets table.

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if not market_ids:
            return []

        if self._use_memory:
            return [b for b in self._bets.values() if b.get("market_id") in market_ids]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM bets WHERE market_id = ANY(%s) ORDER BY created_at",
                    (list(market_ids),),
                )
                rows = cur.fetchall()
                return [self._row_to_bet(row) for row in rows]
        finally:
            self._put_conn(conn)

    @property
    def bets(self) -> Dict[str, BetDict]:
        """Get all bets as dict.

        .. deprecated::
            Loads the **entire** bets table into memory — O(N) on every call.
            Use targeted methods instead:
            - ``get_bets_for_market(market_id)`` for market bets
            - ``get_bets_for_user(user_id)`` for user bets
            - ``get_bets_on_markets(market_ids)`` for batch market bets

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        warnings.warn(
            "db.bets loads the entire bets table into memory. "
            "Use get_bets_for_market(), get_bets_for_user(), or "
            "get_bets_on_markets() instead. "
            "See #54: https://github.com/shirtlessfounder/moltmarkets-api/issues/54",
            DeprecationWarning,
            stacklevel=2,
        )
        if self._use_memory:
            return self._bets

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bets")
                rows = cur.fetchall()
                return {row["id"]: self._row_to_bet(row) for row in rows}
        finally:
            self._put_conn(conn)

    def get_leaderboard_data(self) -> List[LeaderboardEntryDict]:
        """Get leaderboard data using aggregate SQL query.

        Eliminates N+1: previously loaded ALL users + ALL bets + ALL markets
        into memory, then did O(users × bets) Python iteration.
        Now: 1 query with CTEs for volume and win rate aggregation.
        """
        if self._use_memory:
            # Fallback: in-memory calculation (same logic, just organized)
            entries: List[LeaderboardEntryDict] = []
            for user in self._users.values():
                if user.get("status") != "claimed":
                    continue
                total_volume = sum(
                    b["amount"] for b in self._bets.values()
                    if b["user_id"] == user["id"]
                )
                user_bets = [b for b in self._bets.values() if b["user_id"] == user["id"]]
                wins = 0
                resolved_bets = 0
                for bet in user_bets:
                    market = self._markets.get(bet["market_id"])
                    if market and market["status"] == MarketStatus.RESOLVED:
                        resolved_bets += 1
                        if market["resolution"] == bet["outcome"]:
                            wins += 1
                win_rate = wins / resolved_bets if resolved_bets > 0 else 0.5
                entries.append(LeaderboardEntryDict(
                    user_id=user["id"],
                    username=user["username"],
                    balance=user["balance"],
                    pnl=user["profit_all_time"],
                    total_volume=total_volume,
                    win_rate=win_rate,
                ))
            entries.sort(key=lambda x: x["pnl"], reverse=True)
            return entries

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH user_volumes AS (
                        SELECT user_id, COALESCE(SUM(amount), 0) AS total_volume
                        FROM bets
                        GROUP BY user_id
                    ),
                    user_win_rates AS (
                        SELECT
                            b.user_id,
                            COUNT(*) AS resolved_bets,
                            SUM(CASE WHEN UPPER(m.resolution) = UPPER(b.outcome) THEN 1 ELSE 0 END) AS wins
                        FROM bets b
                        JOIN markets m ON b.market_id = m.id
                        WHERE UPPER(m.status) = 'RESOLVED'
                        GROUP BY b.user_id
                    )
                    SELECT
                        u.id AS user_id,
                        u.username,
                        u.balance,
                        u.profit_all_time AS pnl,
                        COALESCE(v.total_volume, 0) AS total_volume,
                        CASE
                            WHEN COALESCE(w.resolved_bets, 0) > 0
                            THEN w.wins::float / w.resolved_bets
                            ELSE 0.5
                        END AS win_rate
                    FROM users u
                    LEFT JOIN user_volumes v ON u.id = v.user_id
                    LEFT JOIN user_win_rates w ON u.id = w.user_id
                    WHERE u.status = 'claimed'
                    ORDER BY u.profit_all_time DESC
                """)
                rows = cur.fetchall()
                return [
                    LeaderboardEntryDict(
                        user_id=row["user_id"],
                        username=row["username"],
                        balance=float(row["balance"]),
                        pnl=float(row["pnl"]),
                        total_volume=float(row["total_volume"]),
                        win_rate=float(row["win_rate"]),
                    )
                    for row in rows
                ]
        finally:
            self._put_conn(conn)

    def get_reputation_data(self, user_id: str) -> ReputationDataDict:
        """Get all data needed for reputation calculation in minimal queries.

        Eliminates N+1: previously looped through ALL markets to fetch
        resolution votes and comments individually (2N extra queries).
        Now: 3 targeted queries instead of 2N+3.
        """
        import json

        if self._use_memory:
            # Fallback for in-memory storage
            all_resolution_votes = []
            if hasattr(self, '_resolution_votes'):
                for votes in self._resolution_votes.values():
                    all_resolution_votes.extend(votes)
            comments_count = 0
            if hasattr(self, '_comments'):
                comments_count = sum(1 for c in self._comments.values() if c.get("user_id") == user_id)
            return ReputationDataDict(
                resolution_votes=all_resolution_votes,
                comments_count=comments_count,
            )

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Single query for all resolution votes (not per-market)
                cur.execute("""
                    SELECT * FROM resolution_votes
                    ORDER BY created_at ASC
                """)
                rows = cur.fetchall()
                all_resolution_votes = [
                    {
                        **dict(row),
                        "sources": json.loads(row["sources"]) if row["sources"] else []
                    }
                    for row in rows
                ]

                # Single query for user's comment count
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM comments WHERE user_id = %s
                """, (user_id,))
                comments_count = cur.fetchone()["cnt"]

                return ReputationDataDict(
                    resolution_votes=all_resolution_votes,
                    comments_count=comments_count,
                )
        finally:
            self._put_conn(conn)
