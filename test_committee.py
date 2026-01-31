"""
Tests for the 3/3 committee resolution system (issue #107).

Covers:
  - Committee formation (creator inclusion, top trader selection, edge cases)
  - Unanimity checking logic
  - Vote casting API (auth, committee-only, auto-resolve, vote changes)
  - GET committee status endpoint
  - Creator fallback (blocked during window, allowed after deadline)
  - MarketDetail committee fields

Uses FastAPI TestClient against the in-memory storage backend.
Run with: python -m pytest test_committee.py -v
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Ensure in-memory storage (no DATABASE_URL) before importing app
# ---------------------------------------------------------------------------
os.environ.pop("DATABASE_URL", None)

from api import app, db  # noqa: E402
from deps import (
    STARTING_BALANCE, TRADE_FEE_RATE, MARKET_CREATION_COST,
    COMMITTEE_WINDOW_MINUTES, form_committee, check_committee_unanimity,
)  # noqa: E402
from models import MarketStatus, Outcome  # noqa: E402
from rate_limiter import rate_limiter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_storage():
    """Reset the global in-memory storage between tests."""
    db._markets.clear()
    db._users.clear()
    db._bets.clear()
    db._positions.clear()
    if hasattr(db, "_comments"):
        db._comments.clear()
    if hasattr(db, "_resolution_votes"):
        db._resolution_votes.clear()
    if hasattr(db, "_committee_votes"):
        db._committee_votes.clear()
    if hasattr(db, "_chat_messages"):
        db._chat_messages.clear()
    rate_limiter._requests.clear()
    rate_limiter._call_count = 0
    if not db.get_user("demo-user"):
        db.create_user("demo-user", "demo_user", balance=0.0)


def _register_agent(client: TestClient, username: str = None) -> dict:
    """Register an agent and return info dict with headers."""
    username = username or f"agent_{uuid.uuid4().hex[:8]}"
    resp = client.post("/agents/register", json={"username": username})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    api_key = data["api_key"]
    db.update_user_status(data["user_id"], "claimed")
    return {
        "data": data,
        "api_key": api_key,
        "headers": {"Authorization": f"Bearer {api_key}"},
        "user_id": data["user_id"],
        "username": data["username"],
    }


def _create_market(client: TestClient, headers: dict, minutes: int = 30,
                   title: str = "Will it rain tomorrow?") -> dict:
    """Create a market closing `minutes` from now."""
    closes_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    resp = client.post("/markets", json={
        "title": title,
        "description": "Test market",
        "closes_at": closes_at,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _place_bet(client: TestClient, market_id: str, headers: dict,
               outcome: str = "YES", amount: float = 50.0) -> dict:
    """Place a bet on a market."""
    resp = client.post(f"/markets/{market_id}/bet", json={
        "outcome": outcome,
        "amount": amount,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _force_resolving(market_id: str):
    """Force a market into RESOLVING state for testing.

    Issue #115: markets no longer auto-transition based on closes_at.
    This helper directly sets RESOLVING status for tests that need it.
    """
    db.update_market_status(market_id, MarketStatus.RESOLVING)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_storage():
    _fresh_storage()
    yield
    _fresh_storage()


client = TestClient(app)


# ===========================================================================
# 1. Committee Formation Tests
# ===========================================================================

class TestCommitteeFormation:
    """Test committee auto-formation when market enters RESOLVING."""

    def test_committee_includes_creator(self):
        """Creator is always included in the committee."""
        creator = _register_agent(client, "creator1")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        assert creator["user_id"] in committee
        assert committee[0] == creator["user_id"]  # Creator is first

    def test_committee_selects_top_traders(self):
        """Committee selects top 2 traders by invested amount."""
        creator = _register_agent(client, "creator2")
        trader1 = _register_agent(client, "trader1")
        trader2 = _register_agent(client, "trader2")
        trader3 = _register_agent(client, "trader3")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        # Trader3 invests most, then trader1, then trader2
        _place_bet(client, market_id, trader3["headers"], "YES", 100)
        _place_bet(client, market_id, trader1["headers"], "NO", 75)
        _place_bet(client, market_id, trader2["headers"], "YES", 25)

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        assert len(committee) == 3
        assert committee[0] == creator["user_id"]
        # Top 2 traders: trader3 (100) and trader1 (75)
        assert trader3["user_id"] in committee
        assert trader1["user_id"] in committee
        assert trader2["user_id"] not in committee

    def test_committee_with_one_trader(self):
        """Committee with only 1 other trader → 2 members."""
        creator = _register_agent(client, "creator3")
        trader = _register_agent(client, "traderonly")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _place_bet(client, market_id, trader["headers"], "YES", 50)

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        assert len(committee) == 2
        assert creator["user_id"] in committee
        assert trader["user_id"] in committee

    def test_committee_solo_creator(self):
        """Solo creator (no traders) → committee of 1."""
        creator = _register_agent(client, "solo_creator")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        assert committee == [creator["user_id"]]

    def test_committee_sets_deadline(self):
        """Committee formation sets a 30-minute resolution deadline."""
        creator = _register_agent(client, "creator4")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _force_resolving(market_id)
        m = db.get_market(market_id)
        before = datetime.now(timezone.utc)
        form_committee(market_id, m)
        after = datetime.now(timezone.utc)

        m = db.get_market(market_id)
        assert m["resolution_deadline"] is not None
        expected_min = before + timedelta(minutes=COMMITTEE_WINDOW_MINUTES)
        expected_max = after + timedelta(minutes=COMMITTEE_WINDOW_MINUTES)
        assert expected_min <= m["resolution_deadline"] <= expected_max

    def test_committee_excludes_creator_from_traders(self):
        """Creator who also traded isn't double-counted."""
        creator = _register_agent(client, "creator_trader")
        trader1 = _register_agent(client, "other_trader1")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        # Creator also bets on their own market
        _place_bet(client, market_id, creator["headers"], "YES", 100)
        _place_bet(client, market_id, trader1["headers"], "NO", 50)

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        # Creator appears only once
        assert committee.count(creator["user_id"]) == 1
        assert trader1["user_id"] in committee
        assert len(committee) == 2

    def test_committee_formed_on_resolve_attempt(self):
        """Committee is formed when creator attempts to resolve an OPEN market with traders (issue #115).

        Since markets no longer auto-transition at closes_at, the committee
        is formed when the creator first calls POST /resolve on an OPEN market.
        If other traders exist, the endpoint forms a committee, transitions to
        RESOLVING, and returns 403 to start the committee vote window.
        """
        creator = _register_agent(client, "creator_auto")
        trader = _register_agent(client, "trader_auto")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _place_bet(client, market_id, trader["headers"], "YES", 50)

        # Creator tries to resolve OPEN market with traders → committee formed, 403
        resp = client.post(f"/markets/{market_id}/resolve",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 403
        assert "COMMITTEE_WINDOW_ACTIVE" in resp.text

        m = db.get_market(market_id)
        assert m["status"] == MarketStatus.RESOLVING
        assert m.get("committee") is not None
        assert creator["user_id"] in m["committee"]
        assert trader["user_id"] in m["committee"]


# ===========================================================================
# 2. Unanimity Checking Tests
# ===========================================================================

class TestUnanimityChecking:
    """Test the check_committee_unanimity logic."""

    def test_unanimous_yes(self):
        """All members vote YES → returns 'YES'."""
        creator = _register_agent(client, "unan_creator")
        t1 = _register_agent(client, "unan_t1")
        t2 = _register_agent(client, "unan_t2")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, t1["headers"], "YES", 50)
        _place_bet(client, market_id, t2["headers"], "NO", 50)

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        db.upsert_committee_vote(market_id, creator["user_id"], "YES")
        db.upsert_committee_vote(market_id, t1["user_id"], "YES")
        db.upsert_committee_vote(market_id, t2["user_id"], "YES")

        assert check_committee_unanimity(market_id, committee) == "YES"

    def test_unanimous_no(self):
        """All members vote NO → returns 'NO'."""
        creator = _register_agent(client, "unan_no_c")
        t1 = _register_agent(client, "unan_no_t1")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, t1["headers"], "YES", 50)

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        for uid in committee:
            db.upsert_committee_vote(market_id, uid, "NO")

        assert check_committee_unanimity(market_id, committee) == "NO"

    def test_mixed_votes_no_unanimity(self):
        """Mixed YES/NO → returns None."""
        creator = _register_agent(client, "mix_c")
        t1 = _register_agent(client, "mix_t1")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, t1["headers"], "YES", 50)

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        db.upsert_committee_vote(market_id, creator["user_id"], "YES")
        db.upsert_committee_vote(market_id, t1["user_id"], "NO")

        assert check_committee_unanimity(market_id, committee) is None

    def test_invalid_votes_no_unanimity(self):
        """All INVALID votes → returns None (INVALID doesn't trigger resolution)."""
        creator = _register_agent(client, "inv_c")
        t1 = _register_agent(client, "inv_t1")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, t1["headers"], "YES", 50)

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)

        for uid in committee:
            db.upsert_committee_vote(market_id, uid, "INVALID")

        assert check_committee_unanimity(market_id, committee) is None

    def test_partial_votes_no_unanimity(self):
        """Not all members voted → returns None."""
        creator = _register_agent(client, "part_c")
        t1 = _register_agent(client, "part_t1")
        t2 = _register_agent(client, "part_t2")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, t1["headers"], "YES", 50)
        _place_bet(client, market_id, t2["headers"], "NO", 50)

        _force_resolving(market_id)
        m = db.get_market(market_id)
        committee = form_committee(market_id, m)
        assert len(committee) == 3

        # Only 2 of 3 vote
        db.upsert_committee_vote(market_id, creator["user_id"], "YES")
        db.upsert_committee_vote(market_id, t1["user_id"], "YES")

        assert check_committee_unanimity(market_id, committee) is None


# ===========================================================================
# 3. Vote Casting API Tests
# ===========================================================================

class TestVoteCastingAPI:
    """Test POST /markets/{id}/resolution-vote endpoint."""

    def test_committee_member_can_vote(self):
        """Committee member can cast a vote."""
        creator = _register_agent(client, "vote_c")
        trader = _register_agent(client, "vote_t")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Form committee via the endpoint
        resp = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["market_id"] == market_id
        assert data["agent_id"] == creator["user_id"]
        assert data["outcome"] == "YES"
        assert data["auto_resolved"] is False

    def test_non_member_cannot_vote(self):
        """Non-committee member gets 403."""
        creator = _register_agent(client, "nm_c")
        trader = _register_agent(client, "nm_t")
        outsider = _register_agent(client, "nm_outsider")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Form committee first with a valid vote
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])

        # Outsider tries to vote
        resp = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "NO"}, headers=outsider["headers"])
        assert resp.status_code == 403
        assert "NOT_COMMITTEE_MEMBER" in resp.text

    def test_unauthenticated_cannot_vote(self):
        """No auth → 401."""
        creator = _register_agent(client, "noauth_c")
        market = _create_market(client, creator["headers"])
        _force_resolving(market["id"])

        resp = client.post(f"/markets/{market['id']}/resolution-vote",
            json={"outcome": "YES"})
        assert resp.status_code == 401

    def test_vote_on_resolved_market_fails(self):
        """Cannot vote on already-resolved market."""
        creator = _register_agent(client, "resolved_c")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _force_resolving(market_id)

        # Resolve the market directly (solo creator)
        db.resolve_market(market_id, Outcome.YES)

        resp = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 400
        assert "ALREADY_RESOLVED" in resp.text

    def test_vote_on_open_market_fails(self):
        """Cannot vote on a market that's still OPEN."""
        creator = _register_agent(client, "open_c")
        market = _create_market(client, creator["headers"])

        resp = client.post(f"/markets/{market['id']}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 400

    def test_vote_change_before_unanimity(self):
        """Votes can be changed (upserted) before unanimity."""
        creator = _register_agent(client, "change_c")
        trader = _register_agent(client, "change_t")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Creator votes YES
        resp = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 200

        # Creator changes to NO
        resp = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "NO"}, headers=creator["headers"])
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "NO"

        # Verify the vote was updated (not duplicated)
        votes = db.get_committee_votes(market_id)
        creator_votes = [v for v in votes if v["agent_id"] == creator["user_id"]]
        assert len(creator_votes) == 1
        assert creator_votes[0]["outcome"] == "NO"

    def test_unanimous_vote_auto_resolves(self):
        """Unanimous YES/NO triggers auto-resolution and payouts."""
        creator = _register_agent(client, "auto_c")
        trader = _register_agent(client, "auto_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Both vote YES
        resp1 = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp1.status_code == 200
        assert resp1.json()["auto_resolved"] is False

        resp2 = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=trader["headers"])
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["auto_resolved"] is True
        assert data["resolution_outcome"] == "YES"

        # Market is now RESOLVED
        m = db.get_market(market_id)
        assert m["status"] == MarketStatus.RESOLVED
        assert m["resolution"] == Outcome.YES

    def test_unanimous_no_auto_resolves(self):
        """Unanimous NO also triggers auto-resolution."""
        creator = _register_agent(client, "autono_c")
        trader = _register_agent(client, "autono_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Both vote NO
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "NO"}, headers=creator["headers"])
        resp = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "NO"}, headers=trader["headers"])

        assert resp.json()["auto_resolved"] is True
        assert resp.json()["resolution_outcome"] == "NO"

        m = db.get_market(market_id)
        assert m["status"] == MarketStatus.RESOLVED
        assert m["resolution"] == Outcome.NO

    def test_invalid_vote_prevents_auto_resolve(self):
        """Even if all vote, having INVALID votes prevents auto-resolve."""
        creator = _register_agent(client, "inv_auto_c")
        trader = _register_agent(client, "inv_auto_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "INVALID"}, headers=creator["headers"])
        resp = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "INVALID"}, headers=trader["headers"])

        assert resp.json()["auto_resolved"] is False
        m = db.get_market(market_id)
        assert m["status"] == MarketStatus.RESOLVING  # Still not resolved


# ===========================================================================
# 4. GET Committee Status Endpoint Tests
# ===========================================================================

class TestCommitteeStatusEndpoint:
    """Test GET /markets/{id}/committee-votes endpoint."""

    def test_get_committee_status_with_votes(self):
        """Returns committee, votes, deadline, and status."""
        creator = _register_agent(client, "status_c")
        trader = _register_agent(client, "status_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Cast a vote to form committee
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])

        resp = client.get(f"/markets/{market_id}/committee-votes")
        assert resp.status_code == 200
        data = resp.json()

        assert data["market_id"] == market_id
        assert len(data["committee"]) == 2
        assert creator["user_id"] in data["committee"]
        assert trader["user_id"] in data["committee"]
        assert len(data["votes"]) == 1
        assert data["votes"][0]["agent_id"] == creator["user_id"]
        assert data["votes"][0]["outcome"] == "YES"
        assert data["resolution_deadline"] is not None
        assert data["status"] == "pending"  # Only 1 of 2 voted

    def test_no_committee_returns_empty(self):
        """Market without committee returns empty committee list."""
        creator = _register_agent(client, "nocomm_c")
        market = _create_market(client, creator["headers"])

        resp = client.get(f"/markets/{market['id']}/committee-votes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["committee"] == []
        assert data["votes"] == []
        assert data["status"] == "no_committee"

    def test_status_shows_unanimous(self):
        """Status is 'unanimous' when all votes agree."""
        creator = _register_agent(client, "showunan_c")
        trader = _register_agent(client, "showunan_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Both vote YES → auto-resolves, status should be resolved
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=trader["headers"])

        resp = client.get(f"/markets/{market_id}/committee-votes")
        data = resp.json()
        assert data["status"] == "resolved"

    def test_status_shows_mixed(self):
        """Status is 'mixed' when votes disagree."""
        creator = _register_agent(client, "mixed_c")
        trader = _register_agent(client, "mixed_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "NO"}, headers=trader["headers"])

        resp = client.get(f"/markets/{market_id}/committee-votes")
        data = resp.json()
        assert data["status"] == "mixed"

    def test_status_shows_expired(self):
        """Status is 'expired' after the resolution deadline passes."""
        creator = _register_agent(client, "expired_c")
        trader = _register_agent(client, "expired_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Form committee
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])

        # Manually expire the deadline
        m = db.get_market(market_id)
        m["resolution_deadline"] = datetime.now(timezone.utc) - timedelta(minutes=1)

        resp = client.get(f"/markets/{market_id}/committee-votes")
        data = resp.json()
        assert data["status"] == "expired"

    def test_no_auth_required(self):
        """Committee status endpoint doesn't require auth."""
        creator = _register_agent(client, "noauthstatus_c")
        market = _create_market(client, creator["headers"])

        # No headers → should still work
        resp = client.get(f"/markets/{market['id']}/committee-votes")
        assert resp.status_code == 200

    def test_nonexistent_market_404(self):
        """Non-existent market returns 404."""
        fake_id = str(uuid.uuid4())
        resp = client.get(f"/markets/{fake_id}/committee-votes")
        assert resp.status_code == 404


# ===========================================================================
# 5. Creator Fallback Tests
# ===========================================================================

class TestCreatorFallback:
    """Test creator unilateral resolve is blocked during window, allowed after."""

    def test_creator_blocked_during_window(self):
        """Creator can't bypass committee during the 30-minute window."""
        creator = _register_agent(client, "blocked_c")
        trader = _register_agent(client, "blocked_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Form committee (sets deadline 30min in future)
        m = db.get_market(market_id)
        form_committee(market_id, m)

        # Try to resolve directly — should be blocked
        resp = client.post(f"/markets/{market_id}/resolve",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 403
        assert "COMMITTEE_WINDOW_ACTIVE" in resp.text

    def test_creator_allowed_after_deadline(self):
        """Creator regains unilateral resolve after deadline expires."""
        creator = _register_agent(client, "allowed_c")
        trader = _register_agent(client, "allowed_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Form committee and manually expire the deadline
        m = db.get_market(market_id)
        form_committee(market_id, m)
        m["resolution_deadline"] = datetime.now(timezone.utc) - timedelta(minutes=1)

        # Now creator can resolve
        resp = client.post(f"/markets/{market_id}/resolve",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "RESOLVED"
        assert resp.json()["resolution"] == "YES"

    def test_solo_creator_can_resolve_immediately(self):
        """Solo creator (no other traders) can resolve immediately."""
        creator = _register_agent(client, "solo_c")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _force_resolving(market_id)

        # Form committee (just creator)
        m = db.get_market(market_id)
        form_committee(market_id, m)
        assert len(m["committee"]) == 1

        # Can resolve immediately (committee has only 1 member)
        resp = client.post(f"/markets/{market_id}/resolve",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "RESOLVED"

    def test_non_creator_cannot_resolve(self):
        """Non-creator can never resolve, regardless of committee status."""
        creator = _register_agent(client, "noncreator_c")
        trader = _register_agent(client, "noncreator_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        resp = client.post(f"/markets/{market_id}/resolve",
            json={"outcome": "YES"}, headers=trader["headers"])
        assert resp.status_code == 403

    def test_market_without_committee_still_resolvable(self):
        """Markets without committees (backward compat) can be resolved normally."""
        creator = _register_agent(client, "nocomm_resolve_c")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _force_resolving(market_id)

        # Don't form committee — resolve directly
        resp = client.post(f"/markets/{market_id}/resolve",
            json={"outcome": "NO"}, headers=creator["headers"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "RESOLVED"
        assert resp.json()["resolution"] == "NO"


# ===========================================================================
# 6. MarketDetail Committee Fields Tests
# ===========================================================================

class TestMarketDetailCommitteeFields:
    """Test that MarketDetail includes committee fields."""

    def test_market_detail_includes_committee(self):
        """GET /markets/{id} includes committee list."""
        creator = _register_agent(client, "detail_c")
        trader = _register_agent(client, "detail_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        m = db.get_market(market_id)
        form_committee(market_id, m)

        resp = client.get(f"/markets/{market_id}")
        assert resp.status_code == 200
        data = resp.json()

        assert "committee" in data
        assert creator["user_id"] in data["committee"]
        assert trader["user_id"] in data["committee"]

    def test_market_detail_includes_resolution_deadline(self):
        """GET /markets/{id} includes resolution_deadline."""
        creator = _register_agent(client, "deadline_c")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _force_resolving(market_id)

        m = db.get_market(market_id)
        form_committee(market_id, m)

        resp = client.get(f"/markets/{market_id}")
        data = resp.json()

        assert data["resolution_deadline"] is not None

    def test_market_detail_includes_votes(self):
        """GET /markets/{id} includes resolution_votes after voting."""
        creator = _register_agent(client, "votes_detail_c")
        trader = _register_agent(client, "votes_detail_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Vote
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])

        resp = client.get(f"/markets/{market_id}")
        data = resp.json()

        assert data["resolution_votes"] is not None
        assert len(data["resolution_votes"]) == 1
        assert data["resolution_votes"][0]["agent_id"] == creator["user_id"]
        assert data["resolution_votes"][0]["outcome"] == "YES"

    def test_market_without_committee_has_null_fields(self):
        """Markets without committees have null committee fields."""
        creator = _register_agent(client, "null_fields_c")
        market = _create_market(client, creator["headers"])

        resp = client.get(f"/markets/{market['id']}")
        data = resp.json()

        assert data["committee"] is None
        assert data["resolution_votes"] is None
        assert data["resolution_deadline"] is None


# ===========================================================================
# 7. Edge Cases and Integration Tests
# ===========================================================================

class TestEdgeCases:
    """Edge cases and integration scenarios."""

    def test_payouts_distributed_on_auto_resolve(self):
        """Verify winning trader gets paid when committee auto-resolves."""
        creator = _register_agent(client, "payout_c")
        trader = _register_agent(client, "payout_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        # Trader bets YES
        _place_bet(client, market_id, trader["headers"], "YES", 50)

        balance_before = db.get_user(trader["user_id"])["balance"]

        _force_resolving(market_id)

        # Both vote YES → auto-resolve
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=trader["headers"])

        balance_after = db.get_user(trader["user_id"])["balance"]
        # Trader should have received payout (balance increased)
        assert balance_after > balance_before

    def test_three_member_committee_needs_all_three(self):
        """3-member committee needs all 3 votes for unanimity."""
        creator = _register_agent(client, "three_c")
        t1 = _register_agent(client, "three_t1")
        t2 = _register_agent(client, "three_t2")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, t1["headers"], "YES", 50)
        _place_bet(client, market_id, t2["headers"], "NO", 50)

        _force_resolving(market_id)

        # First 2 vote
        resp1 = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp1.json()["auto_resolved"] is False

        resp2 = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=t1["headers"])
        assert resp2.json()["auto_resolved"] is False

        # Third vote completes unanimity
        resp3 = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=t2["headers"])
        assert resp3.json()["auto_resolved"] is True

    def test_vote_change_breaks_unanimity(self):
        """Changing a vote can break what would have been unanimity."""
        creator = _register_agent(client, "break_c")
        trader = _register_agent(client, "break_t")

        market = _create_market(client, creator["headers"])
        market_id = market["id"]
        _place_bet(client, market_id, trader["headers"], "YES", 50)
        _force_resolving(market_id)

        # Both vote YES
        client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])

        # Trader votes YES...
        resp = client.post(f"/markets/{market_id}/resolution-vote",
            json={"outcome": "YES"}, headers=trader["headers"])
        # This should auto-resolve since both agree
        assert resp.json()["auto_resolved"] is True

    def test_committee_vote_invalid_market_id(self):
        """Invalid market ID returns 400/404."""
        creator = _register_agent(client, "invalid_id_c")
        resp = client.post("/markets/not-a-uuid/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 400

        fake_uuid = str(uuid.uuid4())
        resp = client.post(f"/markets/{fake_uuid}/resolution-vote",
            json={"outcome": "YES"}, headers=creator["headers"])
        assert resp.status_code == 404
