"""
trendline_screener.py — Trendline Breakout & 52-Week High Screener

Fixes vs original:
  - Separate results/history JSON per market (US / INDIA never overwrite each other)
  - __file__-anchored paths (not os.getcwd())
  - ind_cache / us_cache bulk fetch (not per-symbol yf.Ticker().history())
  - Background thread + progress polling (non-blocking)
  - Last 5 snapshot history per market with restore
  - Timestamped export filename
  - nifty_500.csv / sp500.csv from data/ (not live URL download)
  - Submit button defer via JS fetch (not synchronous onclick disable)
"""

import os
import json
import uuid
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from scipy.signal import find_peaks
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file

from app.services.market_data_cache import ind_cache, us_cache, latest_bar_date

trendline_bp = Blueprint("trendline_screener", __name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER = os.path.join(_PROJECT_ROOT, 'uploads', 'trendline')
SNAP_DIR      = os.path.join(UPLOAD_FOLDER, 'snapshots')
os.makedirs(SNAP_DIR, exist_ok=True)

MARKET_CFG = {
    "US": {
        "results_json":  os.path.join(UPLOAD_FOLDER, 'trendline_us_results.json'),
        "history_json":  os.path.join(UPLOAD_FOLDER, 'trendline_us_history.json'),
        "default_csv":   os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv'),
        "default_label": "S&P 500 (sp500.csv)",
        "cache":         None,   # set at runtime — us_cache
        "suffix":        "",
        "currency":      "$",
    },
    "INDIA": {
        "results_json":  os.path.join(UPLOAD_FOLDER, 'trendline_india_results.json'),
        "history_json":  os.path.join(UPLOAD_FOLDER, 'trendline_india_history.json'),
        "default_csv":   os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv'),
        "default_label": "Nifty 500 (nifty_500.csv)",
        "cache":         None,   # set at runtime — ind_cache
        "suffix":        ".NS",
        "currency":      "₹",
    },
}
HISTORY_LIMIT = 5

# ── Progress (shared; market field disambiguates) ─────────────────────────────
_lock = threading.Lock()
_PROG = {"active": False, "market": "", "processed": 0,
          "total": 0, "stage": "idle", "error": None}

def _set(**kw):
    with _lock: _PROG.update(kw)

def _get():
    with _lock: return dict(_PROG)


# ── DataFrame normaliser (handles MultiIndex from us_cache bulk fetch) ──────────

def _normalise_df(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """
    Flatten MultiIndex columns that us_cache bulk-fetch may return.
    Memory note: direct df['Close'] on a MultiIndex crashes silently.
    """
    if df is None or df.empty:
        return None
    try:
        if isinstance(df.columns, pd.MultiIndex):
            # Try (Price, Ticker) layout
            if symbol in df.columns.get_level_values(1):
                df = df.xs(symbol, axis=1, level=1)
            elif symbol in df.columns.get_level_values(0):
                df = df.xs(symbol, axis=1, level=0)
            else:
                # Flatten by taking first level
                df = df.droplevel(1, axis=1)
        # Ensure expected columns exist
        if 'Close' not in df.columns:
            return None
        # Fill missing OHLV from Close if needed
        for col in ['Open', 'High', 'Low']:
            if col not in df.columns:
                df[col] = df['Close']
        if 'Volume' not in df.columns:
            df['Volume'] = 0
        return df.copy()
    except Exception as e:
        print(f"[trendline] _normalise_df error for {symbol}: {e}")
        return None


# ── Indicator helpers ──────────────────────────────────────────────────────────

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    delta    = np.diff(prices)
    gain     = np.where(delta > 0, delta, 0.0)
    loss     = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def detect_trendline_breakout(df):
    """
    Fits a linear resistance trendline through recent swing highs.
    Returns (is_breakout, trendline_val, slope, vol_ratio, rsi, high_confidence).
    """
    if len(df) < 60:
        return False, 0.0, 0.0, 1.0, 50.0, False

    highs   = df['High'].values
    closes  = df['Close'].values
    volumes = df['Volume'].values
    x_ticks = np.arange(len(df))

    peaks, _ = find_peaks(highs, distance=12, prominence=highs.mean() * 0.01)
    recent_peaks = [p for p in peaks if (len(df) - p) <= 180]
    if len(recent_peaks) < 2:
        return False, 0.0, 0.0, 1.0, 50.0, False

    peak_x = x_ticks[recent_peaks]
    peak_y = highs[recent_peaks]
    slope, intercept = np.polyfit(peak_x, peak_y, 1)
    # Allow flat (≈0) and slightly declining resistance lines.
    # Only reject strongly RISING slopes — those are support lines, not resistance.
    # Threshold: slope > 0.05% of mean high per bar = clearly ascending, not resistance.
    slope_threshold = highs.mean() * 0.0005
    if slope > slope_threshold:
        return False, 0.0, 0.0, 1.0, 50.0, False

    today_idx     = len(df) - 1
    yesterday_idx = len(df) - 2
    tl_today      = slope * today_idx + intercept
    tl_yesterday  = slope * yesterday_idx + intercept

    is_breakout = (closes[yesterday_idx] <= tl_yesterday and
                   closes[today_idx] > tl_today)
    if not is_breakout:
        return False, 0.0, 0.0, 1.0, 50.0, False

    today_vol    = volumes[-1]
    avg_20d_vol  = volumes[-21:-1].mean() if len(volumes) >= 21 else volumes.mean()
    avg_20d_vol  = max(avg_20d_vol, 1.0)
    peak_vol_avg = volumes[recent_peaks].mean() if len(recent_peaks) else avg_20d_vol

    vol_confirmed     = (today_vol > avg_20d_vol * 1.5 and
                         today_vol >= peak_vol_avg * 0.9)
    vol_ratio         = round(today_vol / avg_20d_vol, 2)
    rsi               = calculate_rsi(closes, period=14)
    momentum_ok       = 55 <= rsi <= 72
    high_confidence   = vol_confirmed and momentum_ok

    return True, round(tl_today, 2), round(slope, 4), vol_ratio, rsi, high_confidence


def check_52w_breakout(df):
    closes = df['Close'].values
    highs  = df['High'].values
    curr   = closes[-1]
    # Use up to 252 bars; if less data available use what we have (min 60)
    lookback = min(252, len(highs) - 1)
    if lookback < 60:
        return False, 0.0
    hi52 = highs[-lookback:-1].max()
    # Within 1.5% of the prior high = near or at a new annual high
    return curr >= hi52 * 0.985, round(hi52, 2)


# ── Per-symbol analysis ────────────────────────────────────────────────────────

def highs_mean_proxy(df) -> float:
    """Quick mean of highs for slope threshold calculation."""
    try:
        return float(df['High'].mean())
    except Exception:
        return float(df['Close'].mean())


def check_horizontal_breakout(df):
    """
    Detect breakout above a horizontal resistance zone.
    Resistance = highest close in the 20–60 bar window before today.
    If today's close > that resistance AND volume is elevated, it's a breakout.
    This catches stocks that test the same price level multiple times
    (which don't show a descending trendline but are equally valid breakouts).
    """
    if len(df) < 30:
        return False, 0.0, 1.0

    closes  = df['Close'].values
    volumes = df['Volume'].values

    # Resistance = max close in the 20-60 bar lookback window (before today)
    window_start = max(0, len(closes) - 61)
    window_end   = len(closes) - 1   # exclude today
    if window_end - window_start < 15:
        return False, 0.0, 1.0

    resistance = float(closes[window_start:window_end].max())
    curr       = float(closes[-1])
    prev       = float(closes[-2])

    # Breakout: today's close exceeded the prior 60-bar high AND moved up today.
    # We removed the "prev must be near resistance" gate — a stock can be
    # well below resistance for weeks then gap above it in one session.
    if not (curr > resistance and curr > prev):
        return False, resistance, 1.0

    avg_vol   = float(volumes[-21:-1].mean()) if len(volumes) >= 21 else float(volumes.mean())
    avg_vol   = max(avg_vol, 1.0)
    vol_ratio = round(float(volumes[-1]) / avg_vol, 2)
    return True, round(resistance, 2), vol_ratio


def _analyse(yf_sym: str, df: pd.DataFrame, suffix: str, market: str = "US") -> dict | None:
    """Run both checks on a single normalised DataFrame."""
    # Normalise MultiIndex columns (us_cache bulk fetch can return MultiIndex)
    df = _normalise_df(df, yf_sym)
    if df is None or len(df) < 100:
        return None

    # Normalise tz
    if getattr(df.index, 'tz', None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)

    curr_price = float(df['Close'].iloc[-1])

    # Descending trendline breakout — useful for INDIA; for US most stocks are
    # in uptrends so peaks have positive slope and this check always rejects them.
    # For US, rely on horizontal breakout and 52W high instead.
    has_tl, tl_val, tl_slope, vol_ratio, rsi, high_conf = detect_trendline_breakout(df)
    if market == "US" and has_tl:
        # Only keep if it's genuinely a downtrend breakout for US
        if tl_slope > -highs_mean_proxy(df) * 0.0002:
            has_tl = False  # too flat to count as descending-TL for US

    has_52w, past_high = check_52w_breakout(df)

    # Horizontal resistance breakout as supplementary / primary check for US
    has_horiz, horiz_level, horiz_vol_ratio = check_horizontal_breakout(df)

    # Near 52W high with positive momentum (within 3% of annual high, RSI > 50)
    # Catches stocks that are strong leaders but haven't printed a NEW high today
    closes_arr = df['Close'].values
    highs_arr  = df['High'].values
    hi52_all   = float(highs_arr[-252:-1].max()) if len(highs_arr) >= 252 else float(highs_arr[:-1].max())
    curr_c     = float(closes_arr[-1])
    near_52wh  = (curr_c >= hi52_all * 0.97 and curr_c > float(closes_arr[-2]))

    # Accept if any breakout type triggers
    if not has_tl and not has_52w and not has_horiz and not near_52wh:
        return None

    # Use horizontal vol_ratio if descending trendline check didn't fire
    if not has_tl and has_horiz:
        vol_ratio = horiz_vol_ratio
        tl_val    = horiz_level
    if not rsi:  # rsi defaults to 50 when tl not triggered
        closes = df['Close'].values
        rsi = calculate_rsi(closes, period=14)

    # Clean symbol display (strip exchange suffix)
    display_sym = yf_sym
    if suffix and display_sym.endswith(suffix):
        display_sym = display_sym[:-len(suffix)]

    return {
        "symbol":              display_sym,
        "price":               round(curr_price, 2),
        "has_trendline_break": has_tl or has_horiz,
        "trendline_value":     tl_val,
        "trendline_slope":     tl_slope,
        "volume_ratio":        vol_ratio,
        "rsi":                 rsi,
        "high_confidence":     high_conf,
        "has_52w_break":       has_52w or near_52wh,
        "past_52w_high":       past_high if (has_52w or near_52wh) else (horiz_level if has_horiz else 0.0),
    }


# ── Background scan ────────────────────────────────────────────────────────────

def _load_symbols(market: str) -> list[str]:
    cfg  = MARKET_CFG[market]
    path = cfg["default_csv"]
    if not os.path.exists(path):
        return []
    try:
        df   = pd.read_csv(path)
        cols = {c.lower().strip(): c for c in df.columns}
        col  = next((cols[k] for k in ('symbol','ticker','symbols') if k in cols), None)
        if not col: return []
        out = []
        for s in df[col].dropna().unique():
            raw = str(s).strip().upper()
            # Strip $ prefix (some CSVs store symbols as $AAPL)
            raw = raw.lstrip('$')
            # yfinance uses - not . for sub-classes (BRK.B -> BRK-B)
            raw = raw.replace('.', '-')
            if raw and raw not in ('SYMBOL', 'TICKER', 'N/A', ''):
                out.append(f"{raw}{cfg['suffix']}")
        return out
    except Exception as e:
        print(f"[Trendline] load_symbols error: {e}")
        return []


def _format_section(rows: list) -> list:
    if not rows: return []
    df = pd.DataFrame(rows)
    df.sort_values(["high_confidence","volume_ratio"], ascending=[False,False], inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    return df.to_dict(orient="records")


def run_scan(market: str, source_name: str):
    try:
        _run_scan_inner(market, source_name)
    except Exception as e:
        import traceback
        print(f"[Trendline/{market}] FATAL THREAD ERROR: {e}")
        traceback.print_exc()
        _set(active=False, stage="error", error=str(e)[:120])


def _run_scan_inner(market: str, source_name: str):
    cfg   = MARKET_CFG[market]
    cache = us_cache if market == "US" else ind_cache
    suffix = cfg["suffix"]

    _set(active=True, market=market, processed=0, total=0, stage="loading", error=None)
    print(f"[Trendline/{market}] Starting scan — market={market} suffix='{suffix}'")

    yf_symbols = _load_symbols(market)
    print(f"[Trendline/{market}] Symbols loaded: {len(yf_symbols)}, first 3: {yf_symbols[:3]}")
    if not yf_symbols:
        _set(active=False, stage="error", error=f"Could not load symbols for {market}")
        return

    _set(total=len(yf_symbols), stage="fetching")

    price_data, fetch_report = cache.get_price_history_bulk(
        yf_symbols, interval="1d", lookback_days=400,
        progress_callback=lambda i, t, s: _set(processed=i, total=t)
    )
    price_data_asof = latest_bar_date(price_data)
    _ch, _yf = fetch_report["from_cache"], fetch_report["fetched"]
    print(f"[Trendline/{market}] {len(yf_symbols)} syms | Cache:{_ch} | YF:{_yf}")

    _set(stage="screening", processed=0)
    both, tl_only, hi52_only = [], [], []

    # Debug counters
    _dbg = {"none_from_cache": 0, "too_short": 0, "no_breakout": 0, "passed": 0}

    for i, yf_sym in enumerate(yf_symbols):
        _set(processed=i)
        raw_df = price_data.get(yf_sym)
        if raw_df is None:
            _dbg["none_from_cache"] += 1
            continue
        if len(raw_df) < 100:
            _dbg["too_short"] += 1
            continue
        res = _analyse(yf_sym, raw_df, suffix, market)
        if not res:
            _dbg["no_breakout"] += 1
            continue
        _dbg["passed"] += 1
        if res["has_trendline_break"] and res["has_52w_break"]:
            both.append(res)
        elif res["has_trendline_break"]:
            tl_only.append(res)
        else:
            hi52_only.append(res)

    print(f"[Trendline/{market}] Screening debug: "
          f"none_from_cache={_dbg['none_from_cache']} "
          f"too_short={_dbg['too_short']} "
          f"no_breakout={_dbg['no_breakout']} "
          f"passed={_dbg['passed']}")

    sections = {
        "both":         _format_section(both),
        "tl_only":      _format_section(tl_only),
        "high_52w_only": _format_section(hi52_only),
    }
    last_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    snap_file = f"trendline_{market.lower()}_{uuid.uuid4().hex}.json"

    payload = {
        "sections":        sections,
        "time":            last_time,
        "market":          market,
        "source":          source_name,
        "scanned_count":   len(yf_symbols),
        "passed_count":    len(both) + len(tl_only) + len(hi52_only),
        "price_data_asof": price_data_asof,
        "cache_hits":      _ch,
        "yf_fetches":      _yf,
    }

    with open(os.path.join(SNAP_DIR, snap_file), 'w') as f:
        json.dump(payload, f)
    with open(cfg["results_json"], 'w') as f:
        json.dump(payload, f)

    # History (last 5 per market)
    history = _load_history(market)
    history.insert(0, {
        "time":            last_time,
        "source":          source_name,
        "count":           payload["passed_count"],
        "scanned_count":   len(yf_symbols),
        "price_data_asof": price_data_asof,
        "snapshot_file":   snap_file,
    })
    history = history[:HISTORY_LIMIT]
    keep = {h["snapshot_file"] for h in history if h.get("snapshot_file")}
    for f in os.listdir(SNAP_DIR):
        if f.startswith(f"trendline_{market.lower()}_") and f not in keep:
            try: os.remove(os.path.join(SNAP_DIR, f))
            except OSError: pass
    with open(cfg["history_json"], 'w') as f:
        json.dump(history, f)

    _set(active=False, stage="done")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_results(market):
    path = MARKET_CFG[market]["results_json"]
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except (json.JSONDecodeError, OSError): pass
    return {}

def _load_history(market):
    path = MARKET_CFG[market]["history_json"]
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except (json.JSONDecodeError, OSError): pass
    return []


# ── Routes ─────────────────────────────────────────────────────────────────────

@trendline_bp.route("/trendline-scan", methods=["GET", "POST"])
def trendline_scan_process():
    market = request.args.get("market", "US").upper()
    if market not in MARKET_CFG: market = "US"

    if request.method == "POST":
        market      = request.form.get("market", "US").upper()
        source_name = MARKET_CFG[market]["default_label"]
        prog        = _get()
        if not prog["active"]:
            t = threading.Thread(target=run_scan, args=(market, source_name), daemon=True)
            t.start()
        return redirect(url_for("trendline_screener.trendline_scan_process",
                                market=market, scanning=1))

    data     = _load_results(market)
    sections = data.get("sections", {"both": [], "tl_only": [], "high_52w_only": []})
    history  = _load_history(market)
    prog     = _get()
    is_scan  = prog["active"] and prog["market"] == market

    return render_template(
        "trendline_screener.html",
        both_stocks       = sections.get("both", []),
        tl_stocks         = sections.get("tl_only", []),
        high_52w_stocks   = sections.get("high_52w_only", []),
        last_processed_time = data.get("time"),
        source_name       = data.get("source", ""),
        scanned_count     = data.get("scanned_count", 0),
        passed_count      = data.get("passed_count", 0),
        price_data_asof   = data.get("price_data_asof"),
        cache_hits        = data.get("cache_hits", 0),
        yf_fetches        = data.get("yf_fetches", 0),
        market            = market,
        history           = history,
        is_scanning       = is_scan,
        scan_error        = prog.get("error") if not prog["active"] else None,
        restored          = request.args.get("restored") == "1",
        currency          = MARKET_CFG[market]["currency"],
    )


@trendline_bp.route("/trendline-scan/progress")
def trendline_progress():
    return jsonify(_get())


@trendline_bp.route("/trendline-scan/restore/<snapshot_file>", methods=["POST"])
def trendline_restore(snapshot_file):
    market    = request.form.get("market", "US").upper()
    safe      = os.path.basename(snapshot_file)
    snap_path = os.path.join(SNAP_DIR, safe)
    if not os.path.exists(snap_path):
        return redirect(url_for("trendline_screener.trendline_scan_process", market=market))
    try:
        with open(snap_path) as f: payload = json.load(f)
        with open(MARKET_CFG[market]["results_json"], 'w') as f: json.dump(payload, f)
    except Exception:
        pass
    return redirect(url_for("trendline_screener.trendline_scan_process",
                            market=market, restored=1))


@trendline_bp.route("/export-trendline-csv")
def export_trendline_csv():
    market = request.args.get("market", "US").upper()
    data   = _load_results(market)
    if not data:
        return "No data.", 404
    all_records = []
    for label, items in data.get("sections", {}).items():
        for item in items:
            rec = item.copy()
            rec["category"] = label.upper()
            all_records.append(rec)
    if not all_records:
        return "No records.", 404
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp  = os.path.join(UPLOAD_FOLDER, f"tmp_trendline_{market}.csv")
    pd.DataFrame(all_records).to_csv(tmp, index=False)
    return send_file(tmp, as_attachment=True,
                     download_name=f"Trendline_{market}_{ts}.csv")