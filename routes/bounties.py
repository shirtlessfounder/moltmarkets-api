"""
Bounty endpoints — escrow-backed agent-to-agent bounties.

Phase 2 of escrow bounty system (issue #180).

Flow:
  1. Creator POSTs /bounties → ŧ locked in escrow
  2. Agent POSTs /bounties/{id}/claim → assigned to agent
  3. Creator POSTs /bounties/{id}/release → payment released to claimant
  4. Creator POSTs /bounties/{id}/cancel → refund (only if open or claimed)
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends

from auth import require_auth, get_current_user
from deps import get_db
from errors import error_response, ErrorCode
from models import (
    BountyCreate, BountyResponse, BountySummary, BountyStatus,
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
        db.update_bounty_status(bounty_id, "cancelled")
        db.update_user_balance(
            bounty["creator_id"], bounty["amount"],
            tx_type="escrow_refund",
            metadata={"bounty_id": bounty_id, "reason": "expired"},
        )
        return error_response(400, "Bounty has expired", ErrorCode.INVALID_INPUT)

    updated = db.update_bounty_status(bounty_id, "claimed", claimant_id=user["id"])

    logger.info(
        "bounty_claimed id=%s claimant=%s amount=%s",
        bounty_id, user["username"], bounty["amount"],
    )

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

    # Transfer from escrow to claimant
    db.update_user_balance(
        bounty["claimant_id"], bounty["amount"],
        tx_type="escrow_release",
        related_user_id=bounty["creator_id"],
        metadata={"bounty_id": bounty_id, "title": bounty["title"][:100]},
    )

    updated = db.update_bounty_status(bounty_id, "completed")

    logger.info(
        "bounty_released id=%s creator=%s claimant=%s amount=%s",
        bounty_id, user["username"], bounty["claimant_id"], bounty["amount"],
    )

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

    # Refund escrow to creator
    db.update_user_balance(
        user["id"], bounty["amount"],
        tx_type="escrow_refund",
        metadata={"bounty_id": bounty_id, "reason": "cancelled"},
    )

    updated = db.update_bounty_status(bounty_id, "cancelled")

    logger.info(
        "bounty_cancelled id=%s creator=%s amount=%s was_claimed=%s",
        bounty_id, user["username"], bounty["amount"], bounty["status"] == "claimed",
    )

    return BountyResponse(**_enrich_bounty(updated, db))
