"""
OneCard Platform — Flask Web Application
=========================================
Core entry point with routes for BD Managers, Sales Managers, and Clients.
"""
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import os
import json
import models
import auth

app = Flask(__name__)
app.secret_key = os.urandom(24)


# ── Context Processor ─────────────────────────────────────────────

@app.context_processor
def inject_user():
    """Inject logged-in user and preview state into all templates."""
    curr = auth.get_current_user()
    is_preview = 'preview_user_id' in session
    preview_company = None
    if is_preview:
        profile = models.get_client_profile(session['preview_user_id'])
        if profile:
            preview_company = profile['company_name']
    return {
        'current_user': curr,
        'is_preview': is_preview,
        'preview_company': preview_company
    }


def get_active_client_uid():
    """Get active user_id for client views (supports preview mode)."""
    if 'preview_user_id' in session:
        curr = auth.get_current_user()
        if curr and curr['role'] in ('admin', 'sales'):
            return session['preview_user_id']
    return session.get('user_id')


# ── Global Routes ────────────────────────────────────────────────

@app.route('/')
def index():
    user = auth.get_current_user()
    if not user:
        return redirect(url_for('login'))
    if user['role'] == 'admin':
        return redirect(url_for('admin_dashboard'))
    elif user['role'] == 'sales':
        return redirect(url_for('sales_dashboard'))
    else:
        return redirect(url_for('client_dashboard'))


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


# ── Admin Routes (BD Manager) ────────────────────────────────────

@app.route('/admin')
@auth.admin_required
def admin_dashboard():
    stats = models.get_product_stats()
    tiers = models.get_all_tiers()
    clients = models.get_all_clients()
    return render_template('admin/dashboard.html', active_tab='dashboard', stats=stats, tiers=tiers, clients=clients)


@app.route('/admin/tiers', methods=['GET', 'POST'])
@auth.admin_required
def admin_tiers():
    if request.method == 'POST':
        ids = request.form.getlist('tier_id')
        names = request.form.getlist('tier_name')
        min_sales = request.form.getlist('tier_min_sales')
        min_cats = request.form.getlist('tier_min_cats')
        margins = request.form.getlist('tier_margin')
        colors = request.form.getlist('tier_color')

        for i in range(len(ids)):
            models.upsert_tier(
                ids[i],
                names[i],
                float(min_sales[i] or 0),
                int(min_cats[i] or 1),
                float(margins[i] or 20),
                colors[i],
                i + 1
            )
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


@app.route('/admin/clients')
@auth.admin_required
def admin_clients():
    clients = models.get_all_clients()
    return render_template('admin/clients.html', active_tab='clients', clients=clients)


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

    uid = models.create_user(email, password, name, role)
    if uid:
        flash(f"User {name} created successfully.", "success")
    else:
        flash("Email address is already in use.", "error")
    return redirect(url_for('admin_users'))


# ── Sales Manager Routes ─────────────────────────────────────────

@app.route('/sales')
@auth.sales_required
def sales_dashboard():
    curr = auth.get_current_user()
    clients = models.get_all_clients(registered_by=curr['id'])
    stats = models.get_product_stats()
    return render_template('sales/dashboard.html', active_tab='dashboard', clients=clients, stats=stats)


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

        regs = request.form.getlist('regions')
        cats = request.form.getlist('categories')

        # Auto tier assignment logic
        assigned = models.auto_assign_tier(sales, len(cats))
        tier_id = assigned['id'] if assigned else None

        # Create client user first
        uid = models.create_user(cemail, pw, cname, 'client')
        if uid:
            curr = auth.get_current_user()
            models.create_client(uid, comp, sales, tier_id, curr['id'], regs, cats, notes)
            flash(f"Client '{comp}' registered successfully with '{assigned['name'] if assigned else 'None'}' tier.", "success")
            return redirect(url_for('sales_dashboard'))
        else:
            flash("Client email address is already in use.", "error")

    categories = models.get_all_categories()
    regions = models.get_all_regions()
    tiers = models.get_all_tiers()

    tiers_json = json.dumps([dict(t) for t in tiers])
    return render_template('sales/register.html', active_tab='register',
                           categories=categories, regions=regions,
                           tiers_json=tiers_json)


@app.route('/sales/clients')
@auth.sales_required
def sales_clients():
    curr = auth.get_current_user()
    clients = models.get_all_clients(registered_by=curr['id'])
    return render_template('sales/my_clients.html', active_tab='clients', clients=clients)


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


@app.route('/sales/preview/<int:uid>')
@auth.sales_required
def sales_preview_enter(uid):
    # Verify client belongs to this sales manager (or current user is admin)
    curr = auth.get_current_user()
    profile = models.get_client_profile(uid)
    if not profile:
        flash("Client not found.", "error")
        return redirect(url_for('sales_dashboard'))

    if curr['role'] != 'admin' and profile['registered_by'] != curr['id']:
        flash("Access denied.", "error")
        return redirect(url_for('sales_dashboard'))

    session['preview_user_id'] = uid
    flash(f"Entering portal preview for '{profile['company_name']}'", "info")
    return redirect(url_for('client_dashboard'))


@app.route('/sales/preview/exit')
def sales_preview_exit():
    if 'preview_user_id' in session:
        session.pop('preview_user_id')
        flash("Exited preview mode.", "info")
    return redirect(url_for('sales_dashboard'))


# ── Client Portal Routes ─────────────────────────────────────────

@app.route('/client')
@auth.login_required
def client_dashboard():
    uid = get_active_client_uid()
    prods, profile = models.get_client_products(uid)
    if not profile:
        flash("Client profile not found.", "error")
        return redirect(url_for('logout'))

    # Calculate client tier prices
    tier = profile['tier']
    share = (tier['margin_share_pct'] / 100.0) if tier else 0.20

    enriched = []
    total_disc = 0
    cat_counts = {}
    merchants = set()
    categories = set()

    for p in prods:
        disc = p['oc_margin'] * share
        c_price = max(p['default_price'] - disc, p['cost'])
        saved = p['face_value'] - c_price
        pct = (saved / p['face_value'] * 100.0) if p['face_value'] > 0 else 0
        total_disc += pct

        enriched.append({
            **p,
            'client_price': c_price,
            'discount': disc,
            'margin_pct': pct
        })
        cat_counts[p['category']] = cat_counts.get(p['category'], 0) + 1
        merchants.add(p['merchant'])
        categories.add(p['category'])

    # Sort products by margin descending
    enriched.sort(key=lambda x: -x['margin_pct'])

    avg_discount = total_disc / len(prods) if prods else 0

    return render_template('client/dashboard.html', active_tab='dashboard',
                           profile=profile, products=enriched, top_products=enriched,
                           merchants=list(merchants), categories=list(categories),
                           avg_discount=avg_discount, cat_counts=cat_counts)


@app.route('/client/products')
@auth.login_required
def client_products():
    uid = get_active_client_uid()
    prods, profile = models.get_client_products(uid)
    if not profile:
        return redirect(url_for('logout'))

    tier = profile['tier']
    share = (tier['margin_share_pct'] / 100.0) if tier else 0.20

    enriched = []
    merchants = set()
    categories = set()

    for p in prods:
        disc = p['oc_margin'] * share
        c_price = max(p['default_price'] - disc, p['cost'])
        saved = p['face_value'] - c_price
        pct = (saved / p['face_value'] * 100.0) if p['face_value'] > 0 else 0

        enriched.append({
            **p,
            'client_price': c_price,
            'discount': disc,
            'margin_pct': pct
        })
        merchants.add(p['merchant'])
        categories.add(p['category'])

    products_json = json.dumps(enriched)
    return render_template('client/products.html', active_tab='products',
                           profile=profile, products=enriched, products_json=products_json,
                           merchants=sorted(list(merchants)), categories=sorted(list(categories)))


@app.route('/client/merchants')
@auth.login_required
def client_merchants():
    uid = get_active_client_uid()
    prods, profile = models.get_client_products(uid)
    if not profile:
        return redirect(url_for('logout'))

    tier = profile['tier']
    share = (tier['margin_share_pct'] / 100.0) if tier else 0.20

    enriched = []
    merchants = set()

    for p in prods:
        disc = p['oc_margin'] * share
        c_price = max(p['default_price'] - disc, p['cost'])
        saved = p['face_value'] - c_price
        pct = (saved / p['face_value'] * 100.0) if p['face_value'] > 0 else 0

        enriched.append({
            **p,
            'client_price': c_price,
            'discount': disc,
            'margin_pct': pct
        })
        merchants.add(p['merchant'])

    products_json = json.dumps(enriched)
    return render_template('client/merchants.html', active_tab='merchants',
                           profile=profile, products_json=products_json,
                           merchants=sorted(list(merchants)))


@app.route('/client/calculator')
@auth.login_required
def client_calculator():
    uid = get_active_client_uid()
    prods, profile = models.get_client_products(uid)
    if not profile:
        return redirect(url_for('logout'))

    tier = profile['tier']
    share = (tier['margin_share_pct'] / 100.0) if tier else 0.20

    enriched = []
    for p in prods:
        disc = p['oc_margin'] * share
        c_price = max(p['default_price'] - disc, p['cost'])
        saved = p['face_value'] - c_price
        pct = (saved / p['face_value'] * 100.0) if p['face_value'] > 0 else 0

        enriched.append({
            **p,
            'client_price': c_price,
            'discount': disc,
            'margin_pct': pct
        })

    products_json = json.dumps(enriched)
    return render_template('client/calculator.html', active_tab='calculator',
                           profile=profile, products_json=products_json)


@app.route('/client/recommended')
@auth.login_required
def client_recommended():
    uid = get_active_client_uid()
    prods, profile = models.get_client_products(uid)
    if not profile:
        return redirect(url_for('logout'))

    tier = profile['tier']
    share = (tier['margin_share_pct'] / 100.0) if tier else 0.20

    enriched = []
    categories = set()
    for p in prods:
        disc = p['oc_margin'] * share
        c_price = max(p['default_price'] - disc, p['cost'])
        saved = p['face_value'] - c_price
        pct = (saved / p['face_value'] * 100.0) if p['face_value'] > 0 else 0

        enriched.append({
            **p,
            'client_price': c_price,
            'discount': disc,
            'margin_pct': pct
        })
        categories.add(p['category'])

    # Default sort by margin % descending
    enriched.sort(key=lambda x: -x['margin_pct'])

    products_json = json.dumps(enriched)
    return render_template('client/recommended.html', active_tab='recommended',
                           profile=profile, products_json=products_json,
                           categories=sorted(list(categories)))


# ── Run ──────────────────────────────────────────────────────────

if __name__ == '__main__':
    models.init_db()
    models.seed_default_data()
    app.run(debug=True, port=8000)
