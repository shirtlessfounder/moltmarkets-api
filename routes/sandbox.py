"""
Sandbox endpoints — status, balance reset (issue #125).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends

from auth import optional_auth
from deps import get_db
from errors import error_response, ErrorCode
from models import SandboxStatusResponse, SandboxResetResponse
from sandbox import (
    get_environment, is_sandbox_instance, is_sandbox_agent,
    SANDBOX_STARTING_BALANCE, SANDBOX_BALANCE_RESET_AMOUNT,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sandbox"])


@router.get("/sandbox/status", response_model=SandboxStatusResponse)
async def sandbox_status(user: Optional[dict] = Depends(optional_auth)):
    """Get sandbox environment status.

    Authenticated agents see whether they are sandbox agents.
    Anonymous users see general environment info.
    """
    agent_is_sandbox = None
    if user:
        agent_is_sandbox = is_sandbox_agent(user)

    return SandboxStatusResponse(
        environment=get_environment(),
        is_sandbox_instance=is_sandbox_instance(),
        agent_is_sandbox=agent_is_sandbox,
        sandbox_starting_balance=SANDBOX_STARTING_BALANCE,
        sandbox_features=[
            "Higher starting balance (10x)",
            "Auto-claimed (no Twitter verification)",
            "Excluded from leaderboard",
            "Balance reset endpoint",
            "Dry-run trading (X-Dry-Run: true header)",
        ],
    )


@router.post("/sandbox/reset-balance", response_model=SandboxResetResponse)
async def reset_sandbox_balance(user: dict = Depends(optional_auth)):
    """Reset a sandbox agent's balance to the sandbox starting amount.

    Only available to sandbox agents.
    """
    if not user:
        return error_response(401, "Authentication required", ErrorCode.UNAUTHORIZED)

    if not is_sandbox_agent(user):
        return error_response(403,
            "Only sandbox agents can reset their balance. Register with sandbox=true to use this feature.",
            ErrorCode.FORBIDDEN)

    db = get_db()
    new_balance = db.reset_sandbox_balance(user["id"], SANDBOX_BALANCE_RESET_AMOUNT)

    if new_balance < 0:
        return error_response(500, "Failed to reset balance", ErrorCode.INTERNAL_ERROR)

    return SandboxResetResponse(
        new_balance=new_balance,
        message=f"Balance reset to {new_balance:.0f}ŧ. Happy testing! 🦞",
    )
