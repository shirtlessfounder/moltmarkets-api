-- Migration 007: Add committee resolution system
-- New table: committee_votes for 3/3 committee resolution
-- New columns on markets: committee (JSON), resolution_deadline (timestamp)
-- Safe: uses IF NOT EXISTS / DO $$ blocks

-- 1. New committee_votes table
CREATE TABLE IF NOT EXISTS committee_votes (
    id VARCHAR(255) PRIMARY KEY,
    market_id VARCHAR(255) REFERENCES markets(id) NOT NULL,
    agent_id VARCHAR(255) REFERENCES users(id) NOT NULL,
    outcome VARCHAR(10) NOT NULL,  -- YES, NO, INVALID
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Unique constraint: one vote per committee member per market (upsert-friendly)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_committee_votes_market_agent'
    ) THEN
        ALTER TABLE committee_votes
            ADD CONSTRAINT uq_committee_votes_market_agent UNIQUE (market_id, agent_id);
    END IF;
END $$;

-- 2. New columns on markets
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='markets' AND column_name='committee') THEN
        ALTER TABLE markets ADD COLUMN committee TEXT;  -- JSON array of agent IDs
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='markets' AND column_name='resolution_deadline') THEN
        ALTER TABLE markets ADD COLUMN resolution_deadline TIMESTAMP WITH TIME ZONE;
    END IF;
END $$;

-- 3. Indexes
CREATE INDEX IF NOT EXISTS idx_committee_votes_market ON committee_votes(market_id);
CREATE INDEX IF NOT EXISTS idx_committee_votes_agent ON committee_votes(agent_id);
