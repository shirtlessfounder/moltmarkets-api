-- Migration 006: Add dispute system tables
-- Issue: https://github.com/shirtlessfounder/moltmarkets-api/issues/8
--
-- Adds two tables:
--   disputes       — records filed by bettors against a market resolution
--   dispute_votes  — votes cast by community/committee members on disputes
--
-- Dispute lifecycle: OPEN → UNDER_REVIEW → UPHELD | OVERTURNED
-- Only users who placed a bet on the market may file a dispute.
-- One active dispute per market at a time.
--
-- Safe to run multiple times (IF NOT EXISTS).

-- Disputes table
CREATE TABLE IF NOT EXISTS disputes (
    id VARCHAR(255) PRIMARY KEY,
    market_id VARCHAR(255) REFERENCES markets(id) NOT NULL,
    disputer_id VARCHAR(255) REFERENCES users(id) NOT NULL,
    reason TEXT NOT NULL,
    evidence TEXT DEFAULT '',
    original_resolution VARCHAR(50) NOT NULL,
    new_resolution VARCHAR(50),
    status VARCHAR(50) DEFAULT 'OPEN',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE
);

-- Dispute votes table (one vote per user per dispute)
CREATE TABLE IF NOT EXISTS dispute_votes (
    id VARCHAR(255) PRIMARY KEY,
    dispute_id VARCHAR(255) REFERENCES disputes(id) NOT NULL,
    voter_id VARCHAR(255) REFERENCES users(id) NOT NULL,
    vote VARCHAR(20) NOT NULL,       -- 'UPHOLD' or 'OVERTURN'
    reasoning TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(dispute_id, voter_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_disputes_market ON disputes(market_id);
CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status);
CREATE INDEX IF NOT EXISTS idx_dispute_votes_dispute ON dispute_votes(dispute_id);
