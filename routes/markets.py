"""Market CRUD, comments, resolution committee, and history endpoints."""

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Request, Response

from errors import error_response, APIError, ErrorCode
from cpmm import get_cpmm_probability
from models import (
    MarketCreate, MarketResolve, MarketSummary, MarketDetail, MarketCreated,
    ProbabilityPoint, MarketHistory,
    CommentCreate, Comment, MarketComments,
    ResolutionResult, ResolutionVote,
    MarketStatus, Outcome,
)
from market_cache import market_cache
from resolver import resolve_market as resolver_resolve_market

# Imported from api.py — these live there until a later refactor
from api import (
    db, require_auth,
    _validate_uuid, _set_cache_headers, _calculate_and_distribute_payouts,
    MARKET_CREATION_COST, CURRENCY_SYMBOL,
    CABAL_USERNAMES, CABAL_COOLDOWN_MINUTES, DEFAULT_COOLDOWN_MINUTES,
    MAX_MARKET_DURATION_SECONDS,
)

router = APIRouter()


@router.get("/markets", response_model=List[MarketSummary], tags=["markets"])
async def list_markets(
    request: Request,
    response: Response,
    status: Optional[str] = None,
):
    """List markets, filtered by status.

    Query params:
        status: Filter by market status.
            - omitted or "active" or "open" → only OPEN markets (default)
            - "resolving"       → markets past closes_at, awaiting resolution
            - "closed" or "resolved" → resolved markets
            - "all"             → all markets regardless of status

    Caching:
        Responses include ETag and Last-Modified headers for HTTP caching.
        Send `If-None-Match` or `If-Modified-Since` to receive 304 Not Modified
        when data hasn't changed, saving bandwidth and parse time.
        Server-side results are cached in-memory with a 5-second TTL and
        invalidated immediately on any market mutation (create, bet, sell, resolve).
    """
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
        _set_cache_headers(response, cached["etag"], market_cache.last_modified)
        return cached["data"]

    markets = db.list_markets_with_creators()

    now = datetime.now(timezone.utc)
    transitioned = False
    for m in markets:
        if m["status"] == MarketStatus.OPEN and m["closes_at"] <= now:
            db.update_market_status(m["id"], MarketStatus.RESOLVING)
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

    result = []
    for m in markets:
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

    entry = market_cache.set(status_filter, result)
    _set_cache_headers(response, entry["etag"], market_cache.last_modified)
    return result


@router.get("/markets/{market_id}", response_model=MarketDetail, tags=["markets"])
async def get_market(market_id: str):
    """Get market details including current probability."""
    _validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    now = datetime.now(timezone.utc)
    if market["status"] == MarketStatus.OPEN and market["closes_at"] <= now:
        db.update_market_status(market_id, MarketStatus.RESOLVING)
        market["status"] = MarketStatus.RESOLVING

    creator = db.get_user(market["creator_id"]) if market["creator_id"] else None
    creator_username = creator["username"] if creator else None

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
    )


@router.post("/markets", response_model=MarketCreated, tags=["markets"])
async def create_market(req: MarketCreate, user: dict = Depends(require_auth)):
    """Create a new prediction market."""
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
            last_created = datetime.fromisoformat(last_created.replace('Z', '+00:00'))
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


@router.post("/markets/{market_id}/resolve", response_model=MarketDetail, tags=["markets"])
async def resolve_market(market_id: str, req: MarketResolve, user: dict = Depends(require_auth)):
    """Resolve a market. Only creator can resolve."""
    _validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    if market["creator_id"] != user["id"]:
        return error_response(403, "Only creator can resolve market", ErrorCode.FORBIDDEN)

    if market["status"] == MarketStatus.RESOLVED:
        return error_response(400, "Market already resolved", ErrorCode.ALREADY_RESOLVED)

    now = datetime.now(timezone.utc)
    if market["status"] == MarketStatus.OPEN and market["closes_at"] <= now:
        db.update_market_status(market_id, MarketStatus.RESOLVING)
        market["status"] = MarketStatus.RESOLVING

    db.resolve_market(market_id, req.outcome)
    _calculate_and_distribute_payouts(market_id, req.outcome)

    market_cache.invalidate()

    market = db.get_market(market_id)

    creator = db.get_user(market["creator_id"]) if market["creator_id"] else None
    creator_username = creator["username"] if creator else None

    return MarketDetail(
        id=market["id"],
        title=market["title"],
        description=market["description"],
        probability=1.0 if req.outcome == Outcome.YES else 0.0,
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
    )


@router.get("/markets/{market_id}/history", response_model=MarketHistory, tags=["markets"])
async def get_market_history(market_id: str):
    """Get probability history for charts."""
    _validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    market_bets = sorted(
        db.get_bets_for_market(market_id),
        key=lambda x: x["created_at"]
    )

    points = []
    points.append(ProbabilityPoint(
        timestamp=market["created_at"],
        probability=0.5,
        volume=0.0,
    ))

    cumulative_volume = 0.0
    for bet in market_bets:
        cumulative_volume += bet["amount"]
        points.append(ProbabilityPoint(
            timestamp=bet["created_at"],
            probability=bet["probability_after"],
            volume=cumulative_volume,
        ))

    return MarketHistory(market_id=market_id, points=points)


@router.get("/markets/{market_id}/comments", response_model=MarketComments, tags=["markets"])
async def get_comments(market_id: str):
    """Get all comments for a market."""
    _validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    raw_comments = db.get_market_comments(market_id)

    comments_by_id = {}
    top_level = []

    for c in raw_comments:
        comment = Comment(
            id=c["id"],
            market_id=c["market_id"],
            user_id=c["user_id"],
            username=c.get("username", "unknown"),
            content=c["content"],
            created_at=c["created_at"],
            parent_id=c.get("parent_id"),
            replies=[],
        )
        comments_by_id[c["id"]] = comment

        if c.get("parent_id") is None:
            top_level.append(comment)

    for c in raw_comments:
        if c.get("parent_id") and c["parent_id"] in comments_by_id:
            parent = comments_by_id[c["parent_id"]]
            parent.replies.append(comments_by_id[c["id"]])

    return MarketComments(
        market_id=market_id,
        comments=top_level,
        total=len(raw_comments),
    )


@router.post("/markets/{market_id}/comments", response_model=Comment, tags=["markets"])
async def create_comment(market_id: str, req: CommentCreate, user: dict = Depends(require_auth)):
    """Create a comment on a market."""
    _validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    if req.parent_id:
        parent_comments = db.get_market_comments(market_id)
        if not any(c["id"] == req.parent_id for c in parent_comments):
            return error_response(400, "Parent comment not found", ErrorCode.COMMENT_NOT_FOUND)

    comment_id = str(uuid.uuid4())
    comment = db.create_comment(
        comment_id=comment_id,
        market_id=market_id,
        user_id=user["id"],
        content=req.content,
        parent_id=req.parent_id,
    )

    return Comment(
        id=comment["id"],
        market_id=comment["market_id"],
        user_id=comment["user_id"],
        username=user["username"],
        content=comment["content"],
        created_at=comment["created_at"],
        parent_id=comment.get("parent_id"),
        replies=[],
    )


@router.post("/markets/{market_id}/request-resolution", response_model=ResolutionResult, tags=["markets"])
async def request_resolution(market_id: str, user: dict = Depends(require_auth)):
    """Trigger the 9-agent resolution committee to vote on market resolution."""
    _validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    if market["creator_id"] != user["id"]:
        return error_response(403, "Only market creator can request resolution", ErrorCode.FORBIDDEN)

    if market["status"] == MarketStatus.RESOLVED:
        return error_response(400, "Market already resolved", ErrorCode.ALREADY_RESOLVED)

    now = datetime.now(timezone.utc)
    if market["status"] == MarketStatus.OPEN and market["closes_at"] <= now:
        db.update_market_status(market_id, MarketStatus.RESOLVING)
        market["status"] = MarketStatus.RESOLVING

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    brave_key = os.getenv("BRAVE_API_KEY")

    if not anthropic_key or not brave_key:
        return error_response(500, "Resolution service not configured", ErrorCode.SERVICE_UNAVAILABLE)

    status, outcome, votes = await resolver_resolve_market(
        market_id=market_id,
        market_title=market["title"],
        market_description=market.get("description", ""),
        resolution_criteria=market.get("description", ""),
        anthropic_key=anthropic_key,
        brave_key=brave_key,
    )

    vote_dicts = [
        {
            "agent_id": v.agent_id,
            "vote": v.vote,
            "reasoning": v.reasoning,
            "sources": v.sources,
            "created_at": v.created_at.isoformat(),
        }
        for v in votes
    ]
    db.save_resolution_votes(market_id, vote_dicts)

    resolved_at = None
    if status == "resolved" and outcome:
        outcome_enum = Outcome.YES if outcome == "YES" else Outcome.NO
        db.resolve_market(market_id, outcome_enum)
        _calculate_and_distribute_payouts(market_id, outcome_enum)
        resolved_at = datetime.now(timezone.utc)

    return ResolutionResult(
        market_id=market_id,
        status=status,
        outcome=Outcome(outcome) if outcome else None,
        votes_yes=sum(1 for v in votes if v.vote == "YES"),
        votes_no=sum(1 for v in votes if v.vote == "NO"),
        total_votes=len(votes),
        votes=[
            ResolutionVote(
                agent_id=v.agent_id,
                vote=Outcome(v.vote),
                reasoning=v.reasoning,
                sources=v.sources,
                created_at=v.created_at,
            )
            for v in votes
        ],
        resolved_at=resolved_at,
    )


@router.get("/markets/{market_id}/resolution-votes", response_model=ResolutionResult, tags=["markets"])
async def get_resolution_votes(market_id: str):
    """Get the resolution committee votes for a market."""
    _validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    votes = db.get_resolution_votes(market_id)

    if not votes:
        return error_response(404, "No resolution votes found for this market", ErrorCode.NOT_FOUND)

    yes_votes = sum(1 for v in votes if v["vote"] == "YES")
    no_votes = sum(1 for v in votes if v["vote"] == "NO")

    if market["status"] == MarketStatus.RESOLVED:
        status = "resolved"
        outcome = market["resolution"]
    elif yes_votes >= 5:
        status = "resolved"
        outcome = Outcome.YES
    elif no_votes >= 5:
        status = "resolved"
        outcome = Outcome.NO
    else:
        status = "disputed"
        outcome = None

    return ResolutionResult(
        market_id=market_id,
        status=status,
        outcome=outcome,
        votes_yes=yes_votes,
        votes_no=no_votes,
        total_votes=len(votes),
        votes=[
            ResolutionVote(
                agent_id=v["agent_id"],
                vote=Outcome(v["vote"]),
                reasoning=v["reasoning"],
                sources=v.get("sources", []),
                created_at=datetime.fromisoformat(v["created_at"]) if isinstance(v["created_at"], str) else v["created_at"],
            )
            for v in votes
        ],
        resolved_at=market.get("resolved_at"),
    )
