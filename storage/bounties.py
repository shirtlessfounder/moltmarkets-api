"""
MoltMarkets Storage — bounty escrow (issue #180).

Stores escrow bounties: lock ŧ on creation, release on completion.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class BountyStorageMixin:
    """Mixin providing bounty escrow storage methods."""

    def _ensure_bounties_store(self) -> None:
        """Lazily initialise in-memory bounties list."""
        if not hasattr(self, "_bounties"):
            self._bounties: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_bounty(
        self,
        bounty_id: str,
        creator_id: str,
        title: str,
        amount: float,
        description: str = "",
        expires_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Create a new escrow bounty."""
        now = datetime.now(timezone.utc)

        if self._use_memory:
            self._ensure_bounties_store()
            bounty = {
                "id": bounty_id,
                "creator_id": creator_id,
                "title": title,
                "description": description,
                "amount": amount,
                "status": "open",
                "claimant_id": None,
                "created_at": now,
                "claimed_at": None,
                "disputed_at": None,
                "completed_at": None,
                "cancelled_at": None,
                "expires_at": expires_at,
            }
            self._bounties.append(bounty)
            return bounty

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bounties
                        (id, creator_id, title, description, amount, status, expires_at)
                    VALUES (%s, %s, %s, %s, %s, 'open', %s)
                    RETURNING *
                """, (bounty_id, creator_id, title, description, amount, expires_at))
                row = dict(cur.fetchone())
                conn.commit()
                return row
        finally:
            self._put_conn(conn)

    def get_bounty(self, bounty_id: str) -> Optional[Dict[str, Any]]:
        """Get a bounty by ID."""
        if self._use_memory:
            self._ensure_bounties_store()
            for b in self._bounties:
                if b["id"] == bounty_id:
                    return b
            return None

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bounties WHERE id = %s", (bounty_id,))
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._put_conn(conn)

    def list_bounties(
        self,
        status: Optional[str] = None,
        creator_id: Optional[str] = None,
        claimant_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List bounties with optional filters."""
        if self._use_memory:
            self._ensure_bounties_store()
            results = list(self._bounties)
            if status:
                results = [b for b in results if b["status"] == status]
            if creator_id:
                results = [b for b in results if b["creator_id"] == creator_id]
            if claimant_id:
                results = [b for b in results if b.get("claimant_id") == claimant_id]
            results.sort(key=lambda b: b["created_at"], reverse=True)
            return results[offset:offset + limit]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                conditions = []
                params = []
                if status:
                    conditions.append("status = %s")
                    params.append(status)
                if creator_id:
                    conditions.append("creator_id = %s")
                    params.append(creator_id)
                if claimant_id:
                    conditions.append("claimant_id = %s")
                    params.append(claimant_id)

                where = ""
                if conditions:
                    where = "WHERE " + " AND ".join(conditions)

                cur.execute(f"""
                    SELECT * FROM bounties {where}
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """, (*params, limit, offset))
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def update_bounty_status(
        self,
        bounty_id: str,
        status: str,
        claimant_id: Optional[str] = None,
        expected_status: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update bounty status and optionally set claimant.
        
        If expected_status is provided, performs atomic check-and-set.
        Returns None if current status doesn't match expected (lost race).
        """
        now = datetime.now(timezone.utc)

        if self._use_memory:
            self._ensure_bounties_store()
            for b in self._bounties:
                if b["id"] == bounty_id:
                    # Atomic check: verify current status before updating
                    if expected_status is not None and b["status"] != expected_status:
                        return None  # Lost the race
                    b["status"] = status
                    if claimant_id is not None:
                        b["claimant_id"] = claimant_id
                    if status == "claimed":
                        b["claimed_at"] = now
                    elif status == "disputed":
                        b["disputed_at"] = now
                    elif status == "completed":
                        b["completed_at"] = now
                    elif status == "cancelled":
                        b["cancelled_at"] = now
                    return b
            return None

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                set_parts = ["status = %s"]
                params = [status]

                if claimant_id is not None:
                    set_parts.append("claimant_id = %s")
                    params.append(claimant_id)
                if status == "claimed":
                    set_parts.append("claimed_at = %s")
                    params.append(now)
                elif status == "disputed":
                    set_parts.append("disputed_at = %s")
                    params.append(now)
                elif status == "completed":
                    set_parts.append("completed_at = %s")
                    params.append(now)
                elif status == "cancelled":
                    set_parts.append("cancelled_at = %s")
                    params.append(now)

                params.append(bounty_id)
                where_clause = "WHERE id = %s"
                if expected_status is not None:
                    where_clause += " AND status = %s"
                    params.append(expected_status)
                    
                cur.execute(f"""
                    UPDATE bounties SET {', '.join(set_parts)}
                    {where_clause} RETURNING *
                """, tuple(params))
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None
        finally:
            self._put_conn(conn)

    def count_bounties(self, status: Optional[str] = None) -> int:
        """Count bounties, optionally filtered by status."""
        if self._use_memory:
            self._ensure_bounties_store()
            if status:
                return sum(1 for b in self._bounties if b["status"] == status)
            return len(self._bounties)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute("SELECT COUNT(*) AS cnt FROM bounties WHERE status = %s", (status,))
                else:
                    cur.execute("SELECT COUNT(*) AS cnt FROM bounties")
                return cur.fetchone()["cnt"]
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Votes (arbiter votes on disputed bounties)
    # ------------------------------------------------------------------

    def _ensure_votes_store(self) -> None:
        if not hasattr(self, "_bounty_votes"):
            self._bounty_votes: List[Dict[str, Any]] = []

    def add_vote(self, bounty_id: str, voter_id: str, vote: str, reason: str = "") -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        if self._use_memory:
            self._ensure_votes_store()
            for v in self._bounty_votes:
                if v["bounty_id"] == bounty_id and v["voter_id"] == voter_id:
                    return None
            vote_record = {"bounty_id": bounty_id, "voter_id": voter_id, "vote": vote, "reason": reason, "voted_at": now}
            self._bounty_votes.append(vote_record)
            return vote_record
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO bounty_votes (bounty_id, voter_id, vote, reason, voted_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (bounty_id, voter_id) DO NOTHING RETURNING *""", (bounty_id, voter_id, vote, reason, now))
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None
        finally:
            self._put_conn(conn)

    def get_votes(self, bounty_id: str) -> List[Dict[str, Any]]:
        if self._use_memory:
            self._ensure_votes_store()
            return [v for v in self._bounty_votes if v["bounty_id"] == bounty_id]
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bounty_votes WHERE bounty_id = %s ORDER BY voted_at", (bounty_id,))
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def count_votes(self, bounty_id: str) -> Dict[str, int]:
        if self._use_memory:
            self._ensure_votes_store()
            votes = [v for v in self._bounty_votes if v["bounty_id"] == bounty_id]
            return {"creator": sum(1 for v in votes if v["vote"] == "creator"), "claimant": sum(1 for v in votes if v["vote"] == "claimant"), "total": len(votes)}
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT vote, COUNT(*) as cnt FROM bounty_votes WHERE bounty_id = %s GROUP BY vote", (bounty_id,))
                counts = {"creator": 0, "claimant": 0, "total": 0}
                for row in cur.fetchall():
                    counts[row["vote"]] = row["cnt"]
                    counts["total"] += row["cnt"]
                return counts
        finally:
            self._put_conn(conn)
