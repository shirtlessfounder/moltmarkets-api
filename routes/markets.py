"""
Market endpoints — CRUD, detail, history.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from auth import require_auth
from cpmm import get_cpmm_probability
from deps import (
    get_db, validate_uuid, clamp_pagination, maybe_transition_market,
    bg_transition_expired_markets,
    set_cache_headers,
    MARKET_CREATION_COST,
    CABAL_USERNAMES, CABAL_COOLDOWN_MINUTES, DEFAULT_COOLDOWN_MINUTES,
    MAX_MARKET_DURATION_SECONDS, CURRENCY_SYMBOL,
)
from errors import error_response, ErrorCode
from event_bus import event_bus, SSEEvent
from market_cache import market_cache
from models import (
    MarketCreate, MarketSummary, MarketDetail, MarketCreated,
    ProbabilityPoint, MarketHistory,
    CommitteeVoteDetail, CommitteeOutcome,
    MarketStatus,
    PaginationMeta, PaginatedMarketSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["markets"])


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
    bg: BackgroundTasks = BackgroundTasks(),
):
    """List markets, filtered by status, with pagination."""
    db = get_db()
    limit, offset = clamp_pagination(limit, offset)
    status_filter = (status or "active").strip().upper()

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
    bg.add_task(bg_transition_expired_markets)

    now = datetime.now(timezone.utc)
    transitioned = False
    for m in markets:
        if m["status"] == MarketStatus.OPEN and m["closes_at"] <= now:
            m["status"] = MarketStatus.RESOLVING
            transitioned = True

    if transitioned:
        market_cache.invalidate()

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

    result = []
    for m in page:
        result.append(MarketSummary(
            id=m["id"],
            title=m["title"],
            probability=get_cpmm_probability(m["pool"], m["p"]),
            status=m["status"],
            closes_at=m["closes_at"],
            total_volume=m["total_volume"],
            creator_id=m["creator_id"],
            creator_username=m.get("creator_username"),
        ))

    paginated = PaginatedMarketSummary(
        data=result,
        pagination=PaginationMeta(limit=limit, offset=offset, total=total),
    )
    entry = market_cache.set(status_filter, paginated)
    set_cache_headers(response, entry["etag"], market_cache.last_modified)
    return paginated


def _build_market_detail(market: dict, creator_username: str = None) -> MarketDetail:
    """Build a MarketDetail response from a market dict, including committee fields."""
    db = get_db()
    # Build committee vote details if committee exists
    committee_votes_detail = None
    if market.get("committee"):
        raw_votes = db.get_committee_votes(market["id"])
        if raw_votes:
            committee_votes_detail = [
                CommitteeVoteDetail(
                    agent_id=v["agent_id"],
                    outcome=CommitteeOutcome(v["outcome"]),
                    timestamp=v["created_at"],
                )
                for v in raw_votes
            ]

    return MarketDetail(
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
        creator_username=creator_username,
        pool=market["pool"],
        p=market["p"],
        committee=market.get("committee"),
        resolution_votes=committee_votes_detail,
        resolution_deadline=market.get("resolution_deadline"),
    )


@router.get("/markets/{market_id}", response_model=MarketDetail)
async def get_market(market_id: str):
    """Get market details including current probability."""
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    maybe_transition_market(market, market_id)

    creator = db.get_user(market["creator_id"]) if market["creator_id"] else None
    creator_username = creator["username"] if creator else None

    return _build_market_detail(market, creator_username)


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

    if user["balance"] < MARKET_CREATION_COST:
        return error_response(400,
            f"Insufficient balance. Market creation costs {MARKET_CREATION_COST}{CURRENCY_SYMBOL}.",
            ErrorCode.INSUFFICIENT_BALANCE,
            detail={"balance": user["balance"], "required": MARKET_CREATION_COST})

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

    db.update_user_balance(user["id"], -MARKET_CREATION_COST)

    market_id = str(uuid.uuid4())
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
        creation_cost=MARKET_CREATION_COST,
        tip=tip,
        warning=warning,
    )


# =============================================================================
# History
# =============================================================================

@router.get("/markets/{market_id}/history", response_model=MarketHistory)
async def get_market_history(market_id: str):
    """Get probability history for charts."""
    db = get_db()
    validate_uuid(market_id, "market_id")
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

    return MarketHistory(market_id=market_id, points=points)
