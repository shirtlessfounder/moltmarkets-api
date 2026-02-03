"""
Regression tests for issue #75: snake_case field consistency.

Ensures ALL API response models serialize to snake_case and that
the _SnakeCaseBase guard prevents future camelCase drift.
"""

import re
from datetime import datetime, timezone

import pytest

from models import (
    _SnakeCaseBase,
    MarketSummary, MarketDetail, MarketCreated,
    BetResponse, FeeBreakdown, SellResponse,
    Position, MarketPositions,
    UserProfile, UserMe,
    PortfolioPosition, PortfolioSummary, PortfolioResponse,
    UserBetHistoryItem, LeaderboardEntry,
    ProbabilityPoint, MarketHistory, BetHistoryItem,
    AgentRegisteredWithClaim, AgentKeyReset,
    ClaimPageInfo, ClaimResponse,
    HumanRegistered,
    Comment, MarketComments,
    ChatMessage, ResolutionVote, ResolutionResult,
    AgentReputationResponse,
    TradingScoreResponse, ResolutionScoreResponse,
    CreationScoreResponse, ParticipationScoreResponse,
    ErrorResponse,
    MarketStatus, Outcome, AgentStatus,
)

NOW = datetime.now(timezone.utc)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def assert_all_snake_case(data: dict, model_name: str) -> None:
    """Recursively assert every key in a serialized dict is snake_case."""
    for key, value in data.items():
        assert not _CAMEL_RE.search(key), (
            f"{model_name}: field '{key}' is camelCase — "
            f"all response fields must be snake_case (see #75)"
        )
        if isinstance(value, dict):
            assert_all_snake_case(value, f"{model_name}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    assert_all_snake_case(item, f"{model_name}.{key}[{i}]")


# ---------------------------------------------------------------------------
# Fixtures — one instance per response model
# ---------------------------------------------------------------------------

RESPONSE_MODELS = {
    "MarketSummary": MarketSummary(
        id="t", title="T", probability=0.5, status=MarketStatus.OPEN,
        closes_at=NOW, total_volume=0, creator_id="c",
    ),
    "MarketDetail": MarketDetail(
        id="t", title="T", description="d", probability=0.5,
        status=MarketStatus.OPEN, closes_at=NOW, created_at=NOW,
        total_volume=0, creator_id="c", pool={"YES": 100, "NO": 100}, p=0.5,
    ),
    "MarketCreated": MarketCreated(
        id="t", title="T", description="d", probability=0.5,
        status=MarketStatus.OPEN, closes_at=NOW, created_at=NOW,
        total_volume=0, creator_id="c", pool={"YES": 100, "NO": 100}, p=0.5,
    ),
    "BetResponse": BetResponse(
        bet_id="b", market_id="m", user_id="u", outcome=Outcome.YES,
        amount=50, fee=1,
        fee_breakdown=FeeBreakdown(total_fee=1, creator_fee=0.5, platform_fee=0.5),
        total_cost=51, new_balance=949, shares=60, avg_price=0.83,
        probability_before=0.5, probability_after=0.6, created_at=NOW,
    ),
    "SellResponse": SellResponse(
        market_id="m", user_id="u", outcome=Outcome.YES,
        shares_sold=10, amount_received=9, fee_paid=0.2,
        probability_before=0.5, probability_after=0.4,
    ),
    "Position": Position(
        user_id="u", market_id="m", yes_shares=10, no_shares=0,
        total_invested=50, current_value=55, pnl=5,
    ),
    "MarketPositions": MarketPositions(market_id="m", positions=[]),
    "UserProfile": UserProfile(
        id="u", username="u", display_name="U", balance=1000,
        created_at=NOW, markets_created=0, total_bets=0, profit_all_time=0,
    ),
    "UserMe": UserMe(
        id="u", username="u", display_name="U", balance=1000,
        created_at=NOW, markets_created=0, total_bets=0, profit_all_time=0,
    ),
    "PortfolioPosition": PortfolioPosition(
        market_id="m", market_title="T", market_status=MarketStatus.OPEN,
        yes_shares=10, no_shares=0, total_invested=50, current_value=55,
        pnl=5, current_probability=0.55,
    ),
    "PortfolioSummary": PortfolioSummary(
        total_invested=50, total_current_value=55, total_pnl=5,
        open_positions=1, resolved_positions=0,
    ),
    "PortfolioResponse": PortfolioResponse(
        positions=[], summary=PortfolioSummary(
            total_invested=0, total_current_value=0, total_pnl=0,
            open_positions=0, resolved_positions=0,
        ),
    ),
    "UserBetHistoryItem": UserBetHistoryItem(
        bet_id="b", market_id="m", market_title="T",
        market_status="open", market_resolution=None,
        outcome=Outcome.YES,
        amount=50, shares=60, avg_price=0.83,
        probability_before=0.5, probability_after=0.6, created_at=NOW,
    ),
    "LeaderboardEntry": LeaderboardEntry(
        user_id="u", username="u", balance=1000, pnl=100, total_volume=500, win_rate=0.6,
    ),
    "ProbabilityPoint": ProbabilityPoint(
        timestamp=NOW, probability=0.5, volume=100,
    ),
    "MarketHistory": MarketHistory(market_id="m", points=[]),
    "BetHistoryItem": BetHistoryItem(
        bet_id="b", user_id="u", username="u", outcome=Outcome.YES,
        amount=50, shares=60, probability_after=0.6, created_at=NOW,
    ),
    "AgentRegisteredWithClaim": AgentRegisteredWithClaim(
        user_id="u", username="u", display_name="U", api_key="mm_x",
        balance=1000, created_at=NOW, status=AgentStatus.PENDING,
        verification_code="abc", claim_url="/c",
    ),
    "AgentKeyReset": AgentKeyReset(user_id="u", api_key="mm_x"),
    "ClaimPageInfo": ClaimPageInfo(
        user_id="u", username="u", display_name="U",
        verification_code="abc", instructions="i",
    ),
    "ClaimResponse": ClaimResponse(
        success=True, message="ok", user_id="u", username="u",
        display_name="U", status=AgentStatus.CLAIMED,
    ),
    "HumanRegistered": HumanRegistered(
        user_id="u", username="u", display_name="U", api_key="mm_x",
        balance=1000, user_type="human", created_at=NOW,
    ),
    "Comment": Comment(
        id="c", market_id="m", user_id="u", username="u",
        content="text", created_at=NOW,
    ),
    "MarketComments": MarketComments(market_id="m", comments=[], total=0),
    "ChatMessage": ChatMessage(id="c", username="u", text="hi", created_at=NOW),
    "ResolutionVote": ResolutionVote(
        agent_id="a", vote=Outcome.YES, reasoning="r", created_at=NOW,
    ),
    "ResolutionResult": ResolutionResult(
        market_id="m", status="resolved", votes_yes=5, votes_no=4,
        total_votes=9, votes=[],
    ),
    "AgentReputationResponse": AgentReputationResponse(
        agent_id="a", username="u", overall_score=50, tier="Silver",
        trading=TradingScoreResponse(
            score=50, total_pnl=100, resolved_bets=5,
            win_rate=0.6, total_volume=500,
        ),
        resolution=ResolutionScoreResponse(
            score=50, total_votes=3, correct_votes=2, accuracy=0.66,
        ),
        creation=CreationScoreResponse(
            score=50, markets_created=2, total_volume_attracted=200,
            total_bets_attracted=10, avg_volume_per_market=100,
            avg_bets_per_market=5, resolved_cleanly=1, disputed=0,
        ),
        participation=ParticipationScoreResponse(
            score=50, total_bets=10, markets_traded_in=3,
            markets_created=2, comments_count=5,
        ),
    ),
    "ErrorResponse": ErrorResponse(error="not found"),
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSnakeCaseConsistency:
    """Every response model must serialize with snake_case keys only."""

    @pytest.mark.parametrize("model_name,instance", list(RESPONSE_MODELS.items()))
    def test_response_model_snake_case(self, model_name, instance):
        data = instance.model_dump(mode="json")
        assert_all_snake_case(data, model_name)

    def test_base_class_has_alias_generator(self):
        """The base model must have alias_generator configured."""
        cfg = _SnakeCaseBase.model_config
        assert cfg.get("alias_generator") is not None, (
            "_SnakeCaseBase must define alias_generator for backward compat"
        )
        assert cfg.get("populate_by_name") is True, (
            "_SnakeCaseBase must set populate_by_name=True"
        )

    def test_all_response_models_inherit_base(self):
        """Every response model should inherit from _SnakeCaseBase."""
        for model_name, instance in RESPONSE_MODELS.items():
            assert isinstance(instance, _SnakeCaseBase), (
                f"{model_name} does not inherit from _SnakeCaseBase — "
                f"all models must use the snake_case base class"
            )
