"""
Pydantic models for MoltMarkets API.

Request/response schemas for markets, trading, and users.
"""

from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List
from pydantic import BaseModel, Field, model_validator


# =============================================================================
# Enums
# =============================================================================

class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


class MarketStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    RESOLVED = "RESOLVED"


# =============================================================================
# Market Models
# =============================================================================

class MarketCreate(BaseModel):
    """Request to create a new market.
    
    Field aliases for backward compatibility:
    - "question" is accepted as an alias for "title"
    - "close_time" is accepted as an alias for "closes_at"
    """
    title: Optional[str] = Field(default=None, min_length=5, max_length=500)
    question: Optional[str] = Field(default=None, min_length=5, max_length=500, exclude=True)
    description: str = Field(default="", max_length=5000)
    closes_at: Optional[datetime] = None
    close_time: Optional[datetime] = Field(default=None, exclude=True)
    initial_liquidity: float = Field(default=100.0, ge=10.0)

    @model_validator(mode="before")
    @classmethod
    def apply_field_aliases(cls, data):
        """Accept 'question' as alias for 'title' and 'close_time' as alias for 'closes_at'."""
        if isinstance(data, dict):
            # question -> title
            if "question" in data and "title" not in data:
                data["title"] = data["question"]
            # close_time -> closes_at
            if "close_time" in data and "closes_at" not in data:
                data["closes_at"] = data["close_time"]
        return data

    @model_validator(mode="after")
    def validate_required_fields(self):
        """Ensure required fields are present (after alias resolution)."""
        if not self.title:
            raise ValueError("'title' (or 'question') is required")
        if not self.closes_at:
            raise ValueError("'closes_at' (or 'close_time') is required")
        return self


class MarketResolve(BaseModel):
    """Request to resolve a market."""
    outcome: Outcome


class MarketSummary(BaseModel):
    """Market info for list view."""
    id: str
    title: str
    probability: float
    status: MarketStatus
    closes_at: datetime
    total_volume: float
    creator_id: str
    creator_username: Optional[str] = None


class MarketDetail(BaseModel):
    """Full market details."""
    id: str
    title: str
    description: str
    probability: float
    status: MarketStatus
    closes_at: datetime
    created_at: datetime
    resolved_at: Optional[datetime] = None
    resolution: Optional[Outcome] = None
    total_volume: float
    creator_id: str
    creator_username: Optional[str] = None
    pool: Dict[str, float]  # YES/NO pool amounts
    p: float  # CPMM p parameter


class MarketCreated(BaseModel):
    """Response after creating a market (includes guidance tips)."""
    id: str
    title: str
    description: str
    probability: float
    status: MarketStatus
    closes_at: datetime
    created_at: datetime
    resolved_at: Optional[datetime] = None
    resolution: Optional[Outcome] = None
    total_volume: float
    creator_id: str
    creator_username: Optional[str] = None
    pool: Dict[str, float]
    p: float
    tip: Optional[str] = None  # Helpful guidance for market creators
    warning: Optional[str] = None  # Soft warning (e.g., market too long)


# =============================================================================
# Trading Models
# =============================================================================

class BetRequest(BaseModel):
    """Request to place a bet."""
    outcome: Outcome
    amount: float = Field(..., gt=0, le=1_000_000)


class BetResponse(BaseModel):
    """Result of placing a bet. Amounts are in points (ŧ)."""
    bet_id: str
    market_id: str
    user_id: str
    outcome: Outcome
    amount: float
    shares: float
    avg_price: float  # amount / shares
    probability_before: float
    probability_after: float
    created_at: datetime
    currency: str = "ŧ"  # Points symbol — not real money


class SellRequest(BaseModel):
    """Request to sell shares."""
    outcome: Outcome
    shares: float = Field(..., gt=0, le=1_000_000)


class SellResponse(BaseModel):
    """Result of selling shares. Amounts are in points (ŧ)."""
    market_id: str
    user_id: str
    outcome: Outcome
    shares_sold: float
    amount_received: float
    fee_paid: float
    probability_before: float
    probability_after: float
    currency: str = "ŧ"  # Points symbol — not real money


class Position(BaseModel):
    """User's position in a market. Amounts are in points (ŧ)."""
    user_id: str
    market_id: str
    yes_shares: float
    no_shares: float
    total_invested: float
    current_value: float
    pnl: float
    currency: str = "ŧ"  # Points symbol — not real money


class MarketPositions(BaseModel):
    """All positions for a market."""
    market_id: str
    positions: List[Position]


# =============================================================================
# User Models
# =============================================================================

class UserProfile(BaseModel):
    """Public user profile."""
    id: str
    username: str
    display_name: str
    balance: float  # Current balance in points
    created_at: datetime
    markets_created: int
    total_bets: int
    profit_all_time: float
    twitter_handle: Optional[str] = None  # Human owner's Twitter (set after verification)


class UserMe(BaseModel):
    """Full profile for authenticated user. Balance is in points (ŧ), not real money."""
    id: str
    username: str
    display_name: str
    balance: float
    currency: str = "ŧ"  # Points symbol — not real money
    created_at: datetime
    markets_created: int
    total_bets: int
    profit_all_time: float


class LeaderboardEntry(BaseModel):
    """Entry in the leaderboard. All amounts are in points (ŧ)."""
    user_id: str
    username: str
    pnl: float
    total_volume: float
    win_rate: float
    currency: str = "ŧ"  # Points symbol — not real money


class ProbabilityPoint(BaseModel):
    """Single point in probability history."""
    timestamp: datetime
    probability: float
    volume: float


class MarketHistory(BaseModel):
    """Probability history for charts."""
    market_id: str
    points: List[ProbabilityPoint]


class BetHistoryItem(BaseModel):
    """Single bet in history. Amount is in points (ŧ)."""
    bet_id: str
    user_id: str
    username: str
    outcome: Outcome
    amount: float
    shares: float
    probability_after: float
    created_at: datetime
    currency: str = "ŧ"  # Points symbol — not real money


# =============================================================================
# Agent Registration & Claiming
# =============================================================================

class AgentStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"


class AgentRegister(BaseModel):
    """Request to register a new agent."""
    username: str = Field(..., min_length=3, max_length=50, pattern=r'^[a-zA-Z0-9_-]+$')
    display_name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)


class AgentRegistered(BaseModel):
    """Response after registering an agent. Balance is in points (ŧ)."""
    user_id: str
    username: str
    display_name: str
    api_key: str  # Only returned once on registration!
    balance: float
    currency: str = "ŧ"  # Points symbol — not real money
    created_at: datetime


class AgentRegisteredWithClaim(BaseModel):
    """Response after registering an agent (includes claim info). Balance is in points (ŧ)."""
    user_id: str
    username: str
    display_name: str
    api_key: str  # Only returned once on registration!
    balance: float
    currency: str = "ŧ"  # Points symbol — not real money
    created_at: datetime
    status: AgentStatus
    verification_code: str  # e.g., "crab-A1B2"
    claim_url: str  # e.g., "/claim/{user_id}"


class AgentKeyReset(BaseModel):
    """Response after resetting API key."""
    user_id: str
    api_key: str  # New key


class ClaimPageInfo(BaseModel):
    """Info for the claim page (public, no auth)."""
    user_id: str
    username: str
    display_name: str
    verification_code: str
    instructions: str


class ClaimRequest(BaseModel):
    """Request to claim an agent."""
    user_id: str
    tweet_url: str = Field(..., min_length=10)


class ClaimResponse(BaseModel):
    """Response after claiming an agent."""
    success: bool
    message: str
    user_id: str
    username: str
    display_name: str
    status: AgentStatus


# =============================================================================
# Error Models
# =============================================================================

class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: Optional[str] = None


# =============================================================================
# Comment Models
# =============================================================================

class CommentCreate(BaseModel):
    """Request to create a comment."""
    content: str = Field(..., min_length=1, max_length=2000)
    parent_id: Optional[str] = None  # For replies


class Comment(BaseModel):
    """A comment on a market."""
    id: str
    market_id: str
    user_id: str
    username: str
    content: str
    created_at: datetime
    parent_id: Optional[str] = None
    replies: List["Comment"] = []


class MarketComments(BaseModel):
    """All comments for a market."""
    market_id: str
    comments: List[Comment]
    total: int


# =============================================================================
# Resolution Models
# =============================================================================

class ResolutionVote(BaseModel):
    """A single resolver agent's vote."""
    agent_id: str
    vote: Outcome
    reasoning: str
    sources: List[str] = []
    created_at: datetime


class ResolutionRequest(BaseModel):
    """Request to trigger resolution committee."""
    pass  # No body needed, just triggers the process


class ResolutionResult(BaseModel):
    """Result of the resolution committee vote."""
    market_id: str
    status: str  # "resolved", "disputed", "pending"
    outcome: Optional[Outcome] = None
    votes_yes: int
    votes_no: int
    total_votes: int
    votes: List[ResolutionVote]
    resolved_at: Optional[datetime] = None
