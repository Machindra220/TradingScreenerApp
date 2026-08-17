"""
market_data_cache.py

Shared price-history cache used by every screener/chart page (Adaptive VOLAR,
VOLAR Stage 2 IND/US, chart_carousel, chart_weinstein, chart_multiframe, etc.)
so they read from ONE place instead of each independently re-fetching the same
500-750 stocks from yfinance on every single scan.

MULTI-MARKET DESIGN
Each market gets its own SQLite file so symbol namespaces never collide
(a US ticker "AAPL" vs any hypothetical "AAPL.NS" can't overwrite each other):
  data/market_cache/market_data_ind.db  — NSE / Indian equities
  data/market_cache/market_data_us.db   — US / S&P 500 equities

Use the module-level convenience functions (get_price_history_bulk, etc.) for
the IND market — they proxy to the default IND instance so existing IND
screener code needs zero changes.

For the US market, import and use the pre-built instance:
  from app.services.market_data_cache import us_cache
  results, report = us_cache.get_price_history_bulk(symbols, ...)

WHERE TO PUT THIS FILE
Place it in app/services/ (alongside __init__.py) so it's importable by every
route module as `from app.services.market_data_cache import ...`.

DESIGN PRINCIPLES
- Maximum history: period="max" for daily bars. Intraday/hourly is hard-capped
  at ~730 days by Yahoo — a platform limit, not a design choice.
- A failed fetch for one symbol NEVER touches any other symbol's rows, and
  NEVER touches that symbol's own previously-cached rows. We only overwrite
  after a successful fetch. A bad scan means a symbol stays one day stale,
  not that it goes missing.
- Schema self-heals: CREATE TABLE IF NOT EXISTS runs on every connection open
  (not just at import). If the .db file is deleted while the app runs, the
  next call transparently rebuilds an empty-but-valid database and re-fetches
  from yfinance — the app never crashes with "no such table".
- Daily backup before the first refresh of each calendar day (last 5 kept).
- progress_callback(index, total, symbol) wired through get_price_history_bulk
  so the caller's progress bar moves during the fetch, not after it.
"""

import os
import time
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, date

import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo   # stdlib ≥ 3.9; no pip install needed

# Anchor to __file__ (app/services/market_data_cache.py → project root is two levels up)
# Using os.getcwd() here is fragile: the Werkzeug reloader can run in a different
# working directory than the main process, causing the DB to be created/looked for
# in the wrong place after a hot-reload.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DATA_DIR   = os.path.join(_PROJECT_ROOT, 'data', 'market_cache')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
BACKUP_LIMIT = 5
INTRADAY_MAX_DAYS = 729  # Yahoo hard-cap for intraday history

# ── Market close times (timezone-aware) ─────────────────────────────────────
# Used by _last_settled_bar_date() to decide whether today's bar is final.
# A bar is only "settled" once the market has been closed for ≥ 15 minutes
# (a small buffer for yfinance data to propagate).
#
# Key insight: if you run a screener at 11 AM IST (market open), yfinance
# returns an incomplete bar for today.  _is_stale() must know the bar is NOT
# yet settled and force a re-fetch after market close.
#
# IND: NSE closes 15:30 IST  (UTC+5:30)
# US:  NYSE/NASDAQ closes 16:00 ET (UTC-5 or UTC-4 during DST)
_MARKET_CONFIG = {
    "IND": {
        "tz":           ZoneInfo("Asia/Kolkata"),
        "close_hour":   15,
        "close_minute": 30,
        "label":        "NSE (IND)",
    },
    "US": {
        "tz":           ZoneInfo("America/New_York"),
        "close_hour":   16,
        "close_minute": 0,
        "label":        "NYSE/NASDAQ (US)",
    },
}
_SETTLE_BUFFER_MINUTES = 15   # wait this long after close before treating bar as final

os.makedirs(DATA_DIR,   exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)


class MarketCache:
    """One instance per market/DB file. All internal state is per-instance so
    the IND and US caches are completely independent."""

    def __init__(self, db_filename, market="IND"):
        self.db_path    = os.path.join(DATA_DIR, db_filename)
        self.market     = market.upper() if market.upper() in _MARKET_CONFIG else "IND"
        self._backed_up_today = False
        self._init_db()

    # ------------------------------------------------------------------
    # Schema / connection
    # ------------------------------------------------------------------

    def _ensure_schema(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                symbol TEXT NOT NULL, interval TEXT NOT NULL, date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, interval, date)
            )""")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_price_symbol_interval
            ON price_history (symbol, interval, date)""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fetch_meta (
                symbol TEXT NOT NULL, interval TEXT NOT NULL,
                last_fetched_at TEXT, last_bar_date TEXT, last_fetch_status TEXT,
                PRIMARY KEY (symbol, interval)
            )""")

    def _get_conn(self):
        os.makedirs(DATA_DIR, exist_ok=True)          # self-heal missing dir
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema(conn)
        return conn

    def _init_db(self):
        with self._get_conn():
            pass

    # ------------------------------------------------------------------
    # Backup / restore
    # ------------------------------------------------------------------

    def backup_if_needed(self):
        today = date.today().isoformat()
        tag   = f"{os.path.splitext(os.path.basename(self.db_path))[0]}_{today}"
        marker = os.path.join(BACKUP_DIR, f".backed_up_{tag}")
        if os.path.exists(marker):
            return
        if os.path.exists(self.db_path):
            shutil.copy2(self.db_path, os.path.join(BACKUP_DIR, f"{tag}.db"))
            self._prune_backups()
        open(marker, 'w').close()

    def _prune_backups(self):
        stem = os.path.splitext(os.path.basename(self.db_path))[0]
        files = sorted(
            (f for f in os.listdir(BACKUP_DIR) if f.startswith(stem) and f.endswith('.db')),
            reverse=True
        )
        for old in files[BACKUP_LIMIT:]:
            try: os.remove(os.path.join(BACKUP_DIR, old))
            except OSError: pass

    def restore_latest_backup(self):
        stem = os.path.splitext(os.path.basename(self.db_path))[0]
        files = sorted(
            (f for f in os.listdir(BACKUP_DIR) if f.startswith(stem) and f.endswith('.db')),
            reverse=True
        )
        if not files:
            return False
        shutil.copy2(os.path.join(BACKUP_DIR, files[0]), self.db_path)
        return True

    # ------------------------------------------------------------------
    # Staleness helpers
    # ------------------------------------------------------------------

    def _last_settled_bar_date(self):
        """
        Returns the date of the most recent FULLY SETTLED daily bar for
        this market — i.e. a bar whose close price is final and complete.

        A bar is settled only after the market has closed AND at least
        _SETTLE_BUFFER_MINUTES have elapsed (so yfinance data propagates).

        Examples (IND market, NSE closes 15:30 IST):
          - Run at 10:00 IST (market open)  → yesterday's bar is last settled
          - Run at 15:45 IST (just closed)  → today's bar is now settled
          - Run at 18:00 IST (evening)      → today's bar is settled
          - Run at 10:00 UTC on a UTC server → same as 15:30 IST → still yesterday

        Weekend / holiday handling:
          Walk backwards from the last-settled calendar date until we land
          on a weekday.  Full exchange holiday calendars are not bundled here
          (would require pandas-market-calendars dependency), but the
          yfinance fetch itself naturally returns no row for holidays — the
          missing date simply stays absent from the cache rather than
          causing a crash.
        """
        cfg        = _MARKET_CONFIG[self.market]
        tz         = cfg["tz"]
        now_local  = datetime.now(tz)                      # tz-aware local time
        today      = now_local.date()

        # Build today's settlement deadline in market-local time
        settle_dt  = now_local.replace(
            hour   = cfg["close_hour"],
            minute = cfg["close_minute"] + _SETTLE_BUFFER_MINUTES,
            second = 0, microsecond = 0
        )

        # If we are past today's settlement time, today's bar is settled.
        # Otherwise the last settled bar is from the previous calendar day.
        if now_local >= settle_dt:
            candidate = today
        else:
            candidate = today - timedelta(days=1)

        # Walk back over weekends (Mon=0 … Sun=6; Sat=5, Sun=6 are non-trading)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)

        return candidate

    def _get_meta(self, symbol, interval):
        with self._get_conn() as conn:
            return conn.execute(
                "SELECT last_bar_date, last_fetched_at FROM fetch_meta WHERE symbol=? AND interval=?",
                (symbol, interval)
            ).fetchone()

    def _is_stale(self, symbol, interval):
        """
        A cached entry is stale when:
          (a) it has never been fetched, OR
          (b) its last_bar_date is older than the last settled bar date,  OR
          (c) its last_bar_date equals the last settled bar date BUT the
              fetch happened before today's market close — meaning the
              stored bar was captured mid-session and the close price is
              incomplete.

        Case (c) is the bug you hit: you scanned at 10 AM (market open),
        the cache stored today's date as last_bar_date, _is_stale() saw
        last_bar == today and returned False (not stale), but the close
        price was still an intraday snapshot, not the final EOD figure.
        """
        row = self._get_meta(symbol, interval)
        if not row or not row[0]:
            return True                                    # never fetched

        last_bar        = datetime.strptime(row[0], "%Y-%m-%d").date()
        last_fetched_at = row[1]                           # ISO string or None
        settled_date    = self._last_settled_bar_date()

        # (b) bar is from a previous trading day
        if last_bar < settled_date:
            return True

        # (c) bar date matches today's settled date but was fetched
        #     BEFORE the market settled — the stored close is partial
        if last_bar == settled_date and last_fetched_at:
            try:
                cfg       = _MARKET_CONFIG[self.market]
                tz        = cfg["tz"]
                fetch_dt  = datetime.fromisoformat(last_fetched_at)
                # Make fetch_dt timezone-aware if it is naive (stored without tz)
                if fetch_dt.tzinfo is None:
                    fetch_dt = fetch_dt.replace(tzinfo=ZoneInfo("UTC"))
                settle_dt = datetime.now(tz).replace(
                    hour   = cfg["close_hour"],
                    minute = cfg["close_minute"] + _SETTLE_BUFFER_MINUTES,
                    second = 0, microsecond = 0
                ).astimezone(ZoneInfo("UTC"))
                # Convert to the settled date in local tz for comparison
                settle_date_local = settle_dt.astimezone(tz).date()
                if settle_date_local == settled_date and fetch_dt.astimezone(ZoneInfo("UTC")) < settle_dt:
                    return True   # fetched before close — close price is incomplete
            except (ValueError, TypeError):
                pass              # malformed timestamp — treat as not stale

        return False

    # ------------------------------------------------------------------
    # Fetch / store
    # ------------------------------------------------------------------

    def _fetch_from_yfinance(self, symbol, interval, existing_last_date=None):
        ticker = yf.Ticker(symbol)
        if interval == '1d':
            if existing_last_date:
                start = (datetime.strptime(existing_last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                return ticker.history(start=start, interval='1d')
            return ticker.history(period="max", interval='1d')
        return ticker.history(period=f"{INTRADAY_MAX_DAYS}d", interval=interval)

    def _store(self, symbol, interval, df):
        if df is None or df.empty:
            return 0
        date_fmt = "%Y-%m-%d" if interval == '1d' else "%Y-%m-%d %H:%M:%S"
        rows = [
            (symbol, interval, idx.strftime(date_fmt),
             float(r['Open'])   if not pd.isna(r['Open'])   else None,
             float(r['High'])   if not pd.isna(r['High'])   else None,
             float(r['Low'])    if not pd.isna(r['Low'])    else None,
             float(r['Close'])  if not pd.isna(r['Close'])  else None,
             float(r['Volume']) if not pd.isna(r['Volume']) else None)
            for idx, r in df.iterrows()
        ]
        with self._get_conn() as conn:
            conn.executemany("""
                INSERT INTO price_history (symbol, interval, date, open, high, low, close, volume)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, interval, date) DO UPDATE SET
                  open=excluded.open, high=excluded.high, low=excluded.low,
                  close=excluded.close, volume=excluded.volume""", rows)
            last_date = max(r[2][:10] for r in rows)
            conn.execute("""
                INSERT INTO fetch_meta (symbol, interval, last_fetched_at, last_bar_date, last_fetch_status)
                VALUES (?,?,?,?,?)
                ON CONFLICT(symbol, interval) DO UPDATE SET
                  last_fetched_at=excluded.last_fetched_at,
                  last_bar_date=excluded.last_bar_date,
                  last_fetch_status=excluded.last_fetch_status""",
                (symbol, interval, datetime.now().isoformat(), last_date, 'ok'))
        return len(rows)

    def _mark_fetch_error(self, symbol, interval, error):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO fetch_meta (symbol, interval, last_fetched_at, last_fetch_status)
                VALUES (?,?,?,?)
                ON CONFLICT(symbol, interval) DO UPDATE SET
                  last_fetched_at=excluded.last_fetched_at,
                  last_fetch_status=excluded.last_fetch_status""",
                (symbol, interval, datetime.now().isoformat(), f'error: {error}'))

    def _read_cached(self, symbol, interval, lookback_days=None):
        with self._get_conn() as conn:
            if lookback_days:
                df = pd.read_sql_query("""
                    SELECT date, open, high, low, close, volume FROM (
                        SELECT date, open, high, low, close, volume FROM price_history
                        WHERE symbol=? AND interval=? ORDER BY date DESC LIMIT ?
                    ) ORDER BY date ASC""",
                    conn, params=(symbol, interval, lookback_days))
            else:
                df = pd.read_sql_query("""
                    SELECT date, open, high, low, close, volume FROM price_history
                    WHERE symbol=? AND interval=? ORDER BY date""",
                    conn, params=(symbol, interval))
        if df.empty:
            return df
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
        df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        return df

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_price_history(self, symbol, interval='1d', lookback_days=None, force_refresh=False):
        if force_refresh or self._is_stale(symbol, interval):
            meta = self._get_meta(symbol, interval)
            existing_last_date = meta[0] if meta else None
            try:
                fresh = self._fetch_from_yfinance(symbol, interval, existing_last_date)
                self._store(symbol, interval, fresh)
            except Exception as e:
                self._mark_fetch_error(symbol, interval, e)
        return self._read_cached(symbol, interval, lookback_days)

    def get_price_history_bulk(self, symbols, interval='1d', lookback_days=None,
                                max_retries=2, base_delay=0.6, progress_callback=None):
        """
        Batch read for screener scans. Returns (results, report).

        progress_callback(index, total, symbol) fires once per symbol —
        including instant cache hits — so the caller's progress bar moves
        DURING this call, not after it. This is critical: this function is
        where nearly all wall-clock time goes on a cold/stale cache.
        """
        self.backup_if_needed()
        results = {}
        report  = {"fetched": 0, "from_cache": 0, "failed": []}
        total   = len(symbols)

        for i, symbol in enumerate(symbols):
            if progress_callback:
                progress_callback(i, total, symbol)

            if not self._is_stale(symbol, interval):
                results[symbol] = self._read_cached(symbol, interval, lookback_days)
                report["from_cache"] += 1
                continue

            meta = self._get_meta(symbol, interval)
            existing_last_date = meta[0] if meta else None
            attempt, done = 0, False

            while attempt <= max_retries and not done:
                try:
                    fresh = self._fetch_from_yfinance(symbol, interval, existing_last_date)
                    if fresh is not None and not fresh.empty:
                        self._store(symbol, interval, fresh)
                        report["fetched"] += 1
                    else:
                        report["from_cache"] += 1
                    done = True
                except Exception as e:
                    attempt += 1
                    if attempt > max_retries:
                        self._mark_fetch_error(symbol, interval, e)
                        report["failed"].append(symbol)
                        done = True
                    else:
                        time.sleep(base_delay * attempt + random.uniform(0, 0.3))

            results[symbol] = self._read_cached(symbol, interval, lookback_days)
            time.sleep(0.15)  # pacing — avoids per-minute throttle across a large universe

        if progress_callback:
            progress_callback(total, total, "")

        return results, report

    def cache_stats(self):
        with self._get_conn() as conn:
            sym_count = conn.execute("SELECT COUNT(DISTINCT symbol) FROM price_history").fetchone()[0]
            row_count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
            oldest    = conn.execute("SELECT MIN(date) FROM price_history").fetchone()[0]
            newest    = conn.execute("SELECT MAX(date) FROM price_history").fetchone()[0]
        return {
            "symbols_cached": sym_count, "total_rows": row_count,
            "oldest_date": oldest,       "newest_date": newest,
            "db_size_mb": round(os.path.getsize(self.db_path) / (1024*1024), 2)
                          if os.path.exists(self.db_path) else 0,
        }


# ---------------------------------------------------------------------------
# Pre-built market instances
# ---------------------------------------------------------------------------

# Indian / NSE equities — renamed from the original market_data.db
ind_cache = MarketCache("market_data_ind.db", market="IND")

# US / S&P 500 equities — separate file so symbol namespaces never collide
us_cache  = MarketCache("market_data_us.db", market="US")


# ---------------------------------------------------------------------------
# Module-level convenience shims (IND market, backward-compatible)
# All existing IND screener code that calls get_price_history_bulk(...)
# directly continues to work without any changes.
# ---------------------------------------------------------------------------

def get_price_history(symbol, interval='1d', lookback_days=None, force_refresh=False):
    return ind_cache.get_price_history(symbol, interval, lookback_days, force_refresh)

def get_price_history_bulk(symbols, interval='1d', lookback_days=None,
                            max_retries=2, base_delay=0.6, progress_callback=None):
    return ind_cache.get_price_history_bulk(
        symbols, interval, lookback_days, max_retries, base_delay, progress_callback)

def latest_bar_date(price_data_dict):
    dates = [df.index.max() for df in price_data_dict.values() if df is not None and not df.empty]
    return max(dates).strftime("%Y-%m-%d") if dates else None

def backup_db_if_needed():
    ind_cache.backup_if_needed()

def restore_latest_backup():
    return ind_cache.restore_latest_backup()

def cache_stats():
    return ind_cache.cache_stats()