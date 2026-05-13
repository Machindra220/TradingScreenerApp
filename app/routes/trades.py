from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user
from app.models import Trade, TradeEntry, TradeExit
from app.extensions import db
from datetime import date, datetime, timedelta
import pandas as pd
from io import BytesIO
from flask_wtf.csrf import validate_csrf, CSRFError  # ✅ CSRF validation
from app.routes.stats_helpers import calculate_realized_pnl, is_win

trades_bp = Blueprint('trades', __name__)

# Dashboard Route
@trades_bp.route('/dashboard')
@login_required
def dashboard():
    strategy_filter = request.args.get('strategy_tag')
    query = Trade.query.filter_by(user_id=current_user.id)

    if strategy_filter:
        query = query.filter(Trade.strategy_tag == strategy_filter)

    trades = query.all()

    trade_data = []
    total_invested_open = 0
    open_trade_count = 0  # ✅ Open trades count for starting no data app

    for trade in trades:
        if trade.status != "Open" or not trade.entries:
            continue

        total_invested = sum(float(e.invested_amount) for e in trade.entries)
        total_quantity = sum(e.quantity for e in trade.entries)
        exited_quantity = sum(x.quantity for x in trade.exits)
        remaining_quantity = total_quantity - exited_quantity
        open_trade_count = len(trade_data)

        if total_quantity == 0 or remaining_quantity == 0:
            continue

        avg_entry_price = round(total_invested / total_quantity, 2)
        invested_remaining = round((remaining_quantity / total_quantity) * total_invested, 2)
        realized_pnl = sum((exit.price - avg_entry_price) * exit.quantity for exit in trade.exits)
        realized_profit = sum((x.price - avg_entry_price) * x.quantity for x in trade.exits)

        first_entry = trade.entries[0]
        entry_date = first_entry.date.strftime('%d/%m/%y')
        entry_notes = [e.note for e in trade.entries if e.note]
        exit_notes = [x.note for x in trade.exits if x.note]
        combined_notes = entry_notes + exit_notes
        note = " | ".join(combined_notes) if combined_notes else "—"

        trade_data.append({
            'id': trade.id,
            'stock_name': trade.stock_name,
            'status': trade.status,
            'total_invested': round(invested_remaining, 2),
            'quantity': remaining_quantity,
            'exited_quantity': exited_quantity,
            'realized_pnl': round(realized_pnl, 2),
            'realized_profit': round(realized_profit, 2),
            'entry_date': entry_date,
            'note': note,
            'avg_entry_price': avg_entry_price,
            'strategy_tag': trade.strategy_tag
        })

        total_invested_open += invested_remaining

    incomplete_trades = Trade.query.filter_by(user_id=current_user.id, status='Open') \
        .filter(~Trade.entries.any(), ~Trade.exits.any()).all()

    incomplete_trade_data = [
        {'id': t.id, 'stock_name': t.stock_name.upper()}
        for t in incomplete_trades
    ]

    return render_template(
        'dashboard.html',
        trades=trade_data,
        total_invested=total_invested_open,
        incomplete_trades=incomplete_trade_data,
        open_trade_count=open_trade_count
    )

# Add Trade
@trades_bp.route('/add', methods=['GET', 'POST'])
@login_required
def add_trade():
    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
            return redirect(url_for('trades.add_trade'))

        stock_name = request.form.get('stock_name', '').strip().upper()
        entry_note = request.form.get('entry_note', '').strip()
        strategy_tag = request.form.get('strategy_tag', '').strip()   # ✅ NEW

        if not stock_name:
            flash("Stock name is required.", "error")
            return redirect(url_for('trades.add_trade'))

        try:
            new_trade = Trade(stock_name=stock_name, entry_note=entry_note, user_id=current_user.id, strategy_tag=strategy_tag)
            db.session.add(new_trade)
            db.session.commit()
            flash(f"Trade for {stock_name} created successfully.", "success")
            return redirect(url_for('trades.view_trade', trade_id=new_trade.id))
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating trade: {str(e)}", "error")
            return redirect(url_for('trades.add_trade'))

    return render_template('add_trade.html')

# View Trade
@trades_bp.route('/trade/<int:trade_id>')
@login_required
def view_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    entries = TradeEntry.query.filter_by(trade_id=trade.id).order_by(TradeEntry.date).all()
    exits = TradeExit.query.filter_by(trade_id=trade.id).order_by(TradeExit.date).all()

    total_invested = sum(e.invested_amount for e in entries)
    total_exited = sum(x.exit_amount for x in exits)
    total_buy_qty = sum(e.quantity for e in entries)
    total_sell_qty = sum(x.quantity for x in exits)

    status = "Closed" if total_buy_qty == total_sell_qty else "Open"
    pnl = total_exited - total_invested if status == "Closed" else None

    if entries:
        start_date = entries[0].date
        end_date = exits[-1].date if status == "Closed" and exits else date.today()
        duration_days = (end_date - start_date).days
    else:
        duration_days = 0

    return render_template(
        'view_trade.html',
        trade=trade,
        entries=entries,
        exits=exits,
        status=status,
        pnl=round(pnl, 2) if pnl is not None else None,
        duration_days=duration_days,
        current_date=date.today().isoformat()
    )

# Add Entry
@trades_bp.route('/trade/<int:trade_id>/entry', methods=['POST'])
@login_required
def add_entry(trade_id):
    trade = Trade.query.get_or_404(trade_id)

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF

        quantity = int(request.form['quantity'])
        price = float(request.form['price'])
        date_str = request.form['date']
        date_obj = date.fromisoformat(date_str)
        note = request.form.get('note', '').strip()

        total_buy_qty = sum(e.quantity for e in trade.entries)
        total_sell_qty = sum(x.quantity for x in trade.exits)

        if total_buy_qty == total_sell_qty and total_buy_qty > 0:
            flash("Trade is closed. Start a new trade to buy again.", "error")
            return redirect(url_for('trades.view_trade', trade_id=trade.id))

        if total_sell_qty > total_buy_qty:
            flash("Sell quantity exceeds buy quantity. Trade is invalid.", "error")
            return redirect(url_for('trades.view_trade', trade_id=trade.id))

        entry = TradeEntry(
            quantity=quantity,
            price=price,
            date=date_obj,
            note=note,
            trade_id=trade.id
        )
        db.session.add(entry)

        if not trade.entry_date:
            trade.entry_date = date_obj

        db.session.commit()
        flash("Buy entry added successfully.", "success")

    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error adding buy entry: {str(e)}", "error")

    return redirect(url_for('trades.view_trade', trade_id=trade.id))

# Add Exit
@trades_bp.route('/trade/<int:trade_id>/exit', methods=['POST'])
@login_required
def add_exit(trade_id):
    trade = Trade.query.get_or_404(trade_id)

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF

        quantity = int(request.form['quantity'])
        price = float(request.form['price'])
        date_str = request.form['date']
        date_obj = date.fromisoformat(date_str)
        note = request.form.get('note', '').strip()

        total_buy_qty = sum(e.quantity for e in trade.entries)
        total_sell_qty = sum(x.quantity for x in trade.exits)
        available_qty = total_buy_qty - total_sell_qty

        if quantity > available_qty:
            flash(f"Cannot sell {quantity} units. Only {available_qty} available.", "error")
            return redirect(url_for('trades.view_trade', trade_id=trade.id))

        exit = TradeExit(
            quantity=quantity,
            price=price,
            date=date_obj,
            note=note,
            trade_id=trade.id
        )
        db.session.add(exit)

        updated_sell_qty = total_sell_qty + quantity

        if updated_sell_qty == total_buy_qty and total_buy_qty > 0:
            trade.status = "Closed"
            trade.exit_date = max([x.date for x in trade.exits] + [date_obj])  # Include new exit date

        db.session.commit()
        flash("Sell exit added successfully.", "success")

    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error adding sell exit: {str(e)}", "error")

    return redirect(url_for('trades.view_trade', trade_id=trade.id))
                    
# Edit Buy Entry
@trades_bp.route('/entry/<int:entry_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_entry(entry_id):
    entry = TradeEntry.query.get_or_404(entry_id)
    trade = entry.trade

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ CSRF check
            quantity = int(request.form['quantity'])
            price = float(request.form['price'])
            date_str = request.form['date']
            note = request.form.get('note', '').strip()
            date_obj = date.fromisoformat(date_str)

            entry.quantity = quantity
            entry.price = price
            entry.date = date_obj
            entry.note = note
            db.session.commit()
            flash("Buy entry updated successfully.", "success")
            return redirect(url_for('trades.view_trade', trade_id=trade.id))
        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating buy entry: {str(e)}", "error")

    return render_template('edit_entry.html', entry=entry, trade=trade)

# Delete Buy Entry
@trades_bp.route('/entry/<int:entry_id>/delete', methods=['POST'])
@login_required
def delete_entry(entry_id):
    entry = TradeEntry.query.get_or_404(entry_id)
    trade = entry.trade

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ CSRF check
        db.session.delete(entry)
        db.session.commit()
        flash("Buy entry deleted successfully.", "success")
    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting buy entry: {str(e)}", "error")

    return redirect(url_for('trades.view_trade', trade_id=trade.id))

# Edit Sell Exit
@trades_bp.route('/exit/<int:exit_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_exit(exit_id):
    exit = TradeExit.query.get_or_404(exit_id)
    trade = exit.trade

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ CSRF check
            quantity = int(request.form['quantity'])
            price = float(request.form['price'])
            date_str = request.form['date']
            note = request.form.get('note', '').strip()
            date_obj = date.fromisoformat(date_str)

            total_buy_qty = sum(e.quantity for e in trade.entries)
            other_exits_qty = sum(x.quantity for x in trade.exits if x.id != exit.id)
            available_qty = total_buy_qty - other_exits_qty

            if quantity > available_qty:
                flash(f"Cannot sell {quantity} units. Only {available_qty} available.", "error")
                return redirect(url_for('trades.edit_exit', exit_id=exit.id))

            exit.quantity = quantity
            exit.price = price
            exit.date = date_obj
            exit.note = note
            db.session.commit()
            flash("Sell exit updated successfully.", "success")
            return redirect(url_for('trades.view_trade', trade_id=trade.id))
        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating sell exit: {str(e)}", "error")

    return render_template('edit_exit.html', exit=exit, trade=trade)

# Delete Sell Exit
@trades_bp.route('/exit/<int:exit_id>/delete', methods=['POST'])
@login_required
def delete_exit(exit_id):
    exit = TradeExit.query.get_or_404(exit_id)
    trade = exit.trade

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ CSRF check
        db.session.delete(exit)
        db.session.commit()
        flash("Sell exit deleted successfully.", "success")
    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting sell exit: {str(e)}", "error")

    return redirect(url_for('trades.view_trade', trade_id=trade.id))

# Edit Trade
@trades_bp.route('/trade/<int:trade_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)    

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ CSRF check
            stock_name = request.form.get('stock_name', '').strip().upper()
            entry_date_str = request.form.get('entry_date')
            exit_date_str = request.form.get('exit_date')
            journal = request.form.get('journal', '').strip()
            strategy_tag = request.form.get('strategy_tag', '').strip()

            trade.stock_name = stock_name
            trade.entry_date = date.fromisoformat(entry_date_str) if entry_date_str else None
            trade.exit_date = date.fromisoformat(exit_date_str) if exit_date_str else None
            trade.journal = journal
            trade.strategy_tag = strategy_tag   # ✅ NEW Tag

            db.session.commit()
            flash("Trade updated successfully.", "success")
            return redirect(url_for('trades.dashboard'))

        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating trade: {str(e)}", "error")

    return render_template('edit_trade.html', trade=trade)

# Delete Trade
@trades_bp.route('/trade/<int:trade_id>/delete', methods=['POST'])
@login_required
def delete_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)

    if trade.user_id != current_user.id:
        flash("Unauthorized access.", "error")
        return redirect(url_for('trades.dashboard'))

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ CSRF check
        db.session.delete(trade)
        db.session.commit()
        flash("Trade deleted successfully.", "success")
    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting trade: {str(e)}", "error")

    return redirect(request.referrer or url_for('trades.dashboard'))

#=======
#trade_history() Route
@trades_bp.route('/history', methods=['GET'])
@login_required
def trade_history():
    # 1. Get Filters & Pagination Parameters
    stock_filter = request.args.get('stock', '').upper()
    date_range = request.args.get('date_range', 'all_time')
    strategy_filter = request.args.get('strategy_tag')
    page = request.args.get('page', 1, type=int)
    per_page = 50

    # 2. Base Query (Closed Trades only)
    query = Trade.query.filter_by(user_id=current_user.id, status='Closed')

    # [cite_start]3. Apply Professional Filter Ribbon Logic [cite: 70, 123]
    today = date.today()
    start_date = None
    
    if date_range == 'last_7_days':
        start_date = today - timedelta(days=7)
    elif date_range == 'last_30_days':
        start_date = today - timedelta(days=30)
    elif date_range == 'last_month':
        start_date = today.replace(day=1) - timedelta(days=1)
        start_date = start_date.replace(day=1)
    elif date_range == 'last_3_months':
        start_date = today - timedelta(days=90)
    elif date_range == 'ytd':
        start_date = date(today.year, 1, 1)
    elif date_range == 'last_year':
        start_date = today - timedelta(days=365)

    if start_date:
        query = query.filter(Trade.exit_date >= start_date)
    if stock_filter:
        query = query.filter(Trade.stock_name == stock_filter)
    if strategy_filter:
        query = query.filter(Trade.strategy_tag == strategy_filter)

    # [cite_start]4. Sorting: Always Descending by Exit Date (Last closed at top) [cite: 123]
    query = query.order_by(Trade.exit_date.desc())

    # [cite_start]5. Calculate KPI Metrics for the Header (Based on filtered results) [cite: 74, 75]
    all_filtered_trades = query.all()
    total_trades = len(all_filtered_trades)
    
    wins = [t for t in all_filtered_trades if is_win(t)]
    losses = [t for t in all_filtered_trades if not is_win(t)]
    
    realized_pnl = sum(calculate_realized_pnl(t) for t in all_filtered_trades)
    win_rate = round((len(wins) / total_trades) * 100, 2) if total_trades > 0 else 0
    
    gross_profit = sum(calculate_realized_pnl(t) for t in wins)
    gross_loss = sum(calculate_realized_pnl(t) for t in losses)
    profit_factor = round(gross_profit / abs(gross_loss), 2) if gross_loss != 0 else (round(gross_profit, 2) if gross_profit > 0 else 0)
    
    avg_duration = round(sum((t.exit_date - t.entry_date).days for t in all_filtered_trades if t.entry_date and t.exit_date) / total_trades, 1) if total_trades > 0 else 0

    # [cite_start]6. Apply Pagination [cite: 85]
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    trades_paginated = pagination.items

    # [cite_start]7. Enrich Data for Modern Table [cite: 124, 125, 126]
    enriched = []
    for trade in trades_paginated:
        total_qty = sum(e.quantity for e in trade.entries)
        total_inv = sum(e.quantity * e.price for e in trade.entries)
        total_exit_val = sum(x.quantity * x.price for x in trade.exits)
        
        pnl = round(total_exit_val - total_inv, 2)
        pnl_pct = round((pnl / total_inv) * 100, 2) if total_inv > 0 else 0
        total_days = (trade.exit_date - trade.entry_date).days if trade.entry_date and trade.exit_date else 0
        
        enriched.append({
            'id': trade.id,
            'stock_name': trade.stock_name.upper(),
            'entry_date': trade.entry_date,
            'exit_date': trade.exit_date,
            'avg_entry': round(total_inv / total_qty, 2) if total_qty else 0,
            'avg_exit': round(total_exit_val / total_qty, 2) if total_qty else 0,
            'total_quantity': total_qty,
            'total_days': total_days,
            'total_invested': round(total_inv, 2),
            'realized_pnl': pnl,
            'pnl_pct': pnl_pct,
            'is_win': pnl > 0,
            'strategy': trade.strategy_tag or "No Strategy",
            'notes': " | ".join([e.note for e in trade.entries if e.note] + [x.note for x in trade.exits if x.note])
        })

    stock_list = sorted(set(t.stock_name for t in Trade.query.filter_by(user_id=current_user.id, status='Closed').all()))

    return render_template('trade_history.html',
                           trades=enriched,
                           pagination=pagination,
                           stock_list=stock_list,
                           kpis={
                               'win_rate': win_rate,
                               'pnl': realized_pnl,
                               'profit_factor': profit_factor,
                               'avg_duration': avg_duration
                           },
                           selected_range=date_range,
                           selected_stock=stock_filter,
                           selected_strategy=strategy_filter)

@trades_bp.route('/history/export')
@login_required
def export_history():
    stock_filter = request.args.get('stock', '').upper()
    date_range = request.args.get('date_range')
    sort_order = request.args.get('sort', 'desc')

    query = Trade.query.filter_by(user_id=current_user.id, status='Closed')

    if stock_filter:
        query = query.filter(Trade.stock_name == stock_filter)

    today = date.today()
    if date_range == 'last_month':
        start_date = today.replace(day=1) - timedelta(days=1)
        start_date = start_date.replace(day=1)
        query = query.filter(Trade.exit_date >= start_date)
    elif date_range == 'last_3_months':
        start_date = today - timedelta(days=90)
        query = query.filter(Trade.exit_date >= start_date)
    elif date_range == 'last_year':
        start_date = today - timedelta(days=365)
        query = query.filter(Trade.exit_date >= start_date)

    trades = query.order_by(Trade.exit_date.desc()).all()

    data = []
    for trade in trades:
        total_quantity = sum(e.quantity for e in trade.entries)
        total_invested = sum(e.quantity * e.price for e in trade.entries)
        avg_entry_price = round(total_invested / total_quantity, 2) if total_quantity else 0

        total_exited = sum(x.quantity * x.price for x in trade.exits)
        avg_exit_price = round(total_exited / total_quantity, 2) if total_quantity else 0

        realized_pnl = round(total_exited - total_invested, 2)
        total_days = (trade.exit_date - trade.entry_date).days if trade.entry_date and trade.exit_date else 0

        entry_notes = [e.note for e in trade.entries if e.note]
        exit_notes = [x.note for x in trade.exits if x.note]
        combined_notes = " | ".join(entry_notes + exit_notes)
        date_stamp = datetime.now().strftime('%d-%b-%Y')
        filename = f"trades_{date_stamp}.xlsx"

        data.append({
            'Stock Name': trade.stock_name.upper(),
            'Entry': trade.entry_date.strftime('%d-%b-%Y') if trade.entry_date else '',
            'Exit': trade.exit_date.strftime('%d-%b-%Y') if trade.exit_date else '',
            'Entry (Avg)': avg_entry_price,
            'Exit (Avg)': avg_exit_price,
            'Quantity': total_quantity,
            'Duration': f"{total_days} days",
            'Realized P&L': realized_pnl,
            'Notes': combined_notes
        })

    # ✅ Export logic must be outside the loop
    df = pd.DataFrame(data)

    if sort_order == 'asc':
        df = df.sort_values(by='Realized P&L', ascending=True)
    else:
        df = df.sort_values(by='Realized P&L', ascending=False)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Trade History')

    output.seek(0)

    return send_file(output,
                     download_name=filename,
                 as_attachment=True,
                 mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
