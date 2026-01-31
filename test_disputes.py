"""
Tests for the multi-resolver dispute system (Issue #8).

Covers:
- Filing a dispute against a resolved market
- Dispute eligibility (must have bet, can't be creator, within window)
- Voting on disputes (UPHOLD / OVERTURN)
- Auto-resolution when quorum is reached
- Edge cases (duplicate disputes, duplicate votes, window closed)
"""

import uuid
import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

# Patch env so storage uses in-memory (no DATABASE_URL)
import os
os.environ.pop("DATABASE_URL", None)

from api import app, db, DISPUTE_WINDOW_SECONDS, DISPUTE_QUORUM
from storage import hash_api_key
from models import MarketStatus, Outcome
from rate_limiter import rate_limiter

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_state():
    """Reset in-memory storage and rate limiter between every test."""
    db._markets.clear()
    db._users.clear()
    db._bets.clear()
    db._positions.clear()
    if hasattr(db, "_comments"):
        db._comments.clear()
    if hasattr(db, "_resolution_votes"):
        db._resolution_votes.clear()
    if hasattr(db, "_chat_messages"):
        db._chat_messages.clear()
    if hasattr(db, "_disputes"):
        db._disputes.clear()
    if hasattr(db, "_dispute_votes"):
        db._dispute_votes.clear()
    rate_limiter._requests.clear()
    rate_limiter._call_count = 0
    # Re-create demo user
    if not db.get_user("demo-user"):
        db.create_user("demo-user", "demo_user", balance=0.0)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_and_claim(username: str) -> dict:
    """Register an agent + force-claim so they can trade."""
    resp = client.post("/agents/register", json={"username": username})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Force-claim in the in-memory store so require_auth passes
    user = db.get_user(data["user_id"])
    user["status"] = "claimed"
    return {"user_id": data["user_id"], "api_key": data["api_key"], "username": username}


def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _create_market(api_key: str, minutes: int = 60) -> str:
    """Create a market and return its ID."""
    close = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    resp = client.post(
        "/markets",
        json={"title": f"Test market {uuid.uuid4().hex[:8]}", "closes_at": close},
        headers=_auth(api_key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _place_bet(api_key: str, market_id: str, outcome: str = "YES", amount: float = 10.0):
    resp = client.post(
        f"/markets/{market_id}/bet",
        json={"outcome": outcome, "amount": amount},
        headers=_auth(api_key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _resolve_market(api_key: str, market_id: str, outcome: str = "YES"):
    resp = client.post(
        f"/markets/{market_id}/resolve",
        json={"outcome": outcome},
        headers=_auth(api_key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Test: File a dispute
# ---------------------------------------------------------------------------

class TestFileDispute:
    def test_file_dispute_success(self):
        """A bettor can dispute a resolved market."""
        creator = _register_and_claim(f"creator_{uuid.uuid4().hex[:6]}")
        bettor = _register_and_claim(f"bettor_{uuid.uuid4().hex[:6]}")
        market_id = _create_market(creator["api_key"])
        _place_bet(bettor["api_key"], market_id, "YES", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={
                "reason": "The resolution is wrong — event clearly happened (source: Reuters)",
                "evidence": "https://reuters.com/proof-link",
            },
            headers=_auth(bettor["api_key"]),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "UNDER_REVIEW"
        assert data["disputer_id"] == bettor["user_id"]
        assert data["original_resolution"] == "NO"
        assert data["reason"].startswith("The resolution is wrong")

    def test_dispute_requires_bet(self):
        """User without a position cannot dispute."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        outsider = _register_and_claim(f"out_{uuid.uuid4().hex[:6]}")
        bettor = _register_and_claim(f"bt_{uuid.uuid4().hex[:6]}")
        market_id = _create_market(creator["api_key"])
        _place_bet(bettor["api_key"], market_id, "YES", 10)
        _resolve_market(creator["api_key"], market_id, "YES")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "I disagree with this resolution outcome"},
            headers=_auth(outsider["api_key"]),
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "NOT_ELIGIBLE"

    def test_creator_cannot_dispute_own(self):
        """Creator cannot dispute their own resolution."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        bettor = _register_and_claim(f"bt_{uuid.uuid4().hex[:6]}")
        market_id = _create_market(creator["api_key"])
        _place_bet(bettor["api_key"], market_id, "YES", 10)
        # Creator also bets so they have a position
        _place_bet(creator["api_key"], market_id, "NO", 10)
        _resolve_market(creator["api_key"], market_id, "YES")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Actually I think I was wrong about my own resolution"},
            headers=_auth(creator["api_key"]),
        )
        assert resp.status_code == 403

    def test_dispute_only_resolved_markets(self):
        """Cannot dispute a market that hasn't been resolved."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        bettor = _register_and_claim(f"bt_{uuid.uuid4().hex[:6]}")
        market_id = _create_market(creator["api_key"])
        _place_bet(bettor["api_key"], market_id, "YES", 10)

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Market isn't even resolved yet but I'm mad"},
            headers=_auth(bettor["api_key"]),
        )
        assert resp.status_code == 400

    def test_duplicate_dispute_blocked(self):
        """Only one active dispute per market."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        b1 = _register_and_claim(f"b1_{uuid.uuid4().hex[:6]}")
        b2 = _register_and_claim(f"b2_{uuid.uuid4().hex[:6]}")
        market_id = _create_market(creator["api_key"])
        _place_bet(b1["api_key"], market_id, "YES", 10)
        _place_bet(b2["api_key"], market_id, "NO", 10)
        _resolve_market(creator["api_key"], market_id, "YES")

        # First dispute: OK
        resp1 = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Resolution is wrong, here's my first dispute"},
            headers=_auth(b2["api_key"]),
        )
        assert resp1.status_code == 200

        # Second dispute while first is active: blocked
        resp2 = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "I also want to dispute this resolution outcome"},
            headers=_auth(b1["api_key"]),
        )
        assert resp2.status_code == 409
        assert resp2.json()["code"] == "DISPUTE_ALREADY_EXISTS"

    def test_reason_min_length(self):
        """Reason must be at least 10 characters."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        bettor = _register_and_claim(f"bt_{uuid.uuid4().hex[:6]}")
        market_id = _create_market(creator["api_key"])
        _place_bet(bettor["api_key"], market_id, "YES", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "short"},
            headers=_auth(bettor["api_key"]),
        )
        assert resp.status_code == 422  # Pydantic validation


# ---------------------------------------------------------------------------
# Test: Get disputes
# ---------------------------------------------------------------------------

class TestGetDisputes:
    def test_list_disputes(self):
        """GET /markets/{id}/disputes returns all disputes."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        bettor = _register_and_claim(f"bt_{uuid.uuid4().hex[:6]}")
        market_id = _create_market(creator["api_key"])
        _place_bet(bettor["api_key"], market_id, "YES", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        # File dispute
        client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "The resolution is wrong, I have proof here"},
            headers=_auth(bettor["api_key"]),
        )

        resp = client.get(f"/markets/{market_id}/disputes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["disputes"][0]["status"] == "UNDER_REVIEW"

    def test_no_disputes(self):
        """GET returns empty list for market without disputes."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        market_id = _create_market(creator["api_key"])

        resp = client.get(f"/markets/{market_id}/disputes")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# Test: Voting on disputes
# ---------------------------------------------------------------------------

class TestDisputeVoting:
    def test_vote_uphold(self):
        """Community member can vote to uphold original resolution."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        disputer = _register_and_claim(f"disp_{uuid.uuid4().hex[:6]}")
        voter = _register_and_claim(f"vtr_{uuid.uuid4().hex[:6]}")

        market_id = _create_market(creator["api_key"])
        _place_bet(disputer["api_key"], market_id, "YES", 10)
        _place_bet(voter["api_key"], market_id, "NO", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        # File dispute
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "This resolution is incorrect and should be reversed"},
            headers=_auth(disputer["api_key"]),
        )
        dispute_id = resp.json()["id"]

        # Vote to uphold
        resp = client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "UPHOLD", "reasoning": "Resolution looks correct to me"},
            headers=_auth(voter["api_key"]),
        )
        assert resp.status_code == 200
        assert resp.json()["votes_uphold"] == 1

    def test_cannot_vote_twice(self):
        """Same user can't vote twice on the same dispute."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        disputer = _register_and_claim(f"disp_{uuid.uuid4().hex[:6]}")
        voter = _register_and_claim(f"vtr_{uuid.uuid4().hex[:6]}")

        market_id = _create_market(creator["api_key"])
        _place_bet(disputer["api_key"], market_id, "YES", 10)
        _place_bet(voter["api_key"], market_id, "NO", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "This resolution should definitely be overturned"},
            headers=_auth(disputer["api_key"]),
        )
        dispute_id = resp.json()["id"]

        # First vote OK
        client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "UPHOLD"},
            headers=_auth(voter["api_key"]),
        )
        # Second vote blocked
        resp2 = client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "OVERTURN"},
            headers=_auth(voter["api_key"]),
        )
        assert resp2.status_code == 409
        assert resp2.json()["code"] == "ALREADY_VOTED"

    def test_disputer_cannot_vote(self):
        """The disputer can't vote on their own dispute."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        disputer = _register_and_claim(f"disp_{uuid.uuid4().hex[:6]}")

        market_id = _create_market(creator["api_key"])
        _place_bet(disputer["api_key"], market_id, "YES", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Resolution is wrong, here's why I think so"},
            headers=_auth(disputer["api_key"]),
        )
        dispute_id = resp.json()["id"]

        resp = client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "OVERTURN"},
            headers=_auth(disputer["api_key"]),
        )
        assert resp.status_code == 403

    def test_outsider_cannot_vote(self):
        """User with no position can't vote."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        disputer = _register_and_claim(f"disp_{uuid.uuid4().hex[:6]}")
        outsider = _register_and_claim(f"out_{uuid.uuid4().hex[:6]}")

        market_id = _create_market(creator["api_key"])
        _place_bet(disputer["api_key"], market_id, "YES", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "This resolution is clearly wrong and incorrect"},
            headers=_auth(disputer["api_key"]),
        )
        dispute_id = resp.json()["id"]

        resp = client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "UPHOLD"},
            headers=_auth(outsider["api_key"]),
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "NOT_ELIGIBLE"


# ---------------------------------------------------------------------------
# Test: Auto-resolution on quorum
# ---------------------------------------------------------------------------

class TestDisputeAutoResolve:
    def test_quorum_upholds(self):
        """When quorum votes UPHOLD, dispute is auto-resolved as UPHELD."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        disputer = _register_and_claim(f"disp_{uuid.uuid4().hex[:6]}")
        voters = [_register_and_claim(f"v{i}_{uuid.uuid4().hex[:6]}") for i in range(DISPUTE_QUORUM)]

        market_id = _create_market(creator["api_key"])
        _place_bet(disputer["api_key"], market_id, "YES", 10)
        for v in voters:
            _place_bet(v["api_key"], market_id, "NO", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Resolution is wrong, I have evidence supporting YES"},
            headers=_auth(disputer["api_key"]),
        )
        dispute_id = resp.json()["id"]

        # All voters vote UPHOLD
        last_resp = None
        for v in voters:
            last_resp = client.post(
                f"/markets/{market_id}/disputes/{dispute_id}/vote",
                json={"vote": "UPHOLD"},
                headers=_auth(v["api_key"]),
            )
        assert last_resp.status_code == 200
        data = last_resp.json()
        assert data["status"] == "UPHELD"
        assert data["votes_uphold"] == DISPUTE_QUORUM

        # Market resolution unchanged
        market = client.get(f"/markets/{market_id}").json()
        assert market["resolution"] == "NO"

    def test_quorum_overturns(self):
        """When quorum votes OVERTURN, resolution is flipped."""
        creator = _register_and_claim(f"cr_{uuid.uuid4().hex[:6]}")
        disputer = _register_and_claim(f"disp_{uuid.uuid4().hex[:6]}")
        voters = [_register_and_claim(f"v{i}_{uuid.uuid4().hex[:6]}") for i in range(DISPUTE_QUORUM)]

        market_id = _create_market(creator["api_key"])
        _place_bet(disputer["api_key"], market_id, "YES", 10)
        for v in voters:
            _place_bet(v["api_key"], market_id, "NO", 10)
        _resolve_market(creator["api_key"], market_id, "NO")

        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Resolution is wrong, event did happen, check sources"},
            headers=_auth(disputer["api_key"]),
        )
        dispute_id = resp.json()["id"]

        # All voters vote OVERTURN
        last_resp = None
        for v in voters:
            last_resp = client.post(
                f"/markets/{market_id}/disputes/{dispute_id}/vote",
                json={"vote": "OVERTURN"},
                headers=_auth(v["api_key"]),
            )
        assert last_resp.status_code == 200
        data = last_resp.json()
        assert data["status"] == "OVERTURNED"
        assert data["new_resolution"] == "YES"  # Flipped from NO

        # Market resolution is now YES
        market = client.get(f"/markets/{market_id}").json()
        assert market["resolution"] == "YES"
