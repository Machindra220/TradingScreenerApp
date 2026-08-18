import os
import json
import glob
import threading
from io import StringIO
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Blueprint, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename
from datetime import datetime, date

hh_hl_us_bp = Blueprint("hh_hl_us", __name__)

# ── Path anchoring (never os.getcwd()) ──────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER   = os.path.join(_PROJECT_ROOT, 'uploads', 'us_hhhl')
DATA_FOLDER     = os.path.join(_PROJECT_ROOT, 'data')
RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'last_us_hhhl_results.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
HISTORY_DIR     = os.path.join(UPLOAD_FOLDER, 'history_cache')
TICKER_STATS    = os.path.join(UPLOAD_FOLDER, 'ticker_stats.json')
DEFAULT_CSV     = os.path.join(DATA_FOLDER, 'sp500.csv')

# Shared instance/ folder — same location as market_data_us.db
# sector_cache.json is shared across ALL screener pages (IND + US)
SECTOR_CACHE     = os.path.join(_PROJECT_ROOT, 'instance', 'sector_cache.json')
_SECTOR_TTL_DAYS = 15   # sectors rarely change; 15-day warm cache is safe

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(HISTORY_DIR,   exist_ok=True)
os.makedirs(DATA_FOLDER,   exist_ok=True)
os.makedirs(os.path.join(_PROJECT_ROOT, 'instance'), exist_ok=True)

# ── Shared market-data cache (US) ────────────────────────────────────────────
# Import us_cache directly — NOT the module-level get_price_history_bulk wrapper
# which routes to ind_cache (IND database). US screener must use us_cache so
# symbols are looked up in market_data_us.db, not market_data_ind.db.
try:
    from app.services.market_data_cache import us_cache as _us_cache, latest_bar_date
    _US_CACHE_AVAILABLE = True
except ImportError:
    _us_cache = None
    _US_CACHE_AVAILABLE = False

# ── Progress tracking ────────────────────────────────────────────────────────
_progress = {"pct": 0, "msg": "Idle", "running": False,
             "cache_hits": 0, "yf_fetches": 0, "failed": 0}
_lock = threading.Lock()

def _set_progress(pct, msg):
    with _lock:
        _progress["pct"] = pct
        _progress["msg"] = msg

# ── Sector cache helpers (shared instance/sector_cache.json, TTL 15 days) ───
def _load_sector_cache():
    if os.path.exists(SECTOR_CACHE):
        try:
            return json.load(open(SECTOR_CACHE))
        except Exception:
            pass
    return {}

def _save_sector_cache(cache):
    try:
        with open(SECTOR_CACHE, 'w') as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"[HHHL-US] sector cache save error: {e}")

def _get_sector_cached(symbol, sector_cache):
    """
    Returns GICS sector string for symbol (bare US ticker, e.g. 'AAPL').
    Checks shared sector_cache dict first (TTL = 15 days).
    Only calls yf.Ticker.info on cache miss or stale entry.
    Updates sector_cache in-place — caller persists after the loop.
    """
    today = date.today().isoformat()
    entry = sector_cache.get(symbol)
    if entry:
        try:
            age = (date.today() - date.fromisoformat(entry.get("fetched", ""))).days
            if age < _SECTOR_TTL_DAYS:
                return entry.get("sector", "N/A")
        except Exception:
            pass
    # Cache miss or stale
    try:
        s = yf.Ticker(symbol).info.get('sector', 'N/A') or 'N/A'
    except Exception:
        s = 'N/A'
    sector_cache[symbol] = {"sector": s, "fetched": today}
    return s

# ── S&P 500 default ticker list ──────────────────────────────────────────────
# Wikipedia scrape with GICS sectors — populates sector_cache as a bonus
def _fetch_sp500_wikipedia(sector_cache):
    """
    Scrapes S&P 500 roster from Wikipedia.
    Returns list of {"symbol": str, "sector": str}.
    Also warms the sector_cache for all tickers found (free, no yf calls).
    """
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp   = requests.get(url, headers=headers, timeout=12)
        tables = pd.read_html(StringIO(resp.text))
        df     = tables[0]
        df['Symbol'] = df['Symbol'].str.replace('.', '-', regex=False)
        today = date.today().isoformat()
        result = []
        for _, row in df.iterrows():
            sym    = str(row['Symbol']).strip().upper()
            sector = str(row.get('GICS Sector', 'N/A')).strip()
            result.append({"symbol": sym, "sector": sector})
            # Warm sector cache from Wikipedia (no TTL concern — free data)
            if sym not in sector_cache or sector_cache[sym].get("sector") == "N/A":
                sector_cache[sym] = {"sector": sector, "fetched": today}
        return result
    except Exception as e:
        print(f"[HHHL-US] Wikipedia S&P 500 fetch failed: {e}")
        return []

def _get_default_symbols(sector_cache):
    """
    Returns list of {"symbol": str, "sector": str}.
    Priority: sp500.csv in data/ → Wikipedia scrape → empty list.
    """
    if os.path.exists(DEFAULT_CSV):
        try:
            df = pd.read_csv(DEFAULT_CSV)
            df.columns = df.columns.str.strip().str.lower()
            if 'symbol' in df.columns:
                sym_col = 'symbol'
                sec_col = 'sector' if 'sector' in df.columns else None
                result  = []
                today   = date.today().isoformat()
                for _, row in df.iterrows():
                    sym    = str(row[sym_col]).strip().upper().replace('.', '-')
                    sector = str(row[sec_col]).strip() if sec_col and pd.notna(row.get(sec_col)) else "N/A"
                    result.append({"symbol": sym, "sector": sector})
                    if sector != "N/A" and (sym not in sector_cache or sector_cache[sym].get("sector") == "N/A"):
                        sector_cache[sym] = {"sector": sector, "fetched": today}
                return result
        except Exception:
            pass
    return _fetch_sp500_wikipedia(sector_cache)

# ── Active source helper ─────────────────────────────────────────────────────
def _get_active_source():
    if os.path.exists(LAST_CSV_CONFIG):
        try:
            cfg   = json.load(open(LAST_CSV_CONFIG))
            fname = cfg.get("last_used_csv", "")
            fpath = os.path.join(UPLOAD_FOLDER, fname)
            if fname and os.path.exists(fpath):
                return fpath, fname, False
        except Exception:
            pass
    return DEFAULT_CSV, "S&P 500 (Default)", True

# ── HH-HL analysis on pre-fetched DataFrame ──────────────────────────────────
# ── DataFrame column normaliser (handles single-ticker and MultiIndex bulk) ───
def _normalise_df(df, sym=None):
    if df is None or df.empty:
        return None
    cols = df.columns
    if isinstance(cols, pd.MultiIndex) or (len(cols) > 0 and isinstance(cols[0], tuple)):
        if sym is not None:
            for cand in [sym, sym.replace('-', '.'), sym]:
                try:
                    sliced = df.xs(cand, axis=1, level=1)
                    sliced.columns = [c.title() for c in sliced.columns]
                    return sliced
                except KeyError:
                    pass
        flat_cols = {}
        for c in cols:
            field = c[0] if isinstance(c, tuple) else c
            if field.lower() in ('open','high','low','close','volume'):
                flat_cols[c] = field.title()
        if flat_cols:
            df = df[list(flat_cols.keys())].copy()
            df.columns = list(flat_cols.values())
            return df
        return None
    df = df.copy()
    df.columns = [c.title() if isinstance(c, str) and c.lower() in
                  ('open','high','low','close','volume') else c for c in df.columns]
    return df

def _analyse_us(symbol, df, sector, bench_close):
    """
    Filters:
      1. Price within ±10% of 200-SMA (near-EMA zone — early entry)
      2. Last swing-low > previous swing-low  (Higher Low)
      3. Current price > last swing-low       (confirmation above HL)
    Metrics: RS vs ^GSPC, 20D RS change, SL%, 52W retracement,
             bars_since_pivot, hh_confirmed, ema_dist.
    bench_close: pd.Series of ^GSPC Close aligned to same date range.
    """
    try:
        if df is None or df.empty or len(df) < 200:
            return None

        # _normalise_df handles simple-string, MultiIndex, and tuple columns
        df = _normalise_df(df, symbol)
        if df is None or df.empty:
            return None

        close        = df['Close']
        curr_price   = float(close.iloc[-1])
        ma200_series = close.rolling(window=200).mean()
        ma200        = float(ma200_series.iloc[-1])

        if pd.isna(ma200) or ma200 == 0:
            return None

        # ── Filter 1: ±10% of 200-SMA ─────────────────────────────────────
        if curr_price < ma200 * 0.90 or curr_price > ma200 * 1.10:
            return None

        # ── Swing-low detection (5-bar pivot) ──────────────────────────────
        df['is_low'] = (
            (df['Low'] < df['Low'].shift(1)) &
            (df['Low'] < df['Low'].shift(2)) &
            (df['Low'] < df['Low'].shift(-1)) &
            (df['Low'] < df['Low'].shift(-2))
        )
        lows_df = df[df['is_low']]
        if len(lows_df) < 2:
            return None

        last_swing_low = float(lows_df['Low'].iloc[-1])
        prev_swing_low = float(lows_df['Low'].iloc[-2])

        # bars since last pivot low
        last_low_iloc   = df.index.get_loc(lows_df.index[-1])
        bars_since_pivot = int((len(df) - 1) - last_low_iloc)

        # ── Filter 2 & 3: Higher Low + price above it ──────────────────────
        if not (last_swing_low > prev_swing_low and curr_price > last_swing_low):
            return None

        # ── Swing-high check for HH confirmation ───────────────────────────
        df['is_high'] = (
            (df['High'] > df['High'].shift(1)) &
            (df['High'] > df['High'].shift(2)) &
            (df['High'] > df['High'].shift(-1)) &
            (df['High'] > df['High'].shift(-2))
        )
        highs_df     = df[df['is_high']]
        hh_confirmed = False
        if len(highs_df) >= 2:
            hh_confirmed = float(highs_df['High'].iloc[-1]) > float(highs_df['High'].iloc[-2])

        # ── Metrics ────────────────────────────────────────────────────────
        high_52w    = float(df['High'].max())
        retracement = round(((high_52w - curr_price) / high_52w) * 100, 2)
        sl_percent  = round(((curr_price - last_swing_low) / curr_price) * 100, 2)
        ema_dist    = round(((curr_price - ma200) / ma200) * 100, 2)

        # ── RS vs ^GSPC ────────────────────────────────────────────────────
        # Correct formula: (1+stock_return)/(1+bench_return)-1 gives true RS.
        # We also expose a scaled ratio (×1000) matching the existing column.
        rs_score  = 0.0
        rs_change = 0.0
        if bench_close is not None and len(bench_close) > 0:
            aligned = bench_close.reindex(df.index, method='ffill').dropna()
            if len(aligned) >= 20:
                rs_series = close.reindex(aligned.index) / aligned
                rs_score  = float(round(float(rs_series.iloc[-1]) * 100, 2))
                rs_20d    = float(rs_series.iloc[-20]) if len(rs_series) >= 20 else float(rs_series.iloc[0])
                rs_change = round(((rs_score - rs_20d * 100) / (rs_20d * 100)) * 100, 1) if rs_20d else 0.0
        else:
            # Fallback to SMA-based RS
            rs_score  = round(curr_price / ma200 * 100, 2)
            if len(close) >= 20 and not pd.isna(ma200_series.iloc[-20]):
                rs_20d    = float(close.iloc[-20]) / float(ma200_series.iloc[-20]) * 100
                rs_change = round(((rs_score - rs_20d) / rs_20d) * 100, 1) if rs_20d else 0.0

        rs_trend = ("Accelerating" if rs_change > 3.0
                    else "Steady"  if rs_change >= 0
                    else "Fading")

        is_fresh = bool(bars_since_pivot <= 10 and sl_percent <= 8.0)

        return {
            "symbol":          symbol,
            "sector":          str(sector),
            "price":           round(curr_price, 2),
            "ma200":           round(ma200, 2),
            "ema_dist":        ema_dist,
            "last_swing_low":  round(last_swing_low, 2),
            "sl_percent":      sl_percent,
            "retracement":     retracement,
            "rs":              rs_score,
            "rs_trend":        rs_trend,
            "rs_change":       rs_change,
            "bars_since_pivot": bars_since_pivot,
            "hh_confirmed":    hh_confirmed,
            "is_fresh":        is_fresh,
        }
    except Exception as e:
        print(f"[HHHL-US] _analyse error {symbol}: {e}")
        return None

# ── Scan worker ───────────────────────────────────────────────────────────────
def _run_scan(stock_items, source_name):
    global _progress
    stocks  = []
    total   = len(stock_items)
    symbols = [s['symbol'] for s in stock_items]
    sec_map = {s['symbol']: s.get('sector', 'N/A') for s in stock_items}

    ticker_stats = {}
    if os.path.exists(TICKER_STATS):
        try:
            ticker_stats = json.load(open(TICKER_STATS))
        except Exception:
            pass

    sector_cache = _load_sector_cache()

    # ── Step 0: Fetch ^GSPC benchmark ONCE via us_cache ────────────────────
    # CORRECT params: interval="1d", lookback_days=504 (≈2 trading years).
    # DO NOT pass period= — that is yfinance's .history() API, not the cache API.
    _set_progress(1, "Fetching S&P 500 benchmark (^GSPC)…")
    bench_close = None
    try:
        if _US_CACHE_AVAILABLE:
            bench_data, _ = _us_cache.get_price_history_bulk(
                ["^GSPC"],
                interval="1d",
                lookback_days=504,
                progress_callback=lambda i, t, s: None,
            )
            bdf = bench_data.get("^GSPC")
            if bdf is not None and not bdf.empty:
                bdf = _normalise_df(bdf, "^GSPC")
                if bdf is not None and "Close" in bdf.columns:
                    bench_close = bdf["Close"]
        if bench_close is None:
            # Genuine fallback only when cache service is unavailable
            bdf = yf.Ticker("^GSPC").history(period="2y")
            if not bdf.empty:
                bench_close = bdf["Close"]
    except Exception as e:
        print(f"[HHHL-US] benchmark fetch error: {e}")

    # ── Step 1: Bulk price-history fetch via US cache ────────────────────────
    price_data   = {}
    fetch_report = {"from_cache": 0, "fetched": 0, "failed": []}

    if _US_CACHE_AVAILABLE:
        # CORRECT params: interval + lookback_days, NOT period=.
        # us_cache routes to market_data_us.db (US symbols only).
        # ind_cache (the old wrapper) would query market_data_ind.db → wrong db.
        _set_progress(3, f"Loading {total} symbols from US cache…")

        def _cb(idx, ttl, sym):
            pct = 3 + int((idx / ttl) * 52)
            _set_progress(pct, f"[Cache] {sym} ({idx}/{ttl})")

        try:
            price_data, fetch_report = _us_cache.get_price_history_bulk(
                symbols,
                interval="1d",
                lookback_days=504,        # ≈ 2 trading years; enough for 200-bar MA
                progress_callback=_cb,
            )
        except Exception as e:
            print(f"[HHHL-US] Cache bulk fetch error: {e}")
            fetch_report = {"from_cache": 0, "fetched": 0, "failed": list(symbols)}
    price_data_asof = latest_bar_date(price_data) if price_data else None

    if not _US_CACHE_AVAILABLE:
        _set_progress(3, f"Fetching {total} symbols via yfinance (no cache)…")
        for i, sym in enumerate(symbols):
            pct = 3 + int((i / total) * 52)
            _set_progress(pct, f"[yfinance] {sym} ({i+1}/{total})")
            try:
                df = yf.Ticker(sym).history(period="2y")
                price_data[sym]         = df if not df.empty else None
                fetch_report["fetched"] += 1
            except Exception:
                price_data[sym] = None
                fetch_report["failed"].append(sym)

    # ── Step 2: Analyse each symbol ─────────────────────────────────────────
    _set_progress(57, "Analysing HH-HL patterns…")
    passing = []

    for sym in symbols:
        raw    = price_data.get(sym)
        # Pass raw df directly — _analyse_us() calls _normalise_df internally.
        # Pre-normalising here caused double _normalise_df (here + inside _analyse_us).
        sector = sec_map.get(sym, "N/A")
        res    = _analyse_us(sym, raw, sector, bench_close)
        if res:
            passing.append(res)

    # ── Step 3: Sector enrichment — only for passing symbols ─────────────────
    # Wikipedia/CSV already warmed cache; this loop only hits yf for unknowns.
    pass_total = len(passing)
    _set_progress(60, f"Enriching sector for {pass_total} qualifying stocks…")

    for i, res in enumerate(passing):
        sym = res["symbol"]
        pct = 60 + int((i / max(pass_total, 1)) * 28)
        _set_progress(pct, f"Sector: {sym} ({i+1}/{pass_total})")
        # Only call yf if sector is still N/A (CSV/Wikipedia already set it)
        if res["sector"] == "N/A":
            res["sector"] = _get_sector_cached(sym, sector_cache)
        elif sym not in sector_cache:
            # Warm cache from already-known sector (free, no yf call)
            sector_cache[sym] = {"sector": res["sector"], "fetched": date.today().isoformat()}
        stocks.append(res)

    _save_sector_cache(sector_cache)

    # ── Step 4: Sort + persist ───────────────────────────────────────────────
    stocks.sort(key=lambda x: x['rs'], reverse=True)
    last_run  = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    ts_stamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    rs90_count = sum(1 for s in stocks if s['rs'] >= 110.0)  # RS ratio×100 ≥ 110

    for s in stocks:
        sym   = s["symbol"]
        entry = {
            "date":            date.today().isoformat(),
            "rs":              s["rs"],
            "rs_change":       s["rs_change"],
            "sl_percent":      s["sl_percent"],
            "ema_dist":        s["ema_dist"],
            "bars_since_pivot": s["bars_since_pivot"],
        }
        hist = ticker_stats.get(sym, [])
        hist.append(entry)
        ticker_stats[sym] = hist[-5:]

    print(f"[CACHE] HHHL US — price data source summary")
    print(f"  Total:       {total}")
    print(f"  From cache:  {fetch_report.get('from_cache', 0)}")
    print(f"  From yf:     {fetch_report.get('fetched', 0)}")
    print(f"  Failed:      {len(fetch_report.get('failed', []))}")
    print(f"  Passed scan: {len(stocks)}")

    payload = {
        "last_run":   last_run,
        "source":     source_name,
        "count":      len(stocks),
        "rs90_count": rs90_count,
        "cache_hits":      fetch_report.get("from_cache", 0),
        "yf_fetches":      fetch_report.get("fetched", 0),
        "failed":          len(fetch_report.get("failed", [])),
        "price_data_asof": price_data_asof,
        "stocks":     stocks,
    }

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    hist_file = os.path.join(HISTORY_DIR, f"us_hhhl_scan_{ts_stamp}.json")
    with open(hist_file, 'w') as f:
        json.dump({"timestamp": last_run, "source": source_name,
                   "count": len(stocks), "rs90_count": rs90_count, "stocks": stocks}, f)
    for old_f in sorted(glob.glob(os.path.join(HISTORY_DIR, 'us_hhhl_scan_*.json')))[:-5]:
        try: os.remove(old_f)
        except Exception: pass

    with open(TICKER_STATS, 'w') as f:
        json.dump(ticker_stats, f)

    with _lock:
        _progress.update({
            "pct": 100, "msg": f"Done — {len(stocks)} stocks passed",
            "running": False,
            "cache_hits": fetch_report.get("from_cache", 0),
            "yf_fetches": fetch_report.get("fetched", 0),
            "failed":     len(fetch_report.get("failed", [])),
        })

# ── Routes ────────────────────────────────────────────────────────────────────
@hh_hl_us_bp.route("/hh-hl-us/progress")
def hhhl_us_progress():
    with _lock:
        return jsonify(dict(_progress))

@hh_hl_us_bp.route("/hh-hl-us", methods=["GET", "POST"])
def hh_hl_us_view():
    global _progress
    summary_message = None

    filepath, source_name, is_default = _get_active_source()

    if request.method == "POST":
        file = request.files.get('file')
        if file and file.filename:
            fname = secure_filename(file.filename)
            ext   = os.path.splitext(fname)[1].lower()
            saved = f"uploaded_us_tickers{ext}"
            fpath = os.path.join(UPLOAD_FOLDER, saved)
            file.save(fpath)
            with open(LAST_CSV_CONFIG, 'w') as cf:
                json.dump({"last_used_csv": saved}, cf)
            filepath, source_name, is_default = _get_active_source()

        if request.form.get('use_default') == '1':
            if os.path.exists(LAST_CSV_CONFIG):
                os.remove(LAST_CSV_CONFIG)
            filepath, source_name, is_default = _get_active_source()

        if request.form.get('clear_source') == '1':
            if os.path.exists(LAST_CSV_CONFIG):
                os.remove(LAST_CSV_CONFIG)
            filepath, source_name, is_default = _get_active_source()
            summary_message = "✅ Source cleared. Using S&P 500 default."

        if not _progress.get("running") and 'clear_source' not in request.form:
            sector_cache = _load_sector_cache()

            # Build stock_items list
            if is_default:
                stock_items = _get_default_symbols(sector_cache)
                _save_sector_cache(sector_cache)   # persist Wikipedia warm data
            else:
                stock_items = []
                if os.path.exists(filepath):
                    try:
                        ext   = os.path.splitext(filepath)[1].lower()
                        df_in = pd.read_excel(filepath) if ext in ('.xlsx','.xls') else pd.read_csv(filepath)
                        df_in.columns = df_in.columns.str.strip().str.lower()
                        sym_col = 'symbol' if 'symbol' in df_in.columns else df_in.columns[0]
                        sec_col = 'sector' if 'sector' in df_in.columns else None
                        today   = date.today().isoformat()
                        for _, row in df_in.iterrows():
                            sym    = str(row[sym_col]).strip().upper().replace('.', '-')
                            sector = str(row[sec_col]).strip() if sec_col and pd.notna(row.get(sec_col)) else "N/A"
                            stock_items.append({"symbol": sym, "sector": sector})
                            if sector != "N/A":
                                sector_cache[sym] = {"sector": sector, "fetched": today}
                        _save_sector_cache(sector_cache)
                    except Exception as e:
                        summary_message = f"❌ File read error: {e}"

            if stock_items:
                with _lock:
                    _progress.update({"pct": 0, "msg": "Starting…", "running": True,
                                      "cache_hits": 0, "yf_fetches": 0, "failed": 0})
                t = threading.Thread(
                    target=_run_scan, args=(stock_items, source_name), daemon=True)
                t.start()
                summary_message = summary_message or \
                    f"🔄 Scan started — {len(stock_items)} tickers queued."

    # ── Load cached results ──────────────────────────────────────────────────
    stocks, last_run_time, cached_source = [], "Never", source_name
    rs90_count = cache_hits = yf_fetches = failed_count = 0
    price_data_asof = None

    if os.path.exists(RESULTS_JSON):
        try:
            cached = json.load(open(RESULTS_JSON))
            if isinstance(cached, dict):
                stocks        = cached.get("stocks", [])
                last_run_time = cached.get("last_run", "Unknown")
                cached_source = cached.get("source", source_name)
                rs90_count    = cached.get("rs90_count", 0)
                cache_hits    = cached.get("cache_hits", 0)
                yf_fetches    = cached.get("yf_fetches", 0)
                failed_count      = cached.get("failed", 0)
                price_data_asof   = cached.get("price_data_asof")
            else:
                stocks = cached
        except Exception:
            pass

    # Schema normalisation
    for s in stocks:
        s.setdefault("hh_confirmed",    False)
        s.setdefault("ema_dist",        0.0)
        s.setdefault("ma200",           0.0)
        s.setdefault("rs_change",       0.0)
        s.setdefault("rs_trend",        "Steady")
        s.setdefault("sector",          "N/A")
        s.setdefault("bars_since_pivot", 0)
        s.setdefault("is_fresh",        False)

    # ── Sector breakdown ─────────────────────────────────────────────────────
    sector_counts = {}
    for s in stocks:
        sec = s.get('sector', 'N/A')
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    sorted_secs = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)
    top_5 = [s[0] for s in sorted_secs[:5] if s[0] != 'N/A']

    palette = [
        {"bg":"rgba(16,185,129,.15)","text":"#34d399","badge":"#10b981","border":"rgba(16,185,129,.3)"},
        {"bg":"rgba(59,130,246,.15)","text":"#60a5fa","badge":"#3b82f6","border":"rgba(59,130,246,.3)"},
        {"bg":"rgba(168,85,247,.15)","text":"#c084fc","badge":"#a855f7","border":"rgba(168,85,247,.3)"},
        {"bg":"rgba(245,158,11,.15)","text":"#fbbf24","badge":"#f59e0b","border":"rgba(245,158,11,.3)"},
        {"bg":"rgba(244,63,94,.15)","text":"#f472b6","badge":"#f43f5e","border":"rgba(244,63,94,.3)"},
    ]
    top_sectors_meta = []
    sector_color_map = {}
    for idx, sec in enumerate(top_5):
        theme = palette[idx]
        sector_color_map[sec] = theme
        top_sectors_meta.append({"name": sec, "count": sector_counts[sec], "theme": theme})

    # ── Scan history ─────────────────────────────────────────────────────────
    history = []
    for hf in sorted(glob.glob(os.path.join(HISTORY_DIR, 'us_hhhl_scan_*.json')), reverse=True)[:5]:
        try:
            h = json.load(open(hf))
            h['_file'] = os.path.basename(hf)
            history.append(h)
        except Exception:
            pass

    # ── Per-ticker stats ─────────────────────────────────────────────────────
    ticker_stats = {}
    if os.path.exists(TICKER_STATS):
        try:
            ticker_stats = json.load(open(TICKER_STATS))
        except Exception:
            pass

    with _lock:
        scan_running = _progress.get("running", False)

    return render_template(
        "hh_hl_us.html",
        stocks=stocks,
        summary_message=summary_message,
        last_file=source_name,
        is_default=is_default,
        last_run_time=last_run_time,
        top_sectors=top_sectors_meta,
        sector_color_map=sector_color_map,
        history=history,
        ticker_stats=ticker_stats,
        rs90_count=rs90_count,
        scan_count=len(stocks),
        scan_running=scan_running,
        cache_hits=cache_hits,
        yf_fetches=yf_fetches,
        failed_count=failed_count,
        price_data_asof=price_data_asof,
    )

@hh_hl_us_bp.route("/hh-hl-us/restore/<filename>")
def hhhl_us_restore(filename):
    fpath = os.path.join(HISTORY_DIR, filename)
    if not os.path.exists(fpath):
        return "Not found", 404
    data = json.load(open(fpath))
    with open(RESULTS_JSON, 'w') as f:
        json.dump(data, f)
    return "", 204

@hh_hl_us_bp.route("/export-hhhl-us")
def export_hhhl_us():
    if os.path.exists(RESULTS_JSON):
        cached = json.load(open(RESULTS_JSON))
        data   = cached.get("stocks", []) if isinstance(cached, dict) else cached
        df     = pd.DataFrame(data)
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname  = f"HHHL_US_{ts}.csv"
        out    = os.path.join(UPLOAD_FOLDER, fname)
        df.to_csv(out, index=False)
        return send_file(out, as_attachment=True, download_name=fname)
    return "No data to export", 404