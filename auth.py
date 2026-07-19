"""
OneCard Platform — Authentication (v8)
======================================
Login/logout + role-based access decorators.

Role model (v8):
  * cco is the platform owner: full access everywhere (same as admin).
  * admin remains the technical owner account.
  * bd (Business Development) negotiates deals and hands data to Ops
    through the Deal Pipeline — they don't enter catalogue data themselves.
"""
from functools import wraps
from flask import session, redirect, url_for, flash, request
import models

# cco sees and controls everything (business owner of the platform)
SUPER_ROLES = ('admin', 'cco')


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


def _role_required(*roles):
    """Factory: allow the given roles plus the SUPER_ROLES."""
    allowed = set(roles) | set(SUPER_ROLES)

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            user = get_current_user()
            if not user or user['role'] not in allowed:
                flash('You do not have access to that area.', 'error')
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        return decorated
    return decorator


admin_required = _role_required()               # admin + cco only
cco_required = _role_required()                 # same super pair
sales_required = _role_required('sales')
reseller_required = _role_required('reseller')
ops_required = _role_required('ops')
finance_required = _role_required('finance')
bd_required = _role_required('bd')
# Deal pipeline is shared: BD creates, Ops executes, admin/cco oversee
deals_required = _role_required('bd', 'ops')
