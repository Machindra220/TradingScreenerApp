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
    month = int(request.args.get("month", today.month))
    year = int(request.args.get("year", today.year))

    trades = Trade.query.filter_by(user_id=current_user.id, status='closed').all()
    daily_pnl = defaultdict(lambda: {"pnl": 0.0, "stocks": []})

    for trade in trades:
        if trade.exit_date and trade.exit_date.month == month and trade.exit_date.year == year:
            date_str = trade.exit_date.strftime("%Y-%m-%d")
            daily_pnl[date_str]["pnl"] += trade.pnl
            daily_pnl[date_str]["stocks"].append(trade.stock_name)

    return render_template("calendar.html",
                           month=month,
                           year=year,
                           today=today,
                           daily_pnl=daily_pnl,
                           datetime=datetime,
                           timedelta=timedelta)
