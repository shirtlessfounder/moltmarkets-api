"""
MoltMarkets Storage — committee vote methods (issue #107).
"""

import json
import uuid
from datetime import datetime, timezone
from typing import List

from storage.types import CommitteeVoteDict


class CommitteeStorageMixin:
    """Mixin providing committee vote storage methods."""

    # --- Committee Votes (issue #107) ---

    def update_market_committee(self, market_id: str, committee: List[str], resolution_deadline: datetime) -> None:
        """Set the committee members and resolution deadline for a market."""
        if self._use_memory:
            market = self._markets.get(market_id)
            if market:
                market["committee"] = committee
                market["resolution_deadline"] = resolution_deadline
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE markets
                    SET committee = %s, resolution_deadline = %s
                    WHERE id = %s
                """, (json.dumps(committee), resolution_deadline, market_id))
                conn.commit()
        finally:
            self._put_conn(conn)

    def upsert_committee_vote(self, market_id: str, agent_id: str, outcome: str) -> CommitteeVoteDict:
        """Insert or update a committee vote (one vote per member per market)."""
        now = datetime.now(timezone.utc)
        vote_id = str(uuid.uuid4())

        if self._use_memory:
            if not hasattr(self, '_committee_votes'):
                self._committee_votes = {}
            if market_id not in self._committee_votes:
                self._committee_votes[market_id] = {}
            vote = CommitteeVoteDict(
                id=vote_id,
                market_id=market_id,
                agent_id=agent_id,
                outcome=outcome,
                created_at=now,
            )
            self._committee_votes[market_id][agent_id] = vote
            return vote

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Try update first
                cur.execute("""
                    UPDATE committee_votes
                    SET outcome = %s, created_at = %s
                    WHERE market_id = %s AND agent_id = %s
                    RETURNING *
                """, (outcome, now, market_id, agent_id))
                row = cur.fetchone()
                if row:
                    conn.commit()
                    return CommitteeVoteDict(
                        id=row["id"],
                        market_id=row["market_id"],
                        agent_id=row["agent_id"],
                        outcome=row["outcome"],
                        created_at=row["created_at"],
                    )
                # Insert if no existing vote
                cur.execute("""
                    INSERT INTO committee_votes (id, market_id, agent_id, outcome, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                """, (vote_id, market_id, agent_id, outcome, now))
                row = cur.fetchone()
                conn.commit()
                return CommitteeVoteDict(
                    id=row["id"],
                    market_id=row["market_id"],
                    agent_id=row["agent_id"],
                    outcome=row["outcome"],
                    created_at=row["created_at"],
                )
        finally:
            self._put_conn(conn)

    def get_committee_votes(self, market_id: str) -> List[CommitteeVoteDict]:
        """Get all committee votes for a market."""
        if self._use_memory:
            if not hasattr(self, '_committee_votes'):
                return []
            votes = self._committee_votes.get(market_id, {})
            return list(votes.values())

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM committee_votes
                    WHERE market_id = %s
                    ORDER BY created_at ASC
                """, (market_id,))
                rows = cur.fetchall()
                return [
                    CommitteeVoteDict(
                        id=row["id"],
                        market_id=row["market_id"],
                        agent_id=row["agent_id"],
                        outcome=row["outcome"],
                        created_at=row["created_at"],
                    )
                    for row in rows
                ]
        finally:
            self._put_conn(conn)
