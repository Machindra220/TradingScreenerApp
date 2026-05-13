from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Watchlist
from datetime import date
from flask_wtf.csrf import validate_csrf, CSRFError  # ✅ Add CSRF imports

watchlist_bp = Blueprint('watchlist', __name__, url_prefix='/watchlist')

# Add entry to watchlist route
@watchlist_bp.route('/', methods=['GET', 'POST'])
@login_required
def add():
    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
            stock_name = request.form.get('stock_name', '').strip().upper()
            target_price = request.form.get('target_price', type=float)
            stop_loss = request.form.get('stop_loss', type=float)
            expected_move = request.form.get('expected_move', type=float)
            setup_type = request.form.get('setup_type')
            confidence = request.form.get('confidence')
            date_added = request.form.get('date_added', type=lambda d: date.fromisoformat(d))
            notes = request.form.get('notes', '').strip()

            if not stock_name or target_price <= 0 or stop_loss <= 0 or expected_move <= 0 or not setup_type:
                flash("Please fill all required fields with valid values.", "danger")
                return redirect(url_for('watchlist.add'))

            item = Watchlist(
                user_id=current_user.id,
                stock_name=stock_name,
                target_price=target_price,
                stop_loss=stop_loss,
                expected_move=expected_move,
                setup_type=setup_type,
                confidence=confidence,
                date_added=date_added,
                notes=notes,
                status='Open'
            )
            db.session.add(item)
            db.session.commit()
            flash(f"{stock_name} added to watchlist.", "success")
            return redirect(url_for('watchlist.add'))

        except CSRFError:
            flash("Invalid or missing CSRF token.", "danger")
        except Exception as e:
            db.session.rollback()
            flash(f"Error adding to watchlist: {str(e)}", "danger")

    watchlist = Watchlist.query.filter_by(user_id=current_user.id).order_by(Watchlist.date_added.desc()).all()
    return render_template('watchlist.html', watchlist=watchlist)

# Edit watchlist route
@watchlist_bp.route('/watchlist/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def edit(item_id):
    item = Watchlist.query.filter_by(id=item_id, user_id=current_user.id).first_or_404()

    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
            item.stock_name = request.form.get('stock_name', '').strip().upper()
            item.target_price = request.form.get('target_price', type=float)
            item.stop_loss = request.form.get('stop_loss', type=float)
            item.expected_move = request.form.get('expected_move', type=float)
            item.setup_type = request.form.get('setup_type')
            item.confidence = request.form.get('confidence')
            item.date_added = request.form.get('date_added', type=lambda d: date.fromisoformat(d))
            item.notes = request.form.get('notes', '').strip()
            item.status = request.form.get('status', 'Open')

            if not item.stock_name or item.target_price <= 0 or item.stop_loss <= 0 or item.expected_move <= 0 or not item.setup_type:
                flash("Please fill all required fields with valid values.", "danger")
                return redirect(url_for('watchlist.edit', item_id=item.id))

            db.session.commit()
            flash(f"{item.stock_name} updated successfully.", "success")
            return redirect(url_for('watchlist.add'))

        except CSRFError:
            flash("Invalid or missing CSRF token.", "danger")
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating watchlist: {str(e)}", "danger")

    return render_template('edit_watchlist.html', item=item)

# Delete watchlist route
@watchlist_bp.route('/watchlist/delete/<int:item_id>', methods=['POST'])
@login_required
def delete(item_id):
    item = Watchlist.query.filter_by(id=item_id, user_id=current_user.id).first_or_404()

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
        db.session.delete(item)
        db.session.commit()
        flash(f"{item.stock_name} removed from watchlist.", "success")
    except CSRFError:
        flash("Invalid or missing CSRF token.", "danger")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting watchlist item: {str(e)}", "danger")

    return redirect(url_for('watchlist.add'))
