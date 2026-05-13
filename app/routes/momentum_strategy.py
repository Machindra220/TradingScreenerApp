from datetime import datetime, timedelta
from app.routes.performers import get_top_performers
from app.models import db, MomentumPortfolio, MomentumTrade
from app.utils import get_current_price
from flask import Blueprint, render_template, flash, request

momentum_bp = Blueprint('momentum', __name__)

def get_next_schedule_date():
    today = datetime.today()
    # Move to next month
    year = today.year
    month = today.month + 1 if today.month < 12 else 1
    if month == 1:
        year += 1

    # Start from the 1st of next month
    for day in range(1, 8):  # Max 7 iterations
        d = datetime(year, month, day)
        if d.weekday() < 5:  # 0–4 = Mon–Fri
            return d.date()


def run_momentum_strategy():
    today = datetime.today()
    next_schedule_date = get_next_schedule_date()

    if today.date() != next_schedule_date:
        flash("⚠️ Strategy is designed to run only on the first working day of each month.", "warning")

    # Step 1: Get top 20 performers from nifty_500
    top_20 = get_top_performers("data/nifty_500.csv", top_n=20)
    top_10 = [s for s in top_20 if s["current_price"] <= 5000][:10]

    # Step 2: Load current portfolio
    current = MomentumPortfolio.query.filter_by(holding_status='active').all()
    current_symbols = {t.symbol for t in current}

    # Step 3: Identify underperformers
    top_20_symbols = {s["symbol"] for s in top_20}
    to_remove = [t for t in current if t.symbol not in top_20_symbols]

    # Step 4: Identify replacements
    to_add = []
    for stock in top_10:
        if stock["symbol"] not in current_symbols:
            to_add.append(stock)
        if len(to_add) == len(to_remove):
            break

    # Step 5: Execute trades
    for sell in to_remove:
        current_price = float(get_current_price(sell.symbol))
        quantity = int(5000 / float(sell.buy_price))
        profit_loss_value = round((current_price - float(sell.buy_price)) * quantity, 2)
        profit_loss_pct = round(((current_price - float(sell.buy_price)) / float(sell.buy_price)) * 100, 2)

        # Record SELL trade linked to portfolio entry
        db.session.add(MomentumTrade(
            symbol=sell.symbol,
            action='SELL',
            price=current_price,
            quantity=quantity,
            trade_date=today.date(),
            profit_loss_value=profit_loss_value,
            profit_loss_pct=profit_loss_pct,
            notes='Removed from top 20',
            portfolio_id=sell.id   # link SELL trade to portfolio entry
        ))
        sell.holding_status = 'removed'

    for buy in to_add:
        quantity = int(5000 / float(buy["current_price"]))

        # Create portfolio entry first
        portfolio_entry = MomentumPortfolio(
            symbol=buy["symbol"],
            buy_price=float(buy["current_price"]),
            buy_date=today.date(),
            source_rank=buy["rank"]
        )
        db.session.add(portfolio_entry)
        db.session.flush()  # ensures portfolio_entry.id is available

        # Record BUY trade linked to portfolio entry
        db.session.add(MomentumTrade(
            symbol=buy["symbol"],
            action='BUY',
            price=float(buy["current_price"]),
            quantity=quantity,
            trade_date=today.date(),
            notes='Added to portfolio',
            portfolio_id=portfolio_entry.id
        ))

    db.session.commit()
    return to_remove, to_add, today.date(), next_schedule_date


@momentum_bp.route('/momentum/rebalance', methods=["GET"])
def momentum_rebalance_view():
    return render_template("momentum_result.html",
                           removed=[],
                           added=[],
                           run_date=None,
                           next_schedule_date=get_next_schedule_date(),
                           summary_message=None)


@momentum_bp.route('/momentum/rebalance', methods=["POST"])
def momentum_rebalance_process():
    removed, added, run_date, next_schedule_date = run_momentum_strategy()

    summary_message = f"✅ Rebalance completed on {run_date.strftime('%d %b %Y')}"

    return render_template("momentum_result.html",
                           removed=removed,
                           added=added,
                           run_date=run_date,
                           next_schedule_date=next_schedule_date,
                           summary_message=summary_message)


@momentum_bp.route('/momentum/history')
def momentum_history():
    symbol_filter = request.args.get("symbol", "").upper().strip()
    month_filter = request.args.get("month", "").strip()

    query = MomentumTrade.query.filter_by(action='BUY')
    removed_trades = MomentumTrade.query.filter_by(action='SELL').order_by(MomentumTrade.trade_date.desc()).all()

    if symbol_filter:
        query = query.filter(MomentumTrade.symbol.ilike(f"%{symbol_filter}%"))

    if month_filter:
        try:
            year, month = map(int, month_filter.split("-"))
            start = datetime(year, month, 1)
            end = start + timedelta(days=32)
            end = end.replace(day=1)
            query = query.filter(MomentumTrade.trade_date >= start, MomentumTrade.trade_date < end)
        except:
            flash("Invalid month format. Use YYYY-MM.", "error")

    trades = query.order_by(MomentumTrade.trade_date.desc()).all()
    return render_template("momentum_history.html",
                           trades=trades,
                           removed_trades=removed_trades,
                           symbol_filter=symbol_filter,
                           month_filter=month_filter)
