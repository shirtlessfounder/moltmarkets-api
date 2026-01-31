"""
Payout calculation and distribution for resolved markets.

Extracted from deps.py — see issue #128.
"""

import logging

from deps import get_db

logger = logging.getLogger(__name__)


def calculate_and_distribute_payouts(market_id: str, outcome) -> int:
    """Calculate and distribute payouts for a resolved market.

    Steps:
    1. Pay winning shares to each position holder.
    2. Credit the winning-side pool residual (initial liquidity) to the
       market creator — this is the AMM liquidity that would otherwise
       stay locked in the pool forever.
    3. Zero out the pool so the probability reads 1.0 or 0.0 and no
       tokens remain stuck.
    4. Zero out all positions for this market (prevents double-counting
       in portfolio views).

    Idempotent: if the winning pool is already ≤ 0.001, steps 2-4 are
    skipped (market was already cleaned up).
    """
    from models import Outcome
    db = get_db()
    market = db.get_market(market_id)
    positions = db.get_market_positions(market_id)

    # Step 1: Pay out winning shares to position holders
    paid = 0
    for pos in positions:
        winning_shares = pos["yes_shares"] if outcome == Outcome.YES else pos["no_shares"]
        if winning_shares > 0:
            payout = winning_shares
            db.update_user_balance(pos["user_id"], payout)
            db.update_user_profit(pos["user_id"], payout - pos["total_invested"])
            paid += 1

    # Step 2: Credit pool residual to creator
    if market:
        pool = market["pool"]
        winning_pool = pool["YES"] if outcome == Outcome.YES else pool["NO"]

        if winning_pool > 0.001:
            db.update_user_balance(market["creator_id"], winning_pool)
            logger.info(
                "Pool residual %.4f credited to creator %s for market %s",
                winning_pool, market["creator_id"], market_id,
            )

        # Step 3: Set pool to terminal state (probability = 1.0 or 0.0)
        #   YES resolution → pool_yes=0, pool_no=1 → P = 1.0
        #   NO  resolution → pool_yes=1, pool_no=0 → P = 0.0
        if outcome == Outcome.YES:
            terminal_pool = {"YES": 0.0, "NO": 1.0}
        else:
            terminal_pool = {"YES": 1.0, "NO": 0.0}
        db.update_market_pool(market_id, terminal_pool, market["p"], 0)

    # Step 4: Zero out positions (resolved — no longer relevant)
    for pos in positions:
        if pos["yes_shares"] > 0:
            db.reduce_position(market_id, pos["user_id"], Outcome.YES, pos["yes_shares"])
        if pos["no_shares"] > 0:
            db.reduce_position(market_id, pos["user_id"], Outcome.NO, pos["no_shares"])

    return paid
