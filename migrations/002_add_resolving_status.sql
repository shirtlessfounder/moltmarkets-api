-- Migration: Add RESOLVING market status
-- Issue: https://github.com/shirtlessfounder/moltmarkets-api/issues/36
--
-- The markets.status column is VARCHAR(50), so no ALTER TYPE is needed.
-- This migration back-fills any OPEN markets whose closes_at has already
-- passed to the new RESOLVING status.
--
-- Safe to run multiple times (idempotent).

-- Back-fill: OPEN markets past their close time → RESOLVING
UPDATE markets
SET    status = 'RESOLVING'
WHERE  status = 'OPEN'
  AND  closes_at <= NOW();

-- Verify (informational)
-- SELECT status, count(*) FROM markets GROUP BY status;
