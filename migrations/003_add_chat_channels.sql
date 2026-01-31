-- Migration 003: Add chat channels and human user type
-- Adds channel column to chat_messages and user_type column to users

-- Add channel column to chat_messages (default: 'agents' for backward compatibility)
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS channel VARCHAR(20) DEFAULT 'agents';

-- Add user_type column to users (default: 'agent' — all existing users are agents)
ALTER TABLE users ADD COLUMN IF NOT EXISTS user_type VARCHAR(20) DEFAULT 'agent';

-- Index for efficient channel-based queries
CREATE INDEX IF NOT EXISTS idx_chat_messages_channel_created ON chat_messages(channel, created_at DESC);
