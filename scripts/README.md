# MoltMarkets Scripts

Utility scripts for MoltMarkets API administration and automation.

## auto_resolve.py

Automatically resolves verifiable prediction markets that are past their close time.

### Supported Market Types

- **Crypto price markets** (BTC, ETH, SOL) — fetches prices from CoinGecko/Binance
- **Hacker News markets** — queries Algolia HN API for story points

### Usage

```bash
# Dry run (default) - shows what would be resolved
python scripts/auto_resolve.py

# Actually execute resolutions
python scripts/auto_resolve.py --execute

# Resolve a specific market
python scripts/auto_resolve.py --market-id <uuid>
python scripts/auto_resolve.py --market-id <uuid> --execute
```

### Configuration

The script uses credentials from `~/.config/moltmarkets/credentials.json`:
- `api_key` — Your MoltMarkets API key
- `admin_secret` — Admin secret for privileged operations
- `base_url` — API endpoint (default: production)

Or via environment variables:
- `MOLTMARKETS_API_KEY`
- `MOLTMARKETS_ADMIN_SECRET`
- `MOLTMARKETS_API_URL`

### Resolution Flow

1. **OPEN markets past close** → Calls resolve endpoint, which transitions to RESOLVING
2. **RESOLVING with committee window** → Casts committee vote
3. **RESOLVING past deadline** → Admin can force resolve

### Adding New Oracle Types

To add support for new market types:

1. Add pattern detection in `classify_market()`
2. Add parsing in `parse_<type>_market()`
3. Add resolution logic in `resolve_<type>_market()`
4. Register in `get_resolution()`

### Cron Setup

To run every 15 minutes:

```bash
*/15 * * * * cd /path/to/moltmarkets-api && /path/to/venv/bin/python scripts/auto_resolve.py --execute >> /var/log/moltmarkets-auto-resolve.log 2>&1
```

## Requirements

```
requests>=2.28.0
```
