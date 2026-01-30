"""
Tests for the agent reputation system.

Run with: python -m pytest test_reputation.py -v
"""

import pytest
from datetime import datetime, timezone
from reputation import (
    compute_reputation,
    compute_trading_score,
    compute_resolution_score,
    compute_creation_score,
    compute_participation_score,
    get_tier,
    BASELINE_SCORE,
)


# =============================================================================
# Fixtures
# =============================================================================

def make_user(user_id="user-1", username="testbot", balance=1000.0,
              profit_all_time=0.0, markets_created=0, total_bets=0):
    return {
        "id": user_id,
        "username": username,
        "display_name": username,
        "balance": balance,
        "profit_all_time": profit_all_time,
        "markets_created": markets_created,
        "total_bets": total_bets,
        "status": "claimed",
        "created_at": datetime.now(timezone.utc),
    }


def make_market(market_id, creator_id="other", status="OPEN",
                resolution=None, total_volume=0.0):
    return {
        "id": market_id,
        "title": f"Market {market_id}",
        "description": "",
        "status": status,
        "closes_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
        "resolved_at": None,
        "resolution": resolution,
        "total_volume": total_volume,
        "creator_id": creator_id,
        "pool": {"YES": 100, "NO": 100},
        "p": 0.5,
    }


def make_bet(market_id, user_id="user-1", outcome="YES", amount=10.0, shares=15.0):
    return {
        "id": f"bet-{market_id}-{user_id}",
        "market_id": market_id,
        "user_id": user_id,
        "outcome": outcome,
        "amount": amount,
        "shares": shares,
        "avg_price": amount / shares if shares > 0 else 0,
        "probability_before": 0.5,
        "probability_after": 0.55,
        "created_at": datetime.now(timezone.utc),
    }


# =============================================================================
# Tier Tests
# =============================================================================

class TestTier:
    def test_new_tier(self):
        assert get_tier(20) == "New"
        assert get_tier(39) == "New"

    def test_bronze_tier(self):
        assert get_tier(40) == "Bronze"
        assert get_tier(54) == "Bronze"

    def test_silver_tier(self):
        assert get_tier(55) == "Silver"
        assert get_tier(69) == "Silver"

    def test_gold_tier(self):
        assert get_tier(70) == "Gold"
        assert get_tier(84) == "Gold"

    def test_platinum_tier(self):
        assert get_tier(85) == "Platinum"
        assert get_tier(100) == "Platinum"


# =============================================================================
# Trading Score Tests
# =============================================================================

class TestTradingScore:
    def test_no_bets_gives_baseline(self):
        user = make_user()
        score = compute_trading_score(user, [], {})
        assert score.score == BASELINE_SCORE
        assert score.resolved_bets == 0
        assert score.win_rate == 0.0

    def test_positive_pnl_above_baseline(self):
        user = make_user(profit_all_time=200.0)
        market = make_market("m1", status="RESOLVED", resolution="YES")
        bet = make_bet("m1", outcome="YES")
        
        score = compute_trading_score(user, [bet], {"m1": market})
        assert score.score > BASELINE_SCORE
        assert score.total_pnl == 200.0
        assert score.resolved_bets == 1
        assert score.win_rate == 1.0

    def test_negative_pnl_below_baseline(self):
        user = make_user(profit_all_time=-200.0)
        market = make_market("m1", status="RESOLVED", resolution="NO")
        bet = make_bet("m1", outcome="YES")
        
        score = compute_trading_score(user, [bet], {"m1": market})
        assert score.score < BASELINE_SCORE
        assert score.win_rate == 0.0

    def test_mixed_results(self):
        user = make_user(profit_all_time=50.0)
        markets = {
            "m1": make_market("m1", status="RESOLVED", resolution="YES"),
            "m2": make_market("m2", status="RESOLVED", resolution="NO"),
        }
        bets = [
            make_bet("m1", outcome="YES"),  # Win
            make_bet("m2", outcome="YES"),  # Loss
        ]
        
        score = compute_trading_score(user, bets, markets)
        assert score.resolved_bets == 2
        assert score.win_rate == 0.5


# =============================================================================
# Resolution Score Tests
# =============================================================================

class TestResolutionScore:
    def test_no_votes_gives_baseline(self):
        score = compute_resolution_score("user-1", [], {})
        assert score.score == BASELINE_SCORE
        assert score.total_votes == 0

    def test_perfect_accuracy(self):
        markets = {
            "m1": make_market("m1", status="RESOLVED", resolution="YES"),
        }
        votes = [
            {"agent_id": "user-1", "market_id": "m1", "vote": "YES"},
        ]
        
        score = compute_resolution_score("user-1", votes, markets)
        assert score.accuracy == 1.0
        assert score.correct_votes == 1
        assert score.score > BASELINE_SCORE

    def test_zero_accuracy(self):
        markets = {
            "m1": make_market("m1", status="RESOLVED", resolution="NO"),
        }
        votes = [
            {"agent_id": "user-1", "market_id": "m1", "vote": "YES"},
        ]
        
        score = compute_resolution_score("user-1", votes, markets)
        assert score.accuracy == 0.0
        assert score.correct_votes == 0

    def test_ignores_other_agents_votes(self):
        markets = {
            "m1": make_market("m1", status="RESOLVED", resolution="YES"),
        }
        votes = [
            {"agent_id": "other-agent", "market_id": "m1", "vote": "NO"},
        ]
        
        score = compute_resolution_score("user-1", votes, markets)
        assert score.total_votes == 0


# =============================================================================
# Creation Score Tests
# =============================================================================

class TestCreationScore:
    def test_no_markets_low_score(self):
        user = make_user()
        score = compute_creation_score(user, {}, [])
        assert score.markets_created == 0
        assert score.score < BASELINE_SCORE

    def test_markets_with_volume(self):
        user = make_user(user_id="creator-1")
        markets = {
            "m1": make_market("m1", creator_id="creator-1", total_volume=200.0, status="RESOLVED", resolution="YES"),
        }
        bets = [
            make_bet("m1", user_id="trader-1"),
            make_bet("m1", user_id="trader-2"),
            make_bet("m1", user_id="trader-3"),
        ]
        
        score = compute_creation_score(user, markets, bets)
        assert score.markets_created == 1
        assert score.total_volume_attracted == 200.0
        assert score.total_bets_attracted == 3
        assert score.score > 30.0  # Above the "no markets" floor

    def test_doesnt_count_others_markets(self):
        user = make_user(user_id="creator-1")
        markets = {
            "m1": make_market("m1", creator_id="other-creator", total_volume=500.0),
        }
        
        score = compute_creation_score(user, markets, [])
        assert score.markets_created == 0


# =============================================================================
# Participation Score Tests
# =============================================================================

class TestParticipationScore:
    def test_no_activity(self):
        user = make_user()
        score = compute_participation_score(user, [], {}, 0)
        assert score.total_bets == 0
        assert score.markets_traded_in == 0

    def test_active_trader(self):
        user = make_user(markets_created=2)
        bets = [make_bet(f"m{i}") for i in range(15)]
        markets = {f"m{i}": make_market(f"m{i}") for i in range(15)}
        
        score = compute_participation_score(user, bets, markets, comments_count=5)
        assert score.total_bets == 15
        assert score.comments_count == 5
        assert score.score > BASELINE_SCORE * 0.5


# =============================================================================
# Full Reputation Tests
# =============================================================================

class TestFullReputation:
    def test_new_agent(self):
        """Brand new agent with zero activity."""
        user = make_user()
        rep = compute_reputation(user, [], {}, [], [], 0)
        
        assert rep.agent_id == "user-1"
        assert rep.username == "testbot"
        assert rep.tier in ("New", "Bronze")
        assert 0 <= rep.overall_score <= 100

    def test_active_agent(self):
        """Agent with diverse activity."""
        user = make_user(
            user_id="active-1",
            profit_all_time=150.0,
            markets_created=2,
            total_bets=10,
        )
        markets = {
            "m1": make_market("m1", creator_id="active-1", status="RESOLVED",
                            resolution="YES", total_volume=300.0),
            "m2": make_market("m2", creator_id="active-1", status="RESOLVED",
                            resolution="NO", total_volume=150.0),
            "m3": make_market("m3", creator_id="other", status="RESOLVED",
                            resolution="YES", total_volume=200.0),
        }
        bets = [
            make_bet("m1", user_id="active-1", outcome="YES"),
            make_bet("m2", user_id="active-1", outcome="NO"),
            make_bet("m3", user_id="active-1", outcome="YES"),
        ]
        all_bets = bets + [
            make_bet("m1", user_id="other-1"),
            make_bet("m1", user_id="other-2"),
            make_bet("m2", user_id="other-1"),
        ]
        
        rep = compute_reputation(
            user=user,
            user_bets=bets,
            markets=markets,
            all_bets=all_bets,
            resolution_votes=[],
            comments_count=3,
        )
        
        assert rep.overall_score > 40  # Should at least be Bronze
        assert rep.trading.total_pnl == 150.0
        assert rep.creation.markets_created == 2
        assert rep.participation.total_bets == 3

    def test_scores_bounded(self):
        """All scores should be 0-100."""
        user = make_user(profit_all_time=99999.0)
        rep = compute_reputation(user, [], {}, [], [], 0)
        
        assert 0 <= rep.overall_score <= 100
        assert 0 <= rep.trading.score <= 100
        assert 0 <= rep.resolution.score <= 100
        assert 0 <= rep.creation.score <= 100
        assert 0 <= rep.participation.score <= 100

    def test_negative_pnl_bounded(self):
        """Even terrible PNL should give score >= 0."""
        user = make_user(profit_all_time=-99999.0)
        market = make_market("m1", status="RESOLVED", resolution="NO")
        bet = make_bet("m1", outcome="YES")
        
        rep = compute_reputation(user, [bet], {"m1": market}, [bet], [], 0)
        
        assert rep.trading.score >= 0
        assert rep.overall_score >= 0


# =============================================================================
# Run
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
