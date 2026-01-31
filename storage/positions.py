"""
MoltMarkets Storage — position operations.
"""

from typing import List, Optional

from models import Outcome


class PositionStorageMixin:
    """Mixin providing all position-related storage methods."""

    # --- Positions ---

    def get_position(self, market_id: str, user_id: str) -> Optional[dict]:
        if self._use_memory:
            return self._positions.get(market_id, {}).get(user_id)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM positions WHERE market_id = %s AND user_id = %s", (market_id, user_id))
                row = cur.fetchone()
                return self._row_to_position(row)
        finally:
            self._put_conn(conn)

    def get_market_positions(self, market_id: str) -> List[dict]:
        if self._use_memory:
            return list(self._positions.get(market_id, {}).values())

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM positions WHERE market_id = %s", (market_id,))
                rows = cur.fetchall()
                return [self._row_to_position(row) for row in rows]
        finally:
            self._put_conn(conn)

    def update_position(self, market_id: str, user_id: str,
                        outcome: Outcome, shares_delta: float, invested_delta: float):
        if self._use_memory:
            if market_id not in self._positions:
                self._positions[market_id] = {}

            if user_id not in self._positions[market_id]:
                self._positions[market_id][user_id] = {
                    "user_id": user_id,
                    "market_id": market_id,
                    "yes_shares": 0.0,
                    "no_shares": 0.0,
                    "total_invested": 0.0,
                }

            pos = self._positions[market_id][user_id]
            if outcome == Outcome.YES:
                pos["yes_shares"] += shares_delta
            else:
                pos["no_shares"] += shares_delta
            pos["total_invested"] += invested_delta
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Upsert position
                if outcome == Outcome.YES:
                    cur.execute("""
                        INSERT INTO positions (market_id, user_id, yes_shares, no_shares, total_invested)
                        VALUES (%s, %s, %s, 0, %s)
                        ON CONFLICT (market_id, user_id) DO UPDATE
                        SET yes_shares = positions.yes_shares + %s, total_invested = positions.total_invested + %s
                    """, (market_id, user_id, shares_delta, invested_delta, shares_delta, invested_delta))
                else:
                    cur.execute("""
                        INSERT INTO positions (market_id, user_id, yes_shares, no_shares, total_invested)
                        VALUES (%s, %s, 0, %s, %s)
                        ON CONFLICT (market_id, user_id) DO UPDATE
                        SET no_shares = positions.no_shares + %s, total_invested = positions.total_invested + %s
                    """, (market_id, user_id, shares_delta, invested_delta, shares_delta, invested_delta))
                conn.commit()
        finally:
            self._put_conn(conn)

    def get_user_positions(self, user_id: str) -> List[dict]:
        """Get all positions for a user across all markets."""
        if self._use_memory:
            positions = []
            for market_id, market_positions in self._positions.items():
                if user_id in market_positions:
                    positions.append(market_positions[user_id])
            return positions

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM positions WHERE user_id = %s",
                    (user_id,),
                )
                rows = cur.fetchall()
                return [self._row_to_position(row) for row in rows]
        finally:
            self._put_conn(conn)

    def reduce_position(self, market_id: str, user_id: str,
                        outcome: Outcome, shares: float):
        """Reduce shares in a position (for selling)."""
        if self._use_memory:
            if market_id in self._positions and user_id in self._positions[market_id]:
                pos = self._positions[market_id][user_id]
                if outcome == Outcome.YES:
                    pos["yes_shares"] = max(0, pos["yes_shares"] - shares)
                else:
                    pos["no_shares"] = max(0, pos["no_shares"] - shares)
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if outcome == Outcome.YES:
                    cur.execute("""
                        UPDATE positions
                        SET yes_shares = GREATEST(0, yes_shares - %s)
                        WHERE market_id = %s AND user_id = %s
                    """, (shares, market_id, user_id))
                else:
                    cur.execute("""
                        UPDATE positions
                        SET no_shares = GREATEST(0, no_shares - %s)
                        WHERE market_id = %s AND user_id = %s
                    """, (shares, market_id, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)
