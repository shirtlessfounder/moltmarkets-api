"""
Resolution endpoints — resolve, request-resolution, committee voting.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request

from auth import require_auth, verify_admin_secret
from committee import form_committee, check_committee_unanimity
from deps import get_db
from payouts import calculate_and_distribute_payouts
from utils import validate_uuid
from errors import error_response, ErrorCode
from event_bus import event_bus, SSEEvent
from market_cache import market_cache
from models import (
    MarketResolve, MarketDetail,
    ResolutionResult, ResolutionVote,
    CommitteeVoteRequest, CommitteeVoteResponse, CommitteeStatusResponse,
    CommitteeVoteDetail, CommitteeOutcome,
    MarketStatus, Outcome,
)
from resolver import resolve_market as resolver_resolve_market
from routes.markets import _build_market_detail

logger = logging.getLogger(__name__)

router = APIRouter(tags=["markets"])


# =============================================================================
# Resolve
# =============================================================================

@router.post("/markets/{market_id}/resolve", response_model=MarketDetail)
async def resolve_market(
    market_id: str,
    req: MarketResolve,
    request: Request,
    user: dict = Depends(require_auth),
    x_admin_secret: Optional[str] = Header(None),
):
    """Resolve a market. Creator or admin can resolve.

    Issue #115: Markets remain OPEN and tradeable until resolution.
    - Creator calls resolve on an OPEN market → initiates resolution.
    - If solo creator (no other traders): resolves immediately.
    - If other traders exist: forms committee, transitions to RESOLVING,
      and requires committee vote process (30-minute window).
    - After the committee deadline, creator regains unilateral resolve.
    
    Admin override: If X-Admin-Secret header is provided and valid,
    any authenticated user can resolve any market (bypasses creator check).
    """
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    # Check if admin override is being used
    is_admin = False
    if x_admin_secret:
        try:
            verify_admin_secret(x_admin_secret, request)
            is_admin = True
        except Exception:
            # Invalid admin secret — fall through to creator check
            pass

    # Allow resolution if user is creator OR admin
    if not is_admin and market["creator_id"] != user["id"]:
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
    
    # Batch fetch usernames for voters (required by CommitteeVoteDetail)
    voter_ids = {v["agent_id"] for v in raw_votes}
    voter_users = db.get_users_by_ids(voter_ids) if voter_ids else {}
    
    votes = [
        CommitteeVoteDetail(
            agent_id=v["agent_id"],
            agent_username=voter_users.get(v["agent_id"], {}).get("username", "unknown"),
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
# Resolution Committee (9-agent)
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
