"""
Market endpoints — CRUD, comments, resolution, history.
"""

import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response

from auth import require_auth
from cpmm import get_cpmm_probability
from deps import (
    get_db, validate_uuid, clamp_pagination,
    calculate_and_distribute_payouts,
    set_cache_headers, form_committee, check_committee_unanimity,
    MARKET_CREATION_COST, COMMITTEE_WINDOW_MINUTES,
    CABAL_USERNAMES, CABAL_COOLDOWN_MINUTES, DEFAULT_COOLDOWN_MINUTES,
    MAX_MARKET_DURATION_SECONDS, CURRENCY_SYMBOL,
)
from errors import error_response, ErrorCode
from event_bus import event_bus, SSEEvent
from market_cache import market_cache
from models import (
    MarketCreate, MarketResolve, MarketSummary, MarketDetail, MarketCreated,
    ProbabilityPoint, MarketHistory,
    CommentCreate, Comment, MarketComments,
    ResolutionResult, ResolutionVote,
    CommitteeVoteRequest, CommitteeVoteResponse, CommitteeStatusResponse,
    CommitteeVoteDetail, CommitteeOutcome,
    MarketStatus, Outcome,
    PaginationMeta, PaginatedMarketSummary,
)
from resolver import resolve_market as resolver_resolve_market

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

    creator = db.get_user(market["creator_id"]) if market["creator_id"] else None
    creator_username = creator["username"] if creator else None

    return _build_market_detail(market, creator_username)


# =============================================================================
# Create / Resolve
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


@router.post("/markets/{market_id}/resolve", response_model=MarketDetail)
async def resolve_market(market_id: str, req: MarketResolve, user: dict = Depends(require_auth)):
    """Resolve a market. Only creator can resolve.

    Issue #115: Markets remain OPEN and tradeable until resolution.
    - Creator calls resolve on an OPEN market → initiates resolution.
    - If solo creator (no other traders): resolves immediately.
    - If other traders exist: forms committee, transitions to RESOLVING,
      and requires committee vote process (30-minute window).
    - After the committee deadline, creator regains unilateral resolve.
    """
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    if market["creator_id"] != user["id"]:
        return error_response(403, "Only creator can resolve market", ErrorCode.FORBIDDEN)

    if market["status"] == MarketStatus.RESOLVED:
        return error_response(400, "Market already resolved", ErrorCode.ALREADY_RESOLVED)

    # If market is OPEN, initiate the resolution process (issue #115)
    if market["status"] == MarketStatus.OPEN:
        committee = form_committee(market_id, market)
        db.update_market_status(market_id, MarketStatus.RESOLVING)
        market["status"] = MarketStatus.RESOLVING

        # If committee has multiple members, require the committee vote process
        if len(committee) > 1:
            resolution_deadline = market.get("resolution_deadline")
            now = datetime.now(timezone.utc)
            if resolution_deadline and now < resolution_deadline:
                remaining = (resolution_deadline - now).total_seconds() / 60
                return error_response(403,
                    f"Committee resolution window is active. Use POST /markets/{market_id}/resolution-vote instead. "
                    f"Creator can resolve unilaterally in {remaining:.0f} minutes.",
                    ErrorCode.COMMITTEE_WINDOW_ACTIVE,
                    detail={"resolution_deadline": resolution_deadline.isoformat(),
                            "remaining_minutes": round(remaining, 1)})
        # Solo creator → falls through to resolve immediately

    # Market is RESOLVING — check committee window enforcement (issue #107)
    committee = market.get("committee") or []
    resolution_deadline = market.get("resolution_deadline")
    if len(committee) > 1 and resolution_deadline:
        now = datetime.now(timezone.utc)
        if now < resolution_deadline:
            remaining = (resolution_deadline - now).total_seconds() / 60
            return error_response(403,
                f"Committee resolution window is active. Use POST /markets/{market_id}/resolution-vote instead. "
                f"Creator can resolve unilaterally in {remaining:.0f} minutes.",
                ErrorCode.COMMITTEE_WINDOW_ACTIVE,
                detail={"resolution_deadline": resolution_deadline.isoformat(),
                        "remaining_minutes": round(remaining, 1)})

    db.resolve_market(market_id, req.outcome)
    calculate_and_distribute_payouts(market_id, req.outcome)

    market_cache.invalidate()

    market = db.get_market(market_id)

    creator = db.get_user(market["creator_id"]) if market["creator_id"] else None
    creator_username = creator["username"] if creator else None

    await event_bus.publish(SSEEvent(
        event="market_resolved",
        data={
            "market_id": market["id"],
            "title": market["title"],
            "outcome": req.outcome.value,
            "resolved_at": str(market["resolved_at"]),
        },
        market_id=market["id"],
    ))

    return _build_market_detail(market, creator_username)


# =============================================================================
# Committee Resolution Voting (issue #107)
# =============================================================================

@router.post("/markets/{market_id}/resolution-vote", response_model=CommitteeVoteResponse)
async def cast_committee_vote(market_id: str, req: CommitteeVoteRequest, user: dict = Depends(require_auth)):
    """Cast a committee resolution vote.

    Only committee members can vote. Votes can be changed before unanimity.
    Unanimous YES/NO triggers auto-resolution with payout distribution.
    """
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    if market["status"] == MarketStatus.RESOLVED:
        return error_response(400, "Market already resolved", ErrorCode.ALREADY_RESOLVED)

    if market["status"] != MarketStatus.RESOLVING:
        return error_response(400,
            "Market is not in RESOLVING state. The creator must first call POST /markets/{id}/resolve to initiate resolution.",
            ErrorCode.MARKET_CLOSED)

    # Form committee if not yet formed (e.g., market was set to RESOLVING externally)
    committee = market.get("committee")
    if not committee:
        committee = form_committee(market_id, market)

    # Check membership
    if user["id"] not in committee:
        return error_response(403,
            "You are not a member of the resolution committee for this market",
            ErrorCode.NOT_COMMITTEE_MEMBER)

    # Cast/update vote
    db.upsert_committee_vote(market_id, user["id"], req.outcome.value)

    # Check for unanimity
    unanimous_outcome = check_committee_unanimity(market_id, committee)
    auto_resolved = False
    resolution_outcome = None

    if unanimous_outcome:
        outcome_enum = Outcome.YES if unanimous_outcome == "YES" else Outcome.NO
        db.resolve_market(market_id, outcome_enum)
        calculate_and_distribute_payouts(market_id, outcome_enum)
        market_cache.invalidate()
        auto_resolved = True
        resolution_outcome = outcome_enum

        await event_bus.publish(SSEEvent(
            event="market_resolved",
            data={
                "market_id": market_id,
                "title": market["title"],
                "outcome": unanimous_outcome,
                "resolved_at": str(datetime.now(timezone.utc)),
                "resolution_method": "committee_unanimous",
            },
            market_id=market_id,
        ))

        logger.info(
            "Market %s auto-resolved via unanimous committee vote: %s",
            market_id, unanimous_outcome,
        )

    return CommitteeVoteResponse(
        market_id=market_id,
        agent_id=user["id"],
        outcome=req.outcome,
        auto_resolved=auto_resolved,
        resolution_outcome=resolution_outcome,
    )


@router.get("/markets/{market_id}/committee-votes", response_model=CommitteeStatusResponse)
async def get_committee_status(market_id: str):
    """Get the committee resolution status for a market.

    Shows committee members, their votes, the deadline, and whether
    unanimity has been reached.
    """
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    committee = market.get("committee") or []
    resolution_deadline = market.get("resolution_deadline")

    # Get votes
    raw_votes = db.get_committee_votes(market_id)
    votes = [
        CommitteeVoteDetail(
            agent_id=v["agent_id"],
            outcome=CommitteeOutcome(v["outcome"]),
            timestamp=v["created_at"],
        )
        for v in raw_votes
    ]

    # Determine status
    now = datetime.now(timezone.utc)
    if market["status"] == MarketStatus.RESOLVED:
        status = "resolved"
    elif not committee:
        status = "no_committee"
    else:
        unanimous_outcome = check_committee_unanimity(market_id, committee)
        if unanimous_outcome:
            status = "unanimous"
        elif resolution_deadline and now > resolution_deadline:
            status = "expired"
        elif raw_votes:
            status = "mixed" if len(set(v["outcome"] for v in raw_votes)) > 1 else "pending"
        else:
            status = "pending"

    unanimous_outcome_val = None
    if status == "unanimous":
        result = check_committee_unanimity(market_id, committee)
        if result:
            unanimous_outcome_val = Outcome(result)

    return CommitteeStatusResponse(
        market_id=market_id,
        committee=committee,
        votes=votes,
        resolution_deadline=resolution_deadline,
        status=status,
        unanimous_outcome=unanimous_outcome_val,
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


# =============================================================================
# Comments
# =============================================================================

@router.get("/markets/{market_id}/comments", response_model=MarketComments)
async def get_comments(market_id: str):
    """Get all comments for a market."""
    db = get_db()
    validate_uuid(market_id, "market_id")
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


@router.post("/markets/{market_id}/comments", response_model=Comment)
async def create_comment(market_id: str, req: CommentCreate, user: dict = Depends(require_auth)):
    """Create a comment on a market."""
    db = get_db()
    validate_uuid(market_id, "market_id")
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

    await event_bus.publish(SSEEvent(
        event="chat_message",
        data={
            "id": comment["id"],
            "username": user["username"],
            "text": comment["content"],
            "channel": "comment",
            "market_id": market_id,
            "created_at": str(comment["created_at"]),
        },
        market_id=market_id,
    ))

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


# =============================================================================
# Resolution Committee
# =============================================================================

@router.post("/markets/{market_id}/request-resolution", response_model=ResolutionResult)
async def request_resolution(market_id: str, user: dict = Depends(require_auth)):
    """Trigger the 9-agent resolution committee to vote on market resolution."""
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    if market["creator_id"] != user["id"]:
        return error_response(403, "Only market creator can request resolution", ErrorCode.FORBIDDEN)

    if market["status"] == MarketStatus.RESOLVED:
        return error_response(400, "Market already resolved", ErrorCode.ALREADY_RESOLVED)

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
        calculate_and_distribute_payouts(market_id, outcome_enum)

        resolved_at = datetime.now(timezone.utc)

        await event_bus.publish(SSEEvent(
            event="market_resolved",
            data={
                "market_id": market_id,
                "title": market["title"],
                "outcome": outcome,
                "resolved_at": str(resolved_at),
                "resolution_method": "committee",
            },
            market_id=market_id,
        ))

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


@router.get("/markets/{market_id}/resolution-votes", response_model=ResolutionResult)
async def get_resolution_votes(market_id: str):
    """Get the resolution committee votes for a market."""
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    votes = db.get_resolution_votes(market_id)
    if not votes:
        return error_response(404, "No resolution votes found for this market", ErrorCode.NOT_FOUND)

    yes_votes = sum(1 for v in votes if v["vote"] == "YES")
    no_votes = sum(1 for v in votes if v["vote"] == "NO")

    if market["status"] == MarketStatus.RESOLVED:
        res_status = "resolved"
        outcome = market["resolution"]
    elif yes_votes >= 5:
        res_status = "resolved"
        outcome = Outcome.YES
    elif no_votes >= 5:
        res_status = "resolved"
        outcome = Outcome.NO
    else:
        res_status = "disputed"
        outcome = None

    return ResolutionResult(
        market_id=market_id,
        status=res_status,
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
