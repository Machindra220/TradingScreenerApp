import os
import json
import glob
import threading
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Blueprint, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename
from datetime import datetime

hh_hl_bp = Blueprint("hh_hl_india", __name__)

# ── Path anchoring ───────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER   = os.path.join(_PROJECT_ROOT, 'uploads', 'india_hhhl')
DATA_FOLDER     = os.path.join(_PROJECT_ROOT, 'data')
RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'last_india_hhhl_results.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
HISTORY_DIR     = os.path.join(UPLOAD_FOLDER, 'history_cache')
TICKER_STATS    = os.path.join(UPLOAD_FOLDER, 'ticker_stats.json')
# Sector cache lives in instance/ alongside market_data_ind.db / market_data_us.db
# so ALL screener pages can share it — do not move this to a page-specific folder.
SECTOR_CACHE     = os.path.join(_PROJECT_ROOT, 'instance', 'sector_cache.json')
_SECTOR_TTL_DAYS = 15  # sectors rarely change; 15-day warm cache is safe
DEFAULT_CSV     = os.path.join(DATA_FOLDER, 'nifty_500.csv')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(_PROJECT_ROOT, 'instance'), exist_ok=True)  # shared cache dir
os.makedirs(HISTORY_DIR,   exist_ok=True)
os.makedirs(DATA_FOLDER,   exist_ok=True)

# ── Shared market-data cache (IND) ───────────────────────────────────────────
# Import lazily so the blueprint doesn't crash if the service isn't wired yet.
def _get_cache():
    try:
        from app.services.market_data_cache import get_price_history_bulk
        return get_price_history_bulk
    except ImportError:
        return None

# ── Progress tracking ────────────────────────────────────────────────────────
_progress = {"pct": 0, "msg": "Idle", "running": False,
             "cache_hits": 0, "yf_fetches": 0, "failed": 0}
_lock = threading.Lock()

def _set_progress(pct, msg):
    with _lock:
        _progress["pct"]  = pct
        _progress["msg"]  = msg

# ── Built-in Nifty 500 fallback ──────────────────────────────────────────────
NIFTY500_TICKERS = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","SBIN","BHARTIARTL",
    "ITC","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN","BAJFINANCE",
    "HCLTECH","SUNPHARMA","ULTRACEMCO","WIPRO","NESTLEIND","POWERGRID","NTPC",
    "ONGC","JSWSTEEL","TECHM","INDUSINDBK","TATAMOTORS","TATACONSUM","COALINDIA",
    "DRREDDY","BAJAJFINSV","HINDALCO","BPCL","DIVISLAB","GRASIM","CIPLA","ADANIPORTS",
    "BRITANNIA","EICHERMOT","HEROMOTOCO","APOLLOHOSP","ADANIENT","TATAPOWER",
    "PIDILITIND","HAVELLS","BERGEPAINT","MUTHOOTFIN","GODREJCP","DABUR",
    "MARICO","COLPAL","SHREECEM","AMBUJACEM","ACC","LUPIN","TORNTPHARM",
    "BIOCON","AUROPHARMA","IPCALAB","GLENMARK","ALKEM","ABBOTINDIA",
    "SIEMENS","ABB","BHEL","CUMMINSIND","THERMAX","VOLTAS","BLUESTARCO",
    "MCDOWELL-N","RADICO","UNITDSPR","TRENT","VMART","DMART","ZOMATO",
    "NYKAA","PAYTM","IRCTC","INDIGO","SPICEJET","GMRINFRA","CONCOR",
    "ASTRAL","SUPREMEIND","FINOLEX","JSWENERGY","TATAELXSI","MPHASIS",
    "LTTS","PERSISTENT","COFORGE","OFSS","KPIT","CYIENT","MASTEK",
    "BANKBARODA","PNB","CANBK","UNIONBANK","FEDERALBNK","IDFCFIRSTB",
    "AUBANK","RBLBANK","DCBBANK","KARURVYSYA","UJJIVANSFB","EQUITASBNK",
]

def _get_default_symbols():
    if os.path.exists(DEFAULT_CSV):
        try:
            df = pd.read_csv(DEFAULT_CSV)
            df.columns = df.columns.str.strip().str.lower()
            if 'symbol' in df.columns:
                return df['symbol'].dropna().unique().tolist()
        except Exception:
            pass
    return NIFTY500_TICKERS

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
    return DEFAULT_CSV, "Nifty 500 (Default)", True

# ── Sector cache (JSON file, TTL = _SECTOR_TTL_DAYS) ─────────────────────────
# Stores: { "RELIANCE.NS": {"sector": "Energy", "fetched": "2026-08-12"}, ... }
# This avoids repeated yf.Ticker.info calls across scans for the same symbol.

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
        print(f"[HHHL] sector cache save error: {e}")

def _get_sector_cached(yf_sym, sector_cache):
    """
    Returns sector string for yf_sym.
    Checks in-memory sector_cache dict first (TTL = _SECTOR_TTL_DAYS).
    Only calls yf.Ticker.info if cache is missing or stale.
    Updates sector_cache in-place so caller can persist it once after the loop.
    """
    from datetime import date, timedelta
    today = date.today().isoformat()
    entry = sector_cache.get(yf_sym)
    if entry:
        fetched = entry.get("fetched", "")
        try:
            age = (date.today() - date.fromisoformat(fetched)).days
            if age < _SECTOR_TTL_DAYS:
                return entry.get("sector", "N/A")
        except Exception:
            pass
    # Cache miss or stale — fetch from yfinance
    try:
        t = yf.Ticker(yf_sym)
        s = t.info.get('sector', 'N/A') or 'N/A'
    except Exception:
        s = 'N/A'
    sector_cache[yf_sym] = {"sector": s, "fetched": today}
    return s

# ── DataFrame column normaliser (handles single-ticker and MultiIndex bulk) ───
def _normalise_df(df, sym=None):
    """
    Handles all three DataFrame formats returned by yfinance / cache:
      - yf.Ticker(sym).history()     → simple string cols e.g. 'Close'
      - yf.download([sym1, sym2...]) → MultiIndex tuple cols e.g. ('Close','TCS.NS')
      - cache bulk fetch             → either of the above depending on implementation
    Returns a clean single-level DataFrame with Title-cased OHLCV columns, or None.
    """
    if df is None or df.empty:
        return None
    cols = df.columns
    if isinstance(cols, pd.MultiIndex) or (len(cols) > 0 and isinstance(cols[0], tuple)):
        if sym is not None:
            for cand in [sym, sym.replace('.NS',''), sym + '.NS']:
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

# ── HH-HL analysis on a pre-fetched DataFrame ────────────────────────────────
def _analyse(symbol, df, sector):
    """
    Runs all HH-HL + 200-SMA logic on an already-fetched OHLCV DataFrame.
    Returns a result dict or None if the stock doesn't qualify.
    DataFrame must have columns: Open, High, Low, Close, Volume (yfinance style).
    """
    try:
        if df is None or df.empty or len(df) < 200:
            return None

        # _normalise_df handles simple-string, MultiIndex, and tuple columns
        df = _normalise_df(df, symbol if symbol.endswith('.NS') else f"{symbol}.NS")
        if df is None or df.empty:
            return None

        close        = df['Close']
        curr_price   = float(close.iloc[-1])
        ma200_series = close.rolling(window=200).mean()
        ma200        = float(ma200_series.iloc[-1])

        if pd.isna(ma200) or ma200 == 0:
            return None

        # ── Filter 1: ±10% of 200-SMA ──────────────────────────────────────
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

        # ── Filter 2 & 3: Higher Low + price above swing low ───────────────
        if not (last_swing_low > prev_swing_low and curr_price > last_swing_low):
            return None

        # ── Swing-high detection for HH confirmation ───────────────────────
        df['is_high'] = (
            (df['High'] > df['High'].shift(1)) &
            (df['High'] > df['High'].shift(2)) &
            (df['High'] > df['High'].shift(-1)) &
            (df['High'] > df['High'].shift(-2))
        )
        highs_df = df[df['is_high']]
        hh_confirmed = False
        if len(highs_df) >= 2:
            hh_confirmed = float(highs_df['High'].iloc[-1]) > float(highs_df['High'].iloc[-2])

        # ── Metrics ────────────────────────────────────────────────────────
        high_52w    = float(df['High'].max())
        retracement = round(((high_52w - curr_price) / high_52w) * 100, 2)
        rs_score    = round(curr_price / ma200, 2)
        sl_percent  = round(((curr_price - last_swing_low) / curr_price) * 100, 2)
        ema_dist    = round(((curr_price - ma200) / ma200) * 100, 2)

        # 20D RS change
        rs_change = 0.0
        if len(close) >= 21:
            ma200_clean = ma200_series.dropna()
            if len(ma200_clean) >= 21:
                price_20d  = float(close.iloc[-20])
                ma200_20d  = float(ma200_series.iloc[-20])
                if ma200_20d and not pd.isna(ma200_20d):
                    rs_20d_ago = price_20d / ma200_20d
                    rs_change  = round(((rs_score - rs_20d_ago) / rs_20d_ago) * 100, 1)

        rs_trend = ("Accelerating" if rs_change > 3.0
                    else "Steady"  if rs_change >= 0
                    else "Fading")

        return {
            "symbol":         symbol,
            "sector":         sector,
            "price":          round(curr_price, 2),
            "ma200":          round(ma200, 2),
            "ema_dist":       ema_dist,
            "last_swing_low": round(last_swing_low, 2),
            "sl_percent":     sl_percent,
            "retracement":    retracement,
            "rs":             rs_score,
            "rs_trend":       rs_trend,
            "rs_change":      rs_change,
            "hh_confirmed":   hh_confirmed,
        }
    except Exception as e:
        print(f"[HHHL] _analyse error {symbol}: {e}")
        return None

# ── Scan worker ───────────────────────────────────────────────────────────────
def _run_scan(symbols, source_name):
    """
    1. Bulk-fetch price history via shared SQLite cache (market_data_ind.db).
       Falls back to individual yf.Ticker calls if cache service unavailable.
    2. Fetch sector only for symbols that PASS the HH-HL filter (saves ~90% of
       .info calls).
    3. Reports cache hits / yf fetches in progress and result payload.
    """
    global _progress
    stocks  = []
    total   = len(symbols)

    ticker_stats = {}
    if os.path.exists(TICKER_STATS):
        try:
            ticker_stats = json.load(open(TICKER_STATS))
        except Exception:
            ticker_stats = {}

    # Load sector cache — shared across all passes, persisted after scan
    sector_cache = _load_sector_cache()

    # ── Step 1: Bulk price-history fetch via cache ──────────────────────────
    # Convert bare symbols → .NS suffixed for yfinance
    yf_symbols = [s if s.endswith(".NS") else f"{s}.NS" for s in symbols]
    sym_map    = dict(zip(yf_symbols, symbols))   # yf_sym → bare sym

    price_data  = {}   # yf_sym → DataFrame | None
    fetch_report = {"from_cache": 0, "fetched": 0, "failed": []}

    get_bulk = _get_cache()

    if get_bulk is not None:
        # ── Cache path ──────────────────────────────────────────────────────
        _set_progress(2, f"Loading {total} symbols from cache…")

        def _progress_cb(idx, ttl, sym):
            pct = 2 + int((idx / ttl) * 55)   # 2 → 57% during bulk fetch
            _set_progress(pct, f"[Cache] {sym} ({idx}/{ttl})")

        try:
            price_data, fetch_report = get_bulk(
                yf_symbols,
                period="2y",
                progress_callback=_progress_cb,
            )
        except Exception as e:
            print(f"[HHHL] Cache bulk fetch failed, falling back to yf: {e}")
            get_bulk = None   # trigger fallback below

    if get_bulk is None:
        # ── Fallback: individual yf.Ticker calls (original behaviour) ───────
        _set_progress(2, f"Fetching {total} symbols via yfinance (no cache)…")
        for i, yf_sym in enumerate(yf_symbols):
            pct = 2 + int((i / total) * 55)
            _set_progress(pct, f"[yfinance] {yf_sym} ({i+1}/{total})")
            try:
                df = yf.Ticker(yf_sym).history(period="2y")
                price_data[yf_sym]   = df if not df.empty else None
                fetch_report["fetched"] += 1
            except Exception:
                price_data[yf_sym]   = None
                fetch_report["failed"].append(yf_sym)

    # ── Step 2: Analyse each symbol ─────────────────────────────────────────
    _set_progress(58, "Analysing HH-HL patterns…")
    passing = []   # (bare_sym, yf_sym) tuples that pass the filter

    for i, yf_sym in enumerate(yf_symbols):
        bare = sym_map[yf_sym]
        raw  = price_data.get(yf_sym)
        # Normalise handles simple-column and MultiIndex bulk-fetch formats
        df   = _normalise_df(raw, yf_sym) if raw is not None else None
        # Pass "N/A" sector for now; we'll fill it in for passing symbols only
        res  = _analyse(bare, df, "N/A")
        if res:
            passing.append((bare, yf_sym, res))

    # ── Step 3: Sector lookup — only for passing symbols (avoids bulk .info) ─
    pass_total = len(passing)
    _set_progress(60, f"Fetching sector info for {pass_total} qualifying stocks…")

    for i, (bare, yf_sym, res) in enumerate(passing):
        pct = 60 + int((i / max(pass_total, 1)) * 30)   # 60 → 90%
        _set_progress(pct, f"Sector lookup: {bare} ({i+1}/{pass_total})")
        res["sector"] = _get_sector_cached(yf_sym, sector_cache)
        stocks.append(res)

    # Persist updated sector cache to disk (only stale/new entries were fetched)
    _save_sector_cache(sector_cache)

    # ── Step 4: Sort + persist ───────────────────────────────────────────────
    stocks.sort(key=lambda x: x['rs'], reverse=True)
    last_run = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    ts_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rs90_count = sum(1 for s in stocks if s['rs'] >= 1.15)

    # Update per-ticker stats history
    for s in stocks:
        sym   = s["symbol"]
        entry = {
            "date":       datetime.now().strftime("%Y-%m-%d"),
            "rs":         s["rs"],
            "rs_change":  s["rs_change"],
            "sl_percent": s["sl_percent"],
            "ema_dist":   s["ema_dist"],
        }
        hist = ticker_stats.get(sym, [])
        hist.append(entry)
        ticker_stats[sym] = hist[-5:]

    # Terminal summary (matches other screeners' convention)
    print(f"[CACHE] HHHL India — price data source summary")
    print(f"  Total:       {total}")
    print(f"  From cache:  {fetch_report.get('from_cache', 0)}")
    print(f"  From yf:     {fetch_report.get('fetched', 0)}")
    print(f"  Failed:      {len(fetch_report.get('failed', []))}")
    print(f"  Passed scan: {len(stocks)}")

    payload = {
        "last_run":    last_run,
        "source":      source_name,
        "count":       len(stocks),
        "rs90_count":  rs90_count,
        "cache_hits":  fetch_report.get("from_cache", 0),
        "yf_fetches":  fetch_report.get("fetched", 0),
        "failed":      len(fetch_report.get("failed", [])),
        "stocks":      stocks,
    }

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    # History snapshot (keep 5)
    hist_file = os.path.join(HISTORY_DIR, f"hhhl_scan_{ts_stamp}.json")
    with open(hist_file, 'w') as f:
        json.dump({
            "timestamp":  last_run,
            "source":     source_name,
            "count":      len(stocks),
            "rs90_count": rs90_count,
            "stocks":     stocks,
        }, f)
    for old_f in sorted(glob.glob(os.path.join(HISTORY_DIR, 'hhhl_scan_*.json')))[:-5]:
        try: os.remove(old_f)
        except Exception: pass

    with open(TICKER_STATS, 'w') as f:
        json.dump(ticker_stats, f)

    # Update progress with cache stats for header display
    with _lock:
        _progress.update({
            "pct":       100,
            "msg":       f"Done — {len(stocks)} stocks passed",
            "running":   False,
            "cache_hits": fetch_report.get("from_cache", 0),
            "yf_fetches": fetch_report.get("fetched", 0),
            "failed":     len(fetch_report.get("failed", [])),
        })

# ── Routes ────────────────────────────────────────────────────────────────────
@hh_hl_bp.route("/hh-hl-india/progress")
def hhhl_progress():
    with _lock:
        return jsonify(dict(_progress))

@hh_hl_bp.route("/hh-hl-india", methods=["GET", "POST"])
def hh_hl_view():
    global _progress
    summary_message = None

    filepath, source_name, is_default = _get_active_source()

    if request.method == "POST":
        # File upload
        file = request.files.get('file')
        if file and file.filename:
            fname = secure_filename(file.filename)
            ext   = os.path.splitext(fname)[1].lower()
            saved = f"uploaded_ind_tickers{ext}"
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
            summary_message = "✅ Source cleared. Using Nifty 500 default."

        # Load symbols
        symbols = _get_default_symbols()
        if not is_default and os.path.exists(filepath):
            try:
                ext   = os.path.splitext(filepath)[1].lower()
                df_in = pd.read_excel(filepath) if ext in ('.xlsx','.xls') else pd.read_csv(filepath)
                df_in.columns = df_in.columns.str.strip().str.lower()
                if 'symbol' in df_in.columns:
                    symbols = df_in['symbol'].dropna().unique().tolist()
            except Exception as e:
                summary_message = f"❌ File read error: {e}"

        if not _progress.get("running") and 'clear_source' not in request.form:
            with _lock:
                _progress.update({"pct": 0, "msg": "Starting…", "running": True,
                                   "cache_hits": 0, "yf_fetches": 0, "failed": 0})
            t = threading.Thread(target=_run_scan, args=(symbols, source_name), daemon=True)
            t.start()
            summary_message = summary_message or f"🔄 Scan started — {len(symbols)} tickers queued."

    # ── Load cached results ──────────────────────────────────────────────────
    stocks, last_run_time, cached_source = [], "Never", source_name
    rs90_count = cache_hits = yf_fetches = failed_count = 0
    price_data_date = None

    if os.path.exists(RESULTS_JSON):
        try:
            cached = json.load(open(RESULTS_JSON))
            if isinstance(cached, dict):
                stocks         = cached.get("stocks", [])
                last_run_time  = cached.get("last_run", "Unknown")
                cached_source  = cached.get("source", source_name)
                rs90_count     = cached.get("rs90_count", 0)
                cache_hits     = cached.get("cache_hits", 0)
                yf_fetches     = cached.get("yf_fetches", 0)
                failed_count   = cached.get("failed", 0)
            else:
                stocks = cached
        except Exception:
            pass

    # Schema normalisation — always backfill before passing to template
    for s in stocks:
        s.setdefault("hh_confirmed", False)
        s.setdefault("ema_dist",     0.0)
        s.setdefault("ma200",        0.0)
        s.setdefault("rs_change",    0.0)
        s.setdefault("rs_trend",     "Steady")
        s.setdefault("sector",       "N/A")

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
    for hf in sorted(glob.glob(os.path.join(HISTORY_DIR, 'hhhl_scan_*.json')), reverse=True)[:5]:
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
        "hh_hl_india.html",
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
    )

@hh_hl_bp.route("/hh-hl-india/restore/<filename>")
def hhhl_restore(filename):
    fpath = os.path.join(HISTORY_DIR, filename)
    if not os.path.exists(fpath):
        return "Not found", 404
    data = json.load(open(fpath))
    with open(RESULTS_JSON, 'w') as f:
        json.dump(data, f)
    return "", 204

@hh_hl_bp.route("/export-hhhl")
def export_hhhl():
    if os.path.exists(RESULTS_JSON):
        cached = json.load(open(RESULTS_JSON))
        data   = cached.get("stocks", []) if isinstance(cached, dict) else cached
        df     = pd.DataFrame(data)
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname  = f"HHHL_India_{ts}.csv"
        out    = os.path.join(UPLOAD_FOLDER, fname)
        df.to_csv(out, index=False)
        return send_file(out, as_attachment=True, download_name=fname)
    return "No data to export", 404