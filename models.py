"""
OneCard Platform — Database Models (v3)
===================================
SQLite database layer with all CRUD operations.
"""
import sqlite3
import os
import bcrypt
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'onecard.db')


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
            role TEXT NOT NULL CHECK(role IN ('admin','sales','reseller')),
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    """)
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


def auto_assign_tier(expected_sales, num_merchants):
    """Find the best tier for given sales + merchant count."""
    tiers = get_all_tiers()  # sorted by min_monthly_sales DESC
    for t in tiers:
        if expected_sales >= t['min_monthly_sales'] and num_merchants >= t['min_merchants']:
            return dict(t)
    if tiers:
        return dict(tiers[-1])
    return None


# ── Reseller Profiles ─────────────────────────────────────────────

def create_reseller(user_id, company_name, expected_sales, tier_id, registered_by, regions, merchants, notes=''):
    conn = get_db()
    conn.execute("""INSERT INTO reseller_profiles
                    (user_id, company_name, expected_monthly_sales, assigned_tier_id, registered_by, notes)
                    VALUES (?,?,?,?,?,?)""",
                 (user_id, company_name, expected_sales, tier_id, registered_by, notes))
    reseller_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for r in regions:
        conn.execute("INSERT INTO reseller_regions (reseller_id, region) VALUES (?,?)", (reseller_id, r))
    for m in merchants:
        conn.execute("INSERT INTO reseller_merchants (reseller_id, merchant) VALUES (?,?)", (reseller_id, m))
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
    p['regions'] = [r['region'] for r in conn.execute(
        "SELECT region FROM reseller_regions WHERE reseller_id=?", (profile['id'],)).fetchall()]
    p['merchants'] = [m['merchant'] for m in conn.execute(
        "SELECT merchant FROM reseller_merchants WHERE reseller_id=?", (profile['id'],)).fetchall()]
    if profile['assigned_tier_id']:
        tier = conn.execute("SELECT * FROM tier_rules WHERE id=?", (profile['assigned_tier_id'],)).fetchone()
        p['tier'] = dict(tier) if tier else None
    else:
        p['tier'] = None
    conn.close()
    return p


def get_all_resellers(registered_by=None):
    conn = get_db()
    if registered_by:
        rows = conn.execute("""
            SELECT cp.*, u.email, u.name as contact_name, t.name as tier_name, t.margin_share_pct, t.color as tier_color
            FROM reseller_profiles cp
            JOIN users u ON cp.user_id = u.id
            LEFT JOIN tier_rules t ON cp.assigned_tier_id = t.id
            WHERE cp.registered_by = ?
            ORDER BY cp.created_at DESC
        """, (registered_by,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT cp.*, u.email, u.name as contact_name, t.name as tier_name, t.margin_share_pct, t.color as tier_color,
                   su.name as sales_name
            FROM reseller_profiles cp
            JOIN users u ON cp.user_id = u.id
            LEFT JOIN tier_rules t ON cp.assigned_tier_id = t.id
            LEFT JOIN users su ON cp.registered_by = su.id
            ORDER BY cp.created_at DESC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def get_reseller_products(user_id):
    """Resellers have access to the ENTIRE Master Catalogue, so we return all products."""
    profile = get_reseller_profile(user_id)
    if not profile:
        return [], None
    products = get_products()
    return products, profile


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


def seed_default_data():
    """Create default admin + tier rules if not exists."""
    if not get_user_by_email('admin@onecard.com'):
        create_user('admin@onecard.com', 'OneCard2025!', 'Admin', 'admin')
        print("  [OK] Default admin created: admin@onecard.com")

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

    # Create a demo sales manager
    if not get_user_by_email('sales@onecard.com'):
        create_user('sales@onecard.com', 'Sales2025!', 'Sales Manager', 'sales')
        print("  [OK] Demo sales manager created: sales@onecard.com")
