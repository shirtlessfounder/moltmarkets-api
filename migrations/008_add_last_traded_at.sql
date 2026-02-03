-- Add last_traded_at column for bump feed sorting (issue #160)
-- Markets sorted by most recent trade for "Bump" sort option

ALTER TABLE markets ADD COLUMN last_traded_at TIMESTAMP WITH TIME ZONE;

-- Initialize to created_at for existing markets
UPDATE markets SET last_traded_at = created_at;

-- Index for efficient DESC sorting
CREATE INDEX idx_markets_last_traded_at ON markets(last_traded_at DESC);
