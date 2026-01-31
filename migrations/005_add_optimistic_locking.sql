-- Migration 005: Add optimistic locking version column to markets table
-- Ref: Issue #74 — prevent race conditions on concurrent trades
--
-- Every UPDATE to pool/status now increments `version` and includes
-- `WHERE version = <expected>` so concurrent writers detect conflicts
-- and can retry with fresh state.

ALTER TABLE markets ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
