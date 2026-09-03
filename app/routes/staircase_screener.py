"""
staircase_screener.py — Rising Staircase / Step Base Screener

Identifies stocks forming the "staircase breakout" pattern:
  1. Above EMA 200 (mandatory trend filter)
  2. Recent strong impulse leg up (≥8% in last 20 sessions)
  3. Currently in a tight consolidation "step" (range ≤ 4% over last 5–15 bars)
  4. Higher-low structure — recent swing low > prior swing low (ascending steps)
  5. Volume dry-up during consolidation (volume contracting = healthy rest)
  6. Positive RS vs benchmark (stock outperforming the index)
  7. Price holding above EMA 10 and EMA 20 (riding short-term MAs)

Pattern logic (from Brigade Enterprises chart):
  - After a large move, stock consolidates in a tight range (the "step")
  - Volume drops during the step (institutional holding, not distributing)
  - Price remains above EMA 10/20, which acts as dynamic support
  - When the next impulse comes, stock steps up to a new level
  - Scanning DURING the consolidation phase gives early entry before next leg

Both NSE (IND) and US markets supported via shared cache.
"""

import os
import json
import uuid
import threading
import traceback
import numpy as np
import pandas as pd
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file
from werkzeug.utils import secure_filename

from app.services.market_data_cache import ind_cache, us_cache, latest_bar_date

staircase_bp = Blueprint("staircase", __name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_BASE         = os.path.join(_PROJECT_ROOT, 'uploads', 'staircase')
_SNAP_DIR     = os.path.join(_BASE, 'snapshots')
for _d in (_BASE, _SNAP_DIR):
    os.makedirs(_d, exist_ok=True)

HISTORY_LIMIT = 5

MARKET_CFG = {
    "IND": {
        "cache":         ind_cache,
        "default_csv":   os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv'),
        "default_label": "Nifty 500",
        "suffix":        ".NS",
        "currency":      "₹",
        "benchmark":     "^CRSLDX",
        "bench_fb":      "^NSEI",
        "results_json":  os.path.join(_BASE, 'staircase_ind_results.json'),
        "history_json":  os.path.join(_BASE, 'staircase_ind_history.json'),
    },
    "US": {
        "cache":         us_cache,
        "default_csv":   os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv'),
        "default_label": "S&P 500",
        "suffix":        "",
        "currency":      "$",
        "benchmark":     "^GSPC",
        "bench_fb":      None,
        "results_json":  os.path.join(_BASE, 'staircase_us_results.json'),
        "history_json":  os.path.join(_BASE, 'staircase_us_history.json'),
    },
}

# ── Screener parameters (tune here) ──────────────────────────────────────────
PARAMS = {
    # Impulse leg: minimum % gain in the lookback window to qualify as a "step up"
    "impulse_min_pct":       8.0,
    "impulse_lookback":      20,    # sessions to look back for the impulse leg

    # Consolidation step: how many recent bars to measure tightness
    "step_bars_min":         5,
    "step_bars_max":         15,
    "step_range_max_pct":    4.0,   # high-low range within step ≤ this %

    # Higher-low: prior swing low must be lower than recent swing low
    "hl_lookback":           40,    # bars to find prior swing low

    # Volume dry-up: avg vol in step < X× avg vol of impulse leg
    "vol_dryup_ratio":       0.75,  # step avg vol ≤ 75% of impulse avg vol

    # RS: minimum ratio-of-relatives vs benchmark (0 = any positive RS)
    "rs_min":               -0.02,  # allow slight underperformance (-2%)

    # EMA proximity: price must be within X% above EMA10 (not too extended)
    "ema10_max_ext_pct":    10.0,   # price ≤ EMA10 × 1.10
    "ema20_must_be_above":  True,   # price > EMA20 required

    # Stage 2: price must be above EMA200
    "above_ema200":         True,
}


# ── Progress ──────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_PROG = {"active": False, "market": "", "processed": 0,
         "total": 0, "stage": "idle", "error": None}

def _set(**kw):
    with _lock: _PROG.update(kw)

def _get():
    with _lock: return dict(_PROG)


# ── DataFrame normaliser ──────────────────────────────────────────────────────

def _normalise_df(df, sym=None):
    """Handle simple-string and MultiIndex column formats from cache."""
    if df is None or df.empty:
        return None
    cols = df.columns
    if isinstance(cols, pd.MultiIndex) or (len(cols) > 0 and isinstance(cols[0], tuple)):
        if sym is not None:
            for cand in [sym, sym.replace('.NS', ''), sym.replace('-', '.'), sym + '.NS']:
                try:
                    sliced = df.xs(cand, axis=1, level=1)
                    sliced.columns = [c.title() for c in sliced.columns]
                    return sliced
                except KeyError:
                    pass
        flat = {}
        for c in cols:
            field = c[0] if isinstance(c, tuple) else c
            if field.lower() in ('open', 'high', 'low', 'close', 'volume'):
                flat[c] = field.title()
        if flat:
            df = df[list(flat.keys())].copy()
            df.columns = list(flat.values())
            return df
        return None
    df = df.copy()
    df.columns = [
        c.title() if isinstance(c, str) and c.lower() in
        ('open', 'high', 'low', 'close', 'volume') else c
        for c in df.columns
    ]
    return df


# ── Symbol loader ─────────────────────────────────────────────────────────────

def _load_symbols(market: str) -> list[dict]:
    cfg  = MARKET_CFG[market]
    path = cfg["default_csv"]
    if not os.path.exists(path):
        return []
    try:
        df = pd.read_csv(path)
        cols = {c.lower().strip(): c for c in df.columns}
        col  = next((cols[k] for k in ('symbol', 'ticker', 'symbols') if k in cols), None)
        sec_col = next((cols[k] for k in ('gics sector', 'sector', 'industry') if k in cols), None)
        if not col:
            return []
        out = []
        for _, row in df.iterrows():
            raw = str(row[col]).strip().upper().lstrip('$').replace('.', '-')
            sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
            if raw and raw not in ('SYMBOL', 'TICKER', 'N/A'):
                out.append({'symbol': raw, 'yf_sym': f"{raw}{cfg['suffix']}", 'sector': sec})
        return out
    except Exception as e:
        print(f"[Staircase] load_symbols error: {e}")
        return []


# ── Core detection function ───────────────────────────────────────────────────

def _detect_staircase(yf_sym: str, df: pd.DataFrame, bench_close: pd.Series) -> dict | None:
    """
    Detect rising staircase pattern on a single stock's DataFrame.
    Returns a result dict if the pattern is found, None otherwise.

    Pattern logic:
      The "step" pattern has two phases:
        A. Impulse leg: a strong move up (≥8%) completed recently
        B. Consolidation: price has since settled into a tight range (≤4%)
           with declining volume — the stock is resting, not distributing

      We scan backward from today:
        1. Find the tightest recent N-bar window (5–15 bars) → this is the "step"
        2. Measure the move before the step → this is the "impulse"
        3. Confirm higher-low: step low > prior swing low
        4. Confirm volume dry-up in step vs impulse
        5. Confirm EMA200 / EMA20 / EMA10 conditions
        6. Compute RS vs benchmark
    """
    p = PARAMS

    try:
        close  = df['Close'].dropna()
        high   = df['High'].dropna()
        low    = df['Low'].dropna()
        volume = df['Volume'].fillna(0)

        if len(close) < 250:
            return None

        curr_price = float(close.iloc[-1])

        # ── EMA calculations ────────────────────────────────────────────────
        ema10  = float(close.ewm(span=10,  adjust=False).mean().iloc[-1])
        ema20  = float(close.ewm(span=20,  adjust=False).mean().iloc[-1])
        ema50  = float(close.ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])

        # Condition: above EMA200
        if p["above_ema200"] and curr_price <= ema200:
            return None

        # Condition: price above EMA20
        if p["ema20_must_be_above"] and curr_price <= ema20:
            return None

        # Condition: not too extended above EMA10 (not chasing an already-extended move)
        ema10_ext = (curr_price - ema10) / ema10 * 100
        if ema10_ext > p["ema10_max_ext_pct"]:
            return None

        # ── Find the consolidation "step" ────────────────────────────────────
        # Scan all windows of length step_bars_min to step_bars_max ending at today.
        # Pick the window with the tightest high-low range as a % of price.
        best_step_range_pct = 999.0
        best_step_bars      = 0
        best_step_start_idx = -1  # index into close.iloc[] of step start (from end)

        for n in range(p["step_bars_min"], p["step_bars_max"] + 1):
            step_hi  = float(high.iloc[-n:].max())
            step_lo  = float(low.iloc[-n:].min())
            step_rng = (step_hi - step_lo) / step_lo * 100
            if step_rng < best_step_range_pct:
                best_step_range_pct = step_rng
                best_step_bars      = n
                best_step_start_idx = n

        # Condition: step must be tight
        if best_step_range_pct > p["step_range_max_pct"]:
            return None

        step_n   = best_step_bars
        step_hi  = float(high.iloc[-step_n:].max())
        step_lo  = float(low.iloc[-step_n:].min())
        step_avg_vol = float(volume.iloc[-step_n:].mean())

        # ── Measure the impulse leg before the step ──────────────────────────
        # Impulse window: the p["impulse_lookback"] bars ending at step start
        impulse_end   = len(close) - step_n         # index of last bar before step
        impulse_start = max(0, impulse_end - p["impulse_lookback"])

        if impulse_end <= impulse_start:
            return None

        imp_close  = close.iloc[impulse_start:impulse_end]
        imp_high   = high.iloc[impulse_start:impulse_end]
        imp_volume = volume.iloc[impulse_start:impulse_end]

        impulse_lo    = float(imp_close.min())    # entry of impulse
        impulse_hi    = float(imp_high.max())     # peak of impulse
        impulse_pct   = (impulse_hi - impulse_lo) / impulse_lo * 100
        impulse_avg_vol = float(imp_volume.mean()) if len(imp_volume) > 0 else 1.0

        # Condition: impulse must be strong enough
        if impulse_pct < p["impulse_min_pct"]:
            return None

        # ── Volume dry-up in step vs impulse ─────────────────────────────────
        vol_ratio = step_avg_vol / (impulse_avg_vol + 1e-9)
        vol_dryup = vol_ratio <= p["vol_dryup_ratio"]

        # ── Higher-low structure ─────────────────────────────────────────────
        # Prior swing low = minimum of [hl_lookback bars before the step start]
        prior_start = max(0, impulse_start - p["hl_lookback"])
        prior_low   = float(low.iloc[prior_start:impulse_start].min()) \
                      if impulse_start > prior_start else step_lo
        higher_low  = step_lo > prior_low

        # ── RS vs benchmark (ratio-of-relatives, 63-day) ─────────────────────
        rs_val = None
        if bench_close is not None and len(close) >= 63:
            try:
                bc = bench_close.reindex(close.index).ffill().bfill()
                s_ret = (float(close.iloc[-1]) / float(close.iloc[-63])) - 1
                b_ret = (float(bc.iloc[-1])    / float(bc.iloc[-63]))    - 1
                if (1 + b_ret) != 0:
                    rs_val = round(((1 + s_ret) / (1 + b_ret) - 1) * 100, 2)
            except Exception:
                pass

        # Condition: positive RS
        if rs_val is not None and rs_val < p["rs_min"] * 100:
            return None

        # ── 52W context ───────────────────────────────────────────────────────
        hi52  = float(high.tail(252).max()) if len(high) >= 60 else float(high.max())
        lo52  = float(low.tail(252).min())  if len(low)  >= 60 else float(low.min())
        retrace_from_hi = round((hi52 - curr_price) / hi52 * 100, 1) if hi52 > 0 else 0

        # ── Step quality score (0–100) ────────────────────────────────────────
        # Combines: tightness, impulse strength, volume dry-up, higher-low, RS
        score = 0
        score += min(40, int((p["step_range_max_pct"] - best_step_range_pct) /
                              p["step_range_max_pct"] * 40))   # tighter = higher
        score += min(25, int(min(impulse_pct, 30) / 30 * 25))  # stronger impulse
        score += 15 if vol_dryup else 0
        score += 10 if higher_low else 0
        score += 10 if (rs_val is not None and rs_val > 0) else 0

        # ── Step status ───────────────────────────────────────────────────────
        # Is the stock sitting mid-step (holding) or just starting to break out?
        step_pivot    = step_hi     # resistance to watch
        pct_from_pivot = round((step_pivot - curr_price) / step_pivot * 100, 2)

        if pct_from_pivot <= 0:
            step_status = "Breaking Out 🚀"
        elif pct_from_pivot <= 2:
            step_status = "At Pivot ⚡"
        elif pct_from_pivot <= 5:
            step_status = "Near Pivot 📈"
        else:
            step_status = "Forming 🔄"

        # EMA200 extension
        ma200_ext = round(curr_price / ema200, 3) if ema200 > 0 else None

        return {
            "step_range_pct":    round(best_step_range_pct, 2),
            "step_bars":         step_n,
            "impulse_pct":       round(impulse_pct, 2),
            "vol_dryup":         vol_dryup,
            "vol_ratio":         round(vol_ratio, 2),
            "higher_low":        higher_low,
            "prior_low":         round(prior_low, 2),
            "step_lo":           round(step_lo, 2),
            "step_hi":           round(step_hi, 2),     # pivot
            "step_pivot":        round(step_pivot, 2),
            "pct_from_pivot":    pct_from_pivot,
            "step_status":       step_status,
            "score":             score,
            "price":             round(curr_price, 2),
            "ema10":             round(ema10, 2),
            "ema20":             round(ema20, 2),
            "ema50":             round(ema50, 2),
            "ema200":            round(ema200, 2),
            "ema10_ext_pct":     round(ema10_ext, 2),
            "ma200_ext":         ma200_ext,
            "rs_vs_bench":       rs_val,
            "hi52":              round(hi52, 2),
            "lo52":              round(lo52, 2),
            "retrace_from_hi":   retrace_from_hi,
        }

    except Exception as e:
        print(f"  [Staircase] {yf_sym}: {e}")
        return None


# ── Background scan ───────────────────────────────────────────────────────────

def run_scan(market: str):
    try:
        _run_inner(market)
    except Exception as e:
        traceback.print_exc()
        _set(active=False, stage="error", error=str(e)[:120])


def _run_inner(market: str):
    cfg = MARKET_CFG[market]
    _set(active=True, market=market, processed=0, total=0,
         stage="loading", error=None)

    tickers  = _load_symbols(market)
    yf_syms  = [t['yf_sym'] for t in tickers]
    sym_meta = {t['yf_sym']: t for t in tickers}
    _set(total=len(yf_syms))

    # ── Benchmark (once) ─────────────────────────────────────────────────────
    _set(stage="benchmark")
    bench_close = None
    for bsym in filter(None, [cfg["benchmark"], cfg.get("bench_fb")]):
        data, _ = cfg["cache"].get_price_history_bulk(
            [bsym], interval='1d', lookback_days=500,
            progress_callback=lambda *a: None,
        )
        bdf = _normalise_df(data.get(bsym), bsym)
        if bdf is not None and 'Close' in bdf.columns and len(bdf) >= 63:
            bench_close = bdf['Close'].dropna()
            if getattr(bench_close.index, 'tz', None):
                bench_close.index = bench_close.index.tz_localize(None)
            print(f"[Staircase/{market}] Benchmark: {bsym} ({len(bench_close)} bars)")
            break

    # ── Bulk fetch ────────────────────────────────────────────────────────────
    _set(stage="fetching")
    price_data, fetch_report = cfg["cache"].get_price_history_bulk(
        yf_syms, interval='1d', lookback_days=500,
        progress_callback=lambda i, t, s: _set(processed=i, total=t),
    )
    price_data_asof = latest_bar_date(price_data)
    _ch, _yf = fetch_report["from_cache"], fetch_report["fetched"]
    print(f"[Staircase/{market}] {len(yf_syms)} syms | Cache:{_ch} | YF:{_yf}")

    # ── Screen ────────────────────────────────────────────────────────────────
    _set(stage="screening", processed=0)
    results = []

    for i, yf_sym in enumerate(yf_syms):
        _set(processed=i)
        df_raw = price_data.get(yf_sym)
        df     = _normalise_df(df_raw, yf_sym)
        if df is None or df.empty:
            continue

        result = _detect_staircase(yf_sym, df, bench_close)
        if result is None:
            continue

        meta = sym_meta[yf_sym]
        result['symbol']  = meta['symbol']
        result['sector']  = meta['sector']
        result['yf_sym']  = yf_sym
        results.append(result)

    # RS percentile ranking across passing universe
    if results:
        df_r = pd.DataFrame(results)
        if 'rs_vs_bench' in df_r.columns and df_r['rs_vs_bench'].notna().any():
            df_r['rs_percentile'] = (
                df_r['rs_vs_bench'].rank(pct=True).mul(98).add(1)
                .round(0).clip(1, 99).astype(int)
            )
        else:
            df_r['rs_percentile'] = 0
        df_r.sort_values('score', ascending=False, inplace=True)
        results = df_r.to_dict(orient='records')
    else:
        results = []

    last_time     = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    snap_filename = f"staircase_{market.lower()}_{uuid.uuid4().hex}.json"

    payload = {
        "stocks":         results,
        "time":           last_time,
        "market":         market,
        "scanned_count":  len(yf_syms),
        "passed_count":   len(results),
        "price_data_asof":price_data_asof,
        "cache_hits":     _ch,
        "yf_fetches":     _yf,
        "params":         PARAMS,
    }

    with open(os.path.join(_SNAP_DIR, snap_filename), 'w') as f:
        json.dump(payload, f, default=str)
    with open(cfg["results_json"], 'w') as f:
        json.dump(payload, f, default=str)

    # History
    history = _load_history(market)
    history.insert(0, {
        "time":           last_time,
        "market":         market,
        "count":          len(results),
        "scanned_count":  len(yf_syms),
        "price_data_asof":price_data_asof,
        "snapshot_file":  snap_filename,
        "breakouts":      [r['symbol'] for r in results if 'Breaking' in r.get('step_status','')][:5],
        "high_score":     [r['symbol'] for r in results if r.get('score', 0) >= 70][:5],
    })
    history = history[:HISTORY_LIMIT]

    # Prune old snapshots
    keep = {h["snapshot_file"] for h in history if h.get("snapshot_file")}
    for fn in os.listdir(_SNAP_DIR):
        if fn not in keep:
            try: os.remove(os.path.join(_SNAP_DIR, fn))
            except OSError: pass

    with open(cfg["history_json"], 'w') as f:
        json.dump(history, f)

    _set(active=False, stage="done")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_history(market: str) -> list:
    path = MARKET_CFG[market]["history_json"]
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except (json.JSONDecodeError, OSError): pass
    return []


def _load_results(market: str) -> dict:
    path = MARKET_CFG[market]["results_json"]
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except (json.JSONDecodeError, OSError): pass
    return {}


# ── Routes ────────────────────────────────────────────────────────────────────

@staircase_bp.route("/staircase-screener", methods=["GET", "POST"])
def staircase_view():
    market = request.args.get("market", request.form.get("market", "IND")).upper()
    if market not in MARKET_CFG: market = "IND"

    if request.method == "POST":
        if not _get()["active"]:
            t = threading.Thread(target=run_scan, args=(market,), daemon=True)
            t.start()
        return redirect(url_for("staircase.staircase_view", market=market, scanning=1))

    data     = _load_results(market)
    history  = _load_history(market)
    prog     = _get()
    currency = MARKET_CFG[market]["currency"]

    return render_template(
        "staircase_screener.html",
        stocks            = data.get("stocks", []),
        last_time         = data.get("time"),
        scanned_count     = data.get("scanned_count", 0),
        passed_count      = data.get("passed_count", 0),
        price_data_asof   = data.get("price_data_asof"),
        cache_hits        = data.get("cache_hits", 0),
        yf_fetches        = data.get("yf_fetches", 0),
        params            = data.get("params", PARAMS),
        market            = market,
        currency          = currency,
        history           = history,
        is_scanning       = prog["active"] and prog["market"] == market,
        scan_error        = prog.get("error") if not prog["active"] else None,
        restored          = request.args.get("restored") == "1",
    )


@staircase_bp.route("/staircase-screener/progress")
def staircase_progress():
    return jsonify(_get())


@staircase_bp.route("/staircase-screener/restore/<snap>", methods=["POST"])
def staircase_restore(snap):
    market = request.form.get("market", "IND").upper()
    safe   = os.path.basename(snap)
    path   = os.path.join(_SNAP_DIR, safe)
    if not os.path.exists(path):
        return redirect(url_for("staircase.staircase_view", market=market))
    try:
        with open(path) as f: payload = json.load(f)
        with open(MARKET_CFG[market]["results_json"], 'w') as f:
            json.dump(payload, f, default=str)
    except Exception: pass
    return redirect(url_for("staircase.staircase_view", market=market, restored=1))


@staircase_bp.route("/staircase-screener/export")
def staircase_export():
    market = request.args.get("market", "IND").upper()
    data   = _load_results(market)
    stocks = data.get("stocks", [])
    if not stocks:
        return "No data.", 404
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp = os.path.join(_BASE, f"tmp_staircase_{market}.csv")
    pd.DataFrame(stocks).to_csv(tmp, index=False)
    return send_file(tmp, as_attachment=True,
                     download_name=f"Staircase_{market}_{ts}.csv")
