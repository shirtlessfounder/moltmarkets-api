"""
Market resolution committee helpers.

Handles committee formation, unanimity checks, and market state
transitions (OPEN → RESOLVING).

Extracted from deps.py — see issue #128.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from deps import get_db
from models import MarketStatus

logger = logging.getLogger(__name__)

COMMITTEE_WINDOW_MINUTES = 30  # Creator fallback deadline


def form_committee(market_id: str, market: dict) -> list:
    """Form a resolution committee for a market entering RESOLVING state.

    Committee = market creator + up to 2 highest-invested traders on that market.
    Sets committee list and resolution_deadline on the market.
    Returns the committee member list.
    """
    db = get_db()
    creator_id = market["creator_id"]

    # Get all positions on this market, excluding the creator
    positions = db.get_market_positions(market_id)
    other_traders = [
        p for p in positions
        if p["user_id"] != creator_id and p["total_invested"] > 0
    ]

    # Sort by total invested (highest first) — proxy for "highest reputation on this market"
    other_traders.sort(key=lambda p: p["total_invested"], reverse=True)

    # Committee = creator + top 2 traders
    committee = [creator_id]
    for trader in other_traders[:2]:
        committee.append(trader["user_id"])

    # Set deadline: 30 minutes from now
    now = datetime.now(timezone.utc)
    resolution_deadline = now + timedelta(minutes=COMMITTEE_WINDOW_MINUTES)

    db.update_market_committee(market_id, committee, resolution_deadline)
    market["committee"] = committee
    market["resolution_deadline"] = resolution_deadline

    logger.info(
        "Committee formed for market %s: %d members (creator + %d traders), deadline %s",
        market_id, len(committee), len(committee) - 1, resolution_deadline.isoformat(),
    )
    return committee


def check_committee_unanimity(market_id: str, committee: list) -> Optional[str]:
    """Check if all committee members voted unanimously for the same YES/NO outcome.

    Returns the unanimous outcome string ("YES" or "NO") if achieved, else None.
    INVALID votes or mixed votes return None.
    """
    db = get_db()
    votes = db.get_committee_votes(market_id)

    if not votes or len(votes) < len(committee):
        return None  # Not all members have voted

    outcomes = set()
    for vote in votes:
        if vote["agent_id"] in committee:
            outcomes.add(vote["outcome"])

    # Must be exactly one outcome and it must be YES or NO (not INVALID)
    if len(outcomes) == 1:
        outcome = outcomes.pop()
        if outcome in ("YES", "NO"):
            return outcome

    return None


def transition_market_to_resolving(market_id: str, market: dict) -> bool:
    """Transition a single OPEN market to RESOLVING and form its committee.

    Returns True if the transition happened, False if skipped.
    """
    db = get_db()

    if market.get("status") != "OPEN":
        return False

    committee = form_committee(market_id, market)
    db.update_market_status(market_id, MarketStatus.RESOLVING)
    market["status"] = MarketStatus.RESOLVING

    logger.info(
        "Auto-transitioned market %s to RESOLVING (committee: %d members)",
        market_id, len(committee),
    )
    return True


def sweep_expired_markets() -> int:
    """Find all OPEN markets past closes_at and transition them to RESOLVING.

    Returns the number of markets transitioned.
    Called periodically by the background sweep task.
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    transitioned = 0

    markets = db.list_markets()
    for m in markets:
        if m.get("status") != "OPEN":
            continue
        closes_at = m.get("closes_at")
        if not closes_at:
            continue
        # Normalize closes_at to datetime if it's a string
        if isinstance(closes_at, str):
            closes_at = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
        if closes_at.tzinfo is None:
            closes_at = closes_at.replace(tzinfo=timezone.utc)
        if now > closes_at:
            try:
                transition_market_to_resolving(m["id"], m)
                transitioned += 1
            except Exception:
                logger.exception("Failed to transition market %s", m["id"])

    if transitioned > 0:
        logger.info("Sweep: transitioned %d expired markets to RESOLVING", transitioned)
    return transitioned
