"""
MoltMarkets API — FastAPI application.

Binary prediction markets with CPMM market maker.
Uses JSON file persistence (upgrade to Postgres later).
"""

import os
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware

from cpmm import CpmmState, calculate_cpmm_purchase, get_cpmm_probability, Outcome as CpmmOutcome
import secrets
import hashlib

from models import (
    MarketCreate, MarketResolve, MarketSummary, MarketDetail,
    BetRequest, BetResponse, Position, MarketPositions,
    UserProfile, UserMe, ErrorResponse, LeaderboardEntry,
    ProbabilityPoint, MarketHistory, BetHistoryItem,
    AgentRegister, AgentRegistered, AgentKeyReset,
    MarketStatus, Outcome,
)


def generate_api_key() -> str:
    """Generate a secure API key."""
    return f"mm_{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()

# Persistence file path (use env var for Railway volume or default to local)
_data_path = os.getenv("DATA_PATH", "/data/moltmarkets.json")
# Fallback to current directory if /data doesn't exist (local dev)
if not os.path.exists("/data"):
    _data_path = "./moltmarkets.json"
DATA_FILE = Path(_data_path)


# =============================================================================
# In-Memory Storage (swap for DB later)
# =============================================================================

class Storage:
    """
    In-memory storage with JSON file persistence.
    
    All methods are sync for now — make async when adding real DB.
    """
    
    def __init__(self):
        self.markets: Dict[str, dict] = {}
        self.users: Dict[str, dict] = {}
        self.bets: Dict[str, dict] = {}
        self.positions: Dict[str, Dict[str, dict]] = {}  # market_id -> user_id -> position
        self._load()
    
    def _serialize_datetime(self, obj):
        """Convert datetime objects to ISO strings for JSON."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, dict):
            return {k: self._serialize_datetime(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._serialize_datetime(i) for i in obj]
        elif isinstance(obj, MarketStatus):
            return obj.value
        elif isinstance(obj, Outcome):
            return obj.value
        return obj
    
    def _deserialize_datetime(self, obj, datetime_keys=None):
        """Convert ISO strings back to datetime objects."""
        datetime_keys = datetime_keys or {"created_at", "closes_at", "resolved_at"}
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if k in datetime_keys and isinstance(v, str):
                    try:
                        result[k] = datetime.fromisoformat(v)
                    except:
                        result[k] = v
                elif k == "status" and isinstance(v, str):
                    result[k] = MarketStatus(v)
                elif k == "resolution" and isinstance(v, str):
                    result[k] = Outcome(v)
                elif k == "outcome" and isinstance(v, str):
                    result[k] = Outcome(v)
                else:
                    result[k] = self._deserialize_datetime(v, datetime_keys)
            return result
        elif isinstance(obj, list):
            return [self._deserialize_datetime(i, datetime_keys) for i in obj]
        return obj
    
    def _save(self):
        """Save state to JSON file."""
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "markets": self._serialize_datetime(self.markets),
                "users": self._serialize_datetime(self.users),
                "bets": self._serialize_datetime(self.bets),
                "positions": self._serialize_datetime(self.positions),
            }
            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Warning: Failed to save data: {e}")
    
    def _load(self):
        """Load state from JSON file."""
        try:
            if DATA_FILE.exists():
                with open(DATA_FILE, "r") as f:
                    data = json.load(f)
                self.markets = self._deserialize_datetime(data.get("markets", {}))
                self.users = self._deserialize_datetime(data.get("users", {}))
                self.bets = self._deserialize_datetime(data.get("bets", {}))
                self.positions = self._deserialize_datetime(data.get("positions", {}))
                print(f"Loaded {len(self.markets)} markets, {len(self.users)} users from {DATA_FILE}")
        except Exception as e:
            print(f"Warning: Failed to load data: {e}")
    
    # --- Users ---
    
    def get_user(self, user_id: str) -> Optional[dict]:
        return self.users.get(user_id)
    
    def create_user(self, user_id: str, username: str, balance: float = 1000.0, 
                    api_key_hash: str = None, description: str = "") -> dict:
        user = {
            "id": user_id,
            "username": username,
            "display_name": username,
            "description": description,
            "balance": balance,
            "created_at": datetime.now(timezone.utc),
            "markets_created": 0,
            "total_bets": 0,
            "profit_all_time": 0.0,
            "api_key_hash": api_key_hash,
        }
        self.users[user_id] = user
        self._save()
        return user
    
    def get_user_by_api_key(self, api_key: str) -> Optional[dict]:
        """Find user by API key."""
        key_hash = hash_api_key(api_key)
        for user in self.users.values():
            if user.get("api_key_hash") == key_hash:
                return user
        return None
    
    def get_user_by_username(self, username: str) -> Optional[dict]:
        """Find user by username."""
        for user in self.users.values():
            if user.get("username") == username:
                return user
        return None
    
    def update_api_key(self, user_id: str, new_key_hash: str):
        """Update user's API key hash."""
        self.users[user_id]["api_key_hash"] = new_key_hash
        self._save()
    
    def update_user_balance(self, user_id: str, delta: float) -> float:
        self.users[user_id]["balance"] += delta
        self._save()
        return self.users[user_id]["balance"]
    
    # --- Markets ---
    
    def get_market(self, market_id: str) -> Optional[dict]:
        return self.markets.get(market_id)
    
    def list_markets(self) -> List[dict]:
        return list(self.markets.values())
    
    def create_market(self, market_id: str, creator_id: str, title: str,
                      description: str, closes_at: datetime, 
                      initial_liquidity: float) -> dict:
        # Initialize balanced CPMM pool
        pool = {"YES": initial_liquidity, "NO": initial_liquidity}
        
        market = {
            "id": market_id,
            "title": title,
            "description": description,
            "status": MarketStatus.OPEN,
            "closes_at": closes_at,
            "created_at": datetime.now(timezone.utc),
            "resolved_at": None,
            "resolution": None,
            "total_volume": 0.0,
            "creator_id": creator_id,
            "pool": pool,
            "p": 0.5,  # Start at 50%
        }
        self.markets[market_id] = market
        self.positions[market_id] = {}
        self.users[creator_id]["markets_created"] += 1
        self._save()
        return market
    
    def update_market_pool(self, market_id: str, new_pool: dict, new_p: float, volume_delta: float):
        market = self.markets[market_id]
        market["pool"] = new_pool
        market["p"] = new_p
        market["total_volume"] += volume_delta
        self._save()
    
    def resolve_market(self, market_id: str, outcome: Outcome):
        market = self.markets[market_id]
        market["status"] = MarketStatus.RESOLVED
        market["resolution"] = outcome
        market["resolved_at"] = datetime.now(timezone.utc)
        self._save()
    
    # --- Bets ---
    
    def create_bet(self, bet_id: str, market_id: str, user_id: str,
                   outcome: Outcome, amount: float, shares: float,
                   prob_before: float, prob_after: float) -> dict:
        bet = {
            "id": bet_id,
            "market_id": market_id,
            "user_id": user_id,
            "outcome": outcome,
            "amount": amount,
            "shares": shares,
            "avg_price": amount / shares if shares > 0 else 0,
            "probability_before": prob_before,
            "probability_after": prob_after,
            "created_at": datetime.now(timezone.utc),
        }
        self.bets[bet_id] = bet
        self.users[user_id]["total_bets"] += 1
        self._save()
        return bet
    
    # --- Positions ---
    
    def get_position(self, market_id: str, user_id: str) -> Optional[dict]:
        return self.positions.get(market_id, {}).get(user_id)
    
    def get_market_positions(self, market_id: str) -> List[dict]:
        return list(self.positions.get(market_id, {}).values())
    
    def update_position(self, market_id: str, user_id: str, 
                        outcome: Outcome, shares_delta: float, invested_delta: float):
        if market_id not in self.positions:
            self.positions[market_id] = {}
        
        if user_id not in self.positions[market_id]:
            self.positions[market_id][user_id] = {
                "user_id": user_id,
                "market_id": market_id,
                "yes_shares": 0.0,
                "no_shares": 0.0,
                "total_invested": 0.0,
            }
        
        pos = self.positions[market_id][user_id]
        if outcome == Outcome.YES:
            pos["yes_shares"] += shares_delta
        else:
            pos["no_shares"] += shares_delta
        pos["total_invested"] += invested_delta
        self._save()


# Global storage instance
db = Storage()


# =============================================================================
# Auth (placeholder — swap for real auth later)
# =============================================================================

async def get_current_user(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),  # Legacy support
) -> dict:
    """
    Authenticate via API key.
    
    Accepts:
    - Authorization: Bearer mm_xxx
    - X-API-Key: mm_xxx
    - X-User-ID: user-id (legacy, for backwards compat)
    """
    api_key = None
    
    # Try Authorization header first
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    # Then X-API-Key header
    elif x_api_key:
        api_key = x_api_key
    
    # If we have an API key, authenticate with it
    if api_key:
        user = db.get_user_by_api_key(api_key)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return user
    
    # Legacy: X-User-ID header (for backwards compat during transition)
    if x_user_id:
        user = db.get_user(x_user_id)
        if not user:
            # Auto-create for demo purposes (remove in prod)
            user = db.create_user(x_user_id, f"user_{x_user_id[:8]}")
        return user
    
    # No auth provided — use demo user for now
    user = db.get_user("demo-user")
    if not user:
        user = db.create_user("demo-user", "demo_user", balance=10000.0)
    return user


# =============================================================================
# App Setup
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: seed demo data if empty
    if not db.users:
        db.create_user("demo-user", "demo_user", balance=10000.0)
    print(f"MoltMarkets API started with {len(db.markets)} markets, {len(db.users)} users")
    yield
    # Shutdown: ensure final save
    db._save()
    print("MoltMarkets API shutting down")


app = FastAPI(
    title="MoltMarkets API",
    description="Binary prediction markets with CPMM market maker",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Market Endpoints
# =============================================================================

@app.get("/markets", response_model=List[MarketSummary])
async def list_markets():
    """List all markets."""
    markets = db.list_markets()
    return [
        MarketSummary(
            id=m["id"],
            title=m["title"],
            probability=get_cpmm_probability(m["pool"], m["p"]),
            status=m["status"],
            closes_at=m["closes_at"],
            total_volume=m["total_volume"],
            creator_id=m["creator_id"],
        )
        for m in markets
    ]


@app.get("/markets/{market_id}", response_model=MarketDetail)
async def get_market(market_id: str):
    """Get market details including current probability."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    return MarketDetail(
        id=market["id"],
        title=market["title"],
        description=market["description"],
        probability=get_cpmm_probability(market["pool"], market["p"]),
        status=market["status"],
        closes_at=market["closes_at"],
        created_at=market["created_at"],
        resolved_at=market["resolved_at"],
        resolution=market["resolution"],
        total_volume=market["total_volume"],
        creator_id=market["creator_id"],
        pool=market["pool"],
        p=market["p"],
    )


@app.post("/markets", response_model=MarketDetail)
async def create_market(req: MarketCreate, user: dict = Depends(get_current_user)):
    """Create a new prediction market."""
    if req.closes_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="closes_at must be in the future")
    
    market_id = str(uuid.uuid4())
    market = db.create_market(
        market_id=market_id,
        creator_id=user["id"],
        title=req.title,
        description=req.description,
        closes_at=req.closes_at,
        initial_liquidity=req.initial_liquidity,
    )
    
    return MarketDetail(
        id=market["id"],
        title=market["title"],
        description=market["description"],
        probability=get_cpmm_probability(market["pool"], market["p"]),
        status=market["status"],
        closes_at=market["closes_at"],
        created_at=market["created_at"],
        resolved_at=market["resolved_at"],
        resolution=market["resolution"],
        total_volume=market["total_volume"],
        creator_id=market["creator_id"],
        pool=market["pool"],
        p=market["p"],
    )


@app.post("/markets/{market_id}/resolve", response_model=MarketDetail)
async def resolve_market(market_id: str, req: MarketResolve, user: dict = Depends(get_current_user)):
    """Resolve a market. Only creator can resolve."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    if market["creator_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only creator can resolve market")
    
    if market["status"] == MarketStatus.RESOLVED:
        raise HTTPException(status_code=400, detail="Market already resolved")
    
    db.resolve_market(market_id, req.outcome)
    
    # Payout positions
    for pos in db.get_market_positions(market_id):
        winning_shares = pos["yes_shares"] if req.outcome == Outcome.YES else pos["no_shares"]
        if winning_shares > 0:
            payout = winning_shares  # Each winning share pays $1
            db.update_user_balance(pos["user_id"], payout)
            db.users[pos["user_id"]]["profit_all_time"] += payout - pos["total_invested"]
    
    market = db.get_market(market_id)
    return MarketDetail(
        id=market["id"],
        title=market["title"],
        description=market["description"],
        probability=1.0 if req.outcome == Outcome.YES else 0.0,
        status=market["status"],
        closes_at=market["closes_at"],
        created_at=market["created_at"],
        resolved_at=market["resolved_at"],
        resolution=market["resolution"],
        total_volume=market["total_volume"],
        creator_id=market["creator_id"],
        pool=market["pool"],
        p=market["p"],
    )


# =============================================================================
# Trading Endpoints
# =============================================================================

@app.post("/markets/{market_id}/bet", response_model=BetResponse)
async def place_bet(market_id: str, req: BetRequest, user: dict = Depends(get_current_user)):
    """Place a bet on a market."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    if market["status"] != MarketStatus.OPEN:
        raise HTTPException(status_code=400, detail="Market is not open for trading")
    
    if market["closes_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Market has closed")
    
    if user["balance"] < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    
    # Calculate bet using CPMM
    prob_before = get_cpmm_probability(market["pool"], market["p"])
    
    state = CpmmState(pool=market["pool"].copy(), p=market["p"])
    result = calculate_cpmm_purchase(state, req.amount, req.outcome.value)
    
    shares = result["shares"]
    if shares <= 0:
        raise HTTPException(status_code=400, detail="Trade would result in zero or negative shares")
    
    prob_after = get_cpmm_probability(result["new_pool"], result["new_p"])
    
    # Execute trade
    db.update_user_balance(user["id"], -req.amount)
    db.update_market_pool(market_id, result["new_pool"], result["new_p"], req.amount)
    db.update_position(market_id, user["id"], req.outcome, shares, req.amount)
    
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
    
    return BetResponse(
        bet_id=bet["id"],
        market_id=bet["market_id"],
        user_id=bet["user_id"],
        outcome=bet["outcome"],
        amount=bet["amount"],
        shares=bet["shares"],
        avg_price=bet["avg_price"],
        probability_before=bet["probability_before"],
        probability_after=bet["probability_after"],
        created_at=bet["created_at"],
    )


@app.get("/markets/{market_id}/positions", response_model=MarketPositions)
async def get_positions(market_id: str):
    """Get all positions for a market."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    prob = get_cpmm_probability(market["pool"], market["p"])
    positions = []
    
    for pos in db.get_market_positions(market_id):
        # Calculate current value based on probability
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


@app.get("/markets/{market_id}/history", response_model=MarketHistory)
async def get_market_history(market_id: str):
    """Get probability history for charts."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    # Get all bets for this market, sorted by time
    market_bets = sorted(
        [b for b in db.bets.values() if b["market_id"] == market_id],
        key=lambda x: x["created_at"]
    )
    
    points = []
    
    # Add initial point (50% at market creation)
    points.append(ProbabilityPoint(
        timestamp=market["created_at"],
        probability=0.5,
        volume=0.0,
    ))
    
    # Add point for each bet
    cumulative_volume = 0.0
    for bet in market_bets:
        cumulative_volume += bet["amount"]
        points.append(ProbabilityPoint(
            timestamp=bet["created_at"],
            probability=bet["probability_after"],
            volume=cumulative_volume,
        ))
    
    return MarketHistory(market_id=market_id, points=points)


@app.get("/markets/{market_id}/bets", response_model=List[BetHistoryItem])
async def get_market_bets(market_id: str):
    """Get all bets for a market."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    market_bets = sorted(
        [b for b in db.bets.values() if b["market_id"] == market_id],
        key=lambda x: x["created_at"],
        reverse=True  # Most recent first
    )
    
    items = []
    for bet in market_bets:
        user = db.get_user(bet["user_id"])
        items.append(BetHistoryItem(
            bet_id=bet["id"],
            user_id=bet["user_id"],
            username=user["username"] if user else "unknown",
            outcome=bet["outcome"],
            amount=bet["amount"],
            shares=bet["shares"],
            probability_after=bet["probability_after"],
            created_at=bet["created_at"],
        ))
    
    return items


# =============================================================================
# User Endpoints
# =============================================================================

@app.get("/me", response_model=UserMe)
async def get_me(user: dict = Depends(get_current_user)):
    """Get current user profile with balance."""
    return UserMe(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        balance=user["balance"],
        created_at=user["created_at"],
        markets_created=user["markets_created"],
        total_bets=user["total_bets"],
        profit_all_time=user["profit_all_time"],
    )


@app.get("/users/{user_id}", response_model=UserProfile)
async def get_user(user_id: str):
    """Get public user profile."""
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return UserProfile(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        created_at=user["created_at"],
        markets_created=user["markets_created"],
        total_bets=user["total_bets"],
        profit_all_time=user["profit_all_time"],
    )


# =============================================================================
# Agent Registration
# =============================================================================

@app.post("/agents/register", response_model=AgentRegistered)
async def register_agent(req: AgentRegister):
    """
    Register a new agent and get an API key.
    
    The API key is only returned ONCE — save it securely!
    """
    # Check if username is taken
    if db.get_user_by_username(req.username):
        raise HTTPException(status_code=400, detail="Username already taken")
    
    # Generate API key and user ID
    api_key = generate_api_key()
    user_id = str(uuid.uuid4())
    
    # Create user with hashed API key
    user = db.create_user(
        user_id=user_id,
        username=req.username,
        balance=1000.0,  # Starting balance
        api_key_hash=hash_api_key(api_key),
        description=req.description or "",
    )
    
    # Update display name if provided
    if req.display_name:
        user["display_name"] = req.display_name
        db._save()
    
    return AgentRegistered(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        api_key=api_key,  # Only time we return the raw key!
        balance=user["balance"],
        created_at=user["created_at"],
    )


@app.post("/agents/reset-key", response_model=AgentKeyReset)
async def reset_api_key(user: dict = Depends(get_current_user)):
    """
    Reset your API key. Requires current valid API key.
    
    Old key becomes invalid immediately.
    """
    new_key = generate_api_key()
    db.update_api_key(user["id"], hash_api_key(new_key))
    
    return AgentKeyReset(
        user_id=user["id"],
        api_key=new_key,
    )


# =============================================================================
# Leaderboard
# =============================================================================

@app.get("/leaderboard", response_model=List[LeaderboardEntry])
async def get_leaderboard():
    """Get leaderboard sorted by profit."""
    entries = []
    
    for user in db.users.values():
        # Calculate total volume from bets
        total_volume = sum(
            bet["amount"] 
            for bet in db.bets.values() 
            if bet["user_id"] == user["id"]
        )
        
        # Calculate win rate (simplified: resolved bets where user had winning position)
        user_bets = [b for b in db.bets.values() if b["user_id"] == user["id"]]
        wins = 0
        resolved_bets = 0
        
        for bet in user_bets:
            market = db.get_market(bet["market_id"])
            if market and market["status"] == MarketStatus.RESOLVED:
                resolved_bets += 1
                if market["resolution"] == bet["outcome"]:
                    wins += 1
        
        win_rate = wins / resolved_bets if resolved_bets > 0 else 0.5
        
        entries.append(LeaderboardEntry(
            user_id=user["id"],
            username=user["username"],
            pnl=user["profit_all_time"],
            total_volume=total_volume,
            win_rate=win_rate,
        ))
    
    # Sort by PNL descending
    entries.sort(key=lambda x: x.pnl, reverse=True)
    return entries[:50]  # Top 50


# =============================================================================
# Health Check
# =============================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "markets": len(db.markets), "users": len(db.users)}


# =============================================================================
# Test / Dev
# =============================================================================

if __name__ == "__main__":
    import asyncio
    
    print("=" * 60)
    print("MoltMarkets API - Quick Test")
    print("=" * 60)
    
    async def run_test():
        # Simulate requests without running actual server
        
        # 1. Create a user
        print("\n1. Creating demo user...")
        db.create_user("test-user", "test_user", balance=1000.0)
        user = db.get_user("test-user")
        print(f"   User: {user['username']}, Balance: ${user['balance']}")
        
        # 2. Create a market
        print("\n2. Creating a market...")
        from datetime import timedelta
        market_id = str(uuid.uuid4())
        market = db.create_market(
            market_id=market_id,
            creator_id="test-user",
            title="Will BTC hit $100k by end of 2025?",
            description="Resolves YES if Bitcoin price exceeds $100,000 USD at any point before Dec 31, 2025 11:59 PM UTC.",
            closes_at=datetime.now(timezone.utc) + timedelta(days=365),
            initial_liquidity=100.0,
        )
        prob = get_cpmm_probability(market["pool"], market["p"])
        print(f"   Market: {market['title'][:40]}...")
        print(f"   Pool: {market['pool']}, Probability: {prob:.2%}")
        
        # 3. Place a YES bet
        print("\n3. Placing $50 bet on YES...")
        state = CpmmState(pool=market["pool"].copy(), p=market["p"])
        result = calculate_cpmm_purchase(state, 50, "YES")
        
        shares = result["shares"]
        db.update_user_balance("test-user", -50)
        db.update_market_pool(market_id, result["new_pool"], result["new_p"], 50)
        db.update_position(market_id, "test-user", Outcome.YES, shares, 50)
        
        new_prob = get_cpmm_probability(result["new_pool"], result["new_p"])
        print(f"   Shares received: {shares:.2f}")
        print(f"   Avg price: ${50/shares:.4f} per share")
        print(f"   Probability: {prob:.2%} → {new_prob:.2%}")
        
        # 4. Check position
        print("\n4. Checking position...")
        pos = db.get_position(market_id, "test-user")
        current_value = pos["yes_shares"] * new_prob
        print(f"   YES shares: {pos['yes_shares']:.2f}")
        print(f"   Total invested: ${pos['total_invested']:.2f}")
        print(f"   Current value: ${current_value:.2f}")
        print(f"   P&L: ${current_value - pos['total_invested']:.2f}")
        
        # 5. Check user balance
        print("\n5. Checking user balance...")
        user = db.get_user("test-user")
        print(f"   Balance: ${user['balance']:.2f}")
        print(f"   Total bets: {user['total_bets']}")
        
        print("\n" + "=" * 60)
        print("Test complete! Run with: uvicorn api:app --reload")
        print("=" * 60)
    
    asyncio.run(run_test())
