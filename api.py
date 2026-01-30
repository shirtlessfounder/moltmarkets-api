"""
MoltMarkets API — FastAPI application.

Binary prediction markets with CPMM market maker.
Uses PostgreSQL for persistence.
"""

import os
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import httpx

from cpmm import CpmmState, calculate_cpmm_purchase, calculate_cpmm_sale, get_cpmm_probability, Outcome as CpmmOutcome
import secrets
import hashlib
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from models import (
    MarketCreate, MarketResolve, MarketSummary, MarketDetail, MarketCreated,
    BetRequest, BetResponse, SellRequest, SellResponse, Position, MarketPositions,
    UserProfile, UserMe, ErrorResponse, LeaderboardEntry,
    ProbabilityPoint, MarketHistory, BetHistoryItem,
    AgentRegister, AgentRegistered, AgentRegisteredWithClaim, AgentKeyReset,
    ClaimPageInfo, ClaimRequest, ClaimResponse, AgentStatus,
    CommentCreate, Comment, MarketComments,
    ResolutionRequest, ResolutionResult, ResolutionVote,
    MarketStatus, Outcome,
)
from resolver import resolve_market, get_resolution_summary
import random
import re


# =============================================================================
# Verification Code Generation
# =============================================================================

VERIFICATION_WORDS = [
    "crab", "shell", "reef", "wave", "tide", "coral", "kelp", "pearl", "anchor", "lobster",
    "orca", "squid", "trout", "shark", "whale", "dune", "marsh", "delta", "fjord", "shoal",
]


# =============================================================================
# Economics Constants
# =============================================================================

TRADE_FEE_RATE = 0.02  # 2% total fee
CREATOR_FEE_SHARE = 0.5  # 50% of fee goes to market creator (1%)
# Remaining 50% (1%) is burned (not allocated to anyone)

MARKET_CREATION_COOLDOWN_MINUTES = 30  # Rate limit for market creation


def generate_verification_code() -> str:
    """Generate a cryptographically secure verification code like 'crab-reef-A1B2C3D4'.

    Uses secrets module for randomness.  Two words (20 options each) + 8 alphanumeric
    chars gives ~20^2 * 36^8 ≈ 1.1 trillion possibilities — infeasible to brute-force.
    """
    _alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    word1 = secrets.choice(VERIFICATION_WORDS)
    word2 = secrets.choice(VERIFICATION_WORDS)
    chars = ''.join(secrets.choice(_alphabet) for _ in range(8))
    return f"{word1}-{word2}-{chars}"


def is_valid_twitter_url(url: str) -> bool:
    """Check if URL looks like a Twitter/X post URL."""
    pattern = r'^https?://(www\.)?(twitter\.com|x\.com)/[a-zA-Z0-9_]+/status/\d+'
    return bool(re.match(pattern, url))


def extract_tweet_id(url: str) -> Optional[str]:
    """Extract tweet ID from a Twitter/X URL."""
    pattern = r'(?:twitter\.com|x\.com)/[a-zA-Z0-9_]+/status/(\d+)'
    match = re.search(pattern, url)
    return match.group(1) if match else None


def extract_twitter_handle(url: str) -> Optional[str]:
    """Extract Twitter username from a Twitter/X URL."""
    pattern = r'(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/'
    match = re.search(pattern, url)
    return match.group(1) if match else None


async def fetch_tweet(tweet_id: str) -> dict:
    """
    Fetch tweet content using Twitter's syndication API (no auth required).
    
    Returns dict with tweet data or raises HTTPException on error.
    """
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=x"
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url)
            
            if response.status_code == 404:
                raise HTTPException(
                    status_code=400,
                    detail="Tweet not found. It may be deleted or private."
                )
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch tweet (Twitter returned {response.status_code})"
                )
            
            data = response.json()
            
            # Check if tweet data is valid
            if not data or "text" not in data:
                raise HTTPException(
                    status_code=400,
                    detail="Tweet not accessible. It may be from a private or suspended account."
                )
            
            return data
            
        except httpx.TimeoutException:
            raise HTTPException(
                status_code=504,
                detail="Timeout while fetching tweet. Please try again."
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Network error while fetching tweet: {str(e)}"
            )


def verify_tweet_contains_code(tweet_text: str, code: str) -> bool:
    """
    Check if the tweet text contains the verification code.
    
    Case-insensitive match.
    """
    return code.lower() in tweet_text.lower()


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
        self._pool = None
        if self.database_url:
            self._init_pool()
            self._init_db()
        else:
            print("Warning: DATABASE_URL not set, using in-memory storage (data will be lost on restart)")
            # Fallback to in-memory for local dev without DB
            self._use_memory = True
            self._markets: Dict[str, dict] = {}
            self._users: Dict[str, dict] = {}
            self._bets: Dict[str, dict] = {}
            self._positions: Dict[str, Dict[str, dict]] = {}
    
    def _init_pool(self):
        """Initialize the connection pool."""
        parsed = urlparse(self.database_url)
        self._pool = pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            host=parsed.hostname,
            port=parsed.port or 5432,
            user=parsed.username,
            password=unquote(parsed.password) if parsed.password else None,
            dbname=parsed.path.lstrip('/'),
            cursor_factory=RealDictCursor
        )
        print("Connection pool initialized (min=1, max=10)")
    
    def _get_conn(self):
        """Get a database connection from the pool."""
        return self._pool.getconn()
    
    def _put_conn(self, conn):
        """Return a connection to the pool."""
        self._pool.putconn(conn)
    
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
                        api_key_hash VARCHAR(255),
                        status VARCHAR(50) DEFAULT 'pending',
                        verification_code VARCHAR(50),
                        twitter_handle VARCHAR(100)
                    )
                """)
                
                # Add columns if they don't exist (for existing databases)
                cur.execute("""
                    DO $$ 
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='status') THEN
                            ALTER TABLE users ADD COLUMN status VARCHAR(50) DEFAULT 'pending';
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='verification_code') THEN
                            ALTER TABLE users ADD COLUMN verification_code VARCHAR(50);
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='last_market_created_at') THEN
                            ALTER TABLE users ADD COLUMN last_market_created_at TIMESTAMP WITH TIME ZONE;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='twitter_handle') THEN
                            ALTER TABLE users ADD COLUMN twitter_handle VARCHAR(100);
                        END IF;
                    END $$;
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
                
                # Comments table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS comments (
                        id VARCHAR(255) PRIMARY KEY,
                        market_id VARCHAR(255) REFERENCES markets(id),
                        user_id VARCHAR(255) REFERENCES users(id),
                        content TEXT NOT NULL,
                        parent_id VARCHAR(255),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)
                
                # Resolution votes table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS resolution_votes (
                        id VARCHAR(255) PRIMARY KEY,
                        market_id VARCHAR(255) REFERENCES markets(id),
                        agent_id VARCHAR(255) NOT NULL,
                        vote VARCHAR(10) NOT NULL,
                        reasoning TEXT,
                        sources TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)
                
                # Create indexes for common queries
                cur.execute("CREATE INDEX IF NOT EXISTS idx_resolution_votes_market ON resolution_votes(market_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_comments_market ON comments(market_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_market ON bets(market_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key_hash)")
                
                conn.commit()
                print("Database tables initialized")
        finally:
            self._put_conn(conn)
    
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
            "status": row.get("status", "pending"),
            "verification_code": row.get("verification_code"),
            "last_market_created_at": row.get("last_market_created_at"),
            "twitter_handle": row.get("twitter_handle"),
        }
    
    def _row_to_market(self, row: dict) -> dict:
        """Convert database row to market dict."""
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "status": MarketStatus(row["status"].upper()),
            "closes_at": row["closes_at"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolution": Outcome(row["resolution"].upper()) if row["resolution"] else None,
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
            "outcome": Outcome(row["outcome"].upper()),
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
            self._put_conn(conn)
    
    def create_user(self, user_id: str, username: str, balance: float = 1000.0, 
                    api_key_hash: str = None, description: str = "",
                    status: str = "pending", verification_code: str = None) -> dict:
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
                "status": status,
                "verification_code": verification_code,
                "twitter_handle": None,
            }
            self._users[user_id] = user
            return user
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, username, display_name, description, balance, api_key_hash, status, verification_code)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (user_id, username, username, description, balance, api_key_hash, status, verification_code))
                row = cur.fetchone()
                conn.commit()
                return self._row_to_user(row)
        finally:
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
    def get_user_by_username(self, username: str) -> Optional[dict]:
        """Find user by username (case-insensitive)."""
        username_lower = username.lower()
        if self._use_memory:
            for user in self._users.values():
                if user.get("username", "").lower() == username_lower:
                    return user
            return None
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(%s)", (username_lower,))
                row = cur.fetchone()
                return self._row_to_user(row)
        finally:
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
    def delete_user(self, user_id: str):
        """Delete a user and all their data (admin only)."""
        if self._use_memory:
            if user_id in self._users:
                del self._users[user_id]
            # Also clean up positions and bets
            self._positions = {k: v for k, v in self._positions.items() if v.get("user_id") != user_id}
            self._bets = [b for b in self._bets if b.get("user_id") != user_id]
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Delete in order due to foreign keys
                cur.execute("DELETE FROM bets WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM positions WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM comments WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
    def update_user_status(self, user_id: str, status: str):
        """Update user's claim status."""
        if self._use_memory:
            self._users[user_id]["status"] = status
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET status = %s WHERE id = %s", (status, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)
    
    def update_user_last_market_created(self, user_id: str):
        """Update timestamp when user last created a market (for rate limiting)."""
        now = datetime.now(timezone.utc)
        if self._use_memory:
            self._users[user_id]["last_market_created_at"] = now
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET last_market_created_at = %s WHERE id = %s",
                    (now, user_id)
                )
                conn.commit()
        finally:
            self._put_conn(conn)
    
    def update_user_twitter_handle(self, user_id: str, twitter_handle: str):
        """Update user's twitter handle (from verification tweet)."""
        if self._use_memory:
            self._users[user_id]["twitter_handle"] = twitter_handle
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET twitter_handle = %s WHERE id = %s", (twitter_handle, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
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
            self._put_conn(conn)
    
    def reduce_position(self, market_id: str, user_id: str, 
                        outcome: Outcome, shares: float):
        """Reduce shares in a position (for selling)."""
        if self._use_memory:
            if market_id in self._positions and user_id in self._positions[market_id]:
                pos = self._positions[market_id][user_id]
                if outcome == Outcome.YES:
                    pos["yes_shares"] = max(0, pos["yes_shares"] - shares)
                else:
                    pos["no_shares"] = max(0, pos["no_shares"] - shares)
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if outcome == Outcome.YES:
                    cur.execute("""
                        UPDATE positions 
                        SET yes_shares = GREATEST(0, yes_shares - %s)
                        WHERE market_id = %s AND user_id = %s
                    """, (shares, market_id, user_id))
                else:
                    cur.execute("""
                        UPDATE positions 
                        SET no_shares = GREATEST(0, no_shares - %s)
                        WHERE market_id = %s AND user_id = %s
                    """, (shares, market_id, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)
    
    # --- Comments ---
    
    def create_comment(self, comment_id: str, market_id: str, user_id: str, 
                       content: str, parent_id: Optional[str] = None) -> dict:
        """Create a new comment on a market."""
        now = datetime.now(timezone.utc)
        comment = {
            "id": comment_id,
            "market_id": market_id,
            "user_id": user_id,
            "content": content,
            "parent_id": parent_id,
            "created_at": now,
        }
        
        if self._use_memory:
            if not hasattr(self, '_comments'):
                self._comments = {}
            self._comments[comment_id] = comment
            return comment
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO comments (id, market_id, user_id, content, parent_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (comment_id, market_id, user_id, content, parent_id, now))
                conn.commit()
        finally:
            self._put_conn(conn)
        return comment
    
    def get_market_comments(self, market_id: str) -> List[dict]:
        """Get all comments for a market, ordered by creation time."""
        if self._use_memory:
            if not hasattr(self, '_comments'):
                return []
            return sorted(
                [c for c in self._comments.values() if c["market_id"] == market_id],
                key=lambda x: x["created_at"]
            )
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.*, u.username 
                    FROM comments c
                    JOIN users u ON c.user_id = u.id
                    WHERE c.market_id = %s
                    ORDER BY c.created_at ASC
                """, (market_id,))
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        finally:
            self._put_conn(conn)
    
    # --- Resolution Votes ---
    
    def save_resolution_votes(self, market_id: str, votes: List[dict]):
        """Save resolution votes for a market."""
        if self._use_memory:
            if not hasattr(self, '_resolution_votes'):
                self._resolution_votes = {}
            self._resolution_votes[market_id] = votes
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                for vote in votes:
                    vote_id = str(uuid.uuid4())
                    cur.execute("""
                        INSERT INTO resolution_votes (id, market_id, agent_id, vote, reasoning, sources, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        vote_id,
                        market_id,
                        vote["agent_id"],
                        vote["vote"],
                        vote["reasoning"],
                        json.dumps(vote.get("sources", [])),
                        vote["created_at"],
                    ))
                conn.commit()
        finally:
            self._put_conn(conn)
    
    def get_resolution_votes(self, market_id: str) -> List[dict]:
        """Get all resolution votes for a market."""
        if self._use_memory:
            if not hasattr(self, '_resolution_votes'):
                return []
            return self._resolution_votes.get(market_id, [])
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM resolution_votes
                    WHERE market_id = %s
                    ORDER BY created_at ASC
                """, (market_id,))
                rows = cur.fetchall()
                return [
                    {
                        **dict(row),
                        "sources": json.loads(row["sources"]) if row["sources"] else []
                    }
                    for row in rows
                ]
        finally:
            self._put_conn(conn)
    
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
    Authenticate via API key. Returns demo-user for anonymous reads.
    
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
    
    # No auth provided — use demo user for anonymous reads (read-only, zero balance)
    user = db.get_user("demo-user")
    if not user:
        user = db.create_user("demo-user", "demo_user", balance=0.0)
    return user


async def require_auth(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> dict:
    """
    Strict authentication required. No demo-user fallback.
    Use this for all write operations (bets, markets, comments).
    """
    api_key = None
    
    # Try Authorization header first
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    # Then X-API-Key header
    elif x_api_key:
        api_key = x_api_key
    
    if not api_key:
        raise HTTPException(
            status_code=401, 
            detail="Authentication required. Provide API key via 'Authorization: Bearer mm_xxx' or 'X-API-Key: mm_xxx' header."
        )
    
    user = db.get_user_by_api_key(api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return user


# =============================================================================
# App Setup
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: seed read-only demo user for unauthenticated access (zero balance)
    if not db.get_user("demo-user"):
        db.create_user("demo-user", "demo_user", balance=0.0)
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

# CORS: restrict to known origins. Override via CORS_ORIGINS env var (comma-separated).
_default_origins = [
    "https://moltmarkets.com",
    "https://www.moltmarkets.com",
    "http://localhost:3000",
    "http://localhost:5173",
]
_cors_origins = os.getenv("CORS_ORIGINS")
ALLOWED_ORIGINS = [o.strip() for o in _cors_origins.split(",") if o.strip()] if _cors_origins else _default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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
    result = []
    for m in markets:
        # Look up creator username
        creator = db.get_user(m["creator_id"]) if m["creator_id"] else None
        creator_username = creator["username"] if creator else None
        
        result.append(MarketSummary(
            id=m["id"],
            title=m["title"],
            probability=get_cpmm_probability(m["pool"], m["p"]),
            status=m["status"],
            closes_at=m["closes_at"],
            total_volume=m["total_volume"],
            creator_id=m["creator_id"],
            creator_username=creator_username,
        ))
    return result


@app.get("/markets/{market_id}", response_model=MarketDetail)
async def get_market(market_id: str):
    """Get market details including current probability."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    # Look up creator username
    creator = db.get_user(market["creator_id"]) if market["creator_id"] else None
    creator_username = creator["username"] if creator else None
    
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
        creator_username=creator_username,
        pool=market["pool"],
        p=market["p"],
    )


@app.post("/markets", response_model=MarketCreated)
async def create_market(req: MarketCreate, user: dict = Depends(require_auth)):
    """Create a new prediction market."""
    # Require twitter verification before creating markets
    if user.get("status") != "claimed":
        raise HTTPException(
            status_code=403,
            detail="Twitter verification required before creating markets. Visit /claim/{user_id} to link your Twitter account."
        )
    
    if req.closes_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="closes_at must be in the future")
    
    # Rate limit check: 1 market per MARKET_CREATION_COOLDOWN_MINUTES
    last_created = user.get("last_market_created_at")
    if last_created:
        # Handle both datetime objects and strings
        if isinstance(last_created, str):
            last_created = datetime.fromisoformat(last_created.replace('Z', '+00:00'))
        cooldown_end = last_created + timedelta(minutes=MARKET_CREATION_COOLDOWN_MINUTES)
        now = datetime.now(timezone.utc)
        if now < cooldown_end:
            remaining = (cooldown_end - now).total_seconds() / 60
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit: you can create another market in {remaining:.0f} minutes"
            )
    
    market_id = str(uuid.uuid4())
    market = db.create_market(
        market_id=market_id,
        creator_id=user["id"],
        title=req.title,
        description=req.description,
        closes_at=req.closes_at,
        initial_liquidity=req.initial_liquidity,
    )
    
    # Update last market creation timestamp
    db.update_user_last_market_created(user["id"])
    
    # Calculate market duration for guidance
    now = datetime.now(timezone.utc)
    duration_days = (req.closes_at - now).total_seconds() / 86400
    
    # Generate guidance based on duration
    tip = None
    warning = None
    
    if duration_days <= 7:
        tip = "Nice! Short markets (under 7 days) typically see 2-3x more trading activity."
    elif duration_days > 14:
        warning = "Heads up: markets over 2 weeks often see lower engagement. Consider shorter timeframes for more action."
    
    return MarketCreated(
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
        creator_username=user["username"],
        pool=market["pool"],
        p=market["p"],
        tip=tip,
        warning=warning,
    )


@app.post("/markets/{market_id}/resolve", response_model=MarketDetail)
async def resolve_market(market_id: str, req: MarketResolve, user: dict = Depends(require_auth)):
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
    
    # Look up creator username
    creator = db.get_user(market["creator_id"]) if market["creator_id"] else None
    creator_username = creator["username"] if creator else None
    
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
        creator_username=creator_username,
        pool=market["pool"],
        p=market["p"],
    )


# =============================================================================
# Trading Endpoints
# =============================================================================

@app.post("/markets/{market_id}/bet", response_model=BetResponse)
async def place_bet(market_id: str, req: BetRequest, user: dict = Depends(require_auth)):
    """Place a bet on a market."""
    # Require twitter verification before trading
    if user.get("status") != "claimed":
        raise HTTPException(
            status_code=403,
            detail="Twitter verification required before trading. Visit /claim/{user_id} to link your Twitter account."
        )
    
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    if market["status"] != MarketStatus.OPEN:
        raise HTTPException(status_code=400, detail="Market is not open for trading")
    
    if market["closes_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Market has closed")
    
    # Calculate trade fee (2% total)
    trade_fee = req.amount * TRADE_FEE_RATE
    total_cost = req.amount + trade_fee
    
    if user["balance"] < total_cost:
        raise HTTPException(
            status_code=400, 
            detail=f"Insufficient balance. Need {total_cost:.2f} (bet: {req.amount:.2f} + fee: {trade_fee:.2f})"
        )
    
    # Calculate bet using CPMM
    prob_before = get_cpmm_probability(market["pool"], market["p"])
    
    state = CpmmState(pool=market["pool"].copy(), p=market["p"])
    result = calculate_cpmm_purchase(state, req.amount, req.outcome.value)
    
    shares = result["shares"]
    if shares <= 0:
        raise HTTPException(status_code=400, detail="Trade would result in zero or negative shares")
    
    prob_after = get_cpmm_probability(result["new_pool"], result["new_p"])
    
    # Execute trade - deduct bet amount + fee from user
    db.update_user_balance(user["id"], -total_cost)
    
    # Pay creator their share of the fee (1%)
    creator_fee = trade_fee * CREATOR_FEE_SHARE
    if market["creator_id"] != user["id"]:  # Don't pay yourself
        db.update_user_balance(market["creator_id"], creator_fee)
    # Note: remaining fee (1%) is burned - not allocated to anyone
    
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


@app.post("/markets/{market_id}/sell", response_model=SellResponse)
async def sell_shares(market_id: str, req: SellRequest, user: dict = Depends(require_auth)):
    """Sell shares back to the market."""
    # Require twitter verification before trading
    if user.get("status") != "claimed":
        raise HTTPException(
            status_code=403,
            detail="Twitter verification required before trading. Visit /claim/{user_id} to link your Twitter account."
        )
    
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    if market["status"] != MarketStatus.OPEN:
        raise HTTPException(status_code=400, detail="Market is not open for trading")
    
    if market["closes_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Market has closed")
    
    # Get user's position
    position = db.get_position(market_id, user["id"])
    if not position:
        raise HTTPException(status_code=400, detail="You have no position in this market")
    
    # Check if user has enough shares to sell
    if req.outcome == Outcome.YES:
        available_shares = position["yes_shares"]
    else:
        available_shares = position["no_shares"]
    
    if available_shares < req.shares:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient shares. You have {available_shares:.2f} {req.outcome.value} shares"
        )
    
    # Calculate sale using CPMM
    prob_before = get_cpmm_probability(market["pool"], market["p"])
    
    state = CpmmState(pool=market["pool"].copy(), p=market["p"])
    result = calculate_cpmm_sale(state, req.shares, req.outcome.value)
    
    amount_before_fee = result["amount"]
    if amount_before_fee <= 0:
        raise HTTPException(status_code=400, detail="Sale would result in zero or negative payout")
    
    # Apply trade fee (2%)
    trade_fee = amount_before_fee * TRADE_FEE_RATE
    amount_after_fee = amount_before_fee - trade_fee
    
    prob_after = get_cpmm_probability(result["new_pool"], result["new_p"])
    
    # Execute sale
    # Credit user with the payout (minus fee)
    db.update_user_balance(user["id"], amount_after_fee)
    
    # Pay creator their share of the fee (1%)
    creator_fee = trade_fee * CREATOR_FEE_SHARE
    if market["creator_id"] != user["id"]:
        db.update_user_balance(market["creator_id"], creator_fee)
    
    # Update market pool
    db.update_market_pool(market_id, result["new_pool"], result["new_p"], 0)  # No volume added on sell
    
    # Update user's position (reduce shares)
    db.reduce_position(market_id, user["id"], req.outcome, req.shares)
    
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
# Comment Endpoints
# =============================================================================

@app.get("/markets/{market_id}/comments", response_model=MarketComments)
async def get_comments(market_id: str):
    """Get all comments for a market."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    raw_comments = db.get_market_comments(market_id)
    
    # Build comment tree (top-level + replies)
    comments_by_id = {}
    top_level = []
    
    for c in raw_comments:
        comment = Comment(
            id=c["id"],
            market_id=c["market_id"],
            user_id=c["user_id"],
            username=c.get("username", "unknown"),
            content=c["content"],
            created_at=c["created_at"],
            parent_id=c.get("parent_id"),
            replies=[],
        )
        comments_by_id[c["id"]] = comment
        
        if c.get("parent_id") is None:
            top_level.append(comment)
    
    # Attach replies to parents
    for c in raw_comments:
        if c.get("parent_id") and c["parent_id"] in comments_by_id:
            parent = comments_by_id[c["parent_id"]]
            parent.replies.append(comments_by_id[c["id"]])
    
    return MarketComments(
        market_id=market_id,
        comments=top_level,
        total=len(raw_comments),
    )


@app.post("/markets/{market_id}/comments", response_model=Comment)
async def create_comment(market_id: str, req: CommentCreate, user: dict = Depends(require_auth)):
    """Create a comment on a market."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    # Validate parent comment exists if replying
    if req.parent_id:
        parent_comments = db.get_market_comments(market_id)
        if not any(c["id"] == req.parent_id for c in parent_comments):
            raise HTTPException(status_code=400, detail="Parent comment not found")
    
    comment_id = str(uuid.uuid4())
    comment = db.create_comment(
        comment_id=comment_id,
        market_id=market_id,
        user_id=user["id"],
        content=req.content,
        parent_id=req.parent_id,
    )
    
    return Comment(
        id=comment["id"],
        market_id=comment["market_id"],
        user_id=comment["user_id"],
        username=user["username"],
        content=comment["content"],
        created_at=comment["created_at"],
        parent_id=comment.get("parent_id"),
        replies=[],
    )


# =============================================================================
# Resolution Committee Endpoints
# =============================================================================

@app.post("/markets/{market_id}/request-resolution", response_model=ResolutionResult)
async def request_resolution(market_id: str, user: dict = Depends(require_auth)):
    """
    Trigger the 9-agent resolution committee to vote on market resolution.
    
    Only the market creator can request resolution.
    The committee will research and vote, with majority (5+) deciding the outcome.
    """
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    # Only creator can request resolution
    if market["creator_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only market creator can request resolution")
    
    if market["status"] == MarketStatus.RESOLVED:
        raise HTTPException(status_code=400, detail="Market already resolved")
    
    # Get API keys from environment
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    brave_key = os.getenv("BRAVE_API_KEY")
    
    if not anthropic_key or not brave_key:
        raise HTTPException(status_code=500, detail="Resolution service not configured")
    
    # Run the resolution committee
    status, outcome, votes = await resolve_market(
        market_id=market_id,
        market_title=market["title"],
        market_description=market.get("description", ""),
        resolution_criteria=market.get("description", ""),  # Use description as criteria
        anthropic_key=anthropic_key,
        brave_key=brave_key,
    )
    
    # Save votes
    vote_dicts = [
        {
            "agent_id": v.agent_id,
            "vote": v.vote,
            "reasoning": v.reasoning,
            "sources": v.sources,
            "created_at": v.created_at.isoformat(),
        }
        for v in votes
    ]
    db.save_resolution_votes(market_id, vote_dicts)
    
    # If resolved, update the market
    resolved_at = None
    if status == "resolved" and outcome:
        outcome_enum = Outcome.YES if outcome == "YES" else Outcome.NO
        db.resolve_market(market_id, outcome_enum)
        
        # Payout positions
        for pos in db.get_market_positions(market_id):
            winning_shares = pos["yes_shares"] if outcome == "YES" else pos["no_shares"]
            if winning_shares > 0:
                payout = winning_shares
                db.update_user_balance(pos["user_id"], payout)
                db.update_user_profit(pos["user_id"], payout - pos["total_invested"])
        
        resolved_at = datetime.now(timezone.utc)
    
    # Build response
    return ResolutionResult(
        market_id=market_id,
        status=status,
        outcome=Outcome(outcome) if outcome else None,
        votes_yes=sum(1 for v in votes if v.vote == "YES"),
        votes_no=sum(1 for v in votes if v.vote == "NO"),
        total_votes=len(votes),
        votes=[
            ResolutionVote(
                agent_id=v.agent_id,
                vote=Outcome(v.vote),
                reasoning=v.reasoning,
                sources=v.sources,
                created_at=v.created_at,
            )
            for v in votes
        ],
        resolved_at=resolved_at,
    )


@app.get("/markets/{market_id}/resolution-votes", response_model=ResolutionResult)
async def get_resolution_votes(market_id: str):
    """Get the resolution committee votes for a market."""
    market = db.get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    votes = db.get_resolution_votes(market_id)
    
    if not votes:
        raise HTTPException(status_code=404, detail="No resolution votes found for this market")
    
    yes_votes = sum(1 for v in votes if v["vote"] == "YES")
    no_votes = sum(1 for v in votes if v["vote"] == "NO")
    
    # Determine status
    if market["status"] == MarketStatus.RESOLVED:
        status = "resolved"
        outcome = market["resolution"]
    elif yes_votes >= 5:
        status = "resolved"
        outcome = Outcome.YES
    elif no_votes >= 5:
        status = "resolved"
        outcome = Outcome.NO
    else:
        status = "disputed"
        outcome = None
    
    return ResolutionResult(
        market_id=market_id,
        status=status,
        outcome=outcome,
        votes_yes=yes_votes,
        votes_no=no_votes,
        total_votes=len(votes),
        votes=[
            ResolutionVote(
                agent_id=v["agent_id"],
                vote=Outcome(v["vote"]),
                reasoning=v["reasoning"],
                sources=v.get("sources", []),
                created_at=datetime.fromisoformat(v["created_at"]) if isinstance(v["created_at"], str) else v["created_at"],
            )
            for v in votes
        ],
        resolved_at=market.get("resolved_at"),
    )


# =============================================================================
# User Endpoints
# =============================================================================

@app.get("/me", response_model=UserMe)
async def get_me(user: dict = Depends(require_auth)):
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
        twitter_handle=user.get("twitter_handle"),
    )


# =============================================================================
# Agent Registration
# =============================================================================

@app.post("/agents/register", response_model=AgentRegisteredWithClaim)
async def register_agent(req: AgentRegister):
    """
    Register a new agent and get an API key.
    
    The API key is only returned ONCE — save it securely!
    
    Returns a verification_code and claim_url for human-agent linking.
    The human must tweet the verification code to claim the agent.
    """
    # Normalize username to lowercase (case-insensitive uniqueness)
    username = req.username.lower()
    
    # Check if username is taken
    if db.get_user_by_username(username):
        raise HTTPException(status_code=400, detail="Username already taken")
    
    # Generate API key, user ID, and verification code
    api_key = generate_api_key()
    user_id = str(uuid.uuid4())
    verification_code = generate_verification_code()
    
    # Create user with hashed API key
    user = db.create_user(
        user_id=user_id,
        username=username,
        balance=1000.0,  # Starting balance
        api_key_hash=hash_api_key(api_key),
        description=req.description or "",
        status="pending",
        verification_code=verification_code,
    )
    
    # Update display name if provided
    if req.display_name:
        db.update_user_display_name(user_id, req.display_name)
        user["display_name"] = req.display_name
    
    return AgentRegisteredWithClaim(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        api_key=api_key,  # Only time we return the raw key!
        balance=user["balance"],
        created_at=user["created_at"],
        status=AgentStatus.PENDING,
        verification_code=verification_code,
        claim_url=f"/claim/{user_id}",
    )


@app.post("/agents/reset-key", response_model=AgentKeyReset)
async def reset_api_key(user: dict = Depends(require_auth)):
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


# Admin secret for privileged operations (MUST be set via ADMIN_SECRET env var)
ADMIN_SECRET = os.getenv("ADMIN_SECRET")
if not ADMIN_SECRET:
    print("WARNING: ADMIN_SECRET not set — admin endpoints will be disabled")


@app.delete("/admin/users/{username}")
async def admin_delete_user(username: str, x_admin_secret: str = Header(None)):
    """
    Delete a user by username (admin only).
    Requires X-Admin-Secret header.
    """
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin endpoints disabled — ADMIN_SECRET not configured")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")
    
    user = db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    
    # Delete user (we need to add this method)
    db.delete_user(user["id"])
    
    return {"deleted": True, "username": username, "user_id": user["id"]}


@app.get("/claim/{user_id}", response_model=ClaimPageInfo)
async def get_claim_info(user_id: str):
    """
    Get claim page info for an agent (public, no auth required).
    
    Returns the verification code and instructions for claiming.
    """
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    if not user.get("verification_code"):
        raise HTTPException(status_code=400, detail="Agent has no verification code")
    
    if user.get("status") == "claimed":
        raise HTTPException(status_code=400, detail="Agent already claimed")
    
    instructions = (
        f"To claim this agent, post a tweet containing the verification code: {user['verification_code']}\n\n"
        f"Example tweet: 'I'm claiming my MoltMarkets agent! Verification: {user['verification_code']}'\n\n"
        f"After posting, submit the tweet URL to complete the claim."
    )
    
    return ClaimPageInfo(
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        verification_code=user["verification_code"],
        instructions=instructions,
    )


@app.post("/agents/claim", response_model=ClaimResponse)
async def claim_agent(req: ClaimRequest):
    """
    Claim an agent by providing a tweet URL with the verification code.
    
    The tweet must contain the agent's verification code.
    """
    # Get the user/agent
    user = db.get_user(req.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    if user.get("status") == "claimed":
        raise HTTPException(status_code=400, detail="Agent already claimed")
    
    if not user.get("verification_code"):
        raise HTTPException(status_code=400, detail="Agent has no verification code")
    
    # Validate tweet URL format
    if not is_valid_twitter_url(req.tweet_url):
        raise HTTPException(
            status_code=400, 
            detail="Invalid tweet URL. Must be a twitter.com or x.com status URL (e.g., https://twitter.com/user/status/123456)"
        )
    
    # Extract tweet ID from URL
    tweet_id = extract_tweet_id(req.tweet_url)
    if not tweet_id:
        raise HTTPException(
            status_code=400,
            detail="Could not extract tweet ID from URL"
        )
    
    # Fetch the tweet content
    tweet_data = await fetch_tweet(tweet_id)
    
    # Verify the tweet contains the verification code
    tweet_text = tweet_data.get("text", "")
    if not verify_tweet_contains_code(tweet_text, user["verification_code"]):
        raise HTTPException(
            status_code=400,
            detail=f"Verification failed: tweet does not contain the code '{user['verification_code']}'. "
                   f"Please ensure your tweet includes the exact verification code."
        )
    
    # Extract twitter handle from the tweet URL
    twitter_handle = extract_twitter_handle(req.tweet_url)
    
    # Mark agent as claimed and store twitter handle
    db.update_user_status(req.user_id, "claimed")
    if twitter_handle:
        db.update_user_twitter_handle(req.user_id, twitter_handle)
    
    return ClaimResponse(
        success=True,
        message=f"Agent '{user['username']}' successfully claimed!",
        user_id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        status=AgentStatus.CLAIMED,
    )


# =============================================================================
# Leaderboard
# =============================================================================

@app.get("/leaderboard", response_model=List[LeaderboardEntry])
async def get_leaderboard():
    """Get leaderboard sorted by profit (only shows claimed/verified agents)."""
    entries = []
    all_users = db.users
    all_bets = db.bets
    all_markets = db.markets
    
    for user in all_users.values():
        # Only include claimed/verified agents in leaderboard
        if user.get("status") != "claimed":
            continue
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
