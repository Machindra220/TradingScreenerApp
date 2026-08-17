"""
trade_journal.py

Trade Journal — logs all closed trades with realized P&L, win rate,
R-multiples, average winner/loser, and streak analysis.

A trade entry is created automatically when you close a position from
the Position Tracker, or manually via the Add Closed Trade form.
"""

import os
import json
import pandas as pd
from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, send_file

trade_journal_bp = Blueprint("trade_journal", __name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DATA_DIR      = os.path.join(_PROJECT_ROOT, 'uploads', 'trade_journal')
TRADES_JSON   = os.path.join(DATA_DIR, 'trades.json')

os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _load_trades():
    if os.path.exists(TRADES_JSON):
        try:
            with open(TRADES_JSON) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_trades(trades):
    with open(TRADES_JSON, 'w') as f:
        json.dump(trades, f, default=str)


def _normalize_trade(t):
    t.setdefault('id',           '')
    t.setdefault('symbol',       '')
    t.setdefault('entry_date',   '')
    t.setdefault('exit_date',    '')
    t.setdefault('entry_price',  0.0)
    t.setdefault('exit_price',   0.0)
    t.setdefault('stop_loss',    0.0)
    t.setdefault('qty',          0)
    t.setdefault('pnl',          0.0)
    t.setdefault('pnl_pct',      0.0)
    t.setdefault('r_multiple',   0.0)
    t.setdefault('days_held',    0)
    t.setdefault('exit_reason',  '')
    t.setdefault('entry_rs_pct', 0)
    t.setdefault('notes',        '')
    t.setdefault('closed_at',    '')
    return t


# ---------------------------------------------------------------------------
# Statistics engine
# ---------------------------------------------------------------------------

def _compute_stats(trades):
    """Compute all performance statistics from a list of trades."""
    if not trades:
        return {}

    df = pd.DataFrame(trades)
    df['pnl']        = pd.to_numeric(df['pnl'],        errors='coerce').fillna(0)
    df['r_multiple'] = pd.to_numeric(df['r_multiple'], errors='coerce').fillna(0)
    df['pnl_pct']    = pd.to_numeric(df['pnl_pct'],    errors='coerce').fillna(0)
    df['days_held']  = pd.to_numeric(df['days_held'],  errors='coerce').fillna(0)

    winners = df[df['pnl'] > 0]
    losers  = df[df['pnl'] <= 0]

    win_rate   = len(winners) / len(df) * 100 if len(df) > 0 else 0
    avg_winner = winners['pnl'].mean() if len(winners) > 0 else 0
    avg_loser  = losers['pnl'].mean()  if len(losers)  > 0 else 0
    avg_r_win  = winners['r_multiple'].mean() if len(winners) > 0 else 0
    avg_r_loss = losers['r_multiple'].mean()  if len(losers)  > 0 else 0

    # Expectancy = (win_rate × avg_winner_R) + (loss_rate × avg_loser_R)
    loss_rate  = 1 - win_rate / 100
    expectancy = (win_rate / 100 * avg_r_win) + (loss_rate * avg_r_loss)

    # Current streak
    streak_count, streak_type = 0, None
    for _, row in df.sort_values('exit_date', ascending=False).iterrows():
        kind = 'W' if row['pnl'] > 0 else 'L'
        if streak_type is None:
            streak_type = kind
        if kind == streak_type:
            streak_count += 1
        else:
            break

    # Monthly breakdown
    df['exit_month'] = pd.to_datetime(df['exit_date'], errors='coerce').dt.to_period('M').astype(str)
    monthly = df.groupby('exit_month').agg(
        trades=('pnl', 'count'),
        pnl=('pnl', 'sum'),
        win_rate=('pnl', lambda x: (x > 0).mean() * 100)
    ).reset_index().sort_values('exit_month', ascending=False).to_dict('records')

    # Best/worst trades
    best  = df.loc[df['pnl'].idxmax()].to_dict()  if len(df) > 0 else {}
    worst = df.loc[df['pnl'].idxmin()].to_dict()  if len(df) > 0 else {}

    return {
        "total_trades":     len(df),
        "winners":          len(winners),
        "losers":           len(losers),
        "win_rate":         round(win_rate, 1),
        "total_pnl":        round(df['pnl'].sum(), 2),
        "avg_winner":       round(avg_winner, 2),
        "avg_loser":        round(avg_loser, 2),
        "avg_r_winner":     round(avg_r_win, 2),
        "avg_r_loser":      round(avg_r_loss, 2),
        "best_r":           round(df['r_multiple'].max(), 2),
        "worst_r":          round(df['r_multiple'].min(), 2),
        "avg_days_held":    round(df['days_held'].mean(), 1),
        "expectancy":       round(expectancy, 2),
        "streak_count":     streak_count,
        "streak_type":      streak_type,
        "monthly":          monthly,
        "best_trade":       best,
        "worst_trade":      worst,
        "profit_factor":    round(abs(winners['pnl'].sum() / losers['pnl'].sum()), 2)
                            if losers['pnl'].sum() != 0 else 0,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@trade_journal_bp.route("/trade-journal", methods=["GET"])
def trade_journal_view():
    trades = [_normalize_trade(t) for t in _load_trades()]

    # Apply filters from query string
    sym_filter  = request.args.get('symbol', '').upper().strip()
    month_filter = request.args.get('month', '').strip()
    outcome     = request.args.get('outcome', '').strip()  # 'win' / 'loss'

    filtered = trades
    if sym_filter:
        filtered = [t for t in filtered if sym_filter in t['symbol'].upper()]
    if month_filter:
        filtered = [t for t in filtered if str(t['exit_date']).startswith(month_filter)]
    if outcome == 'win':
        filtered = [t for t in filtered if float(t['pnl']) > 0]
    elif outcome == 'loss':
        filtered = [t for t in filtered if float(t['pnl']) <= 0]

    filtered.sort(key=lambda x: str(x['exit_date']), reverse=True)

    stats = _compute_stats(trades)   # stats always on full dataset

    return render_template(
        "trade_journal.html",
        trades=filtered,
        all_trades=trades,
        stats=stats,
        sym_filter=sym_filter,
        month_filter=month_filter,
        outcome=outcome,
    )


@trade_journal_bp.route("/trade-journal/add", methods=["POST"])
def trade_journal_add():
    """Manually add a closed trade."""
    import uuid as _uuid
    try:
        entry  = float(request.form['entry_price'])
        exit_p = float(request.form['exit_price'])
        stop   = float(request.form['stop_loss'])
        qty    = int(request.form['qty'])
        symbol = request.form['symbol'].strip().upper().replace('.NS', '')
        risk   = entry - stop
        pnl    = (exit_p - entry) * qty
        r_mult = (exit_p - entry) / risk if risk != 0 else 0

        entry_date = request.form.get('entry_date', '')
        exit_date  = request.form.get('exit_date', date.today().strftime("%Y-%m-%d"))
        try:
            ed = datetime.strptime(entry_date, "%Y-%m-%d").date()
            xd = datetime.strptime(exit_date, "%Y-%m-%d").date()
            days_held = (xd - ed).days
        except (ValueError, TypeError):
            days_held = 0

        trades = _load_trades()
        trades.append({
            "id":           _uuid.uuid4().hex[:8],
            "symbol":       symbol,
            "entry_date":   entry_date,
            "exit_date":    exit_date,
            "entry_price":  entry,
            "exit_price":   round(exit_p, 2),
            "stop_loss":    stop,
            "qty":          qty,
            "pnl":          round(pnl, 2),
            "pnl_pct":      round((exit_p - entry) / entry * 100, 2),
            "r_multiple":   round(r_mult, 2),
            "days_held":    days_held,
            "exit_reason":  request.form.get('exit_reason', '').strip(),
            "entry_rs_pct": int(request.form.get('entry_rs_pct', 0) or 0),
            "notes":        request.form.get('notes', '').strip(),
            "closed_at":    datetime.now().strftime("%d-%b-%Y %H:%M"),
        })
        _save_trades(trades)
    except (ValueError, KeyError) as e:
        pass  # silently skip bad input
    return redirect(url_for('trade_journal.trade_journal_view'))


@trade_journal_bp.route("/trade-journal/delete/<trade_id>", methods=["POST"])
def trade_journal_delete(trade_id):
    trades = [t for t in _load_trades() if t.get('id') != trade_id]
    _save_trades(trades)
    return redirect(url_for('trade_journal.trade_journal_view'))


@trade_journal_bp.route("/trade-journal/export")
def trade_journal_export():
    trades = _load_trades()
    if trades:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_path = os.path.join(DATA_DIR, 'temp_journal_export.csv')
        pd.DataFrame(trades).to_csv(temp_path, index=False)
        return send_file(temp_path, as_attachment=True,
                         download_name=f"TradeJournal_{timestamp}.csv")
    return "No trades to export.", 404
