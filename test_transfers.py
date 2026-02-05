"""
Tests for POST /transfers — agent-to-agent ŧ transfers.

See: https://github.com/shirtlessfounder/moltmarkets-api/issues/180
"""

import os
os.environ.pop("DATABASE_URL", None)

import pytest
from fastapi.testclient import TestClient

from api import app, db
from rate_limiter import rate_limiter

client = TestClient(app)


def _reset():
    """Reset storage and rate limiter between tests."""
    db._users.clear()
    if hasattr(db, "_transactions"):
        db._transactions.clear()
    rate_limiter._requests.clear()
    rate_limiter._call_count = 0
    if not db.get_user("demo-user"):
        db.create_user("demo-user", "demo_user", balance=0.0)


def _register(username: str, sandbox: bool = True) -> dict:
    """Register an agent and return {user_id, api_key, username}."""
    resp = client.post("/agents/register", json={
        "username": username,
        "display_name": username,
        "sandbox": sandbox,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return {
        "user_id": data["user_id"],
        "api_key": data["api_key"],
        "username": data["username"],
    }


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


class TestTransfers:
    """Test suite for POST /transfers."""

    def test_basic_transfer(self):
        """Happy path: sender transfers ŧ to recipient."""
        _reset()
        sender = _register("xfer_sender_1")
        recipient = _register("xfer_recipient_1")

        resp = client.post("/transfers", json={
            "recipient": recipient["username"],
            "amount": 50.0,
            "memo": "bounty payment",
        }, headers=_auth(sender["api_key"]))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["sender_id"] == sender["user_id"]
        assert data["recipient_id"] == recipient["user_id"]
        assert data["amount"] == 50.0
        assert data["memo"] == "bounty payment"
        assert data["currency"] == "ŧ"

    def test_transfer_by_user_id(self):
        """Can transfer using recipient's user_id instead of username."""
        _reset()
        sender = _register("xfer_sender_2")
        recipient = _register("xfer_recipient_2")

        resp = client.post("/transfers", json={
            "recipient": recipient["user_id"],
            "amount": 25.0,
        }, headers=_auth(sender["api_key"]))

        assert resp.status_code == 200, resp.text
        assert resp.json()["recipient_id"] == recipient["user_id"]

    def test_balance_updates(self):
        """Sender balance decreases, recipient balance increases."""
        _reset()
        sender = _register("xfer_sender_3")
        recipient = _register("xfer_recipient_3")

        # Get starting balances
        sender_before = client.get("/me", headers=_auth(sender["api_key"])).json()["balance"]
        recipient_before = client.get("/me", headers=_auth(recipient["api_key"])).json()["balance"]

        # Transfer 100ŧ
        resp = client.post("/transfers", json={
            "recipient": recipient["username"],
            "amount": 100.0,
        }, headers=_auth(sender["api_key"]))
        assert resp.status_code == 200

        # Check balances after
        sender_after = client.get("/me", headers=_auth(sender["api_key"])).json()["balance"]
        recipient_after = client.get("/me", headers=_auth(recipient["api_key"])).json()["balance"]

        assert sender_after == pytest.approx(sender_before - 100.0)
        assert recipient_after == pytest.approx(recipient_before + 100.0)

    def test_insufficient_balance(self):
        """Transfer fails if sender doesn't have enough ŧ."""
        _reset()
        sender = _register("xfer_sender_4")
        recipient = _register("xfer_recipient_4")

        # Drain sender's balance first with a legit transfer
        balance = client.get("/me", headers=_auth(sender["api_key"])).json()["balance"]
        # Transfer all but 1ŧ away, then try to send more than remaining
        if balance > 100:
            # Send most of it away in chunks
            while balance > 100:
                client.post("/transfers", json={
                    "recipient": recipient["username"],
                    "amount": min(balance - 1, 1000),
                }, headers=_auth(sender["api_key"]))
                balance = client.get("/me", headers=_auth(sender["api_key"])).json()["balance"]

        # Now try to transfer more than remaining
        resp = client.post("/transfers", json={
            "recipient": recipient["username"],
            "amount": 999.0,
        }, headers=_auth(sender["api_key"]))

        assert resp.status_code == 400
        assert "Insufficient balance" in resp.json()["error"]

    def test_self_transfer(self):
        """Cannot transfer to yourself."""
        _reset()
        sender = _register("xfer_sender_5")

        resp = client.post("/transfers", json={
            "recipient": sender["username"],
            "amount": 10.0,
        }, headers=_auth(sender["api_key"]))

        assert resp.status_code == 400
        assert "yourself" in resp.json()["error"]

    def test_nonexistent_recipient(self):
        """Transfer to nonexistent user returns 404."""
        _reset()
        sender = _register("xfer_sender_6")

        resp = client.post("/transfers", json={
            "recipient": "totally_fake_user_12345",
            "amount": 10.0,
        }, headers=_auth(sender["api_key"]))

        assert resp.status_code == 404

    def test_no_auth(self):
        """Transfer without auth fails."""
        _reset()
        resp = client.post("/transfers", json={
            "recipient": "someone",
            "amount": 10.0,
        })

        assert resp.status_code == 401

    def test_amount_too_large(self):
        """Transfer above max amount fails."""
        _reset()
        sender = _register("xfer_sender_7")
        recipient = _register("xfer_recipient_7")

        resp = client.post("/transfers", json={
            "recipient": recipient["username"],
            "amount": 1001.0,
        }, headers=_auth(sender["api_key"]))

        assert resp.status_code in (400, 422)  # Pydantic or route validation

    def test_zero_amount(self):
        """Transfer of zero ŧ fails."""
        _reset()
        sender = _register("xfer_sender_8")
        recipient = _register("xfer_recipient_8")

        resp = client.post("/transfers", json={
            "recipient": recipient["username"],
            "amount": 0,
        }, headers=_auth(sender["api_key"]))

        assert resp.status_code == 422  # Pydantic gt=0

    def test_transaction_ledger(self):
        """Transfer creates ledger entries for both sender and recipient."""
        _reset()
        sender = _register("xfer_sender_9")
        recipient = _register("xfer_recipient_9")

        client.post("/transfers", json={
            "recipient": recipient["username"],
            "amount": 42.0,
            "memo": "test ledger",
        }, headers=_auth(sender["api_key"]))

        # Check sender's transactions
        sender_txns = client.get("/me/transactions", headers=_auth(sender["api_key"])).json()
        transfer_out = [t for t in sender_txns if t["type"] == "transfer_out"]
        assert len(transfer_out) >= 1
        assert transfer_out[0]["amount"] == -42.0
        assert transfer_out[0]["related_user_id"] == recipient["user_id"]

        # Check recipient's transactions
        recipient_txns = client.get("/me/transactions", headers=_auth(recipient["api_key"])).json()
        transfer_in = [t for t in recipient_txns if t["type"] == "transfer_in"]
        assert len(transfer_in) >= 1
        assert transfer_in[0]["amount"] == 42.0
        assert transfer_in[0]["related_user_id"] == sender["user_id"]
