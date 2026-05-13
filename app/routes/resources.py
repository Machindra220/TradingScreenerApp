from flask import Blueprint, send_file, request, render_template, redirect, url_for, abort, flash
from flask_login import login_required, current_user
from io import BytesIO
import pandas as pd
from app.models import Resource
from app.extensions import db  # ✅ Use extensions to avoid circular import
from datetime import datetime
from flask_wtf.csrf import validate_csrf, CSRFError  # ✅ CSRF imports

resources_bp = Blueprint('resources', __name__)

# ✅ Main Resources Page
@resources_bp.route('/')
@login_required
def show_resources():
    resources = Resource.query.filter_by(user_id=current_user.id).order_by(Resource.pinned.desc()).all()
    categories = db.session.query(Resource.category).filter(Resource.user_id == current_user.id).distinct().all()
    categories = [c[0] for c in categories if c[0]]
    return render_template('resources.html', resources=resources, categories=categories)

# ✅ Add Resource
@resources_bp.route('/add', methods=['POST'])
@login_required
def add_resource():
    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF

        title = request.form.get('title', '').strip()
        url = request.form.get('url', '').strip()
        category = request.form.get('category')
        new_category = request.form.get('new_category', '').strip()
        note = request.form.get('note')
        pinned = bool(request.form.get('pinned'))

        if not title or not url:
            flash("Title and URL are required.", "error")
            return redirect(url_for('resources.show_resources'))

        final_category = new_category if new_category else category

        new = Resource(title=title, url=url, note=note, category=final_category,
                       pinned=pinned, user_id=current_user.id)
        db.session.add(new)
        db.session.commit()
        flash("Resource added successfully!", "success")

    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error adding resource: {str(e)}", "error")

    return redirect(url_for('resources.show_resources'))

# ✅ Edit Resource
@resources_bp.route('/edit/<int:id>', methods=['POST'])
@login_required
def edit_resource(id):
    r = Resource.query.get_or_404(id)
    if r.user_id != current_user.id:
        abort(403)

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF

        r.title = request.form['title']
        r.url = request.form['url']
        r.note = request.form.get('note')
        r.category = request.form.get('category')
        r.pinned = bool(request.form.get('pinned'))
        db.session.commit()
        flash("Resource updated successfully!", "success")

    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating resource: {str(e)}", "error")

    return redirect(url_for('resources.show_resources'))

# ✅ Delete Resource
@resources_bp.route('/delete/<int:id>', methods=['POST'])
@login_required
def delete_resource(id):
    r = Resource.query.get_or_404(id)
    if r.user_id != current_user.id:
        abort(403)

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
        db.session.delete(r)
        db.session.commit()
        flash("Resource deleted.", "success")

    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting resource: {str(e)}", "error")

    return redirect(url_for('resources.show_resources'))

# ✅ Export Resources (GET only — no CSRF needed)
@resources_bp.route('/export')
@login_required
def export_resources():
    resources = Resource.query.filter_by(user_id=current_user.id).all()
    df = pd.DataFrame([{
        'Title': r.title,
        'URL': r.url,
        'Category': r.category,
        'Note': r.note,
        'Pinned': r.pinned,
        'Last Accessed': r.last_accessed
    } for r in resources])
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Resources')
    output.seek(0)
    return send_file(output, download_name='resources.xlsx', as_attachment=True)

# ✅ Last Access Time of Resource (GET only — no CSRF needed)
@resources_bp.route('/resources/access/<int:id>')
@login_required
def access_resource(id):
    resource = Resource.query.get_or_404(id)
    resource.last_accessed = datetime.utcnow()
    db.session.commit()
    return redirect(resource.url)
