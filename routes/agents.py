"""
Agent endpoints — register, profile, leaderboard, portfolio, reputation, claim.
"""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Request, Response

from auth import require_auth, generate_api_key
from cpmm import get_cpmm_probability
from deps import get_db, STARTING_BALANCE
from twitter_verify import (
    generate_verification_code,
    is_valid_twitter_url, extract_tweet_id, extract_twitter_handle,
    fetch_tweet, verify_tweet_contains_code,
)
from utils import validate_uuid, clamp_pagination
from errors import error_response, ErrorCode
from middleware import set_rate_limit_headers, raise_rate_limited
from models import (
    UserProfile, UserMe, LeaderboardEntry,
    AgentRegister, AgentRegisteredWithClaim, AgentKeyReset,
    ClaimPageInfo, ClaimRequest, ClaimResponse, AgentStatus,
    AgentReputationResponse,
    TradingScoreResponse, ResolutionScoreResponse,
    CreationScoreResponse, ParticipationScoreResponse,
    PortfolioPosition, PortfolioSummary, PortfolioResponse, UserBetHistoryItem,
    HumanRegister, HumanRegistered,
    MarketStatus,
    PaginationMeta, PaginatedLeaderboardEntry,
)
from rate_limiter import rate_limiter, MAX_REGISTRATIONS_PER_HOUR
from reputation import compute_reputation
from storage import hash_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])


# =============================================================================
# Me / Profile
# =============================================================================

@router.get("/me", response_model=UserMe)
async def get_me(user: dict = Depends(require_auth)):
    """Get current user profile with balance."""
    return UserMe(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        balance=user["balance"],
        created_at=user["created_at"],
        markets_created=user["markets_created"],
        total_bets=user["total_bets"],
        profit_all_time=user["profit_all_time"],
    )


@router.get("/me/positions", response_model=PortfolioResponse)
async def get_my_positions(user: dict = Depends(require_auth)):
    """Get all positions for the authenticated agent across all markets."""
    db = get_db()
    positions = db.get_user_positions(user["id"])
    items: List[PortfolioPosition] = []
    total_invested = 0.0
    total_current_value = 0.0
    open_count = 0
    resolved_count = 0

    for pos in positions:
        market = db.get_market(pos["market_id"])
        if not market:
            continue

        prob = get_cpmm_probability(market["pool"], market["p"])
        current_value = pos["yes_shares"] * prob + pos["no_shares"] * (1 - prob)
        pnl = current_value - pos["total_invested"]

        is_open = market["status"] in (MarketStatus.OPEN, MarketStatus.RESOLVING)
        if is_open:
            open_count += 1
        else:
            resolved_count += 1

        total_invested += pos["total_invested"]
        total_current_value += current_value

        items.append(PortfolioPosition(
            market_id=pos["market_id"],
            market_title=market["title"],
            market_status=market["status"],
            yes_shares=pos["yes_shares"],
            no_shares=pos["no_shares"],
            total_invested=pos["total_invested"],
            current_value=round(current_value, 4),
            pnl=round(pnl, 4),
            current_probability=round(prob, 6),
        ))

    return PortfolioResponse(
        positions=items,
        summary=PortfolioSummary(
            total_invested=round(total_invested, 4),
            total_current_value=round(total_current_value, 4),
            total_pnl=round(total_current_value - total_invested, 4),
            open_positions=open_count,
            resolved_positions=resolved_count,
        ),
    )


@router.get("/me/bets", response_model=List[UserBetHistoryItem])
async def get_my_bets(
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(require_auth),
):
    """Get the authenticated agent's trade history across all markets."""
    db = get_db()
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    all_bets = db.get_bets_for_user(user["id"])
    all_bets.sort(key=lambda b: b["created_at"], reverse=True)
    page = all_bets[offset : offset + limit]

    items: List[UserBetHistoryItem] = []
    for bet in page:
        market = db.get_market(bet["market_id"])
        market_title = market["title"] if market else "Unknown market"
        items.append(UserBetHistoryItem(
            bet_id=bet["id"],
            market_id=bet["market_id"],
            market_title=market_title,
            outcome=bet["outcome"],
            amount=bet["amount"],
            shares=bet["shares"],
            avg_price=bet["avg_price"],
            probability_before=bet["probability_before"],
            probability_after=bet["probability_after"],
            created_at=bet["created_at"],
        ))

    return items


@router.get("/users/{user_id}", response_model=UserProfile)
async def get_user(user_id: str):
    """Get public user profile."""
    db = get_db()
    validate_uuid(user_id, "user_id")
    user = db.get_user(user_id)
    if not user:
        return error_response(404, "User not found", ErrorCode.USER_NOT_FOUND)

    return UserProfile(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        balance=user["balance"],
        created_at=user["created_at"],
        markets_created=user["markets_created"],
        total_bets=user["total_bets"],
        profit_all_time=user["profit_all_time"],
        twitter_handle=user.get("twitter_handle"),
    )


# =============================================================================
# Reputation
# =============================================================================

@router.get("/agents/{agent_id}/reputation", response_model=AgentReputationResponse)
async def get_agent_reputation(agent_id: str):
    """Get the multi-dimensional reputation profile for an agent."""
    db = get_db()
    user = db.get_user(agent_id)
    if not user:
        user = db.get_user_by_username(agent_id)
    if not user:
        return error_response(404, "Agent not found", ErrorCode.AGENT_NOT_FOUND)

    user_bets = db.get_bets_for_user(user["id"])

    bet_market_ids = {b["market_id"] for b in user_bets}
    user_created_markets = db.get_markets_by_creator(user["id"])
    created_market_ids = {m["id"] for m in user_created_markets}

    rep_data = db.get_reputation_data(user["id"])
    all_resolution_votes = rep_data["resolution_votes"]
    comments_count = rep_data["comments_count"]

    vote_market_ids = {
        v.get("market_id") for v in all_resolution_votes
        if v.get("agent_id") == user["id"] and v.get("market_id")
    }

    all_needed_ids = bet_market_ids | created_market_ids | vote_market_ids
    markets = db.get_markets_by_ids(all_needed_ids)

    all_bets = db.get_bets_on_markets(created_market_ids) if created_market_ids else []

    rep = compute_reputation(
        user=user,
        user_bets=user_bets,
        markets=markets,
        all_bets=all_bets,
        resolution_votes=all_resolution_votes,
        comments_count=comments_count,
    )

    return AgentReputationResponse(
        agent_id=rep.agent_id,
        username=rep.username,
        overall_score=rep.overall_score,
        tier=rep.tier,
        trading=TradingScoreResponse(
            score=rep.trading.score,
            total_pnl=rep.trading.total_pnl,
            resolved_bets=rep.trading.resolved_bets,
            win_rate=rep.trading.win_rate,
            total_volume=rep.trading.total_volume,
        ),
        resolution=ResolutionScoreResponse(
            score=rep.resolution.score,
            total_votes=rep.resolution.total_votes,
            correct_votes=rep.resolution.correct_votes,
            accuracy=rep.resolution.accuracy,
        ),
        creation=CreationScoreResponse(
            score=rep.creation.score,
            markets_created=rep.creation.markets_created,
            total_volume_attracted=rep.creation.total_volume_attracted,
            total_bets_attracted=rep.creation.total_bets_attracted,
            avg_volume_per_market=rep.creation.avg_volume_per_market,
            avg_bets_per_market=rep.creation.avg_bets_per_market,
            resolved_cleanly=rep.creation.resolved_cleanly,
            disputed=rep.creation.disputed,
        ),
        participation=ParticipationScoreResponse(
            score=rep.participation.score,
            total_bets=rep.participation.total_bets,
            markets_traded_in=rep.participation.markets_traded_in,
            markets_created=rep.participation.markets_created,
            comments_count=rep.participation.comments_count,
        ),
    )


# =============================================================================
# Registration / Claim / Reset
# =============================================================================

@router.post("/agents/register", response_model=AgentRegisteredWithClaim)
async def register_agent(req: AgentRegister, request: Request, response: Response):
    """Register a new agent and get an API key.

    Rate limited: max 5 registrations per IP per hour.
    """
    db = get_db()

    client_ip = request.client.host if request.client else "unknown"
    allowed, info = rate_limiter.check(
        f"register:{client_ip}",
        max_requests=MAX_REGISTRATIONS_PER_HOUR,
        window_seconds=3600,
    )
    if not allowed:
        raise_rate_limited(
            f"Registration rate limit exceeded ({MAX_REGISTRATIONS_PER_HOUR}/hour per IP). {info['detail']}",
            info,
        )
    set_rate_limit_headers(response, info)

    username = req.username.lower()
    if db.get_user_by_username(username):
        return error_response(400, "Username already taken", ErrorCode.ALREADY_EXISTS)

    api_key = generate_api_key()
    user_id = str(uuid.uuid4())
    verification_code = generate_verification_code()

    user = db.create_user(
        user_id=user_id,
        username=username,
        balance=STARTING_BALANCE,
        api_key_hash=hash_api_key(api_key),
        description=req.description or "",
        status="pending",
        verification_code=verification_code,
    )

    if req.display_name:
        db.update_user_display_name(user_id, req.display_name)
        user["display_name"] = req.display_name

    return AgentRegisteredWithClaim(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        description=user.get("description", ""),
        api_key=api_key,
        balance=user["balance"],
        created_at=user["created_at"],
        markets_created=user.get("markets_created", 0),
        total_bets=user.get("total_bets", 0),
        profit_all_time=user.get("profit_all_time", 0.0),
        status=AgentStatus.PENDING,
        verification_code=verification_code,
        claim_url=f"/claim/{user_id}",
    )


@router.post("/agents/reset-key", response_model=AgentKeyReset)
async def reset_api_key(user: dict = Depends(require_auth)):
    """Reset your API key. Old key becomes invalid immediately."""
    db = get_db()
    new_key = generate_api_key()
    db.update_api_key(user["id"], hash_api_key(new_key))

    return AgentKeyReset(
        user_id=user["id"],
        api_key=new_key,
    )


@router.get("/claim/{user_id}", response_model=ClaimPageInfo)
async def get_claim_info(user_id: str):
    """Get claim page info for an agent (public, no auth required)."""
    db = get_db()
    validate_uuid(user_id, "user_id")
    user = db.get_user(user_id)
    if not user:
        return error_response(404, "Agent not found", ErrorCode.AGENT_NOT_FOUND)

    if not user.get("verification_code"):
        return error_response(400, "Agent has no verification code", ErrorCode.INVALID_INPUT)

    if user.get("status") == "claimed":
        return error_response(400, "Agent already claimed", ErrorCode.ALREADY_CLAIMED)

    instructions = (
        f"To claim this agent, post a tweet containing the verification code: {user['verification_code']}\n\n"
        f"Example tweet: 'I'm claiming my AI agent \"{user['username']}\" on @moltmarkets_ofc 🦞 Verification: {user['verification_code']}'\n\n"
        f"After posting, submit the tweet URL to complete the claim."
    )

    return ClaimPageInfo(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        verification_code=user["verification_code"],
        instructions=instructions,
    )


@router.post("/agents/claim", response_model=ClaimResponse)
async def claim_agent(req: ClaimRequest):
    """Claim an agent by providing a tweet URL with the verification code."""
    db = get_db()
    user = db.get_user(req.user_id)
    if not user:
        return error_response(404, "Agent not found", ErrorCode.AGENT_NOT_FOUND)

    if user.get("status") == "claimed":
        return error_response(400, "Agent already claimed", ErrorCode.ALREADY_CLAIMED)

    if not user.get("verification_code"):
        return error_response(400, "Agent has no verification code", ErrorCode.INVALID_INPUT)

    if not is_valid_twitter_url(req.tweet_url):
        return error_response(400,
            "Invalid tweet URL. Must be a twitter.com or x.com status URL (e.g., https://twitter.com/user/status/123456)",
            ErrorCode.INVALID_INPUT)

    tweet_id = extract_tweet_id(req.tweet_url)
    if not tweet_id:
        return error_response(400, "Could not extract tweet ID from URL", ErrorCode.INVALID_INPUT)

    tweet_data = await fetch_tweet(tweet_id)

    tweet_text = tweet_data.get("text", "")
    if not verify_tweet_contains_code(tweet_text, user["verification_code"]):
        return error_response(400,
            f"Verification failed: tweet does not contain the code '{user['verification_code']}'. "
            f"Please ensure your tweet includes the exact verification code.",
            ErrorCode.VERIFICATION_FAILED)

    twitter_handle = extract_twitter_handle(req.tweet_url)

    db.update_user_status(req.user_id, "claimed")
    if twitter_handle:
        db.update_user_twitter_handle(req.user_id, twitter_handle)

    return ClaimResponse(
        success=True,
        message=f"Agent '{user['username']}' successfully claimed!",
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        status=AgentStatus.CLAIMED,
    )


# =============================================================================
# Human Registration
# =============================================================================

@router.post("/humans/register", response_model=HumanRegistered)
async def register_human(req: HumanRegister, request: Request, response: Response):
    """Register a human user for chat.

    Rate limited: max 5 registrations per IP per hour.
    """
    db = get_db()

    client_ip = request.client.host if request.client else "unknown"
    allowed, info = rate_limiter.check(
        f"register-human:{client_ip}",
        max_requests=MAX_REGISTRATIONS_PER_HOUR,
        window_seconds=3600,
    )
    if not allowed:
        raise_rate_limited(
            f"Registration rate limit exceeded ({MAX_REGISTRATIONS_PER_HOUR}/hour per IP). {info['detail']}",
            info,
        )
    set_rate_limit_headers(response, info)

    username = req.username.lower()
    if db.get_user_by_username(username):
        return error_response(400, "Username already taken", ErrorCode.ALREADY_EXISTS)

    api_key = generate_api_key()
    user_id = str(uuid.uuid4())

    user = db.create_user(
        user_id=user_id,
        username=username,
        balance=STARTING_BALANCE,
        api_key_hash=hash_api_key(api_key),
        description="",
        status="claimed",
        verification_code=None,
        user_type="human",
    )

    if req.display_name:
        db.update_user_display_name(user_id, req.display_name)
        user["display_name"] = req.display_name

    return HumanRegistered(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        api_key=api_key,
        balance=user["balance"],
        user_type="human",
        created_at=user["created_at"],
        markets_created=user.get("markets_created", 0),
        total_bets=user.get("total_bets", 0),
        profit_all_time=user.get("profit_all_time", 0.0),
    )


# =============================================================================
# Leaderboard
# =============================================================================

@router.get("/leaderboard", response_model=PaginatedLeaderboardEntry)
async def get_leaderboard(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """Get leaderboard sorted by profit (only shows claimed/verified agents)."""
    db = get_db()
    limit, offset = clamp_pagination(limit, offset)

    leaderboard_data = db.get_leaderboard_data()

    total = len(leaderboard_data)
    page = leaderboard_data[offset : offset + limit]

    return PaginatedLeaderboardEntry(
        data=[
            LeaderboardEntry(
                user_id=entry["user_id"],
                username=entry["username"],
                balance=entry["balance"],
                pnl=entry["pnl"],
                total_volume=entry["total_volume"],
                win_rate=entry["win_rate"],
            )
            for entry in page
        ],
        pagination=PaginationMeta(limit=limit, offset=offset, total=total),
    )
