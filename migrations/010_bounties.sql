-- Bounty escrow system (issue #180)
-- Stores escrow bounties: lock ŧ on creation, release on completion.

CREATE TABLE IF NOT EXISTS bounties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    creator_id VARCHAR(255) NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    amount FLOAT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    claimant_id VARCHAR(255) REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bounties_status ON bounties(status);
CREATE INDEX IF NOT EXISTS idx_bounties_creator ON bounties(creator_id);
CREATE INDEX IF NOT EXISTS idx_bounties_claimant ON bounties(claimant_id);
CREATE INDEX IF NOT EXISTS idx_bounties_created ON bounties(created_at DESC);
