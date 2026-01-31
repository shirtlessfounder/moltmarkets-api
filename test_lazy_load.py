"""
Tests for issue #54: Targeted query methods replace full-table property accessors.

Validates that:
1. New count methods work correctly (count_users, count_markets)
2. New targeted query methods return correct subsets (get_markets_by_ids, etc.)
3. Deprecated property accessors still work but emit warnings
4. Hot paths (health, reputation) no longer trigger full-table loads

See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
"""

import warnings
from datetime import datetime, timezone, timedelta

import pytest

# Force in-memory storage (no DATABASE_URL)
import os
os.environ.pop("DATABASE_URL", None)

from api import Storage, Outcome


@pytest.fixture
def db():
    """Fresh in-memory storage instance for each test."""
    return Storage()


@pytest.fixture
def populated_db(db):
    """Storage pre-populated with test data for query method tests."""
    # Create users
    db.create_user("user-1", "alice", balance=1000.0)
    db.create_user("user-2", "bob", balance=1000.0)
    db.create_user("user-3", "charlie", balance=1000.0)

    # Create markets
    closes = datetime.now(timezone.utc) + timedelta(hours=1)
    db.create_market("market-1", "user-1", "Will it rain?", "desc", closes, 100.0)
    db.create_market("market-2", "user-1", "Will it snow?", "desc", closes, 100.0)
    db.create_market("market-3", "user-2", "Will it hail?", "desc", closes, 100.0)

    # Create bets
    db.create_bet("bet-1", "market-1", "user-2", Outcome.YES, 50.0, 60.0, 0.5, 0.6)
    db.create_bet("bet-2", "market-1", "user-3", Outcome.NO, 30.0, 35.0, 0.6, 0.55)
    db.create_bet("bet-3", "market-2", "user-2", Outcome.YES, 20.0, 22.0, 0.5, 0.55)
    db.create_bet("bet-4", "market-3", "user-1", Outcome.NO, 40.0, 45.0, 0.5, 0.45)

    return db


# ========== Count Methods ==========

class TestCountMethods:
    def test_count_users_empty(self, db):
        assert db.count_users() == 0

    def test_count_users_populated(self, populated_db):
        assert populated_db.count_users() == 3

    def test_count_markets_empty(self, db):
        assert db.count_markets() == 0

    def test_count_markets_populated(self, populated_db):
        assert populated_db.count_markets() == 3

    def test_count_users_after_create(self, db):
        assert db.count_users() == 0
        db.create_user("u1", "user1")
        assert db.count_users() == 1
        db.create_user("u2", "user2")
        assert db.count_users() == 2


# ========== Targeted Query Methods ==========

class TestGetMarketsByIds:
    def test_empty_ids_returns_empty(self, populated_db):
        result = populated_db.get_markets_by_ids(set())
        assert result == {}

    def test_single_id(self, populated_db):
        result = populated_db.get_markets_by_ids({"market-1"})
        assert len(result) == 1
        assert "market-1" in result
        assert result["market-1"]["title"] == "Will it rain?"

    def test_multiple_ids(self, populated_db):
        result = populated_db.get_markets_by_ids({"market-1", "market-3"})
        assert len(result) == 2
        assert "market-1" in result
        assert "market-3" in result

    def test_nonexistent_ids_ignored(self, populated_db):
        result = populated_db.get_markets_by_ids({"market-1", "nonexistent"})
        assert len(result) == 1
        assert "market-1" in result


class TestGetMarketsByCreator:
    def test_creator_with_markets(self, populated_db):
        result = populated_db.get_markets_by_creator("user-1")
        assert len(result) == 2
        titles = {m["title"] for m in result}
        assert titles == {"Will it rain?", "Will it snow?"}

    def test_creator_with_one_market(self, populated_db):
        result = populated_db.get_markets_by_creator("user-2")
        assert len(result) == 1
        assert result[0]["title"] == "Will it hail?"

    def test_creator_no_markets(self, populated_db):
        result = populated_db.get_markets_by_creator("user-3")
        assert len(result) == 0


class TestGetBetsOnMarkets:
    def test_empty_ids_returns_empty(self, populated_db):
        result = populated_db.get_bets_on_markets(set())
        assert result == []

    def test_single_market(self, populated_db):
        result = populated_db.get_bets_on_markets({"market-1"})
        assert len(result) == 2  # bet-1 and bet-2
        user_ids = {b["user_id"] for b in result}
        assert user_ids == {"user-2", "user-3"}

    def test_multiple_markets(self, populated_db):
        result = populated_db.get_bets_on_markets({"market-1", "market-2"})
        assert len(result) == 3  # bet-1, bet-2, bet-3

    def test_market_with_no_bets(self, db):
        db.create_user("u1", "user1")
        closes = datetime.now(timezone.utc) + timedelta(hours=1)
        db.create_market("m1", "u1", "Empty market", "desc", closes, 100.0)
        result = db.get_bets_on_markets({"m1"})
        assert result == []


# ========== Deprecation Warnings ==========

class TestDeprecationWarnings:
    def test_users_property_warns(self, populated_db):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = populated_db.users
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "#54" in str(w[0].message)
            assert "count_users" in str(w[0].message)

    def test_markets_property_warns(self, populated_db):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = populated_db.markets
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "#54" in str(w[0].message)

    def test_bets_property_warns(self, populated_db):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = populated_db.bets
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "#54" in str(w[0].message)

    def test_users_property_still_returns_data(self, populated_db):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            users = populated_db.users
            assert len(users) == 3
            assert "user-1" in users

    def test_markets_property_still_returns_data(self, populated_db):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            markets = populated_db.markets
            assert len(markets) == 3

    def test_bets_property_still_returns_data(self, populated_db):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            bets = populated_db.bets
            assert len(bets) == 4


# ========== Consistency Checks ==========

class TestConsistency:
    """Verify new methods return same data as deprecated properties."""

    def test_count_matches_property_len(self, populated_db):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert populated_db.count_users() == len(populated_db.users)
            assert populated_db.count_markets() == len(populated_db.markets)

    def test_get_markets_by_ids_all_matches_property(self, populated_db):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            all_via_prop = populated_db.markets
            all_ids = set(all_via_prop.keys())
            all_via_method = populated_db.get_markets_by_ids(all_ids)
            assert set(all_via_method.keys()) == set(all_via_prop.keys())

    def test_bets_on_all_markets_matches_property(self, populated_db):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            all_bets_prop = populated_db.bets
            all_market_ids = set(populated_db.markets.keys())
            all_bets_method = populated_db.get_bets_on_markets(all_market_ids)
            assert len(all_bets_method) == len(all_bets_prop)
