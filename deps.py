"""
Shared dependencies for route modules.

This module holds the global Storage instance and helper functions
that multiple route files need.  Route files import from here (and
from auth, errors, models, etc.) instead of from api.py — which
avoids circular imports.

The Storage instance is set once by api.py at startup via ``init()``.
"""

import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from errors import APIError, ErrorCode
from storage import Storage

logger = logging.getLogger(__name__)

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
MARKET_CREATION_COST = 100             # Cost in ŧ to create a market
CABAL_USERNAMES = {"bicep", "spotter", "crabby"}
CABAL_COOLDOWN_MINUTES = 1
DEFAULT_COOLDOWN_MINUTES = 30
MAX_MARKET_DURATION_SECONDS = 3600     # 1 hour hard cap (testing phase)

CURRENCY_SYMBOL = "ŧ"
CURRENCY_NAME = "points"
STARTING_BALANCE = 1000.0
MAX_OPTIMISTIC_RETRIES = 5
OPTIMISTIC_RETRY_BASE_MS = 2

# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

PAGINATION_DEFAULT_LIMIT = 50
PAGINATION_MAX_LIMIT = 100


def clamp_pagination(limit: Optional[int], offset: Optional[int]) -> tuple:
    """Clamp and validate pagination parameters."""
    if limit is None or limit < 1:
        limit = PAGINATION_DEFAULT_LIMIT
    if limit > PAGINATION_MAX_LIMIT:
        limit = PAGINATION_MAX_LIMIT
    if offset is None or offset < 0:
        offset = 0
    return limit, offset


# ---------------------------------------------------------------------------
# UUID validation
# ---------------------------------------------------------------------------

def validate_uuid(value: str, param_name: str = "id") -> None:
    """Validate that a string is a valid UUID.  Raises APIError(400) if not."""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        raise APIError(
            status_code=400,
            message=f"Invalid {param_name}: '{value}' is not a valid UUID",
            code=ErrorCode.INVALID_INPUT,
        )


# ---------------------------------------------------------------------------
# Verification helpers (agent claim flow)
# ---------------------------------------------------------------------------

VERIFICATION_WORDS = [
    "crab", "shell", "reef", "wave", "tide", "coral", "kelp", "pearl",
    "anchor", "lobster", "orca", "squid", "trout", "shark", "whale",
    "dune", "marsh", "delta", "fjord", "shoal",
]


def generate_verification_code() -> str:
    """Cryptographically-secure verification code like 'crab-reef-A1B2C3D4'."""
    _alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    word1 = secrets.choice(VERIFICATION_WORDS)
    word2 = secrets.choice(VERIFICATION_WORDS)
    chars = "".join(secrets.choice(_alphabet) for _ in range(8))
    return f"{word1}-{word2}-{chars}"


def is_valid_twitter_url(url: str) -> bool:
    pattern = r"^https?://(www\.)?(twitter\.com|x\.com)/[a-zA-Z0-9_]+/status/\d+"
    return bool(re.match(pattern, url))


def extract_tweet_id(url: str) -> Optional[str]:
    pattern = r"(?:twitter\.com|x\.com)/[a-zA-Z0-9_]+/status/(\d+)"
    match = re.search(pattern, url)
    return match.group(1) if match else None


def extract_twitter_handle(url: str) -> Optional[str]:
    pattern = r"(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/"
    match = re.search(pattern, url)
    return match.group(1) if match else None


async def fetch_tweet(tweet_id: str) -> dict:
    """Fetch tweet via Twitter syndication API (no auth required)."""
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=x"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url)
            if response.status_code == 404:
                raise APIError(status_code=400, message="Tweet not found. It may be deleted or private.", code=ErrorCode.INVALID_INPUT)
            if response.status_code != 200:
                raise APIError(status_code=502, message=f"Failed to fetch tweet (Twitter returned {response.status_code})", code=ErrorCode.BAD_GATEWAY)
            data = response.json()
            if not data or "text" not in data:
                raise APIError(status_code=400, message="Tweet not accessible. It may be from a private or suspended account.", code=ErrorCode.INVALID_INPUT)
            return data
        except httpx.TimeoutException:
            raise APIError(status_code=504, message="Timeout while fetching tweet. Please try again.", code=ErrorCode.GATEWAY_TIMEOUT)
        except httpx.RequestError as e:
            raise APIError(status_code=502, message=f"Network error while fetching tweet: {str(e)}", code=ErrorCode.BAD_GATEWAY)


def verify_tweet_contains_code(tweet_text: str, code: str) -> bool:
    return code.lower() in tweet_text.lower()


# ---------------------------------------------------------------------------
# Market transition helpers
# ---------------------------------------------------------------------------

def maybe_transition_market(market: dict, market_id: str) -> None:
    """Transition a single OPEN market to RESOLVING if past closes_at."""
    from models import MarketStatus
    db = get_db()
    if market["status"] != MarketStatus.OPEN:
        return
    now = datetime.now(timezone.utc)
    if market["closes_at"] <= now:
        db.update_market_status(market_id, MarketStatus.RESOLVING)
        market["status"] = MarketStatus.RESOLVING


def bg_transition_expired_markets() -> None:
    """Background callback: batch-transition expired markets."""
    try:
        db = get_db()
        count = db.transition_expired_markets()
        if count:
            logger.info("Background transition: %d market(s) moved OPEN → RESOLVING", count)
    except Exception:
        logger.exception("Background market transition failed")


# ---------------------------------------------------------------------------
# Payout helper
# ---------------------------------------------------------------------------

def calculate_and_distribute_payouts(market_id: str, outcome) -> int:
    """Calculate and distribute payouts for a resolved market."""
    from models import Outcome
    db = get_db()
    positions = db.get_market_positions(market_id)
    paid = 0
    for pos in positions:
        winning_shares = pos["yes_shares"] if outcome == Outcome.YES else pos["no_shares"]
        if winning_shares > 0:
            payout = winning_shares
            db.update_user_balance(pos["user_id"], payout)
            db.update_user_profit(pos["user_id"], payout - pos["total_invested"])
            paid += 1
    return paid


# ---------------------------------------------------------------------------
# Cache header helper
# ---------------------------------------------------------------------------

def set_cache_headers(response, etag: str, last_modified: datetime) -> None:
    """Set ETag, Last-Modified, and Cache-Control headers."""
    response.headers["ETag"] = etag
    response.headers["Last-Modified"] = last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT")
    response.headers["Cache-Control"] = "public, max-age=5"
