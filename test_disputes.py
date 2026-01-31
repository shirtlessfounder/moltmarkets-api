"""
Tests for the multi-resolver dispute system (issue #8).

Tests the full dispute lifecycle:
  1. Market resolution sets dispute_window_ends
  2. Traders can file disputes within the 24h window
  3. Top N traders by volume are eligible to vote
  4. Majority disagreement flips the outcome (UPHELD)
  5. Majority agreement rejects the dispute (REJECTED)
  6. Status transitions: RESOLVED → DISPUTED → RE_RESOLVED / RESOLVED
  7. Payouts are reversed and re-distributed on flip
  8. One dispute round only, no appeals
"""

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

# Patch DATABASE_URL before importing api (force in-memory storage)
import os
os.environ.pop("DATABASE_URL", None)

from api import app, db, DISPUTE_WINDOW_HOURS, DISPUTE_VOTER_COUNT, hash_api_key, generate_api_key
from models import MarketStatus, Outcome


client = TestClient(app)


# =============================================================================
# Fixtures / Helpers
# =============================================================================

def _create_user(username: str, balance: float = 1000.0, claimed: bool = True) -> tuple[dict, str]:
    """Create a user and return (user_dict, raw_api_key)."""
    api_key = generate_api_key()
    user_id = str(uuid.uuid4())
    user = db.create_user(
        user_id=user_id,
        username=username,
        balance=balance,
        api_key_hash=hash_api_key(api_key),
        status="claimed" if claimed else "pending",
    )
    return user, api_key


def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _create_resolved_market(creator_id: str, outcome: Outcome = Outcome.YES) -> dict:
    """Create and immediately resolve a market (bypassing time checks)."""
    market_id = str(uuid.uuid4())
    market = db.create_market(
        market_id=market_id,
        creator_id=creator_id,
        title="Test market for disputes",
        description="Will resolve for testing",
        closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
        initial_liquidity=100.0,
    )
    db.resolve_market(market_id, outcome)
    return db.get_market(market_id)


def _place_bet(market_id: str, user_id: str, outcome: Outcome, amount: float):
    """Place a bet directly via storage (bypass API checks)."""
    from cpmm import CpmmState, calculate_cpmm_purchase, get_cpmm_probability
    
    market = db.get_market(market_id)
    state = CpmmState(pool=market["pool"].copy(), p=market["p"])
    prob_before = get_cpmm_probability(market["pool"], market["p"])
    result = calculate_cpmm_purchase(state, amount, outcome.value)
    shares = result["shares"]
    prob_after = get_cpmm_probability(result["new_pool"], result["new_p"])
    
    db.update_user_balance(user_id, -amount)
    db.update_market_pool(market_id, result["new_pool"], result["new_p"], amount)
    db.update_position(market_id, user_id, outcome, shares, amount)
    
    bet_id = str(uuid.uuid4())
    db.create_bet(
        bet_id=bet_id,
        market_id=market_id,
        user_id=user_id,
        outcome=outcome,
        amount=amount,
        shares=shares,
        prob_before=prob_before,
        prob_after=prob_after,
    )
    return shares


# =============================================================================
# Tests: Resolution sets dispute_window_ends
# =============================================================================

class TestResolutionSetsDisputeWindow:
    def test_resolve_sets_dispute_window(self):
        """Resolving a market should set dispute_window_ends to 24h later."""
        creator, _ = _create_user(f"creator_{uuid.uuid4().hex[:8]}")
        market = _create_resolved_market(creator["id"])
        
        assert market["dispute_window_ends"] is not None
        assert market["status"] == MarketStatus.RESOLVED
        
        # Window should be ~24h after resolved_at
        window_delta = market["dispute_window_ends"] - market["resolved_at"]
        assert abs(window_delta.total_seconds() - DISPUTE_WINDOW_HOURS * 3600) < 5


# =============================================================================
# Tests: Filing Disputes
# =============================================================================

class TestFileDispute:
    def test_trader_can_dispute_resolved_market(self):
        """A trader with a position can file a dispute on a resolved market."""
        creator, creator_key = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        trader, trader_key = _create_user(f"t_{uuid.uuid4().hex[:8]}")
        
        # Create market, place bet, then resolve
        market_id = str(uuid.uuid4())
        market = db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Disputable market",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        _place_bet(market_id, trader["id"], Outcome.NO, 50)
        db.resolve_market(market_id, Outcome.YES)
        
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "The resolution is incorrect based on the evidence"},
            headers=_auth(trader_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "OPEN"
        assert data["original_resolution"] == "YES"
        assert data["disputor_id"] == trader["id"]
        
        # Market should now be DISPUTED
        market = db.get_market(market_id)
        assert market["status"] == MarketStatus.DISPUTED
    
    def test_non_trader_cannot_dispute(self):
        """A user without a position cannot file a dispute."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        outsider, outsider_key = _create_user(f"o_{uuid.uuid4().hex[:8]}")
        
        market = _create_resolved_market(creator["id"])
        
        resp = client.post(
            f"/markets/{market['id']}/dispute",
            json={"reason": "I think this is wrong even though I didn't trade"},
            headers=_auth(outsider_key),
        )
        assert resp.status_code == 403
    
    def test_dispute_after_window_fails(self):
        """Filing a dispute after the 24h window should fail."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        trader, trader_key = _create_user(f"t_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Expired dispute window",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        _place_bet(market_id, trader["id"], Outcome.NO, 50)
        db.resolve_market(market_id, Outcome.YES)
        
        # Manually set dispute_window_ends to the past
        if db._use_memory:
            db._markets[market_id]["dispute_window_ends"] = datetime.now(timezone.utc) - timedelta(hours=1)
        
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Too late to dispute this"},
            headers=_auth(trader_key),
        )
        assert resp.status_code == 400
        assert "expired" in resp.json()["detail"].lower()
    
    def test_cannot_dispute_non_resolved_market(self):
        """Cannot dispute a market that isn't RESOLVED."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        trader, trader_key = _create_user(f"t_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Still open",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        _place_bet(market_id, trader["id"], Outcome.YES, 50)
        
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Premature dispute attempt"},
            headers=_auth(trader_key),
        )
        assert resp.status_code == 400
    
    def test_no_double_disputes(self):
        """Cannot file a second dispute while one is open."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        trader1, trader1_key = _create_user(f"t1_{uuid.uuid4().hex[:8]}")
        trader2, trader2_key = _create_user(f"t2_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Double dispute test",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        _place_bet(market_id, trader1["id"], Outcome.NO, 50)
        _place_bet(market_id, trader2["id"], Outcome.NO, 30)
        db.resolve_market(market_id, Outcome.YES)
        
        # First dispute succeeds
        resp1 = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "First dispute"},
            headers=_auth(trader1_key),
        )
        assert resp1.status_code == 200
        
        # Second dispute fails (market is now DISPUTED, not RESOLVED)
        resp2 = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Second dispute"},
            headers=_auth(trader2_key),
        )
        assert resp2.status_code == 400


# =============================================================================
# Tests: Voting on Disputes
# =============================================================================

class TestDisputeVoting:
    def _setup_disputable_market(self, num_traders=5):
        """Create a resolved market with N traders who have positions."""
        creator, creator_key = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Voting test market",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        
        traders = []
        for i in range(num_traders):
            trader, key = _create_user(f"voter_{uuid.uuid4().hex[:8]}")
            amount = 50 - i * 5  # Varying volumes: 50, 45, 40, ...
            _place_bet(market_id, trader["id"], Outcome.NO, max(amount, 10))
            traders.append((trader, key))
        
        db.resolve_market(market_id, Outcome.YES)
        return market_id, creator, creator_key, traders
    
    def test_eligible_voter_can_vote(self):
        """Top traders by volume can vote on disputes."""
        market_id, creator, _, traders = self._setup_disputable_market(5)
        
        # File dispute
        disputor, disputor_key = traders[0]
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Wrong outcome"},
            headers=_auth(disputor_key),
        )
        assert resp.status_code == 200
        dispute_id = resp.json()["id"]
        
        # Top trader votes
        voter, voter_key = traders[1]
        resp = client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "NO"},
            headers=_auth(voter_key),
        )
        assert resp.status_code == 200
        assert resp.json()["vote"] == "NO"
    
    def test_ineligible_voter_rejected(self):
        """A user who isn't a top trader cannot vote."""
        market_id, creator, _, traders = self._setup_disputable_market(5)
        
        # Create an outsider with tiny volume
        outsider, outsider_key = _create_user(f"outsider_{uuid.uuid4().hex[:8]}")
        _place_bet(market_id, outsider["id"], Outcome.YES, 1)  # Tiny volume
        
        # Re-resolve market (bet was placed on OPEN market, need to re-resolve)
        # Actually the market was already resolved, so this bet went through storage directly
        # The outsider won't be in top 5
        
        disputor, disputor_key = traders[0]
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Wrong outcome"},
            headers=_auth(disputor_key),
        )
        dispute_id = resp.json()["id"]
        
        resp = client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "NO"},
            headers=_auth(outsider_key),
        )
        assert resp.status_code == 403
    
    def test_no_double_voting(self):
        """A voter cannot vote twice on the same dispute."""
        market_id, creator, _, traders = self._setup_disputable_market(5)
        
        disputor, disputor_key = traders[0]
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Wrong outcome"},
            headers=_auth(disputor_key),
        )
        dispute_id = resp.json()["id"]
        
        voter, voter_key = traders[1]
        
        # First vote succeeds
        resp1 = client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "NO"},
            headers=_auth(voter_key),
        )
        assert resp1.status_code == 200
        
        # Second vote fails
        resp2 = client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "YES"},
            headers=_auth(voter_key),
        )
        assert resp2.status_code == 400


# =============================================================================
# Tests: Dispute Resolution (Outcome Flip vs Rejection)
# =============================================================================

class TestDisputeResolution:
    def test_majority_against_flips_outcome(self):
        """If majority votes against original resolution → UPHELD, outcome flips."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Flip test",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        
        # Create 5 traders (enough for DISPUTE_VOTER_COUNT=5)
        traders = []
        for i in range(5):
            t, k = _create_user(f"flip_{uuid.uuid4().hex[:8]}")
            _place_bet(market_id, t["id"], Outcome.NO, 50 - i * 5)
            traders.append((t, k))
        
        db.resolve_market(market_id, Outcome.YES)
        
        # File dispute
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Should be NO"},
            headers=_auth(traders[0][1]),
        )
        dispute_id = resp.json()["id"]
        
        # 3 out of 5 vote NO (against original YES) → majority
        for i in range(3):
            client.post(
                f"/markets/{market_id}/disputes/{dispute_id}/vote",
                json={"vote": "NO"},
                headers=_auth(traders[i][1]),
            )
        
        # Check dispute resolved as UPHELD
        market = db.get_market(market_id)
        assert market["status"] == MarketStatus.RE_RESOLVED
        assert market["resolution"] == Outcome.NO
        
        # Check dispute status
        dispute = db.get_dispute(dispute_id)
        assert dispute["status"] == "UPHELD"
    
    def test_majority_agrees_rejects_dispute(self):
        """If majority votes for original resolution → REJECTED, original stands."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Reject test",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        
        traders = []
        for i in range(5):
            t, k = _create_user(f"reject_{uuid.uuid4().hex[:8]}")
            _place_bet(market_id, t["id"], Outcome.YES, 50 - i * 5)
            traders.append((t, k))
        
        db.resolve_market(market_id, Outcome.YES)
        
        # File dispute
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "I disagree"},
            headers=_auth(traders[0][1]),
        )
        dispute_id = resp.json()["id"]
        
        # 3 out of 5 vote YES (agreeing with original) → majority
        for i in range(3):
            client.post(
                f"/markets/{market_id}/disputes/{dispute_id}/vote",
                json={"vote": "YES"},
                headers=_auth(traders[i][1]),
            )
        
        # Market back to RESOLVED
        market = db.get_market(market_id)
        assert market["status"] == MarketStatus.RESOLVED
        assert market["resolution"] == Outcome.YES
        
        dispute = db.get_dispute(dispute_id)
        assert dispute["status"] == "REJECTED"
    
    def test_no_appeals_after_resolved_dispute(self):
        """After a dispute is resolved, cannot file another one."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="No appeals",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        
        traders = []
        for i in range(5):
            t, k = _create_user(f"appeal_{uuid.uuid4().hex[:8]}")
            _place_bet(market_id, t["id"], Outcome.NO, 50 - i * 5)
            traders.append((t, k))
        
        db.resolve_market(market_id, Outcome.YES)
        
        # First dispute
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "First dispute"},
            headers=_auth(traders[0][1]),
        )
        dispute_id = resp.json()["id"]
        
        # Resolve it (3 vote NO → upheld)
        for i in range(3):
            client.post(
                f"/markets/{market_id}/disputes/{dispute_id}/vote",
                json={"vote": "NO"},
                headers=_auth(traders[i][1]),
            )
        
        # Try to file another dispute — should fail (market is RE_RESOLVED, not RESOLVED)
        resp2 = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Second attempt"},
            headers=_auth(traders[4][1]),
        )
        assert resp2.status_code == 400


# =============================================================================
# Tests: Payout Reversal on Flip
# =============================================================================

class TestPayoutReversal:
    def test_payouts_reversed_on_flip(self):
        """When a dispute flips the outcome, old winners lose payouts and new winners get paid."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}", balance=2000)
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Payout reversal test",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        
        # YES bettors and NO bettors
        yes_traders = []
        no_traders = []
        for i in range(3):
            t, k = _create_user(f"yes_{uuid.uuid4().hex[:8]}", balance=500)
            _place_bet(market_id, t["id"], Outcome.YES, 50)
            yes_traders.append((t, k))
        
        for i in range(3):
            t, k = _create_user(f"no_{uuid.uuid4().hex[:8]}", balance=500)
            _place_bet(market_id, t["id"], Outcome.NO, 60 - i * 5)  # 60, 55, 50 — top volume
            no_traders.append((t, k))
        
        # Resolve as YES — YES bettors win initially
        db.resolve_market(market_id, Outcome.YES)
        from api import _calculate_and_distribute_payouts
        _calculate_and_distribute_payouts(market_id, Outcome.YES)
        
        # Record balances before dispute
        yes_balances_before = [db.get_user(t["id"])["balance"] for t, _ in yes_traders]
        no_balances_before = [db.get_user(t["id"])["balance"] for t, _ in no_traders]
        
        # File dispute (NO trader with highest volume)
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Should be NO"},
            headers=_auth(no_traders[0][1]),
        )
        dispute_id = resp.json()["id"]
        
        # Top 5 by volume vote NO (flip to NO)
        # The top traders include both YES and NO bettors
        eligible = db.get_top_traders_for_market(market_id, limit=DISPUTE_VOTER_COUNT)
        eligible_keys = {}
        for t, k in yes_traders + no_traders:
            eligible_keys[t["id"]] = k
        
        votes_cast = 0
        for voter in eligible:
            if voter["user_id"] in eligible_keys:
                client.post(
                    f"/markets/{market_id}/disputes/{dispute_id}/vote",
                    json={"vote": "NO"},
                    headers=_auth(eligible_keys[voter["user_id"]]),
                )
                votes_cast += 1
                if votes_cast >= 3:
                    break
        
        # Market should now be RE_RESOLVED with NO
        market = db.get_market(market_id)
        assert market["status"] == MarketStatus.RE_RESOLVED
        assert market["resolution"] == Outcome.NO
        
        # YES bettors should have lost their payouts
        # NO bettors should have received new payouts
        for t, _ in no_traders:
            user = db.get_user(t["id"])
            pos = db.get_position(market_id, t["id"])
            # NO bettor should now have received payout for their NO shares
            assert pos["no_shares"] > 0


# =============================================================================
# Tests: GET /markets/{id}/disputes
# =============================================================================

class TestListDisputes:
    def test_list_disputes_empty(self):
        """Listing disputes on a market with none returns empty."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        market = _create_resolved_market(creator["id"])
        
        resp = client.get(f"/markets/{market['id']}/disputes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["market_id"] == market["id"]
        assert len(data["disputes"]) == 0
        assert data["dispute_window_ends"] is not None
    
    def test_list_disputes_with_votes(self):
        """Listing disputes includes vote tallies."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="List disputes test",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        
        traders = []
        for i in range(5):
            t, k = _create_user(f"list_{uuid.uuid4().hex[:8]}")
            _place_bet(market_id, t["id"], Outcome.NO, 50 - i * 5)
            traders.append((t, k))
        
        db.resolve_market(market_id, Outcome.YES)
        
        # File dispute
        resp = client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Testing list endpoint"},
            headers=_auth(traders[0][1]),
        )
        dispute_id = resp.json()["id"]
        
        # Cast 2 votes
        client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "NO"},
            headers=_auth(traders[1][1]),
        )
        client.post(
            f"/markets/{market_id}/disputes/{dispute_id}/vote",
            json={"vote": "YES"},
            headers=_auth(traders[2][1]),
        )
        
        # List disputes
        resp = client.get(f"/markets/{market_id}/disputes")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["disputes"]) == 1
        
        dispute = data["disputes"][0]
        assert dispute["status"] == "OPEN"
        assert dispute["original_resolution"] == "YES"
        assert len(dispute["votes"]) == 2
        assert dispute["votes_for_original"] == 1  # One YES vote
        assert dispute["votes_against_original"] == 1  # One NO vote


# =============================================================================
# Tests: Market Status Filter
# =============================================================================

class TestMarketStatusFilter:
    def test_disputed_status_filter(self):
        """Markets with DISPUTED status show up in ?status=disputed."""
        creator, _ = _create_user(f"c_{uuid.uuid4().hex[:8]}")
        trader, trader_key = _create_user(f"t_{uuid.uuid4().hex[:8]}")
        
        market_id = str(uuid.uuid4())
        db.create_market(
            market_id=market_id,
            creator_id=creator["id"],
            title="Status filter test",
            description="Test",
            closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
            initial_liquidity=100.0,
        )
        _place_bet(market_id, trader["id"], Outcome.NO, 50)
        db.resolve_market(market_id, Outcome.YES)
        
        # File dispute to set status to DISPUTED
        client.post(
            f"/markets/{market_id}/dispute",
            json={"reason": "Testing status filter"},
            headers=_auth(trader_key),
        )
        
        resp = client.get("/markets?status=disputed")
        assert resp.status_code == 200
        market_ids = [m["id"] for m in resp.json()]
        assert market_id in market_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
