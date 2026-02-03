"""
Shared dependencies for route modules.

This module holds the global Storage instance and economic / timing
constants that multiple route files need.  Route files import from
here (and from auth, errors, models, etc.) instead of from api.py —
which avoids circular imports.

Heavier helpers live in dedicated modules extracted from this file
(see issue #128):

- ``committee.py``      – resolution committee logic
- ``payouts.py``        – payout calculation & distribution
- ``twitter_verify.py`` – Twitter/X verification helpers
- ``utils.py``          – pagination, UUID validation, cache headers

The Storage instance is set once by api.py at startup via ``init()``.
"""

from typing import Optional

from storage import Storage

# ---------------------------------------------------------------------------
# Database singleton — set by api.py at import time
# ---------------------------------------------------------------------------
_db: Optional[Storage] = None


def init(db: Storage) -> None:
    """Bind the global Storage instance.  Called once by api.py."""
    global _db
    _db = db


def get_db() -> Storage:
    """Return the global Storage instance.  Raises if not initialised."""
    if _db is None:
        raise RuntimeError("deps.init() was not called — Storage not available")
    return _db


# ---------------------------------------------------------------------------
# Economics Constants
# ---------------------------------------------------------------------------

TRADE_FEE_RATE = 0.02                  # 2% total fee
CREATOR_FEE_SHARE = 0.5                # 50% of fee → market creator (1%)
MARKET_CREATION_COST = 100             # Legacy constant (kept for backward compat)
MIN_CREATION_LIQUIDITY = 50            # Minimum liquidity to create a market
CABAL_USERNAMES = {"bicep", "spotter", "crabby"}
CABAL_COOLDOWN_MINUTES = 1
DEFAULT_COOLDOWN_MINUTES = 30
MAX_MARKET_DURATION_SECONDS = 3600     # 1 hour hard cap (testing phase)

CURRENCY_SYMBOL = "ŧ"
CURRENCY_NAME = "points"
STARTING_BALANCE = 1000.0
MAX_OPTIMISTIC_RETRIES = 5
OPTIMISTIC_RETRY_BASE_MS = 2
