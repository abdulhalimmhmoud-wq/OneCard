"""
OneCard Platform — Database Models (v4)
=======================================
SQLite database layer with all CRUD operations.

Roles: admin (BD Manager), sales (Sales Manager), reseller, cco, finance.

INTEGRATION NOTES (for the technical team)
------------------------------------------
All data reads/writes go through this module only. When connecting the live
company systems, replace the bodies of these functions (or point them at the
production DB/API) without touching routes or templates:
  * Product catalogue feed .......... seed_products.py + `products` table
  * Reseller actual sales feed ...... `orders` table (currently platform orders;
                                      swap with real sales figures per reseller/month)
  * Wallet / payments ............... `wallet_transactions` (bank-transfer receipts
                                      are verified manually by Finance in v4)
"""
import sqlite3
import os
import math
import calendar
import bcrypt
from datetime import datetime, date, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'onecard.db')

# ── Business Constants ───────────────────────────────────────────

CLIENT_TYPES = [
    'Bank', 'Fintech / Wallet', 'Telecom Operator', 'Retail Chain',
    'E-commerce Platform', 'Gaming Store', 'Distributor / Wholesaler',
    'Travel & Tourism', 'Other'
]

# Which product categories matter most per client type (used by Recommended page)
CLIENT_TYPE_AFFINITY = {
    'Bank':                     ['Gift Cards & Vouchers', 'Shopping & Retail', 'Entertainment & Streaming', 'Gaming'],
    'Fintech / Wallet':         ['Gaming', 'Telecom & Recharge', 'Entertainment & Streaming', 'Gift Cards & Vouchers'],
    'Telecom Operator':         ['Telecom & Recharge', 'eSIM & Connectivity', 'Entertainment & Streaming'],
    'Retail Chain':             ['Gift Cards & Vouchers', 'Shopping & Retail', 'Gaming'],
    'E-commerce Platform':      ['Shopping & Retail', 'Gift Cards & Vouchers', 'Software & Subscriptions'],
    'Gaming Store':             ['Gaming', 'Entertainment & Streaming', 'Software & Subscriptions'],
    'Distributor / Wholesaler': ['Gaming', 'Telecom & Recharge', 'Gift Cards & Vouchers', 'Shopping & Retail'],
    'Travel & Tourism':         ['eSIM & Connectivity', 'Transportation', 'Entertainment & Streaming'],
    'Other':                    [],
}

ROLES = ('admin', 'sales', 'reseller', 'cco', 'finance')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create all tables."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','sales','reseller','cco','finance')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tier_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            min_monthly_sales REAL NOT NULL DEFAULT 0,
            min_merchants INTEGER NOT NULL DEFAULT 1,
            margin_share_pct REAL NOT NULL DEFAULT 20,
            color TEXT NOT NULL DEFAULT '#64748b',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reseller_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            company_name TEXT NOT NULL,
            expected_monthly_sales REAL NOT NULL DEFAULT 0,
            assigned_tier_id INTEGER REFERENCES tier_rules(id),
            registered_by INTEGER REFERENCES users(id),
            notes TEXT,
            client_type TEXT DEFAULT '',
            contract_status TEXT NOT NULL DEFAULT 'prospect',
            wallet_balance REAL NOT NULL DEFAULT 0,
            compliance_status TEXT NOT NULL DEFAULT 'ok',
            grace_until TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reseller_countries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            country TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reseller_regions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            region TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reseller_merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            merchant TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT,
            product_name TEXT NOT NULL,
            merchant TEXT NOT NULL,
            merchant_id TEXT,
            category TEXT NOT NULL,
            country TEXT NOT NULL,
            region TEXT NOT NULL,
            currency TEXT,
            cost REAL NOT NULL DEFAULT 0,
            default_price REAL NOT NULL DEFAULT 0,
            face_value REAL NOT NULL DEFAULT 0,
            oc_margin REAL NOT NULL DEFAULT 0,
            oc_margin_pct REAL NOT NULL DEFAULT 0,
            popularity INTEGER NOT NULL DEFAULT 0,
            is_new INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_products_country ON products(country);
        CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
        CREATE INDEX IF NOT EXISTS idx_products_region ON products(region);
        CREATE INDEX IF NOT EXISTS idx_products_merchant ON products(merchant);

        -- ── v4: Wallet ────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            type TEXT NOT NULL CHECK(type IN ('topup','order','adjustment')),
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            bank_reference TEXT,
            receipt_file TEXT,
            note TEXT,
            reviewed_by INTEGER REFERENCES users(id),
            reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ── v4: Forecasts (pre-contract purchase intent) ──────
        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','reviewed')),
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS forecast_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_id INTEGER NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
            item_type TEXT NOT NULL CHECK(item_type IN ('merchant','product')),
            merchant TEXT NOT NULL,
            product_rowid INTEGER,
            product_name TEXT,
            quantity INTEGER,
            est_value REAL NOT NULL DEFAULT 0
        );

        -- ── v4: Orders (post-contract purchasing) ─────────────
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'placed',
            total_cost REAL NOT NULL DEFAULT 0,
            total_face REAL NOT NULL DEFAULT 0,
            total_savings REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_rowid INTEGER,
            product_name TEXT NOT NULL,
            merchant TEXT NOT NULL,
            category TEXT,
            currency TEXT,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            unit_face REAL NOT NULL,
            line_total REAL NOT NULL
        );

        -- ── v4: Special discount requests (Sales → CCO) ───────
        CREATE TABLE IF NOT EXISTS discount_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            merchant TEXT NOT NULL,
            current_share_pct REAL NOT NULL,
            requested_share_pct REAL NOT NULL,
            current_monthly_sales REAL NOT NULL DEFAULT 0,
            projected_monthly_sales REAL NOT NULL DEFAULT 0,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            requested_by INTEGER NOT NULL REFERENCES users(id),
            decided_by INTEGER REFERENCES users(id),
            decision_note TEXT,
            decided_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Approved per-merchant margin-share overrides (applied automatically)
        CREATE TABLE IF NOT EXISTS merchant_share_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            merchant TEXT NOT NULL,
            share_pct REAL NOT NULL,
            source_request_id INTEGER REFERENCES discount_requests(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(reseller_id, merchant)
        );

        -- ── v4: Notifications ─────────────────────────────────
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT,
            link TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read);

        -- Small key/value store (compliance scheduler, etc.)
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    conn.close()
    migrate_db()


def migrate_db():
    """Upgrade an existing v3 database in place (idempotent)."""
    conn = get_db()

    # 1. users.role CHECK must allow cco/finance → rebuild table if old constraint
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if ddl and "'cco'" not in ddl['sql']:
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','sales','reseller','cco','finance')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users_new (id,email,password_hash,name,role,created_at)
                SELECT id,email,password_hash,name,role,created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
            PRAGMA foreign_keys=ON;
        """)
        conn.commit()

    # 2. reseller_profiles new columns
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(reseller_profiles)")}
    add = {
        'client_type': "ALTER TABLE reseller_profiles ADD COLUMN client_type TEXT DEFAULT ''",
        'contract_status': "ALTER TABLE reseller_profiles ADD COLUMN contract_status TEXT NOT NULL DEFAULT 'prospect'",
        'wallet_balance': "ALTER TABLE reseller_profiles ADD COLUMN wallet_balance REAL NOT NULL DEFAULT 0",
        'compliance_status': "ALTER TABLE reseller_profiles ADD COLUMN compliance_status TEXT NOT NULL DEFAULT 'ok'",
        'grace_until': "ALTER TABLE reseller_profiles ADD COLUMN grace_until TEXT",
    }
    for col, sql in add.items():
        if col not in cols:
            conn.execute(sql)
    conn.commit()
    conn.close()


# ── User Operations ──────────────────────────────────────────────

def hash_pw(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def check_pw(password, hashed):
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


def create_user(email, password, name, role):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash, name, role) VALUES (?,?,?,?)",
            (email.lower().strip(), hash_pw(password), name, role)
        )
        conn.commit()
        user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return user_id
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_user_by_email(email):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
    conn.close()
    return user


def get_user_by_id(uid):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return user


def get_all_users(role=None):
    conn = get_db()
    if role:
        rows = conn.execute("SELECT * FROM users WHERE role=? ORDER BY created_at DESC", (role,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def get_user_ids_by_role(*roles):
    conn = get_db()
    placeholders = ','.join(['?'] * len(roles))
    rows = conn.execute(f"SELECT id FROM users WHERE role IN ({placeholders})", roles).fetchall()
    conn.close()
    return [r['id'] for r in rows]


# ── Tier Rules ───────────────────────────────────────────────────

def get_all_tiers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tier_rules ORDER BY min_monthly_sales DESC, sort_order").fetchall()
    conn.close()
    return rows


def upsert_tier(tid, name, min_sales, min_merch, margin_pct, color, sort_order):
    conn = get_db()
    if tid:
        conn.execute("""UPDATE tier_rules SET name=?, min_monthly_sales=?, min_merchants=?,
                        margin_share_pct=?, color=?, sort_order=? WHERE id=?""",
                     (name, min_sales, min_merch, margin_pct, color, sort_order, tid))
    else:
        conn.execute("""INSERT INTO tier_rules (name, min_monthly_sales, min_merchants,
                        margin_share_pct, color, sort_order) VALUES (?,?,?,?,?,?)""",
                     (name, min_sales, min_merch, margin_pct, color, sort_order))
    conn.commit()
    conn.close()


def delete_tier(tid):
    conn = get_db()
    conn.execute("UPDATE reseller_profiles SET assigned_tier_id=NULL WHERE assigned_tier_id=?", (tid,))
    conn.execute("DELETE FROM tier_rules WHERE id=?", (tid,))
    conn.commit()
    conn.close()


def auto_assign_tier(expected_sales):
    """Find the best tier for given sales. Tiers are sorted by min_monthly_sales DESC."""
    tiers = get_all_tiers()
    for t in tiers:
        if expected_sales >= t['min_monthly_sales']:
            return dict(t)
    if tiers:
        return dict(tiers[-1])
    return None


def next_lower_tier(current_tier_id):
    """Return the tier immediately below the given tier (or None if already lowest)."""
    tiers = [dict(t) for t in get_all_tiers()]  # sorted DESC by min sales
    for i, t in enumerate(tiers):
        if t['id'] == current_tier_id:
            return tiers[i + 1] if i + 1 < len(tiers) else None
    return None


# ── Reseller Profiles ─────────────────────────────────────────────

def create_reseller(user_id, company_name, expected_sales, tier_id, registered_by,
                    notes='', client_type='', countries=None):
    conn = get_db()
    conn.execute("""INSERT INTO reseller_profiles
                    (user_id, company_name, expected_monthly_sales, assigned_tier_id,
                     registered_by, notes, client_type)
                    VALUES (?,?,?,?,?,?,?)""",
                 (user_id, company_name, expected_sales, tier_id, registered_by, notes, client_type))
    reseller_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for c in (countries or []):
        conn.execute("INSERT INTO reseller_countries (reseller_id, country) VALUES (?,?)", (reseller_id, c))
    conn.commit()
    conn.close()
    return reseller_id


def get_reseller_profile(user_id):
    conn = get_db()
    profile = conn.execute("SELECT * FROM reseller_profiles WHERE user_id=?", (user_id,)).fetchone()
    if not profile:
        conn.close()
        return None
    p = dict(profile)
    p['countries'] = [r['country'] for r in conn.execute(
        "SELECT country FROM reseller_countries WHERE reseller_id=?", (p['id'],))]
    if profile['assigned_tier_id']:
        tier = conn.execute("SELECT * FROM tier_rules WHERE id=?", (profile['assigned_tier_id'],)).fetchone()
        p['tier'] = dict(tier) if tier else None
    else:
        p['tier'] = None
    p['overrides'] = {r['merchant']: r['share_pct'] for r in conn.execute(
        "SELECT merchant, share_pct FROM merchant_share_overrides WHERE reseller_id=?", (p['id'],))}
    conn.close()
    return p


def get_reseller_profile_by_id(reseller_id):
    conn = get_db()
    row = conn.execute("SELECT user_id FROM reseller_profiles WHERE id=?", (reseller_id,)).fetchone()
    conn.close()
    return get_reseller_profile(row['user_id']) if row else None


def get_all_resellers(registered_by=None):
    conn = get_db()
    base = """
        SELECT cp.*, u.email, u.name as contact_name, t.name as tier_name,
               t.margin_share_pct, t.color as tier_color, su.name as sales_name
        FROM reseller_profiles cp
        JOIN users u ON cp.user_id = u.id
        LEFT JOIN tier_rules t ON cp.assigned_tier_id = t.id
        LEFT JOIN users su ON cp.registered_by = su.id
    """
    if registered_by:
        rows = conn.execute(base + " WHERE cp.registered_by=? ORDER BY cp.created_at DESC",
                            (registered_by,)).fetchall()
    else:
        rows = conn.execute(base + " ORDER BY cp.created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_contract_status(reseller_id, status):
    conn = get_db()
    conn.execute("UPDATE reseller_profiles SET contract_status=? WHERE id=?", (status, reseller_id))
    conn.commit()
    conn.close()


def update_reseller_profile(reseller_id, client_type=None, countries=None, expected_sales=None):
    conn = get_db()
    if client_type is not None:
        conn.execute("UPDATE reseller_profiles SET client_type=? WHERE id=?", (client_type, reseller_id))
    if expected_sales is not None:
        conn.execute("UPDATE reseller_profiles SET expected_monthly_sales=? WHERE id=?", (expected_sales, reseller_id))
    if countries is not None:
        conn.execute("DELETE FROM reseller_countries WHERE reseller_id=?", (reseller_id,))
        for c in countries:
            conn.execute("INSERT INTO reseller_countries (reseller_id, country) VALUES (?,?)", (reseller_id, c))
    conn.commit()
    conn.close()


# ── Products ─────────────────────────────────────────────────────

def get_products(country=None, region=None, category=None, merchant=None, search=None, limit=None):
    conn = get_db()
    q = "SELECT * FROM products WHERE 1=1"
    params = []
    if country:
        placeholders = ','.join(['?'] * len(country))
        q += f" AND country IN ({placeholders})"
        params.extend(country)
    if region:
        placeholders = ','.join(['?'] * len(region))
        q += f" AND region IN ({placeholders})"
        params.extend(region)
    if category:
        placeholders = ','.join(['?'] * len(category))
        q += f" AND category IN ({placeholders})"
        params.extend(category)
    if merchant:
        q += " AND merchant=?"
        params.append(merchant)
    if search:
        q += " AND (product_name LIKE ? OR merchant LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    q += " ORDER BY merchant, product_name"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def enrich_products_for_reseller(profile, products=None):
    """Compute reseller pricing for every product.

    Applies the tier margin share, then any CCO-approved per-merchant override.
    All money values are rounded to WHOLE numbers (business rule), floored at cost.
    """
    if products is None:
        products = get_products()
    tier = profile.get('tier')
    base_share = (tier['margin_share_pct'] / 100.0) if tier else 0.20
    overrides = profile.get('overrides', {})

    enriched = []
    for p in products:
        share = overrides.get(p['merchant'], None)
        share = (share / 100.0) if share is not None else base_share
        disc = p['oc_margin'] * share
        c_price = max(p['default_price'] - disc, p['cost'])
        # Whole-number pricing (never below cost)
        c_price = round(c_price)
        if c_price < p['cost']:
            c_price = math.ceil(p['cost'])
        face = round(p['face_value'])
        disc_r = round(disc)
        saved = face - c_price
        pct = (saved / face * 100.0) if face > 0 else 0
        enriched.append({
            **p,
            'face_value': face,
            'client_price': c_price,
            'discount': disc_r,
            'margin_pct': round(pct, 1),
            'has_override': p['merchant'] in overrides,
        })
    return enriched


def get_reseller_products(user_id):
    """Resellers have access to the ENTIRE Master Catalogue (enriched pricing)."""
    profile = get_reseller_profile(user_id)
    if not profile:
        return [], None
    return enrich_products_for_reseller(profile), profile


def get_all_regions():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT region FROM products WHERE region != 'eSIM' ORDER BY region").fetchall()
    conn.close()
    return [r['region'] for r in rows]


def get_all_categories():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT category FROM products ORDER BY category").fetchall()
    conn.close()
    return [r['category'] for r in rows]


def get_all_countries():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT country FROM products WHERE country NOT LIKE 'eSIM%' ORDER BY country").fetchall()
    conn.close()
    return [r['country'] for r in rows]


def get_all_currencies():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT currency FROM products WHERE currency IS NOT NULL AND currency != '' ORDER BY currency").fetchall()
    conn.close()
    return [r['currency'] for r in rows]


def get_all_merchants():
    conn = get_db()
    rows = conn.execute("""SELECT merchant, COUNT(*) as product_count,
                           AVG(oc_margin_pct) as avg_margin
                           FROM products GROUP BY merchant ORDER BY product_count DESC""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product_stats():
    conn = get_db()
    stats = {
        'total_products': conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        'total_merchants': conn.execute("SELECT COUNT(DISTINCT merchant) FROM products").fetchone()[0],
        'total_countries': conn.execute("SELECT COUNT(DISTINCT country) FROM products WHERE country NOT LIKE 'eSIM%'").fetchone()[0],
        'total_categories': conn.execute("SELECT COUNT(DISTINCT category) FROM products").fetchone()[0],
        'avg_margin': conn.execute("SELECT AVG(oc_margin_pct) FROM products").fetchone()[0] or 0,
        'total_resellers': conn.execute("SELECT COUNT(*) FROM reseller_profiles").fetchone()[0],
    }
    conn.close()
    return stats


# ── Notifications ────────────────────────────────────────────────

def notify(user_ids, title, body='', link=''):
    if not user_ids:
        return
    if isinstance(user_ids, int):
        user_ids = [user_ids]
    conn = get_db()
    for uid in set(u for u in user_ids if u):
        conn.execute("INSERT INTO notifications (user_id, title, body, link) VALUES (?,?,?,?)",
                     (uid, title, body, link))
    conn.commit()
    conn.close()


def get_notifications(user_id, limit=50):
    conn = get_db()
    rows = conn.execute("""SELECT * FROM notifications WHERE user_id=?
                           ORDER BY created_at DESC, id DESC LIMIT ?""", (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def unread_count(user_id):
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (user_id,)).fetchone()[0]
    conn.close()
    return n


def mark_notifications_read(user_id):
    conn = get_db()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ── Wallet ───────────────────────────────────────────────────────

def create_topup_request(reseller_id, amount, bank_reference, receipt_file, note=''):
    conn = get_db()
    conn.execute("""INSERT INTO wallet_transactions
                    (reseller_id, type, amount, status, bank_reference, receipt_file, note)
                    VALUES (?,?,?,?,?,?,?)""",
                 (reseller_id, 'topup', amount, 'pending', bank_reference, receipt_file, note))
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return txn_id


def get_wallet_transactions(reseller_id, limit=100):
    conn = get_db()
    rows = conn.execute("""SELECT wt.*, u.name as reviewer_name FROM wallet_transactions wt
                           LEFT JOIN users u ON wt.reviewed_by = u.id
                           WHERE wt.reseller_id=?
                           ORDER BY wt.created_at DESC, wt.id DESC LIMIT ?""",
                        (reseller_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_topups(status=None):
    conn = get_db()
    q = """SELECT wt.*, cp.company_name, cp.wallet_balance, u.email as reseller_email,
                  ru.name as reviewer_name
           FROM wallet_transactions wt
           JOIN reseller_profiles cp ON wt.reseller_id = cp.id
           JOIN users u ON cp.user_id = u.id
           LEFT JOIN users ru ON wt.reviewed_by = ru.id
           WHERE wt.type='topup'"""
    params = []
    if status:
        q += " AND wt.status=?"
        params.append(status)
    q += " ORDER BY wt.created_at DESC, wt.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_topup(txn_id):
    conn = get_db()
    row = conn.execute("""SELECT wt.*, cp.company_name, cp.user_id as reseller_user_id
                          FROM wallet_transactions wt
                          JOIN reseller_profiles cp ON wt.reseller_id = cp.id
                          WHERE wt.id=?""", (txn_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def review_topup(txn_id, approve, reviewer_id, note=''):
    """Finance approves/rejects a top-up. On approval the wallet is credited."""
    conn = get_db()
    txn = conn.execute("SELECT * FROM wallet_transactions WHERE id=? AND status='pending'", (txn_id,)).fetchone()
    if not txn:
        conn.close()
        return False
    status = 'approved' if approve else 'rejected'
    conn.execute("""UPDATE wallet_transactions SET status=?, reviewed_by=?,
                    reviewed_at=CURRENT_TIMESTAMP, note=COALESCE(NULLIF(?,''), note) WHERE id=?""",
                 (status, reviewer_id, note, txn_id))
    if approve:
        conn.execute("UPDATE reseller_profiles SET wallet_balance = wallet_balance + ? WHERE id=?",
                     (txn['amount'], txn['reseller_id']))
    conn.commit()
    conn.close()
    return True


# ── Forecasts ────────────────────────────────────────────────────

def create_forecast(reseller_id, note, items):
    """items: list of dicts {item_type, merchant, product_rowid, product_name, quantity, est_value}"""
    conn = get_db()
    conn.execute("INSERT INTO forecasts (reseller_id, note) VALUES (?,?)", (reseller_id, note))
    fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for it in items:
        conn.execute("""INSERT INTO forecast_items
                        (forecast_id, item_type, merchant, product_rowid, product_name, quantity, est_value)
                        VALUES (?,?,?,?,?,?,?)""",
                     (fid, it['item_type'], it['merchant'], it.get('product_rowid'),
                      it.get('product_name'), it.get('quantity'), it.get('est_value', 0)))
    conn.commit()
    conn.close()
    return fid


def get_forecasts_for_sales(sales_user_id=None):
    conn = get_db()
    q = """SELECT f.*, cp.company_name, cp.contract_status, u.name as contact_name,
                  (SELECT COUNT(*) FROM forecast_items fi WHERE fi.forecast_id = f.id) as item_count,
                  (SELECT COALESCE(SUM(est_value),0) FROM forecast_items fi WHERE fi.forecast_id = f.id) as total_value
           FROM forecasts f
           JOIN reseller_profiles cp ON f.reseller_id = cp.id
           JOIN users u ON cp.user_id = u.id"""
    params = []
    if sales_user_id:
        q += " WHERE cp.registered_by=?"
        params.append(sales_user_id)
    q += " ORDER BY f.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_forecast_detail(fid):
    conn = get_db()
    f = conn.execute("""SELECT f.*, cp.company_name, cp.registered_by, cp.user_id as reseller_user_id
                        FROM forecasts f JOIN reseller_profiles cp ON f.reseller_id=cp.id
                        WHERE f.id=?""", (fid,)).fetchone()
    if not f:
        conn.close()
        return None, []
    items = conn.execute("SELECT * FROM forecast_items WHERE forecast_id=? ORDER BY id", (fid,)).fetchall()
    conn.close()
    return dict(f), [dict(i) for i in items]


def get_reseller_forecasts(reseller_id):
    conn = get_db()
    rows = conn.execute("""SELECT f.*,
                  (SELECT COUNT(*) FROM forecast_items fi WHERE fi.forecast_id = f.id) as item_count,
                  (SELECT COALESCE(SUM(est_value),0) FROM forecast_items fi WHERE fi.forecast_id = f.id) as total_value
           FROM forecasts f WHERE f.reseller_id=? ORDER BY f.created_at DESC""", (reseller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_forecast_merchant_values(reseller_id):
    """merchant → forecast est_value from the reseller's most recent forecast."""
    conn = get_db()
    f = conn.execute("SELECT id FROM forecasts WHERE reseller_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                     (reseller_id,)).fetchone()
    if not f:
        conn.close()
        return {}
    rows = conn.execute("""SELECT merchant, SUM(est_value) as v FROM forecast_items
                           WHERE forecast_id=? GROUP BY merchant""", (f['id'],)).fetchall()
    conn.close()
    return {r['merchant']: r['v'] for r in rows}


def mark_forecast_reviewed(fid):
    conn = get_db()
    conn.execute("UPDATE forecasts SET status='reviewed' WHERE id=?", (fid,))
    conn.commit()
    conn.close()


# ── Orders ───────────────────────────────────────────────────────

def create_order(reseller_id, items):
    """items: list of {product_rowid, product_name, merchant, category, currency,
                       quantity, unit_price, unit_face}
    Deducts the order total from the reseller wallet (must have enough balance).
    Returns (order_id, error_message)."""
    total_cost = sum(it['unit_price'] * it['quantity'] for it in items)
    total_face = sum(it['unit_face'] * it['quantity'] for it in items)
    conn = get_db()
    bal = conn.execute("SELECT wallet_balance FROM reseller_profiles WHERE id=?", (reseller_id,)).fetchone()
    if not bal:
        conn.close()
        return None, "Reseller not found."
    if bal['wallet_balance'] < total_cost:
        conn.close()
        return None, f"Insufficient wallet balance. Order total is {total_cost:,.0f} but wallet has {bal['wallet_balance']:,.0f}."
    conn.execute("""INSERT INTO orders (reseller_id, total_cost, total_face, total_savings)
                    VALUES (?,?,?,?)""", (reseller_id, total_cost, total_face, total_face - total_cost))
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for it in items:
        conn.execute("""INSERT INTO order_items
                        (order_id, product_rowid, product_name, merchant, category, currency,
                         quantity, unit_price, unit_face, line_total)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (oid, it.get('product_rowid'), it['product_name'], it['merchant'],
                      it.get('category'), it.get('currency'), it['quantity'],
                      it['unit_price'], it['unit_face'], it['unit_price'] * it['quantity']))
    conn.execute("UPDATE reseller_profiles SET wallet_balance = wallet_balance - ? WHERE id=?",
                 (total_cost, reseller_id))
    conn.execute("""INSERT INTO wallet_transactions (reseller_id, type, amount, status, note)
                    VALUES (?,?,?,?,?)""",
                 (reseller_id, 'order', -total_cost, 'approved', f'Order #{oid}'))
    conn.commit()
    conn.close()
    return oid, None


def get_orders(reseller_id):
    conn = get_db()
    rows = conn.execute("""SELECT o.*,
                  (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id=o.id) as item_count
           FROM orders o WHERE o.reseller_id=? ORDER BY o.created_at DESC""", (reseller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_order_items(order_id):
    conn = get_db()
    rows = conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_month_orders_by_merchant(reseller_id, ym=None):
    """merchant → ordered value for a given month (default: current month)."""
    ym = ym or datetime.now().strftime('%Y-%m')
    conn = get_db()
    rows = conn.execute("""SELECT oi.merchant, SUM(oi.line_total) as v
                           FROM order_items oi JOIN orders o ON oi.order_id=o.id
                           WHERE o.reseller_id=? AND strftime('%Y-%m', o.created_at)=?
                           GROUP BY oi.merchant""", (reseller_id, ym)).fetchall()
    conn.close()
    return {r['merchant']: r['v'] for r in rows}


def get_month_total_orders(reseller_id, ym):
    conn = get_db()
    v = conn.execute("""SELECT COALESCE(SUM(total_cost),0) FROM orders
                        WHERE reseller_id=? AND strftime('%Y-%m', created_at)=?""",
                     (reseller_id, ym)).fetchone()[0]
    conn.close()
    return v


def get_reseller_analysis(reseller_id):
    """Aggregated purchasing analysis for the reseller Analysis page."""
    conn = get_db()
    out = {}
    out['totals'] = dict(conn.execute("""
        SELECT COALESCE(SUM(total_cost),0) as spend, COALESCE(SUM(total_face),0) as face,
               COALESCE(SUM(total_savings),0) as savings, COUNT(*) as orders
        FROM orders WHERE reseller_id=?""", (reseller_id,)).fetchone())
    out['by_merchant'] = [dict(r) for r in conn.execute("""
        SELECT oi.merchant, SUM(oi.line_total) as spend, SUM(oi.quantity) as qty,
               SUM(oi.unit_face*oi.quantity) - SUM(oi.line_total) as savings
        FROM order_items oi JOIN orders o ON oi.order_id=o.id
        WHERE o.reseller_id=? GROUP BY oi.merchant ORDER BY spend DESC""", (reseller_id,))]
    out['by_category'] = [dict(r) for r in conn.execute("""
        SELECT oi.category, SUM(oi.line_total) as spend, SUM(oi.quantity) as qty
        FROM order_items oi JOIN orders o ON oi.order_id=o.id
        WHERE o.reseller_id=? GROUP BY oi.category ORDER BY spend DESC""", (reseller_id,))]
    out['monthly'] = [dict(r) for r in conn.execute("""
        SELECT strftime('%Y-%m', created_at) as ym, SUM(total_cost) as spend,
               SUM(total_savings) as savings, COUNT(*) as orders
        FROM orders WHERE reseller_id=? GROUP BY ym ORDER BY ym""", (reseller_id,))]
    out['top_products'] = [dict(r) for r in conn.execute("""
        SELECT oi.product_name, oi.merchant, SUM(oi.quantity) as qty, SUM(oi.line_total) as spend
        FROM order_items oi JOIN orders o ON oi.order_id=o.id
        WHERE o.reseller_id=? GROUP BY oi.product_name, oi.merchant
        ORDER BY spend DESC LIMIT 10""", (reseller_id,))]
    conn.close()
    return out


# ── Discount Requests (Sales → CCO) ──────────────────────────────

def create_discount_request(reseller_id, merchant, current_share, requested_share,
                            current_sales, projected_sales, note, requested_by):
    conn = get_db()
    conn.execute("""INSERT INTO discount_requests
                    (reseller_id, merchant, current_share_pct, requested_share_pct,
                     current_monthly_sales, projected_monthly_sales, note, requested_by)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (reseller_id, merchant, current_share, requested_share,
                  current_sales, projected_sales, note, requested_by))
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return rid


def get_discount_requests(status=None, requested_by=None):
    conn = get_db()
    q = """SELECT dr.*, cp.company_name, t.name as tier_name, t.color as tier_color,
                  ru.name as requester_name, du.name as decider_name
           FROM discount_requests dr
           JOIN reseller_profiles cp ON dr.reseller_id = cp.id
           LEFT JOIN tier_rules t ON cp.assigned_tier_id = t.id
           LEFT JOIN users ru ON dr.requested_by = ru.id
           LEFT JOIN users du ON dr.decided_by = du.id
           WHERE 1=1"""
    params = []
    if status:
        q += " AND dr.status=?"
        params.append(status)
    if requested_by:
        q += " AND dr.requested_by=?"
        params.append(requested_by)
    q += " ORDER BY dr.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_merchant_avg_margin(merchant):
    conn = get_db()
    row = conn.execute("""SELECT AVG(oc_margin_pct) as avg_margin_pct,
                                 AVG(oc_margin) as avg_margin
                          FROM products WHERE merchant=?""", (merchant,)).fetchone()
    conn.close()
    return dict(row) if row else {'avg_margin_pct': 0, 'avg_margin': 0}


def decide_discount_request(rid, approve, decided_by, decision_note=''):
    """CCO decision. On approval the per-merchant override is applied automatically."""
    conn = get_db()
    req = conn.execute("SELECT * FROM discount_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
    if not req:
        conn.close()
        return None
    status = 'approved' if approve else 'rejected'
    conn.execute("""UPDATE discount_requests SET status=?, decided_by=?, decision_note=?,
                    decided_at=CURRENT_TIMESTAMP WHERE id=?""",
                 (status, decided_by, decision_note, rid))
    if approve:
        conn.execute("""INSERT INTO merchant_share_overrides (reseller_id, merchant, share_pct, source_request_id)
                        VALUES (?,?,?,?)
                        ON CONFLICT(reseller_id, merchant)
                        DO UPDATE SET share_pct=excluded.share_pct, source_request_id=excluded.source_request_id""",
                     (req['reseller_id'], req['merchant'], req['requested_share_pct'], rid))
    conn.commit()
    conn.close()
    return dict(req)


# ── Tier Compliance Automation ───────────────────────────────────

def _month_bounds(d=None):
    d = d or date.today()
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def run_tier_compliance(force=False):
    """Check every contracted reseller's PREVIOUS month actual sales vs commitment.

    Below commitment → 'warning' + one grace month (notifies reseller, sales, admin, CCO).
    Still below after grace → automatic downgrade one tier + notifications.
    Runs at most once per day unless force=True.
    Returns a list of action strings (for the admin UI).

    INTEGRATION NOTE: 'actual sales' currently = platform orders total.
    Point get_month_total_orders() at the company sales feed to use real figures.
    """
    conn = get_db()
    today = date.today()
    last_run = conn.execute("SELECT value FROM app_meta WHERE key='compliance_last_run'").fetchone()
    if not force and last_run and last_run['value'] == today.isoformat():
        conn.close()
        return []
    conn.execute("INSERT INTO app_meta (key,value) VALUES ('compliance_last_run',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today.isoformat(),))
    conn.commit()

    prev_month_end = today.replace(day=1) - timedelta(days=1)
    prev_ym = prev_month_end.strftime('%Y-%m')
    grace_deadline_first, grace_deadline_last = _month_bounds(today)

    resellers = conn.execute("""SELECT cp.*, u.id as uid, t.name as tier_name
                                FROM reseller_profiles cp
                                JOIN users u ON cp.user_id=u.id
                                LEFT JOIN tier_rules t ON cp.assigned_tier_id=t.id
                                WHERE cp.contract_status='contracted'""").fetchall()
    conn.close()

    actions = []
    admin_cco = get_user_ids_by_role('admin', 'cco')

    for r in resellers:
        expected = r['expected_monthly_sales'] or 0
        if expected <= 0:
            continue
        actual = get_month_total_orders(r['id'], prev_ym)

        if r['compliance_status'] == 'warning' and r['grace_until'] and r['grace_until'] < today.isoformat():
            # Grace period is over — re-check the month that just ended
            if actual < expected:
                lower = next_lower_tier(r['assigned_tier_id'])
                conn2 = get_db()
                if lower:
                    conn2.execute("""UPDATE reseller_profiles
                                     SET assigned_tier_id=?, expected_monthly_sales=?,
                                         compliance_status='ok', grace_until=NULL WHERE id=?""",
                                  (lower['id'], lower['min_monthly_sales'], r['id']))
                    msg = (f"{r['company_name']} did not reach the committed monthly sales of "
                           f"{expected:,.0f} SAR after the grace month (actual {actual:,.0f} SAR). "
                           f"Tier automatically moved from {r['tier_name']} to {lower['name']}.")
                    actions.append(f"DOWNGRADED: {r['company_name']} → {lower['name']}")
                else:
                    conn2.execute("""UPDATE reseller_profiles
                                     SET compliance_status='ok', grace_until=NULL WHERE id=?""", (r['id'],))
                    msg = (f"{r['company_name']} remains below commitment but is already on the lowest tier.")
                    actions.append(f"BELOW TARGET (lowest tier): {r['company_name']}")
                conn2.commit()
                conn2.close()
                notify([r['uid'], r['registered_by']] + admin_cco,
                       "Tier updated — commitment not met", msg, "/notifications")
            else:
                conn2 = get_db()
                conn2.execute("""UPDATE reseller_profiles
                                 SET compliance_status='ok', grace_until=NULL WHERE id=?""", (r['id'],))
                conn2.commit()
                conn2.close()
                notify([r['uid'], r['registered_by']] + admin_cco,
                       "Commitment recovered 🎉",
                       f"{r['company_name']} reached {actual:,.0f} SAR in {prev_ym} and keeps the "
                       f"{r['tier_name']} tier.", "/notifications")
                actions.append(f"RECOVERED: {r['company_name']}")

        elif r['compliance_status'] == 'ok' and actual < expected:
            conn2 = get_db()
            conn2.execute("""UPDATE reseller_profiles
                             SET compliance_status='warning', grace_until=? WHERE id=?""",
                          (grace_deadline_last.isoformat(), r['id']))
            conn2.commit()
            conn2.close()
            msg = (f"{r['company_name']} purchased {actual:,.0f} SAR in {prev_ym} — below the committed "
                   f"{expected:,.0f} SAR. Grace period until {grace_deadline_last.isoformat()}: reach the "
                   f"commitment this month or the account moves to the lower tier automatically.")
            notify([r['uid'], r['registered_by']] + admin_cco,
                   "⚠️ Monthly sales commitment not met", msg, "/notifications")
            actions.append(f"WARNING: {r['company_name']} ({actual:,.0f} / {expected:,.0f})")

    return actions


# ── Seed ─────────────────────────────────────────────────────────

def seed_default_data():
    """Create default users + tier rules if not exists."""
    defaults_users = [
        ('admin@onecard.com',   'OneCard2025!', 'Admin',          'admin'),
        ('sales@onecard.com',   'Sales2025!',   'Sales Manager',  'sales'),
        ('cco@onecard.com',     'Cco2025!',     'Chief Commercial Officer', 'cco'),
        ('finance@onecard.com', 'Finance2025!', 'Finance Team',   'finance'),
    ]
    for email, pw, name, role in defaults_users:
        if not get_user_by_email(email):
            create_user(email, pw, name, role)
            print(f"  [OK] Default {role} created: {email}")

    tiers = get_all_tiers()
    if not tiers:
        defaults = [
            ('Diamond', 500000, 5, 60, '#8b5cf6', 1),
            ('Gold',    200000, 3, 50, '#f59e0b', 2),
            ('Silver',   50000, 2, 40, '#9ca3af', 3),
            ('Bronze',   10000, 1, 30, '#b45309', 4),
            ('Starter',      0, 1, 20, '#3b82f6', 5),
        ]
        for name, sales, merch, margin, color, order in defaults:
            upsert_tier(None, name, sales, merch, margin, color, order)
        print("  [OK] Default tier rules created")
