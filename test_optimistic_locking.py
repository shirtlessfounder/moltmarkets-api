"""
Tests for optimistic locking on concurrent trades (Issue #74).

Validates that the compare-and-swap (CAS) mechanism on the market
version column prevents race conditions when two agents bet, sell,
or when resolution races with a late trade.

Run with: python -m pytest test_optimistic_locking.py -v
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest

# ---------------------------------------------------------------------------
# Bootstrap in-memory storage (no DATABASE_URL → memory mode)
# ---------------------------------------------------------------------------
import os
os.environ.pop("DATABASE_URL", None)

from api import (
    Storage,
    STARTING_BALANCE,
)
from models import MarketStatus, Outcome
from cpmm import CpmmState, calculate_cpmm_purchase, calculate_cpmm_sale


# =============================================================================
# Helpers
# =============================================================================

def _fresh_storage() -> Storage:
    """Return a clean in-memory Storage instance."""
    s = Storage.__new__(Storage)
    s._use_memory = True
    s._markets = {}
    s._users = {}
    s._bets = {}
    s._positions = {}
    return s


def _seed_market(storage: Storage, market_id: str = None,
                 creator_id: str = "creator-1",
                 liquidity: float = 100.0,
                 closes_in_seconds: int = 3600) -> dict:
    """Create a creator user and an open market in *storage*."""
    if market_id is None:
        market_id = str(uuid.uuid4())

    # Ensure creator exists
    if not storage.get_user(creator_id):
        storage.create_user(creator_id, f"creator_{creator_id}",
                            balance=STARTING_BALANCE,
                            status="claimed")

    closes = datetime.now(timezone.utc) + timedelta(seconds=closes_in_seconds)
    market = storage.create_market(
        market_id=market_id,
        creator_id=creator_id,
        title="Will it rain tomorrow?",
        description="Test market",
        closes_at=closes,
        initial_liquidity=liquidity,
    )
    return market


def _seed_trader(storage: Storage, user_id: str = None,
                 balance: float = STARTING_BALANCE) -> dict:
    if user_id is None:
        user_id = str(uuid.uuid4())
    return storage.create_user(user_id, f"trader_{user_id}",
                               balance=balance, status="claimed")


# =============================================================================
# Storage-level CAS tests
# =============================================================================

class TestUpdateMarketPoolVersioned:
    """Direct tests for the versioned pool update method."""

    def test_success_increments_version(self):
        s = _fresh_storage()
        m = _seed_market(s)
        assert m["version"] == 1

        new_pool = {"YES": 90.0, "NO": 110.0}
        result = s.update_market_pool_versioned(
            m["id"], new_pool, 0.5, 10.0, expected_version=1,
        )
        assert result == 2

        updated = s.get_market(m["id"])
        assert updated["pool"] == new_pool
        assert updated["version"] == 2
        assert updated["total_volume"] == 10.0

    def test_conflict_returns_none(self):
        s = _fresh_storage()
        m = _seed_market(s)

        # Simulate another writer bumping the version first
        s._markets[m["id"]]["version"] = 5

        result = s.update_market_pool_versioned(
            m["id"], {"YES": 1, "NO": 1}, 0.5, 0.0, expected_version=1,
        )
        assert result is None
        # Pool should be unchanged
        assert s.get_market(m["id"])["pool"]["YES"] == 100.0

    def test_sequential_updates(self):
        s = _fresh_storage()
        m = _seed_market(s)

        for expected_v in range(1, 6):
            new_ver = s.update_market_pool_versioned(
                m["id"],
                {"YES": 100.0 - expected_v, "NO": 100.0 + expected_v},
                0.5, 1.0,
                expected_version=expected_v,
            )
            assert new_ver == expected_v + 1

        assert s.get_market(m["id"])["version"] == 6


class TestResolveMarketVersioned:
    """Direct tests for the versioned resolve method."""

    def test_success(self):
        s = _fresh_storage()
        m = _seed_market(s)

        new_ver = s.resolve_market_versioned(m["id"], Outcome.YES, expected_version=1)
        assert new_ver == 2

        updated = s.get_market(m["id"])
        assert updated["status"] == MarketStatus.RESOLVED
        assert updated["resolution"] == Outcome.YES
        assert updated["resolved_at"] is not None

    def test_conflict(self):
        s = _fresh_storage()
        m = _seed_market(s)
        s._markets[m["id"]]["version"] = 3  # simulate concurrent trade

        result = s.resolve_market_versioned(m["id"], Outcome.NO, expected_version=1)
        assert result is None
        assert s.get_market(m["id"])["status"] == MarketStatus.OPEN


# =============================================================================
# Simulated concurrent bet scenario
# =============================================================================

class TestConcurrentBets:
    """Simulate two bets racing on the same market at the storage level."""

    def test_one_wins_one_loses(self):
        """Two callers read version=1; first CAS wins, second gets None."""
        s = _fresh_storage()
        m = _seed_market(s)

        pool = m["pool"].copy()
        state = CpmmState(pool=pool, p=m["p"])

        # Both compute against the same snapshot
        r1 = calculate_cpmm_purchase(state, 10.0, "YES")
        r2 = calculate_cpmm_purchase(state, 20.0, "NO")

        # First writer wins
        v1 = s.update_market_pool_versioned(
            m["id"], r1["new_pool"], r1["new_p"], 10.0, expected_version=1,
        )
        assert v1 == 2

        # Second writer loses (stale version=1)
        v2 = s.update_market_pool_versioned(
            m["id"], r2["new_pool"], r2["new_p"], 20.0, expected_version=1,
        )
        assert v2 is None

    def test_retry_succeeds_with_fresh_state(self):
        """After a conflict the second caller re-reads and retries successfully."""
        s = _fresh_storage()
        m = _seed_market(s)

        pool = m["pool"].copy()
        state = CpmmState(pool=pool, p=m["p"])

        r1 = calculate_cpmm_purchase(state, 10.0, "YES")
        r2_stale = calculate_cpmm_purchase(state, 20.0, "NO")

        # Writer 1 commits
        s.update_market_pool_versioned(
            m["id"], r1["new_pool"], r1["new_p"], 10.0, expected_version=1,
        )

        # Writer 2 fails on stale version
        assert s.update_market_pool_versioned(
            m["id"], r2_stale["new_pool"], r2_stale["new_p"], 20.0,
            expected_version=1,
        ) is None

        # Writer 2 re-reads fresh state and retries
        fresh = s.get_market(m["id"])
        assert fresh["version"] == 2
        state2 = CpmmState(pool=fresh["pool"].copy(), p=fresh["p"])
        r2_fresh = calculate_cpmm_purchase(state2, 20.0, "NO")

        v2 = s.update_market_pool_versioned(
            m["id"], r2_fresh["new_pool"], r2_fresh["new_p"], 20.0,
            expected_version=2,
        )
        assert v2 == 3

    def test_concurrent_sell_and_buy(self):
        """A buy and a sell racing — one must retry."""
        s = _fresh_storage()
        m = _seed_market(s)

        # First, do a buy so there are shares to sell
        state = CpmmState(pool=m["pool"].copy(), p=m["p"])
        buy = calculate_cpmm_purchase(state, 50.0, "YES")
        s.update_market_pool_versioned(
            m["id"], buy["new_pool"], buy["new_p"], 50.0, expected_version=1,
        )

        # Now read fresh state (version=2) for both a new buy and a sell
        fresh = s.get_market(m["id"])
        state2 = CpmmState(pool=fresh["pool"].copy(), p=fresh["p"])

        buy2 = calculate_cpmm_purchase(state2, 10.0, "NO")
        sell = calculate_cpmm_sale(state2, 5.0, "YES")

        # Buy lands first
        v_buy = s.update_market_pool_versioned(
            m["id"], buy2["new_pool"], buy2["new_p"], 10.0,
            expected_version=2,
        )
        assert v_buy == 3

        # Sell with stale version fails
        v_sell = s.update_market_pool_versioned(
            m["id"], sell["new_pool"], sell["new_p"], 0.0,
            expected_version=2,
        )
        assert v_sell is None


# =============================================================================
# Resolution vs trade race
# =============================================================================

class TestResolutionRace:
    """Ensure resolution and a last-minute bet don't corrupt state."""

    def test_resolve_blocks_stale_trade(self):
        """If resolution lands first, a stale trade CAS fails."""
        s = _fresh_storage()
        m = _seed_market(s)

        # Trade reads version=1
        state = CpmmState(pool=m["pool"].copy(), p=m["p"])
        trade = calculate_cpmm_purchase(state, 30.0, "YES")

        # Resolution commits first
        s.resolve_market_versioned(m["id"], Outcome.YES, expected_version=1)

        # Stale trade CAS fails
        v = s.update_market_pool_versioned(
            m["id"], trade["new_pool"], trade["new_p"], 30.0,
            expected_version=1,
        )
        assert v is None

    def test_trade_blocks_stale_resolution(self):
        """If a trade lands first, a stale resolution CAS fails."""
        s = _fresh_storage()
        m = _seed_market(s)

        # Resolution reads version=1
        # Trade commits first
        state = CpmmState(pool=m["pool"].copy(), p=m["p"])
        trade = calculate_cpmm_purchase(state, 15.0, "NO")
        s.update_market_pool_versioned(
            m["id"], trade["new_pool"], trade["new_p"], 15.0,
            expected_version=1,
        )

        # Stale resolution CAS fails
        v = s.resolve_market_versioned(m["id"], Outcome.NO, expected_version=1)
        assert v is None
        # Market should still be OPEN (trade didn't change status)
        assert s.get_market(m["id"])["status"] == MarketStatus.OPEN


# =============================================================================
# Version field presence
# =============================================================================

class TestVersionFieldPresence:
    """Verify version is correctly threaded through create/read paths."""

    def test_new_market_has_version_1(self):
        s = _fresh_storage()
        m = _seed_market(s)
        assert m["version"] == 1

    def test_get_market_includes_version(self):
        s = _fresh_storage()
        m = _seed_market(s)
        fetched = s.get_market(m["id"])
        assert "version" in fetched
        assert fetched["version"] == 1


# =============================================================================
# Run
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
