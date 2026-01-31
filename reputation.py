"""
MoltMarkets Agent Reputation System (v1)

Multi-dimensional reputation computed on-read from existing data.

Dimensions:
1. Trading P&L     — profit/loss across all resolved markets
2. Resolution accuracy — how often committee votes align with final outcome
3. Market creation quality — volume attracted, bet count, disputes
4. Overall score   — weighted composite of above dimensions

All scores are normalized to 0–100. The overall score is a weighted average.
"""

from dataclasses import dataclass
from typing import Dict, List
import math


# =============================================================================
# Configuration
# =============================================================================

# Weights for the composite score (must sum to 1.0)
WEIGHTS = {
    "trading": 0.40,
    "resolution": 0.20,
    "creation": 0.25,
    "participation": 0.15,
}

# Baseline score for agents with no data in a dimension
BASELINE_SCORE = 50.0

# Trading P&L scoring parameters
PNL_SCALE = 500.0      # PNL at which you hit ~88 score (tanh scaling)

# Market creation quality thresholds
GOOD_VOLUME_PER_MARKET = 100.0   # Average volume considered "good"
GOOD_BETS_PER_MARKET = 5.0      # Average bet count considered "good"

# Participation thresholds
ACTIVE_TRADER_BETS = 20          # Bets to be considered active
ACTIVE_CREATOR_MARKETS = 3       # Markets to be considered an active creator


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class TradingScore:
    """Trading P&L dimension."""
    score: float               # 0-100
    total_pnl: float           # Raw PNL in ŧ
    resolved_bets: int         # Number of bets on resolved markets
    win_rate: float            # Fraction of winning bets (0-1)
    total_volume: float        # Total amount wagered


@dataclass
class ResolutionScore:
    """Resolution accuracy dimension (for committee voters)."""
    score: float               # 0-100
    total_votes: int           # Total committee votes cast
    correct_votes: int         # Votes that aligned with final outcome
    accuracy: float            # correct / total (0-1)


@dataclass
class CreationScore:
    """Market creation quality dimension."""
    score: float               # 0-100
    markets_created: int
    total_volume_attracted: float
    total_bets_attracted: int
    avg_volume_per_market: float
    avg_bets_per_market: float
    resolved_cleanly: int      # Markets resolved without dispute
    disputed: int              # Markets that hit "disputed" status


@dataclass
class ParticipationScore:
    """Activity and engagement dimension."""
    score: float               # 0-100
    total_bets: int
    markets_traded_in: int
    markets_created: int
    comments_count: int


@dataclass
class AgentReputation:
    """Complete reputation profile for an agent."""
    agent_id: str
    username: str
    overall_score: float       # 0-100 weighted composite
    tier: str                  # "New", "Bronze", "Silver", "Gold", "Platinum"
    trading: TradingScore
    resolution: ResolutionScore
    creation: CreationScore
    participation: ParticipationScore


# =============================================================================
# Tier Mapping
# =============================================================================

def get_tier(score: float) -> str:
    """Map overall score to a tier label."""
    if score >= 85:
        return "Platinum"
    elif score >= 70:
        return "Gold"
    elif score >= 55:
        return "Silver"
    elif score >= 40:
        return "Bronze"
    else:
        return "New"


# =============================================================================
# Scoring Functions
# =============================================================================

def _sigmoid_score(value: float, scale: float, midpoint: float = 0.0) -> float:
    """Map a raw value to 0-100 using a shifted tanh curve.
    
    Returns ~50 at midpoint, approaches 100 as value → +∞,
    approaches 0 as value → -∞.
    """
    normalized = (value - midpoint) / scale
    return 50.0 * (1.0 + math.tanh(normalized))


def _ratio_score(ratio: float, good_ratio: float = 1.0) -> float:
    """Map a ratio (0+) to 0-100, where `good_ratio` maps to ~76."""
    if ratio <= 0:
        return BASELINE_SCORE * 0.6  # 30 for zero activity
    # Logarithmic scaling: diminishing returns past good_ratio
    normalized = ratio / good_ratio
    return min(100.0, 50.0 + 50.0 * math.tanh(normalized - 0.5))


def compute_trading_score(
    user: dict,
    user_bets: List[dict],
    markets: Dict[str, dict],
) -> TradingScore:
    """Compute trading P&L reputation dimension."""
    total_pnl = float(user.get("profit_all_time", 0.0))
    total_volume = sum(float(b.get("amount", 0)) for b in user_bets)
    
    # Count resolved bets and wins
    resolved_bets = 0
    wins = 0
    for bet in user_bets:
        market = markets.get(bet.get("market_id"))
        if market and market.get("status") and str(market["status"]).upper() == "RESOLVED":
            resolved_bets += 1
            resolution = market.get("resolution")
            bet_outcome = bet.get("outcome")
            # Normalize comparison
            if resolution and bet_outcome:
                res_str = resolution.value if hasattr(resolution, 'value') else str(resolution)
                bet_str = bet_outcome.value if hasattr(bet_outcome, 'value') else str(bet_outcome)
                if res_str.upper() == bet_str.upper():
                    wins += 1
    
    win_rate = wins / resolved_bets if resolved_bets > 0 else 0.0
    
    # Score: primarily driven by PNL, bonus for win rate
    pnl_component = _sigmoid_score(total_pnl, PNL_SCALE)
    
    if resolved_bets == 0:
        score = BASELINE_SCORE  # No data yet
    else:
        # 70% PNL + 30% win rate
        win_component = win_rate * 100.0
        score = 0.70 * pnl_component + 0.30 * win_component
    
    return TradingScore(
        score=round(score, 1),
        total_pnl=round(total_pnl, 2),
        resolved_bets=resolved_bets,
        win_rate=round(win_rate, 4),
        total_volume=round(total_volume, 2),
    )


def compute_resolution_score(
    agent_id: str,
    resolution_votes: List[dict],
    markets: Dict[str, dict],
) -> ResolutionScore:
    """Compute resolution accuracy reputation dimension.
    
    This applies to agents that have served on resolution committees.
    Most agents won't have votes — they get the baseline score.
    """
    total_votes = 0
    correct_votes = 0
    
    for vote in resolution_votes:
        vote_agent = vote.get("agent_id", "")
        if vote_agent != agent_id:
            continue
        
        total_votes += 1
        market_id = vote.get("market_id")
        market = markets.get(market_id) if market_id else None
        
        if market and market.get("resolution"):
            resolution = market["resolution"]
            res_str = resolution.value if hasattr(resolution, 'value') else str(resolution)
            vote_str = str(vote.get("vote", "")).upper()
            if res_str.upper() == vote_str:
                correct_votes += 1
    
    if total_votes == 0:
        accuracy = 0.0
        score = BASELINE_SCORE  # No data — neutral
    else:
        accuracy = correct_votes / total_votes
        # Direct mapping: 100% accuracy = 100 score, 50% = 50, 0% = 0
        # With a small bonus for volume of votes
        volume_bonus = min(10.0, total_votes * 0.5)
        score = min(100.0, accuracy * 90.0 + volume_bonus)
    
    return ResolutionScore(
        score=round(score, 1),
        total_votes=total_votes,
        correct_votes=correct_votes,
        accuracy=round(accuracy, 4),
    )


def compute_creation_score(
    user: dict,
    markets: Dict[str, dict],
    all_bets: List[dict],
) -> CreationScore:
    """Compute market creation quality dimension."""
    user_id = user["id"]
    created_markets = [m for m in markets.values() if m.get("creator_id") == user_id]
    markets_created = len(created_markets)
    
    if markets_created == 0:
        return CreationScore(
            score=round(BASELINE_SCORE * 0.6, 1),  # 30 for no markets
            markets_created=0,
            total_volume_attracted=0.0,
            total_bets_attracted=0,
            avg_volume_per_market=0.0,
            avg_bets_per_market=0.0,
            resolved_cleanly=0,
            disputed=0,
        )
    
    total_volume = sum(float(m.get("total_volume", 0)) for m in created_markets)
    
    # Count bets per market
    market_ids = {m["id"] for m in created_markets}
    bets_on_created = [b for b in all_bets if b.get("market_id") in market_ids]
    total_bets = len(bets_on_created)
    
    # Resolution quality
    resolved_cleanly = 0
    disputed = 0
    for m in created_markets:
        status = m.get("status")
        status_str = status.value if hasattr(status, 'value') else str(status)
        if status_str.upper() == "RESOLVED":
            resolved_cleanly += 1
        # Note: disputed status isn't tracked separately in v1,
        # but we leave the field for future use
    
    avg_volume = total_volume / markets_created
    avg_bets = total_bets / markets_created
    
    # Score: combination of volume quality and bet attraction
    volume_component = _ratio_score(avg_volume, GOOD_VOLUME_PER_MARKET)
    bets_component = _ratio_score(avg_bets, GOOD_BETS_PER_MARKET)
    
    # 50% volume + 40% bets + 10% creation count bonus
    count_bonus = min(100.0, 50.0 + markets_created * 10.0)
    score = 0.50 * volume_component + 0.40 * bets_component + 0.10 * count_bonus
    
    return CreationScore(
        score=round(score, 1),
        markets_created=markets_created,
        total_volume_attracted=round(total_volume, 2),
        total_bets_attracted=total_bets,
        avg_volume_per_market=round(avg_volume, 2),
        avg_bets_per_market=round(avg_bets, 2),
        resolved_cleanly=resolved_cleanly,
        disputed=disputed,
    )


def compute_participation_score(
    user: dict,
    user_bets: List[dict],
    markets: Dict[str, dict],
    comments_count: int = 0,
) -> ParticipationScore:
    """Compute activity and engagement dimension."""
    total_bets = len(user_bets)
    markets_traded_in = len({b.get("market_id") for b in user_bets})
    markets_created = int(user.get("markets_created", 0))
    
    # Score: based on breadth of activity
    bet_component = _ratio_score(total_bets / ACTIVE_TRADER_BETS if ACTIVE_TRADER_BETS > 0 else 0)
    market_component = _ratio_score(markets_traded_in / 5.0)  # 5 markets = good diversity
    creation_component = _ratio_score(markets_created / ACTIVE_CREATOR_MARKETS if ACTIVE_CREATOR_MARKETS > 0 else 0)
    comment_component = _ratio_score(comments_count / 10.0)  # 10 comments = good engagement
    
    score = 0.35 * bet_component + 0.25 * market_component + 0.20 * creation_component + 0.20 * comment_component
    
    return ParticipationScore(
        score=round(score, 1),
        total_bets=total_bets,
        markets_traded_in=markets_traded_in,
        markets_created=markets_created,
        comments_count=comments_count,
    )


# =============================================================================
# Main Computation
# =============================================================================

def compute_reputation(
    user: dict,
    user_bets: List[dict],
    markets: Dict[str, dict],
    all_bets: List[dict],
    resolution_votes: List[dict],
    comments_count: int = 0,
) -> AgentReputation:
    """
    Compute the full multi-dimensional reputation for an agent.
    
    All data is passed in — this function is a pure calculation
    with no database access, making it easy to test.
    
    Args:
        user: The agent's user record
        user_bets: All bets placed by this agent
        markets: Dict of all markets (market_id -> market)
        all_bets: All bets in the system (for market creation quality)
        resolution_votes: All resolution votes (for accuracy scoring)
        comments_count: Number of comments by this agent
        
    Returns:
        AgentReputation with all dimensions and overall score
    """
    trading = compute_trading_score(user, user_bets, markets)
    resolution = compute_resolution_score(user["id"], resolution_votes, markets)
    creation = compute_creation_score(user, markets, all_bets)
    participation = compute_participation_score(user, user_bets, markets, comments_count)
    
    # Weighted composite
    overall = (
        WEIGHTS["trading"] * trading.score +
        WEIGHTS["resolution"] * resolution.score +
        WEIGHTS["creation"] * creation.score +
        WEIGHTS["participation"] * participation.score
    )
    
    return AgentReputation(
        agent_id=user["id"],
        username=user.get("username", "unknown"),
        overall_score=round(overall, 1),
        tier=get_tier(overall),
        trading=trading,
        resolution=resolution,
        creation=creation,
        participation=participation,
    )
