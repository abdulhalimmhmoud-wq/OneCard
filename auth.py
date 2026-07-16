"""
OneCard Platform — Authentication
==================================
Login/logout + role-based access decorators.
"""
from functools import wraps
from flask import session, redirect, url_for, flash, request
import models


def login_user(email, password):
    """Authenticate user. Returns user dict or None."""
    user = models.get_user_by_email(email)
    if user and models.check_pw(password, user['password_hash']):
        return dict(user)
    return None


def get_current_user():
    """Get the currently logged-in user from session."""
    uid = session.get('user_id')
    if uid:
        user = models.get_user_by_id(uid)
        return dict(user) if user else None
    return None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_current_user()
        if not user or user['role'] != 'admin':
            flash('Admin access required.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def sales_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_current_user()
        if not user or user['role'] not in ('sales', 'admin'):
            flash('Sales access required.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def reseller_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_current_user()
        if not user or user['role'] != 'reseller':
            flash('Reseller access required.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def cco_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_current_user()
        if not user or user['role'] not in ('cco', 'admin'):
            flash('CCO access required.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def finance_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_current_user()
        if not user or user['role'] not in ('finance', 'admin'):
            flash('Finance access required.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated
