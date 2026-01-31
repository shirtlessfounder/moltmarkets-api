"""
Tests for sandbox/testnet environment features (issue #125).

Covers:
  1. Sandbox agent registration (sandbox=true flag)
  2. Sandbox agents get higher starting balance
  3. Sandbox agents are auto-claimed (no Twitter verification)
  4. Sandbox agents excluded from leaderboard
  5. Sandbox balance reset endpoint
  6. Production agents cannot reset balance
  7. GET /sandbox/status (authenticated + anonymous)
  8. Dry-run bet (X-Dry-Run: true header)
  9. Dry-run sell (X-Dry-Run: true header)
  10. Dry-run with insufficient balance still returns simulation
  11. Environment detection (MOLTMARKETS_ENV)

Run with: python -m pytest test_sandbox.py -v
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

# Ensure in-memory storage (no DATABASE_URL) before importing app
os.environ.pop("DATABASE_URL", None)

from api import app, db  # noqa: E402
from deps import STARTING_BALANCE  # noqa: E402
from rate_limiter import rate_limiter  # noqa: E402
from sandbox import SANDBOX_STARTING_BALANCE, SANDBOX_BALANCE_RESET_AMOUNT  # noqa: E402


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
    rate_limiter._requests.clear()
    rate_limiter._call_count = 0
    if not db.get_user("demo-user"):
        db.create_user("demo-user", "demo_user", balance=0.0)


def _register_agent(client: TestClient, username: str = None,
                    sandbox: bool = False) -> dict:
    """Register an agent and return dict with data, api_key, headers."""
    username = username or f"agent_{uuid.uuid4().hex[:8]}"
    resp = client.post("/agents/register", json={
        "username": username,
        "sandbox": sandbox,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    api_key = data["api_key"]
    # Force-claim non-sandbox agents
    if not sandbox:
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
# 1. Sandbox Registration
# ===========================================================================

class TestSandboxRegistration:
    """Test sandbox agent registration flow."""

    def test_register_sandbox_agent(self):
        """Sandbox agents should be created with sandbox flag."""
        agent = _register_agent(client, sandbox=True)
        data = agent["data"]
        assert data["is_sandbox"] is True
        assert data["balance"] == SANDBOX_STARTING_BALANCE
        assert data["status"] == "claimed"  # Auto-claimed

    def test_register_production_agent(self):
        """Production agents should NOT have sandbox flag."""
        resp = client.post("/agents/register", json={
            "username": "prod_agent",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("is_sandbox", False) is False
        assert data["balance"] == STARTING_BALANCE
        assert data["status"] == "pending"  # Needs Twitter verification

    def test_sandbox_agent_higher_balance(self):
        """Sandbox agents get 10x the normal starting balance."""
        agent = _register_agent(client, sandbox=True)
        assert agent["data"]["balance"] == SANDBOX_STARTING_BALANCE
        assert SANDBOX_STARTING_BALANCE > STARTING_BALANCE

    def test_sandbox_agent_auto_claimed(self):
        """Sandbox agents should be auto-claimed (no Twitter needed)."""
        agent = _register_agent(client, sandbox=True)
        assert agent["data"]["status"] == "claimed"

    def test_sandbox_default_false(self):
        """Registration without sandbox flag defaults to false."""
        resp = client.post("/agents/register", json={
            "username": "default_agent",
        })
        data = resp.json()
        assert data.get("is_sandbox", False) is False


# ===========================================================================
# 2. Leaderboard Exclusion
# ===========================================================================

class TestLeaderboardExclusion:
    """Test that sandbox agents are excluded from the leaderboard."""

    def test_sandbox_agent_excluded_from_leaderboard(self):
        """Sandbox agents should not appear on the leaderboard."""
        # Create a production agent and a sandbox agent
        _register_agent(client, username="prod_trader", sandbox=False)
        _register_agent(client, username="sandbox_trader", sandbox=True)

        resp = client.get("/leaderboard")
        assert resp.status_code == 200
        data = resp.json()
        usernames = [e["username"] for e in data["data"]]

        assert "prod_trader" in usernames
        assert "sandbox_trader" not in usernames

    def test_production_agent_on_leaderboard(self):
        """Production agents should appear on the leaderboard normally."""
        _register_agent(client, username="real_trader", sandbox=False)

        resp = client.get("/leaderboard")
        data = resp.json()
        usernames = [e["username"] for e in data["data"]]
        assert "real_trader" in usernames


# ===========================================================================
# 3. Sandbox Balance Reset
# ===========================================================================

class TestSandboxBalanceReset:
    """Test the POST /sandbox/reset-balance endpoint."""

    def test_sandbox_reset_balance(self):
        """Sandbox agents can reset their balance."""
        agent = _register_agent(client, sandbox=True)
        # Spend some balance by creating a market
        _create_market(client, agent["headers"])

        # Verify balance decreased
        me_resp = client.get("/me", headers=agent["headers"])
        assert me_resp.json()["balance"] < SANDBOX_STARTING_BALANCE

        # Reset
        resp = client.post("/sandbox/reset-balance", headers=agent["headers"])
        assert resp.status_code == 200
        data = resp.json()
        assert data["new_balance"] == SANDBOX_BALANCE_RESET_AMOUNT
        assert "Happy testing" in data["message"]

        # Verify balance restored
        me_resp = client.get("/me", headers=agent["headers"])
        assert me_resp.json()["balance"] == SANDBOX_BALANCE_RESET_AMOUNT

    def test_production_agent_cannot_reset(self):
        """Production agents should be rejected from reset-balance."""
        agent = _register_agent(client, sandbox=False)
        resp = client.post("/sandbox/reset-balance", headers=agent["headers"])
        assert resp.status_code == 403
        assert "sandbox" in resp.json()["error"].lower()


# ===========================================================================
# 4. Sandbox Status Endpoint
# ===========================================================================

class TestSandboxStatus:
    """Test GET /sandbox/status."""

    def test_anonymous_status(self):
        """Anonymous users get environment info without agent status."""
        resp = client.get("/sandbox/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["environment"] == "production"
        assert data["is_sandbox_instance"] is False
        assert data["agent_is_sandbox"] is None

    def test_authenticated_sandbox_status(self):
        """Authenticated sandbox agent sees their sandbox status."""
        agent = _register_agent(client, sandbox=True)
        resp = client.get("/sandbox/status", headers=agent["headers"])
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_is_sandbox"] is True

    def test_authenticated_production_status(self):
        """Authenticated production agent sees non-sandbox status."""
        agent = _register_agent(client, sandbox=False)
        resp = client.get("/sandbox/status", headers=agent["headers"])
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_is_sandbox"] is False


# ===========================================================================
# 5. Dry-Run Bet
# ===========================================================================

class TestDryRunBet:
    """Test X-Dry-Run: true header on POST /markets/{id}/bet."""

    def test_dry_run_bet_returns_simulation(self):
        """Dry-run bet returns simulated result without executing."""
        agent = _register_agent(client, sandbox=True)
        market = _create_market(client, agent["headers"])
        market_id = market["id"]

        balance_before = client.get("/me", headers=agent["headers"]).json()["balance"]

        resp = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES",
            "amount": 50,
        }, headers={**agent["headers"], "X-Dry-Run": "true"})

        assert resp.status_code == 200
        data = resp.json()

        # Verify it's a dry-run response
        assert data["dry_run"] is True
        assert data["market_id"] == market_id
        assert data["outcome"] == "YES"
        assert data["amount"] == 50
        assert data["shares"] > 0
        assert data["fee"] > 0
        assert data["total_cost"] > 50  # amount + fee
        assert data["probability_before"] != data["probability_after"]
        assert data["would_succeed"] is True
        assert data.get("rejection_reason") is None

        # Verify balance was NOT changed
        balance_after = client.get("/me", headers=agent["headers"]).json()["balance"]
        assert balance_after == balance_before

    def test_dry_run_bet_insufficient_balance(self):
        """Dry-run with insufficient balance returns would_succeed=false with reason."""
        agent = _register_agent(client, sandbox=True)
        market = _create_market(client, agent["headers"])
        market_id = market["id"]

        # Set balance to near zero
        user = db.get_user(agent["user_id"])
        db.update_user_balance(agent["user_id"], -(user["balance"] - 1.0))

        resp = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES",
            "amount": 100,
        }, headers={**agent["headers"], "X-Dry-Run": "true"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert data["would_succeed"] is False
        assert data["rejection_reason"] is not None
        assert "insufficient" in data["rejection_reason"].lower()

    def test_normal_bet_still_executes(self):
        """Without dry-run header, bet executes normally."""
        agent = _register_agent(client, sandbox=True)
        market = _create_market(client, agent["headers"])
        market_id = market["id"]

        balance_before = client.get("/me", headers=agent["headers"]).json()["balance"]

        resp = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES",
            "amount": 50,
        }, headers=agent["headers"])

        assert resp.status_code == 200
        data = resp.json()
        assert "dry_run" not in data  # Normal response
        assert "bet_id" in data

        # Verify balance WAS changed
        balance_after = client.get("/me", headers=agent["headers"]).json()["balance"]
        assert balance_after < balance_before


# ===========================================================================
# 6. Dry-Run Sell
# ===========================================================================

class TestDryRunSell:
    """Test X-Dry-Run: true header on POST /markets/{id}/sell."""

    def _setup_position(self, agent, market_id):
        """Place a bet to create a position, then return shares owned."""
        resp = client.post(f"/markets/{market_id}/bet", json={
            "outcome": "YES",
            "amount": 100,
        }, headers=agent["headers"])
        assert resp.status_code == 200
        return resp.json()["shares"]

    def test_dry_run_sell_returns_simulation(self):
        """Dry-run sell returns simulated result without executing."""
        agent = _register_agent(client, sandbox=True)
        market = _create_market(client, agent["headers"])
        market_id = market["id"]

        shares = self._setup_position(agent, market_id)
        sell_shares = shares / 2

        balance_before = client.get("/me", headers=agent["headers"]).json()["balance"]

        resp = client.post(f"/markets/{market_id}/sell", json={
            "outcome": "YES",
            "shares": sell_shares,
        }, headers={**agent["headers"], "X-Dry-Run": "true"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert data["market_id"] == market_id
        assert data["shares_to_sell"] == sell_shares
        assert data["amount_received"] > 0
        assert data["fee"] > 0
        assert data["would_succeed"] is True

        # Balance should NOT change
        balance_after = client.get("/me", headers=agent["headers"]).json()["balance"]
        assert balance_after == balance_before

    def test_dry_run_sell_no_position(self):
        """Dry-run sell with no position returns would_succeed=false."""
        agent = _register_agent(client, sandbox=True)
        other = _register_agent(client, sandbox=True, username="other_agent")
        market = _create_market(client, other["headers"])
        market_id = market["id"]

        resp = client.post(f"/markets/{market_id}/sell", json={
            "outcome": "YES",
            "shares": 10,
        }, headers={**agent["headers"], "X-Dry-Run": "true"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert data["would_succeed"] is False
        assert data["rejection_reason"] is not None


# ===========================================================================
# 7. Environment Detection
# ===========================================================================

class TestEnvironmentDetection:
    """Test sandbox.py environment helpers."""

    def test_default_environment(self):
        """Default environment should be 'production'."""
        from sandbox import get_environment
        # MOLTMARKETS_ENV not set → production
        old = os.environ.pop("MOLTMARKETS_ENV", None)
        try:
            assert get_environment() == "production"
        finally:
            if old:
                os.environ["MOLTMARKETS_ENV"] = old

    def test_sandbox_environment(self):
        """MOLTMARKETS_ENV=sandbox should return 'sandbox'."""
        from sandbox import get_environment
        old = os.environ.get("MOLTMARKETS_ENV")
        os.environ["MOLTMARKETS_ENV"] = "sandbox"
        try:
            assert get_environment() == "sandbox"
        finally:
            if old:
                os.environ["MOLTMARKETS_ENV"] = old
            else:
                os.environ.pop("MOLTMARKETS_ENV", None)

    def test_invalid_environment_falls_back(self):
        """Invalid MOLTMARKETS_ENV should fall back to 'production'."""
        from sandbox import get_environment
        old = os.environ.get("MOLTMARKETS_ENV")
        os.environ["MOLTMARKETS_ENV"] = "invalid_value"
        try:
            assert get_environment() == "production"
        finally:
            if old:
                os.environ["MOLTMARKETS_ENV"] = old
            else:
                os.environ.pop("MOLTMARKETS_ENV", None)


# ===========================================================================
# 8. Sandbox Helpers
# ===========================================================================

class TestSandboxHelpers:
    """Test sandbox.py helper functions."""

    def test_is_dry_run_true(self):
        """is_dry_run returns True for various truthy values."""
        from sandbox import is_dry_run
        assert is_dry_run({"X-Dry-Run": "true"}) is True
        assert is_dry_run({"x-dry-run": "TRUE"}) is True
        assert is_dry_run({"X-Dry-Run": "1"}) is True
        assert is_dry_run({"X-Dry-Run": "yes"}) is True

    def test_is_dry_run_false(self):
        """is_dry_run returns False for missing/falsy values."""
        from sandbox import is_dry_run
        assert is_dry_run({}) is False
        assert is_dry_run({"X-Dry-Run": "false"}) is False
        assert is_dry_run({"X-Dry-Run": ""}) is False

    def test_is_sandbox_agent(self):
        """is_sandbox_agent checks the user dict properly."""
        from sandbox import is_sandbox_agent
        assert is_sandbox_agent({"is_sandbox": True}) is True
        assert is_sandbox_agent({"is_sandbox": False}) is False
        assert is_sandbox_agent({}) is False

    def test_get_starting_balance(self):
        """get_starting_balance returns correct amounts."""
        from sandbox import get_starting_balance
        assert get_starting_balance(sandbox=True) == SANDBOX_STARTING_BALANCE
        assert get_starting_balance(sandbox=False) == STARTING_BALANCE
