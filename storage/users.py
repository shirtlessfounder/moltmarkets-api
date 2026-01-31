"""
MoltMarkets Storage — user CRUD operations.
"""

import warnings
from datetime import datetime, timezone
from typing import Dict, Optional

from storage._hash import hash_api_key
from storage.types import UserDict


class UserStorageMixin:
    """Mixin providing all user-related storage methods."""

    # --- Users ---

    def get_user(self, user_id: str) -> Optional[UserDict]:
        if self._use_memory:
            return self._users.get(user_id)
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                return self._row_to_user(row)
        finally:
            self._put_conn(conn)

    def create_user(self, user_id: str, username: str, balance: float = 1000.0,
                    api_key_hash: Optional[str] = None, description: str = "",
                    status: str = "pending", verification_code: Optional[str] = None,
                    user_type: str = "agent", is_sandbox: bool = False) -> UserDict:
        if self._use_memory:
            user = UserDict(
                id=user_id,
                username=username,
                display_name=username,
                description=description,
                balance=balance,
                created_at=datetime.now(timezone.utc),
                markets_created=0,
                total_bets=0,
                profit_all_time=0.0,
                api_key_hash=api_key_hash,
                status=status,
                verification_code=verification_code,
                last_market_created_at=None,
                twitter_handle=None,
                user_type=user_type,
                is_sandbox=is_sandbox,
            )
            self._users[user_id] = user
            return user

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, username, display_name, description, balance, api_key_hash, status, verification_code, user_type, is_sandbox)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (user_id, username, username, description, balance, api_key_hash, status, verification_code, user_type, is_sandbox))
                row = cur.fetchone()
                conn.commit()
                return self._row_to_user(row)
        finally:
            self._put_conn(conn)

    def get_user_by_api_key(self, api_key: str) -> Optional[UserDict]:
        """Find user by API key."""
        key_hash = hash_api_key(api_key)
        if self._use_memory:
            for user in self._users.values():
                if user.get("api_key_hash") == key_hash:
                    return user
            return None

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE api_key_hash = %s", (key_hash,))
                row = cur.fetchone()
                return self._row_to_user(row)
        finally:
            self._put_conn(conn)

    def get_user_by_username(self, username: str) -> Optional[UserDict]:
        """Find user by username (case-insensitive)."""
        username_lower = username.lower()
        if self._use_memory:
            for user in self._users.values():
                if user.get("username", "").lower() == username_lower:
                    return user
            return None

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(%s)", (username_lower,))
                row = cur.fetchone()
                return self._row_to_user(row)
        finally:
            self._put_conn(conn)

    def update_api_key(self, user_id: str, new_key_hash: str) -> None:
        """Update user's API key hash."""
        if self._use_memory:
            self._users[user_id]["api_key_hash"] = new_key_hash
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET api_key_hash = %s WHERE id = %s", (new_key_hash, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)

    def update_user_balance(self, user_id: str, delta: float) -> float:
        if self._use_memory:
            self._users[user_id]["balance"] += delta
            return self._users[user_id]["balance"]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users SET balance = balance + %s WHERE id = %s
                    RETURNING balance
                """, (delta, user_id))
                row = cur.fetchone()
                conn.commit()
                return float(row["balance"])
        finally:
            self._put_conn(conn)

    def update_user_display_name(self, user_id: str, display_name: str) -> None:
        """Update user's display name."""
        if self._use_memory:
            self._users[user_id]["display_name"] = display_name
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET display_name = %s WHERE id = %s", (display_name, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)

    def delete_user(self, user_id: str) -> None:
        """Delete a user and all their data (admin only)."""
        if self._use_memory:
            if user_id in self._users:
                del self._users[user_id]
            # Also clean up positions and bets
            self._positions = {k: v for k, v in self._positions.items() if v.get("user_id") != user_id}
            self._bets = [b for b in self._bets if b.get("user_id") != user_id]
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Delete in order due to foreign keys
                cur.execute("DELETE FROM bets WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM positions WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM comments WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                conn.commit()
        finally:
            self._put_conn(conn)

    def update_user_api_key(self, user_id: str, key_hash: str) -> None:
        """Update a user's API key hash (for key regeneration)."""
        if self._use_memory:
            if user_id in self._users:
                self._users[user_id]["api_key_hash"] = key_hash
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET api_key_hash = %s WHERE id = %s", (key_hash, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)

    def increment_user_markets_created(self, user_id: str) -> None:
        """Increment user's markets_created counter."""
        if self._use_memory:
            self._users[user_id]["markets_created"] += 1
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET markets_created = markets_created + 1 WHERE id = %s", (user_id,))
                conn.commit()
        finally:
            self._put_conn(conn)

    def increment_user_total_bets(self, user_id: str) -> None:
        """Increment user's total_bets counter."""
        if self._use_memory:
            self._users[user_id]["total_bets"] += 1
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET total_bets = total_bets + 1 WHERE id = %s", (user_id,))
                conn.commit()
        finally:
            self._put_conn(conn)

    def update_user_profit(self, user_id: str, profit_delta: float) -> None:
        """Update user's profit_all_time."""
        if self._use_memory:
            self._users[user_id]["profit_all_time"] += profit_delta
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET profit_all_time = profit_all_time + %s WHERE id = %s", (profit_delta, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)

    def update_user_status(self, user_id: str, status: str) -> None:
        """Update user's claim status."""
        if self._use_memory:
            self._users[user_id]["status"] = status
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET status = %s WHERE id = %s", (status, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)

    def update_user_last_market_created(self, user_id: str) -> None:
        """Update timestamp when user last created a market (for rate limiting)."""
        now = datetime.now(timezone.utc)
        if self._use_memory:
            self._users[user_id]["last_market_created_at"] = now
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET last_market_created_at = %s WHERE id = %s",
                    (now, user_id)
                )
                conn.commit()
        finally:
            self._put_conn(conn)

    def update_user_twitter_handle(self, user_id: str, twitter_handle: str) -> None:
        """Update user's twitter handle (from verification tweet)."""
        if self._use_memory:
            self._users[user_id]["twitter_handle"] = twitter_handle
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET twitter_handle = %s WHERE id = %s", (twitter_handle, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)

    def count_users(self) -> int:
        """Count total users without loading all rows.

        O(1) via COUNT(*) instead of O(N) full-table load.
        Replaces ``len(db.users)`` in hot paths (health, startup).

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if self._use_memory:
            return len(self._users)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM users")
                return cur.fetchone()["cnt"]
        finally:
            self._put_conn(conn)

    def reset_sandbox_balance(self, user_id: str, amount: float) -> float:
        """Reset a sandbox agent's balance and stats.

        Sets balance to *amount*, resets total_bets and profit_all_time to 0.
        Returns the new balance, or -1.0 on failure.
        """
        if self._use_memory:
            user = self._users.get(user_id)
            if not user:
                return -1.0
            user["balance"] = amount
            user["total_bets"] = 0
            user["profit_all_time"] = 0.0
            return amount

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET balance = %s, total_bets = 0, profit_all_time = 0.0
                    WHERE id = %s
                    RETURNING balance
                """, (amount, user_id))
                row = cur.fetchone()
                conn.commit()
                if row:
                    return float(row["balance"])
                return -1.0
        finally:
            self._put_conn(conn)

    @property
    def users(self) -> Dict[str, UserDict]:
        """Get all users as dict.

        .. deprecated::
            Loads the **entire** users table into memory — O(N) on every call.
            Use targeted methods instead:
            - ``count_users()`` for counts
            - ``get_user(id)`` for single lookups
            - ``get_user_by_username(name)`` for name lookups
            - ``get_leaderboard_data()`` for leaderboard

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        warnings.warn(
            "db.users loads the entire users table into memory. "
            "Use count_users(), get_user(), or get_leaderboard_data() instead. "
            "See #54: https://github.com/shirtlessfounder/moltmarkets-api/issues/54",
            DeprecationWarning,
            stacklevel=2,
        )
        if self._use_memory:
            return self._users

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users")
                rows = cur.fetchall()
                return {row["id"]: self._row_to_user(row) for row in rows}
        finally:
            self._put_conn(conn)
