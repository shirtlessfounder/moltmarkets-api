"""
Tests for MoltMarkets API endpoints and trading flows.

Covers the full lifecycle:
  1. Register agent → verify response
  2. Create market → verify fields
  3. Place bet (YES/NO) → verify balance changes, probability shifts
  4. Sell position → verify payout with fees
  5. Resolve market → verify winner payouts
  6. Edge cases: bet on closed market, insufficient balance, invalid market ID

Uses FastAPI TestClient against the in-memory storage backend
(DATABASE_URL is not set, so Storage falls back to dicts).

Run with: python -m pytest test_api_flows.py -v
Ref: https://github.com/shirtlessfounder/moltmarkets-api/issues/52
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Ensure in-memory storage (no DATABASE_URL) before importing app
# ---------------------------------------------------------------------------
os.environ.pop("DATABASE_URL", None)

from api import app, db, STARTING_BALANCE, TRADE_FEE_RATE, MARKET_CREATION_COST  # noqa: E402
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
    if hasattr(db, "_chat_messages"):
        db._chat_messages.clear()
    # Reset rate limiter so tests don't hit 429s from prior runs
    rate_limiter._requests.clear()
    rate_limiter._call_count = 0
    # Re-create the demo user the lifespan event would normally create
    if not db.get_user("demo-user"):
        db.create_user("demo-user", "demo_user", balance=0.0)


def _register_agent(client: TestClient, username: str = None, description: str = "") -> dict:
    """Register an agent and return (user_data, api_key, auth_header)."""
    username = username or f"agent_{uuid.uuid4().hex[:8]}"
    resp = client.post("/agents/register", json={
        "username": username,
        "description": description,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    api_key = data["api_key"]
    # Force-claim the agent so it passes verification gates
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
    """Create a market closing `minutes` from now. Returns JSON body."""
    closes_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    resp = client.post("/markets", json={
        "title": title,
        "description": "Test market",
        "closes_at": closes_at,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_storage():
    """Guarantee a clean slate for every test."""
    _fresh_storage()
    yield
    _fresh_storage()


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


# ===================================================================
# 1. Register Agent
# ===================================================================

class TestRegisterAgent:
    """POST /agents/register"""

    def test_register_returns_api_key(self, client):
        resp = client.post("/agents/register", json={
            "username": "testbot",
            "description": "A test agent",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "testbot"
        assert body["api_key"].startswith("mm_")
        assert body["balance"] == STARTING_BALANCE
        assert "user_id" in body

    def test_register_duplicate_username(self, client):
        client.post("/agents/register", json={"username": "dup_agent"})
        resp = client.post("/agents/register", json={"username": "dup_agent"})
        assert resp.status_code == 400

    def test_register_invalid_username(self, client):
        resp = client.post("/agents/register", json={"username": "no spaces!"})
        assert resp.status_code == 422  # Pydantic validation


# ===================================================================
# 2. Create Market
# ===================================================================

class TestCreateMarket:
    """POST /markets"""

    def test_create_market_fields(self, client):
        agent = _register_agent(client)
        market = _create_market(client, agent["headers"])

        assert "id" in market
        assert market["title"] == "Will it rain tomorrow?"
        assert market["status"] == "OPEN"
        assert market["probability"] == pytest.approx(0.5, abs=0.01)
        assert market["pool"]["YES"] == pytest.approx(100.0)
        assert market["pool"]["NO"] == pytest.approx(100.0)
        assert market["creator_id"] == agent["user_id"]
        assert market["creation_cost"] == MARKET_CREATION_COST

    def test_create_market_deducts_balance(self, client):
        agent = _register_agent(client)
        _create_market(client, agent["headers"])

        me = client.get("/me", headers=agent["headers"]).json()
        assert me["balance"] == pytest.approx(STARTING_BALANCE - MARKET_CREATION_COST, abs=0.01)

    def test_create_market_closes_in_past(self, client):
        agent = _register_agent(client)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        resp = client.post("/markets", json={
            "title": "Already closed",
            "closes_at": past,
        }, headers=agent["headers"])
        assert resp.status_code == 400

    def test_create_market_requires_auth(self, client):
        closes = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        resp = client.post("/markets", json={
            "title": "No auth market",
            "closes_at": closes,
        })
        assert resp.status_code == 401


# ===================================================================
# 3. Place Bet (YES / NO)
# ===================================================================

class TestPlaceBet:
    """POST /markets/{id}/bet"""

    def test_bet_yes_updates_probability(self, client):
        creator = _register_agent(client, "creator1")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        bettor = _register_agent(client, "bettor1")
        resp = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES",
            "amount": 50,
        }, headers=bettor["headers"])
        assert resp.status_code == 200
        body = resp.json()

        assert body["outcome"] == "YES"
        assert body["shares"] > 0
        assert body["probability_after"] > body["probability_before"]
        assert body["fee"] == pytest.approx(50 * TRADE_FEE_RATE, abs=0.01)
        assert body["total_cost"] == pytest.approx(50 + 50 * TRADE_FEE_RATE, abs=0.01)

    def test_bet_no_decreases_probability(self, client):
        creator = _register_agent(client, "creator2")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        bettor = _register_agent(client, "bettor2")
        resp = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "NO",
            "amount": 50,
        }, headers=bettor["headers"])
        assert resp.status_code == 200
        body = resp.json()

        assert body["probability_after"] < body["probability_before"]

    def test_bet_balance_decreases(self, client):
        creator = _register_agent(client, "creator3")
        market = _create_market(client, creator["headers"])

        bettor = _register_agent(client, "bettor3")
        before = client.get("/me", headers=bettor["headers"]).json()["balance"]
        resp = client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES",
            "amount": 100,
        }, headers=bettor["headers"])
        body = resp.json()

        assert body["new_balance"] == pytest.approx(
            before - 100 - 100 * TRADE_FEE_RATE, abs=0.01
        )

    def test_bet_creator_receives_fee(self, client):
        creator = _register_agent(client, "creator4")
        market = _create_market(client, creator["headers"])
        creator_bal_before = client.get("/me", headers=creator["headers"]).json()["balance"]

        bettor = _register_agent(client, "bettor4")
        client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES",
            "amount": 100,
        }, headers=bettor["headers"])

        creator_bal_after = client.get("/me", headers=creator["headers"]).json()["balance"]
        expected_creator_fee = 100 * TRADE_FEE_RATE * 0.5  # 50% of 2% fee
        assert creator_bal_after == pytest.approx(
            creator_bal_before + expected_creator_fee, abs=0.01
        )


# ===================================================================
# 4. Sell Position
# ===================================================================

class TestSellPosition:
    """POST /markets/{id}/sell"""

    def test_sell_returns_funds_minus_fee(self, client):
        creator = _register_agent(client, "sell_creator")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        trader = _register_agent(client, "sell_trader")
        # Buy shares first
        buy = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES",
            "amount": 50,
        }, headers=trader["headers"]).json()

        shares_bought = buy["shares"]

        # Sell all shares back
        resp = client.post(f"/markets/{market_id}/sell", json={
            "outcome": "YES",
            "shares": shares_bought,
        }, headers=trader["headers"])
        assert resp.status_code == 200
        body = resp.json()

        assert body["shares_sold"] == pytest.approx(shares_bought, abs=0.001)
        assert body["fee_paid"] > 0
        assert body["amount_received"] > 0
        # Probability should revert toward 0.5 after selling
        assert body["probability_after"] < body["probability_before"]

    def test_sell_insufficient_shares(self, client):
        creator = _register_agent(client, "sell_creator2")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        trader = _register_agent(client, "sell_trader2")
        client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES",
            "amount": 10,
        }, headers=trader["headers"])

        resp = client.post(f"/markets/{market_id}/sell", json={
            "outcome": "YES",
            "shares": 999999,
        }, headers=trader["headers"])
        assert resp.status_code == 400

    def test_sell_no_position(self, client):
        creator = _register_agent(client, "sell_creator3")
        market = _create_market(client, creator["headers"])

        outsider = _register_agent(client, "outsider")
        resp = client.post(f"/markets/{market['id']}/sell", json={
            "outcome": "YES",
            "shares": 1,
        }, headers=outsider["headers"])
        assert resp.status_code == 400


# ===================================================================
# 5. Resolve Market & Payouts
# ===================================================================

class TestResolveMarket:
    """POST /markets/{id}/resolve — payouts to winners."""

    def _setup_market_with_bets(self, client):
        """Helper: create a market, place opposing bets, return context."""
        creator = _register_agent(client, "res_creator")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        yes_bettor = _register_agent(client, "yes_bettor")
        no_bettor = _register_agent(client, "no_bettor")

        client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES", "amount": 100,
        }, headers=yes_bettor["headers"])

        client.post(f"/markets/{market_id}/bet", json={
            "outcome": "NO", "amount": 100,
        }, headers=no_bettor["headers"])

        return {
            "creator": creator,
            "yes_bettor": yes_bettor,
            "no_bettor": no_bettor,
            "market_id": market_id,
        }

    def test_resolve_yes_pays_yes_holders(self, client):
        ctx = self._setup_market_with_bets(client)
        yes_bal_before = client.get("/me", headers=ctx["yes_bettor"]["headers"]).json()["balance"]

        resp = client.post(f"/markets/{ctx['market_id']}/resolve", json={
            "outcome": "YES",
        }, headers=ctx["creator"]["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "RESOLVED"
        assert body["resolution"] == "YES"

        yes_bal_after = client.get("/me", headers=ctx["yes_bettor"]["headers"]).json()["balance"]
        assert yes_bal_after > yes_bal_before, "YES bettor should profit"

    def test_resolve_no_pays_no_holders(self, client):
        ctx = self._setup_market_with_bets(client)
        no_bal_before = client.get("/me", headers=ctx["no_bettor"]["headers"]).json()["balance"]

        resp = client.post(f"/markets/{ctx['market_id']}/resolve", json={
            "outcome": "NO",
        }, headers=ctx["creator"]["headers"])
        assert resp.status_code == 200

        no_bal_after = client.get("/me", headers=ctx["no_bettor"]["headers"]).json()["balance"]
        assert no_bal_after > no_bal_before, "NO bettor should profit"

    def test_losers_do_not_receive_payout(self, client):
        ctx = self._setup_market_with_bets(client)
        no_bal_before = client.get("/me", headers=ctx["no_bettor"]["headers"]).json()["balance"]

        client.post(f"/markets/{ctx['market_id']}/resolve", json={
            "outcome": "YES",
        }, headers=ctx["creator"]["headers"])

        no_bal_after = client.get("/me", headers=ctx["no_bettor"]["headers"]).json()["balance"]
        assert no_bal_after == pytest.approx(no_bal_before, abs=0.01), "Loser balance should not change"

    def test_resolve_twice_fails(self, client):
        ctx = self._setup_market_with_bets(client)
        client.post(f"/markets/{ctx['market_id']}/resolve", json={
            "outcome": "YES",
        }, headers=ctx["creator"]["headers"])

        resp = client.post(f"/markets/{ctx['market_id']}/resolve", json={
            "outcome": "NO",
        }, headers=ctx["creator"]["headers"])
        assert resp.status_code == 400

    def test_only_creator_can_resolve(self, client):
        ctx = self._setup_market_with_bets(client)
        resp = client.post(f"/markets/{ctx['market_id']}/resolve", json={
            "outcome": "YES",
        }, headers=ctx["yes_bettor"]["headers"])
        assert resp.status_code == 403


# ===================================================================
# 6. Edge Cases
# ===================================================================

class TestEdgeCases:

    # --- Bet on non-open market ---
    def test_bet_on_resolved_market(self, client):
        creator = _register_agent(client, "edge_creator")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        # Resolve immediately
        client.post(f"/markets/{market_id}/resolve", json={
            "outcome": "YES",
        }, headers=creator["headers"])

        bettor = _register_agent(client, "edge_bettor")
        resp = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES", "amount": 10,
        }, headers=bettor["headers"])
        assert resp.status_code == 400

    def test_bet_on_expired_market(self, client):
        """Market whose closes_at is in the past should auto-transition."""
        creator = _register_agent(client, "edge_creator2")
        # Create with very short window then fast-forward close time
        market = _create_market(client, creator["headers"], minutes=30)
        market_id = market["id"]
        # Manually set closes_at to the past in storage
        db._markets[market_id]["closes_at"] = datetime.now(timezone.utc) - timedelta(hours=1)

        bettor = _register_agent(client, "edge_bettor2")
        resp = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES", "amount": 10,
        }, headers=bettor["headers"])
        assert resp.status_code == 400

    # --- Insufficient balance ---
    def test_bet_insufficient_balance(self, client):
        creator = _register_agent(client, "edge_creator3")
        market = _create_market(client, creator["headers"])

        poor = _register_agent(client, "poor_agent")
        # Drain balance
        db._users[poor["user_id"]]["balance"] = 1.0

        resp = client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES", "amount": 500,
        }, headers=poor["headers"])
        assert resp.status_code == 400
        assert resp.json().get("error") or resp.json().get("detail")

    def test_create_market_insufficient_balance(self, client):
        agent = _register_agent(client, "broke_creator")
        db._users[agent["user_id"]]["balance"] = 10.0  # Below MARKET_CREATION_COST

        closes = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        resp = client.post("/markets", json={
            "title": "Can't afford this",
            "closes_at": closes,
        }, headers=agent["headers"])
        assert resp.status_code == 400

    # --- Invalid market ID ---
    def test_bet_invalid_market_id(self, client):
        agent = _register_agent(client, "edge_agent4")
        resp = client.post("/markets/not-a-uuid/bet", json={
            "outcome": "YES", "amount": 10,
        }, headers=agent["headers"])
        assert resp.status_code == 400

    def test_bet_nonexistent_market(self, client):
        agent = _register_agent(client, "edge_agent5")
        fake_id = str(uuid.uuid4())
        resp = client.post(f"/markets/{fake_id}/bet", json={
            "outcome": "YES", "amount": 10,
        }, headers=agent["headers"])
        assert resp.status_code == 404

    def test_get_nonexistent_market(self, client):
        fake_id = str(uuid.uuid4())
        resp = client.get(f"/markets/{fake_id}")
        assert resp.status_code == 404

    # --- Sell on non-open market ---
    def test_sell_on_resolved_market(self, client):
        creator = _register_agent(client, "sell_edge_creator")
        market = _create_market(client, creator["headers"])
        market_id = market["id"]

        trader = _register_agent(client, "sell_edge_trader")
        client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES", "amount": 20,
        }, headers=trader["headers"])

        # Resolve
        client.post(f"/markets/{market_id}/resolve", json={
            "outcome": "YES",
        }, headers=creator["headers"])

        resp = client.post(f"/markets/{market_id}/sell", json={
            "outcome": "YES", "shares": 1,
        }, headers=trader["headers"])
        assert resp.status_code == 400

    # --- Auth edge cases ---
    def test_bet_without_auth(self, client):
        creator = _register_agent(client, "noauth_creator")
        market = _create_market(client, creator["headers"])
        resp = client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES", "amount": 10,
        })
        assert resp.status_code == 401

    def test_bet_with_bad_key(self, client):
        creator = _register_agent(client, "badkey_creator")
        market = _create_market(client, creator["headers"])
        resp = client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES", "amount": 10,
        }, headers={"Authorization": "Bearer mm_invalid_key"})
        assert resp.status_code == 401

    # --- Bet amount edge cases ---
    def test_bet_amount_zero(self, client):
        creator = _register_agent(client, "zero_creator")
        market = _create_market(client, creator["headers"])
        bettor = _register_agent(client, "zero_bettor")
        resp = client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES", "amount": 0,
        }, headers=bettor["headers"])
        assert resp.status_code == 422  # Pydantic gt=0

    def test_bet_amount_exceeds_max(self, client):
        creator = _register_agent(client, "max_creator")
        market = _create_market(client, creator["headers"])
        bettor = _register_agent(client, "max_bettor")
        resp = client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES", "amount": 501,
        }, headers=bettor["headers"])
        assert resp.status_code == 422  # Pydantic le=500


# ===================================================================
# 7. Read Endpoints (sanity)
# ===================================================================

class TestReadEndpoints:

    def test_list_markets(self, client):
        creator = _register_agent(client, "list_creator")
        _create_market(client, creator["headers"], title="Market A")
        # Reset cooldown so second create works
        db._users[creator["user_id"]]["last_market_created_at"] = None
        _create_market(client, creator["headers"], title="Market B")

        resp = client.get("/markets?status=all")
        assert resp.status_code == 200
        body = resp.json()
        # /markets now returns a paginated envelope {data: [...], pagination: {...}}
        assert "data" in body
        assert "pagination" in body
        assert len(body["data"]) >= 2

    def test_get_market_detail(self, client):
        creator = _register_agent(client, "detail_creator")
        market = _create_market(client, creator["headers"])

        resp = client.get(f"/markets/{market['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == market["id"]
        assert "pool" in body
        assert "probability" in body

    def test_market_history(self, client):
        creator = _register_agent(client, "hist_creator")
        market = _create_market(client, creator["headers"])

        bettor = _register_agent(client, "hist_bettor")
        client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES", "amount": 20,
        }, headers=bettor["headers"])

        resp = client.get(f"/markets/{market['id']}/history")
        assert resp.status_code == 200
        points = resp.json()["points"]
        assert len(points) >= 2  # Initial + at least 1 bet

    def test_market_positions(self, client):
        creator = _register_agent(client, "pos_creator")
        market = _create_market(client, creator["headers"])

        bettor = _register_agent(client, "pos_bettor")
        client.post(f"/markets/{market['id']}/bet", json={
            "outcome": "YES", "amount": 30,
        }, headers=bettor["headers"])

        resp = client.get(f"/markets/{market['id']}/positions")
        assert resp.status_code == 200
        positions = resp.json()["positions"]
        assert len(positions) >= 1
        assert positions[0]["yes_shares"] > 0

    def test_me_endpoint(self, client):
        agent = _register_agent(client, "me_agent")
        resp = client.get("/me", headers=agent["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "me_agent"
        assert body["currency"] == "ŧ"

    def test_leaderboard(self, client):
        _register_agent(client, "lb_agent")
        resp = client.get("/leaderboard")
        assert resp.status_code == 200
        body = resp.json()
        # /leaderboard now returns a paginated envelope {data: [...], pagination: {...}}
        assert "data" in body
        assert isinstance(body["data"], list)


# ===================================================================
# Run
# ===================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
