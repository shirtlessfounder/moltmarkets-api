"""
Tests for bounty escrow endpoints (issue #180).

Covers full lifecycle: create → claim → release, plus cancel, edge cases.
"""

import os
os.environ.pop("DATABASE_URL", None)

import pytest
from fastapi.testclient import TestClient

from api import app, db
from rate_limiter import rate_limiter

client = TestClient(app)


def _reset():
    """Reset storage and rate limiter."""
    db._users.clear()
    if hasattr(db, "_bounties"):
        db._bounties.clear()
    if hasattr(db, "_transactions"):
        db._transactions.clear()
    rate_limiter._requests.clear()
    rate_limiter._call_count = 0
    if not db.get_user("demo-user"):
        db.create_user("demo-user", "demo_user", balance=0.0)


def _register(username: str) -> dict:
    resp = client.post("/agents/register", json={
        "username": username,
        "display_name": username,
        "sandbox": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return {"user_id": data["user_id"], "api_key": data["api_key"], "username": data["username"]}


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


class TestBountyLifecycle:
    """Full bounty lifecycle: create → claim → release."""

    def test_create_bounty(self):
        _reset()
        creator = _register("bounty_creator_1")

        resp = client.post("/bounties", json={
            "title": "Fix the CSS bug",
            "amount": 50.0,
            "description": "The header is misaligned on mobile",
        }, headers=_auth(creator["api_key"]))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["title"] == "Fix the CSS bug"
        assert data["amount"] == 50.0
        assert data["status"] == "open"
        assert data["creator_id"] == creator["user_id"]
        assert data["claimant_id"] is None
        assert data["currency"] == "ŧ"

    def test_create_deducts_balance(self):
        _reset()
        creator = _register("bounty_creator_2")

        before = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]

        client.post("/bounties", json={
            "title": "Do a thing",
            "amount": 75.0,
        }, headers=_auth(creator["api_key"]))

        after = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]
        assert after == pytest.approx(before - 75.0)

    def test_full_lifecycle(self):
        """Create → claim → release: full happy path."""
        _reset()
        creator = _register("bounty_creator_3")
        worker = _register("bounty_worker_3")

        creator_before = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]
        worker_before = client.get("/me", headers=_auth(worker["api_key"])).json()["balance"]

        # Create
        resp = client.post("/bounties", json={
            "title": "Write a test suite",
            "amount": 100.0,
        }, headers=_auth(creator["api_key"]))
        assert resp.status_code == 200
        bounty_id = resp.json()["id"]

        # Creator balance reduced
        creator_mid = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]
        assert creator_mid == pytest.approx(creator_before - 100.0)

        # Claim
        resp = client.post(f"/bounties/{bounty_id}/claim",
                          headers=_auth(worker["api_key"]))
        assert resp.status_code == 200
        assert resp.json()["status"] == "claimed"
        assert resp.json()["claimant_id"] == worker["user_id"]

        # Release
        resp = client.post(f"/bounties/{bounty_id}/release",
                          headers=_auth(creator["api_key"]))
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

        # Worker got paid
        worker_after = client.get("/me", headers=_auth(worker["api_key"])).json()["balance"]
        assert worker_after == pytest.approx(worker_before + 100.0)

        # Creator balance still deducted (escrow was released, not refunded)
        creator_after = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]
        assert creator_after == pytest.approx(creator_before - 100.0)

    def test_cancel_open_bounty(self):
        """Cancel an open bounty: full refund."""
        _reset()
        creator = _register("bounty_creator_4")

        before = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]

        resp = client.post("/bounties", json={
            "title": "Cancelled task",
            "amount": 60.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        # Cancel
        resp = client.post(f"/bounties/{bounty_id}/cancel",
                          headers=_auth(creator["api_key"]))
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        # Full refund
        after = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]
        assert after == pytest.approx(before)

    def test_cancel_claimed_bounty(self):
        """Cancel a claimed bounty: refund to creator, worker gets nothing."""
        _reset()
        creator = _register("bounty_creator_5")
        worker = _register("bounty_worker_5")

        resp = client.post("/bounties", json={
            "title": "Rejected work",
            "amount": 80.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        # Claim
        client.post(f"/bounties/{bounty_id}/claim",
                    headers=_auth(worker["api_key"]))

        # Cancel (reject)
        resp = client.post(f"/bounties/{bounty_id}/cancel",
                          headers=_auth(creator["api_key"]))
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


class TestBountyEdgeCases:
    """Edge cases and validation."""

    def test_claim_own_bounty(self):
        _reset()
        creator = _register("bounty_edge_1")

        resp = client.post("/bounties", json={
            "title": "Self-assign",
            "amount": 10.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        resp = client.post(f"/bounties/{bounty_id}/claim",
                          headers=_auth(creator["api_key"]))
        assert resp.status_code == 400
        assert "own bounty" in resp.json()["error"]

    def test_double_claim(self):
        _reset()
        creator = _register("bounty_edge_2")
        worker1 = _register("bounty_worker_2a")
        worker2 = _register("bounty_worker_2b")

        resp = client.post("/bounties", json={
            "title": "Race condition",
            "amount": 25.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        # First claim succeeds
        resp1 = client.post(f"/bounties/{bounty_id}/claim",
                           headers=_auth(worker1["api_key"]))
        assert resp1.status_code == 200

        # Second claim fails
        resp2 = client.post(f"/bounties/{bounty_id}/claim",
                           headers=_auth(worker2["api_key"]))
        assert resp2.status_code == 400

    def test_release_unclaimed(self):
        _reset()
        creator = _register("bounty_edge_3")

        resp = client.post("/bounties", json={
            "title": "Not yet claimed",
            "amount": 30.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        resp = client.post(f"/bounties/{bounty_id}/release",
                          headers=_auth(creator["api_key"]))
        assert resp.status_code == 400

    def test_non_creator_release(self):
        _reset()
        creator = _register("bounty_edge_4")
        worker = _register("bounty_worker_4")

        resp = client.post("/bounties", json={
            "title": "Unauthorized release",
            "amount": 40.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        client.post(f"/bounties/{bounty_id}/claim",
                    headers=_auth(worker["api_key"]))

        # Worker tries to release (should fail)
        resp = client.post(f"/bounties/{bounty_id}/release",
                          headers=_auth(worker["api_key"]))
        assert resp.status_code == 403

    def test_non_creator_cancel(self):
        _reset()
        creator = _register("bounty_edge_5")
        other = _register("bounty_other_5")

        resp = client.post("/bounties", json={
            "title": "Unauthorized cancel",
            "amount": 20.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        resp = client.post(f"/bounties/{bounty_id}/cancel",
                          headers=_auth(other["api_key"]))
        assert resp.status_code == 403

    def test_cancel_completed(self):
        """Can't cancel a completed bounty."""
        _reset()
        creator = _register("bounty_edge_6")
        worker = _register("bounty_worker_6")

        resp = client.post("/bounties", json={
            "title": "Already done",
            "amount": 50.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        client.post(f"/bounties/{bounty_id}/claim", headers=_auth(worker["api_key"]))
        client.post(f"/bounties/{bounty_id}/release", headers=_auth(creator["api_key"]))

        resp = client.post(f"/bounties/{bounty_id}/cancel",
                          headers=_auth(creator["api_key"]))
        assert resp.status_code == 400

    def test_insufficient_balance(self):
        _reset()
        creator = _register("bounty_edge_7")

        # Drain balance first
        other = _register("bounty_drain_7")
        balance = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]
        while balance > 100:
            client.post("/transfers", json={
                "recipient": other["username"],
                "amount": min(balance - 1, 1000),
            }, headers=_auth(creator["api_key"]))
            balance = client.get("/me", headers=_auth(creator["api_key"])).json()["balance"]

        resp = client.post("/bounties", json={
            "title": "Can't afford this",
            "amount": 999.0,
        }, headers=_auth(creator["api_key"]))
        assert resp.status_code == 400
        assert "Insufficient" in resp.json()["error"]

    def test_list_bounties(self):
        _reset()
        creator = _register("bounty_list_1")

        # Create a few bounties
        for i in range(3):
            client.post("/bounties", json={
                "title": f"Bounty {i}",
                "amount": 10.0,
            }, headers=_auth(creator["api_key"]))

        resp = client.get("/bounties")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_list_bounties_filter_status(self):
        _reset()
        creator = _register("bounty_list_2")
        worker = _register("bounty_worker_list")

        # Create 2, claim 1
        resp1 = client.post("/bounties", json={"title": "Open one", "amount": 10.0},
                           headers=_auth(creator["api_key"]))
        resp2 = client.post("/bounties", json={"title": "Claimed one", "amount": 10.0},
                           headers=_auth(creator["api_key"]))
        client.post(f"/bounties/{resp2.json()['id']}/claim", headers=_auth(worker["api_key"]))

        open_bounties = client.get("/bounties?status=open").json()
        claimed_bounties = client.get("/bounties?status=claimed").json()
        assert len(open_bounties) == 1
        assert len(claimed_bounties) == 1

    def test_transaction_ledger(self):
        """Full cycle creates proper ledger entries."""
        _reset()
        creator = _register("bounty_ledger_1")
        worker = _register("bounty_ledger_w1")

        resp = client.post("/bounties", json={
            "title": "Ledger test",
            "amount": 42.0,
        }, headers=_auth(creator["api_key"]))
        bounty_id = resp.json()["id"]

        client.post(f"/bounties/{bounty_id}/claim", headers=_auth(worker["api_key"]))
        client.post(f"/bounties/{bounty_id}/release", headers=_auth(creator["api_key"]))

        # Creator should have escrow_lock
        creator_txns = client.get("/me/transactions", headers=_auth(creator["api_key"])).json()
        lock_txns = [t for t in creator_txns if t["type"] == "escrow_lock"]
        assert len(lock_txns) >= 1
        assert lock_txns[0]["amount"] == -42.0

        # Worker should have escrow_release
        worker_txns = client.get("/me/transactions", headers=_auth(worker["api_key"])).json()
        release_txns = [t for t in worker_txns if t["type"] == "escrow_release"]
        assert len(release_txns) >= 1
        assert release_txns[0]["amount"] == 42.0
