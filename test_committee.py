"""
Tests for committee resolution feature (#28).

Run with: python -m pytest test_committee.py -v
"""

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

# Ensure in-memory mode (no DATABASE_URL)
import os
os.environ.pop("DATABASE_URL", None)

from api import app, db, Storage, hash_api_key, generate_api_key, _form_committee, _check_unanimous, _ensure_committee
from models import MarketStatus, Outcome, CommitteeVoteOutcome


def uid():
    """Generate a valid UUID string for tests."""
    return str(uuid.uuid4())


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def fresh_db():
    """Reset in-memory storage before each test."""
    db._markets = {}
    db._users = {}
    db._bets = {}
    db._positions = {}
    if hasattr(db, '_committee_votes'):
        db._committee_votes = {}
    if hasattr(db, '_comments'):
        db._comments = {}
    if hasattr(db, '_resolution_votes'):
        db._resolution_votes = {}
    if hasattr(db, '_chat_messages'):
        db._chat_messages = []
    # Re-create demo user
    db.create_user("demo-user", "demo_user", balance=0.0)
    yield


def create_user(user_id, username, balance=1000.0, status="claimed"):
    """Create a test user with a known API key."""
    api_key = f"mm_test_{username}"
    db.create_user(
        user_id=user_id,
        username=username,
        balance=balance,
        api_key_hash=hash_api_key(api_key),
        status=status,
    )
    return api_key


def create_market(market_id, creator_id, closes_at=None, status="open"):
    """Create a test market."""
    if closes_at is None:
        closes_at = datetime.now(timezone.utc) + timedelta(hours=1)
    market = {
        "id": market_id,
        "title": f"Test Market {market_id}",
        "description": "A test market",
        "status": MarketStatus(status.upper()),
        "closes_at": closes_at,
        "created_at": datetime.now(timezone.utc),
        "resolved_at": None,
        "resolution": None,
        "total_volume": 0.0,
        "creator_id": creator_id,
        "pool": {"YES": 100.0, "NO": 100.0},
        "p": 0.5,
        "committee": None,
        "resolution_deadline": None,
    }
    db._markets[market_id] = market
    return market


def add_bet(market_id, user_id, outcome="YES", amount=50.0):
    """Add a bet for a user on a market."""
    import uuid
    bet_id = str(uuid.uuid4())
    bet = {
        "id": bet_id,
        "market_id": market_id,
        "user_id": user_id,
        "outcome": Outcome(outcome),
        "amount": amount,
        "shares": amount * 1.5,
        "avg_price": amount / (amount * 1.5),
        "probability_before": 0.5,
        "probability_after": 0.55,
        "created_at": datetime.now(timezone.utc),
    }
    db._bets[bet_id] = bet
    return bet


client = TestClient(app)


# =============================================================================
# Committee Formation Tests
# =============================================================================

class TestCommitteeFormation:
    def test_committee_includes_creator(self):
        """Creator is always on the committee."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        create_user(cid, "creator1")
        create_user(t1id, "trader1")
        create_user(t2id, "trader2")
        market = create_market(mid, cid, status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        committee = _form_committee(market)
        assert cid in committee

    def test_committee_picks_top_traders(self):
        """Committee picks top 2 traders by reputation (profit)."""
        cid, t1id, t2id, t3id = uid(), uid(), uid(), uid()
        mid = uid()
        create_user(cid, "creator1")
        create_user(t1id, "trader1")
        create_user(t2id, "trader2")
        create_user(t3id, "trader3")
        
        db._users[t1id]["profit_all_time"] = 500.0
        db._users[t2id]["profit_all_time"] = 200.0
        db._users[t3id]["profit_all_time"] = 100.0
        
        market = create_market(mid, cid, status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        add_bet(mid, t3id)
        
        committee = _form_committee(market)
        assert len(committee) == 3
        assert cid in committee
        assert t1id in committee
        assert t2id in committee

    def test_committee_with_no_traders(self):
        """If no one traded, committee is just the creator."""
        cid = uid()
        mid = uid()
        create_user(cid, "creator1")
        market = create_market(mid, cid, status="resolving")
        
        committee = _form_committee(market)
        assert committee == [cid]

    def test_committee_with_one_trader(self):
        """With only 1 trader, committee is creator + that trader."""
        cid, t1id = uid(), uid()
        mid = uid()
        create_user(cid, "creator1")
        create_user(t1id, "trader1")
        market = create_market(mid, cid, status="resolving")
        add_bet(mid, t1id)
        
        committee = _form_committee(market)
        assert len(committee) == 2
        assert cid in committee
        assert t1id in committee

    def test_ensure_committee_is_idempotent(self):
        """Calling _ensure_committee twice doesn't change the committee."""
        cid, t1id = uid(), uid()
        mid = uid()
        create_user(cid, "creator1")
        create_user(t1id, "trader1")
        market = create_market(mid, cid, status="resolving")
        add_bet(mid, t1id)
        
        market = _ensure_committee(mid, market)
        committee1 = market["committee"]
        deadline1 = market["resolution_deadline"]
        
        market = _ensure_committee(mid, market)
        assert market["committee"] == committee1
        assert market["resolution_deadline"] == deadline1


# =============================================================================
# Unanimity Check Tests
# =============================================================================

class TestUnanimity:
    def test_all_yes(self):
        committee = ["a", "b", "c"]
        votes = [
            {"agent_id": "a", "outcome": "YES"},
            {"agent_id": "b", "outcome": "YES"},
            {"agent_id": "c", "outcome": "YES"},
        ]
        assert _check_unanimous(votes, committee) == "YES"

    def test_all_no(self):
        committee = ["a", "b", "c"]
        votes = [
            {"agent_id": "a", "outcome": "NO"},
            {"agent_id": "b", "outcome": "NO"},
            {"agent_id": "c", "outcome": "NO"},
        ]
        assert _check_unanimous(votes, committee) == "NO"

    def test_all_invalid(self):
        committee = ["a", "b", "c"]
        votes = [
            {"agent_id": "a", "outcome": "INVALID"},
            {"agent_id": "b", "outcome": "INVALID"},
            {"agent_id": "c", "outcome": "INVALID"},
        ]
        assert _check_unanimous(votes, committee) == "INVALID"

    def test_mixed_votes(self):
        committee = ["a", "b", "c"]
        votes = [
            {"agent_id": "a", "outcome": "YES"},
            {"agent_id": "b", "outcome": "NO"},
            {"agent_id": "c", "outcome": "YES"},
        ]
        assert _check_unanimous(votes, committee) is None

    def test_missing_votes(self):
        committee = ["a", "b", "c"]
        votes = [
            {"agent_id": "a", "outcome": "YES"},
            {"agent_id": "b", "outcome": "YES"},
        ]
        assert _check_unanimous(votes, committee) is None

    def test_empty_votes(self):
        committee = ["a", "b", "c"]
        assert _check_unanimous([], committee) is None


# =============================================================================
# API Endpoint Tests
# =============================================================================

class TestCastVoteEndpoint:
    def test_committee_member_can_vote(self):
        """Committee member can cast a vote."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        create_user(t1id, "trader1")
        create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        resp = client.post(
            f"/markets/{mid}/resolution-vote",
            json={"outcome": "YES"},
            headers={"Authorization": f"Bearer {api_creator}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == cid
        assert data["outcome"] == "YES"
        assert data["votes_cast"] == 1
        assert data["unanimous"] is False
        assert data["auto_resolved"] is False

    def test_non_committee_member_rejected(self):
        """Non-committee member gets 403."""
        cid, oid, t1id, t2id = uid(), uid(), uid(), uid()
        mid = uid()
        create_user(cid, "creator1")
        api_outsider = create_user(oid, "outsider1")
        create_user(t1id, "trader1")
        create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        resp = client.post(
            f"/markets/{mid}/resolution-vote",
            json={"outcome": "YES"},
            headers={"Authorization": f"Bearer {api_outsider}"},
        )
        assert resp.status_code == 403

    def test_unanimous_yes_auto_resolves(self):
        """3/3 YES votes auto-resolves the market."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        api_trader1 = create_user(t1id, "trader1")
        api_trader2 = create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        for api_key in [api_creator, api_trader1, api_trader2]:
            resp = client.post(
                f"/markets/{mid}/resolution-vote",
                json={"outcome": "YES"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert resp.status_code == 200
        
        data = resp.json()
        assert data["unanimous"] is True
        assert data["auto_resolved"] is True
        assert data["resolved_outcome"] == "YES"
        
        market = db.get_market(mid)
        assert market["status"] == MarketStatus.RESOLVED
        assert market["resolution"] == Outcome.YES

    def test_unanimous_no_auto_resolves(self):
        """3/3 NO votes auto-resolves the market."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        api_trader1 = create_user(t1id, "trader1")
        api_trader2 = create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        for api_key in [api_creator, api_trader1, api_trader2]:
            resp = client.post(
                f"/markets/{mid}/resolution-vote",
                json={"outcome": "NO"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        
        data = resp.json()
        assert data["auto_resolved"] is True
        assert data["resolved_outcome"] == "NO"

    def test_mixed_votes_no_resolution(self):
        """Mixed votes don't auto-resolve."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        api_trader1 = create_user(t1id, "trader1")
        api_trader2 = create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        client.post(f"/markets/{mid}/resolution-vote", json={"outcome": "YES"},
                     headers={"Authorization": f"Bearer {api_creator}"})
        client.post(f"/markets/{mid}/resolution-vote", json={"outcome": "NO"},
                     headers={"Authorization": f"Bearer {api_trader1}"})
        resp = client.post(f"/markets/{mid}/resolution-vote", json={"outcome": "YES"},
                           headers={"Authorization": f"Bearer {api_trader2}"})
        
        data = resp.json()
        assert data["unanimous"] is False
        assert data["auto_resolved"] is False
        
        market = db.get_market(mid)
        assert market["status"] == MarketStatus.RESOLVING

    def test_vote_on_open_market_rejected(self):
        """Can't vote on a market that's still open."""
        cid = uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        create_market(mid, cid, status="open")
        
        resp = client.post(
            f"/markets/{mid}/resolution-vote",
            json={"outcome": "YES"},
            headers={"Authorization": f"Bearer {api_creator}"},
        )
        assert resp.status_code == 400

    def test_vote_on_resolved_market_rejected(self):
        """Can't vote on already resolved market."""
        cid = uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        create_market(mid, cid, status="resolving")
        db._markets[mid]["status"] = MarketStatus.RESOLVED
        db._markets[mid]["resolution"] = Outcome.YES
        
        resp = client.post(
            f"/markets/{mid}/resolution-vote",
            json={"outcome": "YES"},
            headers={"Authorization": f"Bearer {api_creator}"},
        )
        assert resp.status_code == 400

    def test_vote_can_be_changed(self):
        """Committee member can change their vote (before unanimity)."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        create_user(t1id, "trader1")
        create_user(t2id, "trader2")
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        # Vote YES first
        resp1 = client.post(
            f"/markets/{mid}/resolution-vote",
            json={"outcome": "YES"},
            headers={"Authorization": f"Bearer {api_creator}"},
        )
        assert resp1.status_code == 200
        
        # Change to NO (market not resolved yet — need 3/3)
        resp2 = client.post(
            f"/markets/{mid}/resolution-vote",
            json={"outcome": "NO"},
            headers={"Authorization": f"Bearer {api_creator}"},
        )
        assert resp2.status_code == 200
        
        votes = db.get_committee_votes(mid)
        creator_votes = [v for v in votes if v["agent_id"] == cid]
        assert len(creator_votes) == 1
        assert creator_votes[0]["outcome"] == "NO"

    def test_invalid_vote_outcome(self):
        """INVALID is a valid vote but doesn't auto-resolve."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        api_trader1 = create_user(t1id, "trader1")
        api_trader2 = create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        for api_key in [api_creator, api_trader1, api_trader2]:
            resp = client.post(
                f"/markets/{mid}/resolution-vote",
                json={"outcome": "INVALID"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        
        data = resp.json()
        assert data["unanimous"] is True
        assert data["auto_resolved"] is False


# =============================================================================
# Committee Votes GET Endpoint Tests
# =============================================================================

class TestGetCommitteeVotes:
    def test_get_votes_for_market(self):
        """GET returns committee status with votes."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        create_user(t1id, "trader1")
        create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        client.post(f"/markets/{mid}/resolution-vote", json={"outcome": "YES"},
                     headers={"Authorization": f"Bearer {api_creator}"})
        
        resp = client.get(f"/markets/{mid}/committee-votes")
        assert resp.status_code == 200
        data = resp.json()
        
        assert data["market_id"] == mid
        assert len(data["committee"]) == 3
        assert data["votes_cast"] == 1
        assert data["votes_required"] == 3
        assert data["unanimous"] is False
        assert data["status"] == "pending"

    def test_get_votes_shows_deadline(self):
        """GET shows the resolution deadline."""
        cid = uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        
        client.post(f"/markets/{mid}/resolution-vote", json={"outcome": "YES"},
                     headers={"Authorization": f"Bearer {api_creator}"})
        
        resp = client.get(f"/markets/{mid}/committee-votes")
        data = resp.json()
        assert data["resolution_deadline"] is not None

    def test_get_votes_market_not_found(self):
        resp = client.get("/markets/00000000-0000-0000-0000-000000000000/committee-votes")
        assert resp.status_code == 404


# =============================================================================
# Creator Fallback Tests
# =============================================================================

class TestCreatorFallback:
    def test_creator_blocked_during_committee_window(self):
        """Creator can't directly resolve while committee window is active."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        create_user(t1id, "trader1")
        create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        _ensure_committee(mid, db._markets[mid])
        
        resp = client.post(
            f"/markets/{mid}/resolve",
            json={"outcome": "YES"},
            headers={"Authorization": f"Bearer {api_creator}"},
        )
        assert resp.status_code == 400
        assert "Committee voting is in progress" in resp.json()["detail"]

    def test_creator_can_resolve_after_deadline(self):
        """Creator can resolve after 30min deadline passes."""
        cid, t1id, t2id = uid(), uid(), uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        create_user(t1id, "trader1")
        create_user(t2id, "trader2")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        add_bet(mid, t2id)
        
        committee = [cid, t1id, t2id]
        deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.set_market_committee(mid, committee, deadline)
        
        resp = client.post(
            f"/markets/{mid}/resolve",
            json={"outcome": "YES"},
            headers={"Authorization": f"Bearer {api_creator}"},
        )
        assert resp.status_code == 200
        
        market = db.get_market(mid)
        assert market["status"] == MarketStatus.RESOLVED
        assert market["resolution"] == Outcome.YES

    def test_creator_can_resolve_if_no_traders(self):
        """Creator can resolve directly if no other traders (committee = just creator)."""
        cid = uid()
        mid = uid()
        api_creator = create_user(cid, "creator1")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        
        resp = client.post(
            f"/markets/{mid}/resolve",
            json={"outcome": "YES"},
            headers={"Authorization": f"Bearer {api_creator}"},
        )
        assert resp.status_code == 200


# =============================================================================
# MarketDetail Committee Fields Tests
# =============================================================================

class TestMarketDetailCommitteeFields:
    def test_open_market_has_no_committee(self):
        """Open market shouldn't have committee fields populated."""
        cid = uid()
        mid = uid()
        create_user(cid, "creator1")
        create_market(mid, cid, status="open")
        
        resp = client.get(f"/markets/{mid}")
        data = resp.json()
        assert data["committee"] is None
        assert data["resolution_votes"] is None
        assert data["resolution_deadline"] is None

    def test_resolving_market_shows_committee(self):
        """Resolving market shows committee when fetched."""
        cid, t1id = uid(), uid()
        mid = uid()
        create_user(cid, "creator1")
        create_user(t1id, "trader1")
        
        create_market(mid, cid,
                      closes_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                      status="resolving")
        add_bet(mid, t1id)
        
        resp = client.get(f"/markets/{mid}")
        data = resp.json()
        assert data["committee"] is not None
        assert cid in data["committee"]
        assert data["resolution_deadline"] is not None


# =============================================================================
# Run
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
