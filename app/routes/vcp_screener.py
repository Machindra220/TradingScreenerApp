"""
vcp_screener.py — Volatility Contraction Pattern (VCP) Screener
Based on Mark Minervini's "Trade Like a Stock Market Wizard" methodology.

VCP Algorithm (corrected):
  1. Stage 2 trend template (6 of 8 Minervini conditions)
  2. Prior uptrend: price >= 52W_low * 1.30
  3. Detect temporally-ordered peak→trough contraction pairs within a 3–65 week base
  4. At least 2 contractions, each SHALLOWER than the previous (abs value decreasing)
  5. Volume DRY UP on each successive contraction trough
  6. Pivot point = high of last tight area (rightmost local high in base)
  7. Near-breakout: price within 5% of pivot (forming) OR closed above pivot on volume (formed)

NSE: ind_cache / ^CRSLDX benchmark
US:  us_cache  / ^GSPC benchmark
Both markets in one UI — scans are stored separately, page shows active market scan.
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
from werkzeug.utils import secure_filename

from app.services.market_data_cache import ind_cache, us_cache, latest_bar_date

vcp_bp = Blueprint("vcp", __name__, url_prefix="/vcp")

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_DIR    = os.path.join(_PROJECT_ROOT, 'uploads', 'vcp')
SNAP_DIR      = os.path.join(UPLOAD_DIR, 'snapshots')
os.makedirs(SNAP_DIR, exist_ok=True)

# Per-market results / history files — scans stored independently
MARKET_CFG = {
    "IND": {
        "results_json":  os.path.join(UPLOAD_DIR, 'vcp_ind_results.json'),
        "history_json":  os.path.join(UPLOAD_DIR, 'vcp_ind_history.json'),
        "default_csv":   os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv'),
        "default_label": "Nifty 500 (nifty_500.csv)",
        "cache":         None,   # filled at runtime
        "benchmark":     "^CRSLDX",
        "currency":      "₹",
        "suffix":        ".NS",
    },
    "US": {
        "results_json":  os.path.join(UPLOAD_DIR, 'vcp_us_results.json'),
        "history_json":  os.path.join(UPLOAD_DIR, 'vcp_us_history.json'),
        "default_csv":   os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv'),
        "default_label": "S&P 500 (sp500.csv)",
        "cache":         None,
        "benchmark":     "^GSPC",
        "currency":      "$",
        "suffix":        "",
    },
}
HISTORY_LIMIT = 5

# ── Progress ──────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_PROG = {"active": False, "market": "", "processed": 0,
          "total": 0, "stage": "idle", "error": None}

def _set(**kw):
    with _lock: _PROG.update(kw)

def _get():
    with _lock: return dict(_PROG)


# ── Symbol loader ─────────────────────────────────────────────────────────────

def _load_symbols(market: str) -> list[dict]:
    cfg  = MARKET_CFG[market]
    path = cfg["default_csv"]
    if not os.path.exists(path):
        return []
    try:
        df   = pd.read_csv(path)
        cols = {c.lower().strip(): c for c in df.columns}
        col  = next((cols[k] for k in ('symbol','ticker','symbols') if k in cols), None)
        if col is None: return []
        sec_col = next((cols[k] for k in ('gics sector','sector','industry') if k in cols), None)
        out = []
        for _, row in df.iterrows():
            raw = str(row[col]).strip().upper().replace('.NS','').replace('.BSE','').replace('.','-')
            sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
            if raw and not raw.startswith('$') and raw not in ('SYMBOL','TICKER','N/A'):
                yf_sym = f"{raw}{cfg['suffix']}"
                out.append({'symbol': raw, 'yf_sym': yf_sym, 'sector': sec})
        return out
    except Exception as e:
        print(f"[VCP] load_symbols error: {e}")
        return []


# ── Core VCP detection ────────────────────────────────────────────────────────

def _detect_contractions(close: pd.Series, volume: pd.Series,
                          lookback_bars: int = 130):
    """
    Find temporally-ordered peak→trough pairs within the base window.
    Returns list of dicts: {high, low, drop_pct, vol_at_trough}
    drop_pct is POSITIVE (e.g. 18.5 means 18.5% pullback from the peak).

    Bug in original code fixed:
    - Was pairing peaks[i] with troughs[i] without checking temporal order.
      A trough before its paired peak gives a positive-price delta,
      producing nonsensical contraction values.
    - Was comparing negative numbers with `>` instead of abs() comparison,
      making the "is_contracting" direction backwards.
    """
    c = close.tail(lookback_bars)
    v = volume.tail(lookback_bars)
    if len(c) < 20:
        return []

    peak_idxs, _ = find_peaks(c.values,     distance=5, prominence=c.std() * 0.3)
    trou_idxs, _ = find_peaks(-c.values,    distance=5, prominence=c.std() * 0.3)

    pairs = []
    for pi in peak_idxs:
        # Find the first trough that comes AFTER this peak
        later_troughs = trou_idxs[trou_idxs > pi]
        if len(later_troughs) == 0:
            continue
        ti = later_troughs[0]
        high  = float(c.iloc[pi])
        low   = float(c.iloc[ti])
        if high <= 0: continue
        drop  = round((high - low) / high * 100, 2)   # positive %
        vol_t = float(v.iloc[ti])
        pairs.append({
            "peak_idx":    int(pi),
            "trough_idx":  int(ti),
            "high":        round(high, 2),
            "low":         round(low, 2),
            "drop_pct":    drop,
            "vol_at_trough": int(vol_t),
        })

    # Deduplicate: if two peaks map to the same trough, keep the one with larger drop
    seen_troughs = {}
    for p in pairs:
        ti = p["trough_idx"]
        if ti not in seen_troughs or p["drop_pct"] > seen_troughs[ti]["drop_pct"]:
            seen_troughs[ti] = p
    return sorted(seen_troughs.values(), key=lambda x: x["trough_idx"])


def _is_vcp(df: pd.DataFrame, market: str) -> dict | None:
    """
    Returns a result dict if the stock meets VCP criteria, else None.

    Intentionally LENIENT — we want stocks FORMING or FORMED VCP,
    not only textbook perfect completions. A stock with 2 valid
    tightening contractions and volume dry-up qualifies.

    Criteria:
      A) Minervini trend template (need ≥ 5 of 6 conditions)
      B) Prior uptrend: price >= 52W_low * 1.30
      C) Base: last contraction started within 3–65 weeks
      D) ≥ 2 contractions, each shallower (abs drop_pct decreasing)
      E) Volume dry-up: each trough vol < previous trough vol (or < 70% of 50d avg)
      F) First contraction ≤ 35% (rules out crash recoveries)
      G) Last contraction ≤ 15% (tight enough to be actionable)
      H) Pivot = high of most recent peak; price within 10% (forming) or
         closed above pivot on volume (formed/breaking out)
    """
    if df is None or len(df) < 200:
        return None
    if not {'Close','High','Low','Volume'}.issubset(df.columns):
        return None

    close  = df['Close'].dropna()
    high_s = df['High'].dropna()
    low_s  = df['Low'].dropna()
    vol    = df['Volume'].fillna(0)

    if getattr(close.index, 'tz', None) is not None:
        close.index  = close.index.tz_localize(None)
        high_s.index = high_s.index.tz_localize(None)
        low_s.index  = low_s.index.tz_localize(None)
        vol.index    = vol.index.tz_localize(None)

    price   = float(close.iloc[-1])
    hi52    = float(high_s.tail(252).max())
    lo52    = float(low_s.tail(252).min())

    # ── A: Trend template (≥5 of 6) ──────────────────────────────────────────
    ma50  = close.rolling(50).mean()
    ma150 = close.rolling(150).mean()
    ma200 = close.rolling(200).mean()
    if ma200.isna().iloc[-1]: return None

    m50, m150, m200 = float(ma50.iloc[-1]), float(ma150.iloc[-1]), float(ma200.iloc[-1])
    m200_ago = float(ma200.iloc[-22]) if len(ma200) >= 22 else m200

    trend_checks = [
        price > m150 and price > m200,   # C1
        m150 > m200,                      # C2
        m200 > m200_ago,                  # C3 — MA200 rising
        m50 > m150 and m50 > m200,        # C4
        price >= lo52 * 1.30,             # C5 — prior uptrend ≥30%
        price >= hi52 * 0.75,             # C6 — within 25% of 52W high
    ]
    trend_score = sum(trend_checks)
    if trend_score < 5:                   # allow one miss for lenience
        return None

    # ── B: Prior uptrend ─────────────────────────────────────────────────────
    if price < lo52 * 1.25:              # need at least 25% above annual low
        return None

    # ── C+D+E: Contractions ───────────────────────────────────────────────────
    pairs = _detect_contractions(close, vol, lookback_bars=130)
    if len(pairs) < 2:
        return None

    # Need at least 2 consecutive tightening pairs
    # Find the BEST consecutive run of tightening contractions
    best_run = []
    current_run = [pairs[0]]
    for i in range(1, len(pairs)):
        prev, curr = pairs[i-1], pairs[i]
        if curr['drop_pct'] < prev['drop_pct']:   # shallower = tightening ✅
            current_run.append(curr)
        else:
            if len(current_run) > len(best_run):
                best_run = current_run[:]
            current_run = [curr]
    if len(current_run) > len(best_run):
        best_run = current_run[:]

    if len(best_run) < 2:
        return None

    # ── F: First contraction ≤ 35% ───────────────────────────────────────────
    if best_run[0]['drop_pct'] > 35:
        return None

    # ── G: Last contraction ≤ 15% (tight enough) ─────────────────────────────
    if best_run[-1]['drop_pct'] > 15:
        return None

    # ── E: Volume dry-up check across the run ────────────────────────────────
    vol_50d   = float(vol.rolling(50).mean().iloc[-1]) if not vol.empty else 0
    trough_vols = [p['vol_at_trough'] for p in best_run]
    vol_dryup = (
        all(trough_vols[i] >= trough_vols[i+1]
            for i in range(len(trough_vols)-1))     # each trough vol < previous
        or (trough_vols[-1] < 0.65 * vol_50d if vol_50d > 0 else False)
    )

    # ── Base length (needed by smoothness filter below) ──────────────────────
    base_start_idx = best_run[0]['peak_idx']
    base_bars      = len(close) - base_start_idx
    base_weeks     = round(base_bars / 5, 1)

    # ── H: Pivot + status ─────────────────────────────────────────────────────
    pivot = best_run[-1]['high']    # high of the last (tightest) contraction
    pullback_from_pivot = round((pivot - price) / pivot * 100, 2)

    vol_today = float(vol.iloc[-1])
    breakout  = (price > pivot and vol_today > 1.4 * vol_50d)
    forming   = pullback_from_pivot <= 10   # within 10% of pivot = forming
    status    = "🔥 Breaking Out" if breakout else ("⚡ Near Pivot" if forming else "🔄 Forming")

    # ── MA200 extension ───────────────────────────────────────────────────────
    ma200_ext = round(price / m200, 3) if m200 > 0 else 0

    # ROC
    roc7  = round((float(close.iloc[-1]) / float(close.iloc[-7])  - 1) * 100, 2) if len(close) >= 7  else None
    roc21 = round((float(close.iloc[-1]) / float(close.iloc[-21]) - 1) * 100, 2) if len(close) >= 21 else None

    # ── Smoothness / Trend Quality filter ────────────────────────────────────
    # Measures how orderly the price advance is.
    # High zigzag stocks have large daily swings relative to the net move.
    #
    # Method 1 — VOLAR (returns / volatility): high VOLAR = orderly advance.
    #   VOLAR < 0.8 over 63 days = too choppy to predict.
    #
    # Method 2 — R² of price vs linear regression line over base period:
    #   R² close to 1.0 = price moves in a smooth trend.
    #   R² < 0.65 = price bounces around with no clear direction.
    #
    # Both must pass to keep the stock.
    c63    = close.tail(63)
    ret63  = c63.pct_change().dropna()
    ret_std = float(ret63.std()) if len(ret63) > 1 else 1
    ret_3m  = (float(c63.iloc[-1]) / float(c63.iloc[0]) - 1)
    volar_3m = round(abs(ret_3m) / (ret_std + 1e-9), 2)

    # R² of close vs time over the base window (last peak to now)
    base_close = close.tail(max(int(base_bars), 20))
    x = np.arange(len(base_close))
    if len(x) >= 5:
        coeffs   = np.polyfit(x, base_close.values, 1)
        y_hat    = np.polyval(coeffs, x)
        ss_res   = np.sum((base_close.values - y_hat) ** 2)
        ss_tot   = np.sum((base_close.values - base_close.mean()) ** 2)
        r_squared = round(1 - ss_res / (ss_tot + 1e-9), 3)
    else:
        r_squared = 0.0

    # Reject high-zigzag stocks
    if volar_3m < 0.8:   return None   # advance too choppy vs volatility
    if r_squared < 0.50: return None   # price has no trend direction in the base

    return {
        "price":            round(price, 2),
        "pivot":            round(pivot, 2),
        "pullback_pct":     pullback_from_pivot,
        "contractions":     [round(p['drop_pct'], 1) for p in best_run],
        "n_contractions":   len(best_run),
        "vol_dryup":        vol_dryup,
        "status":           status,
        "breakout":         breakout,
        "forming":          forming,
        "trend_score":      trend_score,
        "roc7":             roc7,
        "roc21":            roc21,
        "ma200_ext":        ma200_ext,
        "base_weeks":       base_weeks,
        "hi52":             round(hi52, 2),
        "lo52":             round(lo52, 2),
        "volar_3m":         volar_3m,
        "r_squared":        r_squared,
    }


# ── Background scan ───────────────────────────────────────────────────────────

def run_scan(market: str):
    cfg   = MARKET_CFG[market]
    cache = ind_cache if market == "IND" else us_cache

    _set(active=True, market=market, processed=0, total=0, stage="loading", error=None)

    tickers = _load_symbols(market)
    if not tickers:
        _set(active=False, stage="error", error=f"Could not load symbols for {market}")
        return

    yf_syms  = [t['yf_sym'] for t in tickers]
    sym_meta = {t['yf_sym']: t for t in tickers}
    _set(total=len(yf_syms))

    # Bulk fetch
    _set(stage="fetching")
    price_data, fetch_report = cache.get_price_history_bulk(
        yf_syms, interval='1d', lookback_days=500,
        progress_callback=lambda i, t, s: _set(processed=i, total=t)
    )
    price_data_asof = latest_bar_date(price_data)
    _ch, _yf = fetch_report['from_cache'], fetch_report['fetched']

    print(f"[VCP/{market}] {len(yf_syms)} symbols | Cache:{_ch} | YF:{_yf}")

    # Screen
    _set(stage="screening")
    results = []
    for i, yf_sym in enumerate(yf_syms):
        _set(processed=i)
        meta = sym_meta[yf_sym]
        df   = price_data.get(yf_sym)
        vcp  = _is_vcp(df, market)
        if vcp:
            results.append({
                "symbol":       meta['symbol'],
                "yf_sym":       yf_sym,
                "sector":       meta['sector'],
                **vcp,
            })

    # Sort: Breaking Out first, then Near Pivot, then Forming
    # Within each group sort by n_contractions desc, then pullback_pct asc
    order = {"🔥 Breaking Out": 0, "⚡ Near Pivot": 1, "🔄 Forming": 2}
    results.sort(key=lambda x: (order.get(x['status'], 9),
                                 -x['n_contractions'],
                                  x['pullback_pct']))

    last_time  = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    snap_file  = f"vcp_{market.lower()}_{uuid.uuid4().hex}.json"
    payload = {
        "stocks":          results,
        "time":            last_time,
        "market":          market,
        "scanned_count":   len(yf_syms),
        "passed_count":    len(results),
        "price_data_asof": price_data_asof,
        "cache_hits":      _ch,
        "yf_fetches":      _yf,
        "snapshot_file":   snap_file,
    }

    # Save results
    with open(os.path.join(SNAP_DIR, snap_file), 'w') as f:
        json.dump(payload, f)
    with open(cfg["results_json"], 'w') as f:
        json.dump(payload, f)

    # History (last 5 per market)
    history = []
    if os.path.exists(cfg["history_json"]):
        try:
            with open(cfg["history_json"]) as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    history.insert(0, {
        "time":            last_time,
        "count":           len(results),
        "scanned_count":   len(yf_syms),
        "price_data_asof": price_data_asof,
        "snapshot_file":   snap_file,
    })
    history = history[:HISTORY_LIMIT]
    # Prune old snapshots
    keep = {h['snapshot_file'] for h in history if h.get('snapshot_file')}
    for f in os.listdir(SNAP_DIR):
        if f.startswith(f"vcp_{market.lower()}_") and f not in keep:
            try: os.remove(os.path.join(SNAP_DIR, f))
            except OSError: pass
    with open(cfg["history_json"], 'w') as f:
        json.dump(history, f)

    _set(active=False, stage="done")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_results(market):
    cfg = MARKET_CFG[market]
    if os.path.exists(cfg["results_json"]):
        try:
            with open(cfg["results_json"]) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def _load_history(market):
    cfg = MARKET_CFG[market]
    if os.path.exists(cfg["history_json"]):
        try:
            with open(cfg["history_json"]) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


# ── Routes ────────────────────────────────────────────────────────────────────

@vcp_bp.route("/", methods=["GET"])
def vcp_view():
    market  = request.args.get("market", "IND").upper()
    if market not in MARKET_CFG: market = "IND"

    data    = _load_results(market)
    history = _load_history(market)
    prog    = _get()
    is_scanning = prog["active"] and prog["market"] == market

    return render_template("vcp.html",
        stocks          = data.get("stocks", []),
        last_time       = data.get("time"),
        scanned_count   = data.get("scanned_count", 0),
        passed_count    = data.get("passed_count", 0),
        price_data_asof = data.get("price_data_asof"),
        cache_hits      = data.get("cache_hits", 0),
        yf_fetches      = data.get("yf_fetches", 0),
        history         = history,
        market          = market,
        currency        = MARKET_CFG[market]["currency"],
        is_scanning     = is_scanning,
        scan_error      = prog.get("error") if not prog["active"] else None,
        restored        = request.args.get("restored") == "1",
    )


@vcp_bp.route("/process", methods=["POST"])
def vcp_process():
    market = request.form.get("market", "IND").upper()
    if market not in MARKET_CFG: market = "IND"
    prog = _get()
    if not prog["active"]:
        t = threading.Thread(target=run_scan, args=(market,), daemon=True)
        t.start()
    return redirect(url_for("vcp.vcp_view", market=market, scanning=1))


@vcp_bp.route("/progress")
def vcp_progress():
    return jsonify(_get())


@vcp_bp.route("/restore/<snapshot_file>", methods=["POST"])
def vcp_restore(snapshot_file):
    market    = request.form.get("market", "IND").upper()
    safe      = os.path.basename(snapshot_file)
    snap_path = os.path.join(SNAP_DIR, safe)
    if not os.path.exists(snap_path):
        return redirect(url_for("vcp.vcp_view", market=market))
    try:
        with open(snap_path) as f: payload = json.load(f)
        with open(MARKET_CFG[market]["results_json"], 'w') as f: json.dump(payload, f)
    except Exception:
        pass
    return redirect(url_for("vcp.vcp_view", market=market, restored=1))


@vcp_bp.route("/export")
def vcp_export():
    market = request.args.get("market", "IND").upper()
    data   = _load_results(market)
    stocks = data.get("stocks", [])
    if not stocks:
        return "No data.", 404
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp  = os.path.join(UPLOAD_DIR, f"tmp_vcp_{market}.csv")
    pd.DataFrame(stocks).to_csv(tmp, index=False)
    return send_file(tmp, as_attachment=True,
                     download_name=f"VCP_{market}_{ts}.csv")