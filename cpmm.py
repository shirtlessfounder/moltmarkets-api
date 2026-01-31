"""
CPMM (Constant Product Market Maker) Implementation

Ported from Manifold Markets: https://github.com/manifoldmarkets/manifold
Reference: common/src/calculate-cpmm.ts

The CPMM uses the invariant formula:
    y^p * n^(1-p) = k

Where:
    y = YES shares in the liquidity pool
    n = NO shares in the liquidity pool  
    p = pool weight parameter (typically 0.5 for balanced markets)
    k = constant invariant that must be maintained

This is a generalization of the classic x*y=k AMM formula (Uniswap v2),
allowing for asymmetric initial probabilities via the p parameter.
"""

from dataclasses import dataclass, field
from typing import Dict, Literal, TypedDict

# Type aliases
Outcome = Literal["YES", "NO"]
Pool = Dict[str, float]


@dataclass
class Fees:
    """Fee breakdown for a trade."""
    creator_fee: float = 0.0
    platform_fee: float = 0.0
    liquidity_fee: float = 0.0
    
    @property
    def total(self) -> float:
        return self.creator_fee + self.platform_fee + self.liquidity_fee


@dataclass
class CpmmState:
    """
    Complete state of a CPMM market.
    
    Attributes:
        pool: Dict with 'YES' and 'NO' share counts
        p: Pool weight parameter (0 < p < 1), affects initial probability
        collected_fees: Accumulated fees from trading
    """
    pool: Pool
    p: float
    collected_fees: Fees = field(default_factory=Fees)


class PurchaseResult(TypedDict):
    """Result of a CPMM purchase calculation."""
    shares: float
    new_pool: Pool
    new_p: float
    fees: Fees


# -----------------------------------------------------------------------------
# Fee Calculations
# -----------------------------------------------------------------------------

# Manifold currently has fees disabled (TAKER_FEE_CONSTANT = 0)
# Keeping the structure for future flexibility
TAKER_FEE_CONSTANT = 0.0


def get_taker_fee(shares: float, prob: float) -> float:
    """
    Calculate the taker fee for a trade.
    
    The fee is proportional to: prob * (1 - prob) * shares
    This means fees are highest at 50% probability and zero at extremes.
    
    Args:
        shares: Number of shares being traded
        prob: Average probability during the trade
        
    Returns:
        Fee amount in points (ŧ)
    """
    return TAKER_FEE_CONSTANT * prob * (1 - prob) * shares


def get_fees_split(total_fees: float) -> Fees:
    """
    Split total fees among creator, platform, and liquidity providers.
    
    Currently all fees go to platform (when enabled).
    """
    return Fees(
        creator_fee=0.0,
        platform_fee=total_fees,
        liquidity_fee=0.0,
    )


NO_FEES = Fees()


# -----------------------------------------------------------------------------
# Core CPMM Functions
# -----------------------------------------------------------------------------

def get_cpmm_probability(pool: Pool, p: float) -> float:
    """
    Calculate the current YES probability from pool state.
    
    Formula derivation:
    - The CPMM invariant is: y^p * n^(1-p) = k
    - Probability is the marginal price, derived from partial derivatives
    - P(YES) = (p * n) / ((1-p) * y + p * n)
    
    When p = 0.5, this simplifies to: n / (y + n)
    
    Args:
        pool: Dict with 'YES' and 'NO' share counts
        p: Pool weight parameter
        
    Returns:
        Probability of YES outcome (0 to 1)
        
    Example:
        >>> get_cpmm_probability({'YES': 100, 'NO': 100}, 0.5)
        0.5
        >>> get_cpmm_probability({'YES': 50, 'NO': 150}, 0.5)
        0.75
    """
    y = pool["YES"]
    n = pool["NO"]
    return (p * n) / ((1 - p) * y + p * n)


def get_cpmm_liquidity(pool: Pool, p: float) -> float:
    """
    Calculate the liquidity constant k for the pool.
    
    Formula: k = y^p * n^(1-p)
    
    Higher k means more liquidity and less price impact per trade.
    """
    y = pool["YES"]
    n = pool["NO"]
    return (y ** p) * (n ** (1 - p))


def calculate_cpmm_shares(
    pool: Pool,
    p: float,
    bet_amount: float,
    outcome: Outcome
) -> float:
    """
    Calculate shares received for a bet (before fees).
    
    When you bet amount `b` on YES:
    - Both y and n increase by b (your money enters the pool)
    - You receive s YES shares, so y decreases by s
    - New pool: [y + b - s, n + b]
    - Must maintain: (y + b - s)^p * (n + b)^(1-p) = k
    
    Solving for s (via Wolfram Alpha):
        s = y + b - (k * (b + n)^(p-1))^(1/p)
    
    For NO bets, the formula is symmetric:
        s = n + b - (k * (b + y)^(-p))^(1/(1-p))
    
    Args:
        pool: Current pool state
        p: Pool weight parameter
        bet_amount: Amount being bet (in points, ŧ)
        outcome: 'YES' or 'NO'
        
    Returns:
        Number of shares received
        
    Example:
        >>> pool = {'YES': 100, 'NO': 100}
        >>> calculate_cpmm_shares(pool, 0.5, 10, 'YES')
        18.18...  # You get ~18 shares for betting 10
    """
    if bet_amount == 0:
        return 0.0
    
    y = pool["YES"]
    n = pool["NO"]
    k = (y ** p) * (n ** (1 - p))
    
    if outcome == "YES":
        # s = y + b - (k * (b + n)^(p-1))^(1/p)
        return y + bet_amount - (k * (bet_amount + n) ** (p - 1)) ** (1 / p)
    else:
        # s = n + b - (k * (b + y)^(-p))^(1/(1-p))
        return n + bet_amount - (k * (bet_amount + y) ** (-p)) ** (1 / (1 - p))


def calculate_cpmm_sale_amount(
    pool: Pool,
    p: float,
    shares: float,
    outcome: Outcome
) -> float:
    """
    Calculate money received when selling shares back to the pool.
    
    When you sell s YES shares:
    - Your s shares go back into the pool: y increases by s
    - You withdraw money: both y and n decrease by the payout amount
    - New pool: [y + s - a, n - a]
    - Must maintain: (y + s - a)^p * (n - a)^(1-p) = k
    
    Solving for a (the payout):
    Using binary search to find the amount that maintains the invariant.
    
    Args:
        pool: Current pool state
        p: Pool weight parameter
        shares: Number of shares to sell
        outcome: 'YES' or 'NO'
        
    Returns:
        Amount received for selling shares
    """
    if shares <= 0:
        return 0.0
    
    y = pool["YES"]
    n = pool["NO"]
    k = (y ** p) * (n ** (1 - p))
    
    # Binary search for the payout amount
    # Maximum possible payout is the smaller of the two pool sides
    low = 0.0
    high = min(y, n) * 0.99  # Can't drain the pool completely
    
    for _ in range(100):  # Binary search iterations
        mid = (low + high) / 2
        
        if outcome == "YES":
            new_y = y + shares - mid
            new_n = n - mid
        else:
            new_y = y - mid
            new_n = n + shares - mid
        
        if new_y <= 0 or new_n <= 0:
            high = mid
            continue
            
        new_k = (new_y ** p) * (new_n ** (1 - p))
        
        if abs(new_k - k) < 0.0001:
            return mid
        elif new_k > k:
            low = mid
        else:
            high = mid
    
    return mid


class SaleResult(TypedDict):
    """Result of a CPMM sale calculation."""
    amount: float
    new_pool: Pool
    new_p: float


def calculate_cpmm_sale(
    state: CpmmState,
    shares: float,
    outcome: Outcome
) -> SaleResult:
    """
    Calculate the full result of selling shares back to the pool.
    
    Args:
        state: Current CPMM state
        shares: Number of shares to sell
        outcome: 'YES' or 'NO'
        
    Returns:
        SaleResult with amount received, new_pool, and new_p
    """
    pool = state.pool
    p = state.p
    
    amount = calculate_cpmm_sale_amount(pool, p, shares, outcome)
    
    y = pool["YES"]
    n = pool["NO"]
    
    if outcome == "YES":
        new_y = y + shares - amount
        new_n = n - amount
    else:
        new_y = y - amount
        new_n = n + shares - amount
    
    new_pool = {"YES": new_y, "NO": new_n}
    
    return {
        "amount": amount,
        "new_pool": new_pool,
        "new_p": p,  # p doesn't change on sale
    }


def get_cpmm_fees(
    state: CpmmState,
    bet_amount: float,
    outcome: Outcome
) -> tuple[float, float, Fees]:
    """
    Calculate fees for a bet using iterative approximation.
    
    The fee depends on the average probability during the trade, but the
    average probability depends on the bet size after fees. We iterate
    to find a consistent solution.
    
    Args:
        state: Current CPMM state
        bet_amount: Total bet amount (before fees)
        outcome: 'YES' or 'NO'
        
    Returns:
        Tuple of (remaining_bet, total_fees, fees_breakdown)
    """
    if bet_amount == 0:
        return bet_amount, 0.0, NO_FEES
    
    # Iterate to find consistent fee
    fee = 0.0
    for _ in range(10):
        bet_after_fee = bet_amount - fee
        shares = calculate_cpmm_shares(state.pool, state.p, bet_after_fee, outcome)
        if shares <= 0:
            break
        avg_prob = bet_after_fee / shares
        fee = get_taker_fee(shares, avg_prob)
    
    total_fees = fee
    fees = get_fees_split(total_fees)
    remaining_bet = bet_amount - total_fees
    
    return remaining_bet, total_fees, fees


def add_cpmm_liquidity(
    pool: Pool,
    p: float,
    amount: float
) -> tuple[Pool, float, float]:
    """
    Add liquidity to the pool while maintaining current probability.
    
    When adding liquidity, we need to adjust p to keep the probability
    constant. The formula is derived from:
        P(YES) = prob = p*n / ((1-p)*y + p*n)
    
    Solving for new_p when both y and n increase by `amount`:
        new_p = (prob * (amount + y)) / (amount - n*(prob-1) + prob*y)
    
    Args:
        pool: Current pool state
        p: Current pool weight
        amount: Liquidity to add
        
    Returns:
        Tuple of (new_pool, liquidity_added, new_p)
    """
    prob = get_cpmm_probability(pool, p)
    
    y = pool["YES"]
    n = pool["NO"]
    
    # Calculate new p to maintain probability
    numerator = prob * (amount + y)
    denominator = amount - n * (prob - 1) + prob * y
    new_p = numerator / denominator
    
    new_pool = {"YES": y + amount, "NO": n + amount}
    
    old_liquidity = get_cpmm_liquidity(pool, new_p)
    new_liquidity = get_cpmm_liquidity(new_pool, new_p)
    liquidity = new_liquidity - old_liquidity
    
    return new_pool, liquidity, new_p


def calculate_cpmm_purchase(
    state: CpmmState,
    bet: float,
    outcome: Outcome,
    free_fees: bool = False
) -> PurchaseResult:
    """
    Calculate the full result of a CPMM purchase including fees.
    
    This is the main entry point for executing a bet. It:
    1. Calculates fees and the remaining bet amount
    2. Calculates shares received
    3. Updates the pool state
    4. Adds liquidity fee back to pool (if any)
    
    Pool update mechanics:
    - Your bet amount enters both sides of the pool
    - You withdraw your shares from your chosen side
    - Liquidity fees stay in the pool
    
    Args:
        state: Current CPMM state
        bet: Bet amount in points (ŧ)
        outcome: 'YES' or 'NO'
        free_fees: If True, skip fee calculation
        
    Returns:
        PurchaseResult with shares, new_pool, new_p, and fees
        
    Example:
        >>> state = CpmmState(pool={'YES': 100, 'NO': 100}, p=0.5)
        >>> result = calculate_cpmm_purchase(state, 10, 'YES')
        >>> print(f"Shares: {result['shares']:.2f}")
        Shares: 18.18
    """
    pool = state.pool
    p = state.p
    
    if free_fees:
        remaining_bet = bet
        fees = NO_FEES
    else:
        remaining_bet, _, fees = get_cpmm_fees(state, bet, outcome)
    
    shares = calculate_cpmm_shares(pool, p, remaining_bet, outcome)
    
    y = pool["YES"]
    n = pool["NO"]
    liquidity_fee = fees.liquidity_fee
    
    # Update pool based on outcome
    if outcome == "YES":
        new_y = y - shares + remaining_bet + liquidity_fee
        new_n = n + remaining_bet + liquidity_fee
    else:
        new_y = y + remaining_bet + liquidity_fee
        new_n = n - shares + remaining_bet + liquidity_fee
    
    post_bet_pool = {"YES": new_y, "NO": new_n}
    
    # Add liquidity fee back to pool
    new_pool, _, new_p = add_cpmm_liquidity(post_bet_pool, p, liquidity_fee)
    
    return {
        "shares": shares,
        "new_pool": new_pool,
        "new_p": new_p,
        "fees": fees,
    }


def get_cpmm_probability_after_bet(
    state: CpmmState,
    outcome: Outcome,
    bet: float
) -> float:
    """
    Calculate the probability after a hypothetical bet.
    
    Useful for showing users the price impact of their trade.
    """
    result = calculate_cpmm_purchase(state, bet, outcome)
    return get_cpmm_probability(result["new_pool"], result["new_p"])


def calculate_cpmm_amount_to_prob(
    state: CpmmState,
    prob: float,
    outcome: Outcome
) -> float:
    """
    Calculate how much to bet to move the market to a target probability.
    
    This is the inverse of calculating probability after a bet.
    Derived by solving the CPMM equations for bet amount given target prob.
    
    Args:
        state: Current market state
        prob: Target probability (0 < prob < 1)
        outcome: Direction of bet ('YES' or 'NO')
        
    Returns:
        Bet amount needed (before fees)
    """
    if prob <= 0 or prob >= 1 or prob != prob:  # NaN check
        return float('inf')
    
    if outcome == "NO":
        prob = 1 - prob
    
    pool = state.pool
    p = state.p
    y = pool["YES"]
    n = pool["NO"]
    k = (y ** p) * (n ** (1 - p))
    
    if outcome == "YES":
        # Derived from Wolfram Alpha
        ratio = (p * (prob - 1)) / ((p - 1) * prob)
        return (ratio ** (-p)) * (k - n * (ratio ** p))
    else:
        ratio = ((1 - p) * (prob - 1)) / ((-p) * prob)
        return (ratio ** (p - 1)) * (k - y * (ratio ** (1 - p)))


# -----------------------------------------------------------------------------
# Test Cases
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("CPMM Test Cases")
    print("=" * 60)
    
    # Test 1: Basic probability calculation
    print("\n1. Probability Calculation")
    print("-" * 40)
    
    pool = {"YES": 100.0, "NO": 100.0}
    prob = get_cpmm_probability(pool, 0.5)
    print(f"   Pool: {pool}, p=0.5")
    print(f"   Probability: {prob:.4f} (expected: 0.5)")
    assert abs(prob - 0.5) < 0.0001, "50/50 pool should give 50% prob"
    
    pool2 = {"YES": 50.0, "NO": 150.0}
    prob2 = get_cpmm_probability(pool2, 0.5)
    print(f"   Pool: {pool2}, p=0.5")
    print(f"   Probability: {prob2:.4f} (expected: 0.75)")
    assert abs(prob2 - 0.75) < 0.0001, "50/150 pool should give 75% prob"
    
    # Test 2: Share calculation
    print("\n2. Share Calculation (before fees)")
    print("-" * 40)
    
    pool = {"YES": 100.0, "NO": 100.0}
    shares = calculate_cpmm_shares(pool, 0.5, 10, "YES")
    print(f"   Betting 10 on YES in {pool}")
    print(f"   Shares received: {shares:.4f}")
    # Verify: shares should be > bet amount (leverage from AMM)
    assert shares > 10, "Should receive more shares than bet amount"
    print(f"   Effective price: {10/shares:.4f} per share")
    
    # Verify invariant is maintained
    k_before = 100 ** 0.5 * 100 ** 0.5
    new_y = 100 + 10 - shares
    new_n = 100 + 10
    k_after = new_y ** 0.5 * new_n ** 0.5
    print(f"   k before: {k_before:.4f}, k after: {k_after:.4f}")
    assert abs(k_before - k_after) < 0.0001, "Invariant should be maintained"
    
    # Test 3: Full purchase calculation
    print("\n3. Full Purchase Calculation")
    print("-" * 40)
    
    state = CpmmState(pool={"YES": 100.0, "NO": 100.0}, p=0.5)
    result = calculate_cpmm_purchase(state, 10, "YES")
    
    print(f"   Initial state: pool={state.pool}, p={state.p}")
    print("   Bet: 10 on YES")
    print(f"   Shares: {result['shares']:.4f}")
    print(f"   New pool: {result['new_pool']}")
    print(f"   New p: {result['new_p']:.4f}")
    print(f"   Fees: {result['fees'].total:.4f}")
    
    new_prob = get_cpmm_probability(result["new_pool"], result["new_p"])
    print(f"   New probability: {new_prob:.4f}")
    assert new_prob > 0.5, "YES bet should increase probability"
    
    # Test 4: Probability after bet
    print("\n4. Probability Movement")
    print("-" * 40)
    
    state = CpmmState(pool={"YES": 100.0, "NO": 100.0}, p=0.5)
    
    for bet_size in [1, 10, 50, 100]:
        prob_after = get_cpmm_probability_after_bet(state, "YES", bet_size)
        print(f"   Bet {bet_size:3d} on YES: prob moves from 50% to {prob_after*100:.2f}%")
    
    # Test 5: NO bet direction
    print("\n5. NO Bet Direction")
    print("-" * 40)
    
    state = CpmmState(pool={"YES": 100.0, "NO": 100.0}, p=0.5)
    result = calculate_cpmm_purchase(state, 20, "NO")
    
    print("   Bet: 20 on NO")
    print(f"   Shares: {result['shares']:.4f}")
    new_prob = get_cpmm_probability(result["new_pool"], result["new_p"])
    print(f"   New probability: {new_prob:.4f}")
    assert new_prob < 0.5, "NO bet should decrease probability"
    
    # Test 6: Non-0.5 p parameter
    print("\n6. Asymmetric p Parameter")
    print("-" * 40)
    
    # p=0.7 weights the pool toward YES
    pool = {"YES": 100.0, "NO": 100.0}
    prob_p5 = get_cpmm_probability(pool, 0.5)
    prob_p7 = get_cpmm_probability(pool, 0.7)
    prob_p3 = get_cpmm_probability(pool, 0.3)
    
    print(f"   Same pool {pool}, different p:")
    print(f"   p=0.3: prob={prob_p3:.4f}")
    print(f"   p=0.5: prob={prob_p5:.4f}")
    print(f"   p=0.7: prob={prob_p7:.4f}")
    
    # Test 7: Edge cases
    print("\n7. Edge Cases")
    print("-" * 40)
    
    # Zero bet
    shares = calculate_cpmm_shares({"YES": 100, "NO": 100}, 0.5, 0, "YES")
    print(f"   Zero bet: shares={shares} (expected: 0)")
    assert shares == 0, "Zero bet should give zero shares"
    
    # Very small bet
    state = CpmmState(pool={"YES": 100.0, "NO": 100.0}, p=0.5)
    result = calculate_cpmm_purchase(state, 0.001, "YES")
    print(f"   Tiny bet (0.001): shares={result['shares']:.6f}")
    assert result["shares"] > 0, "Should get positive shares"
    
    # Test 8: Amount to probability
    print("\n8. Calculate Amount to Target Probability")
    print("-" * 40)
    
    state = CpmmState(pool={"YES": 100.0, "NO": 100.0}, p=0.5)
    target_prob = 0.6
    amount = calculate_cpmm_amount_to_prob(state, target_prob, "YES")
    print(f"   Target: {target_prob*100}% YES")
    print(f"   Amount needed: {amount:.4f}")
    
    # Verify by making the bet
    result = calculate_cpmm_purchase(state, amount, "YES", free_fees=True)
    actual_prob = get_cpmm_probability(result["new_pool"], result["new_p"])
    print(f"   Actual probability after bet: {actual_prob:.4f}")
    assert abs(actual_prob - target_prob) < 0.01, "Should hit target probability"
    
    print("\n" + "=" * 60)
    print("All tests passed! ✓")
    print("=" * 60)
