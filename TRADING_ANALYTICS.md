# Trading Analytics — Setup & Architecture Guide

## Overview

The Trading Analytics feature fetches your trade history from the Dhan API,
matches BUY and SELL executions using FIFO rules, and computes P&L, win rate,
holding periods, monthly/yearly aggregations, and behavioral insights.

All processing is server-side. No Dhan credentials are ever sent to the browser.

---

## Prerequisites

- Python packages: `pandas`, `openpyxl`, `xlsxwriter`, `reportlab`, `requests`
  (all already in `requirements.txt`)
- Dhan trading account with API access enabled

---

## Setup

### 1. Generate a Dhan Access Token

1. Log in to [Dhan](https://dhan.co) → Profile → API
2. Generate an access token (note: tokens expire periodically — check Dhan docs)
3. Copy your **Client ID** (numeric, shown in your profile)

### 2. Add credentials to `.env`

Open `.env` in the project root and add:

```
# Dhan API — Trading Analytics
DHAN_CLIENT_ID=your_client_id_here
DHAN_ACCESS_TOKEN=your_access_token_here
```

**Security rules:**
- Never commit `.env` to git. Confirm `.gitignore` contains `.env`.
- Never share or log the access token.
- The token is read server-side only via `os.getenv()` — it never reaches the browser.

### 3. Create the analytics database

```bash
flask shell
>>> from app.extensions import db
>>> db.create_all(bind_key='analytics')
>>> exit()
```

This creates `data/trading_analytics/trading_analytics.db` — a separate SQLite
database from the main app DB. It can be inspected with DBeaver, DB Browser for
SQLite, or any SQLite-compatible tool.

### 4. Restart Flask

```bash
flask run --port=5005
```

### 5. First sync

Go to `http://localhost:5005/trading-analytics`, select a date range
(max 90 days), and click **Sync Now**. After sync completes, click
**Re-run FIFO** to compute matched trades.

---

## Synchronization

### How it works

```
POST /trading-analytics/sync
  → DhanClient.from_env()          reads credentials from .env
  → DhanTradeSync.fetch_range()    paginates /trades/{from}/{to}/{page}
  → persist_trades()               saves DhanRawTradeRecord + TradeExecution
  → background thread completes
  → poll GET /trading-analytics/sync/progress
```

### API limits

| Limit | Value | Handled by |
|---|---|---|
| Max date range per request | 90 days | `validate_date_range()` |
| Records per page | 50 | `_fetch_page()` pagination |
| Retries on 5xx | 3× (backoff 0.5s) | `urllib3.Retry` on `HTTPAdapter` |
| Request timeout | 15 seconds | `_REQUEST_TIMEOUT` constant |
| Rate limit (429) | Raises `DhanRateLimitError` | Caller must back off |

### Re-syncing

Running Sync Now again is safe. Duplicate trades are detected by
`exchange_trade_id` and skipped. The `TradeExecution` table has a
`UNIQUE(source, provider_trade_id)` constraint at the DB level.

### Token expiry

Dhan access tokens expire periodically. When the token expires:
- The sync endpoint returns `{"ok": false, "error_type": "auth"}`
- Generate a new token from the Dhan portal
- Update `DHAN_ACCESS_TOKEN` in `.env` and restart Flask

---

## Data Architecture

```
Dhan API
  ↓ (raw JSON)
DhanRawTradeRecord      — exact Dhan response fields, analytics DB
  ↓ (via TradeNormalizer)
TradeExecution          — provider-independent, analytics DB
  ↓ (via FifoEngine)
MatchedTrade            — completed buy→sell lot pairs, analytics DB
OpenLot                 — remaining unmatched BUY quantity, analytics DB
  ↓ (via TradeAnalyticsEngine)
AnalyticsReport         — aggregates: win rate, P&L, holding periods
  ↓ (via PeriodAnalytics)
MonthlyPeriod           — per-month aggregation
YearlyPeriod            — per-year aggregation
  ↓ (via InsightsEngine)
InsightsReport          — deterministic behavioral observations
```

### Database

All trading analytics data lives in a **separate SQLite database**:

```
data/trading_analytics/trading_analytics.db
```

Tables:
| Table | Contents |
|---|---|
| `dhan_connection_status` | Connection ping history |
| `dhan_sync_log` | Sync attempt audit trail |
| `dhan_raw_trades` | Exact Dhan API response per trade |
| `trade_executions` | Normalized, provider-independent trade records |
| `matched_trades` | FIFO-matched buy→sell pairs with P&L |
| `open_lots` | Remaining open positions |

---

## FIFO Matching Behavior

### Rules

1. Trades are grouped by `(symbol, exchange, instrument_type)`.
2. Within each group, all executions are sorted by `(traded_at, id)` — oldest first.
3. BUY lots enter a FIFO queue. SELL quantities consume the oldest available BUY lots first.
4. Partial fills are handled: a BUY lot can be split across multiple SELLs.

### Example

```
10 Jun: BUY 100 ABC @ ₹100
15 Jun: BUY 100 ABC @ ₹110
20 Jun: SELL 150 ABC @ ₹130

Result:
  MatchedTrade 1: 100 shares, buy=₹100, sell=₹130, hold=10d, P&L=+₹3,000
  MatchedTrade 2:  50 shares, buy=₹110, sell=₹130, hold=5d,  P&L=+₹1,000
  OpenLot:         50 shares, buy=₹110 (unsold)
```

### Re-matching

Clicking **Re-run FIFO** clears and rebuilds `matched_trades` and `open_lots`
for all affected symbols from the current `trade_executions` table.
This is idempotent — running it twice produces the same result.

### Orphan SELLs

If a SELL has no corresponding BUY (e.g. pre-existing position before sync
start date), it is recorded as an "unmatched sell" and excluded from P&L.
Check the Flask terminal log for: `[FifoEngine] orphan SELL ... qty ignored`.

---

## Holding Period Calculation

**Calendar days only.** Formula: `sell_date - buy_date` in calendar days.

Trading days are **not calculated** — no market calendar library is installed.
Intraday trades (same-day buy and sell) have `holding_days = 0`.

To add trading-day support in future: install `exchange_calendars` and add a
calendar adapter in `trade_analytics.py`.

---

## Analytics Cache

Period analytics (monthly/yearly) are cached to:

```
data/trading_analytics/analytics_cache.json
```

The cache is keyed by the `run_id` of the most recent FIFO run. When a new
sync and re-run happens, `run_id` changes and the cache is automatically stale
— the next `/monthly` or `/yearly` request recomputes and re-caches.

To manually clear the cache: delete `analytics_cache.json`.

---

## Known Limitations

| Limitation | Detail |
|---|---|
| 90-day history only | Dhan API hard limit. Trades older than 90 days are not accessible. |
| No charge allocation | P&L is **gross only** — brokerage, STT, GST are not included. |
| Calendar days only | Holding period is calendar days, not trading days. |
| No short selling | Orphan SELLs (no matching BUY) are excluded from matching. |
| Token expiry | Dhan tokens expire; regenerate from Dhan portal when sync fails with auth error. |
| NSE/BSE only | Exchange segments outside `NSE_EQ`, `BSE_EQ`, `NSE_FNO` etc. may not normalize correctly. |
| Single account | The current implementation supports one Dhan account per deployment. |
| No historical screener context | Screener signals at trade entry date are not available (only last 5 scan snapshots stored). |

---

## Exports

| Export | Route | Format |
|---|---|---|
| Completed trades | `/trading-analytics/export` | CSV |
| Completed trades | `/trading-analytics/export/trades/xlsx` | XLSX |
| Analytics summary | `/trading-analytics/export/analytics/json` | JSON |
| Analytics summary | `/trading-analytics/export/analytics/csv` | CSV |
| Full report | `/trading-analytics/export/report/pdf` | PDF (reportlab) |
| Chart images | Browser only — click the download icon on each chart | PNG |

---

## Security Checklist

- [x] `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` loaded via `os.getenv()` only
- [x] Token never logged, never in error messages, never in API responses
- [x] `client_id` always masked in UI (first 4 + last 2 chars)
- [x] All routes protected with `@login_required`
- [x] All POST routes protected with CSRF token
- [x] Dashboard reads local DB only on GET — never calls Dhan API automatically
- [x] No credentials stored in `analytics_cache.json` or any export file
- [x] `.env` must be in `.gitignore`