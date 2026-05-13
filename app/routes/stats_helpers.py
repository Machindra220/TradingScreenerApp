# stats_helpers.py

def calculate_realized_pnl(trade):
    return sum(x.quantity * x.price for x in trade.exits) - sum(e.quantity * e.price for e in trade.entries)

def is_win(trade):
    return calculate_realized_pnl(trade) > 0

def holding_days(trade):
    if trade.entry_date and trade.exit_date:
        return (trade.exit_date - trade.entry_date).days
    return 0

def get_equity_curve(trades):
    equity = 0
    peak = 0
    max_drawdown = 0
    curve = []
    for t in sorted(trades, key=lambda x: x.exit_date):
        pnl = calculate_realized_pnl(t)
        equity += pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        curve.append({'date': t.exit_date.strftime('%d-%m-%Y'), 'value': equity})
    return curve, max_drawdown

def get_stock_stats(trades, limit=20):
    stats = {}
    for t in trades:
        pnl = calculate_realized_pnl(t)
        stock = t.stock_name.upper()
        if stock not in stats:
            stats[stock] = {'count': 0, 'pnl': 0}
        stats[stock]['count'] += 1
        stats[stock]['pnl'] += pnl

    # Sort by trade count (most traded)
    most_traded = dict(sorted(stats.items(), key=lambda x: x[1]['count'], reverse=True)[:limit])

    # Sort by total P&L (most profitable)
    most_profitable = dict(sorted(stats.items(), key=lambda x: x[1]['pnl'], reverse=True)[:limit])

    return most_traded, most_profitable

def max_drawdown_trade(trade):
    """Approximate max drawdown % for a trade using exits."""
    if not trade.entries or not trade.exits:
        return 0
    entry_price = trade.entries[0].price
    min_exit_price = min(x.price for x in trade.exits)
    drawdown = (min_exit_price - entry_price) / entry_price * 100
    return round(drawdown, 2)



def r_multiple(trade, risk_per_share=None):
    """
    Calculate R multiple for a trade.
    risk_per_share: difference between entry and stop-loss.
    If not provided, assume 4% of entry price as risk.
    """
    if not trade.entries or not trade.exits:
        return 0
    entry_price = trade.entries[0].price
    exit_price = trade.exits[-1].price   # last exit
    risk = risk_per_share or (0.04 * entry_price)
    reward = exit_price - entry_price
    return round(reward / risk, 2) if risk else 0


