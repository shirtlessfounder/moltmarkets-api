"""
MoltMarkets Storage — PostgreSQL storage backend.

Extracted from api.py as part of modular refactoring (Phase 2 of 3).
Contains the Storage class with all database operations.
"""

import hashlib
import json
import os
import uuid
import warnings
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse, unquote

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from models import MarketStatus, Outcome


def hash_api_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


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
    
    def close_pool(self):
        """Close all connections in the pool.
        
        Called during application shutdown to release database connections
        cleanly instead of leaving them to be garbage-collected.
        """
        if self._pool is not None:
            try:
                self._pool.closeall()
                print("Connection pool closed")
            except Exception as e:
                print(f"Warning: error closing connection pool: {e}")

    def _init_pool(self):
        """Initialize a thread-safe connection pool.
        
        Uses ThreadedConnectionPool instead of SimpleConnectionPool because
        FastAPI handles concurrent requests across multiple threads — 
        SimpleConnectionPool is NOT thread-safe and can hand the same
        connection to two threads simultaneously, causing data corruption
        and 'connection already in use' errors under load.
        
        Pool size is configurable via environment variables:
          DB_POOL_MIN  – minimum connections kept open (default: 2)
          DB_POOL_MAX  – maximum connections allowed   (default: 10)
        
        Configures TCP keepalives so Railway's Postgres proxy doesn't silently
        drop idle connections, and sets a statement timeout as a safety net
        against queries that hang forever.
        """
        parsed = urlparse(self.database_url)
        min_conn = int(os.getenv("DB_POOL_MIN", "2"))
        max_conn = int(os.getenv("DB_POOL_MAX", "10"))
        self._pool = pool.ThreadedConnectionPool(
            minconn=min_conn,
            maxconn=max_conn,
            host=parsed.hostname,
            port=parsed.port or 5432,
            user=parsed.username,
            password=unquote(parsed.password) if parsed.password else None,
            dbname=parsed.path.lstrip('/'),
            cursor_factory=RealDictCursor,
            connect_timeout=10,          # Don't wait forever to connect
            keepalives=1,                # Enable TCP keepalives
            keepalives_idle=30,          # Send keepalive after 30s idle
            keepalives_interval=10,      # Retry keepalive every 10s
            keepalives_count=3,          # Give up after 3 missed keepalives
            options='-c statement_timeout=30000',  # 30s query timeout
        )
        print(f"ThreadedConnectionPool initialized (min={min_conn}, max={max_conn}, keepalives=on, statement_timeout=30s)")
    
    def _get_conn(self):
        """Get a database connection from the pool.
        
        Validates the connection is alive before returning it.  If the
        connection is dead (e.g., closed by Railway's proxy), it is discarded
        and a fresh one is created.
        """
        conn = self._pool.getconn()
        try:
            # Quick health check — if the connection was silently closed
            # by the proxy/firewall, this will raise an exception.
            conn.isolation_level  # Triggers a round-trip if connection is bad
            if conn.closed:
                raise psycopg2.OperationalError("connection is closed")
            # Run a trivial query to truly verify the connection is live
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            # Connection is dead — discard it and get a fresh one
            try:
                self._pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = self._pool.getconn()
        return conn
    
    def _put_conn(self, conn):
        """Return a connection to the pool.
        
        Always rolls back any uncommitted transaction first so the next
        user of this connection doesn't inherit a broken transaction state
        (InFailedSqlTransaction).
        """
        try:
            conn.rollback()  # Clean slate — no stale transaction state
        except Exception:
            # Connection is broken beyond repair — close instead of returning
            try:
                self._pool.putconn(conn, close=True)
            except Exception:
                pass
            return
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
                
                # Chat messages table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id VARCHAR(255) REFERENCES users(id),
                        username TEXT NOT NULL,
                        text TEXT NOT NULL,
                        channel VARCHAR(20) DEFAULT 'agents',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)
                
                # Add channel column if it doesn't exist (for existing databases)
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='chat_messages' AND column_name='channel') THEN
                            ALTER TABLE chat_messages ADD COLUMN channel VARCHAR(20) DEFAULT 'agents';
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='user_type') THEN
                            ALTER TABLE users ADD COLUMN user_type VARCHAR(20) DEFAULT 'agent';
                        END IF;
                    END $$;
                """)
                
                # Create indexes for common queries
                cur.execute("CREATE INDEX IF NOT EXISTS idx_resolution_votes_market ON resolution_votes(market_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_comments_market ON comments(market_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_created ON chat_messages(created_at DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_channel_created ON chat_messages(channel, created_at DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_market ON bets(market_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key_hash)")
                
                # Additional indexes for common query patterns (see #50)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_creator ON markets(creator_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_closes_at ON markets(closes_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_users_lower_username ON users(LOWER(username))")
                
                # Composite index for the /markets list query (status + created_at DESC)
                # Covers the common ORDER BY created_at DESC with optional WHERE status filter
                cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_status_created ON markets(status, created_at DESC)")
                # Index on created_at alone for the default ORDER BY
                cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_created_at ON markets(created_at DESC)")
                
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
            "user_type": row.get("user_type", "agent"),
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
                    status: str = "pending", verification_code: str = None,
                    user_type: str = "agent") -> dict:
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
                "user_type": user_type,
            }
            self._users[user_id] = user
            return user
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, username, display_name, description, balance, api_key_hash, status, verification_code, user_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (user_id, username, username, description, balance, api_key_hash, status, verification_code, user_type))
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
            self._put_conn(conn)
    
    def update_user_api_key(self, user_id: str, key_hash: str):
        """Update a user's API key hash (for key regeneration)."""
        if self._use_memory:
            if user_id in self._users:
                self._users[user_id]["api_key_hash"] = key_hash
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET api_key_hash = %s WHERE id = %s", (key_hash, user_id))
                conn.commit()
        finally:
            self._put_conn(conn)
    
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
    
    def count_users(self) -> int:
        """Count total users without loading all rows.

        O(1) via COUNT(*) instead of O(N) full-table load.
        Replaces ``len(db.users)`` in hot paths (health, startup).

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if self._use_memory:
            return len(self._users)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM users")
                return cur.fetchone()["cnt"]
        finally:
            self._put_conn(conn)

    @property
    def users(self) -> Dict[str, dict]:
        """Get all users as dict.

        .. deprecated::
            Loads the **entire** users table into memory — O(N) on every call.
            Use targeted methods instead:
            - ``count_users()`` for counts
            - ``get_user(id)`` for single lookups
            - ``get_user_by_username(name)`` for name lookups
            - ``get_leaderboard_data()`` for leaderboard

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        warnings.warn(
            "db.users loads the entire users table into memory. "
            "Use count_users(), get_user(), or get_leaderboard_data() instead. "
            "See #54: https://github.com/shirtlessfounder/moltmarkets-api/issues/54",
            DeprecationWarning,
            stacklevel=2,
        )
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
    
    def list_markets_with_creators(self) -> List[dict]:
        """List all markets with creator usernames in a single JOIN query.
        
        Eliminates N+1: previously list_markets + N × get_user calls.
        Now: 1 query total.
        """
        if self._use_memory:
            results = []
            for m in self._markets.values():
                market = dict(m)
                creator = self._users.get(m["creator_id"])
                market["creator_username"] = creator["username"] if creator else None
                results.append(market)
            return results
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.*, u.username AS creator_username
                    FROM markets m
                    LEFT JOIN users u ON m.creator_id = u.id
                    ORDER BY m.created_at DESC
                """)
                rows = cur.fetchall()
                results = []
                for row in rows:
                    market = self._row_to_market(row)
                    market["creator_username"] = row.get("creator_username")
                    results.append(market)
                return results
        finally:
            self._put_conn(conn)
    
    def count_markets(self) -> int:
        """Count total markets without loading all rows.

        O(1) via COUNT(*) instead of O(N) full-table load.
        Replaces ``len(db.markets)`` / ``len(db.list_markets())`` in hot paths.

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if self._use_memory:
            return len(self._markets)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM markets")
                return cur.fetchone()["cnt"]
        finally:
            self._put_conn(conn)

    def get_markets_by_ids(self, market_ids: set) -> Dict[str, dict]:
        """Get specific markets by their IDs in a single query.

        Returns a dict of {market_id: market_dict} for only the requested IDs.
        O(K) where K = len(market_ids), instead of O(N) for the full table.

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if not market_ids:
            return {}

        if self._use_memory:
            return {mid: self._markets[mid] for mid in market_ids if mid in self._markets}

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM markets WHERE id = ANY(%s)",
                    (list(market_ids),),
                )
                rows = cur.fetchall()
                return {row["id"]: self._row_to_market(row) for row in rows}
        finally:
            self._put_conn(conn)

    def get_markets_by_creator(self, creator_id: str) -> List[dict]:
        """Get all markets created by a specific user.

        Uses idx_markets_creator index for O(1) lookup.

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if self._use_memory:
            return [m for m in self._markets.values() if m.get("creator_id") == creator_id]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM markets WHERE creator_id = %s ORDER BY created_at DESC",
                    (creator_id,),
                )
                rows = cur.fetchall()
                return [self._row_to_market(row) for row in rows]
        finally:
            self._put_conn(conn)

    def get_bets_on_markets(self, market_ids: set) -> List[dict]:
        """Get all bets placed on specific markets.

        Used by reputation creation-score to count bets on a user's
        created markets without loading the entire bets table.

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        if not market_ids:
            return []

        if self._use_memory:
            return [b for b in self._bets.values() if b.get("market_id") in market_ids]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM bets WHERE market_id = ANY(%s) ORDER BY created_at",
                    (list(market_ids),),
                )
                rows = cur.fetchall()
                return [self._row_to_bet(row) for row in rows]
        finally:
            self._put_conn(conn)

    @property
    def markets(self) -> Dict[str, dict]:
        """Get all markets as dict.

        .. deprecated::
            Loads the **entire** markets table into memory — O(N) on every call.
            Use targeted methods instead:
            - ``count_markets()`` for counts
            - ``get_market(id)`` for single lookups
            - ``list_markets()`` for ordered listing
            - ``get_markets_by_ids(ids)`` for batch lookups
            - ``get_markets_by_creator(uid)`` for creator filtering

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        warnings.warn(
            "db.markets loads the entire markets table into memory. "
            "Use count_markets(), get_market(), get_markets_by_ids(), or "
            "get_markets_by_creator() instead. "
            "See #54: https://github.com/shirtlessfounder/moltmarkets-api/issues/54",
            DeprecationWarning,
            stacklevel=2,
        )
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
    
    def update_market_status(self, market_id: str, status: MarketStatus):
        """Update a market's status (e.g. OPEN → RESOLVING)."""
        if self._use_memory:
            market = self._markets[market_id]
            market["status"] = status
            return
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE markets SET status = %s WHERE id = %s",
                    (status.value, market_id),
                )
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
    
    def get_bets_for_market_with_users(self, market_id: str) -> List[dict]:
        """Get all bets for a market with user info in a single JOIN query.
        
        Eliminates N+1: previously get_bets_for_market + N × get_user calls.
        Now: 1 query total.
        """
        if self._use_memory:
            results = []
            for b in self._bets.values():
                if b["market_id"] == market_id:
                    bet = dict(b)
                    user = self._users.get(b["user_id"])
                    bet["username"] = user["username"] if user else "unknown"
                    results.append(bet)
            return results
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT b.*, u.username
                    FROM bets b
                    LEFT JOIN users u ON b.user_id = u.id
                    WHERE b.market_id = %s
                    ORDER BY b.created_at
                """, (market_id,))
                rows = cur.fetchall()
                results = []
                for row in rows:
                    bet = self._row_to_bet(row)
                    bet["username"] = row.get("username") or "unknown"
                    results.append(bet)
                return results
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
        """Get all bets as dict.

        .. deprecated::
            Loads the **entire** bets table into memory — O(N) on every call.
            Use targeted methods instead:
            - ``get_bets_for_market(market_id)`` for market bets
            - ``get_bets_for_user(user_id)`` for user bets
            - ``get_bets_on_markets(market_ids)`` for batch market bets

        See: https://github.com/shirtlessfounder/moltmarkets-api/issues/54
        """
        warnings.warn(
            "db.bets loads the entire bets table into memory. "
            "Use get_bets_for_market(), get_bets_for_user(), or "
            "get_bets_on_markets() instead. "
            "See #54: https://github.com/shirtlessfounder/moltmarkets-api/issues/54",
            DeprecationWarning,
            stacklevel=2,
        )
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
    
    def get_leaderboard_data(self) -> List[dict]:
        """Get leaderboard data using aggregate SQL query.
        
        Eliminates N+1: previously loaded ALL users + ALL bets + ALL markets
        into memory, then did O(users × bets) Python iteration.
        Now: 1 query with CTEs for volume and win rate aggregation.
        """
        if self._use_memory:
            # Fallback: in-memory calculation (same logic, just organized)
            entries = []
            for user in self._users.values():
                if user.get("status") != "claimed":
                    continue
                total_volume = sum(
                    b["amount"] for b in self._bets.values()
                    if b["user_id"] == user["id"]
                )
                user_bets = [b for b in self._bets.values() if b["user_id"] == user["id"]]
                wins = 0
                resolved_bets = 0
                for bet in user_bets:
                    market = self._markets.get(bet["market_id"])
                    if market and market["status"] == MarketStatus.RESOLVED:
                        resolved_bets += 1
                        if market["resolution"] == bet["outcome"]:
                            wins += 1
                win_rate = wins / resolved_bets if resolved_bets > 0 else 0.5
                entries.append({
                    "user_id": user["id"],
                    "username": user["username"],
                    "pnl": user["profit_all_time"],
                    "total_volume": total_volume,
                    "win_rate": win_rate,
                })
            entries.sort(key=lambda x: x["pnl"], reverse=True)
            return entries
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH user_volumes AS (
                        SELECT user_id, COALESCE(SUM(amount), 0) AS total_volume
                        FROM bets
                        GROUP BY user_id
                    ),
                    user_win_rates AS (
                        SELECT 
                            b.user_id,
                            COUNT(*) AS resolved_bets,
                            SUM(CASE WHEN UPPER(m.resolution) = UPPER(b.outcome) THEN 1 ELSE 0 END) AS wins
                        FROM bets b
                        JOIN markets m ON b.market_id = m.id
                        WHERE UPPER(m.status) = 'RESOLVED'
                        GROUP BY b.user_id
                    )
                    SELECT 
                        u.id AS user_id,
                        u.username,
                        u.profit_all_time AS pnl,
                        COALESCE(v.total_volume, 0) AS total_volume,
                        CASE 
                            WHEN COALESCE(w.resolved_bets, 0) > 0 
                            THEN w.wins::float / w.resolved_bets 
                            ELSE 0.5 
                        END AS win_rate
                    FROM users u
                    LEFT JOIN user_volumes v ON u.id = v.user_id
                    LEFT JOIN user_win_rates w ON u.id = w.user_id
                    WHERE u.status = 'claimed'
                    ORDER BY u.profit_all_time DESC
                """)
                rows = cur.fetchall()
                return [
                    {
                        "user_id": row["user_id"],
                        "username": row["username"],
                        "pnl": float(row["pnl"]),
                        "total_volume": float(row["total_volume"]),
                        "win_rate": float(row["win_rate"]),
                    }
                    for row in rows
                ]
        finally:
            self._put_conn(conn)
    
    def get_reputation_data(self, user_id: str) -> dict:
        """Get all data needed for reputation calculation in minimal queries.
        
        Eliminates N+1: previously looped through ALL markets to fetch
        resolution votes and comments individually (2N extra queries).
        Now: 3 targeted queries instead of 2N+3.
        """
        if self._use_memory:
            # Fallback for in-memory storage
            all_resolution_votes = []
            if hasattr(self, '_resolution_votes'):
                for votes in self._resolution_votes.values():
                    all_resolution_votes.extend(votes)
            comments_count = 0
            if hasattr(self, '_comments'):
                comments_count = sum(1 for c in self._comments.values() if c.get("user_id") == user_id)
            return {
                "resolution_votes": all_resolution_votes,
                "comments_count": comments_count,
            }
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Single query for all resolution votes (not per-market)
                cur.execute("""
                    SELECT * FROM resolution_votes
                    ORDER BY created_at ASC
                """)
                rows = cur.fetchall()
                all_resolution_votes = [
                    {
                        **dict(row),
                        "sources": json.loads(row["sources"]) if row["sources"] else []
                    }
                    for row in rows
                ]
                
                # Single query for user's comment count
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM comments WHERE user_id = %s
                """, (user_id,))
                comments_count = cur.fetchone()["cnt"]
                
                return {
                    "resolution_votes": all_resolution_votes,
                    "comments_count": comments_count,
                }
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
    
    def get_user_positions(self, user_id: str) -> List[dict]:
        """Get all positions for a user across all markets."""
        if self._use_memory:
            positions = []
            for market_id, market_positions in self._positions.items():
                if user_id in market_positions:
                    positions.append(market_positions[user_id])
            return positions

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM positions WHERE user_id = %s",
                    (user_id,),
                )
                rows = cur.fetchall()
                return [self._row_to_position(row) for row in rows]
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
    
    # --- Chat Messages ---
    
    def create_chat_message(self, user_id: str, username: str, text: str, channel: str = "agents") -> dict:
        """Create a new chat message."""
        now = datetime.now(timezone.utc)
        msg_id = str(uuid.uuid4())
        message = {
            "id": msg_id,
            "user_id": user_id,
            "username": username,
            "text": text,
            "channel": channel,
            "created_at": now,
        }
        
        if self._use_memory:
            if not hasattr(self, '_chat_messages'):
                self._chat_messages = []
            self._chat_messages.append(message)
            return message
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO chat_messages (id, user_id, username, text, channel, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, user_id, username, text, channel, created_at
                """, (msg_id, user_id, username, text, channel, now))
                row = cur.fetchone()
                conn.commit()
                return dict(row)
        finally:
            self._put_conn(conn)
    
    def get_chat_messages(self, limit: int = 50, since: Optional[datetime] = None, channel: str = "agents") -> List[dict]:
        """Get recent chat messages, optionally filtering by since timestamp and channel."""
        if self._use_memory:
            if not hasattr(self, '_chat_messages'):
                return []
            msgs = [m for m in self._chat_messages if m.get("channel", "agents") == channel]
            if since:
                msgs = [m for m in msgs if m["created_at"] > since]
            msgs = sorted(msgs, key=lambda x: x["created_at"], reverse=True)
            return msgs[:limit]
        
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if since:
                    cur.execute("""
                        SELECT id, username, text, channel, created_at
                        FROM chat_messages
                        WHERE channel = %s AND created_at > %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (channel, since, limit))
                else:
                    cur.execute("""
                        SELECT id, username, text, channel, created_at
                        FROM chat_messages
                        WHERE channel = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (channel, limit))
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        finally:
            self._put_conn(conn)
    
    # --- Utility ---
    
    def _save(self):
        """No-op for PostgreSQL (kept for compatibility)."""
        pass


