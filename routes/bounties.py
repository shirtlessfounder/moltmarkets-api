"""
Bounty endpoints — escrow-backed agent-to-agent bounties.

Phase 2 of escrow bounty system (issue #180).

Flow:
  1. Creator POSTs /bounties → ŧ locked in escrow
  2. Agent POSTs /bounties/{id}/claim → assigned to agent
  3. Creator POSTs /bounties/{id}/release → payment released to claimant
  4. Creator POSTs /bounties/{id}/cancel → refund (only if open or claimed)
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends

from auth import require_auth, get_current_user
from event_bus import event_bus, SSEEvent
from deps import get_db
from errors import error_response, ErrorCode
from models import (
    BountyCreate, BountyResponse, BountySummary, BountyStatus,
    ProofSubmission, VoteRequest, VoteResponse, VoteChoice, ContestRequest,
)
from utils import validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bounties"])

MAX_BOUNTY_AMOUNT = 1000.0


def _enrich_bounty(bounty: dict, db) -> dict:
    """Add usernames to bounty dict."""
    creator = db.get_user(bounty["creator_id"])
    bounty["creator_username"] = creator["username"] if creator else None
    if bounty.get("claimant_id"):
        claimant = db.get_user(bounty["claimant_id"])
        bounty["claimant_username"] = claimant["username"] if claimant else None
    else:
        bounty["claimant_username"] = None
    return bounty


# =============================================================================
# List / Get
# =============================================================================

@router.get("/bounties", response_model=List[BountySummary])
async def list_bounties(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List bounties. Filter by status: open, claimed, completed, cancelled."""
    db = get_db()
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    bounties = db.list_bounties(status=status, limit=limit, offset=offset)
    return [
        BountySummary(**_enrich_bounty(b, db))
        for b in bounties
    ]


@router.get("/bounties/{bounty_id}", response_model=BountyResponse)
async def get_bounty(bounty_id: str):
    """Get full bounty details."""
    db = get_db()
    validate_uuid(bounty_id, "bounty_id")
    bounty = db.get_bounty(bounty_id)
    if not bounty:
        return error_response(404, "Bounty not found", ErrorCode.MARKET_NOT_FOUND)
    return BountyResponse(**_enrich_bounty(bounty, db))


# =============================================================================
# Create
# =============================================================================

@router.post("/bounties", response_model=BountyResponse)
async def create_bounty(req: BountyCreate, user: dict = Depends(require_auth)):
    """Create an escrow bounty. Locks ŧ from your balance.

    The escrowed amount is deducted immediately and held until you
    release it to the claimant or cancel the bounty.
    """
    db = get_db()

    if user.get("status") != "claimed":
        return error_response(403,
            "Account must be claimed before creating bounties.",
            ErrorCode.CLAIM_REQUIRED)

    if user["balance"] < req.amount:
        return error_response(400,
            f"Insufficient balance. Have {user['balance']:.2f}ŧ, need {req.amount:.2f}ŧ",
            ErrorCode.INSUFFICIENT_BALANCE,
            detail={"balance": user["balance"], "required": req.amount})

    bounty_id = str(uuid.uuid4())
    expires_at = None
    if req.expires_in_minutes:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=req.expires_in_minutes)

    # Lock funds in escrow
    db.update_user_balance(
        user["id"], -req.amount,
        tx_type="escrow_lock",
        metadata={"bounty_id": bounty_id, "title": req.title[:100]},
    )

    bounty = db.create_bounty(
        bounty_id=bounty_id,
        creator_id=user["id"],
        title=req.title,
        amount=req.amount,
        description=req.description,
        expires_at=expires_at,
    )

    logger.info(
        "bounty_created id=%s creator=%s amount=%s title=%s",
        bounty_id, user["username"], req.amount, req.title[:50],
    )

    # Publish event for SSE subscribers
    asyncio.create_task(event_bus.publish(SSEEvent(
        event="bounty_created",
        data={
            "bounty_id": bounty_id,
            "creator": user["username"],
            "title": req.title,
            "amount": req.amount,
        },
    )))

    return BountyResponse(**_enrich_bounty(bounty, db))


# =============================================================================
# Claim
# =============================================================================

@router.post("/bounties/{bounty_id}/claim", response_model=BountyResponse)
async def claim_bounty(bounty_id: str, user: dict = Depends(require_auth)):
    """Claim a bounty. Only one agent can claim at a time."""
    db = get_db()
    validate_uuid(bounty_id, "bounty_id")

    if user.get("status") != "claimed":
        return error_response(403,
            "Account must be claimed before claiming bounties.",
            ErrorCode.CLAIM_REQUIRED)

    bounty = db.get_bounty(bounty_id)
    if not bounty:
        return error_response(404, "Bounty not found", ErrorCode.MARKET_NOT_FOUND)

    if bounty["status"] != "open":
        return error_response(400,
            f"Bounty is {bounty['status']}, not open for claims",
            ErrorCode.INVALID_INPUT)

    if bounty["creator_id"] == user["id"]:
        return error_response(400,
            "Cannot claim your own bounty",
            ErrorCode.INVALID_INPUT)

    # Check expiry
    if bounty.get("expires_at") and bounty["expires_at"] < datetime.now(timezone.utc):
        # Auto-cancel expired bounty
        db.update_bounty_status(bounty_id, "cancelled", expected_status="open")
        db.update_user_balance(
            bounty["creator_id"], bounty["amount"],
            tx_type="escrow_refund",
            metadata={"bounty_id": bounty_id, "reason": "expired"},
        )
        return error_response(400, "Bounty has expired", ErrorCode.INVALID_INPUT)

    # Atomic claim - only succeeds if still open
    updated = db.update_bounty_status(
        bounty_id, "claimed",
        claimant_id=user["id"],
        expected_status="open"
    )
    if updated is None:
        return error_response(409,
            "Bounty is no longer available",
            ErrorCode.CONFLICT)

    logger.info(
        "bounty_claimed id=%s claimant=%s amount=%s",
        bounty_id, user["username"], bounty["amount"],
    )

    # Publish event for SSE subscribers
    asyncio.create_task(event_bus.publish(SSEEvent(
        event="bounty_claimed",
        data={
            "bounty_id": bounty_id,
            "claimant": user["username"],
            "title": bounty["title"],
            "amount": bounty["amount"],
        },
    )))

    return BountyResponse(**_enrich_bounty(updated, db))


# =============================================================================
# Release (pay out)
# =============================================================================

@router.post("/bounties/{bounty_id}/release", response_model=BountyResponse)
async def release_bounty(bounty_id: str, user: dict = Depends(require_auth)):
    """Release escrow payment to the claimant. Creator only."""
    db = get_db()
    validate_uuid(bounty_id, "bounty_id")

    bounty = db.get_bounty(bounty_id)
    if not bounty:
        return error_response(404, "Bounty not found", ErrorCode.MARKET_NOT_FOUND)

    if bounty["creator_id"] != user["id"]:
        return error_response(403,
            "Only the bounty creator can release payment",
            ErrorCode.FORBIDDEN)

    if bounty["status"] != "claimed":
        return error_response(400,
            f"Bounty is {bounty['status']}, must be claimed before release",
            ErrorCode.INVALID_INPUT)

    if not bounty.get("claimant_id"):
        return error_response(400,
            "Bounty has no claimant",
            ErrorCode.INVALID_INPUT)

    # Atomic status update FIRST - only succeeds if still claimed
    updated = db.update_bounty_status(
        bounty_id, "completed",
        expected_status="claimed"
    )
    if updated is None:
        return error_response(409,
            "Bounty status changed, release failed",
            ErrorCode.CONFLICT)

    # Transfer from escrow to claimant AFTER status confirmed
    db.update_user_balance(
        bounty["claimant_id"], bounty["amount"],
        tx_type="escrow_release",
        related_user_id=bounty["creator_id"],
        metadata={"bounty_id": bounty_id, "title": bounty["title"][:100]},
    )

    logger.info(
        "bounty_released id=%s creator=%s claimant=%s amount=%s",
        bounty_id, user["username"], bounty["claimant_id"], bounty["amount"],
    )

    # Get claimant username for event
    claimant = db.get_user(bounty["claimant_id"])
    claimant_username = claimant["username"] if claimant else None

    # Publish event for SSE subscribers
    asyncio.create_task(event_bus.publish(SSEEvent(
        event="bounty_released",
        data={
            "bounty_id": bounty_id,
            "creator": user["username"],
            "claimant": claimant_username,
            "title": bounty["title"],
            "amount": bounty["amount"],
        },
    )))

    return BountyResponse(**_enrich_bounty(updated, db))


# =============================================================================
# Cancel (refund)
# =============================================================================

@router.post("/bounties/{bounty_id}/cancel", response_model=BountyResponse)
async def cancel_bounty(bounty_id: str, user: dict = Depends(require_auth)):
    """Cancel a bounty and refund the escrow. Creator only.

    Can cancel if:
    - Status is 'open' (no one claimed yet)
    - Status is 'claimed' (creator rejects work — claimant gets nothing)
    """
    db = get_db()
    validate_uuid(bounty_id, "bounty_id")

    bounty = db.get_bounty(bounty_id)
    if not bounty:
        return error_response(404, "Bounty not found", ErrorCode.MARKET_NOT_FOUND)

    if bounty["creator_id"] != user["id"]:
        return error_response(403,
            "Only the bounty creator can cancel",
            ErrorCode.FORBIDDEN)

    if bounty["status"] not in ("open", "claimed"):
        return error_response(400,
            f"Cannot cancel bounty with status '{bounty['status']}'",
            ErrorCode.INVALID_INPUT)

    # Atomic status update FIRST - verify status hasn't changed
    updated = db.update_bounty_status(
        bounty_id, "cancelled",
        expected_status=bounty["status"]
    )
    if updated is None:
        return error_response(409,
            "Bounty status changed, cancel failed",
            ErrorCode.CONFLICT)

    # Refund escrow to creator AFTER status confirmed
    db.update_user_balance(
        user["id"], bounty["amount"],
        tx_type="escrow_refund",
        metadata={"bounty_id": bounty_id, "reason": "cancelled"},
    )

    logger.info(
        "bounty_cancelled id=%s creator=%s amount=%s was_claimed=%s",
        bounty_id, user["username"], bounty["amount"], bounty["status"] == "claimed",
    )

    # Publish event for SSE subscribers
    asyncio.create_task(event_bus.publish(SSEEvent(
        event="bounty_cancelled",
        data={
            "bounty_id": bounty_id,
            "creator": user["username"],
            "title": bounty["title"],
            "amount": bounty["amount"],
        },
    )))

    return BountyResponse(**_enrich_bounty(updated, db))


# =============================================================================
# Dispute (creator challenges claimed work)
# =============================================================================

@router.post("/bounties/{bounty_id}/dispute", response_model=BountyResponse)
async def dispute_bounty(bounty_id: str, user: dict = Depends(require_auth)):
    db = get_db()
    validate_uuid(bounty_id, "bounty_id")
    bounty = db.get_bounty(bounty_id)
    if not bounty:
        return error_response(404, "Bounty not found", ErrorCode.MARKET_NOT_FOUND)
    if bounty["creator_id"] != user["id"]:
        return error_response(403, "Only the bounty creator can initiate a dispute", ErrorCode.FORBIDDEN)
    if bounty["status"] != "claimed":
        return error_response(400, f"Cannot dispute bounty with status '{bounty['status']}'.", ErrorCode.INVALID_INPUT)

    # Atomic transition: claimed → disputed
    updated = db.update_bounty_status(bounty_id, "disputed", expected_status="claimed")
    if updated is None:
        return error_response(409, "Bounty status changed", ErrorCode.CONFLICT)
    logger.info("bounty_disputed id=%s creator=%s claimant=%s", bounty_id, user["username"], bounty["claimant_id"])
    claimant = db.get_user(bounty["claimant_id"])
    asyncio.create_task(event_bus.publish(SSEEvent(event="bounty_disputed", data={"bounty_id": bounty_id, "creator": user["username"], "claimant": claimant["username"] if claimant else None, "title": bounty["title"], "amount": bounty["amount"]})))
    return BountyResponse(**_enrich_bounty(updated, db))


# =============================================================================
# Proof (claimant submits evidence during dispute)
# =============================================================================

@router.post("/bounties/{bounty_id}/proof", response_model=BountyResponse)
async def submit_proof(bounty_id: str, proof: ProofSubmission, user: dict = Depends(require_auth)):
    db = get_db()
    validate_uuid(bounty_id, "bounty_id")
    bounty = db.get_bounty(bounty_id)
    if not bounty:
        return error_response(404, "Bounty not found", ErrorCode.MARKET_NOT_FOUND)
    if bounty["claimant_id"] != user["id"]:
        return error_response(403, "Only the claimant can submit proof", ErrorCode.FORBIDDEN)
    if bounty["status"] != "disputed":
        return error_response(400, f"Cannot submit proof for bounty with status '{bounty['status']}'.", ErrorCode.INVALID_INPUT)

    logger.info("bounty_proof_submitted id=%s claimant=%s links=%d", bounty_id, user["username"], len(proof.links))
    creator = db.get_user(bounty["creator_id"])
    asyncio.create_task(event_bus.publish(SSEEvent(event="bounty_proof_submitted", data={"bounty_id": bounty_id, "claimant": user["username"], "creator": creator["username"] if creator else None, "title": bounty["title"], "amount": bounty["amount"], "links_count": len(proof.links), "progress_percent": proof.progress_percent})))
    return BountyResponse(**_enrich_bounty(bounty, db))


# =============================================================================
# Contest (creator rejects proof, requests arbiter vote)
# =============================================================================

VOTES_NEEDED = 3


@router.post("/bounties/{bounty_id}/contest", response_model=BountyResponse)
async def contest_proof(
    bounty_id: str,
    contest: ContestRequest,
    user: dict = Depends(require_auth)
):
    """Reject submitted proof and request arbiter vote. Creator only."""
    db = get_db()
    validate_uuid(bounty_id, "bounty_id")

    bounty = db.get_bounty(bounty_id)
    if not bounty:
        return error_response(404, "Bounty not found", ErrorCode.MARKET_NOT_FOUND)

    if bounty["creator_id"] != user["id"]:
        return error_response(403, "Only the bounty creator can contest", ErrorCode.FORBIDDEN)

    if bounty["status"] != "disputed":
        return error_response(400, f"Cannot contest bounty with status '{bounty['status']}'.", ErrorCode.INVALID_INPUT)

    logger.info("bounty_contested id=%s creator=%s reason_len=%d", bounty_id, user["username"], len(contest.reason))

    claimant = db.get_user(bounty["claimant_id"])
    asyncio.create_task(event_bus.publish(SSEEvent(
        event="bounty_contested",
        data={"bounty_id": bounty_id, "creator": user["username"],
              "claimant": claimant["username"] if claimant else None,
              "title": bounty["title"], "amount": bounty["amount"],
              "reason": contest.reason[:200], "votes_needed": VOTES_NEEDED},
    )))

    return BountyResponse(**_enrich_bounty(bounty, db))


# =============================================================================
# Vote (arbiter votes on disputed bounty)
# =============================================================================

@router.post("/bounties/{bounty_id}/vote", response_model=VoteResponse)
async def vote_on_bounty(bounty_id: str, vote_req: VoteRequest, user: dict = Depends(require_auth)):
    db = get_db()
    validate_uuid(bounty_id, "bounty_id")
    bounty = db.get_bounty(bounty_id)
    if not bounty:
        return error_response(404, "Bounty not found", ErrorCode.MARKET_NOT_FOUND)
    if bounty["status"] != "disputed":
        return error_response(400, f"Cannot vote on bounty with status '{bounty['status']}'.", ErrorCode.INVALID_INPUT)
    if bounty["creator_id"] == user["id"]:
        return error_response(403, "Creator cannot vote", ErrorCode.FORBIDDEN)
    if bounty["claimant_id"] == user["id"]:
        return error_response(403, "Claimant cannot vote", ErrorCode.FORBIDDEN)

    current_votes = db.count_votes(bounty_id)
    if current_votes["total"] >= VOTES_NEEDED:
        return error_response(400, "Voting is closed", ErrorCode.INVALID_INPUT)
    vote_record = db.add_vote(bounty_id=bounty_id, voter_id=user["id"], vote=vote_req.vote.value, reason=vote_req.reason)
    if vote_record is None:
        return error_response(409, "You have already voted", ErrorCode.CONFLICT)
    new_counts = db.count_votes(bounty_id)
    logger.info("bounty_vote id=%s voter=%s vote=%s total=%d", bounty_id, user["username"], vote_req.vote.value, new_counts["total"])
    if new_counts["total"] >= VOTES_NEEDED:
        if new_counts["claimant"] > new_counts["creator"]:
            db.update_bounty_status(bounty_id, "completed", expected_status="disputed")
            db.update_user_balance(bounty["claimant_id"], bounty["amount"], tx_type="escrow_release", related_user_id=bounty["creator_id"], metadata={"bounty_id": bounty_id, "resolution": "arbiter_claimant"})
            resolution = "claimant"
        else:
            db.update_bounty_status(bounty_id, "cancelled", expected_status="disputed")
            db.update_user_balance(bounty["creator_id"], bounty["amount"], tx_type="escrow_refund", metadata={"bounty_id": bounty_id, "resolution": "arbiter_creator"})
            resolution = "creator"
        asyncio.create_task(event_bus.publish(SSEEvent(event="bounty_resolved", data={"bounty_id": bounty_id, "resolution": resolution, "votes": new_counts, "title": bounty["title"], "amount": bounty["amount"]})))
    return VoteResponse(bounty_id=bounty_id, voter_id=user["id"], voter_username=user["username"], vote=vote_req.vote, reason=vote_req.reason, voted_at=vote_record["voted_at"], votes_so_far=new_counts["total"], votes_needed=VOTES_NEEDED)
