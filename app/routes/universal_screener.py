"""
universal_screener.py — Config-Driven Universal Stock Screener

Architecture
────────────
One pipeline per scan config. Each config specifies:
  - market (IND / US)
  - universe (default CSV or uploaded file)
  - fixed_filters: dict of {filter_name: {enabled, threshold, operator}}
  - ma_rules: list of MA rule dicts (same format as ma_screener.py)
  - visible_columns: list of column keys to show in the results table

The pipeline runs all enabled compute functions against each stock's
DataFrame, then applies all active conditions. Stocks that pass all
conditions appear in the results table with only the selected columns.

Compute functions are IMPORTED from existing screeners — no duplication.
Each function runs independently; a failure in one doesn't block others.

IND benchmark: ^CRSLDX with ^NSEI fallback (Memory #1)
US  benchmark: ^GSPC
"""

import os
import json
import uuid
import threading
import traceback
import numpy as np
import pandas as pd
from datetime import datetime
from flask import (Blueprint, render_template, request,
                   redirect, url_for, jsonify, send_file)
from werkzeug.utils import secure_filename

from app.services.market_data_cache import ind_cache, us_cache, latest_bar_date

# ── Import compute functions from existing screeners ─────────────────────────
# Stage 2 conditions
from app.routes.stage2_screener_us import _screen_symbol as _stage2_us
from app.routes.stage2_india        import _screen_symbol as _stage2_ind

# VCP
from app.routes.vcp_screener import _is_vcp, _detect_contractions

# MA rules engine
from app.routes.ma_screener import (
    evaluate_rules, _calc_ma, _build_indicator_cache, _get_series_at
)

# Trendline / breakout
from app.routes.trendline_screener import (
    detect_trendline_breakout, check_52w_breakout,
    check_horizontal_breakout, calculate_rsi, _normalise_df
)

# VOLAR
from app.routes.volar_stage2_ind_screener import (
    compute_volar, compute_relative_strength, is_volar_candidate
)

# ── Blueprint ─────────────────────────────────────────────────────────────────
universal_bp = Blueprint("universal_screener", __name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT  = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_BASE          = os.path.join(_PROJECT_ROOT, 'uploads', 'universal_screener')
_SNAP_DIR      = os.path.join(_BASE, 'snapshots')
_SAVED_DIR     = os.path.join(_BASE, 'saved_configs')
_HIST_JSON     = os.path.join(_BASE, 'universal_history.json')

for _d in (_BASE, _SNAP_DIR, _SAVED_DIR):
    os.makedirs(_d, exist_ok=True)

HISTORY_LIMIT = 5

# ── Market config ─────────────────────────────────────────────────────────────
_MARKET = {
    "IND": {
        "cache":          ind_cache,
        "default_csv":    os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv'),
        "default_label":  "Nifty 500",
        "suffix":         ".NS",
        "currency":       "₹",
        "benchmark_sym":  "^CRSLDX",
        "bench_fallback": "^NSEI",
    },
    "US": {
        "cache":          us_cache,
        "default_csv":    os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv'),
        "default_label":  "S&P 500",
        "suffix":         "",
        "currency":       "$",
        "benchmark_sym":  "^GSPC",
        "bench_fallback": None,
    },
}

# ── Filter catalogue (all fixed filters with defaults) ────────────────────────
# Each fixed filter is one named boolean check with optional numeric threshold.
FILTER_CATALOGUE = {
    # ── Stage 2 ──────────────────────────────────────────────────────────────
    "stage2_trend_score": {
        "label":       "Stage 2 Trend Template",
        "category":    "Stage 2",
        "description": "Minervini's 6-condition template (price>MA150>MA200, MA50 stack, 52W range). Score = how many of 6 pass.",
        "type":        "threshold",
        "default_op":  ">=",
        "default_val": 5,
        "min": 1, "max": 6,
        "column_key":  "trend_score",
        "column_label":"Trend Score /6",
    },
    "price_above_ema200": {
        "label":       "Price Above EMA 200",
        "category":    "Stage 2",
        "description": "Current price is above the 200-day exponential moving average.",
        "type":        "boolean",
        "column_key":  "above_ema200",
        "column_label":"EMA 200",
    },
    "ma200_rising": {
        "label":       "MA200 Rising",
        "category":    "Stage 2",
        "description": "200-day MA is higher today than it was 20 sessions ago (upward slope).",
        "type":        "boolean",
        "column_key":  "ma200_rising",
        "column_label":"MA200 ↗",
    },
    "ma200_ext": {
        "label":       "MA200 Extension",
        "category":    "Stage 2",
        "description": "Price / MA200. Shows how extended the stock is above its long-term average.",
        "type":        "threshold",
        "default_op":  ">=",
        "default_val": 1.1,
        "min": 0.5, "max": 5.0,
        "column_key":  "ma200_ext",
        "column_label":"MA200 Ext×",
    },
    "retracement": {
        "label":       "52W Pullback %",
        "category":    "Stage 2",
        "description": "(52W_high − price) / 52W_high × 100. Lower = closer to highs.",
        "type":        "threshold",
        "default_op":  "<=",
        "default_val": 15,
        "min": 1, "max": 50,
        "column_key":  "retracement",
        "column_label":"52W Pull %",
    },

    # ── Relative Strength ────────────────────────────────────────────────────
    "rs_percentile": {
        "label":       "RS Percentile",
        "category":    "Relative Strength",
        "description": "RS ratio vs benchmark ranked 1–99 within the scanned universe.",
        "type":        "threshold",
        "default_op":  ">=",
        "default_val": 70,
        "min": 1, "max": 99,
        "column_key":  "rs_percentile",
        "column_label":"RS %tile",
    },
    "rs_trend": {
        "label":       "RS Trend",
        "category":    "Relative Strength",
        "description": "Direction of RS ratio change over 20 sessions.",
        "type":        "choice",
        "choices":     ["Accelerating", "Steady", "Fading"],
        "default_val": "Accelerating",
        "column_key":  "rs_trend",
        "column_label":"RS Trend",
    },
    "rs_at_52wh": {
        "label":       "RS at 52-Week High",
        "category":    "Relative Strength",
        "description": "RS ratio line at or near a 52-week high — strongest RS signal.",
        "type":        "boolean",
        "column_key":  "rs_at_52wh",
        "column_label":"★ RS 52WH",
    },

    # ── VOLAR ────────────────────────────────────────────────────────────────
    "volar": {
        "label":       "VOLAR (3M)",
        "category":    "VOLAR",
        "description": "3M return / std(daily returns). Higher = smoother, more orderly advance.",
        "type":        "threshold",
        "default_op":  ">=",
        "default_val": 0.8,
        "min": 0.0, "max": 10.0,
        "column_key":  "volar",
        "column_label":"VOLAR",
    },
    "r_squared": {
        "label":       "Trend R² (Smoothness)",
        "category":    "VOLAR",
        "description": "R² of price vs linear regression line in base. Higher = more orderly trend.",
        "type":        "threshold",
        "default_op":  ">=",
        "default_val": 0.50,
        "min": 0.0, "max": 1.0,
        "column_key":  "r_squared",
        "column_label":"R²",
    },

    # ── VCP ──────────────────────────────────────────────────────────────────
    "vcp_active": {
        "label":       "VCP Pattern Active",
        "category":    "VCP",
        "description": "Stock is forming or has formed a Volatility Contraction Pattern.",
        "type":        "boolean",
        "column_key":  "vcp_status",
        "column_label":"VCP Status",
    },
    "vcp_n_contractions": {
        "label":       "VCP Contractions (T value)",
        "category":    "VCP",
        "description": "Minimum number of tightening contractions. T≥3 = higher quality VCP.",
        "type":        "threshold",
        "default_op":  ">=",
        "default_val": 2,
        "min": 1, "max": 6,
        "column_key":  "n_contractions",
        "column_label":"VCP T=",
    },
    "vcp_vol_dryup": {
        "label":       "VCP Volume Dry-Up",
        "category":    "VCP",
        "description": "Volume contracts on each successive VCP trough — institutional accumulation signal.",
        "type":        "boolean",
        "column_key":  "vol_dryup",
        "column_label":"Vol Dry-Up",
    },
    "vcp_near_pivot": {
        "label":       "VCP Near Pivot (≤10%)",
        "category":    "VCP",
        "description": "Price within 10% of the VCP pivot point (buy trigger level).",
        "type":        "boolean",
        "column_key":  "near_pivot",
        "column_label":"Near Pivot",
    },

    # ── Trendline / Breakout ─────────────────────────────────────────────────
    "tl_breakout": {
        "label":       "Trendline Breakout",
        "category":    "Breakout",
        "description": "Price crossed above a descending resistance trendline with volume confirmation.",
        "type":        "boolean",
        "column_key":  "tl_breakout",
        "column_label":"TL Break",
    },
    "hizone_52w": {
        "label":       "Near 52-Week High",
        "category":    "Breakout",
        "description": "Price within 3% of its 52-week high — in leadership territory.",
        "type":        "boolean",
        "column_key":  "near_52wh",
        "column_label":"52W High Zone",
    },
    "horiz_breakout": {
        "label":       "Horizontal Breakout",
        "category":    "Breakout",
        "description": "Price crossed above the prior 60-bar high with positive momentum.",
        "type":        "boolean",
        "column_key":  "horiz_break",
        "column_label":"Horiz Break",
    },

    # ── Volume ───────────────────────────────────────────────────────────────
    "vol_surge": {
        "label":       "Volume Surge",
        "category":    "Volume",
        "description": "Today's volume exceeds the 50-day average.",
        "type":        "threshold",
        "default_op":  ">=",
        "default_val": 1.5,
        "min": 1.0, "max": 5.0,
        "column_key":  "vol_ratio",
        "column_label":"Vol Ratio×",
    },

    # ── RSI ──────────────────────────────────────────────────────────────────
    "rsi_filter": {
        "label":       "RSI (14)",
        "category":    "Momentum",
        "description": "RSI between two values — avoids overbought (>80) and oversold (<30) stocks.",
        "type":        "range",
        "default_min": 50,
        "default_max": 75,
        "column_key":  "rsi",
        "column_label":"RSI",
    },
}

# ── Preset configs ────────────────────────────────────────────────────────────
PRESET_CONFIGS = {
    "stage2_leaders": {
        "name": "Stage 2 Leaders (RS + Trend)",
        "description": "Minervini Stage 2 + RS ≥ 70 + Accelerating RS",
        "fixed_filters": {
            "stage2_trend_score": {"enabled": True,  "op": ">=", "val": 5},
            "rs_percentile":      {"enabled": True,  "op": ">=", "val": 70},
            "rs_trend":           {"enabled": True,  "val": "Accelerating"},
            "retracement":        {"enabled": True,  "op": "<=", "val": 15},
        },
        "ma_rules": [],
        "visible_columns": ["price","trend_score","rs_percentile","rs_trend","retracement","ma200_ext"],
    },
    "vcp_setup": {
        "name": "VCP Near Breakout",
        "description": "Stage 2 + VCP with vol dry-up + near pivot",
        "fixed_filters": {
            "stage2_trend_score":  {"enabled": True,  "op": ">=", "val": 5},
            "vcp_active":          {"enabled": True},
            "vcp_n_contractions":  {"enabled": True,  "op": ">=", "val": 2},
            "vcp_vol_dryup":       {"enabled": True},
            "vcp_near_pivot":      {"enabled": True},
        },
        "ma_rules": [],
        "visible_columns": ["price","trend_score","n_contractions","vcp_status","vol_dryup","near_pivot","volar"],
    },
    "ma_momentum": {
        "name": "EMA Momentum Stack",
        "description": "Bullish EMA alignment 10>20>50 + RS ≥ 60",
        "fixed_filters": {
            "rs_percentile": {"enabled": True, "op": ">=", "val": 60},
            "retracement":   {"enabled": True, "op": "<=", "val": 20},
        },
        "ma_rules": [
            {"left":{"type":"EMA","period":10},"operator":"ABOVE","right":{"type":"EMA","period":20},"lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
            {"left":{"type":"EMA","period":20},"operator":"ABOVE","right":{"type":"EMA","period":50},"lookback":5,"slope_days":10,"enabled":True,"logic":"AND"},
        ],
        "visible_columns": ["price","rs_percentile","rs_trend","retracement","ma200_ext"],
    },
    "smooth_breakout": {
        "name": "Smooth Breakout Leaders",
        "description": "Horizontal/trendline breakout + high VOLAR + RS ≥ 70",
        "fixed_filters": {
            "rs_percentile":  {"enabled": True,  "op": ">=", "val": 70},
            "volar":          {"enabled": True,  "op": ">=", "val": 0.8},
            "r_squared":      {"enabled": True,  "op": ">=", "val": 0.50},
            "horiz_breakout": {"enabled": True},
        },
        "ma_rules": [],
        "visible_columns": ["price","rs_percentile","rs_trend","volar","r_squared","vol_ratio","near_52wh","horiz_break"],
    },
    "rs_52wh": {
        "name": "RS New High Leaders",
        "description": "RS ratio at 52-week high + Stage 2 + Vol surge",
        "fixed_filters": {
            "stage2_trend_score": {"enabled": True, "op": ">=", "val": 5},
            "rs_at_52wh":         {"enabled": True},
            "vol_surge":          {"enabled": True, "op": ">=", "val": 1.3},
        },
        "ma_rules": [],
        "visible_columns": ["price","trend_score","rs_percentile","rs_at_52wh","vol_ratio","retracement"],
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
            if raw and raw not in ('SYMBOL', 'TICKER', 'N/A'):
                out.append({'symbol': raw, 'yf_sym': f"{raw}{cfg['suffix']}", 'sector': sec})
        return out
    except Exception as e:
        print(f"[Universal] load_symbols error: {e}")
        return []


# ── Compute pipeline ──────────────────────────────────────────────────────────

def _compute_metrics(yf_sym: str, df: pd.DataFrame,
                     bench_close: pd.Series | None,
                     index_df: pd.DataFrame | None,
                     market: str,
                     active_filters: dict,
                     ma_rules: list) -> dict:
    """
    Run all enabled compute functions on one stock's DataFrame.
    Returns a flat metrics dict. Missing/failed metrics stay None.
    Only computes what's needed for the active filters — skips
    expensive functions when their filter group is disabled.
    """
    m = {}   # metrics accumulator

    if df is None or df.empty or len(df) < 50:
        return m

    # Normalise tz
    norm = _normalise_df(df, yf_sym)
    if norm is None or norm.empty:
        return m

    close  = norm['Close'].dropna()
    high_s = norm['High'].dropna()
    low_s  = norm['Low'].dropna()
    vol    = norm['Volume'].fillna(0)

    if len(close) < 50:
        return m

    curr_price = float(close.iloc[-1])
    m['price'] = round(curr_price, 2)

    # ── Price / MA based ─────────────────────────────────────────────────────
    need_ma = any(k in active_filters for k in (
        'stage2_trend_score','price_above_ema200','ma200_rising',
        'ma200_ext','retracement'
    ))
    if need_ma or ma_rules:
        if len(close) >= 200:
            ma50  = float(close.rolling(50).mean().iloc[-1])
            ma150 = float(close.rolling(150).mean().iloc[-1])
            ma200 = float(close.rolling(200).mean().iloc[-1])
            ma200_ago = float(close.rolling(200).mean().iloc[-22]) if len(close) >= 222 else ma200

            hi52  = float(high_s.tail(252).max()) if len(high_s) >= 60 else float(high_s.max())
            lo52  = float(low_s.tail(252).min())  if len(low_s)  >= 60 else float(low_s.min())

            # Stage 2 conditions
            conds = [
                curr_price > ma150 and curr_price > ma200,
                ma150 > ma200,
                ma200 > ma200_ago,
                ma50 > ma150 and ma50 > ma200,
                curr_price >= lo52 * 1.30,
                curr_price >= hi52 * 0.75,
            ]
            m['trend_score']  = sum(conds)
            m['above_ema200'] = bool(curr_price > ma200)
            m['ma200_rising'] = bool(ma200 > ma200_ago)
            m['ma200_ext']    = round(curr_price / ma200, 3) if ma200 > 0 else None
            m['retracement']  = round((hi52 - curr_price) / hi52 * 100, 2) if hi52 > 0 else None

    # ── RS metrics ──────────────────────────────────────────────────────────
    need_rs = any(k in active_filters for k in (
        'rs_percentile','rs_trend','rs_at_52wh'
    ))
    if need_rs and bench_close is not None and len(close) >= 63:
        try:
            bc = bench_close.reindex(close.index).ffill().bfill()
            s3 = (float(close.iloc[-1]) / float(close.iloc[-63])) - 1
            b3 = (float(bc.iloc[-1]) / float(bc.iloc[-63])) - 1
            rs_raw = ((1 + s3) / (1 + b3) - 1) if (1 + b3) != 0 else 0.0
            m['rs_raw'] = round(rs_raw, 4)

            # RS 52WH
            if len(bc.dropna()) >= 63:
                rs_series = close / bc.dropna().reindex(close.index).ffill()
                rs_series = rs_series.dropna()
                if len(rs_series) >= 252:
                    hi = float(rs_series.tail(252).max())
                    m['rs_at_52wh'] = bool(float(rs_series.iloc[-1]) >= hi * 0.995)
                else:
                    m['rs_at_52wh'] = False

            # RS Trend (20D pp)
            if len(close) >= 84 and len(bc.dropna()) >= 84:
                s20 = (float(close.iloc[-21]) / float(close.iloc[-84])) - 1
                b20 = (float(bc.iloc[-21]) / float(bc.iloc[-84])) - 1
                rs20 = ((1 + s20) / (1 + b20) - 1) if (1 + b20) != 0 else 0.0
                rs_chg = (rs_raw - rs20) * 100
                m['rs_change'] = round(rs_chg, 2)
                if rs_chg > 3.0:  m['rs_trend'] = "Accelerating"
                elif rs_chg >= 0: m['rs_trend'] = "Steady"
                else:             m['rs_trend'] = "Fading"
        except Exception as e:
            print(f"[Universal] RS error {yf_sym}: {e}")

    # ── VOLAR ────────────────────────────────────────────────────────────────
    need_volar = any(k in active_filters for k in ('volar','r_squared'))
    if need_volar and len(close) >= 63:
        try:
            c63   = close.tail(63)
            ret63 = c63.pct_change().dropna()
            ret_std = float(ret63.std()) if len(ret63) > 1 else 1e-9
            ret_3m  = (float(c63.iloc[-1]) / float(c63.iloc[0])) - 1
            m['volar'] = round(abs(ret_3m) / (ret_std + 1e-9), 2)

            # R²
            base_close = close.tail(max(60, len(close) // 2))
            x = np.arange(len(base_close))
            coeffs = np.polyfit(x, base_close.values, 1)
            y_hat  = np.polyval(coeffs, x)
            ss_res = float(np.sum((base_close.values - y_hat) ** 2))
            ss_tot = float(np.sum((base_close.values - base_close.mean()) ** 2))
            m['r_squared'] = round(1 - ss_res / (ss_tot + 1e-9), 3)
        except Exception as e:
            print(f"[Universal] VOLAR error {yf_sym}: {e}")

    # ── VCP ──────────────────────────────────────────────────────────────────
    need_vcp = any(k in active_filters for k in (
        'vcp_active','vcp_n_contractions','vcp_vol_dryup','vcp_near_pivot'
    ))
    if need_vcp and len(close) >= 100:
        try:
            vcp = _is_vcp(norm, market)
            if vcp:
                m['vcp_status']    = vcp.get('status', '')
                m['n_contractions']= vcp.get('n_contractions', 0)
                m['vol_dryup']     = vcp.get('vol_dryup', False)
                m['near_pivot']    = vcp.get('pullback_pct', 999) <= 10
                m['pivot']         = vcp.get('pivot')
            else:
                m['vcp_status'] = ''
                m['n_contractions'] = 0
                m['vol_dryup'] = False
                m['near_pivot'] = False
        except Exception as e:
            print(f"[Universal] VCP error {yf_sym}: {e}")

    # ── Trendline / Breakout ─────────────────────────────────────────────────
    need_tl = any(k in active_filters for k in (
        'tl_breakout','hizone_52w','horiz_breakout','rsi_filter'
    ))
    if need_tl and len(close) >= 60:
        try:
            has_tl, _, _, vol_ratio, rsi, _ = detect_trendline_breakout(norm)
            has_52w, _                       = check_52w_breakout(norm)
            has_horiz, _, h_vol              = check_horizontal_breakout(norm)

            hi52  = float(high_s.tail(252).max()) if len(high_s) >= 60 else 0
            near  = curr_price >= hi52 * 0.97 if hi52 > 0 else False

            avg_vol = float(vol.rolling(50).mean().iloc[-1]) if len(vol) >= 50 else float(vol.mean())
            avg_vol = max(avg_vol, 1.0)
            v_ratio = round(float(vol.iloc[-1]) / avg_vol, 2)

            m['tl_breakout'] = bool(has_tl)
            m['near_52wh']   = bool(near)
            m['horiz_break'] = bool(has_horiz)
            m['vol_ratio']   = v_ratio
            m['rsi']         = rsi if rsi else calculate_rsi(close.values, 14)
        except Exception as e:
            print(f"[Universal] Trendline error {yf_sym}: {e}")

    # ── Volume ────────────────────────────────────────────────────────────────
    if 'vol_surge' in active_filters and 'vol_ratio' not in m:
        try:
            avg_vol = float(vol.rolling(50).mean().iloc[-1]) if len(vol) >= 50 else float(vol.mean())
            m['vol_ratio'] = round(float(vol.iloc[-1]) / max(avg_vol, 1.0), 2)
        except Exception:
            pass

    # ── MA rules (from ma_screener engine) ───────────────────────────────────
    if ma_rules:
        try:
            passed, explain = evaluate_rules(norm, ma_rules)
            m['ma_rules_passed'] = passed
            m['ma_explain']      = explain
        except Exception as e:
            print(f"[Universal] MA rules error {yf_sym}: {e}")
            m['ma_rules_passed'] = False
            m['ma_explain']      = []

    return m


# ── Filter engine ─────────────────────────────────────────────────────────────

def _apply_filters(m: dict, fixed_filters: dict, ma_rules: list) -> tuple[bool, list[str]]:
    """
    Apply all enabled filters to a metrics dict.
    Returns (passed: bool, reasons: list[str]).
    All filters use AND logic.
    """
    if not m:
        return False, ["No data"]

    reasons = []

    for key, cfg in fixed_filters.items():
        if not cfg.get("enabled", False):
            continue

        spec    = FILTER_CATALOGUE.get(key, {})
        ftype   = spec.get("type", "boolean")
        col     = spec.get("column_key", key)
        val     = m.get(col)

        if ftype == "boolean":
            ok = bool(val)
            reasons.append(f"{'✅' if ok else '❌'} {spec.get('label', key)}: {val}")
            if not ok: return False, reasons

        elif ftype == "threshold":
            op      = cfg.get("op", ">=")
            thresh  = float(cfg.get("val", 0))
            if val is None:
                reasons.append(f"❌ {spec.get('label', key)}: no data")
                return False, reasons
            val_f = float(val)
            ok = (
                (op == ">=" and val_f >= thresh) or
                (op == "<=" and val_f <= thresh) or
                (op == ">"  and val_f >  thresh) or
                (op == "<"  and val_f <  thresh) or
                (op == "==" and val_f == thresh)
            )
            reasons.append(f"{'✅' if ok else '❌'} {spec.get('label', key)}: {val_f} {op} {thresh}")
            if not ok: return False, reasons

        elif ftype == "choice":
            expected = cfg.get("val", "")
            ok = (str(val) == expected) if val is not None else False
            reasons.append(f"{'✅' if ok else '❌'} {spec.get('label', key)}: {val}")
            if not ok: return False, reasons

        elif ftype == "range":
            lo = float(cfg.get("val_min", 0))
            hi = float(cfg.get("val_max", 100))
            if val is None:
                reasons.append(f"❌ {spec.get('label', key)}: no data")
                return False, reasons
            val_f = float(val)
            ok    = lo <= val_f <= hi
            reasons.append(f"{'✅' if ok else '❌'} {spec.get('label', key)}: {val_f} in [{lo}, {hi}]")
            if not ok: return False, reasons

    # MA rules
    if ma_rules:
        ok = m.get('ma_rules_passed', False)
        for line in m.get('ma_explain', []):
            reasons.append(line)
        if not ok: return False, reasons

    return True, reasons


# ── Background scan ───────────────────────────────────────────────────────────

def run_scan(market: str, config: dict):
    try:
        _run_inner(market, config)
    except Exception as e:
        traceback.print_exc()
        _set(active=False, stage="error", error=str(e)[:120])


def _run_inner(market: str, config: dict):
    cfg_m  = _MARKET[market]
    cache  = cfg_m["cache"]
    suffix = cfg_m["suffix"]

    scan_name     = config.get("name", "Custom Scan")
    fixed_filters = config.get("fixed_filters", {})
    ma_rules      = config.get("ma_rules", [])

    _set(active=True, market=market, processed=0, total=0,
         stage="loading", error=None)

    tickers  = _load_symbols(market)
    yf_syms  = [t['yf_sym'] for t in tickers]
    sym_meta = {t['yf_sym']: t for t in tickers}
    _set(total=len(yf_syms))

    # ── Benchmark (once) ─────────────────────────────────────────────────────
    _set(stage="benchmark")
    bench_close = None
    for bsym in filter(None, [cfg_m["benchmark_sym"], cfg_m.get("bench_fallback")]):
        res, _ = cache.get_price_history_bulk([bsym], interval='1d', lookback_days=500)
        bdf = res.get(bsym)
        if bdf is not None and not bdf.empty and len(bdf) >= 60:
            bench_close = bdf['Close'].dropna()
            if getattr(bench_close.index, 'tz', None) is not None:
                bench_close.index = bench_close.index.tz_localize(None)
            print(f"[Universal/{market}] Benchmark: {bsym} ({len(bench_close)} bars)")
            break

    # index_df for VOLAR candidate (IND only)
    index_df = None
    if market == "IND":
        try:
            from app.routes.volar_stage2_ind_screener import fetch_index_data
            index_df, _ = fetch_index_data()
        except Exception:
            pass

    # ── Bulk fetch ────────────────────────────────────────────────────────────
    _set(stage="fetching")
    price_data, fetch_report = cache.get_price_history_bulk(
        yf_syms, interval='1d', lookback_days=500,
        progress_callback=lambda i, t, s: _set(processed=i, total=t)
    )
    price_data_asof = latest_bar_date(price_data)
    _ch, _yf = fetch_report["from_cache"], fetch_report["fetched"]
    print(f"[Universal/{market}] {len(yf_syms)} syms | Cache:{_ch} | YF:{_yf}")

    # ── Screen ────────────────────────────────────────────────────────────────
    _set(stage="screening", processed=0)
    raw_results = []

    for i, yf_sym in enumerate(yf_syms):
        _set(processed=i)
        meta = sym_meta[yf_sym]
        df   = price_data.get(yf_sym)

        try:
            metrics = _compute_metrics(
                yf_sym, df, bench_close, index_df, market, fixed_filters, ma_rules
            )
            passed, reasons = _apply_filters(metrics, fixed_filters, ma_rules)
        except Exception as e:
            print(f"[Universal] {yf_sym}: {e}")
            continue

        if not passed:
            continue

        metrics['symbol'] = meta['symbol']
        metrics['sector'] = meta['sector']
        metrics['yf_sym'] = yf_sym
        metrics['reasons']= reasons
        raw_results.append(metrics)

    # RS percentile ranking across passing universe
    if raw_results:
        df_r = pd.DataFrame(raw_results)
        if 'rs_raw' in df_r.columns and df_r['rs_raw'].notna().any():
            df_r['rs_percentile'] = (
                df_r['rs_raw'].rank(pct=True).mul(98).add(1)
                .round(0).clip(1, 99).astype(int)
            )
        else:
            df_r['rs_percentile'] = 0
        df_r.sort_values('rs_percentile', ascending=False, inplace=True)
        results = df_r.to_dict(orient='records')
    else:
        results = []

    last_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    snap_file = f"universal_{market.lower()}_{uuid.uuid4().hex}.json"

    payload = {
        "stocks":           results,
        "time":             last_time,
        "market":           market,
        "scan_name":        scan_name,
        "config":           config,
        "scanned_count":    len(yf_syms),
        "passed_count":     len(results),
        "price_data_asof":  price_data_asof,
        "cache_hits":       _ch,
        "yf_fetches":       _yf,
    }

    results_path = os.path.join(_BASE, f"universal_{market.lower()}_results.json")
    with open(os.path.join(_SNAP_DIR, snap_file), 'w') as f:
        json.dump(payload, f, default=str)
    with open(results_path, 'w') as f:
        json.dump(payload, f, default=str)

    history = _load_history()
    history.insert(0, {
        "time":            last_time,
        "market":          market,
        "scan_name":       scan_name,
        "count":           len(results),
        "scanned_count":   len(yf_syms),
        "price_data_asof": price_data_asof,
        "snapshot_file":   snap_file,
        "filter_count":    sum(1 for v in fixed_filters.values() if v.get("enabled")),
        "ma_rule_count":   len([r for r in ma_rules if r.get("enabled", True)]),
    })
    history = history[:HISTORY_LIMIT]
    keep = {h["snapshot_file"] for h in history if h.get("snapshot_file")}
    for fn in os.listdir(_SNAP_DIR):
        if fn not in keep:
            try: os.remove(os.path.join(_SNAP_DIR, fn))
            except OSError: pass
    with open(_HIST_JSON, 'w') as f:
        json.dump(history, f)

    _set(active=False, stage="done")


# ── History helpers ───────────────────────────────────────────────────────────

def _load_history():
    if os.path.exists(_HIST_JSON):
        try:
            with open(_HIST_JSON) as f: return json.load(f)
        except (json.JSONDecodeError, OSError): pass
    return []

def _load_results(market: str) -> dict:
    path = os.path.join(_BASE, f"universal_{market.lower()}_results.json")
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except (json.JSONDecodeError, OSError): pass
    return {}

def _load_saved() -> dict:
    out = {}
    for fn in os.listdir(_SAVED_DIR):
        if fn.endswith('.json'):
            try:
                with open(os.path.join(_SAVED_DIR, fn)) as f:
                    out[fn[:-5]] = json.load(f)
            except (json.JSONDecodeError, OSError): pass
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@universal_bp.route("/universal-screener", methods=["GET", "POST"])
def universal_view():
    if request.method == "POST":
        market = request.form.get("market", "IND").upper()

        # Build config from form
        fixed_filters = {}
        for key, spec in FILTER_CATALOGUE.items():
            enabled = request.form.get(f"ff_{key}_enabled") == "1"
            if not enabled:
                continue
            fc = {"enabled": True}
            ftype = spec.get("type")
            if ftype == "threshold":
                fc["op"]  = request.form.get(f"ff_{key}_op",  spec.get("default_op", ">="))
                fc["val"] = float(request.form.get(f"ff_{key}_val",
                                  spec.get("default_val", 0)))
            elif ftype == "choice":
                fc["val"] = request.form.get(f"ff_{key}_val", spec.get("choices", [""])[0])
            elif ftype == "range":
                fc["val_min"] = float(request.form.get(f"ff_{key}_min", spec.get("default_min", 0)))
                fc["val_max"] = float(request.form.get(f"ff_{key}_max", spec.get("default_max", 100)))
            fixed_filters[key] = fc

        try:
            ma_rules = json.loads(request.form.get("ma_rules_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            ma_rules = []

        visible_columns = request.form.getlist("visible_cols")
        scan_name       = request.form.get("scan_name", "Custom Scan").strip()

        # Load preset if selected instead
        preset_key = request.form.get("preset_key", "")
        if preset_key and preset_key in PRESET_CONFIGS and not fixed_filters:
            preset  = PRESET_CONFIGS[preset_key]
            fixed_filters   = preset["fixed_filters"]
            ma_rules        = preset.get("ma_rules", [])
            visible_columns = preset.get("visible_columns", [])
            scan_name       = preset["name"]

        config = {
            "name":            scan_name,
            "fixed_filters":   fixed_filters,
            "ma_rules":        ma_rules,
            "visible_columns": visible_columns,
        }

        if not _get()["active"]:
            t = threading.Thread(target=run_scan, args=(market, config), daemon=True)
            t.start()
        return redirect(url_for("universal_screener.universal_view",
                                market=market, scanning=1))

    # GET
    market  = request.args.get("market", "IND").upper()
    if market not in _MARKET: market = "IND"

    data    = _load_results(market)
    prog    = _get()
    history = _load_history()
    saved   = _load_saved()

    # Reconstruct last config for pre-filling the form
    last_config = data.get("config", {})

    return render_template(
        "universal_screener.html",
        stocks            = data.get("stocks", []),
        last_time         = data.get("time"),
        scan_name         = data.get("scan_name", ""),
        scanned_count     = data.get("scanned_count", 0),
        passed_count      = data.get("passed_count", 0),
        price_data_asof   = data.get("price_data_asof"),
        cache_hits        = data.get("cache_hits", 0),
        yf_fetches        = data.get("yf_fetches", 0),
        market            = market,
        currency          = _MARKET[market]["currency"],
        history           = history,
        saved_configs     = saved,
        filter_catalogue  = FILTER_CATALOGUE,
        preset_configs    = PRESET_CONFIGS,
        last_config       = last_config,
        is_scanning       = prog["active"] and prog["market"] == market,
        scan_error        = prog.get("error") if not prog["active"] else None,
        restored          = request.args.get("restored") == "1",
    )


@universal_bp.route("/universal-screener/progress")
def universal_progress():
    return jsonify(_get())


@universal_bp.route("/universal-screener/restore/<snap>", methods=["POST"])
def universal_restore(snap):
    market = request.form.get("market", "IND").upper()
    safe   = os.path.basename(snap)
    path   = os.path.join(_SNAP_DIR, safe)
    if not os.path.exists(path):
        return redirect(url_for("universal_screener.universal_view", market=market))
    try:
        with open(path) as f: payload = json.load(f)
        with open(os.path.join(_BASE, f"universal_{market.lower()}_results.json"), 'w') as f:
            json.dump(payload, f, default=str)
    except Exception: pass
    return redirect(url_for("universal_screener.universal_view",
                            market=market, restored=1))


@universal_bp.route("/universal-screener/save-config", methods=["POST"])
def universal_save_config():
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Name required"})
    try:
        config = json.loads(request.form.get("config_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        return jsonify({"status": "error", "message": "Invalid config"})
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)[:50]
    with open(os.path.join(_SAVED_DIR, f"{safe}.json"), 'w') as f:
        json.dump({"name": name, **config,
                   "saved_at": datetime.now().isoformat()}, f)
    return jsonify({"status": "ok", "name": safe})


@universal_bp.route("/universal-screener/guide")
def universal_guide():
    return render_template("universal_screener_guide.html")


@universal_bp.route("/universal-screener/export")
def universal_export():
    market = request.args.get("market", "IND").upper()
    data   = _load_results(market)
    stocks = data.get("stocks", [])
    if not stocks:
        return "No data.", 404
    rows = []
    for s in stocks:
        row = {k: v for k, v in s.items()
               if k not in ('reasons', 'ma_explain', 'yf_sym')}
        row['why_passed'] = " | ".join(s.get('reasons', []))
        rows.append(row)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp = os.path.join(_BASE, f"tmp_universal_{market}.csv")
    pd.DataFrame(rows).to_csv(tmp, index=False)
    return send_file(tmp, as_attachment=True,
                     download_name=f"Universal_{market}_{ts}.csv")