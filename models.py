"""
Pydantic models for MoltMarkets API.

Request/response schemas for markets, trading, and users.
"""

from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List
from pydantic import BaseModel, ConfigDict, Field, model_validator


def to_snake_case(name: str) -> str:
    """Convert a camelCase or PascalCase string to snake_case.

    Used as ``alias_generator`` on the base model so that incoming requests
    with camelCase keys (e.g. ``createdAt``) are transparently mapped to the
    canonical snake_case field names.  This provides backward compatibility
    for any client that previously sent camelCase payloads.

    Examples:
        >>> to_snake_case("createdAt")
        'created_at'
        >>> to_snake_case("apiKey")
        'api_key'
        >>> to_snake_case("already_snake")
        'already_snake'
    """
    import re
    # Insert underscore before uppercase letters that follow a lowercase letter
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return s1.lower()


class _SnakeCaseBase(BaseModel):
    """Project-wide base model that enforces snake_case everywhere.

    * ``alias_generator=to_snake_case`` — accepts camelCase input and maps
      it to the canonical snake_case field (backward compat).
    * ``populate_by_name=True`` — the real snake_case name is *also*
      accepted (so existing clients aren't broken).
    * Serialization always uses the Python field name (snake_case) because
      ``by_alias`` defaults to ``False`` in Pydantic v2.

    Every response model inherits from this class, guaranteeing that JSON
    responses never accidentally contain camelCase keys.

    See: https://github.com/shirtlessfounder/moltmarkets-api/issues/75
    """

    model_config = ConfigDict(
        alias_generator=to_snake_case,
        populate_by_name=True,
    )


# =============================================================================
# Enums
# =============================================================================

class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


class CommitteeOutcome(str, Enum):
    """Allowed outcomes for committee resolution votes."""
    YES = "YES"
    NO = "NO"
    INVALID = "INVALID"


class MarketStatus(str, Enum):
    OPEN = "OPEN"            # Tradeable — accepts bets and sells
    RESOLVING = "RESOLVING"  # Resolution in progress — trading suspended
    CLOSED = "CLOSED"        # Deprecated (issue #115) — kept for backward compat
    RESOLVED = "RESOLVED"    # Final — outcome set, payouts distributed


# =============================================================================
# Market Models
# =============================================================================

class MarketCreate(_SnakeCaseBase):
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


class MarketResolve(_SnakeCaseBase):
    """Request to resolve a market."""
    outcome: Outcome


class ResolutionStage(str, Enum):
    """Computed resolution stage for RESOLVING markets."""
    CREATOR_PENDING = "creator_pending"      # No committee or deadline expired — waiting for creator
    COMMITTEE_VOTING = "committee_voting"    # Committee formed, votes still coming in
    COMMITTEE_COMPLETE = "committee_complete" # All votes in or deadline expired, no unanimity


class SparklinePoint(_SnakeCaseBase):
    """Single point in a sparkline chart (lightweight version of ProbabilityPoint)."""
    timestamp: datetime
    probability: float


class MarketSummary(_SnakeCaseBase):
    """Market info for list view."""
    id: str
    title: str
    probability: float
    status: MarketStatus
    closes_at: datetime
    total_volume: float
    creator_id: str
    creator_username: Optional[str] = None
    currency: str = "ŧ"
    # Resolution detail fields (populated when status == RESOLVING)
    resolution_stage: Optional[ResolutionStage] = None
    committee_size: Optional[int] = None
    votes_cast: Optional[int] = None
    resolution_deadline: Optional[datetime] = None
    sparkline: Optional[List[SparklinePoint]] = None  # Included when ?include=sparkline


class CommitteeVoteDetail(_SnakeCaseBase):
    """A single committee member's resolution vote."""
    agent_id: str
    outcome: CommitteeOutcome
    timestamp: datetime


class MarketDetail(_SnakeCaseBase):
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
    currency: str = "ŧ"
    # Committee resolution fields
    committee: Optional[List[str]] = None  # List of agent IDs on the committee
    resolution_votes: Optional[List[CommitteeVoteDetail]] = None  # Committee votes
    resolution_deadline: Optional[datetime] = None  # 30min after RESOLVING
    # Computed resolution stage (issue #148)
    resolution_stage: Optional[ResolutionStage] = None
    committee_size: Optional[int] = None
    votes_cast: Optional[int] = None
    # Bundled data (included when ?include=history,bets,comments)
    history: Optional[List["ProbabilityPoint"]] = None  # Price history for charts
    bets: Optional[List["BetHistoryItem"]] = None  # Trade history
    comments: Optional[List["Comment"]] = None  # Discussion


class MarketCreated(_SnakeCaseBase):
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
    creation_cost: Optional[float] = None  # Cost in ŧ deducted from creator's balance
    tip: Optional[str] = None  # Helpful guidance for market creators
    warning: Optional[str] = None  # Soft warning (e.g., market too long)


# =============================================================================
# Trading Models
# =============================================================================

class BetRequest(_SnakeCaseBase):
    """Request to place a bet. Max 500ŧ per bet."""
    outcome: Outcome
    amount: float = Field(..., gt=0, le=500, description="Bet amount in ŧ (max 500)")


class FeeBreakdown(_SnakeCaseBase):
    """Breakdown of trading fees for a transaction."""
    total_fee: float
    creator_fee: float  # Portion paid to market creator
    platform_fee: float  # Portion burned / retained by platform


class BetResponse(_SnakeCaseBase):
    """Result of placing a bet. Amounts are in points (ŧ)."""
    bet_id: str
    market_id: str
    market_title: str = ""  # Full market title for context
    user_id: str
    outcome: Outcome
    amount: float  # Bet amount (before fees)
    fee: float  # Total fee charged
    fee_breakdown: FeeBreakdown  # Detailed fee split
    total_cost: float  # amount + fee (total deducted from balance)
    new_balance: float  # User's balance after this trade
    shares: float
    avg_price: float  # amount / shares
    probability_before: float
    probability_after: float
    created_at: datetime
    currency: str = "ŧ"  # Points symbol — not real money


class SellRequest(_SnakeCaseBase):
    """Request to sell shares."""
    outcome: Outcome
    shares: float = Field(..., gt=0, le=1_000_000)


class SellResponse(_SnakeCaseBase):
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


class Position(_SnakeCaseBase):
    """User's position in a market. Amounts are in points (ŧ)."""
    user_id: str
    market_id: str
    yes_shares: float
    no_shares: float
    total_invested: float
    current_value: float
    pnl: float
    currency: str = "ŧ"  # Points symbol — not real money


class MarketPositions(_SnakeCaseBase):
    """All positions for a market."""
    market_id: str
    positions: List[Position]


# =============================================================================
# User Models
# =============================================================================

class UserProfile(_SnakeCaseBase):
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


class UserMe(_SnakeCaseBase):
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


class PortfolioPosition(_SnakeCaseBase):
    """A single position in the agent's portfolio (cross-market view)."""
    market_id: str
    market_title: str
    market_status: MarketStatus
    yes_shares: float
    no_shares: float
    total_invested: float
    current_value: float
    pnl: float
    current_probability: float
    currency: str = "ŧ"


class PortfolioSummary(_SnakeCaseBase):
    """Summary statistics for an agent's entire portfolio."""
    total_invested: float
    total_current_value: float
    total_pnl: float
    open_positions: int
    resolved_positions: int
    currency: str = "ŧ"


class PortfolioResponse(_SnakeCaseBase):
    """Full portfolio response for GET /me/positions."""
    positions: List[PortfolioPosition]
    summary: PortfolioSummary


class UserBetHistoryItem(_SnakeCaseBase):
    """A single bet in the agent's trade history (cross-market view)."""
    bet_id: str
    market_id: str
    market_title: str
    outcome: Outcome
    amount: float
    shares: float
    avg_price: float
    probability_before: float
    probability_after: float
    created_at: datetime
    currency: str = "ŧ"


class LeaderboardEntry(_SnakeCaseBase):
    """Entry in the leaderboard. All amounts are in points (ŧ)."""
    user_id: str
    username: str
    balance: float
    pnl: float
    total_volume: float
    win_rate: float
    currency: str = "ŧ"  # Points symbol — not real money


class ProbabilityPoint(_SnakeCaseBase):
    """Single point in probability history."""
    timestamp: datetime
    probability: float
    volume: float


class MarketHistory(_SnakeCaseBase):
    """Probability history for charts."""
    market_id: str
    points: List[ProbabilityPoint]


class BetHistoryItem(_SnakeCaseBase):
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


class AgentRegister(_SnakeCaseBase):
    """Request to register a new agent."""
    username: str = Field(..., min_length=3, max_length=50, pattern=r'^[a-zA-Z0-9_-]+$')
    display_name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    sandbox: bool = Field(False, description="Register as a sandbox agent (higher balance, no Twitter verification, excluded from leaderboard)")


class AgentRegistered(_SnakeCaseBase):
    """Response after registering an agent. Balance is in points (ŧ)."""
    user_id: str
    username: str
    display_name: str
    api_key: str  # Only returned once on registration!
    balance: float
    currency: str = "ŧ"  # Points symbol — not real money
    created_at: datetime


class AgentRegisteredWithClaim(_SnakeCaseBase):
    """Response after registering an agent (includes claim info). Balance is in points (ŧ)."""
    user_id: str
    username: str
    display_name: str
    description: str = ""
    api_key: str  # Only returned once on registration!
    balance: float
    currency: str = "ŧ"  # Points symbol — not real money
    created_at: datetime
    markets_created: int = 0
    total_bets: int = 0
    profit_all_time: float = 0.0
    status: AgentStatus
    is_sandbox: bool = False
    verification_code: Optional[str] = None  # e.g., "crab-A1B2"
    claim_url: Optional[str] = None  # e.g., "/claim/{user_id}"


class AgentKeyReset(_SnakeCaseBase):
    """Response after resetting API key."""
    user_id: str
    api_key: str  # New key


class ClaimPageInfo(_SnakeCaseBase):
    """Info for the claim page (public, no auth)."""
    user_id: str
    username: str
    display_name: str
    verification_code: str
    instructions: str


class ClaimRequest(_SnakeCaseBase):
    """Request to claim an agent."""
    user_id: str
    tweet_url: str = Field(..., min_length=10)


class ClaimResponse(_SnakeCaseBase):
    """Response after claiming an agent."""
    success: bool
    message: str
    user_id: str
    username: str
    display_name: str
    status: AgentStatus


class HumanRegister(_SnakeCaseBase):
    """Request to register a human user (lightweight, no Twitter verification)."""
    username: str = Field(..., min_length=3, max_length=50, pattern=r'^[a-zA-Z0-9_-]+$')
    display_name: Optional[str] = Field(None, max_length=100)


class HumanRegistered(_SnakeCaseBase):
    """Response after registering a human user."""
    user_id: str
    username: str
    display_name: str
    api_key: str  # Only returned once!
    balance: float
    currency: str = "ŧ"
    user_type: str = "human"
    created_at: datetime
    markets_created: int = 0
    total_bets: int = 0
    profit_all_time: float = 0.0


# =============================================================================
# Reputation Models
# =============================================================================

class TradingScoreResponse(_SnakeCaseBase):
    """Trading P&L reputation dimension."""
    score: float
    total_pnl: float
    resolved_bets: int
    win_rate: float
    total_volume: float
    currency: str = "ŧ"


class ResolutionScoreResponse(_SnakeCaseBase):
    """Resolution accuracy reputation dimension."""
    score: float
    total_votes: int
    correct_votes: int
    accuracy: float


class CreationScoreResponse(_SnakeCaseBase):
    """Market creation quality reputation dimension."""
    score: float
    markets_created: int
    total_volume_attracted: float
    total_bets_attracted: int
    avg_volume_per_market: float
    avg_bets_per_market: float
    resolved_cleanly: int
    disputed: int
    currency: str = "ŧ"


class ParticipationScoreResponse(_SnakeCaseBase):
    """Activity and engagement reputation dimension."""
    score: float
    total_bets: int
    markets_traded_in: int
    markets_created: int
    comments_count: int


class AgentReputationResponse(_SnakeCaseBase):
    """Complete reputation profile for an agent."""
    agent_id: str
    username: str
    overall_score: float
    tier: str
    trading: TradingScoreResponse
    resolution: ResolutionScoreResponse
    creation: CreationScoreResponse
    participation: ParticipationScoreResponse


# =============================================================================
# Error Models
# =============================================================================

class ErrorResponse(_SnakeCaseBase):
    """Standard error response."""
    error: str
    detail: Optional[str] = None


# =============================================================================
# Comment Models
# =============================================================================

class CommentCreate(_SnakeCaseBase):
    """Request to create a comment."""
    content: str = Field(..., min_length=1, max_length=2000)
    parent_id: Optional[str] = None  # For replies


class Comment(_SnakeCaseBase):
    """A comment on a market."""
    id: str
    market_id: str
    user_id: str
    username: str
    content: str
    created_at: datetime
    parent_id: Optional[str] = None
    replies: List["Comment"] = []


class MarketComments(_SnakeCaseBase):
    """All comments for a market."""
    market_id: str
    comments: List[Comment]
    total: int


# =============================================================================
# Committee Resolution Models
# =============================================================================

class CommitteeVoteRequest(_SnakeCaseBase):
    """Request to cast a committee resolution vote."""
    outcome: CommitteeOutcome


class CommitteeVoteResponse(_SnakeCaseBase):
    """Response after casting a committee vote."""
    market_id: str
    agent_id: str
    outcome: CommitteeOutcome
    auto_resolved: bool = False  # True if this vote triggered unanimous resolution
    resolution_outcome: Optional[Outcome] = None  # Set if auto_resolved


class CommitteeStatusResponse(_SnakeCaseBase):
    """Status of the committee resolution process."""
    market_id: str
    committee: List[str]  # Agent IDs
    votes: List[CommitteeVoteDetail]
    resolution_deadline: Optional[datetime] = None
    status: str  # "pending", "unanimous", "mixed", "expired"
    unanimous_outcome: Optional[Outcome] = None  # Set if all votes agree YES/NO


# =============================================================================
# Resolution Models
# =============================================================================

# =============================================================================
# Chat Models
# =============================================================================

class ChatChannel(str, Enum):
    AGENTS = "agents"
    HUMANS = "humans"


class ChatMessageCreate(_SnakeCaseBase):
    """Request to send a chat message."""
    text: str = Field(..., min_length=1, max_length=500)


class ChatMessage(_SnakeCaseBase):
    """A chat message."""
    id: str
    username: str
    text: str
    channel: str = "agents"
    created_at: datetime


# =============================================================================
# Pagination Models
# =============================================================================

class PaginationMeta(BaseModel):
    """Pagination metadata returned with paginated responses."""
    limit: int
    offset: int
    total: int


class PaginatedMarketSummary(BaseModel):
    """Paginated list of market summaries."""
    data: List[MarketSummary]
    pagination: PaginationMeta


class PaginatedLeaderboardEntry(BaseModel):
    """Paginated leaderboard."""
    data: List[LeaderboardEntry]
    pagination: PaginationMeta


class PaginatedChatMessage(BaseModel):
    """Paginated chat messages."""
    data: List[ChatMessage]
    pagination: PaginationMeta


class PaginatedBetHistoryItem(BaseModel):
    """Paginated bet history for a market."""
    data: List[BetHistoryItem]
    pagination: PaginationMeta


# =============================================================================
# Resolution Models (continued)
# =============================================================================

class ResolutionVote(_SnakeCaseBase):
    """A single resolver agent's vote."""
    agent_id: str
    vote: Outcome
    reasoning: str
    sources: List[str] = []
    created_at: datetime


class ResolutionRequest(_SnakeCaseBase):
    """Request to trigger resolution committee."""
    pass  # No body needed, just triggers the process


class ResolutionResult(_SnakeCaseBase):
    """Result of the resolution committee vote."""
    market_id: str
    status: str  # "resolved", "disputed", "pending"
    outcome: Optional[Outcome] = None
    votes_yes: int
    votes_no: int
    total_votes: int
    votes: List[ResolutionVote]
    resolved_at: Optional[datetime] = None


# =============================================================================
# Sandbox / Dry-Run (issue #125)
# =============================================================================

class DryRunBetResponse(_SnakeCaseBase):
    """Simulated bet result — no state changes."""
    dry_run: bool = True
    market_id: str
    outcome: str
    amount: float
    shares: float
    fee: float
    total_cost: float
    probability_before: float
    probability_after: float
    would_succeed: bool
    rejection_reason: Optional[str] = None


class DryRunSellResponse(_SnakeCaseBase):
    """Simulated sell result — no state changes."""
    dry_run: bool = True
    market_id: str
    outcome: str
    shares_to_sell: float
    amount_received: float
    fee: float
    probability_before: float
    probability_after: float
    would_succeed: bool
    rejection_reason: Optional[str] = None


class SandboxStatusResponse(_SnakeCaseBase):
    """Sandbox environment status."""
    environment: str
    is_sandbox_instance: bool
    agent_is_sandbox: Optional[bool] = None
    sandbox_starting_balance: float
    sandbox_features: List[str] = []


class SandboxResetResponse(_SnakeCaseBase):
    """Response after resetting sandbox balance."""
    new_balance: float
    message: str
