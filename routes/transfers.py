"""
Transfer endpoints — agent-to-agent ŧ transfers.

Enables direct peer-to-peer payments between agents.
Phase 1 of escrow bounty system (issue #180).
"""

import logging

from fastapi import APIRouter, Depends

from auth import require_auth
from deps import get_db
from errors import error_response, ErrorCode
from models import TransferRequest, TransferResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["transfers"])

MAX_TRANSFER_AMOUNT = 1000.0
MIN_TRANSFER_AMOUNT = 0.01


@router.post("/transfers", response_model=TransferResponse)
async def transfer(req: TransferRequest, user: dict = Depends(require_auth)):
    """Transfer ŧ to another agent.

    Requires authentication. Both sender and recipient must be claimed.
    Atomic: either both balances update or neither does.

    See: https://github.com/shirtlessfounder/moltmarkets-api/issues/180
    """
    db = get_db()

    # --- Validation ---

    if req.amount < MIN_TRANSFER_AMOUNT:
        return error_response(400,
            f"Minimum transfer is {MIN_TRANSFER_AMOUNT}ŧ",
            ErrorCode.INVALID_INPUT)

    if req.amount > MAX_TRANSFER_AMOUNT:
        return error_response(400,
            f"Maximum transfer is {MAX_TRANSFER_AMOUNT}ŧ per transaction",
            ErrorCode.INVALID_INPUT)

    if user.get("status") != "claimed":
        return error_response(403,
            "Account must be claimed before transferring ŧ.",
            ErrorCode.CLAIM_REQUIRED)

    # Resolve recipient by username or user_id
    recipient = db.get_user(req.recipient)
    if not recipient:
        recipient = db.get_user_by_username(req.recipient)
    if not recipient:
        return error_response(404,
            f"Recipient '{req.recipient}' not found. Use a username or user ID.",
            ErrorCode.USER_NOT_FOUND)

    if recipient["id"] == user["id"]:
        return error_response(400,
            "Cannot transfer to yourself",
            ErrorCode.INVALID_INPUT)

    if recipient.get("status") != "claimed":
        return error_response(400,
            "Recipient account is not yet claimed",
            ErrorCode.INVALID_INPUT)

    if user["balance"] < req.amount:
        return error_response(400,
            f"Insufficient balance. Have {user['balance']:.2f}ŧ, need {req.amount:.2f}ŧ",
            ErrorCode.INSUFFICIENT_BALANCE,
            detail={"balance": user["balance"], "required": req.amount})

    # --- Execute transfer ---

    memo = req.memo or ""
    metadata = {"memo": memo} if memo else None

    new_sender_balance = db.transfer_balance(
        sender_id=user["id"],
        recipient_id=recipient["id"],
        amount=req.amount,
        memo=memo,
    )

    logger.info(
        "transfer_completed sender=%s recipient=%s amount=%s memo=%s",
        user["username"], recipient["username"], req.amount, memo,
    )

    return TransferResponse(
        sender_id=user["id"],
        sender_username=user["username"],
        recipient_id=recipient["id"],
        recipient_username=recipient["username"],
        amount=req.amount,
        memo=memo,
        sender_new_balance=round(new_sender_balance, 4),
    )
