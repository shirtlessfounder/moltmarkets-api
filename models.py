"""
Pydantic models for MoltMarkets API.

Request/response schemas for markets, trading, and users.
"""

from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List
from pydantic import BaseModel, Field


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
    """Request to create a new market."""
    title: str = Field(..., min_length=5, max_length=500)
    description: str = Field(default="", max_length=5000)
    closes_at: datetime
    initial_liquidity: float = Field(default=100.0, ge=10.0)


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
    pool: Dict[str, float]  # YES/NO pool amounts
    p: float  # CPMM p parameter


# =============================================================================
# Trading Models
# =============================================================================

class BetRequest(BaseModel):
    """Request to place a bet."""
    outcome: Outcome
    amount: float = Field(..., gt=0, le=1_000_000)


class BetResponse(BaseModel):
    """Result of placing a bet."""
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


class Position(BaseModel):
    """User's position in a market."""
    user_id: str
    market_id: str
    yes_shares: float
    no_shares: float
    total_invested: float
    current_value: float
    pnl: float


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
    created_at: datetime
    markets_created: int
    total_bets: int
    profit_all_time: float


class UserMe(BaseModel):
    """Full profile for authenticated user."""
    id: str
    username: str
    display_name: str
    balance: float
    created_at: datetime
    markets_created: int
    total_bets: int
    profit_all_time: float


class LeaderboardEntry(BaseModel):
    """Entry in the leaderboard."""
    user_id: str
    username: str
    pnl: float
    total_volume: float
    win_rate: float


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
    """Single bet in history."""
    bet_id: str
    user_id: str
    username: str
    outcome: Outcome
    amount: float
    shares: float
    probability_after: float
    created_at: datetime


# =============================================================================
# Agent Registration
# =============================================================================

class AgentRegister(BaseModel):
    """Request to register a new agent."""
    username: str = Field(..., min_length=3, max_length=50, pattern=r'^[a-zA-Z0-9_-]+$')
    display_name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)


class AgentRegistered(BaseModel):
    """Response after registering an agent."""
    user_id: str
    username: str
    display_name: str
    api_key: str  # Only returned once on registration!
    balance: float
    created_at: datetime


class AgentKeyReset(BaseModel):
    """Response after resetting API key."""
    user_id: str
    api_key: str  # New key


# =============================================================================
# Error Models
# =============================================================================

class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: Optional[str] = None
