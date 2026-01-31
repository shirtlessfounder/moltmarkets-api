-- Migration 004: Add missing database indexes for query performance
-- Issue: https://github.com/shirtlessfounder/moltmarkets-api/issues/50
--
-- Adds indexes on frequently queried columns that were missing.
-- Existing indexes (already created in _init_db):
--   idx_bets_market, idx_bets_user, idx_markets_status,
--   idx_users_api_key, idx_chat_messages_created,
--   idx_chat_messages_channel_created, idx_resolution_votes_market,
--   idx_comments_market
--
-- Safe to run multiple times (IF NOT EXISTS).

-- Markets: creator_id (used in creator lookups, market filtering)
CREATE INDEX IF NOT EXISTS idx_markets_creator ON markets(creator_id);

-- Markets: closes_at (used in auto-transition OPEN → RESOLVING)
CREATE INDEX IF NOT EXISTS idx_markets_closes_at ON markets(closes_at);

-- Markets: created_at DESC (used in ORDER BY for listing)
CREATE INDEX IF NOT EXISTS idx_markets_created_at ON markets(created_at DESC);

-- Bets: created_at DESC (used in ORDER BY for history/listing)
CREATE INDEX IF NOT EXISTS idx_bets_created_at ON bets(created_at DESC);

-- Users: username lowercase (used in case-insensitive lookups via LOWER())
CREATE INDEX IF NOT EXISTS idx_users_username ON users(LOWER(username));
