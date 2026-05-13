from flask import Blueprint, render_template, request
from flask_login import login_required, current_user
from app.models import Trade
from collections import defaultdict
from datetime import datetime, timedelta

calendar_bp = Blueprint('calendar', __name__)

@calendar_bp.route('/calendar')
@login_required
def calendar_view():
    today = datetime.today()
    # Get month/year from args or default to today
    month = int(request.args.get("month", today.month))
    year = int(request.args.get("year", today.year))

    # Fetch all closed trades for the current user
    trades = Trade.query.filter_by(user_id=current_user.id, status='Closed').all()
    
    # Structure: { "YYYY-MM-DD": {"pnl": 0.0, "trades": [{"name": "AAPL", "pnl": 50.0}, ...]} }
    daily_data = defaultdict(lambda: {"total_pnl": 0.0, "trades": []})

    for trade in trades:
        # Check if trade has an exit date and matches the viewed month/year
        if trade.exit_date and trade.exit_date.month == month and trade.exit_date.year == year:
            date_str = trade.exit_date.strftime("%Y-%m-%d")
            
            # Use the calculated pnl property from your model
            trade_pnl = trade.realized_pnl if hasattr(trade, 'realized_pnl') else 0.0
            
            daily_data[date_str]["total_pnl"] += trade_pnl
            daily_data[date_str]["trades"].append({
                "stock_name": trade.stock_name,
                "pnl": trade_pnl,
                "id": trade.id
            })

    return render_template("calendar.html",
                           month=month,
                           year=year,
                           today=today,
                           daily_pnl=daily_data,
                           datetime=datetime,
                           timedelta=timedelta)