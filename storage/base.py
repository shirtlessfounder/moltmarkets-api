"""
MoltMarkets Storage — base class with pool management and schema init.

Contains Storage.__init__, connection pool, _init_db, _save, and constants.
"""

import json
import os
import warnings
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse, unquote

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from models import MarketStatus, Outcome


class BaseStorage:
    """
    PostgreSQL storage backend — base infrastructure.

    Uses DATABASE_URL environment variable (Railway provides this automatically).
    Falls back to in-memory storage if DATABASE_URL is not set.
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
                        p DECIMAL(10, 8) DEFAULT 0.5,
                        version INTEGER NOT NULL DEFAULT 1
                    )
                """)

                # Add version column if missing (existing databases — see migration 005)
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='markets' AND column_name='version') THEN
                            ALTER TABLE markets ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
                        END IF;
                    END $$;
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

                # Committee votes table (issue #107)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS committee_votes (
                        id VARCHAR(255) PRIMARY KEY,
                        market_id VARCHAR(255) REFERENCES markets(id) NOT NULL,
                        agent_id VARCHAR(255) REFERENCES users(id) NOT NULL,
                        outcome VARCHAR(10) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)

                # Committee columns on markets
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='markets' AND column_name='committee') THEN
                            ALTER TABLE markets ADD COLUMN committee TEXT;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='markets' AND column_name='resolution_deadline') THEN
                            ALTER TABLE markets ADD COLUMN resolution_deadline TIMESTAMP WITH TIME ZONE;
                        END IF;
                    END $$;
                """)

                # Unique constraint for committee votes
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'uq_committee_votes_market_agent'
                        ) THEN
                            ALTER TABLE committee_votes
                                ADD CONSTRAINT uq_committee_votes_market_agent UNIQUE (market_id, agent_id);
                        END IF;
                    END $$;
                """)

                # Create indexes for common queries
                cur.execute("CREATE INDEX IF NOT EXISTS idx_committee_votes_market ON committee_votes(market_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_committee_votes_agent ON committee_votes(agent_id)")
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

    # --- Utility ---

    def _save(self):
        """No-op for PostgreSQL (kept for compatibility)."""
        pass
