from flask import Blueprint, render_template, request, redirect, url_for
from flask_login import login_required, current_user
from datetime import date, datetime, timedelta
from collections import defaultdict
from app.models import Trade, TradeEntry, TradeExit
from app import cache

from app.routes.stats_helpers import (
    calculate_realized_pnl, is_win, holding_days,
    get_equity_curve, get_stock_stats,
    max_drawdown_trade, r_multiple
)



stats_bp = Blueprint('stats', __name__, template_folder='../templates')
#stats Route
@stats_bp.route('/')
@login_required
def stats_dashboard():
    filter_range = request.args.get('range', 'all_time')
    cache_key = f"stats:{current_user.id}:{filter_range}"
    cached_data = cache.get(cache_key)
    if cached_data:
        return render_template('stats_dashboard.html', **cached_data)

    today = date.today()
    if filter_range == 'last_7_days':
        start_date = today - timedelta(days=7)
    elif filter_range == 'last_30_days':
        start_date = today - timedelta(days=30)
    elif filter_range == 'last_90_days':
        start_date = today - timedelta(days=90)
    elif filter_range == 'ytd':
        start_date = date(today.year, 1, 1)
    elif filter_range == 'last_year':
        start_date = date(today.year - 1, 1, 1)
        end_date = date(today.year - 1, 12, 31)
    else:
        start_date = None

    trades = Trade.query.filter_by(user_id=current_user.id).all()
    if start_date:
        trades = [
            t for t in trades
            if (
                (t.status == "Closed" and t.exit_date and t.exit_date >= start_date) or
                (t.status == "Open" and t.entry_date and t.entry_date >= start_date)
            )
        ]

    if filter_range == 'last_year':
        trades = [
            t for t in trades
            if (
                (t.status == "Closed" and t.exit_date and start_date <= t.exit_date <= end_date) or
                (t.status == "Open" and t.entry_date and start_date <= t.entry_date <= end_date)
            )
        ]

    closed_trades = [t for t in trades if t.status == "Closed"]
    open_trades = [t for t in trades if t.status == "Open"]
    win_trades = [t for t in closed_trades if is_win(t)]
    loss_trades = [t for t in closed_trades if not is_win(t)]

    realized_pnl = sum(calculate_realized_pnl(t) for t in closed_trades)
    win_rate = round((len(win_trades) / len(closed_trades)) * 100, 2) if closed_trades else 0
    expectancy = round(realized_pnl / len(closed_trades), 2) if closed_trades else 0
    gross_profit = sum(calculate_realized_pnl(t) for t in win_trades)
    gross_loss = sum(calculate_realized_pnl(t) for t in loss_trades)
    profit_factor = round(gross_profit / abs(gross_loss), 2) if gross_loss else 0
    avg_win_hold = round(sum(holding_days(t) for t in win_trades) / len(win_trades), 1) if win_trades else 0
    avg_loss_hold = round(sum(holding_days(t) for t in loss_trades) / len(loss_trades), 1) if loss_trades else 0
    avg_win = round(sum(calculate_realized_pnl(t) for t in win_trades) / len(win_trades), 2) if win_trades else 0
    avg_loss = round(sum(calculate_realized_pnl(t) for t in loss_trades) / len(loss_trades), 2) if loss_trades else 0
# New drawdown metrics
    avg_drawdown = round(sum(max_drawdown_trade(t) for t in closed_trades) / len(closed_trades), 2) if closed_trades else 0
    avg_r_multiple = round(sum(r_multiple(t) for t in closed_trades) / len(closed_trades), 2) if closed_trades else 0


    win_streak = loss_streak = 0
    current_streak = 0
    last_was_win = None
    for t in closed_trades:
        result = is_win(t)
        if result == last_was_win:
            current_streak += 1
        else:
            current_streak = 1
        last_was_win = result
        if result:
            win_streak = max(win_streak, current_streak)
        else:
            loss_streak = max(loss_streak, current_streak)

    total_entries = [e for t in trades for e in t.entries]
    avg_daily_vol = round(sum(e.quantity for e in total_entries) / len(trades), 1) if trades else 0
    avg_size = round(sum(e.quantity for e in total_entries) / len(total_entries), 1) if total_entries else 0
    equity_curve, max_drawdown = get_equity_curve(closed_trades)
    most_traded_stats, most_profitable_stats = get_stock_stats(closed_trades, limit=20) # Most Traded and Most Profitable stocks

    # 📊 Prepare Profit/Loss Bar Chart Data
    daily_pnl = defaultdict(float)

    for t in closed_trades:
        if t.exit_date and calculate_realized_pnl(t) != 0:
            date_label = t.exit_date.strftime("%d-%b-%Y")  # e.g., "Apr-05"= %Y-%b-%d
            daily_pnl[date_label] += calculate_realized_pnl(t)

    # Convert to sorted list of bars
    sorted_days = sorted(daily_pnl.items(), key=lambda x: datetime.strptime(x[0], "%d-%b-%Y"))
    trade_bars = [{"date": label, "pnl": round(pnl, 2)} for label, pnl in sorted_days[-15:]]
    
    # 📊 Weekly Profit/Loss Bar Chart Data
    weekly_pnl = defaultdict(float)

    for t in closed_trades:
        if t.exit_date and calculate_realized_pnl(t) != 0:
            week_start = t.exit_date - timedelta(days=t.exit_date.weekday())  # Monday of the week
            week_label = week_start.strftime("Week of %d %b %Y")  # e.g., "Week of Oct 06"
            weekly_pnl[week_label] += calculate_realized_pnl(t)

    # Sort by week and keep last 10
    sorted_weeks = sorted(weekly_pnl.items(), key=lambda x: datetime.strptime(x[0], "Week of %d %b %Y"))
    weekly_bars = [{"week": label, "pnl": round(pnl, 2)} for label, pnl in sorted_weeks[-12:]]

    # 📊 Monthly Profit/Loss Bar Chart Data
    monthly_pnl = defaultdict(float)

    for t in closed_trades:
        if t.exit_date and calculate_realized_pnl(t) != 0:
            month_label = t.exit_date.strftime("%Y-%m")  # e.g., "2025-10"
            monthly_pnl[month_label] += calculate_realized_pnl(t)

    # Sort by month and keep last 12
    sorted_months = sorted(monthly_pnl.items(), key=lambda x: datetime.strptime(x[0], "%Y-%m"))
    monthly_bars = [{"month": datetime.strptime(label, "%Y-%m").strftime("%b %Y"), "pnl": round(pnl, 2)} for label, pnl in sorted_months[-12:]]


    context = {
        'realized_pnl': realized_pnl,
        'open_trades': len(open_trades),
        'closed_trades': len(closed_trades),
        'win_rate': win_rate,
        'expectancy': expectancy,
        'profit_factor': profit_factor,
        'avg_win_hold': avg_win_hold,
        'avg_loss_hold': avg_loss_hold,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'avg_drawdown': avg_drawdown, 
        'avg_r_multiple': avg_r_multiple,
        'win_streak': win_streak,
        'loss_streak': loss_streak,
        'avg_daily_vol': avg_daily_vol,
        'avg_size': avg_size,
        'max_drawdown': max_drawdown,
        'stock_stats': most_traded_stats,
        'most_profitable_stats': most_profitable_stats,
        'equity_curve': equity_curve,
        'trade_bars': trade_bars,  # 👈 Add this to template context for daily bars
        'weekly_bars': weekly_bars, # context for weekly bars
        'monthly_bars': monthly_bars,  # context for monthly bars
        'last_computed': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    cache.set(cache_key, context, timeout=300)  # Cache for 5 minutes
    return render_template('stats_dashboard.html', **context)


@stats_bp.route('/refresh')
@login_required
def refresh_stats():
    filter_range = request.args.get('range', 'all_time')
    cache_key = f"stats:{current_user.id}:{filter_range}"
    cache.delete(cache_key)
    return redirect(url_for('stats.stats_dashboard', range=filter_range))
