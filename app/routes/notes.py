from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user
from app.models import DayNote, NoteImage
from app.extensions import db
from werkzeug.utils import secure_filename
import os
from datetime import date
from flask_wtf.csrf import validate_csrf, CSRFError  # ✅ CSRF imports

notes_bp = Blueprint('notes', __name__)
UPLOAD_FOLDER = 'static/uploads'

@notes_bp.route('/notes')
@login_required
def notes_page():
    notes = DayNote.query.filter_by(user_id=current_user.id).order_by(DayNote.date.desc()).all()
    return render_template('notes.html', notes=notes, today=date.today())

@notes_bp.route('/notes/add', methods=['POST'])
@login_required
def add_note():
    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF

        note_date = request.form.get('date') or str(date.today())
        summary = request.form.get('summary')
        content = request.form.get('content')
        images = request.files.getlist('images')

        if not summary or not content:
            flash("Summary and content are required.", "error")
            return redirect(url_for('notes.notes_page'))

        new_note = DayNote(date=note_date, summary=summary, content=content, user_id=current_user.id)
        db.session.add(new_note)
        db.session.commit()

        for img in images:
            if img.filename:
                filename = secure_filename(img.filename)
                path = os.path.join(UPLOAD_FOLDER, filename)
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                img.save(path)
                db.session.add(NoteImage(filename=filename, note_id=new_note.id))

        db.session.commit()
        flash("Note added successfully!", "success")

    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error adding note: {str(e)}", "error")

    return redirect(url_for('notes.notes_page'))

@notes_bp.route('/notes/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_note(id):
    note = DayNote.query.get_or_404(id)
    if note.user_id != current_user.id:
        abort(403)

    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF

            note.date = request.form.get('date')
            note.summary = request.form.get('summary')
            note.content = request.form.get('content')
            db.session.commit()
            flash("Note updated successfully!", "success")

        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating note: {str(e)}", "error")

        return redirect(url_for('notes.notes_page'))

    return render_template('edit_note.html', note=note)

@notes_bp.route('/notes/delete/<int:id>', methods=['POST'])
@login_required
def delete_note(id):
    note = DayNote.query.get_or_404(id)
    if note.user_id != current_user.id:
        abort(403)

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF

        for img in note.images:
            try:
                os.remove(os.path.join(UPLOAD_FOLDER, img.filename))
            except:
                pass
        db.session.delete(note)
        db.session.commit()
        flash("Note deleted.", "success")

    except CSRFError:
        flash("Invalid or missing CSRF token.", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting note: {str(e)}", "error")

    return redirect(url_for('notes.notes_page'))

@notes_bp.route('/notes/image/delete/<int:id>', methods=['POST'])
@login_required
def delete_image(id):
    img = NoteImage.query.get_or_404(id)
    if img.note.user_id != current_user.id:
        abort(403)

    try:
        validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
        os.remove(os.path.join(UPLOAD_FOLDER, img.filename))
    except CSRFError:
        return jsonify({'success': False, 'error': 'Invalid CSRF token'}), 400
    except:
        pass

    db.session.delete(img)
    db.session.commit()
    return jsonify({'success': True})
