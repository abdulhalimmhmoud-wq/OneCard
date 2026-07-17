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
import uuid
from datetime import datetime
import models
import auth

app = Flask(__name__)
app.secret_key = os.urandom(24)

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads', 'receipts')
PRICEFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads', 'pricefiles')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PRICEFILE_DIR, exist_ok=True)
ALLOWED_RECEIPT_EXT = {'.png', '.jpg', '.jpeg', '.pdf', '.webp'}
ALLOWED_PRICEFILE_EXT = {'.xls', '.xlsx'}


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
    return {
        'current_user': curr,
        'is_preview': is_preview,
        'preview_company': preview_company,
        'unread_notifications': unread,
    }


@app.template_filter('money')
def money_filter(v):
    """Whole-number money formatting (business rule: no decimals)."""
    try:
        return f"{round(float(v or 0)):,}"
    except (TypeError, ValueError):
        return '0'


@app.before_request
def daily_compliance_check():
    """Lazy daily tier-compliance run (throttled inside the function)."""
    if request.endpoint not in ('static',):
        models.run_tier_compliance()


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

ROLE_HOME = {'admin': 'admin_dashboard', 'sales': 'sales_dashboard',
             'cco': 'cco_dashboard', 'finance': 'finance_dashboard',
             'ops': 'ops_dashboard', 'reseller': 'reseller_dashboard'}


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
        email = request.form.get('email')
        password = request.form.get('password')
        user = auth.login_user(email, password)
        if user:
            session['user_id'] = user['id']
            session.permanent = True
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for('index'))
        else:
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


# ── Admin Routes (BD Manager) ────────────────────────────────────

@app.route('/admin')
@auth.admin_required
def admin_dashboard():
    stats = models.get_product_stats()
    tiers = models.get_all_tiers()
    resellers = models.get_all_resellers()
    pending_topups = len(models.get_topups('pending'))
    pending_discounts = len(models.get_discount_requests('pending'))
    return render_template('admin/dashboard.html', active_tab='dashboard', stats=stats,
                           tiers=tiers, resellers=resellers,
                           pending_topups=pending_topups, pending_discounts=pending_discounts)


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


@app.route('/admin/tiers/add')
@auth.admin_required
def admin_add_tier():
    models.upsert_tier(None, "New Tier", 0, 1, 20, "#64748b", 99)
    flash("Blank tier rule added. Edit and save below.", "info")
    return redirect(url_for('admin_tiers'))


@app.route('/admin/tiers/delete/<int:tid>')
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
    products_json = json.dumps(products)
    tiers_json = json.dumps([dict(t) for t in tiers])
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


@app.route('/admin/compliance/run')
@auth.admin_required
def admin_run_compliance():
    actions = models.run_tier_compliance(force=True)
    if actions:
        flash("Compliance check done: " + " | ".join(actions), "info")
    else:
        flash("Compliance check done — all contracted resellers are on track.", "success")
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
        comp = request.form.get('company_name')
        cname = request.form.get('contact_name')
        cemail = request.form.get('contact_email')
        pw = request.form.get('password')
        sales = float(request.form.get('expected_sales') or 0)
        notes = request.form.get('notes', '')
        client_type = request.form.get('client_type', '')
        countries = request.form.getlist('countries')

        assigned = models.auto_assign_tier(sales)
        tier_id = assigned['id'] if assigned else None

        uid = models.create_user(cemail, pw, cname, 'reseller')
        if uid:
            curr = auth.get_current_user()
            models.create_reseller(uid, comp, sales, tier_id, curr['id'], notes,
                                   client_type=client_type, countries=countries)
            flash(f"Reseller '{comp}' registered successfully with "
                  f"'{assigned['name'] if assigned else 'None'}' plan.", "success")
            return redirect(url_for('sales_dashboard'))
        else:
            flash("Reseller email address is already in use.", "error")

    tiers = models.get_all_tiers()
    tiers_json = json.dumps([dict(t) for t in tiers])
    return render_template('sales/register.html', active_tab='register',
                           tiers_json=tiers_json,
                           client_types=models.CLIENT_TYPES,
                           countries=models.get_all_countries())


@app.route('/sales/resellers')
@auth.sales_required
def sales_resellers():
    curr = auth.get_current_user()
    resellers = models.get_all_resellers(registered_by=curr['id'])
    return render_template('sales/my_resellers.html', active_tab='resellers', resellers=resellers)


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
    models.update_reseller_profile(rid, client_type=client_type, countries=countries)
    flash(f"Profile updated for {profile['company_name']}.", "success")
    return redirect(url_for('sales_resellers'))


@app.route('/sales/catalogue')
@auth.sales_required
def sales_catalogue():
    products = models.get_products()
    categories = models.get_all_categories()
    regions = models.get_all_regions()
    tiers = models.get_all_tiers()
    products_json = json.dumps(products)
    tiers_json = json.dumps([dict(t) for t in tiers])
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
    resellers_json = json.dumps([{
        'id': r['id'], 'company_name': r['company_name'],
        'share': r['margin_share_pct'] or 20, 'tier_name': r['tier_name'] or 'None',
    } for r in resellers])
    merchants_json = json.dumps([{'merchant': m['merchant'], 'avg_margin': m['avg_margin'] or 0}
                                 for m in merchants])
    return render_template('sales/discounts.html', active_tab='discounts',
                           resellers=resellers, merchants=merchants, requests=my_requests,
                           resellers_json=resellers_json, merchants_json=merchants_json)


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


# ── Operations Routes (v5) ───────────────────────────────────────

@app.route('/ops')
@auth.ops_required
def ops_dashboard():
    stats = models.get_ops_stats()
    recent = models.get_price_log(limit=12)
    return render_template('ops/dashboard.html', active_tab='ops_dashboard',
                           stats=stats, recent=recent)


@app.route('/ops/products')
@auth.ops_required
def ops_products():
    products = models.get_products(include_inactive=True)
    return render_template('ops/products.html', active_tab='ops_products',
                           products=products, products_json=json.dumps(products))


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
                           product=None, categories=models.get_all_categories(),
                           countries=models.get_all_countries(), regions=models.get_all_regions(),
                           currencies=models.get_all_currencies())


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
                           product=product, categories=models.get_all_categories(),
                           countries=models.get_all_countries(), regions=models.get_all_regions(),
                           currencies=models.get_all_currencies())


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
                           matrix=matrix, matrix_json=json.dumps(matrix),
                           merchant_summary=merchant_summary,
                           suppliers=suppliers,
                           suppliers_json=json.dumps([{'id': s['id'], 'name': s['name']} for s in suppliers]),
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
    products_json = json.dumps([{'id': p['id'], 'product_name': p['product_name'],
                                 'merchant': p['merchant'], 'cost': p['cost'],
                                 'currency': p['currency']} for p in products])
    # per-product best offers for the live warning
    offers = {}
    for row in models.get_sourcing_matrix():
        if row['best_cost'] is not None:
            offers[row['id']] = {'best_cost': row['best_cost'], 'best_supplier': row['best_supplier']}
    return render_template('ops/batches.html', active_tab='ops_batches',
                           batches=batches, suppliers=suppliers,
                           products_json=products_json, offers_json=json.dumps(offers))


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


# ── BD / CCO: Sourcing Intelligence (v6) ─────────────────────────

@app.route('/sourcing-intel')
@auth.cco_required
def sourcing_intel():
    ym = request.args.get('ym') or datetime.now().strftime('%Y-%m')
    kpis = models.get_sourcing_kpis()
    sales_by_supplier = models.get_sales_by_supplier(ym)
    sales_all_time = models.get_sales_by_supplier(None)
    scorecards = models.get_supplier_scorecards()
    opportunities = [r for r in models.get_sourcing_matrix() if r['saving_vs_std'] > 0]
    opportunities.sort(key=lambda x: -x['saving_vs_std'])
    improvements = models.get_margin_improvements()
    variance_batches = [b for b in models.get_batches() if b['sourcing_variance'] > 0][:15]
    return render_template('sourcing_intel.html', active_tab='sourcing_intel',
                           kpis=kpis, ym=ym,
                           sales_by_supplier=sales_by_supplier,
                           sales_all_time=sales_all_time,
                           scorecards=scorecards,
                           opportunities=opportunities[:15],
                           improvements=improvements,
                           variance_batches=variance_batches)


# ── Governance: Team Performance (v5) ────────────────────────────

@app.route('/team')
@auth.cco_required
def team_performance():
    ym = request.args.get('ym') or datetime.now().strftime('%Y-%m')
    team = models.get_team_performance(ym)
    # month options: last 12 months
    months = []
    y, m = datetime.now().year, datetime.now().month
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
    ym = request.args.get('ym') or datetime.now().strftime('%Y-%m')
    card = models.get_sales_scorecard(curr['id'], ym)
    resellers = models.get_all_resellers(registered_by=curr['id'])
    return render_template('sales/scorecard.html', active_tab='scorecard',
                           card=card, ym=ym, resellers=resellers)


# ── Reseller Portal Routes ────────────────────────────────────────

def _reseller_ctx():
    """Common data for reseller pages: enriched products + profile."""
    uid = get_active_reseller_uid()
    prods, profile = models.get_reseller_products(uid)
    return uid, prods, profile


@app.route('/reseller')
@auth.login_required
def reseller_dashboard():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        flash("Reseller profile not found.", "error")
        return redirect(url_for('logout'))

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
        return redirect(url_for('logout'))
    merchants = sorted({p['merchant'] for p in enriched})
    categories = sorted({p['category'] for p in enriched})
    countries = sorted({p['country'] for p in enriched})
    regions = sorted({p['region'] for p in enriched})
    currencies = sorted({p['currency'] for p in enriched if p['currency']})
    return render_template('reseller/products.html', active_tab='products',
                           profile=profile, products=enriched,
                           products_json=json.dumps(enriched),
                           merchants=merchants, categories=categories,
                           countries=countries, regions=regions, currencies=currencies)


@app.route('/reseller/merchants')
@auth.login_required
def reseller_merchants():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return redirect(url_for('logout'))
    merchants = sorted({p['merchant'] for p in enriched})
    categories = sorted({p['category'] for p in enriched})
    countries = sorted({p['country'] for p in enriched})
    regions = sorted({p['region'] for p in enriched})
    currencies = sorted({p['currency'] for p in enriched if p['currency']})
    return render_template('reseller/merchants.html', active_tab='merchants',
                           profile=profile, products_json=json.dumps(enriched),
                           merchants=merchants, categories=categories,
                           countries=countries, regions=regions, currencies=currencies)


@app.route('/reseller/calculator')
@auth.login_required
def reseller_calculator():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return redirect(url_for('logout'))
    return render_template('reseller/calculator.html', active_tab='calculator',
                           profile=profile, products_json=json.dumps(enriched))


@app.route('/reseller/recommended')
@auth.login_required
def reseller_recommended():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return redirect(url_for('logout'))

    # ── Personalized recommendation scoring ──
    my_countries = set(profile.get('countries') or [])
    affinity_cats = set(models.CLIENT_TYPE_AFFINITY.get(profile.get('client_type') or '', []))

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
        if p['category'] in affinity_cats: reasons.append(f"Popular with {profile.get('client_type') or 'similar clients'}")
        if p['country'] in my_countries: reasons.append('Your market')
        if p['margin_pct'] >= max_margin * 0.6: reasons.append('High margin')
        if p.get('is_new'): reasons.append('New')
        p['rec_reasons'] = reasons[:3]

    enriched.sort(key=lambda x: -x['rec_score'])
    top_for_type = [p for p in enriched if p['category'] in affinity_cats][:12] if affinity_cats else []
    top_markets = [p for p in enriched if country_match(p) >= 0.9][:12] if my_countries else []
    categories = sorted({p['category'] for p in enriched})

    return render_template('reseller/recommended.html', active_tab='recommended',
                           profile=profile, products_json=json.dumps(enriched[:200]),
                           top_for_type=top_for_type, top_markets=top_markets,
                           categories=categories)


# ── Reseller: Forecast (pre-contract purchase intent) ────────────

@app.route('/reseller/forecast', methods=['GET', 'POST'])
@auth.login_required
def reseller_forecast():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return redirect(url_for('logout'))

    if request.method == 'POST':
        if block_in_preview():
            return redirect(url_for('reseller_forecast'))
        try:
            items = json.loads(request.form.get('items_json') or '[]')
        except json.JSONDecodeError:
            items = []
        note = request.form.get('note', '')
        clean = []
        by_id = {p['id']: p for p in enriched}
        for it in items:
            if it.get('type') == 'merchant' and it.get('merchant') and float(it.get('value') or 0) > 0:
                clean.append({'item_type': 'merchant', 'merchant': it['merchant'],
                              'est_value': float(it['value'])})
            elif it.get('type') == 'product' and it.get('product_id') in by_id:
                p = by_id[it['product_id']]
                qty = int(it.get('quantity') or 0)
                if qty > 0:
                    clean.append({'item_type': 'product', 'merchant': p['merchant'],
                                  'product_rowid': p['id'], 'product_name': p['product_name'],
                                  'quantity': qty, 'est_value': qty * p['client_price']})
        if not clean:
            flash("Add at least one merchant or product to your plan.", "error")
        else:
            fid = models.create_forecast(profile['id'], note, clean)
            total = sum(i['est_value'] for i in clean)
            models.notify(profile['registered_by'],
                          "New purchase forecast received 📋",
                          f"{profile['company_name']} submitted a purchase plan worth "
                          f"{total:,.0f} SAR ({len(clean)} items).", f"/sales/forecasts/{fid}")
            flash("Your purchase plan was submitted to your account manager. "
                  "They will contact you to finalize the contract.", "success")
            return redirect(url_for('reseller_forecast'))

    merchants_data = {}
    for p in enriched:
        m = merchants_data.setdefault(p['merchant'], {'merchant': p['merchant'], 'count': 0,
                                                      'avg_margin': 0, 'currency': p['currency']})
        m['count'] += 1
        m['avg_margin'] += p['margin_pct']
    for m in merchants_data.values():
        m['avg_margin'] = round(m['avg_margin'] / m['count'], 1)

    my_forecasts = models.get_reseller_forecasts(profile['id'])
    return render_template('reseller/forecast.html', active_tab='forecast',
                           profile=profile,
                           products_json=json.dumps(enriched),
                           merchants_json=json.dumps(sorted(merchants_data.values(),
                                                            key=lambda x: x['merchant'])),
                           my_forecasts=my_forecasts)


# ── Reseller: Orders (post-contract) ─────────────────────────────

@app.route('/reseller/orders', methods=['GET', 'POST'])
@auth.login_required
def reseller_orders():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return redirect(url_for('logout'))

    if profile['contract_status'] != 'contracted':
        return render_template('reseller/orders_locked.html', active_tab='orders', profile=profile)

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
                total = sum(i['unit_price'] * i['quantity'] for i in order_items)
                models.notify(profile['registered_by'], "New order placed 🛒",
                              f"{profile['company_name']} placed order #{oid} worth {total:,.0f} SAR.",
                              "/sales/resellers")
                flash(f"Order #{oid} placed successfully — {total:,.0f} SAR deducted from your wallet.", "success")
                return redirect(url_for('reseller_orders'))

    forecast_by_merchant = models.get_latest_forecast_merchant_values(profile['id'])
    actual_by_merchant = models.get_month_orders_by_merchant(profile['id'])
    comparison = []
    for m in sorted(set(list(forecast_by_merchant) + list(actual_by_merchant))):
        comparison.append({'merchant': m,
                           'forecast': forecast_by_merchant.get(m, 0),
                           'actual': actual_by_merchant.get(m, 0)})
    orders = models.get_orders(profile['id'])
    for o in orders:
        o['items'] = models.get_order_items(o['id'])

    return render_template('reseller/orders.html', active_tab='orders',
                           profile=profile, products_json=json.dumps(enriched),
                           orders=orders, comparison=comparison)


# ── Reseller: Wallet ─────────────────────────────────────────────

@app.route('/reseller/wallet', methods=['GET', 'POST'])
@auth.login_required
def reseller_wallet():
    uid, _, profile = _reseller_ctx()
    if not profile:
        return redirect(url_for('logout'))

    if request.method == 'POST':
        if block_in_preview():
            return redirect(url_for('reseller_wallet'))
        amount = float(request.form.get('amount') or 0)
        bank_ref = request.form.get('bank_reference', '').strip()
        note = request.form.get('note', '')
        file = request.files.get('receipt')

        if amount <= 0:
            flash("Enter a valid transfer amount.", "error")
        elif not file or not file.filename:
            flash("Please attach the bank transfer receipt.", "error")
        else:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in ALLOWED_RECEIPT_EXT:
                flash("Receipt must be an image (PNG/JPG/WEBP) or PDF.", "error")
            else:
                fname = f"r{profile['id']}_{uuid.uuid4().hex[:12]}{ext}"
                file.save(os.path.join(UPLOAD_DIR, fname))
                txn_id = models.create_topup_request(profile['id'], amount, bank_ref, fname, note)
                models.notify(models.get_user_ids_by_role('finance'),
                              "New wallet top-up to verify 🧾",
                              f"{profile['company_name']} uploaded a transfer receipt for "
                              f"{amount:,.0f} SAR (ref: {bank_ref or '—'}).", "/finance")
                flash("Receipt uploaded. The finance team will verify your transfer and "
                      "credit your wallet.", "success")
                return redirect(url_for('reseller_wallet'))

    transactions = models.get_wallet_transactions(profile['id'])
    pending_total = sum(t['amount'] for t in transactions
                        if t['type'] == 'topup' and t['status'] == 'pending')
    return render_template('reseller/wallet.html', active_tab='wallet',
                           profile=profile, transactions=transactions,
                           pending_total=pending_total)


# ── Reseller: Analysis ───────────────────────────────────────────

@app.route('/reseller/analysis')
@auth.login_required
def reseller_analysis():
    uid, enriched, profile = _reseller_ctx()
    if not profile:
        return redirect(url_for('logout'))
    data = models.get_reseller_analysis(profile['id'])

    insights = []
    t = data['totals']
    if t['orders'] > 0:
        savings_pct = (t['savings'] / t['face'] * 100) if t['face'] else 0
        insights.append(f"You purchased {t['face']:,.0f} SAR of face value for {t['spend']:,.0f} SAR — "
                        f"a total gain of {t['savings']:,.0f} SAR ({savings_pct:.1f}%).")
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
        ym = datetime.now().strftime('%Y-%m')
        this_month = models.get_month_total_orders(profile['id'], ym)
        pct = this_month / profile['expected_monthly_sales'] * 100
        insights.append(f"This month you've ordered {this_month:,.0f} SAR of your "
                        f"{profile['expected_monthly_sales']:,.0f} SAR monthly commitment ({pct:.0f}%).")

    return render_template('reseller/analysis.html', active_tab='analysis',
                           profile=profile, data=data, insights=insights)


# ── Run ──────────────────────────────────────────────────────────

if __name__ == '__main__':
    models.init_db()
    models.seed_default_data()
    app.run(debug=True, port=8000)
