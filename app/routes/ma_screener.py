"""
ma_screener.py — Configurable Moving Average (EMA/SMA) Screener

Architecture
────────────
Rule engine: each rule is a dict evaluated against a DataFrame.
No look-ahead bias: all comparisons use only index [-1] (today) and [-2]
(yesterday). Crossover lookback scans backward from [-1] up to N days.

Rule dict schema:
  {
    "left":     {"type": "EMA"|"SMA"|"PRICE", "period": 20},
    "operator": "CROSS_ABOVE"|"CROSS_BELOW"|"ABOVE"|"BELOW"|"RISING"|"FALLING",
    "right":    {"type": "EMA"|"SMA"|"PRICE", "period": 50},  # PRICE ignored for RISING/FALLING
    "lookback": 5,        # crossover only — days to look back
    "slope_days": 10,     # RISING/FALLING only — comparison window
    "enabled":  true,
    "logic":    "AND"|"OR"   # how this rule combines with the next
  }

Predefined scans are stored as lists of such dicts.
"""

import os
import json
import uuid
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from flask import (Blueprint, render_template, request,
                   redirect, url_for, jsonify, send_file)

from app.services.market_data_cache import ind_cache, us_cache, latest_bar_date

ma_screener_bp = Blueprint("ma_screener", __name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_BASE         = os.path.join(_PROJECT_ROOT, 'uploads', 'ma_screener')
_SNAP_DIR     = os.path.join(_BASE, 'snapshots')
_SAVED_DIR    = os.path.join(_BASE, 'saved_scans')
_HIST_JSON    = os.path.join(_BASE, 'ma_scan_history.json')

for _d in (_BASE, _SNAP_DIR, _SAVED_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Market config ─────────────────────────────────────────────────────────────
_MARKET = {
    "IND": {
        "cache":         ind_cache,
        "default_csv":   os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv'),
        "default_label": "Nifty 500",
        "suffix":        ".NS",
        "currency":      "₹",
        "benchmark":     "^CRSLDX",
    },
    "US": {
        "cache":         us_cache,
        "default_csv":   os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv'),
        "default_label": "S&P 500",
        "suffix":        "",
        "currency":      "$",
        "benchmark":     "^GSPC",
    },
}
HISTORY_LIMIT = 5

# ── Predefined scans ──────────────────────────────────────────────────────────
PREDEFINED_SCANS = {
    "ema10_cross_ema20": {
        "name": "EMA 10 Crossed Above EMA 20",
        "rules": [{"left": {"type":"EMA","period":10}, "operator":"CROSS_ABOVE",
                   "right": {"type":"EMA","period":20}, "lookback":5,
                   "slope_days":10, "enabled":True, "logic":"AND"}]
    },
    "ema10_cross_ema50": {
        "name": "EMA 10 Crossed Above EMA 50",
        "rules": [{"left": {"type":"EMA","period":10}, "operator":"CROSS_ABOVE",
                   "right": {"type":"EMA","period":50}, "lookback":5,
                   "slope_days":10, "enabled":True, "logic":"AND"}]
    },
    "ema20_cross_ema50": {
        "name": "EMA 20 Crossed Above EMA 50",
        "rules": [{"left": {"type":"EMA","period":20}, "operator":"CROSS_ABOVE",
                   "right": {"type":"EMA","period":50}, "lookback":10,
                   "slope_days":10, "enabled":True, "logic":"AND"}]
    },
    "bullish_ema_alignment": {
        "name": "Bullish EMA Alignment (10>20>50>100>200)",
        "rules": [
            {"left":{"type":"EMA","period":10},  "operator":"ABOVE", "right":{"type":"EMA","period":20},  "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":20},  "operator":"ABOVE", "right":{"type":"EMA","period":50},  "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":50},  "operator":"ABOVE", "right":{"type":"EMA","period":100}, "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":100}, "operator":"ABOVE", "right":{"type":"EMA","period":200}, "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
        ]
    },
    "bearish_ema_alignment": {
        "name": "Bearish EMA Alignment (10<20<50<100<200)",
        "rules": [
            {"left":{"type":"EMA","period":10},  "operator":"BELOW", "right":{"type":"EMA","period":20},  "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":20},  "operator":"BELOW", "right":{"type":"EMA","period":50},  "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":50},  "operator":"BELOW", "right":{"type":"EMA","period":100}, "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":100}, "operator":"BELOW", "right":{"type":"EMA","period":200}, "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
        ]
    },
    "price_above_ema20": {
        "name": "Price Above EMA 20",
        "rules": [{"left":{"type":"PRICE","period":0}, "operator":"ABOVE",
                   "right":{"type":"EMA","period":20}, "lookback":5,
                   "slope_days":10, "enabled":True, "logic":"AND"}]
    },
    "price_above_ema50": {
        "name": "Price Above EMA 50",
        "rules": [{"left":{"type":"PRICE","period":0}, "operator":"ABOVE",
                   "right":{"type":"EMA","period":50}, "lookback":5,
                   "slope_days":10, "enabled":True, "logic":"AND"}]
    },
    "price_above_sma200": {
        "name": "Price Above SMA 200",
        "rules": [{"left":{"type":"PRICE","period":0}, "operator":"ABOVE",
                   "right":{"type":"SMA","period":200}, "lookback":5,
                   "slope_days":10, "enabled":True, "logic":"AND"}]
    },
    "short_term_bullish": {
        "name": "Short-Term Bullish Alignment (10>20>50)",
        "rules": [
            {"left":{"type":"EMA","period":10}, "operator":"ABOVE", "right":{"type":"EMA","period":20}, "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":20}, "operator":"ABOVE", "right":{"type":"EMA","period":50}, "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
        ]
    },
    "long_term_bullish": {
        "name": "Long-Term Bullish Alignment (50>100>200)",
        "rules": [
            {"left":{"type":"EMA","period":50},  "operator":"ABOVE", "right":{"type":"EMA","period":100}, "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":100}, "operator":"ABOVE", "right":{"type":"EMA","period":200}, "lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
        ]
    },
    "triple_crossover": {
        "name": "Triple EMA Crossover (10×20, 20×50, 50×100)",
        "rules": [
            {"left":{"type":"EMA","period":10}, "operator":"CROSS_ABOVE", "right":{"type":"EMA","period":20},  "lookback":5, "slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":20}, "operator":"CROSS_ABOVE", "right":{"type":"EMA","period":50},  "lookback":10,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":50}, "operator":"CROSS_ABOVE", "right":{"type":"EMA","period":100}, "lookback":20,"slope_days":10,"enabled":True,"logic":"AND"},
        ]
    },
}

# ── Progress ──────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_PROG = {"active": False, "market": "", "processed": 0,
          "total": 0, "stage": "idle", "error": None}

def _set(**kw):
    with _lock: _PROG.update(kw)

def _get():
    with _lock: return dict(_PROG)


# ── Indicator calculation (vectorized, no look-ahead) ─────────────────────────

def _calc_ma(close: pd.Series, ma_type: str, period: int) -> pd.Series:
    """
    Calculate EMA or SMA.
    No look-ahead: each value at index i uses only data[0..i].
    EMA uses adjust=False (standard recursive EMA, not one-pass approximation).
    """
    if ma_type == "EMA":
        return close.ewm(span=period, adjust=False).mean()
    return close.rolling(window=period).mean()


def _build_indicator_cache(close: pd.Series, rules: list[dict]) -> dict:
    """
    Compute only the indicators required by the active rules.
    Returns {"EMA_20": Series, "SMA_50": Series, ...}
    """
    needed = set()
    for r in rules:
        if not r.get("enabled", True):
            continue
        for side in ("left", "right"):
            s = r.get(side, {})
            if s.get("type") in ("EMA", "SMA") and int(s.get("period", 0)) > 0:
                needed.add((s["type"], int(s["period"])))

    cache = {}
    for ma_type, period in needed:
        key = f"{ma_type}_{period}"
        if len(close) >= period:
            cache[key] = _calc_ma(close, ma_type, period)
        else:
            cache[key] = pd.Series([np.nan] * len(close), index=close.index)
    return cache


def _get_series_at(close: pd.Series, side: dict,
                   ind_cache: dict, offset: int = 0) -> float | None:
    """
    Return the value of a left/right operand at position [-1 - offset].
    offset=0 → today, offset=1 → yesterday. No look-ahead.
    """
    t = side.get("type", "")
    p = int(side.get("period", 0))
    idx = -(1 + offset)

    if t == "PRICE":
        try: return float(close.iloc[idx])
        except IndexError: return None

    key = f"{t}_{p}"
    ser = ind_cache.get(key)
    if ser is None or ser.empty:
        return None
    try:
        val = float(ser.iloc[idx])
        return None if np.isnan(val) else val
    except IndexError:
        return None


# ── Rule engine ───────────────────────────────────────────────────────────────

def evaluate_rules(df: pd.DataFrame, rules: list[dict],
                   prebuilt_ind: dict | None = None) -> tuple[bool, list[str]]:
    """
    Evaluate a list of rules against a DataFrame.

    Returns (passed: bool, explanations: list[str]).
    Rules are combined left-to-right using each rule's 'logic' field:
      AND → running result &= this_rule
      OR  → running result |= this_rule
    Disabled rules are skipped (not counted).

    No look-ahead: we only read index [-1] for current and [-2..] for history.

    prebuilt_ind: optional pre-computed indicator cache (avoids double-compute
    when the caller already built the cache for ma_values snapshot).
    """
    if df is None or df.empty or len(df) < 10:
        return False, ["Insufficient data"]

    close     = df["Close"].dropna()
    ind       = prebuilt_ind if prebuilt_ind is not None else _build_indicator_cache(close, rules)
    result    = True   # identity for AND chain
    first     = True
    explain   = []
    has_or    = any(r.get("logic") == "OR" for r in rules if r.get("enabled", True))

    for rule in rules:
        if not rule.get("enabled", True):
            continue

        op        = rule.get("operator", "ABOVE")
        left_spec = rule.get("left",  {"type": "PRICE", "period": 0})
        right_spec= rule.get("right", {"type": "EMA",   "period": 20})
        lookback  = int(rule.get("lookback", 5))
        slope_days= int(rule.get("slope_days", 10))
        logic     = rule.get("logic", "AND")

        passed, desc = _eval_single_rule(
            close, ind, left_spec, right_spec, op, lookback, slope_days
        )

        if first:
            result = passed
            first  = False
        elif logic == "OR":
            result = result or passed
        else:
            result = result and passed

        explain.append(f"{'✅' if passed else '❌'} {desc}")

    return result, explain


def _rule_label(spec: dict) -> str:
    t = spec.get("type", "")
    p = spec.get("period", 0)
    return "Price" if t == "PRICE" else f"{t} {p}"


def _eval_single_rule(close, ind, left_spec, right_spec,
                       op, lookback, slope_days) -> tuple[bool, str]:
    """Evaluate one rule. Returns (passed, human-readable description)."""

    L = _rule_label(left_spec)
    R = _rule_label(right_spec)

    if op in ("CROSS_ABOVE", "CROSS_BELOW"):
        # Scan backward up to `lookback` days for a crossover event.
        # A crossover on day D means:
        #   CROSS_ABOVE: series_left[D-1] <= series_right[D-1]
        #             AND series_left[D]  >  series_right[D]
        # We test D = today, yesterday, ..., today-lookback+1
        # to find the most recent occurrence.
        found_day = None
        for d in range(lookback):
            cur_l  = _get_series_at(close, left_spec,  ind, offset=d)
            cur_r  = _get_series_at(close, right_spec, ind, offset=d)
            prev_l = _get_series_at(close, left_spec,  ind, offset=d + 1)
            prev_r = _get_series_at(close, right_spec, ind, offset=d + 1)
            if None in (cur_l, cur_r, prev_l, prev_r):
                continue
            if op == "CROSS_ABOVE":
                if prev_l <= prev_r and cur_l > cur_r:
                    found_day = d
                    break
            else:  # CROSS_BELOW
                if prev_l >= prev_r and cur_l < cur_r:
                    found_day = d
                    break

        day_str  = "today" if found_day == 0 else f"{found_day}d ago"
        verb     = "crossed above" if op == "CROSS_ABOVE" else "crossed below"
        passed   = found_day is not None
        desc     = (f"{L} {verb} {R} ({day_str})" if passed
                    else f"{L} did NOT {verb} {R} within {lookback}d")
        return passed, desc

    if op in ("ABOVE", "BELOW"):
        cur_l = _get_series_at(close, left_spec,  ind, offset=0)
        cur_r = _get_series_at(close, right_spec, ind, offset=0)
        if cur_l is None or cur_r is None:
            return False, f"{L} or {R} has no data"
        passed = (cur_l > cur_r) if op == "ABOVE" else (cur_l < cur_r)
        verb   = ">" if op == "ABOVE" else "<"
        desc   = f"{L} ({cur_l:.2f}) {verb} {R} ({cur_r:.2f})"
        return passed, desc

    if op in ("RISING", "FALLING"):
        # MA today vs MA N days ago. PRICE is not meaningful here.
        cur  = _get_series_at(close, left_spec, ind, offset=0)
        past = _get_series_at(close, left_spec, ind, offset=slope_days)
        if cur is None or past is None:
            return False, f"{L} slope data unavailable"
        passed = (cur > past) if op == "RISING" else (cur < past)
        verb   = "rising" if op == "RISING" else "falling"
        desc   = f"{L} is {verb} over {slope_days}d ({past:.2f} → {cur:.2f})"
        return passed, desc

    return False, f"Unknown operator: {op}"


# ── Symbol loader ─────────────────────────────────────────────────────────────

def _load_symbols(market: str) -> list[dict]:
    cfg  = _MARKET[market]
    path = cfg["default_csv"]
    if not os.path.exists(path):
        return []
    try:
        df   = pd.read_csv(path)
        cols = {c.lower().strip(): c for c in df.columns}
        col  = next((cols[k] for k in ('symbol','ticker','symbols') if k in cols), None)
        sec_col = next((cols[k] for k in ('gics sector','sector','industry') if k in cols), None)
        if not col: return []
        out = []
        for _, row in df.iterrows():
            raw = str(row[col]).strip().upper().lstrip('$').replace('.', '-')
            sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
            if raw and raw not in ('SYMBOL','TICKER','N/A'):
                out.append({'symbol': raw, 'yf_sym': f"{raw}{cfg['suffix']}", 'sector': sec})
        return out
    except Exception as e:
        print(f"[MA] load_symbols error: {e}")
        return []


# ── Background scan ───────────────────────────────────────────────────────────

def run_scan(market: str, rules: list[dict], scan_name: str):
    try:
        _run_scan_inner(market, rules, scan_name)
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set(active=False, stage="error", error=str(e)[:120])


def _run_scan_inner(market: str, rules: list[dict], scan_name: str):
    cfg   = _MARKET[market]
    cache = cfg["cache"]

    _set(active=True, market=market, processed=0, total=0,
         stage="loading", error=None)

    tickers  = _load_symbols(market)
    yf_syms  = [t['yf_sym'] for t in tickers]
    sym_meta = {t['yf_sym']: t for t in tickers}
    _set(total=len(yf_syms))

    # Determine minimum lookback needed
    max_period = 200
    for r in rules:
        for side in ("left", "right"):
            p = r.get(side, {}).get("period", 0)
            if p: max_period = max(max_period, int(p))
    max_lookback = max(5, max(r.get("lookback", 5) for r in rules))
    needed_bars  = max_period + max_lookback + 10

    _set(stage="fetching")
    price_data, fetch_report = cache.get_price_history_bulk(
        yf_syms, interval='1d', lookback_days=needed_bars,
        progress_callback=lambda i, t, s: _set(processed=i, total=t)
    )
    price_data_asof = latest_bar_date(price_data)
    _ch, _yf = fetch_report["from_cache"], fetch_report["fetched"]
    print(f"[MA/{market}] {len(yf_syms)} syms | Cache:{_ch} | YF:{_yf} | Rules:{len(rules)}")

    _set(stage="screening", processed=0)
    results = []
    for i, yf_sym in enumerate(yf_syms):
        _set(processed=i)
        df   = price_data.get(yf_sym)
        meta = sym_meta[yf_sym]
        if df is None or df.empty or len(df) < max_period:
            continue
        try:
            close = df["Close"].dropna()
            ind   = _build_indicator_cache(close, rules)   # build ONCE — reused below
            passed, explain = evaluate_rules(df, rules, prebuilt_ind=ind)
        except Exception as e:
            print(f"[MA] {yf_sym}: {e}")
            continue
        if not passed:
            continue

        # Snapshot current MA values for the results table (reuses ind — no extra calc)
        ma_values = {}
        for r in rules:
            for side in ("left", "right"):
                s = r.get(side, {})
                if s.get("type") in ("EMA", "SMA"):
                    key = f"{s['type']}_{s['period']}"
                    if key not in ma_values:
                        val = _get_series_at(close, s, ind, offset=0)
                        if val is not None:
                            ma_values[key] = round(val, 2)

        results.append({
            "symbol":    meta['symbol'],
            "sector":    meta['sector'],
            "price":     round(float(close.iloc[-1]), 2),
            "explain":   explain,
            "ma_values": ma_values,
        })

    last_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    snap_file = f"ma_{market.lower()}_{uuid.uuid4().hex}.json"

    payload = {
        "stocks":          results,
        "time":            last_time,
        "market":          market,
        "scan_name":       scan_name,
        "rules":           rules,
        "scanned_count":   len(yf_syms),
        "passed_count":    len(results),
        "price_data_asof": price_data_asof,
        "cache_hits":      _ch,
        "yf_fetches":      _yf,
    }

    results_path = os.path.join(_BASE, f"ma_{market.lower()}_results.json")
    with open(os.path.join(_SNAP_DIR, snap_file), 'w') as f:
        json.dump(payload, f)
    with open(results_path, 'w') as f:
        json.dump(payload, f)

    # History
    history = _load_history()
    history.insert(0, {
        "time":            last_time,
        "market":          market,
        "scan_name":       scan_name,
        "count":           len(results),
        "scanned_count":   len(yf_syms),
        "price_data_asof": price_data_asof,
        "snapshot_file":   snap_file,
        "rules_count":     len([r for r in rules if r.get("enabled", True)]),
    })
    history = history[:HISTORY_LIMIT]
    keep = {h["snapshot_file"] for h in history if h.get("snapshot_file")}
    for f in os.listdir(_SNAP_DIR):
        if f not in keep:
            try: os.remove(os.path.join(_SNAP_DIR, f))
            except OSError: pass
    with open(_HIST_JSON, 'w') as f:
        json.dump(history, f)

    _set(active=False, stage="done")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_results(market: str) -> dict:
    path = os.path.join(_BASE, f"ma_{market.lower()}_results.json")
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except (json.JSONDecodeError, OSError): pass
    return {}

def _load_history() -> list:
    if os.path.exists(_HIST_JSON):
        try:
            with open(_HIST_JSON) as f: return json.load(f)
        except (json.JSONDecodeError, OSError): pass
    return []

def _load_saved_scans() -> dict:
    out = {}
    for fn in os.listdir(_SAVED_DIR):
        if fn.endswith('.json'):
            try:
                with open(os.path.join(_SAVED_DIR, fn)) as f:
                    data = json.load(f)
                out[fn[:-5]] = data
            except (json.JSONDecodeError, OSError):
                pass
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@ma_screener_bp.route("/ma-screener", methods=["GET", "POST"])
def ma_screener_view():
    if request.method == "POST":
        market    = request.form.get("market", "IND").upper()
        scan_name = request.form.get("scan_name", "Custom Scan").strip()
        rules_raw = request.form.get("rules_json", "[]")
        try:
            rules = json.loads(rules_raw)
        except (json.JSONDecodeError, TypeError):
            rules = []

        if not rules:
            preset_key = request.form.get("preset_key", "")
            if preset_key and preset_key in PREDEFINED_SCANS:
                rules     = PREDEFINED_SCANS[preset_key]["rules"]
                scan_name = PREDEFINED_SCANS[preset_key]["name"]

        if not rules:
            return redirect(url_for("ma_screener.ma_screener_view",
                                    market=market, error="no_rules"))

        if not _get()["active"]:
            t = threading.Thread(target=run_scan,
                                 args=(market, rules, scan_name), daemon=True)
            t.start()
        return redirect(url_for("ma_screener.ma_screener_view",
                                market=market, scanning=1))

    market  = request.args.get("market", "IND").upper()
    if market not in _MARKET: market = "IND"

    data    = _load_results(market)
    prog    = _get()
    history = _load_history()
    saved   = _load_saved_scans()

    return render_template(
        "ma_screener.html",
        stocks           = data.get("stocks", []),
        last_time        = data.get("time"),
        scan_name        = data.get("scan_name", ""),
        rules_used       = data.get("rules", []),
        scanned_count    = data.get("scanned_count", 0),
        passed_count     = data.get("passed_count", 0),
        price_data_asof  = data.get("price_data_asof"),
        cache_hits       = data.get("cache_hits", 0),
        yf_fetches       = data.get("yf_fetches", 0),
        market           = market,
        currency         = _MARKET[market]["currency"],
        history          = history,
        saved_scans      = saved,
        predefined_scans = PREDEFINED_SCANS,
        is_scanning      = prog["active"] and prog["market"] == market,
        scan_error       = prog.get("error") if not prog["active"] else None,
        error_flag       = request.args.get("error"),
        restored         = request.args.get("restored") == "1",
    )


@ma_screener_bp.route("/ma-screener/progress")
def ma_progress():
    return jsonify(_get())


@ma_screener_bp.route("/ma-screener/restore/<snapshot_file>", methods=["POST"])
def ma_restore(snapshot_file):
    market = request.form.get("market", "IND").upper()
    safe   = os.path.basename(snapshot_file)
    snap   = os.path.join(_SNAP_DIR, safe)
    if not os.path.exists(snap):
        return redirect(url_for("ma_screener.ma_screener_view", market=market))
    try:
        with open(snap) as f: payload = json.load(f)
        dest = os.path.join(_BASE, f"ma_{market.lower()}_results.json")
        with open(dest, 'w') as f: json.dump(payload, f)
    except Exception:
        pass
    return redirect(url_for("ma_screener.ma_screener_view",
                            market=market, restored=1))


@ma_screener_bp.route("/ma-screener/export")
def ma_export():
    market = request.args.get("market", "IND").upper()
    data   = _load_results(market)
    stocks = data.get("stocks", [])
    if not stocks:
        return "No data.", 404
    rows = []
    for s in stocks:
        row = {"symbol": s["symbol"], "sector": s["sector"],
               "price": s["price"], "market": market,
               "scan_name": data.get("scan_name", "")}
        row.update(s.get("ma_values", {}))
        row["why_passed"] = " | ".join(s.get("explain", []))
        rows.append(row)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp = os.path.join(_BASE, f"tmp_ma_{market}.csv")
    pd.DataFrame(rows).to_csv(tmp, index=False)
    return send_file(tmp, as_attachment=True,
                     download_name=f"MA_Screener_{market}_{ts}.csv")


@ma_screener_bp.route("/ma-screener/save-scan", methods=["POST"])
def ma_save_scan():
    name      = request.form.get("name", "").strip()
    rules_raw = request.form.get("rules_json", "[]")
    market    = request.form.get("market", "IND").upper()
    if not name:
        return jsonify({"status": "error", "message": "Name required"})
    try:
        rules = json.loads(rules_raw)
    except (json.JSONDecodeError, TypeError):
        return jsonify({"status": "error", "message": "Invalid rules"})
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)[:50]
    path      = os.path.join(_SAVED_DIR, f"{safe_name}.json")
    with open(path, 'w') as f:
        json.dump({"name": name, "rules": rules, "market": market,
                   "saved_at": datetime.now().isoformat()}, f)
    return jsonify({"status": "ok", "name": safe_name})


@ma_screener_bp.route("/ma-screener/delete-scan/<name>", methods=["POST"])
def ma_delete_scan(name):
    safe = os.path.basename(name)
    path = os.path.join(_SAVED_DIR, f"{safe}.json")
    if os.path.exists(path):
        try: os.remove(path)
        except OSError: pass
    return jsonify({"status": "ok"})


@ma_screener_bp.route("/ma-screener/guide")
def ma_guide():
    return render_template("ma_screener_guide.html")