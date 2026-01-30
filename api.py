"""
MoltMarkets API — FastAPI application.

Binary prediction markets with CPMM market maker.
Uses PostgreSQL for persistence.
"""

import os
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware

from cpmm import CpmmState, calculate_cpmm_purchase, get_cpmm_probability, Outcome as CpmmOutcome
import secrets
import hashlib
import psycopg2
from psycopg2.extras import RealDictCursor

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


# =============================================================================
# PostgreSQL Storage
# =============================================================================

class Storage:
    """
    PostgreSQL storage backend.
    
    Uses DATABASE_URL environment variable (Railway provides this automatically).
    Falls back to JSON file storage if DATABASE_URL is not set.
    """
    
    def __init__(self):
        self.database_url = os.getenv("DATABASE_URL")
        if self.database_url:
            self._init_db()
        else:
            print("Warning: DATABASE_URL not set, using in-memory storage (data will be lost on restart)")
            # Fallback to in-memory for local dev without DB
            self._use_memory = True
            self._markets: Dict[str, dict] = {}
            self._users: Dict[str, dict] = {}
            self._bets: Dict[str, dict] = {}
            self._positions: Dict[str, Dict[str, dict]] = {}
    
    def _get_conn(self):
        """Get a database connection."""
        return psycopg2.connect(self.database_url, cursor_factory=RealDictCursor)
    
    def _init_db(self):
        """Initialize database tables."""
        self._use_memory = False
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Users table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id VARCHAR(255) PRIMARY KEY,
                        username VARCHAR(255) UNIQUE NOT NULL,
                        display_name VARCHAR(255),
                        description TEXT DEFAULT '',
                        balance DECIMAL(20, 8) DEFAULT 1000.0,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        markets_created INTEGER DEFAULT 0,
                        total_bets INTEGER DEFAULT 0,
                        profit_all_time DECIMAL(20, 8) DEFAULT 0.0,
                        api_key_hash VARCHAR(255)
                    )
                """)
                
                # Markets table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS markets (
                        id VARCHAR(255) PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        status VARCHAR(50) DEFAULT 'open',
                        closes_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        resolved_at TIMESTAMP WITH TIME ZONE,
                        resolution VARCHAR(50),
                        total_volume DECIMAL(20, 8) DEFAULT 0.0,
                        creator_id VARCHAR(255) REFERENCES users(id),
                        pool_yes DECIMAL(20, 8) DEFAULT 100.0,
                        pool_no DECIMAL(20, 8) DEFAULT 100.0,
                        p DECIMAL(10, 8) DEFAULT 0.5
                    )
                """)
                
                # Bets table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bets (
                        id VARCHAR(255) PRIMARY KEY,
                        market_id VARCHAR(255) REFERENCES markets(id),
                        user_id VARCHAR(255) REFERENCES users(id),
                        outcome VARCHAR(50) NOT NULL,
                        amount DECIMAL(20, 8) NOT NULL,
                        shares DECIMAL(20, 8) NOT NULL,
                        avg_price DECIMAL(20, 8),
                        probability_before DECIMAL(10, 8),
                        probability_after DECIMAL(10, 8),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)
                
                # Positions table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS positions (
                        market_id VARCHAR(255) REFERENCES markets(id),
                        user_id VARCHAR(255) REFERENCES users(id),
                        yes_shares DECIMAL(20, 8) DEFAULT 0.0,
                        no_shares DECIMAL(20, 8) DEFAULT 0.0,
                        total_invested DECIMAL(20, 8) DEFAULT 0.0,
                        PRIMARY KEY (market_id, user_id)
                    )
                """)
                
                # Create indexes for common queries
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_market ON bets(market_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key_hash)")
                
                conn.commit()
                print("Database tables initialized")
        finally:
            conn.close()
    
    def _row_to_user(self, row: dict) -> dict:
        """Convert database row to user dict."""
        if not row:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"] or row["username"],
            "description": row["description"] or "",
            "balance": float(row["balance"]),
            "created_at": row["created_at"],
            "markets_created": row["markets_created"],
            "total_bets": row["total_bets"],
            "profit_all_time": float(row["profit_all_time"]),
            "api_key_hash": row["api_key_hash"],
        }
    
    def _row_to_market(self, row: dict) -> dict:
        """Convert database row to market dict."""
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "status": MarketStatus(row["status"]),
            "closes_at": row["closes_at"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolution": Outcome(row["resolution"]) if row["resolution"] else None,
            "total_volume": float(row["total_volume"]),
            "creator_id": row["creator_id"],
            "pool": {"YES": float(row["pool_yes"]), "NO": float(row["pool_no"])},
            "p": float(row["p"]),
        }
    
    def _row_to_bet(self, row: dict) -> dict:
        """Convert database row to bet dict."""
        if not row:
            return None
        return {
            "id": row["id"],
            "market_id": row["market_id"],
            "user_id": row["user_id"],
            "outcome": Outcome(row["outcome"]),
            "amount": float(row["amount"]),
            "shares": float(row["shares"]),
            "avg_price": float(row["avg_price"]) if row["avg_price"] else 0,
            "probability_before": float(row["probability_before"]) if row["probability_before"] else 0,
            "probability_after": float(row["probability_after"]) if row["probability_after"] else 0,
            "created_at": row["created_at"],
        }
    
    def _row_to_position(self, row: dict) -> dict:
        """Convert database row to position dict."""
        if not row:
            return None
        return {
            "market_id": row["market_id"],
            "user_id": row["user_id"],
            "yes_shares": float(row["yes_shares"]),
            "no_shares": float(row["no_shares"]),
            "total_invested": float(row["total_invested"]),
        }
    
    # --- Users ---
    
    def get_user(self, user_id: str) -> Optional[dict]:
        if self._use_memory:
            return self._users.get(user_id)
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                return self._row_to_user(row)
        finally:
            conn.close()
    
    def create_user(self, user_id: str, username: str, balance: float = 1000.0, 
                    api_key_hash: str = None, description: str = "") -> dict:
        if self._use_memory:
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
            self._users[user_id] = user
            return user
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, username, display_name, description, balance, api_key_hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (user_id, username, username, description, balance, api_key_hash))
                row = cur.fetchone()
                conn.commit()
                return self._row_to_user(row)
        finally:
            conn.close()
    
    def get_user_by_api_key(self, api_key: str) -> Optional[dict]:
        """Find user by API key."""
        key_hash = hash_api_key(api_key)
        if self._use_memory:
            for user in self._users.values():
                if user.get("api_key_hash") == key_hash:
                    return user
            return None
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE api_key_hash = %s", (key_hash,))
                row = cur.fetchone()
                return self._row_to_user(row)
        finally:
            conn.close()
    
    def get_user_by_username(self, username: str) -> Optional[dict]:
        """Find user by username."""
        if self._use_memory:
            for user in self._users.values():
                if user.get("username") == username:
                    return user
            return None
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
                return self._row_to_user(row)
        finally:
            conn.close()
    
    def update_api_key(self, user_id: str, new_key_hash: str):
        """Update user's API key hash."""
        if self._use_memory:
            self._users[user_id]["api_key_hash"] = new_key_hash
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET api_key_hash = %s WHERE id = %s", (new_key_hash, user_id))
                conn.commit()
        finally:
            conn.close()
    
    def update_user_balance(self, user_id: str, delta: float) -> float:
        if self._use_memory:
            self._users[user_id]["balance"] += delta
            return self._users[user_id]["balance"]
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users SET balance = balance + %s WHERE id = %s
                    RETURNING balance
                """, (delta, user_id))
                row = cur.fetchone()
                conn.commit()
                return float(row["balance"])
        finally:
            conn.close()
    
    def update_user_display_name(self, user_id: str, display_name: str):
        """Update user's display name."""
        if self._use_memory:
            self._users[user_id]["display_name"] = display_name
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET display_name = %s WHERE id = %s", (display_name, user_id))
                conn.commit()
        finally:
            conn.close()
    
    def increment_user_markets_created(self, user_id: str):
        """Increment user's markets_created counter."""
        if self._use_memory:
            self._users[user_id]["markets_created"] += 1
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET markets_created = markets_created + 1 WHERE id = %s", (user_id,))
                conn.commit()
        finally:
            conn.close()
    
    def increment_user_total_bets(self, user_id: str):
        """Increment user's total_bets counter."""
        if self._use_memory:
            self._users[user_id]["total_bets"] += 1
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET total_bets = total_bets + 1 WHERE id = %s", (user_id,))
                conn.commit()
        finally:
            conn.close()
    
    def update_user_profit(self, user_id: str, profit_delta: float):
        """Update user's profit_all_time."""
        if self._use_memory:
            self._users[user_id]["profit_all_time"] += profit_delta
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET profit_all_time = profit_all_time + %s WHERE id = %s", (profit_delta, user_id))
                conn.commit()
        finally:
            conn.close()
    
    @property
    def users(self) -> Dict[str, dict]:
        """Get all users (for leaderboard etc.)."""
        if self._use_memory:
            return self._users
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users")
                rows = cur.fetchall()
                return {row["id"]: self._row_to_user(row) for row in rows}
        finally:
            conn.close()
    
    # --- Markets ---
    
    def get_market(self, market_id: str) -> Optional[dict]:
        if self._use_memory:
            return self._markets.get(market_id)
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM markets WHERE id = %s", (market_id,))
                row = cur.fetchone()
                return self._row_to_market(row)
        finally:
            conn.close()
    
    def list_markets(self) -> List[dict]:
        if self._use_memory:
            return list(self._markets.values())
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM markets ORDER BY created_at DESC")
                rows = cur.fetchall()
                return [self._row_to_market(row) for row in rows]
        finally:
            conn.close()
    
    @property
    def markets(self) -> Dict[str, dict]:
        """Get all markets as dict."""
        if self._use_memory:
            return self._markets
        
        markets_list = self.list_markets()
        return {m["id"]: m for m in markets_list}
    
    def create_market(self, market_id: str, creator_id: str, title: str,
                      description: str, closes_at: datetime, 
                      initial_liquidity: float) -> dict:
        if self._use_memory:
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
                "p": 0.5,
            }
            self._markets[market_id] = market
            self._positions[market_id] = {}
            self._users[creator_id]["markets_created"] += 1
            return market
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO markets (id, title, description, closes_at, creator_id, pool_yes, pool_no, p)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (market_id, title, description, closes_at, creator_id, initial_liquidity, initial_liquidity, 0.5))
                row = cur.fetchone()
                
                # Increment user's markets_created
                cur.execute("UPDATE users SET markets_created = markets_created + 1 WHERE id = %s", (creator_id,))
                conn.commit()
                return self._row_to_market(row)
        finally:
            conn.close()
    
    def update_market_pool(self, market_id: str, new_pool: dict, new_p: float, volume_delta: float):
        if self._use_memory:
            market = self._markets[market_id]
            market["pool"] = new_pool
            market["p"] = new_p
            market["total_volume"] += volume_delta
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE markets 
                    SET pool_yes = %s, pool_no = %s, p = %s, total_volume = total_volume + %s
                    WHERE id = %s
                """, (new_pool["YES"], new_pool["NO"], new_p, volume_delta, market_id))
                conn.commit()
        finally:
            conn.close()
    
    def resolve_market(self, market_id: str, outcome: Outcome):
        if self._use_memory:
            market = self._markets[market_id]
            market["status"] = MarketStatus.RESOLVED
            market["resolution"] = outcome
            market["resolved_at"] = datetime.now(timezone.utc)
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE markets 
                    SET status = %s, resolution = %s, resolved_at = %s
                    WHERE id = %s
                """, (MarketStatus.RESOLVED.value, outcome.value, datetime.now(timezone.utc), market_id))
                conn.commit()
        finally:
            conn.close()
    
    # --- Bets ---
    
    def create_bet(self, bet_id: str, market_id: str, user_id: str,
                   outcome: Outcome, amount: float, shares: float,
                   prob_before: float, prob_after: float) -> dict:
        avg_price = amount / shares if shares > 0 else 0
        
        if self._use_memory:
            bet = {
                "id": bet_id,
                "market_id": market_id,
                "user_id": user_id,
                "outcome": outcome,
                "amount": amount,
                "shares": shares,
                "avg_price": avg_price,
                "probability_before": prob_before,
                "probability_after": prob_after,
                "created_at": datetime.now(timezone.utc),
            }
            self._bets[bet_id] = bet
            self._users[user_id]["total_bets"] += 1
            return bet
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bets (id, market_id, user_id, outcome, amount, shares, avg_price, probability_before, probability_after)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (bet_id, market_id, user_id, outcome.value, amount, shares, avg_price, prob_before, prob_after))
                row = cur.fetchone()
                
                # Increment user's total_bets
                cur.execute("UPDATE users SET total_bets = total_bets + 1 WHERE id = %s", (user_id,))
                conn.commit()
                return self._row_to_bet(row)
        finally:
            conn.close()
    
    def get_bets_for_market(self, market_id: str) -> List[dict]:
        """Get all bets for a market."""
        if self._use_memory:
            return [b for b in self._bets.values() if b["market_id"] == market_id]
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bets WHERE market_id = %s ORDER BY created_at", (market_id,))
                rows = cur.fetchall()
                return [self._row_to_bet(row) for row in rows]
        finally:
            conn.close()
    
    def get_bets_for_user(self, user_id: str) -> List[dict]:
        """Get all bets for a user."""
        if self._use_memory:
            return [b for b in self._bets.values() if b["user_id"] == user_id]
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bets WHERE user_id = %s ORDER BY created_at", (user_id,))
                rows = cur.fetchall()
                return [self._row_to_bet(row) for row in rows]
        finally:
            conn.close()
    
    @property
    def bets(self) -> Dict[str, dict]:
        """Get all bets as dict."""
        if self._use_memory:
            return self._bets
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bets")
                rows = cur.fetchall()
                return {row["id"]: self._row_to_bet(row) for row in rows}
        finally:
            conn.close()
    
    # --- Positions ---
    
    def get_position(self, market_id: str, user_id: str) -> Optional[dict]:
        if self._use_memory:
            return self._positions.get(market_id, {}).get(user_id)
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM positions WHERE market_id = %s AND user_id = %s", (market_id, user_id))
                row = cur.fetchone()
                return self._row_to_position(row)
        finally:
            conn.close()
    
    def get_market_positions(self, market_id: str) -> List[dict]:
        if self._use_memory:
            return list(self._positions.get(market_id, {}).values())
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM positions WHERE market_id = %s", (market_id,))
                rows = cur.fetchall()
                return [self._row_to_position(row) for row in rows]
        finally:
            conn.close()
    
    def update_position(self, market_id: str, user_id: str, 
                        outcome: Outcome, shares_delta: float, invested_delta: float):
        if self._use_memory:
            if market_id not in self._positions:
                self._positions[market_id] = {}
            
            if user_id not in self._positions[market_id]:
                self._positions[market_id][user_id] = {
                    "user_id": user_id,
                    "market_id": market_id,
                    "yes_shares": 0.0,
                    "no_shares": 0.0,
                    "total_invested": 0.0,
                }
            
            pos = self._positions[market_id][user_id]
            if outcome == Outcome.YES:
                pos["yes_shares"] += shares_delta
            else:
                pos["no_shares"] += shares_delta
            pos["total_invested"] += invested_delta
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Upsert position
                if outcome == Outcome.YES:
                    cur.execute("""
                        INSERT INTO positions (market_id, user_id, yes_shares, no_shares, total_invested)
                        VALUES (%s, %s, %s, 0, %s)
                        ON CONFLICT (market_id, user_id) DO UPDATE
                        SET yes_shares = positions.yes_shares + %s, total_invested = positions.total_invested + %s
                    """, (market_id, user_id, shares_delta, invested_delta, shares_delta, invested_delta))
                else:
                    cur.execute("""
                        INSERT INTO positions (market_id, user_id, yes_shares, no_shares, total_invested)
                        VALUES (%s, %s, 0, %s, %s)
                        ON CONFLICT (market_id, user_id) DO UPDATE
                        SET no_shares = positions.no_shares + %s, total_invested = positions.total_invested + %s
                    """, (market_id, user_id, shares_delta, invested_delta, shares_delta, invested_delta))
                conn.commit()
        finally:
            conn.close()
    
    # --- Utility ---
    
    def _save(self):
        """No-op for PostgreSQL (kept for compatibility)."""
        pass


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
    if not db.get_user("demo-user"):
        db.create_user("demo-user", "demo_user", balance=10000.0)
    market_count = len(db.list_markets())
    user_count = len(db.users)
    print(f"MoltMarkets API started with {market_count} markets, {user_count} users")
    yield
    # Shutdown
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
            db.update_user_profit(pos["user_id"], payout - pos["total_invested"])
    
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
        db.get_bets_for_market(market_id),
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
        db.get_bets_for_market(market_id),
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
        db.update_user_display_name(user_id, req.display_name)
        user["display_name"] = req.display_name
    
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
    all_users = db.users
    all_bets = db.bets
    all_markets = db.markets
    
    for user in all_users.values():
        # Calculate total volume from bets
        total_volume = sum(
            bet["amount"] 
            for bet in all_bets.values() 
            if bet["user_id"] == user["id"]
        )
        
        # Calculate win rate (simplified: resolved bets where user had winning position)
        user_bets = [b for b in all_bets.values() if b["user_id"] == user["id"]]
        wins = 0
        resolved_bets = 0
        
        for bet in user_bets:
            market = all_markets.get(bet["market_id"])
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
    market_count = len(db.list_markets())
    user_count = len(db.users)
    return {"status": "ok", "markets": market_count, "users": user_count}


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
