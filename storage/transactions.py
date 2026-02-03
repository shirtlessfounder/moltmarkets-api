"""
MoltMarkets Storage — transactions ledger (issue #173).

Records every balance-changing event for audit trail and user history.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class TransactionStorageMixin:
    """Mixin providing transaction ledger methods."""

    def _ensure_transactions_store(self) -> None:
        """Lazily initialise in-memory transactions list."""
        if not hasattr(self, "_transactions"):
            self._transactions: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record_transaction(
        self,
        user_id: str,
        amount: float,
        tx_type: str,
        market_id: Optional[str] = None,
        related_user_id: Optional[str] = None,
        balance_after: float = 0.0,
        metadata: Optional[dict] = None,
        *,
        _cursor: Any = None,
    ) -> Dict[str, Any]:
        """Insert a transaction row.

        When ``_cursor`` is provided the INSERT is executed on that cursor
        (allowing the caller to keep everything in a single DB transaction).
        Otherwise a standalone connection is used.
        """
        if self._use_memory:
            self._ensure_transactions_store()
            txn = {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "amount": amount,
                "type": tx_type,
                "market_id": market_id,
                "related_user_id": related_user_id,
                "balance_after": balance_after,
                "metadata": metadata,
                "created_at": datetime.now(timezone.utc),
            }
            self._transactions.append(txn)
            return txn

        sql = """
            INSERT INTO transactions
                (user_id, amount, type, market_id, related_user_id, balance_after, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """
        params = (
            user_id,
            amount,
            tx_type,
            market_id,
            related_user_id,
            balance_after,
            json.dumps(metadata) if metadata else None,
        )

        if _cursor is not None:
            _cursor.execute(sql, params)
            return dict(_cursor.fetchone())

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = dict(cur.fetchone())
                conn.commit()
                return row
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_transactions_for_user(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return paginated transaction history for a user (newest first)."""
        if self._use_memory:
            self._ensure_transactions_store()
            user_txns = sorted(
                [t for t in self._transactions if t["user_id"] == user_id],
                key=lambda t: t["created_at"],
                reverse=True,
            )
            return user_txns[offset : offset + limit]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM transactions
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, limit, offset),
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._put_conn(conn)
