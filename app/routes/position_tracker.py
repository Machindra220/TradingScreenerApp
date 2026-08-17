"""
position_tracker.py

Position Tracker — monitors all open trades with live refreshed metrics.

For each open position the tracker shows:
  - Current price vs entry price → unrealized P&L, R-multiple achieved
  - Current RS %tile → still leading or starting to fade?
  - Current vol_ratio → institutions still accumulating?
  - Closing % of most recent bar → did yesterday close strong or weak?
  - Days held
  - Stop distance (current price vs stop loss)
  - Exit signal flag: HIGH VOLUME + WEAK CLOSE = distribution warning

Exit signal logic:
  A distribution day is detected when:
    vol_ratio ≥ 1.5× avg  AND  closing_pct < 0.40 (closed in bottom 60% of range)
  This is the NSE equivalent of "institutions selling into retail buying."
  One distribution day = caution. Two in three days = book profits.
"""

import os
import json
import threading
import pandas as pd
from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, flash

from app.services.market_data_cache import ind_cache, latest_bar_date

position_tracker_bp = Blueprint("position_tracker", __name__)

_PROJECT_ROOT    = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DATA_DIR         = os.path.join(_PROJECT_ROOT, 'uploads', 'position_tracker')
POSITIONS_JSON   = os.path.join(DATA_DIR, 'open_positions.json')
REFRESH_LOCK_FILE = os.path.join(DATA_DIR, '.refresh_running')

os.makedirs(DATA_DIR, exist_ok=True)

PRIMARY_BENCHMARK  = ("^CRSLDX", "Nifty 500")
FALLBACK_BENCHMARK = ("^NSEI",   "Nifty 50")

_lock = threading.Lock()
_REFRESH = {"active": False, "stage": "idle", "error": None}

def _set_refresh(**kw):
    with _lock: _REFRESH.update(kw)

def _get_refresh():
    with _lock: return dict(_REFRESH)


# ---------------------------------------------------------------------------
# Position storage helpers
# ---------------------------------------------------------------------------

def _load_positions():
    if os.path.exists(POSITIONS_JSON):
        try:
            with open(POSITIONS_JSON) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_positions(positions):
    with open(POSITIONS_JSON, 'w') as f:
        json.dump(positions, f, default=str)


def _normalize_position(p):
    p.setdefault('id',               '')
    p.setdefault('symbol',           '')
    p.setdefault('entry_price',      0.0)
    p.setdefault('qty',              0)
    p.setdefault('stop_loss',        0.0)
    p.setdefault('target_1',         0.0)
    p.setdefault('target_2',         0.0)
    p.setdefault('entry_date',       '')
    p.setdefault('entry_rs_pct',     0)
    p.setdefault('notes',            '')
    # Live fields (refreshed)
    p.setdefault('current_price',    0.0)
    p.setdefault('unrealized_pnl',   0.0)
    p.setdefault('unrealized_pct',   0.0)
    p.setdefault('r_multiple',       0.0)
    p.setdefault('days_held',        0)
    p.setdefault('rs_percentile',    0)
    p.setdefault('vol_ratio',        1.0)
    p.setdefault('closing_pct',      50.0)
    p.setdefault('dist_days_3',      0)
    p.setdefault('exit_signal',      False)
    p.setdefault('exit_reason',      '')
    p.setdefault('stop_distance_pct', 0.0)
    p.setdefault('target1_distance_pct', 0.0)
    p.setdefault('momentum_alive',   True)
    p.setdefault('last_refreshed',   '')
    return p


# ---------------------------------------------------------------------------
# Live metric refresh
# ---------------------------------------------------------------------------

def _refresh_all():
    """Refresh live metrics for all open positions using ind_cache."""
    _set_refresh(active=True, stage="fetching_benchmark", error=None)

    positions = _load_positions()
    if not positions:
        _set_refresh(active=False, stage="done")
        return

    symbols_ns = []
    for p in positions:
        sym = p['symbol']
        yf_sym = sym if sym.endswith('.NS') else f"{sym}.NS"
        if yf_sym not in symbols_ns:
            symbols_ns.append(yf_sym)

    # Benchmark for RS calculation
    bench_close, _ = None, None
    for ticker, label in (PRIMARY_BENCHMARK, FALLBACK_BENCHMARK):
        data, _ = ind_cache.get_price_history_bulk([ticker], interval='1d', lookback_days=300)
        df = data.get(ticker)
        if df is not None and not df.empty and len(df) >= 200:
            bench_close = df['Close'].dropna()
            break

    _set_refresh(stage="fetching_prices")
    price_data, _ = ind_cache.get_price_history_bulk(symbols_ns, interval='1d', lookback_days=60)

    _set_refresh(stage="computing")
    now = datetime.now().strftime("%H:%M:%S")

    updated = []
    for p in positions:
        p = _normalize_position(p)
        sym   = p['symbol']
        yf_sym = sym if sym.endswith('.NS') else f"{sym}.NS"
        df    = price_data.get(yf_sym)

        if df is None or df.empty or len(df) < 5:
            updated.append(p)
            continue

        try:
            close  = df['Close'].dropna()
            volume = df['Volume'].dropna()

            curr_price = float(close.iloc[-1])
            entry      = float(p['entry_price'])
            stop       = float(p['stop_loss'])
            qty        = int(p['qty'])
            risk_per_share = entry - stop

            p['current_price']   = round(curr_price, 2)
            p['unrealized_pnl']  = round((curr_price - entry) * qty, 2)
            p['unrealized_pct']  = round((curr_price - entry) / entry * 100, 2)
            p['r_multiple']      = round((curr_price - entry) / risk_per_share, 2) if risk_per_share != 0 else 0

            # Days held
            try:
                ed = datetime.strptime(str(p['entry_date']), "%Y-%m-%d").date()
                p['days_held'] = (date.today() - ed).days
            except (ValueError, TypeError):
                p['days_held'] = 0

            # Stop and target distances
            p['stop_distance_pct']    = round((curr_price - stop) / curr_price * 100, 2)
            if p.get('target_1', 0) > 0:
                p['target1_distance_pct'] = round((p['target_1'] - curr_price) / curr_price * 100, 2)

            # Vol ratio (20-day avg)
            if len(volume) >= 22:
                avg_20d = float(volume.iloc[-21:-1].mean())
                curr_vol = float(volume.iloc[-1])
                p['vol_ratio'] = round(curr_vol / avg_20d, 2) if avg_20d > 0 else 1.0

            # Closing % of last bar
            if 'High' in df.columns and 'Low' in df.columns:
                day_high  = float(df['High'].iloc[-1])
                day_low   = float(df['Low'].iloc[-1])
                day_range = day_high - day_low
                p['closing_pct'] = round((curr_price - day_low) / day_range * 100, 1) if day_range > 0 else 50.0

            # Distribution day detection: high vol + weak close
            dist_days = 0
            for k in range(1, 4):
                if len(close) < k + 1 or len(volume) < k + 21:
                    break
                v_today = float(volume.iloc[-k])
                c_today = float(close.iloc[-k])
                avg_v   = float(volume.iloc[-(k+20):-(k)].mean()) if len(volume) >= k + 20 else 0
                vr      = v_today / avg_v if avg_v > 0 else 0
                h_today = float(df['High'].iloc[-k]) if 'High' in df.columns else c_today
                l_today = float(df['Low'].iloc[-k])  if 'Low'  in df.columns else c_today
                rng     = h_today - l_today
                cp      = (c_today - l_today) / rng if rng > 0 else 0.5
                if vr >= 1.5 and cp < 0.40:
                    dist_days += 1

            p['dist_days_3']  = dist_days
            p['exit_signal']  = dist_days >= 2
            p['exit_reason']  = "⚠️ 2+ distribution days — institutions selling" if p['exit_signal'] else ""

            # RS percentile (vs benchmark, all open positions pool)
            if bench_close is not None:
                bench_aligned = bench_close.reindex(close.index).ffill()
                if not bench_aligned.isna().any() and len(close) >= 63:
                    stock_ret = (float(close.iloc[-1]) / float(close.iloc[0])) - 1
                    bench_ret = (float(bench_aligned.iloc[-1]) / float(bench_aligned.iloc[0])) - 1
                    rs_raw    = ((1 + stock_ret) / (1 + bench_ret) - 1) if (1 + bench_ret) != 0 else 0
                    p['rs_raw_live'] = rs_raw

            # Momentum alive: price still above EMA200 and rs_pct still ≥ 50
            ema200 = close.ewm(span=min(200, len(close)), adjust=False).mean().iloc[-1]
            p['momentum_alive'] = curr_price > ema200
            p['last_refreshed'] = now

        except Exception as e:
            print(f"  Error refreshing {sym}: {e}")

        updated.append(p)

    # Compute live RS percentiles across all positions together
    live_rs = [(i, p.get('rs_raw_live', 0)) for i, p in enumerate(updated) if 'rs_raw_live' in p]
    if len(live_rs) > 1:
        import numpy as np
        vals = [v for _, v in live_rs]
        ranks = pd.Series(vals).rank(pct=True).mul(100).round(0).values
        for idx, (i, _) in enumerate(live_rs):
            updated[i]['rs_percentile'] = int(ranks[idx])
    elif len(live_rs) == 1:
        updated[live_rs[0][0]]['rs_percentile'] = 50

    _save_positions(updated)
    _set_refresh(active=False, stage="done")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@position_tracker_bp.route("/position-tracker", methods=["GET"])
def position_tracker_view():
    positions = [_normalize_position(p) for p in _load_positions()]
    refresh   = _get_refresh()

    # Summary stats
    total_pnl   = sum(p['unrealized_pnl'] for p in positions)
    open_count  = len(positions)
    winning     = sum(1 for p in positions if p['unrealized_pnl'] > 0)
    exit_alerts = sum(1 for p in positions if p['exit_signal'])
    best_r      = max((p['r_multiple'] for p in positions), default=0)

    return render_template(
        "position_tracker.html",
        positions=positions,
        total_pnl=round(total_pnl, 2),
        open_count=open_count,
        winning=winning,
        exit_alerts=exit_alerts,
        best_r=round(best_r, 2),
        is_refreshing=refresh["active"],
        refresh_error=refresh.get("error"),
        last_refreshed=positions[0].get('last_refreshed', '') if positions else '',
    )


@position_tracker_bp.route("/position-tracker/refresh", methods=["POST"])
def position_tracker_refresh():
    if not _get_refresh()["active"]:
        thread = threading.Thread(target=_refresh_all, daemon=True)
        thread.start()
    return redirect(url_for('position_tracker.position_tracker_view', refreshing=1))


@position_tracker_bp.route("/position-tracker/refresh-status")
def position_tracker_refresh_status():
    return jsonify(_get_refresh())


@position_tracker_bp.route("/position-tracker/add", methods=["POST"])
def position_add():
    """Add a new open position."""
    import uuid as _uuid
    try:
        entry_price = float(request.form['entry_price'])
        stop_loss   = float(request.form['stop_loss'])
        qty         = int(request.form['qty'])
        symbol      = request.form['symbol'].strip().upper().replace('.NS', '')
        t1          = float(request.form.get('target_1', 0) or 0)
        t2          = float(request.form.get('target_2', 0) or 0)
        notes       = request.form.get('notes', '').strip()
        entry_date  = request.form.get('entry_date', date.today().strftime("%Y-%m-%d"))

        # Auto-calculate targets if not provided
        risk = entry_price - stop_loss
        if t1 == 0 and risk > 0: t1 = round(entry_price + 2 * risk, 2)
        if t2 == 0 and risk > 0: t2 = round(entry_price + 4 * risk, 2)

        positions = _load_positions()
        positions.append({
            "id":          _uuid.uuid4().hex[:8],
            "symbol":      symbol,
            "entry_price": entry_price,
            "stop_loss":   stop_loss,
            "qty":         qty,
            "target_1":    t1,
            "target_2":    t2,
            "entry_date":  entry_date,
            "entry_rs_pct": int(request.form.get('entry_rs_pct', 0) or 0),
            "notes":       notes,
            "added_at":    datetime.now().strftime("%d-%b-%Y %H:%M"),
        })
        _save_positions(positions)
    except (ValueError, KeyError) as e:
        flash(f"Error adding position: {e}", "error")
    return redirect(url_for('position_tracker.position_tracker_view'))


@position_tracker_bp.route("/position-tracker/close/<position_id>", methods=["POST"])
def position_close(position_id):
    """Close a position — moves it to the Trade Journal."""
    positions = _load_positions()
    exit_price = float(request.form.get('exit_price', 0))
    exit_reason = request.form.get('exit_reason', 'Manual close').strip()

    closed = None
    remaining = []
    for p in positions:
        if p.get('id') == position_id:
            closed = p
        else:
            remaining.append(p)

    if closed and exit_price > 0:
        _save_positions(remaining)
        # Append to Trade Journal
        _journal_append_closed(closed, exit_price, exit_reason)

    return redirect(url_for('position_tracker.position_tracker_view'))


@position_tracker_bp.route("/position-tracker/delete/<position_id>", methods=["POST"])
def position_delete(position_id):
    """Delete a position without journaling (entry mistake)."""
    positions = [p for p in _load_positions() if p.get('id') != position_id]
    _save_positions(positions)
    return redirect(url_for('position_tracker.position_tracker_view'))


def _journal_append_closed(pos, exit_price, exit_reason):
    """Write a closed trade to the Trade Journal file."""
    journal_path = os.path.join(
        os.path.dirname(DATA_DIR), 'trade_journal', 'trades.json'
    )
    os.makedirs(os.path.dirname(journal_path), exist_ok=True)

    trades = []
    if os.path.exists(journal_path):
        try:
            with open(journal_path) as f:
                trades = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    entry  = float(pos['entry_price'])
    stop   = float(pos['stop_loss'])
    qty    = int(pos['qty'])
    risk   = entry - stop
    pnl    = (exit_price - entry) * qty
    r_mult = (exit_price - entry) / risk if risk != 0 else 0

    try:
        ed = datetime.strptime(str(pos['entry_date']), "%Y-%m-%d").date()
        days_held = (date.today() - ed).days
    except (ValueError, TypeError):
        days_held = 0

    trades.append({
        "id":           pos.get('id', ''),
        "symbol":       pos['symbol'],
        "entry_date":   str(pos['entry_date']),
        "exit_date":    date.today().strftime("%Y-%m-%d"),
        "entry_price":  entry,
        "exit_price":   round(exit_price, 2),
        "stop_loss":    stop,
        "qty":          qty,
        "pnl":          round(pnl, 2),
        "pnl_pct":      round((exit_price - entry) / entry * 100, 2),
        "r_multiple":   round(r_mult, 2),
        "days_held":    days_held,
        "exit_reason":  exit_reason,
        "entry_rs_pct": pos.get('entry_rs_pct', 0),
        "notes":        pos.get('notes', ''),
        "closed_at":    datetime.now().strftime("%d-%b-%Y %H:%M"),
    })

    with open(journal_path, 'w') as f:
        json.dump(trades, f)
