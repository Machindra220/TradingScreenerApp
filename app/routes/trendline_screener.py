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
    if slope >= 0:
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
    if len(df) < 252:
        return False, 0.0
    closes   = df['Close'].values
    highs    = df['High'].values
    curr     = closes[-1]
    hi52     = highs[-252:-1].max()
    return curr >= hi52 * 0.985, round(hi52, 2)


# ── Per-symbol analysis ────────────────────────────────────────────────────────

def _analyse(yf_sym: str, df: pd.DataFrame, suffix: str) -> dict | None:
    """Run both checks on a single normalised DataFrame."""
    if df is None or df.empty or len(df) < 100:
        return None
    if not {'Open','High','Low','Close','Volume'}.issubset(df.columns):
        return None

    # Normalise tz
    if getattr(df.index, 'tz', None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)

    curr_price = float(df['Close'].iloc[-1])
    has_tl, tl_val, tl_slope, vol_ratio, rsi, high_conf = detect_trendline_breakout(df)
    has_52w, past_high = check_52w_breakout(df)

    if not has_tl and not has_52w:
        return None

    return {
        "symbol":             yf_sym.replace(suffix, ""),
        "price":              round(curr_price, 2),
        "has_trendline_break": has_tl,
        "trendline_value":    tl_val,
        "trendline_slope":    tl_slope,
        "volume_ratio":       vol_ratio,
        "rsi":                rsi,
        "high_confidence":    high_conf,
        "has_52w_break":      has_52w,
        "past_52w_high":      past_high,
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
            raw = str(s).strip().upper().replace('.','-')
            if raw and not raw.startswith('$') and raw not in ('SYMBOL','TICKER'):
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
    cfg   = MARKET_CFG[market]
    cache = us_cache if market == "US" else ind_cache
    suffix = cfg["suffix"]

    _set(active=True, market=market, processed=0, total=0, stage="loading", error=None)

    yf_symbols = _load_symbols(market)
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

    for i, yf_sym in enumerate(yf_symbols):
        _set(processed=i)
        res = _analyse(yf_sym, price_data.get(yf_sym), suffix)
        if not res: continue
        if res["has_trendline_break"] and res["has_52w_break"]:
            both.append(res)
        elif res["has_trendline_break"]:
            tl_only.append(res)
        else:
            hi52_only.append(res)

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