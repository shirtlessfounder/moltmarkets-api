"""
Trading endpoints — bet, sell, positions, bet history.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Response

from auth import require_auth
from cpmm import CpmmState, calculate_cpmm_purchase, calculate_cpmm_sale, get_cpmm_probability
from deps import get_db, TRADE_FEE_RATE, CREATOR_FEE_SHARE
from utils import validate_uuid, clamp_pagination
from errors import error_response, ErrorCode
from event_bus import event_bus, SSEEvent
from market_cache import market_cache
from middleware import set_rate_limit_headers, raise_rate_limited
from models import (
    BetRequest, BetResponse, FeeBreakdown,
    SellRequest, SellResponse,
    Position, MarketPositions,
    BetHistoryItem,
    MarketStatus, Outcome,
    PaginationMeta, PaginatedBetHistoryItem,
)
from rate_limiter import rate_limiter, MAX_BETS_PER_MINUTE, MAX_BET_AMOUNT

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trading"])


@router.post("/markets/{market_id}/bet", response_model=BetResponse)
async def place_bet(market_id: str, req: BetRequest, response: Response, user: dict = Depends(require_auth)):
    """Place a bet on a market.

    Rate limited: max 30 bets per agent per minute.
    Max bet amount: 500ŧ per single bet.
    """
    db = get_db()
    validate_uuid(market_id, "market_id")

    if user.get("status") != "claimed":
        return error_response(403,
            "Twitter verification required before trading. Visit /claim/{user_id} to link your Twitter account.",
            ErrorCode.CLAIM_REQUIRED)

    if req.amount > MAX_BET_AMOUNT:
        return error_response(400,
            f"Bet amount {req.amount}ŧ exceeds maximum of {MAX_BET_AMOUNT}ŧ per bet.",
            ErrorCode.INVALID_INPUT,
            detail={"amount": req.amount, "max": MAX_BET_AMOUNT})

    allowed, info = rate_limiter.check(
        f"bet:{user['id']}",
        max_requests=MAX_BETS_PER_MINUTE,
        window_seconds=60,
    )
    if not allowed:
        raise_rate_limited(
            f"Betting rate limit exceeded ({MAX_BETS_PER_MINUTE}/minute). {info['detail']}",
            info,
        )
    set_rate_limit_headers(response, info)

    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    # Markets remain tradeable until resolution (issue #115).
    # closes_at is display-only; only RESOLVING/RESOLVED blocks trading.
    if market["status"] != MarketStatus.OPEN:
        if market["status"] == MarketStatus.RESOLVING:
            status_msg = "Market is being resolved — trading is suspended"
        elif market["status"] == MarketStatus.RESOLVED:
            status_msg = "Market has been resolved"
        else:
            status_msg = "Market is not open for trading"
        return error_response(400, status_msg, ErrorCode.MARKET_CLOSED)

    trade_fee = req.amount * TRADE_FEE_RATE
    total_cost = req.amount + trade_fee

    if user["balance"] < total_cost:
        return error_response(400,
            f"Insufficient balance. Need {total_cost:.2f} (bet: {req.amount:.2f} + fee: {trade_fee:.2f})",
            ErrorCode.INSUFFICIENT_BALANCE,
            detail={"balance": user["balance"], "required": total_cost})

    prob_before = get_cpmm_probability(market["pool"], market["p"])

    state = CpmmState(pool=market["pool"].copy(), p=market["p"])
    result = calculate_cpmm_purchase(state, req.amount, req.outcome.value)

    shares = result["shares"]
    if shares <= 0:
        return error_response(400, "Trade would result in zero or negative shares", ErrorCode.ZERO_SHARES)

    prob_after = get_cpmm_probability(result["new_pool"], result["new_p"])

    db.update_user_balance(user["id"], -total_cost)

    creator_fee = trade_fee * CREATOR_FEE_SHARE
    if market["creator_id"] != user["id"]:
        db.update_user_balance(market["creator_id"], creator_fee)

    db.update_market_pool(market_id, result["new_pool"], result["new_p"], req.amount)
    db.update_position(market_id, user["id"], req.outcome, shares, req.amount)

    market_cache.invalidate()

    bet_id = str(uuid.uuid4())
    bet = db.create_bet(
        bet_id=bet_id,
        market_id=market_id,
        user_id=user["id"],
        outcome=req.outcome,
        amount=req.amount,
        shares=shares,
        prob_before=prob_before,
        prob_after=prob_after,
    )

    updated_user = db.get_user(user["id"])
    new_balance = updated_user["balance"] if updated_user else user["balance"] - total_cost

    await event_bus.publish(SSEEvent(
        event="bet_placed",
        data={
            "bet_id": bet["id"],
            "market_id": market_id,
            "user_id": user["id"],
            "username": user["username"],
            "outcome": req.outcome.value,
            "amount": req.amount,
            "shares": shares,
            "probability_before": prob_before,
            "probability_after": prob_after,
        },
        market_id=market_id,
    ))
    await event_bus.publish(SSEEvent(
        event="market_update",
        data={
            "market_id": market_id,
            "probability": prob_after,
            "total_volume": market["total_volume"] + req.amount,
            "pool": result["new_pool"],
        },
        market_id=market_id,
    ))

    return BetResponse(
        bet_id=bet["id"],
        market_id=bet["market_id"],
        market_title=market["title"],
        user_id=bet["user_id"],
        outcome=bet["outcome"],
        amount=bet["amount"],
        fee=trade_fee,
        fee_breakdown=FeeBreakdown(
            total_fee=trade_fee,
            creator_fee=creator_fee,
            platform_fee=trade_fee - creator_fee,
        ),
        total_cost=total_cost,
        new_balance=round(new_balance, 8),
        shares=bet["shares"],
        avg_price=bet["avg_price"],
        probability_before=bet["probability_before"],
        probability_after=bet["probability_after"],
        created_at=bet["created_at"],
    )


@router.post("/markets/{market_id}/bets", response_model=BetResponse)
async def place_bet_plural_alias(market_id: str, req: BetRequest, response: Response, user: dict = Depends(require_auth)):
    """Place a bet on a market (alias for POST /markets/{id}/bet)."""
    return await place_bet(market_id, req, response, user)


@router.post("/markets/{market_id}/sell", response_model=SellResponse)
async def sell_shares(market_id: str, req: SellRequest, user: dict = Depends(require_auth)):
    """Sell shares back to the market."""
    db = get_db()
    validate_uuid(market_id, "market_id")

    if user.get("status") != "claimed":
        return error_response(403,
            "Twitter verification required before trading. Visit /claim/{user_id} to link your Twitter account.",
            ErrorCode.CLAIM_REQUIRED)

    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    # Markets remain tradeable until resolution (issue #115).
    if market["status"] != MarketStatus.OPEN:
        if market["status"] == MarketStatus.RESOLVING:
            status_msg = "Market is being resolved — trading is suspended"
        elif market["status"] == MarketStatus.RESOLVED:
            status_msg = "Market has been resolved"
        else:
            status_msg = "Market is not open for trading"
        return error_response(400, status_msg, ErrorCode.MARKET_CLOSED)

    position = db.get_position(market_id, user["id"])
    if not position:
        return error_response(400, "You have no position in this market", ErrorCode.NO_POSITION)

    if req.outcome == Outcome.YES:
        available_shares = position["yes_shares"]
    else:
        available_shares = position["no_shares"]

    if available_shares < req.shares:
        return error_response(400,
            f"Insufficient shares. You have {available_shares:.2f} {req.outcome.value} shares",
            ErrorCode.INSUFFICIENT_SHARES,
            detail={"available": available_shares, "requested": req.shares})

    prob_before = get_cpmm_probability(market["pool"], market["p"])

    state = CpmmState(pool=market["pool"].copy(), p=market["p"])
    result = calculate_cpmm_sale(state, req.shares, req.outcome.value)

    amount_before_fee = result["amount"]
    if amount_before_fee <= 0:
        return error_response(400, "Sale would result in zero or negative payout", ErrorCode.ZERO_SHARES)

    trade_fee = amount_before_fee * TRADE_FEE_RATE
    amount_after_fee = amount_before_fee - trade_fee

    prob_after = get_cpmm_probability(result["new_pool"], result["new_p"])

    db.update_user_balance(user["id"], amount_after_fee)

    creator_fee = trade_fee * CREATOR_FEE_SHARE
    if market["creator_id"] != user["id"]:
        db.update_user_balance(market["creator_id"], creator_fee)

    db.update_market_pool(market_id, result["new_pool"], result["new_p"], 0)
    db.reduce_position(market_id, user["id"], req.outcome, req.shares)

    market_cache.invalidate()

    await event_bus.publish(SSEEvent(
        event="market_update",
        data={
            "market_id": market_id,
            "probability": prob_after,
            "pool": result["new_pool"],
        },
        market_id=market_id,
    ))

    return SellResponse(
        market_id=market_id,
        user_id=user["id"],
        outcome=req.outcome,
        shares_sold=req.shares,
        amount_received=amount_after_fee,
        fee_paid=trade_fee,
        probability_before=prob_before,
        probability_after=prob_after,
    )


@router.get("/markets/{market_id}/positions", response_model=MarketPositions)
async def get_positions(market_id: str):
    """Get all positions for a market."""
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    prob = get_cpmm_probability(market["pool"], market["p"])
    positions = []

    for pos in db.get_market_positions(market_id):
        current_value = pos["yes_shares"] * prob + pos["no_shares"] * (1 - prob)
        pnl = current_value - pos["total_invested"]
        positions.append(Position(
            user_id=pos["user_id"],
            market_id=pos["market_id"],
            yes_shares=pos["yes_shares"],
            no_shares=pos["no_shares"],
            total_invested=pos["total_invested"],
            current_value=current_value,
            pnl=pnl,
        ))

    return MarketPositions(market_id=market_id, positions=positions)


@router.get("/markets/{market_id}/bets", response_model=PaginatedBetHistoryItem)
async def get_market_bets(
    market_id: str,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """Get bets for a market with pagination."""
    db = get_db()
    validate_uuid(market_id, "market_id")
    limit, offset = clamp_pagination(limit, offset)

    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    market_bets = sorted(
        db.get_bets_for_market_with_users(market_id),
        key=lambda x: x["created_at"],
        reverse=True,
    )

    total = len(market_bets)
    page = market_bets[offset : offset + limit]

    items = []
    for bet in page:
        items.append(BetHistoryItem(
            bet_id=bet["id"],
            user_id=bet["user_id"],
            username=bet.get("username", "unknown"),
            outcome=bet["outcome"],
            amount=bet["amount"],
            shares=bet["shares"],
            probability_after=bet["probability_after"],
            created_at=bet["created_at"],
        ))

    return PaginatedBetHistoryItem(
        data=items,
        pagination=PaginationMeta(limit=limit, offset=offset, total=total),
    )
