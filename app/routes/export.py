from flask import Blueprint, request, send_file
from flask_login import login_required, current_user
from io import BytesIO
import pandas as pd
from datetime import date, timedelta, datetime
from app.models import Trade
from app.routes.stats_helpers import (
    calculate_realized_pnl, is_win, holding_days,
    get_equity_curve, get_stock_stats
)


from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

export_bp = Blueprint('export', __name__)

# 🔧 Time-based trade filter
def get_filtered_trades(user_id, filter_range):
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

    trades = Trade.query.filter_by(user_id=user_id).all()
    if start_date:
        trades = [t for t in trades if t.exit_date and t.exit_date >= start_date]
    if filter_range == 'last_year':
        trades = [t for t in trades if t.exit_date and start_date <= t.exit_date <= end_date]
    return trades

# 📤 Export trades + metrics
@export_bp.route('/export')
@login_required
def export_history():
    filter_range = request.args.get('range', 'all_time')
    output_format = request.args.get('format', 'excel')
    trades = get_filtered_trades(current_user.id, filter_range)
    last_computed = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 📊 Compute metrics
    closed_trades = [t for t in trades if t.status == "Closed"]
    win_trades = [t for t in closed_trades if is_win(t)]
    loss_trades = [t for t in closed_trades if not is_win(t)]

    realized_pnl = sum(calculate_realized_pnl(t) for t in closed_trades)
    win_rate = round((len(win_trades) / len(closed_trades)) * 100, 2) if closed_trades else 0
    expectancy = round(realized_pnl / len(closed_trades), 2) if closed_trades else 0
    gross_profit = sum(calculate_realized_pnl(t) for t in win_trades)
    gross_loss = sum(calculate_realized_pnl(t) for t in loss_trades)
    profit_factor = round(gross_profit / abs(gross_loss), 2) if gross_loss else 0
    equity_curve, max_drawdown = get_equity_curve(closed_trades)

    if output_format == 'pdf':
        output = BytesIO()
        doc = SimpleDocTemplate(output, pagesize=A4)
        elements = []
        styles = getSampleStyleSheet()

        elements.append(Paragraph("Trade History Report", styles['Title']))
        elements.append(Spacer(1, 12))
        elements.append(Paragraph(f"📅 Stats last computed: {last_computed}", styles['Normal']))
        elements.append(Spacer(1, 12))


        # 📈 Metrics summary
        summary = [
            ['Metric', 'Value'],
            ['Realized P&L', f"₹{realized_pnl:.2f}"],
            ['Win Rate', f"{win_rate}%"],
            ['Expectancy', f"₹{expectancy:.2f}"],
            ['Profit Factor', f"{profit_factor}"],
            ['Max Drawdown', f"₹{max_drawdown:.2f}"]
        ]
        summary_table = Table(summary)
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#343a40')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        elements.append(summary_table)
        elements.append(Spacer(1, 20))

        # 📋 Trade table
        table_data = [['Stock', 'Entry Date', 'Exit Date', 'P&L', 'Status']]
        for t in trades:
            table_data.append([
                t.stock_name,
                t.entry_date.strftime('%Y-%m-%d') if t.entry_date else '',
                t.exit_date.strftime('%Y-%m-%d') if t.exit_date else '',
                f"₹{calculate_realized_pnl(t):.2f}",
                t.status
            ])
        trade_table = Table(table_data, repeatRows=1)
        trade_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#007bff')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey])
        ]))
        elements.append(trade_table)
        doc.build(elements)
        output.seek(0)
        return send_file(output, download_name='trades.pdf', as_attachment=True, mimetype='application/pdf')

    else:
        output = BytesIO()
        data = []
        for t in trades:
            data.append({
                'Stock': t.stock_name,
                'Entry Date': t.entry_date,
                'Exit Date': t.exit_date,
                'P&L': calculate_realized_pnl(t),
                'Status': t.status
            })
        df = pd.DataFrame(data)
        metrics_df = pd.DataFrame({
            'Metric': ['Realized P&L', 'Win Rate', 'Expectancy', 'Profit Factor', 'Max Drawdown'],
            'Value': [f"₹{realized_pnl:.2f}", f"{win_rate}%", f"₹{expectancy:.2f}", f"{profit_factor}", f"₹{max_drawdown:.2f}"]
        })
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Trades')
            metrics_df.to_excel(writer, index=False, sheet_name='Summary')
        output.seek(0)
        return send_file(output, download_name='trades.xlsx', as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

# 📊 Export metrics only
@export_bp.route('/export/stats')
@login_required
def export_stats_only():
    filter_range = request.args.get('range', 'all_time')
    output_format = request.args.get('format', 'excel')
    trades = get_filtered_trades(current_user.id, filter_range)
    closed_trades = [t for t in trades if t.status == "Closed"]
    win_trades = [t for t in closed_trades if is_win(t)]
    loss_trades = [t for t in closed_trades if not is_win(t)]
    last_computed = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    realized_pnl = sum(calculate_realized_pnl(t) for t in closed_trades)
    win_rate = round((len(win_trades) / len(closed_trades)) * 100, 2) if closed_trades else 0
    expectancy = round(realized_pnl / len(closed_trades), 2) if closed_trades else 0
    gross_profit = sum(calculate_realized_pnl(t) for t in win_trades)
    gross_loss = sum(calculate_realized_pnl(t) for t in loss_trades)
    profit_factor = round(gross_profit / abs(gross_loss), 2) if gross_loss else 0
    equity_curve, max_drawdown = get_equity_curve(closed_trades)

    output = BytesIO()
    if output_format == 'pdf':
        doc = SimpleDocTemplate(output, pagesize=A4)
        elements = []
        styles = getSampleStyleSheet()
        elements.append(Paragraph("Performance Metrics", styles['Title']))
        elements.append(Spacer(1, 12))
        elements.append(Paragraph(f"📅 Stats last computed: {last_computed}", styles['Normal']))
        elements.append(Spacer(1, 12))


        summary = [
            ['Metric', 'Value'],
            ['Realized P&L', f"₹{realized_pnl:.2f}"],
            ['Win Rate', f"{win_rate}%"],
            ['Expectancy', f"₹{expectancy:.2f}"],
            ['Profit Factor', f"{profit_factor}"],
            ['Max Drawdown', f"₹{max_drawdown:.2f}"]
        ]
        table = Table(summary)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#343a40')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        elements.append(table)
        doc.build(elements)
        output.seek(0)
        return send_file(output, download_name='stats_summary.pdf', as_attachment=True, mimetype='application/pdf')

    else:
        metrics_df = pd.DataFrame({
            'Metric': ['Realized P&L', 'Win Rate', 'Expectancy', 'Profit Factor', 'Max Drawdown'],
            'Value': [f"₹{realized_pnl:.2f}", f"{win_rate}%", f"₹{expectancy:.2f}", f"{profit_factor}", f"₹{max_drawdown:.2f}"]
        })
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            metrics_df.to_excel(writer, index=False, sheet_name='Summary')
        output.seek(0)
        return send_file(output, download_name='stats_summary.xlsx', as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
