-- migrations/011_bounty_disputes.sql
-- Bounty dispute resolution (issue #180 phase 2)

ALTER TABLE bounties ADD COLUMN IF NOT EXISTS disputed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS bounty_votes (
    bounty_id UUID NOT NULL REFERENCES bounties(id),
    voter_id VARCHAR(255) NOT NULL REFERENCES users(id),
    vote VARCHAR(20) NOT NULL CHECK (vote IN ('creator', 'claimant')),
    reason TEXT DEFAULT '',
    voted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bounty_id, voter_id)
);

CREATE INDEX IF NOT EXISTS idx_bounty_votes_bounty ON bounty_votes(bounty_id);
