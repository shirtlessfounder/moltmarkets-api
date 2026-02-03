-- Transactions ledger (issue #173)
-- Records every balance-changing event for audit trail and user history.

CREATE TABLE IF NOT EXISTS transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    amount FLOAT NOT NULL,
    type VARCHAR(30) NOT NULL,
    market_id UUID REFERENCES markets(id),
    related_user_id UUID,
    balance_after FLOAT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_market ON transactions(market_id);
CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(type);
