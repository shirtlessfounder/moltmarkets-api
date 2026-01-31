"""
Sandbox/testnet environment helpers (issue #125).

Provides:
- Environment detection (MOLTMARKETS_ENV)
- Sandbox agent helpers (is_sandbox_agent, get_starting_balance)
- Dry-run header parsing (is_dry_run)
- Constants for sandbox balances
"""

import os
from typing import Any, Mapping

from deps import STARTING_BALANCE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SANDBOX_STARTING_BALANCE = 10_000.0   # 10x normal starting balance
SANDBOX_BALANCE_RESET_AMOUNT = 10_000.0

VALID_ENVIRONMENTS = {"production", "sandbox", "staging"}


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def get_environment() -> str:
    """Return the current environment name from MOLTMARKETS_ENV.

    Falls back to 'production' if unset or invalid.
    """
    env = os.environ.get("MOLTMARKETS_ENV", "production").lower().strip()
    if env not in VALID_ENVIRONMENTS:
        return "production"
    return env


def is_sandbox_instance() -> bool:
    """True if the entire instance is running in sandbox mode."""
    return get_environment() == "sandbox"


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------

def is_sandbox_agent(user: Mapping[str, Any]) -> bool:
    """Check if a user dict represents a sandbox agent."""
    return bool(user.get("is_sandbox", False))


def get_starting_balance(sandbox: bool = False) -> float:
    """Return the appropriate starting balance."""
    return SANDBOX_STARTING_BALANCE if sandbox else STARTING_BALANCE


# ---------------------------------------------------------------------------
# Dry-run helpers
# ---------------------------------------------------------------------------

_TRUTHY = {"true", "1", "yes"}


def is_dry_run(headers: Mapping[str, str]) -> bool:
    """Check if the X-Dry-Run header is set to a truthy value.

    Case-insensitive header name and value.
    """
    # FastAPI/Starlette normalises header keys to lowercase
    for key in ("x-dry-run", "X-Dry-Run"):
        val = headers.get(key, "")
        if val.lower().strip() in _TRUTHY:
            return True
    return False
