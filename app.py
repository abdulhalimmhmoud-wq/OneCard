"""
OneCard Platform — Flask Web Application (v4)
=============================================
Roles: Admin (BD Manager), Sales Manager, Reseller, CCO, Finance.

Workflows:
  Sales registers reseller (client type + markets) → auto tier by expected sales
  Reseller browses catalogue → submits Forecast (pre-contract)
  Contract signed → Reseller orders using Wallet balance
  Wallet top-up: bank transfer receipt → Finance approval → balance credited
  Sales requests special merchant discount → CCO approves → auto-applied
  Tier compliance: below commitment → grace month → automatic downgrade
"""
from flask import (Flask, render_template, request, redirect, url_for, session,
                   flash, send_from_directory, abort)
import os
import json
import time
import uuid
import secrets as _secrets
from datetime import datetime, timezone, timedelta
import models
import auth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

# ── Hardening: stable secret key (env first, else persisted file) ──
# A random-per-boot key logs everyone out on restart and breaks multi-worker.
_key = os.environ.get('ONECARD_SECRET_KEY')
if not _key:
    _key_file = os.path.join(BASE_DIR, 'instance_secret.key')
    if os.path.exists(_key_file):
        _key = open(_key_file).read().strip()
    else:
        _key = _secrets.token_hex(32)
        with open(_key_file, 'w') as f:
            f.write(_key)
app.secret_key = _key

# ── Hardening: upload size cap (largest legit upload is a price file) ──
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024   # 8 MB

# ── Hardening: session cookie policy ──
# SESSION_COOKIE_SECURE is off by default because the prototype serves plain
# HTTP on localhost — browsers silently drop "Secure" cookies over HTTP, which
# would break every login. Set ONECARD_COOKIE_SECURE=1 once deployed behind
# HTTPS (production must run behind TLS regardless).
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('ONECARD_COOKIE_SECURE', '0') == '1'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

DEBUG_MODE = os.environ.get('ONECARD_DEBUG', '0') == '1'

UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads', 'receipts')
PRICEFILE_DIR = os.path.join(BASE_DIR, 'uploads', 'pricefiles')
CONTRACT_DIR = os.path.join(BASE_DIR, 'uploads', 'contracts')
INTEL_DIR = os.path.join(BASE_DIR, 'uploads', 'intel')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PRICEFILE_DIR, exist_ok=True)
os.makedirs(CONTRACT_DIR, exist_ok=True)
os.makedirs(INTEL_DIR, exist_ok=True)
ALLOWED_CONTRACT_EXT = {'.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg'}
ALLOWED_INTEL_EXT = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.xls', '.xlsx', '.csv', '.doc', '.docx'}


def _save_contract_file(file, reseller_id):
    """Persist an uploaded contract document; returns the stored filename or None
    if the extension isn't an allowed document/image type."""
    if not file or not file.filename:
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_CONTRACT_EXT:
        return None
    fname = f"c{reseller_id}_{uuid.uuid4().hex[:12]}{ext}"
    file.save(os.path.join(CONTRACT_DIR, fname))
    return fname


def _save_intel_file(file):
    """Persist a competitor-intel source file (screenshot / PDF / Excel).
    Returns the stored filename or None if the type isn't allowed."""
    if not file or not file.filename:
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_INTEL_EXT:
        return None
    fname = f"intel_{uuid.uuid4().hex[:12]}{ext}"
    file.save(os.path.join(INTEL_DIR, fname))
    return fname
# Receipt uploads are validated by content (see _sniff_receipt_ext), not
# extension — they're reachable by any reseller. Price files stay
# extension-checked; only staff (Ops) can reach that upload.
ALLOWED_PRICEFILE_EXT = {'.xls', '.xlsx'}


def jdump(obj):
    """JSON for inline <script> use — escapes '</' so markup in data can't
    break out of the script tag (XSS guard for |safe payloads)."""
    return json.dumps(obj).replace('</', '<\\/')


def _sniff_receipt_ext(file_storage):
    """Identify the real file type from its magic bytes rather than trusting
    the claimed filename extension — a renamed .html/.exe must not pass as a
    receipt just because someone typed .png on the end. Returns the true
    extension to save with, or None if the content isn't one of the allowed
    types at all."""
    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if head.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if head.startswith(b'%PDF-'):
        return '.pdf'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return '.webp'
    return None


# ── Hardening: generic in-memory attempt limiter ──────────────────
# Shared by login (per email+IP) and gift-card redemption (per code) so a
# PIN can't be brute-forced any more than a password can. In-memory only:
# resets on restart and is per-worker, which is fine at prototype scale —
# a production deployment should back this with Redis/memcached.
_attempt_log = {}


def rate_limited(bucket, key, max_tries, window_sec):
    now = time.time()
    log_key = f"{bucket}:{key}"
    tries = [t for t in _attempt_log.get(log_key, []) if now - t < window_sec]
    _attempt_log[log_key] = tries
    return len(tries) >= max_tries


def record_attempt(bucket, key):
    _attempt_log.setdefault(f"{bucket}:{key}", []).append(time.time())


LOGIN_MAX_TRIES = 5
LOGIN_WINDOW_SEC = 15 * 60
REDEEM_MAX_TRIES = 8
REDEEM_WINDOW_SEC = 15 * 60


# ── Context Processor ─────────────────────────────────────────────

@app.context_processor
def inject_user():
    """Inject logged-in user, preview state and unread notifications into all templates."""
    curr = auth.get_current_user()
    is_preview = 'preview_user_id' in session
    preview_company = None
    if is_preview:
        profile = models.get_reseller_profile(session['preview_user_id'])
        if profile:
            preview_company = profile['company_name']
    unread = models.unread_count(curr['id']) if curr else 0
    # CSRF token: created once per session, injected into every form by app.js
    if '_csrf' not in session:
        session['_csrf'] = _secrets.token_hex(16)
    return {
        'current_user': curr,
        'is_preview': is_preview,
        'preview_company': preview_company,
        'unread_notifications': unread,
        'csrf_token': session['_csrf'],
    }


@app.template_filter('money')
def money_filter(v):
    """Whole-number money formatting (business rule: no decimals)."""
    try:
        return f"{round(float(v or 0)):,}"
    except (TypeError, ValueError):
        return '0'


@app.before_request
def csrf_protect():
    """Reject any state-changing request without the session CSRF token.
    The supplier API is exempt (authenticated by per-supplier api_key)."""
    if request.method == 'POST' and not request.path.startswith('/api/'):
        token = session.get('_csrf')
        sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
        if not token or sent != token:
            abort(403, description='CSRF token missing or invalid. Refresh the page and try again.')


@app.before_request
def enforce_suspension():
    """A reseller suspended mid-session is signed out on their next request.
    Sales preview is unaffected: there the session user is the sales manager,
    while the previewed reseller is only in preview_user_id."""
    if request.endpoint in (None, 'static', 'login', 'logout'):
        return
    uid = session.get('user_id')
    if uid and models.is_user_suspended(uid):
        session.clear()
        flash("Your account is inactive. Please contact your account manager.", "error")
        return redirect(url_for('login'))


@app.before_request
def enforce_nda():
    """A new reseller must accept the confidentiality notice (NDA) before using
    the portal. Sales preview is unaffected (session user is the sales manager)."""
    if request.endpoint in (None, 'static', 'login', 'logout',
                            'reseller_nda', 'reseller_accept_nda'):
        return
    if request.path.startswith('/api/'):
        return
    uid = session.get('user_id')
    if uid and 'preview_user_id' not in session and models.reseller_nda_pending(uid):
        return redirect(url_for('reseller_nda'))


_last_compliance_tick = 0.0
COMPLIANCE_TICK_INTERVAL = 300   # seconds


@app.before_request
def daily_compliance_check():
    """Lazy daily tier-compliance run. models.run_tier_compliance() already
    throttles itself to once/day via app_meta, but that still means an extra
    SQLite connection + query on every single request. This in-process gate
    (per worker) caps how often we even open a connection to ask; a few
    minutes' delay in noticing the day rolled over is irrelevant for a
    monthly compliance job."""
    global _last_compliance_tick
    if request.endpoint == 'static':
        return
    now = time.time()
    if now - _last_compliance_tick < COMPLIANCE_TICK_INTERVAL:
        return
    _last_compliance_tick = now
    models.run_tier_compliance()


# ── Hardening: friendly error pages ──────────────────────────────

@app.errorhandler(403)
def err_403(e):
    return render_template('error.html', code=403,
                           message=getattr(e, 'description', 'Access denied.')), 403


@app.errorhandler(404)
def err_404(e):
    return render_template('error.html', code=404, message='Page not found.'), 404


@app.errorhandler(413)
def err_413(e):
    return render_template('error.html', code=413,
                           message='File too large — maximum upload size is 8 MB.'), 413


@app.errorhandler(500)
def err_500(e):
    return render_template('error.html', code=500,
                           message='Something went wrong. The error was logged — please try again.'), 500


def get_active_reseller_uid():
    """Get active user_id for reseller views (supports preview mode)."""
    if 'preview_user_id' in session:
        curr = auth.get_current_user()
        if curr and curr['role'] in ('admin', 'sales'):
            return session['preview_user_id']
    return session.get('user_id')


def block_in_preview():
    """Preview mode is read-only: sales/admin cannot act on behalf of the reseller."""
    if 'preview_user_id' in session:
        flash("Preview mode is read-only — actions are disabled.", "warning")
        return True
    return False


# ── Global Routes ────────────────────────────────────────────────

# v8: CCO is the platform owner — lands on the full admin dashboard
ROLE_HOME = {'admin': 'admin_dashboard', 'sales': 'sales_dashboard',
             'cco': 'admin_dashboard', 'finance': 'finance_dashboard',
             'ops': 'ops_dashboard', 'bd': 'bd_dashboard',
             'partner': 'partner_dashboard',
             'reseller': 'reseller_dashboard'}


@app.route('/')
def index():
    user = auth.get_current_user()
    if not user:
        return redirect(url_for('login'))
    return redirect(url_for(ROLE_HOME.get(user['role'], 'reseller_dashboard')))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if auth.get_current_user():
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = (request.form.get('email') or '').lower().strip()
        password = request.form.get('password')
        rl_key = f"{email}|{request.remote_addr}"
        if rate_limited('login', rl_key, LOGIN_MAX_TRIES, LOGIN_WINDOW_SEC):
            flash("Too many failed attempts — try again in 15 minutes.", "error")
            return render_template('login.html'), 429
        user = auth.login_user(email, password)
        if user:
            _attempt_log.pop(f"login:{rl_key}", None)
            if user['role'] == 'reseller' and models.is_user_suspended(user['id']):
                flash("Your account is inactive. Please contact your account manager "
                      "to reactivate it.", "error")
                return render_template('login.html')
            session['user_id'] = user['id']
            session.permanent = True
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for('index'))
        else:
            record_attempt('login', rl_key)
            flash("Invalid email or password.", "error")
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for('login'))


@app.route('/notifications')
@auth.login_required
def notifications():
    curr = auth.get_current_user()
    notes = models.get_notifications(curr['id'])
    models.mark_notifications_read(curr['id'])
    return render_template('notifications.html', active_tab='notifications', notes=notes)


@app.route('/receipts/<int:txn_id>')
@auth.login_required
def view_receipt(txn_id):
    """Receipt files are private: finance/admin, or the owning reseller."""
    txn = models.get_topup(txn_id)
    if not txn or not txn['receipt_file']:
        abort(404)
    curr = auth.get_current_user()
    if curr['role'] not in ('finance', 'admin') and curr['id'] != txn['reseller_user_id']:
        abort(403)
    return send_from_directory(UPLOAD_DIR, txn['receipt_file'])


@app.route('/contracts/<int:cid>/file/<which>')
@auth.login_required
def contract_file(cid, which):
    """Serve a contract's draft/signed document. Private: the owning reseller,
    the sales manager who owns them, or CCO/Finance/admin."""
    contract, _ = models.get_contract(cid)
    if not contract:
        abort(404)
    fname = contract['file_draft'] if which == 'draft' else contract['file_signed']
    if not fname:
        abort(404)
    curr = auth.get_current_user()
    allowed = (curr['role'] in ('cco', 'admin', 'finance')
               or curr['id'] == contract['reseller_user_id']
               or (curr['role'] == 'sales' and curr['id'] == contract['registered_by']))
    if not allowed:
        abort(403)
    return send_from_directory(CONTRACT_DIR, fname)


# ── Admin Routes (BD Manager) ────────────────────────────────────

@app.route('/admin')
@auth.admin_required
def admin_dashboard():
    stats = models.get_product_stats()
    tiers = models.get_all_tiers()
    resellers = models.get_all_resellers()
    pending_topups = len(models.get_topups('pending'))
    pending_discounts = len(models.get_discount_requests('pending'))
    backups = models.list_backups()[:5]
    return render_template('admin/dashboard.html', active_tab='dashboard', stats=stats,
                           tiers=tiers, resellers=resellers,
                           pending_topups=pending_topups, pending_discounts=pending_discounts,
                           backups=backups)


@app.route('/admin/tiers', methods=['GET', 'POST'])
@auth.admin_required
def admin_tiers():
    if request.method == 'POST':
        ids = request.form.getlist('tier_id')
        names = request.form.getlist('tier_name')
        min_sales = request.form.getlist('tier_min_sales')
        min_merch = request.form.getlist('tier_min_merch')
        margins = request.form.getlist('tier_margin')
        colors = request.form.getlist('tier_color')
        for i in range(len(ids)):
            models.upsert_tier(ids[i], names[i], float(min_sales[i] or 0),
                               int(min_merch[i] or 1), float(margins[i] or 20),
                               colors[i], i + 1)
        flash("Tier rules updated successfully.", "success")
        return redirect(url_for('admin_tiers'))
    tiers = models.get_all_tiers()
    return render_template('admin/tiers.html', active_tab='tiers', tiers=tiers)


@app.route('/admin/tiers/add', methods=['POST'])
@auth.admin_required
def admin_add_tier():
    models.upsert_tier(None, "New Tier", 0, 1, 20, "#64748b", 99)
    flash("Blank tier rule added. Edit and save below.", "info")
    return redirect(url_for('admin_tiers'))


@app.route('/admin/tiers/delete/<int:tid>', methods=['POST'])
@auth.admin_required
def admin_delete_tier(tid):
    models.delete_tier(tid)
    flash("Tier rule deleted.", "info")
    return redirect(url_for('admin_tiers'))


@app.route('/admin/catalogue')
@auth.admin_required
def admin_catalogue():
    products = models.get_products()
    categories = models.get_all_categories()
    regions = models.get_all_regions()
    tiers = models.get_all_tiers()
    products_json = jdump(products)
    tiers_json = jdump([dict(t) for t in tiers])
    return render_template('admin/catalogue.html', active_tab='catalogue',
                           products=products, products_json=products_json,
                           categories=categories, regions=regions,
                           tiers=tiers, tiers_json=tiers_json)


@app.route('/admin/resellers')
@auth.admin_required
def admin_resellers():
    resellers = models.get_all_resellers()
    return render_template('admin/resellers.html', active_tab='resellers', resellers=resellers)


@app.route('/admin/users')
@auth.admin_required
def admin_users():
    users = models.get_all_users()
    return render_template('admin/users.html', active_tab='users', users=users)


@app.route('/admin/users/create', methods=['POST'])
@auth.admin_required
def admin_create_user():
    name = request.form.get('name')
    email = request.form.get('email')
    password = request.form.get('password')
    role = request.form.get('role')
    if role not in models.ROLES:
        flash("Invalid role.", "error")
        return redirect(url_for('admin_users'))
    uid = models.create_user(email, password, name, role)
    if uid:
        flash(f"User {name} created successfully.", "success")
    else:
        flash("Email address is already in use.", "error")
    return redirect(url_for('admin_users'))


@app.route('/admin/compliance/run', methods=['POST'])
@auth.admin_required
def admin_run_compliance():
    actions = models.run_tier_compliance(force=True)
    if actions:
        flash("Compliance check done: " + " | ".join(actions), "info")
    else:
        flash("Compliance check done — all contracted resellers are on track.", "success")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/backup/run', methods=['POST'])
@auth.admin_required
def admin_run_backup():
    path = models.backup_database()
    flash(f"Backup created: {os.path.basename(path)}. Older than "
          f"{models.BACKUP_RETENTION_DAYS} days are pruned automatically.", "success")
    return redirect(url_for('admin_dashboard'))


# ── Contract signing (sales manager or admin) ────────────────────

@app.route('/resellers/<int:rid>/contract', methods=['POST'])
@auth.sales_required
def sign_contract(rid):
    curr = auth.get_current_user()
    profile = models.get_reseller_profile_by_id(rid)
    if not profile:
        flash("Reseller not found.", "error")
        return redirect(url_for('index'))
    if curr['role'] != 'admin' and profile['registered_by'] != curr['id']:
        flash("Access denied.", "error")
        return redirect(url_for('index'))
    new_status = request.form.get('status', 'contracted')
    models.set_contract_status(rid, new_status)
    if new_status == 'contracted':
        models.notify(profile['user_id'], "Contract activated 🎉",
                      "Your contract is now active. You can place orders from your wallet balance.",
                      "/reseller/orders")
        flash(f"Contract marked as signed for {profile['company_name']}. Ordering is now enabled.", "success")
    else:
        flash(f"{profile['company_name']} set back to prospect.", "info")
    return redirect(request.referrer or url_for('index'))


@app.route('/sales/resellers/<int:rid>/contract/upload', methods=['POST'])
@auth.sales_required
def sales_contract_upload(rid):
    """Sales uploads a draft contract and proposes the commercial terms
    (account type + credit line). The reseller then signs and re-uploads."""
    curr = auth.get_current_user()
    profile = models.get_reseller_profile_by_id(rid)
    if not profile or (curr['role'] not in ('admin', 'cco') and profile['registered_by'] != curr['id']):
        flash("Access denied.", "error")
        return redirect(url_for('sales_resellers'))
    fname = _save_contract_file(request.files.get('draft'), rid)
    if not fname:
        flash("Attach the contract as PDF, Word or an image.", "error")
        return redirect(url_for('sales_resellers'))
    account_type = request.form.get('account_type', 'prepaid')
    if account_type not in models.ACCOUNT_TYPES:
        account_type = 'prepaid'
    to_sar = lambda v: round(models.convert_amount(float(v or 0), profile['display_currency'], 'SAR'), 2)
    credit_limit = to_sar(request.form.get('credit_limit')) if account_type != 'prepaid' else 0
    credit_tranche = to_sar(request.form.get('credit_tranche')) if account_type == 'credit' else 0
    disbursement = request.form.get('credit_disbursement', 'full')
    if disbursement not in models.CREDIT_DISBURSEMENTS:
        disbursement = 'full'
    try:
        terms_days = int(request.form.get('settlement_terms_days') or 30)
    except ValueError:
        terms_days = 30
    billing_cycle = request.form.get('billing_cycle', 'monthly')
    if billing_cycle not in models.BILLING_CYCLES:
        billing_cycle = 'monthly'
    # v17 CRM: the commercial registration number is the customer's primary
    # identifier, usually captured here at contract time. Guard against another
    # customer already using the same CR.
    crn = (request.form.get('commercial_reg_no') or '').strip()
    if crn:
        dup = models.find_duplicate_reseller(commercial_reg_no=crn, exclude_id=rid)
        if dup:
            flash(f"Commercial registration number already belongs to "
                  f"'{dup['company_name']}' ({dup['sales_name'] or 'another manager'}).", "error")
            return redirect(request.referrer or url_for('sales_resellers'))
    cid = models.create_contract(rid, curr['id'], fname, account_type, credit_limit,
                                 disbursement, credit_tranche, terms_days, billing_cycle,
                                 request.form.get('note', ''), commercial_reg_no=crn)
    models.notify(profile['user_id'], "Your contract is ready to sign ✍️",
                  "Your account manager sent a contract to review and sign. Open it in your "
                  "portal, sign it, and upload the signed copy.", "/reseller/contract")
    flash(f"Contract sent to {profile['company_name']} for signing.", "success")
    return redirect(request.referrer or url_for('sales_resellers'))


@app.route('/contracts/<int:cid>/activate', methods=['POST'])
@auth.login_required
def activate_contract(cid):
    """Activate a signed contract, applying its terms. Governance: CCO/admin may
    always activate; the sales owner may activate only when CCO sign-off isn't
    required (prepaid, or a credit/consignment limit at or below the cap)."""
    curr = auth.get_current_user()
    contract, _ = models.get_contract(cid)
    if not contract:
        flash("Contract not found.", "error")
        return redirect(url_for('index'))
    needs_cco = models.contract_needs_cco(contract)
    is_super = curr['role'] in ('cco', 'admin')
    is_owner_sales = curr['role'] == 'sales' and curr['id'] == contract['registered_by']
    if not (is_super or (is_owner_sales and not needs_cco)):
        flash("This credit line needs CCO approval to activate." if needs_cco
              else "Access denied.", "error")
        return redirect(request.referrer or url_for('index'))
    ok, err = models.activate_contract(cid, curr['id'])
    if not ok:
        flash(err, "error")
        return redirect(request.referrer or url_for('index'))
    label = models.ACCOUNT_TYPE_LABELS.get(contract['account_type'], (contract['account_type'],))[0]
    models.notify(contract['reseller_user_id'], "Contract activated 🎉",
                  f"Your contract is active on a {label} arrangement. You can now place orders.",
                  "/reseller/orders")
    # Finance always gets visibility on a newly provisioned credit/consignment line.
    if contract['account_type'] != 'prepaid':
        models.notify(models.get_user_ids_by_role('finance', 'cco'),
                      "New credit line activated 💳",
                      f"{contract['company_name']} activated as {label} with a "
                      f"{contract['credit_limit']:,.0f} SAR limit.", "/finance")
    flash(f"Contract activated for {contract['company_name']}.", "success")
    return redirect(request.referrer or url_for('sales_resellers'))


# ── Sales Manager Routes ─────────────────────────────────────────

@app.route('/sales')
@auth.sales_required
def sales_dashboard():
    curr = auth.get_current_user()
    resellers = models.get_all_resellers(registered_by=curr['id'])
    stats = models.get_product_stats()
    forecasts = models.get_forecasts_for_sales(curr['id'])
    pending_discounts = [d for d in models.get_discount_requests(requested_by=curr['id'])
                         if d['status'] == 'pending']
    return render_template('sales/dashboard.html', active_tab='dashboard',
                           resellers=resellers, stats=stats,
                           forecasts=forecasts[:5], pending_discounts=len(pending_discounts))


@app.route('/sales/register', methods=['GET', 'POST'])
@auth.sales_required
def sales_register():
    if request.method == 'POST':
        comp = (request.form.get('company_name') or '').strip()
        cname = request.form.get('contact_name')
        cemail = (request.form.get('contact_email') or '').strip()
        cphone = (request.form.get('contact_phone') or '').strip()
        pw = request.form.get('password')
        sales = float(request.form.get('expected_sales') or 0)
        notes = request.form.get('notes', '')
        client_types = request.form.getlist('client_types')
        countries = request.form.getlist('countries')
        hidden_merchants = request.form.getlist('hidden_merchants')
        # Currency the reseller sees everything in. Default derives from their
        # markets; the form lets the sales manager override it explicitly.
        display_currency = request.form.get('display_currency') or None

        assigned = models.auto_assign_tier(sales)
        tier_id = assigned['id'] if assigned else None

        # v17 CRM: block a second registration of the same customer (a customer
        # may reach two sales managers who don't know about each other).
        dup = models.find_duplicate_reseller(email=cemail, company_name=comp, phone=cphone)
        if not cphone:
            flash("Please enter the contact person's phone number.", "error")
        elif dup:
            flash(f"This customer is already registered (matched on {dup['matched']}) as "
                  f"'{dup['company_name']}' under {dup['sales_name'] or 'another manager'}. "
                  f"Please coordinate with your team / CCO.", "error")
        else:
            uid = models.create_user(cemail, pw, cname, 'reseller')
            if uid:
                curr = auth.get_current_user()
                models.create_reseller(uid, comp, sales, tier_id, curr['id'], notes,
                                       client_types=client_types, countries=countries,
                                       display_currency=display_currency, contact_phone=cphone,
                                       hidden_merchants=hidden_merchants)
                flash(f"Reseller '{comp}' registered successfully with "
                      f"'{assigned['name'] if assigned else 'None'}' plan.", "success")
                return redirect(url_for('sales_dashboard'))
            else:
                flash("Reseller email address is already in use.", "error")

    tiers = models.get_all_tiers()
    tiers_json = jdump([dict(t) for t in tiers])
    return render_template('sales/register.html', active_tab='register',
                           tiers_json=tiers_json,
                           client_types=models.CLIENT_TYPES,
                           display_currencies=models.DISPLAY_CURRENCIES,
                           merchants=models.get_all_merchants(),
                           countries=models.get_all_countries())


@app.route('/sales/resellers')
@auth.sales_required
def sales_resellers():
    curr = auth.get_current_user()
    resellers = models.get_all_resellers(registered_by=curr['id'])
    for r in resellers:
        r['contract'] = models.get_latest_contract(r['id'])
        r['account_type_needs_cco'] = (models.contract_needs_cco(r['contract'])
                                       if r['contract'] else False)
        r['hidden_merchants'] = models.get_reseller_profile_by_id(r['id'])['hidden_merchants']
    return render_template('sales/my_resellers.html', active_tab='resellers', resellers=resellers,
                           account_types=models.ACCOUNT_TYPES,
                           account_labels=models.ACCOUNT_TYPE_LABELS,
                           auto_cap=models.AUTO_APPROVE_CAP,
                           all_merchants=models.get_all_merchants())


@app.route('/sales/resellers/<int:rid>/hidden-merchants', methods=['POST'])
@auth.sales_required
def sales_hidden_merchants(rid):
    """Sales sets which merchants a reseller can't see (e.g. their competitors)."""
    curr = auth.get_current_user()
    profile = models.get_reseller_profile_by_id(rid)
    if not profile or (curr['role'] not in ('admin', 'cco') and profile['registered_by'] != curr['id']):
        flash("Access denied.", "error")
        return redirect(url_for('sales_resellers'))
    models.set_reseller_hidden_merchants(rid, request.form.getlist('hidden_merchants'))
    flash(f"Hidden merchants updated for {profile['company_name']}.", "success")
    return redirect(url_for('sales_resellers'))


@app.route('/sales/merchant-pricing')
@auth.sales_required
def sales_merchant_pricing():
    """JSON feed for the discount calculator: a merchant's products priced in the
    reseller's display currency, so the browser can compute the margin-share %
    needed to hit a target price (and vice versa)."""
    curr = auth.get_current_user()
    try:
        rid = int(request.args.get('reseller_id') or 0)
    except ValueError:
        return {'error': 'bad reseller_id'}, 400
    merchant = request.args.get('merchant', '')
    profile = models.get_reseller_profile_by_id(rid)
    if not profile or (curr['role'] not in ('admin', 'cco') and profile['registered_by'] != curr['id']):
        return {'error': 'not_found'}, 404
    data = models.get_merchant_pricing_for_reseller(rid, merchant)
    return data or {'error': 'not_found'}, (200 if data else 404)


@app.route('/sales/resellers/<int:rid>/update', methods=['POST'])
@auth.sales_required
def sales_update_reseller(rid):
    curr = auth.get_current_user()
    profile = models.get_reseller_profile_by_id(rid)
    if not profile or (curr['role'] != 'admin' and profile['registered_by'] != curr['id']):
        flash("Access denied.", "error")
        return redirect(url_for('sales_resellers'))
    client_type = request.form.get('client_type')
    countries = request.form.getlist('countries')
    display_currency = request.form.get('display_currency')
    models.update_reseller_profile(rid, client_type=client_type, countries=countries,
                                   display_currency=display_currency)
    flash(f"Profile updated for {profile['company_name']}.", "success")
    return redirect(url_for('sales_resellers'))


@app.route('/sales/resellers/<int:rid>/reactivate', methods=['POST'])
@auth.sales_required
def sales_reactivate_reseller(rid):
    """Sales manager re-enables a suspended reseller, granting them a fresh
    conversion window before the auto-suspension sweep applies again."""
    curr = auth.get_current_user()
    profile = models.get_reseller_profile_by_id(rid)
    if not profile or (curr['role'] not in ('admin', 'cco') and profile['registered_by'] != curr['id']):
        flash("Access denied.", "error")
        return redirect(url_for('sales_resellers'))
    uid = models.set_reseller_suspended(rid, False, actor_id=curr['id'])
    if uid:
        models.notify(uid, "Account reactivated ✅",
                      "Your account manager reactivated your account. You can log in again.",
                      "/login")
    flash(f"{profile['company_name']} reactivated — they can log in again.", "success")
    return redirect(url_for('sales_resellers'))


@app.route('/sales/resellers/<int:rid>/credit-request', methods=['POST'])
@auth.sales_required
def sales_credit_request(rid):
    """Sales asks CCO + Finance to raise a credit/consignment limit."""
    curr = auth.get_current_user()
    profile = models.get_reseller_profile_by_id(rid)
    if not profile or (curr['role'] not in ('admin', 'cco') and profile['registered_by'] != curr['id']):
        flash("Access denied.", "error")
        return redirect(url_for('sales_resellers'))
    if (profile['account_type'] or 'prepaid') == 'prepaid':
        flash("Credit increases apply to credit/consignment accounts only.", "error")
        return redirect(url_for('sales_resellers'))
    amount_disp = float(request.form.get('amount') or 0)
    if amount_disp <= 0:
        flash("Enter a valid increase amount.", "error")
        return redirect(url_for('sales_resellers'))
    amount_sar = round(models.convert_amount(amount_disp, profile['display_currency'], 'SAR'), 2)
    kind = 'temporary' if request.form.get('kind') == 'temporary' else 'permanent'
    expires_on = request.form.get('expires_on') or None
    models.create_credit_request(rid, curr['id'], amount_sar, kind, expires_on,
                                 request.form.get('reason', ''))
    flash(f"Credit increase requested for {profile['company_name']} — sent to CCO and Finance.", "success")
    return redirect(url_for('sales_resellers'))


@app.route('/sales/resellers/<int:rid>/api', methods=['POST'])
@auth.sales_required
def sales_reseller_api(rid):
    """Generate/rotate the reseller's API key and set their webhook URL."""
    curr = auth.get_current_user()
    profile = models.get_reseller_profile_by_id(rid)
    if not profile or (curr['role'] not in ('admin', 'cco') and profile['registered_by'] != curr['id']):
        flash("Access denied.", "error")
        return redirect(url_for('sales_resellers'))
    action = request.form.get('action')
    if action == 'rotate':
        key = models.set_reseller_api(rid, rotate_key=True)
        flash(f"API key for {profile['company_name']} (copy it now, it is shown once): {key}", "success")
    elif action == 'webhook':
        models.set_reseller_api(rid, webhook_url=request.form.get('webhook_url', ''))
        flash(f"Webhook URL saved for {profile['company_name']}.", "success")
    return redirect(url_for('sales_resellers'))


@app.route('/sales/catalogue')
@auth.sales_required
def sales_catalogue():
    products = models.get_products()
    categories = models.get_all_categories()
    regions = models.get_all_regions()
    tiers = models.get_all_tiers()
    products_json = jdump(products)
    tiers_json = jdump([dict(t) for t in tiers])
    return render_template('sales/catalogue.html', active_tab='catalogue',
                           products=products, products_json=products_json,
                           categories=categories, regions=regions,
                           tiers=tiers, tiers_json=tiers_json)


@app.route('/sales/forecasts')
@auth.sales_required
def sales_forecasts():
    curr = auth.get_current_user()
    scope = None if curr['role'] == 'admin' else curr['id']
    forecasts = models.get_forecasts_for_sales(scope)
    return render_template('sales/forecasts.html', active_tab='forecasts', forecasts=forecasts)


@app.route('/sales/forecasts/<int:fid>')
@auth.sales_required
def sales_forecast_detail(fid):
    curr = auth.get_current_user()
    forecast, items = models.get_forecast_detail(fid)
    if not forecast:
        flash("Forecast not found.", "error")
        return redirect(url_for('sales_forecasts'))
    if curr['role'] != 'admin' and forecast['registered_by'] != curr['id']:
        flash("Access denied.", "error")
        return redirect(url_for('sales_forecasts'))
    if forecast['status'] == 'submitted':
        models.mark_forecast_reviewed(fid)
    return render_template('sales/forecast_detail.html', active_tab='forecasts',
                           forecast=forecast, items=items)


@app.route('/sales/discounts', methods=['GET', 'POST'])
@auth.sales_required
def sales_discounts():
    curr = auth.get_current_user()
    if request.method == 'POST':
        rid = int(request.form.get('reseller_id'))
        merchant = request.form.get('merchant')
        requested_share = float(request.form.get('requested_share') or 0)
        current_sales = float(request.form.get('current_sales') or 0)
        projected_sales = float(request.form.get('projected_sales') or 0)
        note = request.form.get('note', '')

        profile = models.get_reseller_profile_by_id(rid)
        if not profile or (curr['role'] != 'admin' and profile['registered_by'] != curr['id']):
            flash("Access denied.", "error")
            return redirect(url_for('sales_discounts'))

        current_share = profile['overrides'].get(
            merchant, profile['tier']['margin_share_pct'] if profile['tier'] else 20)
        models.create_discount_request(rid, merchant, current_share, requested_share,
                                       current_sales, projected_sales, note, curr['id'])
        models.notify(models.get_user_ids_by_role('cco'),
                      "New special discount request",
                      f"{curr['name']} requests {requested_share:.0f}% margin share on "
                      f"'{merchant}' for {profile['company_name']} "
                      f"(current {current_share:.0f}%).", "/cco")
        flash("Discount request sent to the CCO for approval.", "success")
        return redirect(url_for('sales_discounts'))

    resellers = models.get_all_resellers(registered_by=None if curr['role'] == 'admin' else curr['id'])
    merchants = models.get_all_merchants()
    my_requests = models.get_discount_requests(requested_by=curr['id'])
    # tier + override data for the form's live preview
    resellers_json = jdump([{
        'id': r['id'], 'company_name': r['company_name'],
        'share': r['margin_share_pct'] or 20, 'tier_name': r['tier_name'] or 'None',
    } for r in resellers])
    merchants_json = jdump([{'merchant': m['merchant'], 'avg_margin': m['avg_margin'] or 0}
                                 for m in merchants])
    return render_template('sales/discounts.html', active_tab='discounts',
                           resellers=resellers, merchants=merchants, requests=my_requests,
                           resellers_json=resellers_json, merchants_json=merchants_json)


# ── Sales: Competitor price intelligence -> BD (v19) ─────────────

@app.route('/sales/competitor-intel', methods=['GET', 'POST'])
@auth.sales_required
def sales_competitor_intel():
    curr = auth.get_current_user()
    if request.method == 'POST':
        merchant = request.form.get('merchant', '').strip()
        competitor = request.form.get('competitor_name', '').strip()
        note = request.form.get('note', '').strip()
        if not (merchant or competitor or note):
            flash("Add at least a merchant, competitor, or a note.", "error")
            return redirect(url_for('sales_competitor_intel'))
        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        fname = _save_intel_file(request.files.get('attachment'))
        if request.files.get('attachment') and request.files.get('attachment').filename and not fname:
            flash("Attachment must be an image, PDF, Excel or CSV file.", "error")
            return redirect(url_for('sales_competitor_intel'))
        models.create_competitor_intel(
            curr['id'], merchant, request.form.get('product_name', '').strip(), competitor,
            num(request.form.get('competitor_price')), num(request.form.get('our_price')),
            'SAR', note, fname)
        flash("Competitor intel sent to Business Development. Thank you!", "success")
        return redirect(url_for('sales_competitor_intel'))
    return render_template('sales/competitor_intel.html', active_tab='competitor_intel',
                           merchants=models.get_all_merchants(),
                           submissions=models.get_competitor_intel(submitted_by=curr['id']))


@app.route('/intel/<int:intel_id>/file')
@auth.login_required
def intel_file(intel_id):
    """Serve a competitor-intel attachment. Private: the submitting sales manager,
    BD, CCO and admin."""
    intel = models.get_competitor_intel_one(intel_id)
    if not intel or not intel['attachment_file']:
        abort(404)
    curr = auth.get_current_user()
    if curr['role'] not in ('bd', 'cco', 'admin') and curr['id'] != intel['submitted_by']:
        abort(403)
    return send_from_directory(INTEL_DIR, intel['attachment_file'])


# ── BD: Competitor Price Intel inbox (v19) ───────────────────────

@app.route('/bd/intel')
@auth.bd_required
def bd_intel():
    return render_template('bd/intel.html', active_tab='bd_intel',
                           submissions=models.get_competitor_intel())


@app.route('/bd/intel/<int:intel_id>/status', methods=['POST'])
@auth.bd_required
def bd_intel_status(intel_id):
    curr = auth.get_current_user()
    status = request.form.get('status', '')
    if status not in ('new', 'reviewing', 'actioned', 'dismissed'):
        flash("Invalid status.", "error")
        return redirect(url_for('bd_intel'))
    intel = models.update_competitor_intel(intel_id, status, request.form.get('bd_note', ''), curr['id'])
    flash("Intel updated." if intel else "Intel not found.", "info" if intel else "error")
    return redirect(url_for('bd_intel'))


@app.route('/sales/preview/<int:uid>')
@auth.sales_required
def sales_preview_enter(uid):
    curr = auth.get_current_user()
    profile = models.get_reseller_profile(uid)
    if not profile:
        flash("Reseller not found.", "error")
        return redirect(url_for('sales_dashboard'))
    if curr['role'] != 'admin' and profile['registered_by'] != curr['id']:
        flash("Access denied.", "error")
        return redirect(url_for('sales_dashboard'))
    session['preview_user_id'] = uid
    flash(f"Entering portal preview for '{profile['company_name']}'", "info")
    return redirect(url_for('reseller_dashboard'))


@app.route('/sales/preview/exit')
def sales_preview_exit():
    if 'preview_user_id' in session:
        session.pop('preview_user_id')
        flash("Exited preview mode.", "info")
    return redirect(url_for('sales_dashboard'))


# ── CCO Routes ───────────────────────────────────────────────────

@app.route('/cco')
@auth.cco_required
def cco_dashboard():
    pending = models.get_discount_requests('pending')
    history = [d for d in models.get_discount_requests() if d['status'] != 'pending'][:20]
    # enrich with merchant margin economics for decision support
    for d in pending:
        mm = models.get_merchant_avg_margin(d['merchant'])
        margin_rate = (mm['avg_margin_pct'] or 0) / 100.0
        oc_keep_now = 1 - d['current_share_pct'] / 100.0
        oc_keep_req = 1 - d['requested_share_pct'] / 100.0
        d['profit_now'] = d['current_monthly_sales'] * margin_rate * oc_keep_now
        d['profit_after'] = d['projected_monthly_sales'] * margin_rate * oc_keep_req
        d['profit_delta'] = d['profit_after'] - d['profit_now']
        d['merchant_margin_pct'] = mm['avg_margin_pct'] or 0
    return render_template('cco/dashboard.html', active_tab='cco',
                           pending=pending, history=history)


@app.route('/cco/decide/<int:rid>', methods=['POST'])
@auth.cco_required
def cco_decide(rid):
    curr = auth.get_current_user()
    approve = request.form.get('decision') == 'approve'
    note = request.form.get('decision_note', '')
    req = models.decide_discount_request(rid, approve, curr['id'], note)
    if not req:
        flash("Request not found or already decided.", "error")
        return redirect(url_for('cco_dashboard'))
    profile = models.get_reseller_profile_by_id(req['reseller_id'])
    verdict = "approved ✅" if approve else "rejected ❌"
    models.notify([req['requested_by']],
                  f"Discount request {verdict}",
                  f"{req['requested_share_pct']:.0f}% margin share on '{req['merchant']}' for "
                  f"{profile['company_name'] if profile else '?'} was {verdict} by {curr['name']}."
                  + (f" Note: {note}" if note else ""),
                  "/sales/discounts")
    if approve and profile:
        models.notify(profile['user_id'], "Better pricing unlocked 🎉",
                      f"You now get improved pricing on all '{req['merchant']}' products.",
                      "/reseller/merchants")
        flash(f"Approved — override applied automatically for {profile['company_name']} on {req['merchant']}.", "success")
    else:
        flash("Request rejected.", "info")
    return redirect(url_for('cco_dashboard'))


@app.route('/cco/contracts')
@auth.cco_required
def cco_contracts():
    """Signed contracts awaiting activation — the credit-line approval queue."""
    queue = models.contracts_awaiting_activation()
    return render_template('cco/contracts.html', active_tab='cco_contracts',
                           queue=queue, cap=models.AUTO_APPROVE_CAP,
                           account_labels=models.ACCOUNT_TYPE_LABELS)


@app.route('/cco/credit')
@auth.cco_required
def cco_credit():
    """CCO view of additional-credit requests + portfolio exposure."""
    return render_template('cco/credit.html', active_tab='cco_credit',
                           exposure=models.get_credit_exposure(),
                           credit_requests=models.get_pending_credit_requests())


@app.route('/cco/customers')
@auth.cco_required
def cco_customers():
    """CRM tracking board: every registered customer across all sales managers,
    with filters + a dashboard so the CCO can see the whole book at a glance."""
    resellers = models.get_all_resellers()

    # ── filters (all optional, combine with AND) ──
    f_sales = request.args.get('sales', '').strip()
    f_stage = request.args.get('stage', '').strip()
    f_tier = request.args.get('tier', '').strip()
    f_type = request.args.get('account_type', '').strip()
    f_from = request.args.get('from', '').strip()
    f_to = request.args.get('to', '').strip()
    f_q = request.args.get('q', '').strip().lower()

    def keep(r):
        if f_sales and str(r.get('registered_by')) != f_sales:
            return False
        if f_stage and r.get('lifecycle') != f_stage:
            return False
        if f_tier and str(r.get('assigned_tier_id') or '') != f_tier:
            return False
        if f_type and (r.get('account_type') or 'prepaid') != f_type:
            return False
        created = (r.get('created_at') or '')[:10]
        if f_from and created < f_from:
            return False
        if f_to and created > f_to:
            return False
        if f_q:
            hay = ' '.join(str(r.get(k) or '') for k in
                           ('company_name', 'email', 'contact_name', 'contact_phone',
                            'commercial_reg_no')).lower()
            if f_q not in hay:
                return False
        return True

    rows = [r for r in resellers if keep(r)]

    # ── dashboard KPIs on the filtered set ──
    ym = models.datetime.now(models.timezone.utc).strftime('%Y-%m')
    stats = {
        'total': len(rows),
        'by_stage': {}, 'by_type': {}, 'by_tier': {},
        'new_this_month': sum(1 for r in rows if (r.get('created_at') or '')[:7] == ym),
        'with_contract': sum(1 for r in rows if r.get('contract_status') == 'contracted'),
        'with_cr': sum(1 for r in rows if r.get('commercial_reg_no')),
        'exposure': round(sum((r.get('wallet_balance') or 0) + (r.get('credit_outstanding') or 0) for r in rows)),
    }
    for r in rows:
        stats['by_stage'][r.get('lifecycle')] = stats['by_stage'].get(r.get('lifecycle'), 0) + 1
        at = r.get('account_type') or 'prepaid'
        stats['by_type'][at] = stats['by_type'].get(at, 0) + 1
        tn = r.get('tier_name') or 'Unassigned'
        stats['by_tier'][tn] = stats['by_tier'].get(tn, 0) + 1

    return render_template('cco/customers.html', active_tab='cco_customers',
                           rows=rows, stats=stats, total_all=len(resellers),
                           sales_users=models.get_all_users('sales'),
                           tiers=models.get_all_tiers(),
                           account_types=models.ACCOUNT_TYPES,
                           account_labels=models.ACCOUNT_TYPE_LABELS,
                           lifecycle_labels=models.LIFECYCLE_LABELS,
                           filters={'sales': f_sales, 'stage': f_stage, 'tier': f_tier,
                                    'account_type': f_type, 'from': f_from, 'to': f_to, 'q': f_q})


# ── Finance Routes ───────────────────────────────────────────────

@app.route('/finance')
@auth.finance_required
def finance_dashboard():
    pending = models.get_topups('pending')
    history = [t for t in models.get_topups() if t['status'] != 'pending'][:30]
    resellers = models.get_all_resellers()
    total_balance = sum(r['wallet_balance'] or 0 for r in resellers)
    pending_batches = len(models.get_batches(status='awaiting_reconciliation'))
    return render_template('finance/dashboard.html', active_tab='finance',
                           pending=pending, history=history,
                           resellers=resellers, total_balance=total_balance,
                           pending_batches=pending_batches)


@app.route('/finance/review/<int:txn_id>', methods=['POST'])
@auth.finance_required
def finance_review(txn_id):
    curr = auth.get_current_user()
    approve = request.form.get('decision') == 'approve'
    note = request.form.get('note', '')
    txn = models.get_topup(txn_id)
    if not txn or not models.review_topup(txn_id, approve, curr['id'], note):
        flash("Transaction not found or already reviewed.", "error")
        return redirect(url_for('finance_dashboard'))
    if approve:
        models.notify(txn['reseller_user_id'], "Wallet top-up approved 💰",
                      f"Your transfer of {txn['amount']:,.0f} SAR was verified and added to your wallet.",
                      "/reseller/wallet")
        flash(f"Approved — {txn['amount']:,.0f} SAR credited to {txn['company_name']}.", "success")
    else:
        models.notify(txn['reseller_user_id'], "Wallet top-up rejected",
                      f"Your transfer of {txn['amount']:,.0f} SAR could not be verified."
                      + (f" Note: {note}" if note else " Please contact your account manager."),
                      "/reseller/wallet")
        flash("Top-up rejected.", "info")
    return redirect(url_for('finance_dashboard'))


# ── Finance: credit & settlement (v14) ───────────────────────────

@app.route('/finance/credit')
@auth.finance_required
def finance_credit():
    return render_template('finance/credit.html', active_tab='finance_credit',
                           exposure=models.get_credit_exposure(),
                           aging=models.get_credit_aging(),
                           settlements=models.get_pending_settlements(),
                           open_statements=models.get_all_statements('issued')
                                          + models.get_all_statements('overdue'),
                           credit_requests=models.get_pending_credit_requests())


@app.route('/finance/credit/export.csv')
@auth.finance_required
def finance_credit_export():
    """Download the credit/consignment portfolio as CSV (SAR)."""
    import csv, io
    rows = models.get_credit_portfolio()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Reseller', 'Account Type', 'Credit Limit', 'Outstanding', 'Unbilled',
                'Open Billed', 'Overdue Amount', 'Oldest Overdue (days)', 'Frozen',
                'Terms (net days)', 'Billing Cycle'])
    for r in rows:
        w.writerow([r['company_name'], r['account_type'], round(r['credit_limit'] or 0),
                    round(r['credit_outstanding'] or 0), round(r['unbilled']),
                    round(r['open_billed'] or 0), round(r['overdue_amount'] or 0),
                    r['oldest_overdue_days'], 'yes' if r['credit_frozen'] else 'no',
                    r['settlement_terms_days'], r['billing_cycle']])
    from flask import Response
    stamp = models.datetime.now(models.timezone.utc).strftime('%Y%m%d')
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=credit_portfolio_{stamp}.csv'})


@app.route('/finance/settlements/<int:txn_id>/review', methods=['POST'])
@auth.finance_required
def finance_settlement_review(txn_id):
    curr = auth.get_current_user()
    approve = request.form.get('decision') == 'approve'
    note = request.form.get('note', '')
    txn = models.get_topup(txn_id)   # generic wallet_transactions fetch
    if not txn or not models.review_settlement(txn_id, approve, curr['id'], note):
        flash("Settlement not found or already reviewed.", "error")
        return redirect(url_for('finance_credit'))
    verdict = "verified" if approve else "rejected"
    models.notify(txn['reseller_user_id'],
                  f"Statement settlement {verdict}",
                  f"Your settlement of {txn['amount']:,.0f} SAR was {verdict}."
                  + (f" Note: {note}" if note else ""), "/reseller/wallet")
    flash(f"Settlement {verdict} for {txn['company_name']}.", "success")
    return redirect(url_for('finance_credit'))


@app.route('/finance/statements/<int:rid>/issue', methods=['POST'])
@auth.finance_required
def finance_issue_statement(rid):
    curr = auth.get_current_user()
    sid = models.issue_statement(rid, actor_id=curr['id'])
    flash("Statement issued." if sid else "Nothing new to bill for this reseller.", "info")
    return redirect(request.referrer or url_for('finance_credit'))


@app.route('/credit-requests/<int:req_id>/decide', methods=['POST'])
@auth.login_required
def decide_credit_request(req_id):
    """CCO and Finance each sign off on an additional-credit request; the bump
    applies once both approve."""
    curr = auth.get_current_user()
    if curr['role'] not in ('cco', 'finance', 'admin'):
        abort(403)
    approve = request.form.get('decision') == 'approve'
    note = request.form.get('note', '')
    status, cr = models.decide_credit_request(req_id, curr['role'], approve, curr['id'], note)
    if not cr:
        flash("Request not found or already decided.", "error")
    elif status == 'approved':
        flash("Credit increase approved and applied.", "success")
    elif status == 'rejected':
        flash("Credit request rejected.", "info")
    else:
        flash("Your approval was recorded — awaiting the other sign-off.", "success")
    return redirect(request.referrer or url_for('finance_credit'))


# ── Operations Routes (v5) ───────────────────────────────────────

@app.route('/ops')
@auth.ops_required
def ops_dashboard():
    stats = models.get_ops_stats()
    recent = models.get_price_log(limit=12)
    return render_template('ops/dashboard.html', active_tab='ops_dashboard',
                           stats=stats, recent=recent)


@app.route('/ops/forecasts')
@auth.ops_required
def ops_forecasts():
    """Resellers' purchase forecasts, so Operations can plan stock ahead.
    Values are shown in SAR (the internal base currency)."""
    demand = models.get_forecast_demand_summary(days=90)
    forecasts = models.get_all_forecasts(limit=100)
    return render_template('ops/forecasts.html', active_tab='ops_forecasts',
                           demand=demand, forecasts=forecasts)


@app.route('/ops/consignment')
@auth.ops_required
def ops_consignment():
    """Credit/consignment accounts draw in real time (often via API) instead of
    ordering stock up front — Operations watches their live draw-down here to
    anticipate restock. All amounts SAR."""
    activity = models.get_consignment_activity()
    return render_template('ops/consignment.html', active_tab='ops_consignment',
                           activity=activity, exposure=models.get_credit_exposure(),
                           account_labels=models.ACCOUNT_TYPE_LABELS)


@app.route('/ops/products')
@auth.ops_required
def ops_products():
    products = models.get_products(include_inactive=True)
    return render_template('ops/products.html', active_tab='ops_products',
                           products=products, products_json=jdump(products))


@app.route('/ops/products/add', methods=['GET', 'POST'])
@auth.ops_required
def ops_add_product():
    curr = auth.get_current_user()
    if request.method == 'POST':
        data = {k: request.form.get(k, '') for k in
                ('product_id', 'product_name', 'merchant', 'merchant_id', 'category',
                 'country', 'region', 'currency', 'cost', 'default_price', 'face_value')}
        if not data['product_name'] or not data['merchant']:
            flash("Product name and merchant are required.", "error")
        else:
            pid = models.add_product(data, curr['id'])
            flash(f"Product added (#{pid}). It appears as a New Arrival for 30 days.", "success")
            return redirect(url_for('ops_products'))
    return render_template('ops/product_form.html', active_tab='ops_products',
                           product=None, categories=models.get_form_categories(),
                           countries=models.get_all_countries_full(),
                           regions=models.get_all_regions_full(),
                           currencies=models.get_all_currencies(),
                           merchants=[m['merchant'] for m in models.get_all_merchants()])


@app.route('/ops/products/<int:pid>/edit', methods=['GET', 'POST'])
@auth.ops_required
def ops_edit_product(pid):
    curr = auth.get_current_user()
    prods = models.get_products(include_inactive=True)
    product = next((p for p in prods if p['id'] == pid), None)
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for('ops_products'))
    if request.method == 'POST':
        fields = {k: request.form.get(k) for k in
                  ('product_name', 'merchant', 'category', 'country', 'region', 'currency',
                   'cost', 'default_price', 'face_value')}
        fields['is_new'] = 1 if request.form.get('is_new') else 0
        changes = models.update_product(pid, fields, curr['id'])
        if changes:
            flash(f"Product updated — {len(changes)} field(s) changed and logged.", "success")
        else:
            flash("No changes detected.", "info")
        return redirect(url_for('ops_products'))
    return render_template('ops/product_form.html', active_tab='ops_products',
                           product=product, categories=models.get_form_categories(),
                           countries=models.get_all_countries_full(),
                           regions=models.get_all_regions_full(),
                           currencies=models.get_all_currencies(),
                           merchants=[m['merchant'] for m in models.get_all_merchants()])


@app.route('/ops/products/<int:pid>/toggle', methods=['POST'])
@auth.ops_required
def ops_toggle_product(pid):
    curr = auth.get_current_user()
    active = request.form.get('active') == '1'
    models.set_product_active(pid, active, curr['id'])
    flash(f"Product {'activated' if active else 'deactivated'} — "
          f"{'now visible' if active else 'hidden'} in all catalogues.", "success")
    return redirect(request.referrer or url_for('ops_products'))


@app.route('/ops/bulk', methods=['GET', 'POST'])
@auth.ops_required
def ops_bulk():
    preview, token = None, None
    if request.method == 'POST':
        file = request.files.get('pricefile')
        if not file or not file.filename:
            flash("Choose a price file first.", "error")
        else:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in ALLOWED_PRICEFILE_EXT:
                flash("Price file must be .xls or .xlsx", "error")
            else:
                token = f"pf_{uuid.uuid4().hex[:12]}{ext}"
                path = os.path.join(PRICEFILE_DIR, token)
                file.save(path)
                preview, err = models.parse_price_file(path)
                if err:
                    flash(err, "error")
                    preview, token = None, None
                elif not preview['diffs']:
                    flash(f"File parsed ({preview['total_rows']} rows) — no price differences found.", "info")
                    preview, token = None, None
    return render_template('ops/bulk.html', active_tab='ops_bulk',
                           preview=preview, token=token)


@app.route('/ops/bulk/apply', methods=['POST'])
@auth.ops_required
def ops_bulk_apply():
    curr = auth.get_current_user()
    token = request.form.get('token', '')
    path = os.path.join(PRICEFILE_DIR, os.path.basename(token))
    if not token or not os.path.exists(path):
        flash("Upload session expired — please upload the file again.", "error")
        return redirect(url_for('ops_bulk'))
    count, err = models.apply_price_file(path, curr['id'])
    os.remove(path)
    if err:
        flash(err, "error")
    else:
        flash(f"Bulk update applied — {count} products repriced. All changes are in the Price Log.", "success")
    return redirect(url_for('ops_pricelog'))


@app.route('/ops/suppliers', methods=['GET', 'POST'])
@auth.ops_required
def ops_suppliers():
    if request.method == 'POST':
        sid = request.form.get('supplier_id') or None
        merchants = [m.strip() for m in request.form.getlist('merchants') if m.strip()]
        models.upsert_supplier(int(sid) if sid else None,
                               request.form.get('name', '').strip(),
                               request.form.get('contact_person', ''),
                               request.form.get('email', ''),
                               request.form.get('phone', ''),
                               request.form.get('payment_terms', ''),
                               request.form.get('notes', ''), merchants)
        flash("Supplier saved.", "success")
        return redirect(url_for('ops_suppliers'))
    suppliers = models.get_suppliers()
    all_merchants = [m['merchant'] for m in models.get_all_merchants()]
    return render_template('ops/suppliers.html', active_tab='ops_suppliers',
                           suppliers=suppliers, all_merchants=all_merchants)


@app.route('/ops/suppliers/<int:sid>/delete', methods=['POST'])
@auth.ops_required
def ops_delete_supplier(sid):
    models.delete_supplier(sid)
    flash("Supplier deleted.", "info")
    return redirect(url_for('ops_suppliers'))


@app.route('/ops/pricelog')
@auth.ops_required
def ops_pricelog():
    log = models.get_price_log(limit=300)
    return render_template('ops/pricelog.html', active_tab='ops_pricelog', log=log)


# ── Ops: Multi-Supplier Sourcing (v6) ────────────────────────────

@app.route('/ops/sourcing')
@auth.ops_required
def ops_sourcing():
    search = request.args.get('q', '').strip()
    merchant = request.args.get('merchant', '').strip()
    only_multi = request.args.get('multi') == '1'
    matrix = models.get_sourcing_matrix(search or None, merchant or None, only_multi)
    merchant_summary = models.get_merchant_sourcing_summary()[:10]
    suppliers = models.get_suppliers()
    return render_template('ops/sourcing.html', active_tab='ops_sourcing',
                           matrix=matrix, matrix_json=jdump(matrix),
                           merchant_summary=merchant_summary,
                           suppliers=suppliers,
                           suppliers_json=jdump([{'id': s['id'], 'name': s['name']} for s in suppliers]),
                           merchants=[m['merchant'] for m in models.get_all_merchants()],
                           q=search, sel_merchant=merchant, only_multi=only_multi)


@app.route('/ops/sourcing/price', methods=['POST'])
@auth.ops_required
def ops_sourcing_price():
    curr = auth.get_current_user()
    sid = int(request.form.get('supplier_id'))
    pid = int(request.form.get('product_rowid'))
    cost = float(request.form.get('cost') or 0)
    if cost <= 0:
        flash("Enter a valid cost.", "error")
    else:
        changed = models.upsert_supplier_price(sid, pid, cost, source='manual', changed_by=curr['id'])
        flash("Offer saved." if changed else "Price unchanged.", "success" if changed else "info")
    return redirect(request.referrer or url_for('ops_sourcing'))


@app.route('/ops/sourcing/availability', methods=['POST'])
@auth.ops_required
def ops_sourcing_availability():
    models.set_offer_availability(int(request.form.get('supplier_id')),
                                  int(request.form.get('product_rowid')),
                                  request.form.get('available') == '1')
    flash("Offer availability updated.", "info")
    return redirect(request.referrer or url_for('ops_sourcing'))


@app.route('/ops/suppliers/<int:sid>/prices/upload', methods=['POST'])
@auth.ops_required
def ops_supplier_price_upload(sid):
    curr = auth.get_current_user()
    file = request.files.get('pricefile')
    if not file or not file.filename:
        flash("Choose a price file.", "error")
        return redirect(url_for('ops_suppliers'))
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_PRICEFILE_EXT:
        flash("Price file must be .xls or .xlsx", "error")
        return redirect(url_for('ops_suppliers'))
    path = os.path.join(PRICEFILE_DIR, f"sp_{uuid.uuid4().hex[:10]}{ext}")
    file.save(path)
    summary, err = models.bulk_import_supplier_prices(sid, path, curr['id'])
    os.remove(path)
    if err:
        flash(err, "error")
    else:
        flash(f"Supplier price list imported — {summary['updated']} updated, "
              f"{summary['unchanged']} unchanged, {summary['unmatched']} unmatched.", "success")
    return redirect(url_for('ops_sourcing'))


@app.route('/ops/suppliers/<int:sid>/apikey', methods=['POST'])
@auth.ops_required
def ops_supplier_apikey(sid):
    key = f"oc_{uuid.uuid4().hex}"
    models.set_supplier_api_key(sid, key)
    flash(f"API key generated for automated price sync: {key}", "success")
    return redirect(url_for('ops_suppliers'))


# ═════════════════ Integration API v1 (v9) ══════════════════════
# Machine-to-machine endpoints. Auth: X-API-Key header (or Bearer token)
# holding the reseller's key. CSRF-exempt by /api/ prefix. Full contract
# in API_GUIDE.md.

def api_error(code, message, http=400):
    return {'error': {'code': code, 'message': message}}, http


def require_api_reseller():
    key = (request.headers.get('X-API-Key')
           or (request.headers.get('Authorization') or '').replace('Bearer ', '').strip())
    return models.get_reseller_by_api_key(key)


@app.route('/api/v1/ping')
def api_ping():
    profile = require_api_reseller()
    if not profile:
        return api_error('unauthorized', 'Missing or invalid API key.', 401)
    return {'ok': True, 'company': profile['company_name'],
            'contract_status': profile['contract_status']}


@app.route('/api/v1/catalogue')
def api_catalogue():
    profile = require_api_reseller()
    if not profile:
        return api_error('unauthorized', 'Missing or invalid API key.', 401)
    enriched = models.enrich_products_for_reseller(profile)

    def fparam(name):
        v = request.args.get(name, '').strip()
        return v or None
    merchant, category = fparam('merchant'), fparam('category')
    country, region, currency = fparam('country'), fparam('region'), fparam('currency')
    search = (fparam('search') or '').lower()

    rows = [p for p in enriched if
            (not merchant or p['merchant'] == merchant) and
            (not category or p['category'] == category) and
            (not country or p['country'] == country) and
            (not region or p['region'] == region) and
            (not currency or p['currency'] == currency) and
            (not search or search in p['product_name'].lower()
             or search in p['merchant'].lower())]

    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size = min(500, max(1, int(request.args.get('page_size', 100))))
    except ValueError:
        return api_error('bad_request', 'page and page_size must be integers.')
    total = len(rows)
    chunk = rows[(page - 1) * page_size: page * page_size]

    items = [{'id': p['id'], 'sku': p['product_id'], 'name': p['product_name'],
              'merchant': p['merchant'], 'category': p['category'],
              'country': p['country'], 'region': p['region'],
              'currency': p['currency'], 'face_value': p['face_value'],
              'your_price': p['client_price'], 'your_discount': p['discount'],
              'margin_pct': p['margin_pct'], 'is_new': bool(p.get('is_new')),
              'special_rate': bool(p.get('has_override'))} for p in chunk]
    return {'ok': True, 'page': page, 'page_size': page_size, 'total': total,
            'items': items}


@app.route('/api/v1/wallet')
def api_wallet():
    profile = require_api_reseller()
    if not profile:
        return api_error('unauthorized', 'Missing or invalid API key.', 401)
    txns = models.get_wallet_transactions(profile['id'], limit=20)
    return {'ok': True, 'currency': 'SAR',
            'balance': round(profile['wallet_balance'], 2),
            'recent_transactions': [{'type': t['type'], 'amount': t['amount'],
                                     'status': t['status'], 'at': t['created_at']}
                                    for t in txns]}


@app.route('/api/v1/account')
def api_account():
    """Account model + live spending headroom (SAR base). For credit/consignment
    clients (e.g. banks pulling card-by-card) this is how much they can still
    draw before settling — the API mirror of available_to_spend()."""
    profile = require_api_reseller()
    if not profile:
        return api_error('unauthorized', 'Missing or invalid API key.', 401)
    at = profile['account_type']
    resp = {'ok': True, 'currency': 'SAR', 'account_type': at,
            'contract_status': profile['contract_status'],
            'available_to_spend': profile['available_to_spend']}
    if at == 'prepaid':
        resp['wallet_balance'] = round(profile['wallet_balance'], 2)
    else:
        resp.update({
            'credit_limit': round(profile['credit_limit'], 2),
            'credit_outstanding': round(profile['credit_outstanding'], 2),
            'unbilled': models.unbilled_amount(profile['id']),
            'frozen': bool(profile['credit_frozen']),
            'settlement_terms_days': profile['settlement_terms_days'],
            'billing_cycle': profile['billing_cycle'],
        })
    return resp


@app.route('/api/v1/statements')
def api_statements():
    """Statements (invoices) for a credit/consignment client, plus the current
    un-billed drawn amount."""
    profile = require_api_reseller()
    if not profile:
        return api_error('unauthorized', 'Missing or invalid API key.', 401)
    statements = models.get_statements(profile['id'])
    return {'ok': True, 'currency': 'SAR',
            'unbilled': models.unbilled_amount(profile['id']),
            'statements': [{'statement_id': s['id'], 'amount': s['amount'],
                            'status': s['status'], 'period_start': s['period_start'],
                            'period_end': s['period_end'], 'due_at': s['due_at'],
                            'paid_at': s['paid_at']} for s in statements]}


def _api_order_payload(order):
    items = models.get_order_items(order['id'])
    codes = models.get_all_codes_for_order(order['id'])
    return {'order_id': order['id'], 'status': order['status'],
            'created_at': order['created_at'],
            'total_sar': order['total_cost'],
            'items': [{'line_id': it['id'], 'product_id': it['product_rowid'],
                       'name': it['product_name'], 'merchant': it['merchant'],
                       'quantity': it['quantity'],
                       'unit_price': it['unit_price'], 'currency': it['currency'],
                       'line_total_sar': it.get('line_total_sar'),
                       'fulfillment_status': it.get('fulfillment_status', 'external'),
                       'codes': [{'code': cd['code'], 'pin': cd.get('pin')}
                                 for cd in codes.get(it['id'], [])]}
                      for it in items]}


@app.route('/api/v1/orders', methods=['GET', 'POST'])
def api_orders():
    profile = require_api_reseller()
    if not profile:
        return api_error('unauthorized', 'Missing or invalid API key.', 401)

    if request.method == 'GET':
        orders = models.get_orders(profile['id'])[:50]
        return {'ok': True,
                'orders': [{'order_id': o['id'], 'status': o['status'],
                            'created_at': o['created_at'], 'total_sar': o['total_cost'],
                            'item_count': o['item_count']} for o in orders]}

    # ── POST: create order ──
    if profile['contract_status'] != 'contracted':
        return api_error('contract_required',
                         'Ordering unlocks after your contract is signed.', 403)
    body = request.get_json(silent=True) or {}
    idem = (body.get('idempotency_key') or request.headers.get('Idempotency-Key') or '').strip()
    if idem:
        existing = models.idempotency_lookup(idem, profile['id'])
        if existing:
            order = next((o for o in models.get_orders(profile['id']) if o['id'] == existing), None)
            if order:
                resp = _api_order_payload(order)
                resp.update({'ok': True, 'idempotent_replay': True})
                return resp

    raw_items = body.get('items') or []
    if not isinstance(raw_items, list) or not raw_items:
        return api_error('bad_request', "Body must include a non-empty 'items' array.")

    enriched = models.enrich_products_for_reseller(profile)
    by_id = {p['id']: p for p in enriched}
    by_sku = {str(p['product_id']): p for p in enriched if p['product_id']}
    order_items = []
    for it in raw_items:
        p = by_id.get(it.get('id')) or by_sku.get(str(it.get('sku', '')).strip())
        try:
            qty = int(it.get('quantity') or 0)
        except (TypeError, ValueError):
            qty = 0
        if not p:
            return api_error('unknown_product',
                             f"Product not found: {it.get('id') or it.get('sku')}", 422)
        if qty <= 0:
            return api_error('bad_request', f"Invalid quantity for '{p['product_name']}'.")
        order_items.append({'product_rowid': p['id'], 'product_name': p['product_name'],
                            'merchant': p['merchant'], 'category': p['category'],
                            'currency': p['currency'], 'quantity': qty,
                            'unit_price': p['client_price'], 'unit_face': p['face_value']})

    oid, err = models.create_order(profile['id'], order_items)
    if err:
        el = err.lower()
        code = ('insufficient_balance' if 'wallet' in el
                else 'insufficient_stock' if 'codes left' in err
                else 'account_on_hold' if 'hold' in el
                else 'credit_limit_reached' if 'available right now' in el
                else 'order_rejected')
        return api_error(code, err, 409)
    if idem:
        models.idempotency_store(idem, profile['id'], oid)
    order = next(o for o in models.get_orders(profile['id']) if o['id'] == oid)
    resp = _api_order_payload(order)
    resp['ok'] = True
    return resp, 201


@app.route('/api/v1/orders/<int:oid>')
def api_order_detail(oid):
    profile = require_api_reseller()
    if not profile:
        return api_error('unauthorized', 'Missing or invalid API key.', 401)
    order = next((o for o in models.get_orders(profile['id']) if o['id'] == oid), None)
    if not order:
        return api_error('not_found', 'Order not found.', 404)
    resp = _api_order_payload(order)
    resp['ok'] = True
    return resp


@app.route('/api/v1/orders/<int:oid>/codes')
def api_order_codes(oid):
    profile = require_api_reseller()
    if not profile:
        return api_error('unauthorized', 'Missing or invalid API key.', 401)
    order = next((o for o in models.get_orders(profile['id']) if o['id'] == oid), None)
    if not order:
        return api_error('not_found', 'Order not found.', 404)
    codes = models.get_all_codes_for_order(oid)
    return {'ok': True, 'order_id': oid,
            'codes': [{'line_id': line, 'code': c['code'], 'pin': c.get('pin')}
                      for line, arr in codes.items() for c in arr]}


@app.route('/api/supplier-prices', methods=['POST'])
def api_supplier_prices():
    """INTEGRATION ENDPOINT — suppliers (or the company middleware) push price updates.
    Body: {"api_key": "...", "items": [{"product_id": "6017", "cost": 95.5}, ...]}"""
    data = request.get_json(silent=True) or {}
    supplier = models.get_supplier_by_api_key(data.get('api_key', ''))
    if not supplier:
        return {'ok': False, 'error': 'invalid api_key'}, 401
    conn = models.get_db()
    id_map = {str(r['product_id']): r['id'] for r in conn.execute("SELECT id, product_id FROM products")}
    conn.close()
    updated = unmatched = 0
    for it in data.get('items', []):
        pid = id_map.get(str(it.get('product_id', '')).strip())
        try:
            cost = float(it.get('cost'))
        except (TypeError, ValueError):
            continue
        if pid and cost > 0:
            if models.upsert_supplier_price(supplier['id'], pid, cost, source='api'):
                updated += 1
        else:
            unmatched += 1
    return {'ok': True, 'supplier': supplier['name'], 'updated': updated, 'unmatched': unmatched}


@app.route('/ops/batches', methods=['GET', 'POST'])
@auth.ops_required
def ops_batches():
    curr = auth.get_current_user()
    if request.method == 'POST':
        sid = int(request.form.get('supplier_id') or 0)
        pid = int(request.form.get('product_rowid') or 0)
        qty = int(request.form.get('quantity') or 0)
        unit_cost = float(request.form.get('unit_cost') or 0)
        invoice = request.form.get('invoice_ref', '').strip()
        reason = request.form.get('reason', '').strip()
        if not sid or not pid or qty <= 0 or unit_cost <= 0:
            flash("Supplier, product, quantity and unit cost are required.", "error")
        else:
            bid, err = models.create_batch(sid, pid, qty, unit_cost, invoice, reason, curr['id'])
            if err:
                flash(err, "error")
            else:
                flash(f"Batch #{bid} recorded — sent to Finance for reconciliation.", "success")
                return redirect(url_for('ops_batches'))

    batches = models.get_batches()
    suppliers = models.get_suppliers()
    products = models.get_products(include_inactive=False)
    products_json = jdump([{'id': p['id'], 'product_name': p['product_name'],
                                 'merchant': p['merchant'], 'cost': p['cost'],
                                 'currency': p['currency']} for p in products])
    # per-product best offers for the live warning
    offers = {}
    for row in models.get_sourcing_matrix():
        if row['best_cost'] is not None:
            offers[row['id']] = {'best_cost': row['best_cost'], 'best_supplier': row['best_supplier']}
    return render_template('ops/batches.html', active_tab='ops_batches',
                           batches=batches, suppliers=suppliers,
                           products_json=products_json, offers_json=jdump(offers))


# ── Ops: Issuing Hub (v8) — we issue & sell partner gift cards ───

@app.route('/ops/issuing', methods=['GET', 'POST'])
@auth.ops_required
def ops_issuing():
    curr = auth.get_current_user()
    if request.method == 'POST':
        pid = request.form.get('partner_id') or None
        name = request.form.get('name', '').strip()
        if not name:
            flash("Partner name is required.", "error")
        else:
            models.upsert_issuing_partner(int(pid) if pid else None, name,
                                          request.form.get('contact_person', ''),
                                          request.form.get('email', ''),
                                          request.form.get('phone', ''),
                                          float(request.form.get('share_pct') or 80),
                                          request.form.get('status', 'active'),
                                          request.form.get('notes', ''), curr['id'])
            flash(f"Partner '{name}' saved.", "success")
            return redirect(url_for('ops_issuing'))
    partners = models.get_issuing_partners()
    report = models.get_partner_report()
    totals = {
        'partners': len(partners),
        'products': sum(p['product_count'] for p in partners),
        'stock': sum(p['stock_available'] for p in partners),
        'sold': sum(p['sold_total'] for p in partners),
        'profit': sum(r['profit_sar'] for r in report),
    }
    return render_template('ops/issuing.html', active_tab='ops_issuing',
                           partners=partners, totals=totals)


@app.route('/ops/issuing/products', methods=['GET', 'POST'])
@auth.ops_required
def ops_issuing_products():
    curr = auth.get_current_user()
    if request.method == 'POST':
        try:
            prow, err = models.create_issued_product(
                int(request.form.get('partner_id')),
                request.form.get('product_name', '').strip(),
                request.form.get('sku', '').strip(),
                float(request.form.get('face_value') or 0),
                float(request.form.get('selling_price') or 0),
                request.form.get('currency', 'SAR').strip() or 'SAR',
                request.form.get('category', 'Gift Cards & Vouchers'),
                request.form.get('country', 'Saudi Arabia'),
                curr['id'])
        except (TypeError, ValueError):
            prow, err = None, "Fill all fields with valid values."
        if err:
            flash(err, "error")
        else:
            flash("Issued product created — it is now live in every reseller catalogue. "
                  "Generate a code batch so it can actually sell.", "success")
            return redirect(url_for('ops_issuing_products'))
    products = models.get_issued_products()
    partners = models.get_issuing_partners()
    return render_template('ops/issuing_products.html', active_tab='ops_issuing',
                           products=products, partners=partners,
                           low_threshold=models.ISSUED_LOW_STOCK_THRESHOLD)


@app.route('/ops/issuing/batch', methods=['POST'])
@auth.ops_required
def ops_issuing_batch():
    curr = auth.get_current_user()
    prow = int(request.form.get('product_rowid') or 0)
    qty = int(request.form.get('quantity') or 0)
    if not prow or qty <= 0 or qty > 100000:
        flash("Enter a valid quantity (1 – 100,000).", "error")
        return redirect(url_for('ops_issuing_products'))
    result, err = models.generate_voucher_batch(prow, qty, curr['id'],
                                                request.form.get('note', ''))
    if err:
        flash(err, "error")
    else:
        flash(f"Batch {result['batch_ref']} generated — {qty:,} unique codes added to stock.", "success")
    return redirect(url_for('ops_issuing_products'))


@app.route('/ops/issuing/checker', methods=['GET', 'POST'])
@auth.ops_required
def ops_issuing_checker():
    result = None
    code = ''
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        action = request.form.get('action')
        if code and rate_limited('redeem', code, REDEEM_MAX_TRIES, REDEEM_WINDOW_SEC):
            flash("Too many attempts on this code — try again in 15 minutes.", "error")
            return render_template('ops/issuing_checker.html', active_tab='ops_issuing',
                                   result=None, code=code), 429
        if action == 'redeem' and code:
            ok, msg = models.redeem_voucher(code)
            flash(msg, "success" if ok else "error")
            if not ok:
                record_attempt('redeem', code)
        result = models.check_voucher(code) if code else None
        if code and not result:
            flash("Code not found in the system.", "error")
    return render_template('ops/issuing_checker.html', active_tab='ops_issuing',
                           result=result, code=code)


# ── Partner Portal (v8.1) — the business we issue cards FOR ─────

def _current_partner():
    curr = auth.get_current_user()
    return models.get_partner_by_user(curr['id']) if curr else None


@app.route('/partner')
@auth.partner_required
def partner_dashboard():
    partner = _current_partner()
    if not partner:
        flash("No issuing-partner record is linked to this account.", "error")
        return _no_reseller_profile()
    programs, totals, recent = models.get_partner_stats(partner['id'])
    return render_template('partner/dashboard.html', active_tab='partner_dashboard',
                           partner=partner, programs=programs, totals=totals, recent=recent)


@app.route('/partner/redeem', methods=['GET', 'POST'])
@auth.partner_required
def partner_redeem():
    partner = _current_partner()
    if not partner:
        return _no_reseller_profile()
    result = None
    code = ''
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        pin = request.form.get('pin', '').strip()
        action = request.form.get('action')
        if code and rate_limited('redeem', code, REDEEM_MAX_TRIES, REDEEM_WINDOW_SEC):
            flash("Too many attempts on this code — try again in 15 minutes.", "error")
            return render_template('partner/redeem.html', active_tab='partner_redeem',
                                   partner=partner, result=None, code=code), 429
        if action == 'redeem':
            ok, msg = models.partner_redeem(partner['id'], code, pin)
            flash(msg, "success" if ok else "error")
            if not ok:
                record_attempt('redeem', code)
        result, err = models.partner_check_voucher(partner['id'], code) if code else (None, None)
        if code and err and action != 'redeem':
            flash(err, "error")
    return render_template('partner/redeem.html', active_tab='partner_redeem',
                           partner=partner, result=result, code=code)


@app.route('/partner/statement')
@auth.partner_required
def partner_statement():
    partner = _current_partner()
    if not partner:
        return _no_reseller_profile()
    statement = models.get_partner_statement(partner['id'])
    totals = {
        'units': sum(r['units'] for r in statement),
        'gross': sum(r['gross_sar'] for r in statement),
        'payout': sum(r['payout_sar'] for r in statement),
    }
    return render_template('partner/statement.html', active_tab='partner_statement',
                           partner=partner, statement=statement, totals=totals)


@app.route('/ops/issuing/<int:pid>/login', methods=['POST'])
@auth.ops_required
def ops_partner_login(pid):
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')
    contact = request.form.get('contact_name', '').strip() or 'Partner User'
    if not email or len(password) < 6:
        flash("Email and a password of 6+ characters are required.", "error")
        return redirect(url_for('ops_issuing'))
    uid = models.create_partner_login(pid, email, password, contact)
    if uid:
        flash(f"Portal login created — the partner signs in at /login with {email}.", "success")
    else:
        flash("That email is already in use.", "error")
    return redirect(url_for('ops_issuing'))


# ── Finance: Batch Reconciliation (v6) ───────────────────────────

@app.route('/finance/batches')
@auth.finance_required
def finance_batches():
    pending = models.get_batches(status='awaiting_reconciliation')
    history = [b for b in models.get_batches() if b['status'] != 'awaiting_reconciliation'][:30]
    return render_template('finance/batches.html', active_tab='finance_batches',
                           pending=pending, history=history)


@app.route('/finance/batches/<int:bid>/review', methods=['POST'])
@auth.finance_required
def finance_batch_review(bid):
    curr = auth.get_current_user()
    ok = request.form.get('decision') == 'reconcile'
    note = request.form.get('note', '')
    b = models.reconcile_batch(bid, ok, curr['id'], note)
    if not b:
        flash("Batch not found or already reviewed.", "error")
    elif ok:
        flash(f"Batch #{bid} reconciled against invoice.", "success")
    else:
        flash(f"Batch #{bid} marked as disputed — Ops notified.", "warning")
    return redirect(url_for('finance_batches'))


# ── BD: Deal Pipeline (v8) ───────────────────────────────────────

@app.route('/bd')
@auth.bd_required
def bd_dashboard():
    curr = auth.get_current_user()
    mine = models.get_bd_requests(created_by=curr['id'] if curr['role'] == 'bd' else None)
    kpis = models.get_sourcing_kpis()
    counts = {'submitted': 0, 'in_progress': 0, 'done': 0, 'rejected': 0}
    for d in mine:
        counts[d['status']] = counts.get(d['status'], 0) + 1
    return render_template('bd/dashboard.html', active_tab='bd_dashboard',
                           deals=mine[:8], counts=counts, kpis=kpis)


@app.route('/deals', methods=['GET', 'POST'])
@auth.deals_required
def deals():
    curr = auth.get_current_user()
    if request.method == 'POST':
        # Only BD (and admin/cco) submit deals; Ops act on them
        if curr['role'] == 'ops':
            flash("Ops execute deals — Business Development submits them.", "error")
            return redirect(url_for('deals'))
        dtype = request.form.get('type')
        title = request.form.get('title', '').strip()
        if dtype not in models.BD_DEAL_TYPES or not title:
            flash("Deal type and title are required.", "error")
        else:
            models.create_bd_request(dtype, title,
                                     request.form.get('merchant', '').strip(),
                                     request.form.get('supplier_name', '').strip(),
                                     request.form.get('details', ''),
                                     request.form.get('expected_terms', ''),
                                     curr['id'])
            flash("Deal submitted — Operations were notified to enter the data.", "success")
            return redirect(url_for('deals'))

    scope_mine = curr['id'] if curr['role'] == 'bd' else None
    all_deals = models.get_bd_requests(created_by=scope_mine)
    return render_template('deals.html', active_tab='deals',
                           deals=all_deals, deal_types=models.BD_DEAL_TYPES,
                           can_create=curr['role'] in ('bd', 'admin', 'cco'),
                           can_handle=curr['role'] in ('ops', 'admin', 'cco'))


@app.route('/deals/<int:rid>/status', methods=['POST'])
@auth.deals_required
def deal_status(rid):
    curr = auth.get_current_user()
    if curr['role'] == 'bd':
        flash("Only Operations update deal status.", "error")
        return redirect(url_for('deals'))
    status = request.form.get('status')
    if status not in ('in_progress', 'done', 'rejected'):
        flash("Invalid status.", "error")
        return redirect(url_for('deals'))
    req = models.update_bd_request_status(rid, status, curr['id'],
                                          request.form.get('note', ''))
    if req:
        flash(f"Deal '{req['title']}' marked {status.replace('_', ' ')} — BD notified.", "success")
    return redirect(url_for('deals'))


# ── Finance: FX Rates (v7 hardening) ─────────────────────────────

@app.route('/finance/rates', methods=['GET', 'POST'])
@auth.finance_required
def finance_rates():
    curr = auth.get_current_user()
    if request.method == 'POST':
        currencies = request.form.getlist('currency')
        rates = request.form.getlist('rate')
        updated = 0
        for c, r in zip(currencies, rates):
            try:
                val = float(r)
            except (TypeError, ValueError):
                continue
            if val > 0:
                models.set_fx_rate(c.strip(), val, curr['id'])
                updated += 1
        flash(f"FX rates saved ({updated} currencies). All new orders convert at these rates.", "success")
        return redirect(url_for('finance_rates'))
    return render_template('finance/rates.html', active_tab='finance_rates',
                           rates=models.get_fx_rates_full())


# ── BD / CCO: Sourcing Intelligence (v6) ─────────────────────────

@app.route('/sourcing-intel')
@auth.bd_required          # BD works on margin improvement — needs this view too
def sourcing_intel():
    ym = request.args.get('ym') or datetime.now(timezone.utc).strftime('%Y-%m')
    kpis = models.get_sourcing_kpis()
    sales_by_supplier = models.get_sales_by_supplier(ym)
    sales_all_time = models.get_sales_by_supplier(None)
    scorecards = models.get_supplier_scorecards()
    rates = models.get_fx_rates()
    units30 = models.get_units_sold_30d()
    opportunities = [r for r in models.get_sourcing_matrix() if r['saving_vs_std'] > 0]
    # v8: turn raw savings into decision-ready numbers — estimated SAR/month
    for o in opportunities:
        sold = units30.get(o['id'], 0)
        rate = rates.get(o['currency'] or 'SAR', 1)
        o['units_30d'] = sold
        o['est_monthly_saving_sar'] = round(o['saving_vs_std'] * sold * rate, 0)
        o['saving_sar_unit'] = round(o['saving_vs_std'] * rate, 2)
    opportunities.sort(key=lambda x: (-x['est_monthly_saving_sar'], -x['saving_sar_unit']))
    # Recommended actions = opportunities with actual recent sales (money now, not theory)
    actions = [o for o in opportunities if o['est_monthly_saving_sar'] > 0][:5]
    improvements = models.get_margin_improvements()
    variance_batches = [b for b in models.get_batches() if b['sourcing_variance'] > 0][:15]
    return render_template('sourcing_intel.html', active_tab='sourcing_intel',
                           kpis=kpis, ym=ym,
                           sales_by_supplier=sales_by_supplier,
                           sales_all_time=sales_all_time,
                           scorecards=scorecards,
                           opportunities=opportunities[:15],
                           actions=actions,
                           improvements=improvements,
                           variance_batches=variance_batches)


# ── Governance: Team Performance (v5) ────────────────────────────

@app.route('/team')
@auth.cco_required
def team_performance():
    ym = request.args.get('ym') or datetime.now(timezone.utc).strftime('%Y-%m')
    team = models.get_team_performance(ym)
    # month options: last 12 months
    months = []
    y, m = datetime.now(timezone.utc).year, datetime.now(timezone.utc).month
    for _ in range(12):
        months.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return render_template('team.html', active_tab='team', team=team, ym=ym, months=months)


@app.route('/team/targets', methods=['POST'])
@auth.cco_required
def team_set_targets():
    ym = request.form.get('ym')
    uids = request.form.getlist('sales_user_id')
    news = request.form.getlist('target_new')
    values = request.form.getlist('target_value')
    for i, uid in enumerate(uids):
        models.upsert_sales_target(int(uid), ym,
                                   int(news[i] or 0), float(values[i] or 0))
    flash(f"Targets saved for {ym}.", "success")
    return redirect(url_for('team_performance', ym=ym))


@app.route('/sales/scorecard')
@auth.sales_required
def sales_scorecard():
    curr = auth.get_current_user()
    ym = request.args.get('ym') or datetime.now(timezone.utc).strftime('%Y-%m')
    card = models.get_sales_scorecard(curr['id'], ym)
    resellers = models.get_all_resellers(registered_by=curr['id'])
    return render_template('sales/scorecard.html', active_tab='scorecard',
                           card=card, ym=ym, resellers=resellers)


# ── Reseller Portal Routes ────────────────────────────────────────

def _no_reseller_profile():
    """Staff hitting reseller-only pages must NOT be logged out — send them home.
    Only an actual reseller with a broken profile is signed out."""
    curr = auth.get_current_user()
    if curr and curr['role'] != 'reseller':
        flash("That page belongs to the reseller portal.", "warning")
        return redirect(url_for(ROLE_HOME.get(curr['role'], 'login')))
    return _no_reseller_profile()


def _reseller_ctx():
    """Common data for reseller pages: enriched products + profile."""
    uid = get_active_reseller_uid()
    prods, profile = models.get_reseller_products(uid)
    return uid, prods, profile


@app.route('/reseller/nda')
@auth.login_required
def reseller_nda():
    """Confidentiality notice a new reseller must accept before using the portal."""
    curr = auth.get_current_user()
    profile = models.get_reseller_profile(curr['id'])
    if not profile:
        return _no_reseller_profile()
    if profile.get('nda_accepted_at'):
        return redirect(url_for('reseller_dashboard'))
    return render_template('reseller/nda.html', profile=profile,
                           suspend_days=models.PROSPECT_SUSPEND_DAYS)


@app.route('/reseller/accept-nda', methods=['POST'])
@auth.login_required
def reseller_accept_nda():
    curr = auth.get_current_user()
    profile = models.get_reseller_profile(curr['id'])
    if not profile:
        return _no_reseller_profile()
    if request.form.get('agree') != 'yes':
        flash("Please tick the box to acknowledge the confidentiality notice.", "error")
        return redirect(url_for('reseller_nda'))
    models.set_nda_accepted(profile['id'])
    flash("Thank you. Welcome to OneCard!", "success")
    return redirect(url_for('reseller_dashboard'))


@app.route('/reseller')
@auth.login_required
def reseller_dashboard():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        flash("Reseller profile not found.", "error")
        return _no_reseller_profile()

    cat_counts = {}
    merchants = set()
    categories = set()
    for p in enriched:
        cat_counts[p['category']] = cat_counts.get(p['category'], 0) + 1
        merchants.add(p['merchant'])
        categories.add(p['category'])

    enriched.sort(key=lambda x: -x['margin_pct'])
    new_arrivals = [p for p in enriched if p.get('is_new') == 1]

    return render_template('reseller/dashboard.html', active_tab='dashboard',
                           profile=profile, products=enriched, top_products=enriched,
                           new_arrivals=new_arrivals, merchants=list(merchants),
                           categories=list(categories), cat_counts=cat_counts)


@app.route('/reseller/products')
@auth.login_required
def reseller_products():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()
    merchants = sorted({p['merchant'] for p in enriched})
    categories = sorted({p['category'] for p in enriched})
    countries = sorted({p['country'] for p in enriched})
    regions = sorted({p['region'] for p in enriched})
    currencies = sorted({p['currency'] for p in enriched if p['currency']})
    return render_template('reseller/products.html', active_tab='products',
                           profile=profile, products=enriched,
                           products_json=jdump(enriched),
                           merchants=merchants, categories=categories,
                           countries=countries, regions=regions, currencies=currencies)


@app.route('/reseller/merchants')
@auth.login_required
def reseller_merchants():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()
    merchants = sorted({p['merchant'] for p in enriched})
    categories = sorted({p['category'] for p in enriched})
    countries = sorted({p['country'] for p in enriched})
    regions = sorted({p['region'] for p in enriched})
    currencies = sorted({p['currency'] for p in enriched if p['currency']})
    return render_template('reseller/merchants.html', active_tab='merchants',
                           profile=profile, products_json=jdump(enriched),
                           merchants=merchants, categories=categories,
                           countries=countries, regions=regions, currencies=currencies)


@app.route('/reseller/calculator')
@auth.login_required
def reseller_calculator():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()
    return render_template('reseller/calculator.html', active_tab='calculator',
                           profile=profile, products_json=jdump(enriched))


@app.route('/reseller/recommended')
@auth.login_required
def reseller_recommended():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()

    # ── Personalized recommendation scoring ──
    my_countries = set(profile.get('countries') or [])
    # v8: client can be several types at once — combine all their affinities
    my_types = profile.get('client_types') or []
    affinity_cats = set()
    for t in my_types:
        affinity_cats |= set(models.CLIENT_TYPE_AFFINITY.get(t, []))
    types_label = ' / '.join(my_types) if my_types else (profile.get('client_type') or '')

    def country_match(p):
        if p['country'] in my_countries:
            return 1.0
        if my_countries and any(p['country'].startswith(f"eSIM - {c}") for c in my_countries):
            return 0.9
        if p['country'] in ('Global', 'GCC', 'MENA'):
            return 0.6
        return 0.0 if my_countries else 0.5

    max_margin = max((p['margin_pct'] for p in enriched), default=1) or 1
    for p in enriched:
        score = 0.40 * (p['popularity'] / 100.0)
        score += 0.25 * (p['margin_pct'] / max_margin)
        score += 0.20 * country_match(p)
        score += 0.10 * (1.0 if p['category'] in affinity_cats else 0.0)
        score += 0.05 * (1.0 if p.get('is_new') else 0.0)
        p['rec_score'] = round(score, 4)
        reasons = []
        if p['popularity'] > 80: reasons.append('Top seller')
        if p['category'] in affinity_cats: reasons.append(f"Popular with {types_label or 'similar clients'}")
        if p['country'] in my_countries: reasons.append('Your market')
        if p['margin_pct'] >= max_margin * 0.6: reasons.append('High margin')
        if p.get('is_new'): reasons.append('New')
        p['rec_reasons'] = reasons[:3]

    enriched.sort(key=lambda x: -x['rec_score'])
    top_for_type = [p for p in enriched if p['category'] in affinity_cats][:12] if affinity_cats else []
    top_markets = [p for p in enriched if country_match(p) >= 0.9][:12] if my_countries else []
    categories = sorted({p['category'] for p in enriched})

    return render_template('reseller/recommended.html', active_tab='recommended',
                           profile=profile, products_json=jdump(enriched[:200]),
                           top_for_type=top_for_type, top_markets=top_markets,
                           categories=categories, types_label=types_label)


# ── Reseller: Forecast (pre-contract purchase intent) ────────────

@app.route('/reseller/forecast', methods=['GET', 'POST'])
@auth.login_required
def reseller_forecast():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()

    if request.method == 'POST':
        if block_in_preview():
            return redirect(url_for('reseller_forecast'))
        try:
            items = json.loads(request.form.get('items_json') or '[]')
        except json.JSONDecodeError:
            items = []
        note = request.form.get('note', '')
        disp = profile['display_currency']
        clean = []
        by_id = {p['id']: p for p in enriched}
        for it in items:
            if it.get('type') == 'merchant' and it.get('merchant') and float(it.get('value') or 0) > 0:
                # Value comes in the reseller's display currency; store SAR base.
                est_sar = models.convert_amount(float(it['value']), disp, 'SAR')
                clean.append({'item_type': 'merchant', 'merchant': it['merchant'],
                              'est_value': round(est_sar, 2)})
            elif it.get('type') == 'product' and it.get('product_id') in by_id:
                p = by_id[it['product_id']]
                qty = int(it.get('quantity') or 0)
                if qty > 0:
                    # client_price is already in the display currency -> to SAR
                    est_sar = models.convert_amount(qty * p['client_price'], disp, 'SAR')
                    clean.append({'item_type': 'product', 'merchant': p['merchant'],
                                  'product_rowid': p['id'], 'product_name': p['product_name'],
                                  'quantity': qty, 'est_value': round(est_sar, 2)})
        if not clean:
            flash("Add at least one merchant or product to your plan.", "error")
        else:
            fid = models.create_forecast(profile['id'], note, clean)
            total_sar = sum(i['est_value'] for i in clean)
            models.notify(profile['registered_by'],
                          "New purchase forecast received 📋",
                          f"{profile['company_name']} submitted a purchase plan worth "
                          f"{total_sar:,.0f} SAR ({len(clean)} items).", f"/sales/forecasts/{fid}")
            # Operations also needs the forecast so they can plan stock ahead.
            models.notify(models.get_user_ids_by_role('ops'),
                          "Forecast for stock planning 📦",
                          f"{profile['company_name']} forecasts {total_sar:,.0f} SAR "
                          f"({len(clean)} items) for the coming period.", "/ops/forecasts")
            flash("Your purchase plan was submitted to your account manager. "
                  "They will contact you to finalize the contract.", "success")
            return redirect(url_for('reseller_forecast'))

    disp = profile['display_currency']
    merchants_data = {}
    for p in enriched:
        m = merchants_data.setdefault(p['merchant'], {'merchant': p['merchant'], 'count': 0,
                                                      'avg_margin': 0, 'currency': p['currency']})
        m['count'] += 1
        m['avg_margin'] += p['margin_pct']
    for m in merchants_data.values():
        m['avg_margin'] = round(m['avg_margin'] / m['count'], 1)

    my_forecasts = models.get_reseller_forecasts(profile['id'])
    # forecast values are stored in SAR; show them in the display currency
    for f in my_forecasts:
        f['total_value'] = round(models.convert_amount(f['total_value'], 'SAR', disp))
    return render_template('reseller/forecast.html', active_tab='forecast',
                           profile=profile, disp=disp,
                           products_json=jdump(enriched),
                           merchants_json=jdump(sorted(merchants_data.values(),
                                                            key=lambda x: x['merchant'])),
                           my_forecasts=my_forecasts)


@app.route('/reseller/forecast/budget', methods=['POST'])
@auth.login_required
def reseller_forecast_budget():
    """Brand-new clients who don't yet know what to buy just commit a starting
    budget; they'll connect via API and explore the catalogue."""
    uid, _, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()
    if block_in_preview():
        return redirect(url_for('reseller_forecast'))
    disp = profile['display_currency']
    amount = float(request.form.get('amount') or 0)
    if amount <= 0:
        flash("Enter the budget you'd like to start with.", "error")
        return redirect(url_for('reseller_forecast'))
    amount_sar = round(models.convert_amount(amount, disp, 'SAR'), 2)
    models.create_budget_forecast(profile['id'], amount_sar, request.form.get('note', ''))
    models.notify(profile['registered_by'], "New client set a starting budget 💡",
                  f"{profile['company_name']} will start with {amount:,.0f} {disp} and explore "
                  f"the catalogue (likely via API).", "/sales/forecasts")
    models.notify(models.get_user_ids_by_role('ops'), "Exploratory budget for planning 📦",
                  f"{profile['company_name']} committed a {amount_sar:,.0f} SAR starting budget.",
                  "/ops/forecasts")
    flash(f"Your starting budget of {amount:,.0f} {disp} was recorded. Browse the catalogue and "
          f"place orders whenever you're ready — via the portal or the API.", "success")
    return redirect(url_for('reseller_forecast'))


# ── Reseller: Orders (post-contract) ─────────────────────────────

@app.route('/reseller/orders', methods=['GET', 'POST'])
@auth.login_required
def reseller_orders():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()

    if profile['contract_status'] != 'contracted':
        return render_template('reseller/orders_locked.html', active_tab='orders', profile=profile)

    disp = profile['display_currency']

    def to_disp(v):
        return round(models.convert_amount(v or 0, 'SAR', disp))

    if request.method == 'POST':
        if block_in_preview():
            return redirect(url_for('reseller_orders'))
        try:
            items = json.loads(request.form.get('items_json') or '[]')
        except json.JSONDecodeError:
            items = []
        by_id = {p['id']: p for p in enriched}
        order_items = []
        for it in items:
            p = by_id.get(it.get('product_id'))
            qty = int(it.get('quantity') or 0)
            if p and qty > 0:
                # enriched prices are already in the display currency
                order_items.append({'product_rowid': p['id'], 'product_name': p['product_name'],
                                    'merchant': p['merchant'], 'category': p['category'],
                                    'currency': p['currency'], 'quantity': qty,
                                    'unit_price': p['client_price'], 'unit_face': p['face_value']})
        if not order_items:
            flash("Your order is empty.", "error")
        else:
            oid, err = models.create_order(profile['id'], order_items)
            if err:
                flash(err, "error")
            else:
                total_disp = sum(i['unit_price'] * i['quantity'] for i in order_items)
                total_sar = round(models.convert_amount(total_disp, disp, 'SAR'))
                # Sales sees SAR (internal base); the customer sees their currency.
                models.notify(profile['registered_by'], "New order placed 🛒",
                              f"{profile['company_name']} placed order #{oid} worth {total_sar:,.0f} SAR.",
                              "/sales/resellers")
                flash(f"Order #{oid} placed successfully — {total_disp:,.0f} {disp} deducted from your wallet.", "success")
                return redirect(url_for('reseller_orders'))

    forecast_by_merchant = models.get_latest_forecast_merchant_values(profile['id'])
    actual_by_merchant = models.get_month_orders_by_merchant(profile['id'])
    comparison = []
    for m in sorted(set(list(forecast_by_merchant) + list(actual_by_merchant))):
        comparison.append({'merchant': m,
                           'forecast': to_disp(forecast_by_merchant.get(m, 0)),
                           'actual': to_disp(actual_by_merchant.get(m, 0))})
    orders = models.get_orders(profile['id'])
    for o in orders:
        o['total_cost'] = to_disp(o['total_cost'])
        o['total_savings'] = to_disp(o['total_savings'])
        o['items'] = models.get_order_items(o['id'])
        for it in o['items']:
            it['line_total_sar'] = to_disp(it.get('line_total_sar') or it.get('line_total'))
        o['codes'] = models.get_order_codes(o['id'])   # v8: gift-card codes they bought

    return render_template('reseller/orders.html', active_tab='orders',
                           profile=profile, products_json=jdump(enriched),
                           wallet_display=profile['wallet_balance_display'], disp=disp,
                           orders=orders, comparison=comparison)


# ── Reseller: Wallet ─────────────────────────────────────────────

@app.route('/reseller/wallet', methods=['GET', 'POST'])
@auth.login_required
def reseller_wallet():
    uid, _, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()

    disp = profile['display_currency']
    if request.method == 'POST':
        if block_in_preview():
            return redirect(url_for('reseller_wallet'))
        amount = float(request.form.get('amount') or 0)   # in the reseller's display currency
        bank_ref = request.form.get('bank_reference', '').strip()
        note = request.form.get('note', '')
        file = request.files.get('receipt')

        if amount <= 0:
            flash("Enter a valid transfer amount.", "error")
        elif not file or not file.filename:
            flash("Please attach the bank transfer receipt.", "error")
        else:
            sniffed_ext = _sniff_receipt_ext(file)
            if not sniffed_ext:
                flash("Receipt must be a genuine image (PNG/JPG/WEBP) or PDF file.", "error")
            else:
                fname = f"r{profile['id']}_{uuid.uuid4().hex[:12]}{sniffed_ext}"
                file.save(os.path.join(UPLOAD_DIR, fname))
                # Wallet base is SAR; convert what the reseller entered.
                amount_sar = round(models.convert_amount(amount, disp, 'SAR'), 2)
                txn_id = models.create_topup_request(profile['id'], amount_sar, bank_ref, fname,
                                                     note, orig_amount=amount, orig_currency=disp)
                models.notify(models.get_user_ids_by_role('finance'),
                              "New wallet top-up to verify 🧾",
                              f"{profile['company_name']} uploaded a transfer receipt for "
                              f"{amount:,.0f} {disp} (ref: {bank_ref or '—'}).", "/finance")
                flash("Receipt uploaded. The finance team will verify your transfer and "
                      "credit your wallet.", "success")
                return redirect(url_for('reseller_wallet'))

    transactions = models.get_wallet_transactions(profile['id'])
    # Present every stored SAR amount in the reseller's display currency.
    for t in transactions:
        if t.get('orig_currency') == disp and t.get('orig_amount') is not None:
            t['display_amount'] = round(t['orig_amount'])       # exact, no double-convert
        else:
            t['display_amount'] = round(models.convert_amount(t['amount'], 'SAR', disp))
    pending_total = sum(t['display_amount'] for t in transactions
                        if t['type'] == 'topup' and t['status'] == 'pending')
    # v14: credit/consignment resellers settle statements (not top-ups).
    statements = []
    if profile['account_type'] != 'prepaid':
        statements = models.get_statements(profile['id'])
        pending_settle_ids = {t['statement_id'] for t in transactions
                              if t['type'] == 'settlement' and t['status'] == 'pending'}
        for s in statements:
            s['amount_display'] = round(models.convert_amount(s['amount'], 'SAR', disp))
            s['settlement_pending'] = s['id'] in pending_settle_ids
    return render_template('reseller/wallet.html', active_tab='wallet',
                           profile=profile, transactions=transactions,
                           pending_total=pending_total, disp=disp, statements=statements)


@app.route('/reseller/statements/<int:sid>/settle', methods=['POST'])
@auth.login_required
def reseller_settle_statement(sid):
    uid, _, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()
    if block_in_preview():
        return redirect(url_for('reseller_wallet'))
    st = models.get_statement(sid)
    if not st or st['reseller_id'] != profile['id']:
        flash("Statement not found.", "error")
        return redirect(url_for('reseller_wallet'))
    if st['status'] not in ('issued', 'overdue'):
        flash("This statement is already settled.", "info")
        return redirect(url_for('reseller_wallet'))
    disp = profile['display_currency']
    bank_ref = request.form.get('bank_reference', '').strip()
    file = request.files.get('receipt')
    if not file or not file.filename:
        flash("Please attach the bank transfer receipt.", "error")
        return redirect(url_for('reseller_wallet'))
    ext = _sniff_receipt_ext(file)
    if not ext:
        flash("Receipt must be a genuine image (PNG/JPG/WEBP) or PDF file.", "error")
        return redirect(url_for('reseller_wallet'))
    fname = f"s{profile['id']}_{uuid.uuid4().hex[:12]}{ext}"
    file.save(os.path.join(UPLOAD_DIR, fname))
    # Settlement covers the full statement; store the SAR amount + what they paid.
    orig_amount = round(models.convert_amount(st['amount'], 'SAR', disp))
    models.create_settlement_request(profile['id'], sid, st['amount'], bank_ref, fname,
                                     note=f'Settle statement #{sid}',
                                     orig_amount=orig_amount, orig_currency=disp)
    models.notify(models.get_user_ids_by_role('finance'),
                  "Settlement receipt to verify 🧾",
                  f"{profile['company_name']} uploaded a settlement for statement #{sid} "
                  f"({st['amount']:,.0f} SAR).", "/finance/credit")
    flash("Settlement receipt uploaded. Finance will verify it and clear the statement.", "success")
    return redirect(url_for('reseller_wallet'))


# ── Reseller: Contract (review & sign) ───────────────────────────

@app.route('/reseller/contract')
@auth.login_required
def reseller_contract():
    uid, _, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()
    contracts = models.get_reseller_contracts(profile['id'])
    latest, events = (models.get_contract(contracts[0]['id']) if contracts else (None, []))
    return render_template('reseller/contract.html', active_tab='contract',
                           profile=profile, contracts=contracts,
                           latest=latest, events=events,
                           account_labels=models.ACCOUNT_TYPE_LABELS)


@app.route('/reseller/contract/<int:cid>/sign', methods=['POST'])
@auth.login_required
def reseller_contract_sign(cid):
    uid, _, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()
    if block_in_preview():
        return redirect(url_for('reseller_contract'))
    contract, _ = models.get_contract(cid)
    if not contract or contract['reseller_id'] != profile['id']:
        flash("Contract not found.", "error")
        return redirect(url_for('reseller_contract'))
    fname = _save_contract_file(request.files.get('signed'), profile['id'])
    if not fname:
        flash("Attach the signed contract as PDF, Word or an image.", "error")
        return redirect(url_for('reseller_contract'))
    models.reseller_upload_signed(cid, fname, uid)
    # Sales owner + CCO see the signed copy is ready to activate.
    recipients = [contract['registered_by']] + models.get_user_ids_by_role('cco')
    models.notify(recipients, "Signed contract received ✅",
                  f"{profile['company_name']} uploaded their signed contract — ready to activate.",
                  "/cco/contracts")
    flash("Signed contract uploaded. Your account manager will activate your account.", "success")
    return redirect(url_for('reseller_contract'))


# ── Reseller: Analysis ───────────────────────────────────────────

@app.route('/reseller/analysis')
@auth.login_required
def reseller_analysis():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return _no_reseller_profile()
    disp = profile['display_currency']
    data = models.get_reseller_analysis(profile['id'])

    # All analysis figures are stored in SAR — present them in the
    # reseller's display currency (a % share is unaffected by conversion).
    def cv(v):
        return round(models.convert_amount(v or 0, 'SAR', disp))
    for k in ('spend', 'face', 'savings'):
        data['totals'][k] = cv(data['totals'].get(k))
    for row in data['by_merchant']:
        row['spend'], row['savings'] = cv(row.get('spend')), cv(row.get('savings'))
    for row in data['by_category']:
        row['spend'] = cv(row.get('spend'))
    for row in data['monthly']:
        row['spend'], row['savings'] = cv(row.get('spend')), cv(row.get('savings'))
    for row in data['top_products']:
        row['spend'] = cv(row.get('spend'))

    insights = []
    t = data['totals']
    if t['orders'] > 0:
        savings_pct = (t['savings'] / t['face'] * 100) if t['face'] else 0
        insights.append(f"You purchased {t['face']:,.0f} {disp} of face value for {t['spend']:,.0f} {disp} — "
                        f"a total gain of {t['savings']:,.0f} {disp} ({savings_pct:.1f}%).")
        if data['by_merchant']:
            top = data['by_merchant'][0]
            share = top['spend'] / t['spend'] * 100 if t['spend'] else 0
            insights.append(f"'{top['merchant']}' is your biggest merchant: {share:.0f}% of your spend.")
            if share > 60:
                insights.append("Consider diversifying across more merchants to reduce concentration risk.")
        if data['by_category']:
            catset = {c['category'] for c in data['by_category']}
            affinity = set(models.CLIENT_TYPE_AFFINITY.get(profile.get('client_type') or '', []))
            missing = affinity - catset
            if missing:
                insights.append("Untapped categories that perform well for similar businesses: "
                                + ", ".join(sorted(missing)) + ".")
    if profile['contract_status'] == 'contracted' and profile['expected_monthly_sales']:
        ym = datetime.now(timezone.utc).strftime('%Y-%m')
        this_month = cv(models.get_month_total_orders(profile['id'], ym))
        commit_disp = cv(profile['expected_monthly_sales'])
        pct = (this_month / commit_disp * 100) if commit_disp else 0
        insights.append(f"This month you've ordered {this_month:,.0f} {disp} of your "
                        f"{commit_disp:,.0f} {disp} monthly commitment ({pct:.0f}%).")

    return render_template('reseller/analysis.html', active_tab='analysis',
                           profile=profile, data=data, insights=insights, disp=disp)


# ── Run ──────────────────────────────────────────────────────────

if __name__ == '__main__':
    models.init_db()
    models.seed_default_data()
    # v16: background worker drains the webhook retry queue (skip the debug
    # reloader's parent process to avoid a duplicate worker; ONECARD_NO_WEBHOOK_WORKER=1
    # disables it so tests can drive delivery deterministically).
    if ((not DEBUG_MODE or os.environ.get('WERKZEUG_RUN_MAIN') == 'true')
            and os.environ.get('ONECARD_NO_WEBHOOK_WORKER') != '1'):
        models.start_webhook_worker()
    # Debug/reloader only when explicitly requested: set ONECARD_DEBUG=1
    app.run(debug=DEBUG_MODE, port=8000)
