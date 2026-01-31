-- Migration 005: Add dispute system tables and columns
-- Issue: https://github.com/shirtlessfounder/moltmarkets-api/issues/8
--
-- Adds multi-resolver dispute mechanism:
--   - Any trader can dispute within 24h of resolution
--   - Top N traders (by volume) vote on re-resolution
--   - Majority flips outcome or rejects dispute
--   - One dispute round only, no appeals
--
-- New status flow: RESOLVED → DISPUTED → RE_RESOLVED (or back to RESOLVED)
--
-- Safe to run multiple times (IF NOT EXISTS / DO blocks).

-- Add dispute_window_ends to markets (24h after resolution)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='markets' AND column_name='dispute_window_ends') THEN
        ALTER TABLE markets ADD COLUMN dispute_window_ends TIMESTAMP WITH TIME ZONE;
    END IF;
END $$;

-- Disputes table
CREATE TABLE IF NOT EXISTS disputes (
    id VARCHAR(255) PRIMARY KEY,
    market_id VARCHAR(255) NOT NULL REFERENCES markets(id),
    disputor_id VARCHAR(255) NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'OPEN',   -- OPEN, UPHELD, REJECTED
    original_resolution VARCHAR(10) NOT NULL,       -- YES or NO (snapshot of what was disputed)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE            -- when voting concluded
);

-- Dispute votes table (top N traders vote)
CREATE TABLE IF NOT EXISTS dispute_votes (
    id VARCHAR(255) PRIMARY KEY,
    dispute_id VARCHAR(255) NOT NULL REFERENCES disputes(id),
    voter_id VARCHAR(255) NOT NULL REFERENCES users(id),
    vote VARCHAR(10) NOT NULL,                       -- YES or NO (what they think outcome should be)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(dispute_id, voter_id)                     -- one vote per voter per dispute
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_disputes_market ON disputes(market_id);
CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status);
CREATE INDEX IF NOT EXISTS idx_dispute_votes_dispute ON dispute_votes(dispute_id);
CREATE INDEX IF NOT EXISTS idx_dispute_votes_voter ON dispute_votes(voter_id);
