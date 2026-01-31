"""
MoltMarkets Storage — TypedDict definitions for storage return types.

Provides compile-time type safety for all dict-shaped data returned by
Storage methods.  IDE autocompletion, mypy checking, and contributor
clarity — without changing any runtime behavior.

See: https://github.com/shirtlessfounder/moltmarkets-api/issues/72
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from typing import TypedDict

from models import MarketStatus, Outcome


# =============================================================================
# Core entity types
# =============================================================================


class UserDict(TypedDict):
    """Shape returned by get_user, create_user, _row_to_user, etc."""

    id: str
    username: str
    display_name: str
    description: str
    balance: float
    created_at: datetime
    markets_created: int
    total_bets: int
    profit_all_time: float
    api_key_hash: Optional[str]
    status: str
    verification_code: Optional[str]
    last_market_created_at: Optional[datetime]
    twitter_handle: Optional[str]
    user_type: str
    is_sandbox: bool


class PoolDict(TypedDict):
    """YES/NO liquidity pool amounts."""

    YES: float
    NO: float


class MarketDict(TypedDict):
    """Shape returned by get_market, create_market, _row_to_market, etc."""

    id: str
    title: str
    description: Optional[str]
    status: MarketStatus
    closes_at: datetime
    created_at: datetime
    resolved_at: Optional[datetime]
    resolution: Optional[Outcome]
    total_volume: float
    creator_id: str
    pool: PoolDict
    p: float
    version: int
    committee: Optional[List[str]]
    resolution_deadline: Optional[datetime]


class MarketWithCreatorDict(MarketDict, total=False):
    """MarketDict extended with an optional creator_username from JOINs."""

    creator_username: Optional[str]


class BetDict(TypedDict):
    """Shape returned by create_bet, _row_to_bet, etc."""

    id: str
    market_id: str
    user_id: str
    outcome: Outcome
    amount: float
    shares: float
    avg_price: float
    probability_before: float
    probability_after: float
    created_at: datetime


class BetWithUsernameDict(BetDict, total=False):
    """BetDict extended with an optional username from JOINs."""

    username: str


class PositionDict(TypedDict):
    """Shape returned by get_position, _row_to_position, etc."""

    market_id: str
    user_id: str
    yes_shares: float
    no_shares: float
    total_invested: float


class CommentDict(TypedDict):
    """Shape returned by create_comment."""

    id: str
    market_id: str
    user_id: str
    content: str
    parent_id: Optional[str]
    created_at: datetime


class CommentWithUsernameDict(CommentDict, total=False):
    """CommentDict extended with username from JOINs."""

    username: str


class ResolutionVoteDict(TypedDict):
    """Shape returned by get_resolution_votes / save_resolution_votes."""

    id: str
    market_id: str
    agent_id: str
    vote: str
    reasoning: str
    sources: List[str]
    created_at: datetime


class ChatMessageDict(TypedDict):
    """Shape returned by create_chat_message / get_chat_messages."""

    id: str
    user_id: str
    username: str
    text: str
    channel: str
    created_at: datetime


class CommitteeVoteDict(TypedDict):
    """Shape returned by upsert_committee_vote / get_committee_votes."""

    id: str
    market_id: str
    agent_id: str
    outcome: str
    created_at: datetime


# =============================================================================
# Aggregate / query-specific types
# =============================================================================


class LeaderboardEntryDict(TypedDict):
    """Shape returned by get_leaderboard_data."""

    user_id: str
    username: str
    balance: float
    pnl: float
    total_volume: float
    win_rate: float


class ReputationDataDict(TypedDict):
    """Shape returned by get_reputation_data."""

    resolution_votes: List[ResolutionVoteDict]
    comments_count: int
