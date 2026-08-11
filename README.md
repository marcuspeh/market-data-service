# market-data-service

A small FastAPI service that:

- **Proxies ETF constituents** (SPY, QQQ, IWM) from multiple data providers
  (SSGA, iShares) behind a single, provider-agnostic REST endpoint.
- **Caches market data** (OHLCV bars) from Polygon.io with a configurable
  cache horizon so repeated requests don't re-hit the upstream API.
- Caches results in **MySQL** via the [Tortoise ORM](https://tortoise.github.io/).

## Project layout

```
.
├── app/
│   ├── api/              # FastAPI routers
│   │   ├── constituents.py
│   │   └── market_data.py
│   ├── clients/          # External API clients
│   │   └── polygon.py
│   ├── config/
│   │   └── settings.py   # Settings (loaded from .env via pydantic-settings)
│   ├── database/
│   │   ├── models/       # Tortoise ORM models
│   │   ├── repositories/ # Async data access
│   │   └── session.py    # init/close Tortoise connections
│   ├── services/
│   │   ├── constituents_fetcher.py   # Provider dispatch + registry
│   │   ├── constituents_service.py   # Cache + fetch orchestration
│   │   ├── market_data_service.py    # Bar cache + backfill
│   │   └── parsers/                  # Per-provider parsers
│   │       ├── ssga.py
│   │       └── ishares.py
│   └── main.py           # FastAPI app + lifespan
├── migrations/           # Idempotent SQL migrations (MySQL)
├── pyproject.toml        # uv-managed dependencies
└── uv.lock
```

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (manages the venv + dependencies)
- MySQL 8.x reachable from the app
- (For Polygon-backed endpoints) a Polygon.io API key

## Setup

```bash
# 1. Install dependencies into .venv
uv sync

# 2. Copy and edit the env file
cp .env.example .env
# then fill in MYSQL_* and POLYGON_API_KEY

# 3. Apply the SQL migrations
mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" \
      -u "$MYSQL_USER" -p "$MYSQL_DATABASE" \
      < migrations/01_initial_db_setup.sql
```

> Note: the app also calls `Tortoise.generate_schemas()` at startup, so the
> tables will be created automatically if they don't exist. The migrations
> folder exists for explicit, reviewable DDL and CI/DBA workflows.

## Run

```bash
uv run python -m app.main
# or
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## Configuration

All settings come from environment variables (or a `.env` file via
`pydantic-settings`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `MYSQL_HOST` | `mysql` | MySQL host |
| `MYSQL_PORT` | `3306` | MySQL port |
| `MYSQL_USER` | _empty_ | MySQL user |
| `MYSQL_PASSWORD` | _empty_ | MySQL password |
| `MYSQL_DATABASE` | _empty_ | MySQL database |
| `POLYGON_API_KEY` | _empty_ | Polygon.io API key |
| `POLYGON_BASE_URL` | `https://api.polygon.io` | Override Polygon base URL |

## API

### `GET /health`

Liveness probe.

```bash
curl http://localhost:8001/health
# {"status":"ok"}
```

### `GET /constituents?symbol={ETF}`

Returns the holdings of a supported ETF. Results are cached in MySQL for
**7 days**; cache misses/expiries trigger a fetch from the upstream provider
specified in the registry.

| Symbol | Provider | Source |
| --- | --- | --- |
| `SPY` | SSGA | `holdings-daily-us-en-spy.xlsx` |
| `QQQ` | iShares | iShares Nasdaq-100 ETF holdings CSV |
| `IWM` | iShares | iShares Russell 2000 ETF holdings CSV |

```bash
curl 'http://localhost:8001/constituents?symbol=SPY'
```

```json
{
  "symbol": "SPY",
  "source": "cache",
  "constituents": [
    {"ticker": "NVDA", "name": "NVIDIA CORP", "weight": 8.13},
    ...
  ]
}
```

The `source` field is either `"cache"` (served from MySQL) or `"external"`
(freshly fetched and re-cached).

### `GET /market-data/{ticker}`

Returns OHLCV bars for a ticker, with a backfill-on-cache-miss strategy.
Backed by Polygon.io's `/v2/aggs/.../range/{multiplier}/{timespan}/{from}/{to}`
endpoint.

| Query param | Type | Default | Notes |
| --- | --- | --- | --- |
| `from` | `YYYY-MM-DD` | required | Start date (inclusive) |
| `to` | `YYYY-MM-DD` | today | End date (inclusive) |
| `timespan` | `day` \| `hour` \| `minute` | `day` | Bar size unit |
| `multiplier` | int (≥1) | `1` | Bar size multiplier |

Any time range is supported; the cache will backfill missing dates from Polygon on demand.

```bash
curl 'http://localhost:8001/market-data/AAPL?from=2026-07-01&to=2026-08-01&timespan=day'
```

```json
{
  "ticker": "AAPL",
  "timespan": "day",
  "multiplier": 1,
  "from": "2026-07-01",
  "to": "2026-08-01",
  "backfilled_bars": 22,
  "bars": [
    {"timestamp_ms": 1720564800000, "bar_date": "2026-07-09", ...},
    ...
  ]
}
```

## Adding a new ETF

1. If the provider is new, add a parser under
   `app/services/parsers/<provider>.py` exposing `parse(content: bytes) -> list[dict]`.
   Each parser must return `{"ticker", "name", "weight}` dicts.
2. Register the parser in `PROVIDER_PARSERS` in
   [`app/services/constituents_fetcher.py`](app/services/constituents_fetcher.py).
3. Add a registry entry to `ETF_REGISTRY` with `provider` and `link`.

No other code changes are needed — the service auto-derives its
`SUPPORTED_SYMBOLS` set from the registry.

## Adding a database migration

Create a new file `migrations/NN_description.sql` (zero-padded sequence
number). Use `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`,
etc., so the script is idempotent. See
[`migrations/README.md`](migrations/README.md) for details.

## License

MIT (or whatever your repo says — update as appropriate).