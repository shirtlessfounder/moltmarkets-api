"""
MoltMarkets Storage — comments, chat messages, and resolution votes.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from storage.types import ChatMessageDict, CommentDict, ResolutionVoteDict


class SocialStorageMixin:
    """Mixin providing comments, chat messages, and resolution vote methods."""

    # --- Comments ---

    def create_comment(self, comment_id: str, market_id: str, user_id: str,
                       content: str, parent_id: Optional[str] = None) -> CommentDict:
        """Create a new comment on a market."""
        now = datetime.now(timezone.utc)
        comment = CommentDict(
            id=comment_id,
            market_id=market_id,
            user_id=user_id,
            content=content,
            parent_id=parent_id,
            created_at=now,
        )

        if self._use_memory:
            if not hasattr(self, '_comments'):
                self._comments = {}
            self._comments[comment_id] = comment
            return comment

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO comments (id, market_id, user_id, content, parent_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (comment_id, market_id, user_id, content, parent_id, now))
                conn.commit()
        finally:
            self._put_conn(conn)
        return comment

    def get_market_comments(self, market_id: str) -> List[dict]:
        """Get all comments for a market, ordered by creation time.

        Returns dicts that may include a ``username`` key from the JOIN query
        (DB path) or plain CommentDicts (in-memory path).
        """
        if self._use_memory:
            if not hasattr(self, '_comments'):
                return []
            return sorted(
                [c for c in self._comments.values() if c["market_id"] == market_id],
                key=lambda x: x["created_at"]
            )

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.*, u.username
                    FROM comments c
                    JOIN users u ON c.user_id = u.id
                    WHERE c.market_id = %s
                    ORDER BY c.created_at ASC
                """, (market_id,))
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        finally:
            self._put_conn(conn)

    # --- Resolution Votes ---

    def save_resolution_votes(self, market_id: str, votes: List[dict]) -> None:
        """Save resolution votes for a market."""
        if self._use_memory:
            if not hasattr(self, '_resolution_votes'):
                self._resolution_votes = {}
            self._resolution_votes[market_id] = votes
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                for vote in votes:
                    vote_id = str(uuid.uuid4())
                    cur.execute("""
                        INSERT INTO resolution_votes (id, market_id, agent_id, vote, reasoning, sources, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        vote_id,
                        market_id,
                        vote["agent_id"],
                        vote["vote"],
                        vote["reasoning"],
                        json.dumps(vote.get("sources", [])),
                        vote["created_at"],
                    ))
                conn.commit()
        finally:
            self._put_conn(conn)

    def get_resolution_votes(self, market_id: str) -> List[ResolutionVoteDict]:
        """Get all resolution votes for a market."""
        if self._use_memory:
            if not hasattr(self, '_resolution_votes'):
                return []
            return self._resolution_votes.get(market_id, [])

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM resolution_votes
                    WHERE market_id = %s
                    ORDER BY created_at ASC
                """, (market_id,))
                rows = cur.fetchall()
                return [
                    ResolutionVoteDict(
                        id=row["id"],
                        market_id=row["market_id"],
                        agent_id=row["agent_id"],
                        vote=row["vote"],
                        reasoning=row["reasoning"],
                        sources=json.loads(row["sources"]) if row["sources"] else [],
                        created_at=row["created_at"],
                    )
                    for row in rows
                ]
        finally:
            self._put_conn(conn)

    # --- Chat Messages ---

    def create_chat_message(self, user_id: str, username: str, text: str, channel: str = "agents") -> ChatMessageDict:
        """Create a new chat message."""
        now = datetime.now(timezone.utc)
        msg_id = str(uuid.uuid4())
        message = ChatMessageDict(
            id=msg_id,
            user_id=user_id,
            username=username,
            text=text,
            channel=channel,
            created_at=now,
        )

        if self._use_memory:
            if not hasattr(self, '_chat_messages'):
                self._chat_messages = []
            self._chat_messages.append(message)
            return message

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO chat_messages (id, user_id, username, text, channel, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, user_id, username, text, channel, created_at
                """, (msg_id, user_id, username, text, channel, now))
                row = cur.fetchone()
                conn.commit()
                return ChatMessageDict(
                    id=str(row["id"]),
                    user_id=row["user_id"],
                    username=row["username"],
                    text=row["text"],
                    channel=row["channel"],
                    created_at=row["created_at"],
                )
        finally:
            self._put_conn(conn)

    def get_chat_messages(self, limit: int = 50, since: Optional[datetime] = None, channel: str = "agents") -> List[ChatMessageDict]:
        """Get recent chat messages, optionally filtering by since timestamp and channel."""
        if self._use_memory:
            if not hasattr(self, '_chat_messages'):
                return []
            msgs = [m for m in self._chat_messages if m.get("channel", "agents") == channel]
            if since:
                msgs = [m for m in msgs if m["created_at"] > since]
            msgs = sorted(msgs, key=lambda x: x["created_at"], reverse=True)
            return msgs[:limit]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if since:
                    cur.execute("""
                        SELECT id, user_id, username, text, channel, created_at
                        FROM chat_messages
                        WHERE channel = %s AND created_at > %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (channel, since, limit))
                else:
                    cur.execute("""
                        SELECT id, user_id, username, text, channel, created_at
                        FROM chat_messages
                        WHERE channel = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (channel, limit))
                rows = cur.fetchall()
                return [
                    ChatMessageDict(
                        id=str(row["id"]),
                        user_id=row["user_id"],
                        username=row["username"],
                        text=row["text"],
                        channel=row["channel"],
                        created_at=row["created_at"],
                    )
                    for row in rows
                ]
        finally:
            self._put_conn(conn)
