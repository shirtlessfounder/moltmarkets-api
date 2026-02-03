"""
Market endpoints — CRUD, detail, history.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response

from auth import require_auth
from cpmm import get_cpmm_probability
from deps import (
    get_db,
    MIN_CREATION_LIQUIDITY,
    CABAL_USERNAMES, CABAL_COOLDOWN_MINUTES, DEFAULT_COOLDOWN_MINUTES,
    MAX_MARKET_DURATION_SECONDS, CURRENCY_SYMBOL,
)
from utils import validate_uuid, clamp_pagination, set_cache_headers
from errors import error_response, ErrorCode
from event_bus import event_bus, SSEEvent
from market_cache import market_cache
from history_cache import history_cache
from models import (
    MarketCreate, MarketSummary, MarketDetail, MarketCreated,
    ProbabilityPoint, MarketHistory, SparklinePoint,
    CommitteeVoteDetail, CommitteeOutcome, CommitteeMemberStatus,
    MarketStatus, ResolutionStage,
    PaginationMeta, PaginatedMarketSummary,
    BetHistoryItem, Comment,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["markets"])


def _compute_resolution_stage(market: dict, preloaded_votes: list = None) -> tuple:
    """Compute resolution stage, committee_size, and votes_cast for a RESOLVING market.

    Args:
        market: Market dict from database
        preloaded_votes: Optional preloaded committee votes to avoid duplicate query.
                        If None, will fetch from DB.

    Returns (resolution_stage, committee_size, votes_cast).
    Returns (None, None, None) if market is not RESOLVING.
    """
    if market.get("status") != MarketStatus.RESOLVING:
        return None, None, None

    committee = market.get("committee") or []
    committee_size = len(committee) if committee else None

    if not committee or len(committee) <= 1:
        # Solo creator — no committee or just the creator
        return ResolutionStage.CREATOR_PENDING, committee_size, 0

    # Committee exists — check votes (use preloaded if available)
    if preloaded_votes is not None:
        raw_votes = preloaded_votes
    else:
        db = get_db()
        raw_votes = db.get_committee_votes(market["id"])
    votes_cast = len(raw_votes) if raw_votes else 0

    resolution_deadline = market.get("resolution_deadline")
    now = datetime.now(timezone.utc)
    deadline_expired = resolution_deadline and now > resolution_deadline

    if deadline_expired:
        # Deadline passed — creator can resolve unilaterally
        return ResolutionStage.COMMITTEE_COMPLETE, committee_size, votes_cast

    if votes_cast < len(committee):
        # Still waiting for votes
        return ResolutionStage.COMMITTEE_VOTING, committee_size, votes_cast

    # All votes in but no unanimity (otherwise market would be RESOLVED)
    return ResolutionStage.COMMITTEE_COMPLETE, committee_size, votes_cast


# =============================================================================
# List / Detail
# =============================================================================

@router.get("/markets", response_model=PaginatedMarketSummary)
async def list_markets(
    request: Request,
    response: Response,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    include: Optional[str] = None,
):
    """List markets, filtered by status, with pagination.
    
    Query params:
        status: Filter by market status (active, resolved, all)
        limit: Max results per page
        offset: Pagination offset
        include: Comma-separated list of extra data to include.
                 Supported: "sparkline" - includes last 20 price points per market
    """
    db = get_db()
    limit, offset = clamp_pagination(limit, offset)
    status_filter = (status or "active").strip().upper()
    include_sparkline = include and "sparkline" in include.lower()

    # Skip cache when sparklines requested (they need fresh batch query)
    if not include_sparkline:
        client_etag = request.headers.get("if-none-match")
        cached = market_cache.get(status_filter)
        if cached and client_etag and client_etag == cached["etag"]:
            return Response(
                status_code=304,
                headers={
                    "ETag": cached["etag"],
                    "Last-Modified": market_cache.last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT"),
                    "Cache-Control": "public, max-age=5",
                },
            )

        if cached:
            set_cache_headers(response, cached["etag"], market_cache.last_modified)
            return cached["data"]

    markets = db.list_markets_with_creators()

    # Markets no longer auto-transition based on closes_at (issue #115).
    # They remain OPEN until explicitly resolved.

    if status_filter == "ALL":
        pass
    elif status_filter in ("CLOSED", "RESOLVED"):
        markets = [m for m in markets if m["status"] == MarketStatus.RESOLVED]
    elif status_filter == "RESOLVING":
        markets = [m for m in markets if m["status"] == MarketStatus.RESOLVING]
    else:
        markets = [m for m in markets if m["status"] == MarketStatus.OPEN]

    total = len(markets)
    page = markets[offset : offset + limit]

    # Batch-fetch sparklines if requested (one query for all markets)
    sparklines_by_market = {}
    if include_sparkline and page:
        market_ids = [m["id"] for m in page]
        sparklines_by_market = db.get_sparklines_batch(market_ids, limit=20)

    result = []
    for m in page:
        resolution_stage, committee_size, votes_cast = _compute_resolution_stage(m)
        sparkline_data = None
        if include_sparkline:
            raw_points = sparklines_by_market.get(m["id"], [])
            if raw_points:
                sparkline_data = [
                    SparklinePoint(timestamp=p["timestamp"], probability=p["probability"])
                    for p in raw_points
                ]
        
        result.append(MarketSummary(
            id=m["id"],
            title=m["title"],
            probability=get_cpmm_probability(m["pool"], m["p"]),
            status=m["status"],
            closes_at=m["closes_at"],
            total_volume=m["total_volume"],
            creator_id=m["creator_id"],
            creator_username=m.get("creator_username"),
            last_traded_at=m.get("last_traded_at"),
            resolution_stage=resolution_stage,
            committee_size=committee_size,
            votes_cast=votes_cast,
            resolution_deadline=m.get("resolution_deadline"),
            sparkline=sparkline_data,
        ))

    paginated = PaginatedMarketSummary(
        data=result,
        pagination=PaginationMeta(limit=limit, offset=offset, total=total),
    )
    
    # Only cache non-sparkline requests (sparklines would bloat cache)
    if not include_sparkline:
        entry = market_cache.set(status_filter, paginated)
        set_cache_headers(response, entry["etag"], market_cache.last_modified)
    
    return paginated


def _build_market_detail(market: dict, creator_username: str = None, include: str = None) -> MarketDetail:
    """Build a MarketDetail response from a market dict, including committee fields.
    
    Args:
        market: Market dict from database
        creator_username: Optional creator username
        include: Comma-separated list of extra data to include (history, bets, comments)
    """
    db = get_db()
    market_id = market["id"]
    
    # Build committee vote details if committee exists
    # Fetch once and reuse for resolution_stage computation (fixes duplicate query)
    committee_votes_detail = None
    committee_members_status = None
    raw_votes = []
    committee_ids = market.get("committee") or []
    
    if committee_ids:
        raw_votes = db.get_committee_votes(market_id)
        
        # Build a map of agent_id -> vote for quick lookup
        votes_by_agent = {v["agent_id"]: v for v in raw_votes}
        
        # Batch fetch all committee member usernames
        committee_users = db.get_users_by_ids(set(committee_ids))
        
        # Build vote details with usernames
        if raw_votes:
            committee_votes_detail = [
                CommitteeVoteDetail(
                    agent_id=v["agent_id"],
                    agent_username=committee_users.get(v["agent_id"], {}).get("username", "unknown"),
                    outcome=CommitteeOutcome(v["outcome"]),
                    timestamp=v["created_at"],
                )
                for v in raw_votes
            ]
        
        # Build full committee member status list
        committee_members_status = []
        for agent_id in committee_ids:
            user = committee_users.get(agent_id, {})
            vote = votes_by_agent.get(agent_id)
            committee_members_status.append(CommitteeMemberStatus(
                agent_id=agent_id,
                username=user.get("username", "unknown"),
                voted=vote is not None,
                outcome=CommitteeOutcome(vote["outcome"]) if vote else None,
            ))

    # Pass preloaded votes to avoid duplicate DB query
    resolution_stage, committee_size, votes_cast = _compute_resolution_stage(market, preloaded_votes=raw_votes)
    
    # Parse include param
    include_set = set(include.lower().split(",")) if include else set()
    
    # Fetch optional bundled data
    history_points = None
    bets_list = None
    comments_list = None
    
    if "history" in include_set:
        # Build price history (same logic as /history endpoint)
        market_bets = sorted(
            db.get_bets_for_market(market_id),
            key=lambda x: x["created_at"],
        )
        history_points = [ProbabilityPoint(
            timestamp=market["created_at"],
            probability=0.5,
            volume=0.0,
        )]
        cumulative_volume = 0.0
        for bet in market_bets:
            cumulative_volume += bet["amount"]
            history_points.append(ProbabilityPoint(
                timestamp=bet["created_at"],
                probability=bet["probability_after"],
                volume=cumulative_volume,
            ))
    
    if "bets" in include_set:
        # Build trade history (same logic as /bets endpoint)
        raw_bets = db.get_bets_for_market(market_id)
        bets_list = []
        for bet in sorted(raw_bets, key=lambda x: x["created_at"], reverse=True):
            user = db.get_user(bet["user_id"])
            bets_list.append(BetHistoryItem(
                bet_id=bet["id"],
                user_id=bet["user_id"],
                username=user["username"] if user else "unknown",
                outcome=bet["outcome"],
                amount=bet["amount"],
                shares=bet["shares"],
                probability_after=bet["probability_after"],
                created_at=bet["created_at"],
            ))
    
    if "comments" in include_set:
        # Build comments (same logic as /comments endpoint)
        raw_comments = db.get_market_comments(market_id)
        comments_list = [
            Comment(
                id=c["id"],
                market_id=c["market_id"],
                user_id=c["user_id"],
                username=c.get("username", "unknown"),
                content=c["content"],
                created_at=c["created_at"],
                parent_id=c.get("parent_id"),
            )
            for c in raw_comments
        ]

    return MarketDetail(
        id=market_id,
        title=market["title"],
        description=market["description"],
        probability=get_cpmm_probability(market["pool"], market["p"]),
        status=market["status"],
        closes_at=market["closes_at"],
        created_at=market["created_at"],
        resolved_at=market["resolved_at"],
        resolution=market["resolution"],
        total_volume=market["total_volume"],
        creator_id=market["creator_id"],
        creator_username=creator_username,
        pool=market["pool"],
        p=market["p"],
        last_traded_at=market.get("last_traded_at"),
        committee=market.get("committee"),
        resolution_votes=committee_votes_detail,
        committee_members=committee_members_status,
        resolution_deadline=market.get("resolution_deadline"),
        resolution_stage=resolution_stage,
        committee_size=committee_size,
        votes_cast=votes_cast,
        history=history_points,
        bets=bets_list,
        comments=comments_list,
    )


@router.get("/markets/{market_id}", response_model=MarketDetail)
async def get_market(market_id: str, include: Optional[str] = None):
    """Get market details including current probability.
    
    Query params:
        include: Comma-separated list of extra data to bundle.
                 Supported: "history" (price points), "bets" (trade history), "comments"
                 Example: ?include=history,bets,comments
    """
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    creator = db.get_user(market["creator_id"]) if market["creator_id"] else None
    creator_username = creator["username"] if creator else None

    return _build_market_detail(market, creator_username, include=include)


# =============================================================================
# Create
# =============================================================================

@router.post("/markets", response_model=MarketCreated)
async def create_market(req: MarketCreate, user: dict = Depends(require_auth)):
    """Create a new prediction market."""
    db = get_db()

    if user.get("status") != "claimed":
        return error_response(403,
            "Twitter verification required before creating markets. Visit /claim/{user_id} to link your Twitter account.",
            ErrorCode.CLAIM_REQUIRED)

    now = datetime.now(timezone.utc)

    if req.closes_at <= now:
        return error_response(400, "closes_at must be in the future", ErrorCode.INVALID_INPUT)

    max_close = now + timedelta(seconds=MAX_MARKET_DURATION_SECONDS)
    if req.closes_at > max_close:
        return error_response(422,
            "Market duration cannot exceed 1 hour during testing phase",
            ErrorCode.MARKET_DURATION_EXCEEDED)

    # Creation cost = initial liquidity (the full amount seeds the pool
    # and is recoverable at resolution via the winning-pool residual).
    # Previously MARKET_CREATION_COST was a flat 100ŧ while initial_liquidity
    # defaulted to 50ŧ — the 50ŧ gap was silently burned.  See issue #170.
    creation_cost = req.initial_liquidity

    if user["balance"] < creation_cost:
        return error_response(400,
            f"Insufficient balance. Market creation costs {creation_cost}{CURRENCY_SYMBOL} "
            f"(= initial liquidity). Minimum: {MIN_CREATION_LIQUIDITY}{CURRENCY_SYMBOL}.",
            ErrorCode.INSUFFICIENT_BALANCE,
            detail={"balance": user["balance"], "required": creation_cost})

    username = user.get("username", "").lower()
    cooldown_minutes = CABAL_COOLDOWN_MINUTES if username in CABAL_USERNAMES else DEFAULT_COOLDOWN_MINUTES
    last_created = user.get("last_market_created_at")
    if last_created:
        if isinstance(last_created, str):
            last_created = datetime.fromisoformat(last_created.replace("Z", "+00:00"))
        cooldown_end = last_created + timedelta(minutes=cooldown_minutes)
        if now < cooldown_end:
            remaining = (cooldown_end - now).total_seconds() / 60
            return error_response(429,
                f"Rate limit: you can create another market in {remaining:.0f} minutes",
                ErrorCode.RATE_LIMITED,
                detail={"retry_after_minutes": round(remaining, 1)})

    market_id = str(uuid.uuid4())

    db.update_user_balance(
        user["id"], -creation_cost,
        tx_type="market_creation", market_id=market_id,
    )
    market = db.create_market(
        market_id=market_id,
        creator_id=user["id"],
        title=req.title,
        description=req.description,
        closes_at=req.closes_at,
        initial_liquidity=req.initial_liquidity,
    )

    db.update_user_last_market_created(user["id"])
    market_cache.invalidate()

    now = datetime.now(timezone.utc)
    duration_days = (req.closes_at - now).total_seconds() / 86400

    tip = None
    warning = None
    if duration_days <= 7:
        tip = "Nice! Short markets (under 7 days) typically see 2-3x more trading activity."
    elif duration_days > 14:
        warning = "Heads up: markets over 2 weeks often see lower engagement. Consider shorter timeframes for more action."

    await event_bus.publish(SSEEvent(
        event="market_created",
        data={
            "market_id": market["id"],
            "title": market["title"],
            "creator_username": user["username"],
            "probability": get_cpmm_probability(market["pool"], market["p"]),
            "closes_at": str(market["closes_at"]),
        },
        market_id=market["id"],
    ))

    return MarketCreated(
        id=market["id"],
        title=market["title"],
        description=market["description"],
        probability=get_cpmm_probability(market["pool"], market["p"]),
        status=market["status"],
        closes_at=market["closes_at"],
        created_at=market["created_at"],
        resolved_at=market["resolved_at"],
        resolution=market["resolution"],
        total_volume=market["total_volume"],
        creator_id=market["creator_id"],
        creator_username=user["username"],
        pool=market["pool"],
        p=market["p"],
        creation_cost=creation_cost,
        tip=tip,
        warning=warning,
    )


# =============================================================================
# History
# =============================================================================

@router.get("/markets/{market_id}/history", response_model=MarketHistory)
async def get_market_history(market_id: str):
    """Get probability history for charts.
    
    Cached with invalidation on new bets — history is append-only so
    cached data is always valid until a new bet is placed.
    """
    validate_uuid(market_id, "market_id")
    
    # Check cache first
    cached = history_cache.get("history", market_id)
    if cached:
        return cached
    
    db = get_db()
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    market_bets = sorted(
        db.get_bets_for_market(market_id),
        key=lambda x: x["created_at"],
    )

    points = [ProbabilityPoint(
        timestamp=market["created_at"],
        probability=0.5,
        volume=0.0,
    )]

    cumulative_volume = 0.0
    for bet in market_bets:
        cumulative_volume += bet["amount"]
        points.append(ProbabilityPoint(
            timestamp=bet["created_at"],
            probability=bet["probability_after"],
            volume=cumulative_volume,
        ))

    result = MarketHistory(market_id=market_id, points=points)
    
    # Cache for next request
    history_cache.set("history", market_id, result)
    
    return result
