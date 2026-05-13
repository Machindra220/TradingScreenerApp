from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.models import User
from app.extensions import db, login_manager, mail
from flask_login import login_user, logout_user, login_required
from werkzeug.security import generate_password_hash
import re
from flask_mail import Message
from flask import current_app
from flask_wtf.csrf import validate_csrf, CSRFError  # ✅ Add CSRF validation

auth_bp = Blueprint('auth', __name__)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
            return redirect(url_for('auth.register'))

        username = request.form.get('username').strip()
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')

        # Basic validation
        if not username or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for('auth.register'))

        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash("Invalid email format.", "error")
            return redirect(url_for('auth.register'))

        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "error")
            return redirect(url_for('auth.register'))

        # Check for duplicates
        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "error")
            return redirect(url_for('auth.register'))

        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "error")
            return redirect(url_for('auth.register'))

        # Create user
        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
        user = User(username=username, email=email, password_hash=hashed_pw)
        db.session.add(user)
        db.session.commit()

        flash("Registration successful. You can now log in.", "success")
        return redirect(url_for('auth.login'))

    return render_template('register.html')

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
            return redirect(url_for('auth.login'))
        # print('Machindra and machindra')
        username = request.form.get('username').strip()
        password = request.form.get('password')

        if not username or not password:
            flash("Both username and password are required.", "error")
            return redirect(url_for('auth.login'))

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            flash("Login successful. You are now log in.", "success")
            return redirect(url_for('trades.dashboard'))
        else:
            flash("Invalid username or password.", "error")
            return redirect(url_for('auth.login'))

    return render_template('login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for('auth.login'))

@auth_bp.route('/reset_password', methods=['GET', 'POST'])
def reset_request():
    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
            return redirect(url_for('auth.reset_request'))

        email = request.form.get('email').strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            token = user.get_reset_token()
            reset_url = url_for('auth.reset_token', token=token, _external=True)
            print("Reset link:", reset_url)
            flash("Password reset link sent to your email.", "success")
        else:
            flash("Email not found.", "error")
        return redirect(url_for('auth.login'))
    return render_template('reset_request.html')

@auth_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_token(token):
    user = User.verify_reset_token(token)
    if not user:
        flash("Invalid or expired token.", "error")
        return redirect(url_for('auth.reset_request'))

    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))  # ✅ Validate CSRF
        except CSRFError:
            flash("Invalid or missing CSRF token.", "error")
            return redirect(url_for('auth.reset_token', token=token))

        password = request.form.get('password')
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for('auth.reset_token', token=token))
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        flash("Your password has been updated.", "success")
        return redirect(url_for('auth.login'))

    return render_template('reset_form.html')

def send_reset_email(user):
    token = user.get_reset_token()
    reset_url = url_for('auth.reset_token', token=token, _external=True)
    msg = Message(
        subject="Password Reset Request",
        recipients=[user.email],
        body=f"""Hi {user.username},

To reset your password, click the link below:
{reset_url}

If you did not request this, please ignore this email.

Thanks,
Trading Journal Team
"""
    )
    mail.send(msg)
