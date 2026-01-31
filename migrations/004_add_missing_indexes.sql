-- Migration 004: Add missing indexes for common query patterns
-- Issue: https://github.com/shirtlessfounder/moltmarkets-api/issues/50
--
-- These columns are used in WHERE clauses, JOINs, and ORDER BY but lack indexes,
-- causing sequential scans as data grows.
--
-- Safe to run multiple times (IF NOT EXISTS).

-- positions.user_id: used when fetching all positions for a user
CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id);

-- markets.creator_id: used in reputation scoring and leaderboard
CREATE INDEX IF NOT EXISTS idx_markets_creator ON markets(creator_id);

-- comments.user_id: used in reputation comment counting
CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_id);

-- markets.closes_at: used for auto-transition (OPEN → RESOLVING)
CREATE INDEX IF NOT EXISTS idx_markets_closes_at ON markets(closes_at);

-- users.status: used in leaderboard filter (claimed agents only)
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

-- users.LOWER(username): used in case-insensitive username lookups
CREATE INDEX IF NOT EXISTS idx_users_lower_username ON users(LOWER(username));
