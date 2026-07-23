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
import hashlib
import bcrypt
from datetime import datetime, date, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'onecard.db')


# ── Encryption at rest (v10 hardening) ────────────────────────────
# Gift-card codes and PINs are money. They are encrypted before being
# written to the database; a deterministic hash column enables lookup
# by plaintext code without ever storing the code unencrypted.

_FERNET = None


def _get_fernet():
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    from cryptography.fernet import Fernet
    key = os.environ.get('ONECARD_ENCRYPTION_KEY')
    if not key:
        key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance_encryption.key')
        if os.path.exists(key_file):
            key = open(key_file).read().strip()
        else:
            key = Fernet.generate_key().decode()
            with open(key_file, 'w') as f:
                f.write(key)
    _FERNET = Fernet(key.encode() if isinstance(key, str) else key)
    return _FERNET


def _enc(plaintext):
    if plaintext is None:
        return None
    return _get_fernet().encrypt(str(plaintext).encode()).decode()


def _dec(token):
    """Decrypt; tolerates already-plaintext legacy values so old rows
    (before this migration ran) don't crash the UI."""
    if token is None:
        return None
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except Exception:
        return token


def _code_hash(code):
    """Deterministic lookup key for an encrypted code — never reversible."""
    return hashlib.sha256(code.strip().encode()).hexdigest()


def _ensure_table_encrypted(conn, table, id_col='id'):
    """One-time upgrade: any row whose code/pin fail to decrypt is legacy
    plaintext and gets encrypted in place. Idempotent — already-encrypted
    rows decrypt successfully and are left untouched."""
    rows = conn.execute(f"SELECT {id_col} as rid, code, pin FROM {table}").fetchall()
    for r in rows:
        try:
            _get_fernet().decrypt(r['code'].encode())
        except Exception:
            conn.execute(f"UPDATE {table} SET code=?, pin=? WHERE {id_col}=?",
                         (_enc(r['code']), _enc(r['pin']) if r['pin'] is not None else None, r['rid']))

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

ROLES = ('admin', 'sales', 'reseller', 'cco', 'finance', 'ops', 'bd', 'partner')

BD_DEAL_TYPES = {
    'new_merchant': 'New Merchant Deal',
    'better_rate': 'Better Rate Negotiated',
    'new_supplier': 'New Supplier Found',
    'gift_card_program': 'Gift Card Issuing Lead',
    'other': 'Other',
}

# Margin guard: alert BD/CCO when an ops price change leaves a product below
# this OneCard margin %, or cuts the margin by more than half.
MARGIN_ALERT_FLOOR_PCT = 1.0


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A concurrent writer waits up to 5s for the lock instead of failing
    # immediately with "database is locked" (matters once BEGIN IMMEDIATE
    # transactions, e.g. in create_order, start serializing writers).
    conn.execute("PRAGMA busy_timeout = 5000")
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
            role TEXT NOT NULL CHECK(role IN ('admin','sales','reseller','cco','finance','ops','bd','partner')),
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

        CREATE TABLE IF NOT EXISTS reseller_client_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            client_type TEXT NOT NULL,
            UNIQUE(reseller_id, client_type)
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

        -- ── v5: Suppliers (Operations) ────────────────────────
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_person TEXT,
            email TEXT,
            phone TEXT,
            payment_terms TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS supplier_merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
            merchant TEXT NOT NULL,
            UNIQUE(supplier_id, merchant)
        );

        -- ── v5: Price change audit log ────────────────────────
        CREATE TABLE IF NOT EXISTS price_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_rowid INTEGER,
            product_name TEXT NOT NULL,
            merchant TEXT,
            action TEXT NOT NULL,          -- created / price_update / activated / deactivated
            field TEXT,                    -- cost / default_price / face_value ...
            old_value TEXT,
            new_value TEXT,
            source TEXT NOT NULL DEFAULT 'manual',   -- manual / bulk_import
            changed_by INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_pricelog_product ON price_change_log(product_rowid);

        -- ── v5: Monthly sales targets (governance) ────────────
        CREATE TABLE IF NOT EXISTS sales_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sales_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ym TEXT NOT NULL,              -- 'YYYY-MM'
            target_new_resellers INTEGER NOT NULL DEFAULT 0,
            target_sales_value REAL NOT NULL DEFAULT 0,
            UNIQUE(sales_user_id, ym)
        );

        -- One-shot SLA reminders (so we never nag twice for the same item)
        CREATE TABLE IF NOT EXISTS sla_nudges (
            key TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ── v8: Issuing Hub — we issue & manage digital gift cards
        -- for partner merchants and sell them through our channels ──
        CREATE TABLE IF NOT EXISTS issuing_partners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            contact_person TEXT,
            email TEXT,
            phone TEXT,
            partner_share_pct REAL NOT NULL DEFAULT 80,   -- % of selling price paid to the partner
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
            notes TEXT,
            created_by INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS issued_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_rowid INTEGER NOT NULL REFERENCES products(id),
            batch_ref TEXT,
            quantity INTEGER NOT NULL,
            generated_by INTEGER REFERENCES users(id),
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS issued_vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES issued_batches(id) ON DELETE CASCADE,
            product_rowid INTEGER NOT NULL,
            code TEXT NOT NULL,
            code_hash TEXT UNIQUE NOT NULL,
            pin TEXT,
            status TEXT NOT NULL DEFAULT 'available'
                CHECK(status IN ('available','sold','redeemed','void')),
            order_item_id INTEGER REFERENCES order_items(id),
            sold_at TIMESTAMP,
            redeemed_at TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_vouchers_stock ON issued_vouchers(product_rowid, status);
        CREATE INDEX IF NOT EXISTS idx_vouchers_order_item ON issued_vouchers(order_item_id);
        -- idx_vouchers_code_hash is created in migrate_db(): on an existing
        -- (pre-v10) database this table still lacks code_hash at this point
        -- in the script, and CREATE TABLE IF NOT EXISTS above is then a no-op.

        -- ── v9: Integration API ───────────────────────────────
        CREATE TABLE IF NOT EXISTS api_idempotency (
            key TEXT PRIMARY KEY,
            reseller_id INTEGER NOT NULL,
            order_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Codes delivered by EXTERNAL provider adapters (issued-hub codes
        -- live in issued_vouchers; this unifies fulfillment for the rest)
        CREATE TABLE IF NOT EXISTS external_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_item_id INTEGER NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            pin TEXT,
            provider TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_external_codes_item ON external_codes(order_item_id);

        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            url TEXT NOT NULL,
            status_code INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ── v8: BD Deal Pipeline (BD negotiates → Ops executes) ──
        CREATE TABLE IF NOT EXISTS bd_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('new_merchant','better_rate','new_supplier','gift_card_program','other')),
            title TEXT NOT NULL,
            merchant TEXT,
            supplier_name TEXT,
            details TEXT,
            expected_terms TEXT,
            status TEXT NOT NULL DEFAULT 'submitted'
                CHECK(status IN ('submitted','in_progress','done','rejected')),
            created_by INTEGER NOT NULL REFERENCES users(id),
            handled_by INTEGER REFERENCES users(id),
            handler_note TEXT,
            handled_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ── v6: Multi-supplier sourcing ───────────────────────
        -- One product can be offered by many suppliers, each at their own cost.
        CREATE TABLE IF NOT EXISTS supplier_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
            product_rowid INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            supplier_cost REAL NOT NULL,
            is_available INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'manual',    -- manual / bulk / api
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(supplier_id, product_rowid)
        );

        CREATE INDEX IF NOT EXISTS idx_supplier_products_product ON supplier_products(product_rowid);

        CREATE TABLE IF NOT EXISTS supplier_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
            product_rowid INTEGER NOT NULL,
            old_cost REAL,
            new_cost REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            changed_by INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ── v6: Purchase batches (inventory lots) ─────────────
        CREATE TABLE IF NOT EXISTS purchase_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            product_rowid INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL,
            remaining_qty INTEGER NOT NULL,
            unit_cost REAL NOT NULL,
            total_cost REAL NOT NULL,
            best_cost_at_purchase REAL,               -- cheapest available offer at buy time
            sourcing_variance REAL NOT NULL DEFAULT 0, -- (unit_cost - best) * qty, >0 = overpaid
            reason TEXT,                               -- why this supplier (governance)
            invoice_ref TEXT,
            status TEXT NOT NULL DEFAULT 'awaiting_reconciliation'
                CHECK(status IN ('awaiting_reconciliation','reconciled','disputed')),
            created_by INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reconciled_by INTEGER REFERENCES users(id),
            reconciled_at TIMESTAMP,
            reconcile_note TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_batches_product ON purchase_batches(product_rowid, created_at);

        -- Which batch every sold unit came from (FIFO) → true COGS per sale
        CREATE TABLE IF NOT EXISTS order_item_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_item_id INTEGER NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
            batch_id INTEGER REFERENCES purchase_batches(id),   -- NULL = unsourced (no stock recorded)
            quantity INTEGER NOT NULL,
            unit_cost REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_allocations_batch ON order_item_allocations(batch_id);

        -- ── v7 hardening: FX rates (wallet & reporting currency = SAR) ──
        -- INTEGRATION NOTE: replace/refresh these rates from the company FX feed.
        CREATE TABLE IF NOT EXISTS currency_rates (
            currency TEXT PRIMARY KEY,
            rate_to_sar REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER REFERENCES users(id)
        );

        -- Hot-path indexes (order history, analysis, wallet statements)
        CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
        CREATE INDEX IF NOT EXISTS idx_orders_reseller ON orders(reseller_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_wallet_reseller ON wallet_transactions(reseller_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_allocations_item ON order_item_allocations(order_item_id);
    """)
    conn.commit()
    conn.close()
    migrate_db()


def migrate_db():
    """Upgrade an existing v3 database in place (idempotent)."""
    conn = get_db()

    # 1. users.role CHECK must allow all v5 roles → rebuild table if old constraint
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if ddl and ("'cco'" not in ddl['sql'] or "'ops'" not in ddl['sql'] or "'bd'" not in ddl['sql'] or "'partner'" not in ddl['sql']):
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','sales','reseller','cco','finance','ops','bd','partner')),
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
        'contracted_at': "ALTER TABLE reseller_profiles ADD COLUMN contracted_at TIMESTAMP",
        # v11: the ONE currency this reseller sees the whole catalogue, wallet
        # and orders in. Derived from their market at registration (Saudi
        # Arabia -> SAR, otherwise -> USD); SAR is the internal base currency.
        'display_currency': "ALTER TABLE reseller_profiles ADD COLUMN display_currency TEXT NOT NULL DEFAULT 'SAR'",
    }
    just_added_display_cur = 'display_currency' not in cols
    for col, sql in add.items():
        if col not in cols:
            conn.execute(sql)
    # Backfill display_currency for existing resellers from their markets.
    if just_added_display_cur:
        conn.commit()
        for r in conn.execute("SELECT id FROM reseller_profiles").fetchall():
            countries = [x['country'] for x in conn.execute(
                "SELECT country FROM reseller_countries WHERE reseller_id=?", (r['id'],))]
            conn.execute("UPDATE reseller_profiles SET display_currency=? WHERE id=?",
                         (derive_display_currency(countries), r['id']))
        conn.commit()

    # 3. products: active flag + added date (ops catalogue management)
    pcols = {r['name'] for r in conn.execute("PRAGMA table_info(products)")}
    if 'is_active' not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if 'added_at' not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN added_at TIMESTAMP")

    # 4. forecasts: review timestamp (sales response-time metric)
    fcols = {r['name'] for r in conn.execute("PRAGMA table_info(forecasts)")}
    if 'reviewed_at' not in fcols:
        conn.execute("ALTER TABLE forecasts ADD COLUMN reviewed_at TIMESTAMP")

    # 5. suppliers: API key for automated price sync (v6)
    scols = {r['name'] for r in conn.execute("PRAGMA table_info(suppliers)")}
    if 'api_key' not in scols:
        conn.execute("ALTER TABLE suppliers ADD COLUMN api_key TEXT")

    # 6. v7 hardening: FX-aware order items + backfill of historical data
    oicols = {r['name'] for r in conn.execute("PRAGMA table_info(order_items)")}
    needs_fx_backfill = 'line_total_sar' not in oicols
    if 'fx_rate' not in oicols:
        conn.execute("ALTER TABLE order_items ADD COLUMN fx_rate REAL NOT NULL DEFAULT 1")
    if 'line_total_sar' not in oicols:
        conn.execute("ALTER TABLE order_items ADD COLUMN line_total_sar REAL")
    conn.commit()

    # Seed default FX rates once (Finance can edit them in the app)
    if conn.execute("SELECT COUNT(*) FROM currency_rates").fetchone()[0] == 0:
        defaults = [('SAR', 1.0), ('USD', 3.75), ('AED', 1.021), ('KWD', 12.22),
                    ('QAR', 1.030), ('JOD', 5.290), ('EGP', 0.078),
                    ('EUR', 4.06), ('LBP1', 0.000042)]
        for cur, rate in defaults:
            conn.execute("INSERT OR IGNORE INTO currency_rates (currency, rate_to_sar) VALUES (?,?)",
                         (cur, rate))
        conn.commit()

    # 7c. v9: reseller API credentials + webhook
    rcols = {r['name'] for r in conn.execute("PRAGMA table_info(reseller_profiles)")}
    if 'api_key' not in rcols:
        conn.execute("ALTER TABLE reseller_profiles ADD COLUMN api_key TEXT")
    if 'webhook_url' not in rcols:
        conn.execute("ALTER TABLE reseller_profiles ADD COLUMN webhook_url TEXT")

    # 7d. v9: per-line fulfillment status ('delivered' = codes attached,
    # 'external' = awaiting a provider adapter for that merchant)
    oicols2 = {r['name'] for r in conn.execute("PRAGMA table_info(order_items)")}
    if 'fulfillment_status' not in oicols2:
        conn.execute("ALTER TABLE order_items ADD COLUMN fulfillment_status TEXT NOT NULL DEFAULT 'external'")
        conn.execute("""UPDATE order_items SET fulfillment_status='delivered'
                        WHERE product_rowid IN (SELECT id FROM products WHERE is_issued=1)""")
    conn.commit()

    # 7e. v11: wallet top-ups record the amount as the reseller entered it
    # (their display currency) alongside the SAR base that hits the wallet,
    # so Finance sees exactly what the customer transferred.
    wtcols = {r['name'] for r in conn.execute("PRAGMA table_info(wallet_transactions)")}
    if 'orig_amount' not in wtcols:
        conn.execute("ALTER TABLE wallet_transactions ADD COLUMN orig_amount REAL")
    if 'orig_currency' not in wtcols:
        conn.execute("ALTER TABLE wallet_transactions ADD COLUMN orig_currency TEXT DEFAULT 'SAR'")
    conn.commit()

    # 7b. v8.1: partner portal login link
    ipcols = {r['name'] for r in conn.execute("PRAGMA table_info(issuing_partners)")}
    if 'portal_user_id' not in ipcols:
        conn.execute("ALTER TABLE issuing_partners ADD COLUMN portal_user_id INTEGER REFERENCES users(id)")
    conn.commit()

    # 7a. v8: Issuing Hub flags on products
    pcols2 = {r['name'] for r in conn.execute("PRAGMA table_info(products)")}
    if 'is_issued' not in pcols2:
        conn.execute("ALTER TABLE products ADD COLUMN is_issued INTEGER NOT NULL DEFAULT 0")
    if 'issuing_partner_id' not in pcols2:
        conn.execute("ALTER TABLE products ADD COLUMN issuing_partner_id INTEGER")
    conn.commit()

    # 7. v8: client types became multi-select — backfill from the legacy column
    conn.execute("""INSERT OR IGNORE INTO reseller_client_types (reseller_id, client_type)
                    SELECT id, client_type FROM reseller_profiles
                    WHERE client_type IS NOT NULL AND client_type != ''""")
    conn.commit()

    if needs_fx_backfill:
        # Backfill historical order lines + recompute order totals in SAR
        rates = {r['currency']: r['rate_to_sar'] for r in conn.execute("SELECT * FROM currency_rates")}
        for oi in conn.execute("SELECT id, currency, line_total FROM order_items").fetchall():
            rate = rates.get(oi['currency'], 1.0)
            conn.execute("UPDATE order_items SET fx_rate=?, line_total_sar=? WHERE id=?",
                         (rate, round(oi['line_total'] * rate, 2), oi['id']))
        for o in conn.execute("SELECT id FROM orders").fetchall():
            tot = conn.execute("SELECT COALESCE(SUM(line_total_sar),0), COALESCE(SUM(unit_face*quantity*fx_rate),0) "
                               "FROM order_items WHERE order_id=?", (o['id'],)).fetchone()
            conn.execute("UPDATE orders SET total_cost=?, total_face=?, total_savings=? WHERE id=?",
                         (round(tot[0], 2), round(tot[1], 2), round(tot[1] - tot[0], 2), o['id']))
        conn.commit()

    # 8. v10 hardening: encrypt voucher codes/PINs at rest.
    # issued_vouchers gets a rebuild (adds the code_hash lookup column and
    # drops the old plaintext-unique constraint on code); external_codes has
    # no lookup path so it's just encrypted in place.
    ivcols = {r['name'] for r in conn.execute("PRAGMA table_info(issued_vouchers)")}
    if 'code_hash' not in ivcols:
        old_rows = conn.execute("SELECT * FROM issued_vouchers").fetchall()
        conn.executescript("""
            CREATE TABLE issued_vouchers_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL REFERENCES issued_batches(id) ON DELETE CASCADE,
                product_rowid INTEGER NOT NULL,
                code TEXT NOT NULL,
                code_hash TEXT UNIQUE NOT NULL,
                pin TEXT,
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK(status IN ('available','sold','redeemed','void')),
                order_item_id INTEGER REFERENCES order_items(id),
                sold_at TIMESTAMP,
                redeemed_at TIMESTAMP
            );
        """)
        for r in old_rows:
            conn.execute("""INSERT INTO issued_vouchers_new
                            (id, batch_id, product_rowid, code, code_hash, pin, status,
                             order_item_id, sold_at, redeemed_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (r['id'], r['batch_id'], r['product_rowid'], _enc(r['code']),
                          _code_hash(r['code']), _enc(r['pin']) if r['pin'] is not None else None,
                          r['status'], r['order_item_id'], r['sold_at'], r['redeemed_at']))
        conn.executescript("""
            DROP TABLE issued_vouchers;
            ALTER TABLE issued_vouchers_new RENAME TO issued_vouchers;
            CREATE INDEX IF NOT EXISTS idx_vouchers_stock ON issued_vouchers(product_rowid, status);
            CREATE INDEX IF NOT EXISTS idx_vouchers_order_item ON issued_vouchers(order_item_id);
            CREATE INDEX IF NOT EXISTS idx_vouchers_code_hash ON issued_vouchers(code_hash);
        """)
        conn.commit()
    else:
        _ensure_table_encrypted(conn, 'issued_vouchers')
        conn.commit()
    # Unconditional: covers both the rebuild path above and a fresh install
    # where issued_vouchers was created with code_hash already present.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vouchers_code_hash ON issued_vouchers(code_hash)")
    _ensure_table_encrypted(conn, 'external_codes')
    conn.commit()

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
                    notes='', client_type='', countries=None, client_types=None,
                    display_currency=None):
    """client_types: list (v8 multi-select). client_type stays as the primary/legacy value.
    display_currency (v11): defaults to the one derived from the reseller's markets."""
    types = [t for t in (client_types or ([client_type] if client_type else [])) if t]
    primary = types[0] if types else (client_type or '')
    disp = display_currency or derive_display_currency(countries or [])
    conn = get_db()
    conn.execute("""INSERT INTO reseller_profiles
                    (user_id, company_name, expected_monthly_sales, assigned_tier_id,
                     registered_by, notes, client_type, display_currency)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (user_id, company_name, expected_sales, tier_id, registered_by, notes,
                  primary, disp))
    reseller_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for c in (countries or []):
        conn.execute("INSERT INTO reseller_countries (reseller_id, country) VALUES (?,?)", (reseller_id, c))
    for t in types:
        conn.execute("INSERT OR IGNORE INTO reseller_client_types (reseller_id, client_type) VALUES (?,?)",
                     (reseller_id, t))
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
    p['client_types'] = [r['client_type'] for r in conn.execute(
        "SELECT client_type FROM reseller_client_types WHERE reseller_id=?", (p['id'],))]
    if not p['client_types'] and p.get('client_type'):
        p['client_types'] = [p['client_type']]
    if profile['assigned_tier_id']:
        tier = conn.execute("SELECT * FROM tier_rules WHERE id=?", (profile['assigned_tier_id'],)).fetchone()
        p['tier'] = dict(tier) if tier else None
    else:
        p['tier'] = None
    p['overrides'] = {r['merchant']: r['share_pct'] for r in conn.execute(
        "SELECT merchant, share_pct FROM merchant_share_overrides WHERE reseller_id=?", (p['id'],))}
    conn.close()
    # v11: wallet is stored in SAR (base); expose it in the reseller's
    # display currency for every customer-facing screen.
    p.setdefault('display_currency', 'SAR')
    p['display_currency'] = p['display_currency'] or 'SAR'
    p['wallet_balance_display'] = round(
        convert_amount(p.get('wallet_balance', 0), 'SAR', p['display_currency']))
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
               t.margin_share_pct, t.color as tier_color, su.name as sales_name,
               (SELECT COUNT(*) FROM orders o WHERE o.reseller_id=cp.id) as orders_count,
               (SELECT COALESCE(SUM(total_cost),0) FROM orders o WHERE o.reseller_id=cp.id) as orders_value,
               (SELECT MAX(created_at) FROM orders o WHERE o.reseller_id=cp.id) as last_order_at
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
    out = []
    for r in rows:
        d = dict(r)
        d['lifecycle'] = _lifecycle_stage(d)
        out.append(d)
    return out


def _lifecycle_stage(r):
    """Prospect → Contracted → Active → At-Risk (lifecycle chip shown everywhere)."""
    if r.get('compliance_status') == 'warning':
        return 'at_risk'
    if r.get('contract_status') != 'contracted':
        return 'prospect'
    if r.get('orders_count', 0) > 0:
        return 'active'
    return 'contracted'


LIFECYCLE_LABELS = {
    'prospect':   ('Prospect', '#f59e0b'),
    'contracted': ('Contracted — not active', '#3b82f6'),
    'active':     ('Active', '#10b981'),
    'at_risk':    ('At-Risk', '#ef4444'),
}


def set_contract_status(reseller_id, status):
    conn = get_db()
    if status == 'contracted':
        conn.execute("""UPDATE reseller_profiles SET contract_status=?,
                        contracted_at=COALESCE(contracted_at, CURRENT_TIMESTAMP) WHERE id=?""",
                     (status, reseller_id))
    else:
        conn.execute("UPDATE reseller_profiles SET contract_status=? WHERE id=?", (status, reseller_id))
    conn.commit()
    conn.close()


def update_reseller_profile(reseller_id, client_type=None, countries=None, expected_sales=None,
                            display_currency=None):
    conn = get_db()
    if client_type is not None:
        conn.execute("UPDATE reseller_profiles SET client_type=? WHERE id=?", (client_type, reseller_id))
    if expected_sales is not None:
        conn.execute("UPDATE reseller_profiles SET expected_monthly_sales=? WHERE id=?", (expected_sales, reseller_id))
    if display_currency in DISPLAY_CURRENCIES:
        conn.execute("UPDATE reseller_profiles SET display_currency=? WHERE id=?", (display_currency, reseller_id))
    if countries is not None:
        conn.execute("DELETE FROM reseller_countries WHERE reseller_id=?", (reseller_id,))
        for c in countries:
            conn.execute("INSERT INTO reseller_countries (reseller_id, country) VALUES (?,?)", (reseller_id, c))
    conn.commit()
    conn.close()


# ── Products ─────────────────────────────────────────────────────

def get_products(country=None, region=None, category=None, merchant=None, search=None,
                 limit=None, include_inactive=False):
    conn = get_db()
    q = "SELECT * FROM products WHERE 1=1"
    params = []
    if not include_inactive:
        q += " AND is_active=1"
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
    Every price is then converted from the product's own currency into the
    reseller's single DISPLAY currency (v11) using Finance's FX rates, and
    rounded to WHOLE numbers (business rule), floored at cost. The returned
    `currency` field is the display currency; `orig_currency` keeps the source.
    """
    if products is None:
        products = get_products()
    tier = profile.get('tier')
    base_share = (tier['margin_share_pct'] / 100.0) if tier else 0.20
    overrides = profile.get('overrides', {})
    display_cur = profile.get('display_currency') or 'SAR'
    rates = get_fx_rates()

    enriched = []
    for p in products:
        share = overrides.get(p['merchant'], None)
        share = (share / 100.0) if share is not None else base_share
        disc = p['oc_margin'] * share
        c_price_orig = max(p['default_price'] - disc, p['cost'])   # product currency

        # Convert to the reseller's display currency, then round once.
        m = convert_amount(1.0, p['currency'], display_cur, rates)   # multiplier
        cost_disp = p['cost'] * m
        c_price = round(c_price_orig * m)
        if c_price < cost_disp:            # never below (converted) cost
            c_price = math.ceil(cost_disp)
        face = round(p['face_value'] * m)
        disc_disp = round(disc * m)
        saved = face - c_price
        pct = (saved / face * 100.0) if face > 0 else 0
        enriched.append({
            **p,
            'orig_currency': p['currency'],
            'currency': display_cur,
            'face_value': face,
            'client_price': c_price,
            'discount': disc_disp,
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


# Full canonical category list — form dropdowns must cover everything,
# not only the categories that happen to exist in the DB right now.
CANONICAL_CATEGORIES = [
    'Entertainment & Streaming', 'Entertainment & Leisure', 'Food & Delivery',
    'Gaming', 'Gift Cards & Vouchers', 'Health & Fitness', 'Shopping & Retail',
    'Software & Subscriptions', 'Telecom & Recharge', 'Transportation',
    'eSIM & Connectivity', 'Other',
]


def get_form_categories():
    """DB categories merged with the canonical list — a complete dropdown."""
    return sorted(set(get_all_categories()) | set(CANONICAL_CATEGORIES))


def get_all_countries_full():
    """Every country in the catalogue INCLUDING eSIM markets (ops forms)."""
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT country FROM products ORDER BY country").fetchall()
    conn.close()
    return [r['country'] for r in rows]


def get_all_regions_full():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT region FROM products ORDER BY region").fetchall()
    conn.close()
    return [r['region'] for r in rows]


def get_all_currencies():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT currency FROM products WHERE currency IS NOT NULL AND currency != '' ORDER BY currency").fetchall()
    conn.close()
    return [r['currency'] for r in rows]


# ── FX Rates (v7: wallet & reporting currency is SAR) ────────────

def get_fx_rates():
    conn = get_db()
    rows = conn.execute("SELECT * FROM currency_rates ORDER BY currency").fetchall()
    conn.close()
    return {r['currency']: r['rate_to_sar'] for r in rows}


def get_fx_rates_full():
    conn = get_db()
    rows = conn.execute("""SELECT cr.*, u.name as updated_by_name,
                                  (SELECT COUNT(*) FROM products p WHERE p.currency=cr.currency) as product_count
                           FROM currency_rates cr LEFT JOIN users u ON cr.updated_by=u.id
                           ORDER BY cr.currency""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_fx_rate(currency, rate, user_id):
    conn = get_db()
    conn.execute("""INSERT INTO currency_rates (currency, rate_to_sar, updated_by, updated_at)
                    VALUES (?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(currency) DO UPDATE SET rate_to_sar=excluded.rate_to_sar,
                        updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP""",
                 (currency, rate, user_id))
    conn.commit()
    conn.close()


def to_sar(amount, currency, rates=None):
    rates = rates or get_fx_rates()
    return amount * rates.get(currency or 'SAR', 1.0)


# ── Display currency (v11) ───────────────────────────────────────
# Resellers see the whole catalogue/wallet/orders in ONE currency. SAR
# stays the internal base for every report; the display currency is
# purely a presentation + wallet-facing layer, converted via the FX
# rates Finance maintains.

DISPLAY_CURRENCIES = ['SAR', 'USD']


def derive_display_currency(countries):
    """Saudi-based resellers see SAR; everyone else sees USD. Falls back to
    SAR (the company's home currency) when no market is set."""
    if not countries:
        return 'SAR'
    return 'SAR' if 'Saudi Arabia' in countries else 'USD'


def convert_amount(amount, from_cur, to_cur, rates=None):
    """Convert between any two currencies through the SAR base:
       amount_sar = amount * rate_to_sar[from]
       result     = amount_sar / rate_to_sar[to]
    Returns the raw (unrounded) figure; callers round for display."""
    if amount is None:
        return 0.0
    from_cur = from_cur or 'SAR'
    to_cur = to_cur or 'SAR'
    if from_cur == to_cur:
        return amount
    rates = rates or get_fx_rates()
    from_rate = rates.get(from_cur, 1.0)
    to_rate = rates.get(to_cur, 1.0)
    if not to_rate:
        return amount
    return amount * from_rate / to_rate


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

def create_topup_request(reseller_id, amount, bank_reference, receipt_file, note='',
                         orig_amount=None, orig_currency='SAR'):
    """amount is ALWAYS SAR (the wallet base). orig_amount/orig_currency record
    what the reseller actually entered in their display currency, for Finance."""
    conn = get_db()
    conn.execute("""INSERT INTO wallet_transactions
                    (reseller_id, type, amount, status, bank_reference, receipt_file, note,
                     orig_amount, orig_currency)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (reseller_id, 'topup', amount, 'pending', bank_reference, receipt_file, note,
                  orig_amount if orig_amount is not None else amount, orig_currency or 'SAR'))
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
    # BEGIN IMMEDIATE: two Finance users approving the same top-up at nearly
    # the same moment must not both pass the 'pending' check and both credit
    # the wallet — that would create money. Locking here makes the second
    # click's SELECT see the already-'approved'/'rejected' row and no-op.
    conn.execute("BEGIN IMMEDIATE")
    txn = conn.execute("SELECT * FROM wallet_transactions WHERE id=? AND status='pending'", (txn_id,)).fetchone()
    if not txn:
        conn.rollback()
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
    conn.execute("UPDATE forecasts SET status='reviewed', reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (fid,))
    conn.commit()
    conn.close()


# ── Orders ───────────────────────────────────────────────────────

def create_order(reseller_id, items):
    """items: list of {product_rowid, product_name, merchant, category, currency,
                       quantity, unit_price, unit_face}
    Line prices are in each product's own currency; the ORDER TOTAL and the
    WALLET are always SAR (v7): every line is converted at the stored FX rate.
    Returns (order_id, error_message)."""
    rates = get_fx_rates()
    total_cost = total_face = 0.0
    for it in items:
        rate = rates.get(it.get('currency') or 'SAR', 1.0)
        it['_fx'] = rate
        it['_line_sar'] = round(it['unit_price'] * it['quantity'] * rate, 2)
        total_cost += it['_line_sar']
        total_face += it['unit_face'] * it['quantity'] * rate
    total_cost = round(total_cost, 2)
    total_face = round(total_face, 2)
    conn = get_db()
    # BEGIN IMMEDIATE acquires the write lock up front, before the balance/
    # stock checks below. Without this, two concurrent orders for the same
    # reseller (or the same limited-stock gift card) can both read a stale
    # "sufficient" balance/stock, since a plain SELECT takes no lock — the
    # classic check-then-act race. Holding the lock from here through
    # commit() makes the whole check-and-deduct sequence atomic; a second
    # concurrent call simply waits (up to the busy_timeout in get_db()).
    conn.execute("BEGIN IMMEDIATE")
    bal = conn.execute("SELECT wallet_balance, display_currency FROM reseller_profiles WHERE id=?",
                       (reseller_id,)).fetchone()
    if not bal:
        conn.rollback()
        conn.close()
        return None, "Reseller not found."
    disp = bal['display_currency'] or 'SAR'
    # v8 Issuing Hub: never sell codes we don't have — validate stock first
    for it in items:
        prow = it.get('product_rowid')
        if prow:
            p = conn.execute("SELECT is_issued, product_name FROM products WHERE id=?", (prow,)).fetchone()
            if p and p['is_issued']:
                left = voucher_stock(conn, prow)
                if left < it['quantity']:
                    conn.rollback()
                    conn.close()
                    return None, (f"Only {left} gift-card codes left for '{p['product_name']}' "
                                  f"(you requested {it['quantity']}). Ops were alerted to restock.")
    if bal['wallet_balance'] < total_cost:
        conn.rollback()
        conn.close()
        # Show the shortfall in the reseller's own display currency.
        total_disp = round(convert_amount(total_cost, 'SAR', disp, rates))
        wallet_disp = round(convert_amount(bal['wallet_balance'], 'SAR', disp, rates))
        return None, (f"Insufficient wallet balance. Order total is {total_disp:,.0f} {disp} "
                      f"but wallet has {wallet_disp:,.0f} {disp}.")
    conn.execute("""INSERT INTO orders (reseller_id, total_cost, total_face, total_savings)
                    VALUES (?,?,?,?)""", (reseller_id, total_cost, total_face,
                                          round(total_face - total_cost, 2)))
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for it in items:
        conn.execute("""INSERT INTO order_items
                        (order_id, product_rowid, product_name, merchant, category, currency,
                         quantity, unit_price, unit_face, line_total, fx_rate, line_total_sar)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (oid, it.get('product_rowid'), it['product_name'], it['merchant'],
                      it.get('category'), it.get('currency'), it['quantity'],
                      it['unit_price'], it['unit_face'], it['unit_price'] * it['quantity'],
                      it['_fx'], it['_line_sar']))
    conn.execute("UPDATE reseller_profiles SET wallet_balance = wallet_balance - ? WHERE id=?",
                 (total_cost, reseller_id))
    conn.execute("""INSERT INTO wallet_transactions (reseller_id, type, amount, status, note)
                    VALUES (?,?,?,?,?)""",
                 (reseller_id, 'order', -total_cost, 'approved', f'Order #{oid}'))
    # v6: tie every sold unit to the supplier batch it came from (FIFO)
    _allocate_order_fifo(conn, oid)
    # v8: hand actual gift-card codes to the buyer for issued products
    touched_issued = _assign_issued_codes(conn, oid)
    # v9: run provider adapters for merchants that have one registered
    _run_fulfillment_adapters(conn, oid)
    conn.commit()
    conn.close()
    notify_low_issued_stock(touched_issued)
    send_webhook(reseller_id, 'order.placed',
                 {'order_id': oid, 'total_sar': total_cost})
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
    ym = ym or datetime.now(timezone.utc).strftime('%Y-%m')
    conn = get_db()
    rows = conn.execute("""SELECT oi.merchant, SUM(COALESCE(oi.line_total_sar, oi.line_total)) as v
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
        SELECT oi.merchant, SUM(COALESCE(oi.line_total_sar, oi.line_total)) as spend,
               SUM(oi.quantity) as qty,
               SUM(oi.unit_face*oi.quantity*oi.fx_rate) - SUM(COALESCE(oi.line_total_sar, oi.line_total)) as savings
        FROM order_items oi JOIN orders o ON oi.order_id=o.id
        WHERE o.reseller_id=? GROUP BY oi.merchant ORDER BY spend DESC""", (reseller_id,))]
    out['by_category'] = [dict(r) for r in conn.execute("""
        SELECT oi.category, SUM(COALESCE(oi.line_total_sar, oi.line_total)) as spend,
               SUM(oi.quantity) as qty
        FROM order_items oi JOIN orders o ON oi.order_id=o.id
        WHERE o.reseller_id=? GROUP BY oi.category ORDER BY spend DESC""", (reseller_id,))]
    out['monthly'] = [dict(r) for r in conn.execute("""
        SELECT strftime('%Y-%m', created_at) as ym, SUM(total_cost) as spend,
               SUM(total_savings) as savings, COUNT(*) as orders
        FROM orders WHERE reseller_id=? GROUP BY ym ORDER BY ym""", (reseller_id,))]
    out['top_products'] = [dict(r) for r in conn.execute("""
        SELECT oi.product_name, oi.merchant, SUM(oi.quantity) as qty,
               SUM(COALESCE(oi.line_total_sar, oi.line_total)) as spend
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


# ── BD Deal Pipeline (v8) ────────────────────────────────────────

def create_bd_request(dtype, title, merchant, supplier_name, details, expected_terms, created_by):
    conn = get_db()
    conn.execute("""INSERT INTO bd_requests (type, title, merchant, supplier_name, details, expected_terms, created_by)
                    VALUES (?,?,?,?,?,?,?)""",
                 (dtype, title, merchant, supplier_name, details, expected_terms, created_by))
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    notify(get_user_ids_by_role('ops'),
           "New deal from Business Development 📨",
           f"{BD_DEAL_TYPES.get(dtype, dtype)}: {title}. Please enter the data into the system.",
           "/deals")
    return rid


def get_bd_requests(created_by=None, status=None):
    conn = get_db()
    q = """SELECT br.*, cu.name as created_by_name, hu.name as handled_by_name
           FROM bd_requests br
           JOIN users cu ON br.created_by=cu.id
           LEFT JOIN users hu ON br.handled_by=hu.id WHERE 1=1"""
    params = []
    if created_by:
        q += " AND br.created_by=?"
        params.append(created_by)
    if status:
        q += " AND br.status=?"
        params.append(status)
    q += " ORDER BY CASE br.status WHEN 'submitted' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, br.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_bd_request_status(rid, status, handled_by, note=''):
    conn = get_db()
    req = conn.execute("SELECT * FROM bd_requests WHERE id=?", (rid,)).fetchone()
    if not req:
        conn.close()
        return None
    conn.execute("""UPDATE bd_requests SET status=?, handled_by=?, handler_note=?,
                    handled_at=CURRENT_TIMESTAMP WHERE id=?""", (status, handled_by, note, rid))
    conn.commit()
    conn.close()
    labels = {'in_progress': 'is being worked on 🔧', 'done': 'is DONE ✅', 'rejected': 'was rejected ❌'}
    notify(req['created_by'], f"Your deal '{req['title']}' {labels.get(status, status)}",
           note or '', "/deals")
    return dict(req)


# ── Ops: Product Management (v5) ─────────────────────────────────

def _log_price_change(conn, product_rowid, product_name, merchant, action,
                      field=None, old=None, new=None, source='manual', user_id=None):
    conn.execute("""INSERT INTO price_change_log
                    (product_rowid, product_name, merchant, action, field, old_value, new_value, source, changed_by)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (product_rowid, product_name, merchant, action, field,
                  str(old) if old is not None else None,
                  str(new) if new is not None else None, source, user_id))


def _margin_fields(cost, default_price):
    margin = round(default_price - cost, 4) if default_price > cost else 0
    pct = round((margin / default_price) * 100, 2) if default_price > 0 else 0
    return margin, pct


def add_product(data, user_id):
    """Ops adds a new product. Flagged as New Arrival for 30 days.
    Notifies resellers who previously ordered from this merchant."""
    cost = float(data.get('cost') or 0)
    price = float(data.get('default_price') or 0)
    face = float(data.get('face_value') or price)
    margin, pct = _margin_fields(cost, price)
    conn = get_db()
    conn.execute("""INSERT INTO products
                    (product_id, product_name, merchant, merchant_id, category, country, region,
                     currency, cost, default_price, face_value, oc_margin, oc_margin_pct,
                     popularity, is_new, is_active, added_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,CURRENT_TIMESTAMP)""",
                 (data.get('product_id', ''), data['product_name'], data['merchant'],
                  data.get('merchant_id', ''), data['category'], data['country'],
                  data.get('region', 'Global'), data.get('currency', 'SAR'),
                  cost, price, face, margin, pct, int(data.get('popularity') or 30)))
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _log_price_change(conn, pid, data['product_name'], data['merchant'], 'created',
                      field='default_price', new=price, user_id=user_id)
    # Resellers who bought from this merchant before get a heads-up
    buyers = conn.execute("""SELECT DISTINCT cp.user_id FROM order_items oi
                             JOIN orders o ON oi.order_id=o.id
                             JOIN reseller_profiles cp ON o.reseller_id=cp.id
                             WHERE oi.merchant=?""", (data['merchant'],)).fetchall()
    conn.commit()
    conn.close()
    notify([b['user_id'] for b in buyers], "New product from a merchant you buy 🆕",
           f"'{data['product_name']}' was just added under {data['merchant']}.",
           "/reseller/products")
    _check_margin_alert(pid, None, pct, data['product_name'], data['merchant'])
    return pid


def update_product(pid, fields, user_id, source='manual'):
    """Ops updates a product. Price fields are audited; margins recomputed.
    Returns dict of changes {field: (old, new)}."""
    conn = get_db()
    old = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not old:
        conn.close()
        return None
    old = dict(old)
    editable = ('product_name', 'merchant', 'category', 'country', 'region', 'currency',
                'cost', 'default_price', 'face_value', 'is_new')
    changes = {}
    new_vals = dict(old)
    for f in editable:
        if f in fields and fields[f] is not None and str(fields[f]) != '':
            val = float(fields[f]) if f in ('cost', 'default_price', 'face_value') else fields[f]
            if f == 'is_new':
                val = int(val)
            if val != old[f]:
                changes[f] = (old[f], val)
                new_vals[f] = val
    if not changes:
        conn.close()
        return {}
    margin, pct = _margin_fields(float(new_vals['cost']), float(new_vals['default_price']))
    conn.execute("""UPDATE products SET product_name=?, merchant=?, category=?, country=?, region=?,
                    currency=?, cost=?, default_price=?, face_value=?, is_new=?,
                    oc_margin=?, oc_margin_pct=? WHERE id=?""",
                 (new_vals['product_name'], new_vals['merchant'], new_vals['category'],
                  new_vals['country'], new_vals['region'], new_vals['currency'],
                  new_vals['cost'], new_vals['default_price'], new_vals['face_value'],
                  new_vals['is_new'], margin, pct, pid))
    for f, (o, n) in changes.items():
        _log_price_change(conn, pid, new_vals['product_name'], new_vals['merchant'],
                          'price_update' if f in ('cost', 'default_price', 'face_value') else 'edit',
                          field=f, old=o, new=n, source=source, user_id=user_id)
    conn.commit()
    conn.close()
    if any(f in changes for f in ('cost', 'default_price')):
        _check_margin_alert(pid, old['oc_margin_pct'], pct,
                            new_vals['product_name'], new_vals['merchant'])
    return changes


def _check_margin_alert(pid, old_pct, new_pct, name, merchant):
    """Alert BD + CCO when a price change leaves OneCard margin dangerously low."""
    low_floor = new_pct < MARGIN_ALERT_FLOOR_PCT
    halved = old_pct is not None and old_pct > 0 and new_pct < old_pct / 2
    if low_floor or halved:
        reason = (f"margin now {new_pct:.2f}% (below {MARGIN_ALERT_FLOOR_PCT}% floor)"
                  if low_floor else f"margin dropped from {old_pct:.2f}% to {new_pct:.2f}%")
        notify(get_user_ids_by_role('admin', 'cco'),
               "⚠️ Low margin after price update",
               f"'{name}' ({merchant}): {reason}. Review pricing.", "/ops/pricelog")


def set_product_active(pid, active, user_id):
    conn = get_db()
    p = conn.execute("SELECT product_name, merchant FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        return False
    conn.execute("UPDATE products SET is_active=? WHERE id=?", (1 if active else 0, pid))
    _log_price_change(conn, pid, p['product_name'], p['merchant'],
                      'activated' if active else 'deactivated', user_id=user_id)
    conn.commit()
    conn.close()
    return True


def get_price_log(limit=200, product_rowid=None):
    conn = get_db()
    q = """SELECT pl.*, u.name as changed_by_name FROM price_change_log pl
           LEFT JOIN users u ON pl.changed_by = u.id WHERE 1=1"""
    params = []
    if product_rowid:
        q += " AND pl.product_rowid=?"
        params.append(product_rowid)
    q += " ORDER BY pl.created_at DESC, pl.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ops_stats():
    conn = get_db()
    stats = {
        'total_products': conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        'active_products': conn.execute("SELECT COUNT(*) FROM products WHERE is_active=1").fetchone()[0],
        'inactive_products': conn.execute("SELECT COUNT(*) FROM products WHERE is_active=0").fetchone()[0],
        'new_products': conn.execute("SELECT COUNT(*) FROM products WHERE is_new=1").fetchone()[0],
        'low_margin': conn.execute("SELECT COUNT(*) FROM products WHERE is_active=1 AND oc_margin_pct < ?",
                                   (MARGIN_ALERT_FLOOR_PCT,)).fetchone()[0],
        'suppliers': conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0],
        'changes_this_week': conn.execute(
            "SELECT COUNT(*) FROM price_change_log WHERE created_at >= datetime('now','-7 days')").fetchone()[0],
    }
    stats['low_margin_products'] = [dict(r) for r in conn.execute(
        """SELECT id, product_name, merchant, cost, default_price, oc_margin_pct
           FROM products WHERE is_active=1 AND oc_margin_pct < ?
           ORDER BY oc_margin_pct LIMIT 15""", (MARGIN_ALERT_FLOOR_PCT,))]
    conn.close()
    return stats


# ── Ops: Bulk Price Import (v5) ──────────────────────────────────

def parse_price_file(path):
    """Parse a supplier/company price file (same column contract as seed_products.py).
    Returns (diffs, unmatched): rows whose cost/price/face differ from the DB."""
    import pandas as pd
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    cols = {c.lower(): c for c in df.columns}

    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    c_id = col('product id')
    c_cost = col('cost price')
    c_price = col('default reseller price')
    c_face = col('recommended retail price (resellers currency)')
    if not c_id or (not c_cost and not c_price):
        return None, "File must contain 'Product ID' and at least 'Cost Price' or 'Default Reseller Price' columns."

    conn = get_db()
    db_products = {str(r['product_id']): dict(r) for r in
                   conn.execute("SELECT id, product_id, product_name, merchant, cost, default_price, face_value FROM products")}
    conn.close()

    diffs, unmatched = [], 0
    for _, row in df.iterrows():
        ext_id = str(row.get(c_id, '')).strip().replace('.0', '')
        p = db_products.get(ext_id)
        if not p:
            unmatched += 1
            continue
        d = {'rowid': p['id'], 'product_id': ext_id, 'product_name': p['product_name'],
             'merchant': p['merchant']}
        changed = False
        for key, c in (('cost', c_cost), ('default_price', c_price), ('face_value', c_face)):
            if c is None:
                continue
            try:
                new = float(row.get(c))
            except (TypeError, ValueError):
                continue
            if new > 0 and abs(new - float(p[key])) > 0.005:
                d[f'old_{key}'] = p[key]
                d[f'new_{key}'] = new
                changed = True
        if changed:
            diffs.append(d)
    return {'diffs': diffs, 'unmatched': unmatched, 'total_rows': len(df)}, None


def apply_price_file(path, user_id):
    """Apply a parsed price file. Returns number of updated products."""
    parsed, err = parse_price_file(path)
    if err:
        return 0, err
    count = 0
    for d in parsed['diffs']:
        fields = {}
        for key in ('cost', 'default_price', 'face_value'):
            if f'new_{key}' in d:
                fields[key] = d[f'new_{key}']
        if fields:
            update_product(d['rowid'], fields, user_id, source='bulk_import')
            count += 1
    return count, None


# ── Ops: Suppliers (v5) ──────────────────────────────────────────

def get_suppliers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['merchants'] = [m['merchant'] for m in conn.execute(
            "SELECT merchant FROM supplier_merchants WHERE supplier_id=? ORDER BY merchant", (r['id'],))]
        if d['merchants']:
            ph = ','.join('?' * len(d['merchants']))
            d['product_count'] = conn.execute(
                f"SELECT COUNT(*) FROM products WHERE merchant IN ({ph})", d['merchants']).fetchone()[0]
        else:
            d['product_count'] = 0
        out.append(d)
    conn.close()
    return out


def upsert_supplier(sid, name, contact_person, email, phone, payment_terms, notes, merchants):
    conn = get_db()
    if sid:
        conn.execute("""UPDATE suppliers SET name=?, contact_person=?, email=?, phone=?,
                        payment_terms=?, notes=? WHERE id=?""",
                     (name, contact_person, email, phone, payment_terms, notes, sid))
    else:
        conn.execute("""INSERT INTO suppliers (name, contact_person, email, phone, payment_terms, notes)
                        VALUES (?,?,?,?,?,?)""",
                     (name, contact_person, email, phone, payment_terms, notes))
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("DELETE FROM supplier_merchants WHERE supplier_id=?", (sid,))
    for m in merchants:
        conn.execute("INSERT OR IGNORE INTO supplier_merchants (supplier_id, merchant) VALUES (?,?)", (sid, m))
    conn.commit()
    conn.close()
    return sid


def delete_supplier(sid):
    conn = get_db()
    conn.execute("DELETE FROM suppliers WHERE id=?", (sid,))
    conn.commit()
    conn.close()


def set_supplier_api_key(sid, key):
    conn = get_db()
    conn.execute("UPDATE suppliers SET api_key=? WHERE id=?", (key, sid))
    conn.commit()
    conn.close()


def get_supplier_by_api_key(key):
    conn = get_db()
    row = conn.execute("SELECT * FROM suppliers WHERE api_key=? AND api_key IS NOT NULL", (key,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── v6: Supplier Price Lists ─────────────────────────────────────

def upsert_supplier_price(supplier_id, product_rowid, cost, source='manual', changed_by=None):
    """Set a supplier's offer for a product. Logs history when the price changes.
    INTEGRATION NOTE: the supplier API endpoint calls this with source='api'."""
    conn = get_db()
    old = conn.execute("""SELECT supplier_cost FROM supplier_products
                          WHERE supplier_id=? AND product_rowid=?""",
                       (supplier_id, product_rowid)).fetchone()
    if old and abs(old['supplier_cost'] - cost) < 0.0005:
        conn.close()
        return False   # unchanged
    conn.execute("""INSERT INTO supplier_products (supplier_id, product_rowid, supplier_cost, source, updated_at)
                    VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(supplier_id, product_rowid)
                    DO UPDATE SET supplier_cost=excluded.supplier_cost, source=excluded.source,
                                  is_available=1, updated_at=CURRENT_TIMESTAMP""",
                 (supplier_id, product_rowid, cost, source))
    conn.execute("""INSERT INTO supplier_price_history
                    (supplier_id, product_rowid, old_cost, new_cost, source, changed_by)
                    VALUES (?,?,?,?,?,?)""",
                 (supplier_id, product_rowid, old['supplier_cost'] if old else None,
                  cost, source, changed_by))
    conn.commit()
    conn.close()
    return True


def set_offer_availability(supplier_id, product_rowid, available):
    conn = get_db()
    conn.execute("""UPDATE supplier_products SET is_available=?, updated_at=CURRENT_TIMESTAMP
                    WHERE supplier_id=? AND product_rowid=?""",
                 (1 if available else 0, supplier_id, product_rowid))
    conn.commit()
    conn.close()


def get_product_offers(product_rowid):
    """All supplier offers for one product, cheapest first."""
    conn = get_db()
    rows = conn.execute("""SELECT sp.*, s.name as supplier_name
                           FROM supplier_products sp JOIN suppliers s ON sp.supplier_id=s.id
                           WHERE sp.product_rowid=? ORDER BY sp.supplier_cost""",
                        (product_rowid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_best_source(product_rowid):
    """Cheapest AVAILABLE supplier offer for a product (or None)."""
    conn = get_db()
    row = conn.execute("""SELECT sp.supplier_id, sp.supplier_cost, s.name as supplier_name
                          FROM supplier_products sp JOIN suppliers s ON sp.supplier_id=s.id
                          WHERE sp.product_rowid=? AND sp.is_available=1
                          ORDER BY sp.supplier_cost LIMIT 1""", (product_rowid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_sourcing_matrix(search=None, merchant=None, only_multi=False):
    """Per product: every supplier offer + the cheapest, vs our standard cost.
    The heart of 'who is the cheapest supplier for every product / merchant'."""
    conn = get_db()
    q = """SELECT p.id, p.product_id, p.product_name, p.merchant, p.cost as std_cost,
                  p.currency, p.oc_margin_pct,
                  COUNT(sp.id) as offer_count,
                  MIN(CASE WHEN sp.is_available=1 THEN sp.supplier_cost END) as best_cost
           FROM products p
           JOIN supplier_products sp ON sp.product_rowid = p.id
           WHERE p.is_active=1"""
    params = []
    if search:
        q += " AND (p.product_name LIKE ? OR p.merchant LIKE ?)"
        params += [f'%{search}%', f'%{search}%']
    if merchant:
        q += " AND p.merchant=?"
        params.append(merchant)
    q += " GROUP BY p.id"
    if only_multi:
        q += " HAVING COUNT(sp.id) > 1"
    q += " ORDER BY p.merchant, p.product_name"
    rows = [dict(r) for r in conn.execute(q, params)]

    # attach offers + best supplier name
    ids = [r['id'] for r in rows]
    offers_by_product = {}
    if ids:
        ph = ','.join('?' * len(ids))
        for o in conn.execute(f"""SELECT sp.*, s.name as supplier_name
                                  FROM supplier_products sp JOIN suppliers s ON sp.supplier_id=s.id
                                  WHERE sp.product_rowid IN ({ph})
                                  ORDER BY sp.supplier_cost""", ids):
            offers_by_product.setdefault(o['product_rowid'], []).append(dict(o))
    conn.close()
    for r in rows:
        r['offers'] = offers_by_product.get(r['id'], [])
        best = next((o for o in r['offers'] if o['is_available']), None)
        r['best_supplier'] = best['supplier_name'] if best else None
        r['best_supplier_id'] = best['supplier_id'] if best else None
        r['saving_vs_std'] = round(r['std_cost'] - r['best_cost'], 3) if r['best_cost'] is not None else 0
    return rows


def get_merchant_sourcing_summary():
    """Per merchant: how much we'd save buying everything from the cheapest source."""
    matrix = get_sourcing_matrix()
    agg = {}
    for r in matrix:
        m = agg.setdefault(r['merchant'], {'merchant': r['merchant'], 'products': 0,
                                           'improvable': 0, 'total_saving': 0.0})
        m['products'] += 1
        if r['saving_vs_std'] > 0:
            m['improvable'] += 1
            m['total_saving'] += r['saving_vs_std']
    return sorted(agg.values(), key=lambda x: -x['total_saving'])


def bulk_import_supplier_prices(supplier_id, path, changed_by):
    """Import a supplier price file (columns: Product ID, Cost). Returns summary."""
    import pandas as pd
    df = pd.read_excel(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    id_col = next((c for c in df.columns if 'product' in c and 'id' in c), None)
    cost_col = next((c for c in df.columns if 'cost' in c or 'price' in c), None)
    if not id_col or not cost_col:
        return None, "File must contain 'Product ID' and 'Cost' columns."
    conn = get_db()
    db_products = {str(r['product_id']): r['id'] for r in
                   conn.execute("SELECT id, product_id FROM products")}
    conn.close()
    updated = unchanged = unmatched = 0
    for _, row in df.iterrows():
        ext = str(row.get(id_col, '')).strip().replace('.0', '')
        pid = db_products.get(ext)
        try:
            cost = float(row.get(cost_col))
        except (TypeError, ValueError):
            continue
        if not pid or cost <= 0:
            unmatched += 1
            continue
        if upsert_supplier_price(supplier_id, pid, cost, source='bulk', changed_by=changed_by):
            updated += 1
        else:
            unchanged += 1
    return {'updated': updated, 'unchanged': unchanged, 'unmatched': unmatched}, None


# ── v6: Purchase Batches (inventory lots + sourcing governance) ──

SOURCING_VARIANCE_TOLERANCE = 0.02   # alert BD/CCO if bought >2% above best available


def create_batch(supplier_id, product_rowid, quantity, unit_cost, invoice_ref='',
                 reason='', created_by=None):
    """Ops records a stock purchase. Captures the best available cost at this moment;
    overpaying beyond tolerance alerts BD + CCO automatically (governance)."""
    best = get_best_source(product_rowid)
    best_cost = best['supplier_cost'] if best else None
    variance = round((unit_cost - best_cost) * quantity, 2) if best_cost is not None else 0

    conn = get_db()
    p = conn.execute("SELECT product_name, merchant FROM products WHERE id=?", (product_rowid,)).fetchone()
    s = conn.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not p or not s:
        conn.close()
        return None, "Product or supplier not found."
    conn.execute("""INSERT INTO purchase_batches
                    (supplier_id, product_rowid, quantity, remaining_qty, unit_cost, total_cost,
                     best_cost_at_purchase, sourcing_variance, reason, invoice_ref, created_by)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                 (supplier_id, product_rowid, quantity, quantity, unit_cost,
                  round(unit_cost * quantity, 2), best_cost, max(variance, 0),
                  reason, invoice_ref, created_by))
    bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    # Finance reconciliation queue
    notify(get_user_ids_by_role('finance'),
           "New purchase batch to reconcile 🧾",
           f"Batch #{bid}: {quantity:,} × '{p['product_name']}' from {s['name']} "
           f"({unit_cost * quantity:,.0f} total, invoice {invoice_ref or '—'}).",
           "/finance/batches")

    # Governance: bought above the cheapest available source
    if best_cost is not None and unit_cost > best_cost * (1 + SOURCING_VARIANCE_TOLERANCE):
        notify(get_user_ids_by_role('admin', 'cco'),
               "⚠️ Sourcing variance — bought above best price",
               f"Batch #{bid}: '{p['product_name']}' bought from {s['name']} at {unit_cost:,.2f} "
               f"while {best['supplier_name']} offers {best_cost:,.2f}. "
               f"Overpaid ≈ {variance:,.0f} on this batch."
               + (f" Ops reason: {reason}" if reason else " No reason given."),
               "/sourcing-intel")
    return bid, None


def get_batches(status=None, product_rowid=None, supplier_id=None, limit=200):
    conn = get_db()
    q = """SELECT b.*, p.product_name, p.merchant, p.currency, s.name as supplier_name,
                  u.name as created_by_name, ru.name as reconciled_by_name
           FROM purchase_batches b
           JOIN products p ON b.product_rowid=p.id
           JOIN suppliers s ON b.supplier_id=s.id
           LEFT JOIN users u ON b.created_by=u.id
           LEFT JOIN users ru ON b.reconciled_by=ru.id
           WHERE 1=1"""
    params = []
    if status:
        q += " AND b.status=?"
        params.append(status)
    if product_rowid:
        q += " AND b.product_rowid=?"
        params.append(product_rowid)
    if supplier_id:
        q += " AND b.supplier_id=?"
        params.append(supplier_id)
    q += " ORDER BY b.created_at DESC, b.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reconcile_batch(batch_id, ok, reviewer_id, note=''):
    """Finance matches the batch against the supplier invoice."""
    conn = get_db()
    b = conn.execute("""SELECT b.*, p.product_name, s.name as supplier_name
                        FROM purchase_batches b
                        JOIN products p ON b.product_rowid=p.id
                        JOIN suppliers s ON b.supplier_id=s.id
                        WHERE b.id=? AND b.status='awaiting_reconciliation'""", (batch_id,)).fetchone()
    if not b:
        conn.close()
        return None
    status = 'reconciled' if ok else 'disputed'
    conn.execute("""UPDATE purchase_batches SET status=?, reconciled_by=?,
                    reconciled_at=CURRENT_TIMESTAMP, reconcile_note=? WHERE id=?""",
                 (status, reviewer_id, note, batch_id))
    conn.commit()
    conn.close()
    if b['created_by']:
        verdict = "reconciled ✅" if ok else "DISPUTED ❌"
        notify(b['created_by'], f"Batch #{batch_id} {verdict}",
               f"{b['quantity']:,} × '{b['product_name']}' from {b['supplier_name']} — "
               + (note or "matched against invoice."), "/ops/batches")
    return dict(b)


def _allocate_order_fifo(conn, order_id):
    """Consume purchase batches oldest-first for every item of an order.
    Units without recorded stock get a NULL-batch allocation at standard cost."""
    items = conn.execute("""SELECT oi.id, oi.product_rowid, oi.quantity, p.cost as std_cost
                            FROM order_items oi LEFT JOIN products p ON oi.product_rowid=p.id
                            WHERE oi.order_id=?""", (order_id,)).fetchall()
    for it in items:
        need = it['quantity']
        batches = conn.execute("""SELECT id, remaining_qty, unit_cost FROM purchase_batches
                                  WHERE product_rowid=? AND remaining_qty>0
                                  ORDER BY created_at, id""", (it['product_rowid'],)).fetchall()
        for b in batches:
            if need <= 0:
                break
            take = min(need, b['remaining_qty'])
            conn.execute("""INSERT INTO order_item_allocations (order_item_id, batch_id, quantity, unit_cost)
                            VALUES (?,?,?,?)""", (it['id'], b['id'], take, b['unit_cost']))
            conn.execute("UPDATE purchase_batches SET remaining_qty=remaining_qty-? WHERE id=?",
                         (take, b['id']))
            need -= take
        if need > 0:   # no (more) recorded stock → unsourced at standard cost
            conn.execute("""INSERT INTO order_item_allocations (order_item_id, batch_id, quantity, unit_cost)
                            VALUES (?,NULL,?,?)""", (it['id'], need, it['std_cost'] or 0))


# ── v6: Sourcing intelligence (BD / CCO) ─────────────────────────

def get_sales_by_supplier(ym=None):
    """'Which supplier are we actually selling from?' — allocations grouped by supplier."""
    conn = get_db()
    q = """SELECT COALESCE(s.name, '— Unsourced (no batch) —') as supplier_name,
                  b.supplier_id,
                  SUM(a.quantity) as units,
                  SUM(a.quantity * a.unit_cost * COALESCE(cr.rate_to_sar, 1)) as cogs,
                  SUM(a.quantity * oi.unit_price * COALESCE(cr.rate_to_sar, 1)) as revenue
           FROM order_item_allocations a
           JOIN order_items oi ON a.order_item_id=oi.id
           JOIN orders o ON oi.order_id=o.id
           LEFT JOIN currency_rates cr ON cr.currency = oi.currency
           LEFT JOIN purchase_batches b ON a.batch_id=b.id
           LEFT JOIN suppliers s ON b.supplier_id=s.id"""
    params = []
    if ym:
        q += " WHERE strftime('%Y-%m', o.created_at)=?"
        params.append(ym)
    q += " GROUP BY supplier_name ORDER BY revenue DESC"
    rows = [dict(r) for r in conn.execute(q, params)]
    conn.close()
    total_rev = sum(r['revenue'] or 0 for r in rows) or 1
    for r in rows:
        r['profit'] = round((r['revenue'] or 0) - (r['cogs'] or 0), 2)
        r['share_pct'] = round((r['revenue'] or 0) / total_rev * 100, 1)
    return rows


def get_supplier_scorecards():
    """Per supplier: offers, competitiveness, spend, variance and realized profit."""
    conn = get_db()
    suppliers = [dict(r) for r in conn.execute("SELECT id, name FROM suppliers ORDER BY name")]
    for s in suppliers:
        sid = s['id']
        s['offers'] = conn.execute(
            "SELECT COUNT(*) FROM supplier_products WHERE supplier_id=? AND is_available=1", (sid,)).fetchone()[0]
        s['best_offers'] = conn.execute("""
            SELECT COUNT(*) FROM supplier_products sp
            WHERE sp.supplier_id=? AND sp.is_available=1
              AND sp.supplier_cost = (SELECT MIN(sp2.supplier_cost) FROM supplier_products sp2
                                      WHERE sp2.product_rowid=sp.product_rowid AND sp2.is_available=1)""",
            (sid,)).fetchone()[0]
        row = conn.execute("""SELECT COUNT(*) as batches, COALESCE(SUM(total_cost),0) as spend,
                                     COALESCE(SUM(sourcing_variance),0) as variance
                              FROM purchase_batches WHERE supplier_id=?""", (sid,)).fetchone()
        s['batches'], s['spend'], s['variance'] = row['batches'], row['spend'], row['variance']
        sold = conn.execute("""SELECT COALESCE(SUM(a.quantity),0) as units,
                                      COALESCE(SUM(a.quantity*a.unit_cost*COALESCE(cr.rate_to_sar,1)),0) as cogs,
                                      COALESCE(SUM(a.quantity*oi.unit_price*COALESCE(cr.rate_to_sar,1)),0) as revenue
                               FROM order_item_allocations a
                               JOIN purchase_batches b ON a.batch_id=b.id
                               JOIN order_items oi ON a.order_item_id=oi.id
                               LEFT JOIN currency_rates cr ON cr.currency = oi.currency
                               WHERE b.supplier_id=?""", (sid,)).fetchone()
        s['units_sold'] = sold['units']
        s['realized_profit'] = round(sold['revenue'] - sold['cogs'], 2)
    conn.close()
    return suppliers


def get_margin_improvements(limit=25):
    """BD view: products whose newest batch is cheaper than the previous one —
    'our margin improved since batch #X from supplier Y on date Z'."""
    conn = get_db()
    rows = conn.execute("""
        SELECT b.product_rowid, p.product_name, p.merchant, p.currency,
               b.id as batch_id, b.unit_cost as new_cost, b.created_at as improved_at,
               s.name as supplier_name,
               (SELECT b2.unit_cost FROM purchase_batches b2
                WHERE b2.product_rowid=b.product_rowid AND b2.created_at < b.created_at
                ORDER BY b2.created_at DESC, b2.id DESC LIMIT 1) as prev_cost
        FROM purchase_batches b
        JOIN products p ON b.product_rowid=p.id
        JOIN suppliers s ON b.supplier_id=s.id
        WHERE b.id IN (SELECT MAX(b3.id) FROM purchase_batches b3 GROUP BY b3.product_rowid)
        ORDER BY b.created_at DESC LIMIT ?""", (limit * 3,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if d['prev_cost'] is not None and d['new_cost'] < d['prev_cost']:
            d['saving_per_unit'] = round(d['prev_cost'] - d['new_cost'], 3)
            d['improvement_pct'] = round((d['prev_cost'] - d['new_cost']) / d['prev_cost'] * 100, 1)
            out.append(d)
    return out[:limit]


def get_units_sold_30d():
    """product_rowid -> units sold in the last 30 days (drives 'estimated monthly saving')."""
    conn = get_db()
    rows = conn.execute("""SELECT oi.product_rowid, SUM(oi.quantity) as units
                           FROM order_items oi JOIN orders o ON oi.order_id=o.id
                           WHERE o.created_at >= datetime('now','-30 days')
                             AND oi.product_rowid IS NOT NULL
                           GROUP BY oi.product_rowid""").fetchall()
    conn.close()
    return {r['product_rowid']: r['units'] for r in rows}


def get_sourcing_kpis():
    conn = get_db()
    k = {}
    k['products_with_offers'] = conn.execute(
        "SELECT COUNT(DISTINCT product_rowid) FROM supplier_products WHERE is_available=1").fetchone()[0]
    k['multi_source_products'] = conn.execute("""
        SELECT COUNT(*) FROM (SELECT product_rowid FROM supplier_products WHERE is_available=1
                              GROUP BY product_rowid HAVING COUNT(*)>1)""").fetchone()[0]
    k['potential_saving'] = conn.execute("""
        SELECT COALESCE(SUM((p.cost - x.best) * COALESCE(cr.rate_to_sar, 1)),0) FROM products p
        JOIN (SELECT product_rowid, MIN(supplier_cost) as best FROM supplier_products
              WHERE is_available=1 GROUP BY product_rowid) x ON x.product_rowid=p.id
        LEFT JOIN currency_rates cr ON cr.currency = p.currency
        WHERE p.is_active=1 AND x.best < p.cost""").fetchone()[0]
    k['open_variance'] = conn.execute("""
        SELECT COALESCE(SUM(b.sourcing_variance * COALESCE(cr.rate_to_sar, 1)),0)
        FROM purchase_batches b JOIN products p ON b.product_rowid=p.id
        LEFT JOIN currency_rates cr ON cr.currency = p.currency""").fetchone()[0]
    k['unreconciled'] = conn.execute(
        "SELECT COUNT(*) FROM purchase_batches WHERE status='awaiting_reconciliation'").fetchone()[0]
    k['stock_value'] = conn.execute("""
        SELECT COALESCE(SUM(b.remaining_qty * b.unit_cost * COALESCE(cr.rate_to_sar, 1)),0)
        FROM purchase_batches b JOIN products p ON b.product_rowid=p.id
        LEFT JOIN currency_rates cr ON cr.currency = p.currency""").fetchone()[0]
    conn.close()
    return k


# ── Governance: Targets + Team Performance (v5) ──────────────────

def upsert_sales_target(sales_user_id, ym, target_new_resellers, target_sales_value):
    conn = get_db()
    conn.execute("""INSERT INTO sales_targets (sales_user_id, ym, target_new_resellers, target_sales_value)
                    VALUES (?,?,?,?)
                    ON CONFLICT(sales_user_id, ym)
                    DO UPDATE SET target_new_resellers=excluded.target_new_resellers,
                                  target_sales_value=excluded.target_sales_value""",
                 (sales_user_id, ym, target_new_resellers, target_sales_value))
    conn.commit()
    conn.close()


def get_team_performance(ym=None):
    """Full sales-team governance snapshot for a month ('YYYY-MM').

    Per sales manager: registration funnel (registered → contracted → activated),
    monthly new resellers + order value vs targets, commitment attainment,
    discount request stats and forecast responsiveness.
    """
    ym = ym or datetime.now(timezone.utc).strftime('%Y-%m')
    conn = get_db()
    sales_users = conn.execute("SELECT id, name, email FROM users WHERE role='sales' ORDER BY name").fetchall()
    targets = {t['sales_user_id']: dict(t) for t in
               conn.execute("SELECT * FROM sales_targets WHERE ym=?", (ym,))}

    team = []
    for su in sales_users:
        uid = su['id']
        m = {'user_id': uid, 'name': su['name'], 'email': su['email']}

        m['registered_total'] = conn.execute(
            "SELECT COUNT(*) FROM reseller_profiles WHERE registered_by=?", (uid,)).fetchone()[0]
        m['registered_month'] = conn.execute(
            "SELECT COUNT(*) FROM reseller_profiles WHERE registered_by=? AND strftime('%Y-%m',created_at)=?",
            (uid, ym)).fetchone()[0]
        m['contracted'] = conn.execute(
            "SELECT COUNT(*) FROM reseller_profiles WHERE registered_by=? AND contract_status='contracted'",
            (uid,)).fetchone()[0]
        m['activated'] = conn.execute(
            """SELECT COUNT(DISTINCT cp.id) FROM reseller_profiles cp
               JOIN orders o ON o.reseller_id=cp.id WHERE cp.registered_by=?""", (uid,)).fetchone()[0]
        m['at_risk'] = conn.execute(
            "SELECT COUNT(*) FROM reseller_profiles WHERE registered_by=? AND compliance_status='warning'",
            (uid,)).fetchone()[0]

        m['orders_value_month'] = conn.execute(
            """SELECT COALESCE(SUM(o.total_cost),0) FROM orders o
               JOIN reseller_profiles cp ON o.reseller_id=cp.id
               WHERE cp.registered_by=? AND strftime('%Y-%m',o.created_at)=?""", (uid, ym)).fetchone()[0]
        m['orders_value_total'] = conn.execute(
            """SELECT COALESCE(SUM(o.total_cost),0) FROM orders o
               JOIN reseller_profiles cp ON o.reseller_id=cp.id
               WHERE cp.registered_by=?""", (uid,)).fetchone()[0]

        m['commitment_expected'] = conn.execute(
            """SELECT COALESCE(SUM(expected_monthly_sales),0) FROM reseller_profiles
               WHERE registered_by=? AND contract_status='contracted'""", (uid,)).fetchone()[0]

        dr = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as approved
               FROM discount_requests WHERE requested_by=?""", (uid,)).fetchone()
        m['discount_requests'] = dr['total'] or 0
        m['discount_approved'] = dr['approved'] or 0

        m['forecasts_pending'] = conn.execute(
            """SELECT COUNT(*) FROM forecasts f JOIN reseller_profiles cp ON f.reseller_id=cp.id
               WHERE cp.registered_by=? AND f.status='submitted'""", (uid,)).fetchone()[0]
        m['avg_review_hours'] = conn.execute(
            """SELECT AVG((julianday(f.reviewed_at)-julianday(f.created_at))*24)
               FROM forecasts f JOIN reseller_profiles cp ON f.reseller_id=cp.id
               WHERE cp.registered_by=? AND f.reviewed_at IS NOT NULL""", (uid,)).fetchone()[0]

        # Derived rates
        m['contract_rate'] = round(m['contracted'] / m['registered_total'] * 100) if m['registered_total'] else 0
        m['activation_rate'] = round(m['activated'] / m['contracted'] * 100) if m['contracted'] else 0
        m['commitment_attainment'] = (round(m['orders_value_month'] / m['commitment_expected'] * 100)
                                      if m['commitment_expected'] else None)

        t = targets.get(uid)
        m['target_new'] = t['target_new_resellers'] if t else None
        m['target_value'] = t['target_sales_value'] if t else None
        m['target_new_pct'] = (round(m['registered_month'] / t['target_new_resellers'] * 100)
                               if t and t['target_new_resellers'] else None)
        m['target_value_pct'] = (round(m['orders_value_month'] / t['target_sales_value'] * 100)
                                 if t and t['target_sales_value'] else None)
        team.append(m)
    conn.close()
    return team


def get_sales_scorecard(uid, ym=None):
    """A single sales manager's own performance view (feedback loop)."""
    team = get_team_performance(ym)
    for m in team:
        if m['user_id'] == uid:
            return m
    return None


# ── SLA Nudges (v5, runs on the daily tick) ──────────────────────

def _nudge_once(key, user_ids, title, body, link):
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM sla_nudges WHERE key=?", (key,)).fetchone()
    if exists:
        conn.close()
        return False
    conn.execute("INSERT INTO sla_nudges (key) VALUES (?)", (key,))
    conn.commit()
    conn.close()
    notify(user_ids, title, body, link)
    return True


def run_sla_nudges():
    """Forecast with no review for 3+ days → remind its sales manager.
    Wallet top-up pending 24h+ → remind the finance team."""
    conn = get_db()
    stale_forecasts = conn.execute(
        """SELECT f.id, cp.company_name, cp.registered_by FROM forecasts f
           JOIN reseller_profiles cp ON f.reseller_id=cp.id
           WHERE f.status='submitted' AND f.created_at <= datetime('now','-3 days')""").fetchall()
    stale_topups = conn.execute(
        """SELECT wt.id, wt.amount, cp.company_name FROM wallet_transactions wt
           JOIN reseller_profiles cp ON wt.reseller_id=cp.id
           WHERE wt.type='topup' AND wt.status='pending'
             AND wt.created_at <= datetime('now','-1 day')""").fetchall()
    stale_batches = conn.execute(
        """SELECT b.id, b.total_cost, s.name as supplier_name FROM purchase_batches b
           JOIN suppliers s ON b.supplier_id=s.id
           WHERE b.status='awaiting_reconciliation'
             AND b.created_at <= datetime('now','-3 days')""").fetchall()
    conn.close()

    finance_ids = get_user_ids_by_role('finance')
    for f in stale_forecasts:
        _nudge_once(f"forecast:{f['id']}", [f['registered_by']],
                    "⏰ Forecast awaiting your review",
                    f"The purchase plan from {f['company_name']} has been waiting 3+ days.",
                    f"/sales/forecasts/{f['id']}")
    for t in stale_topups:
        _nudge_once(f"topup:{t['id']}", finance_ids,
                    "⏰ Top-up pending verification 24h+",
                    f"{t['company_name']}'s transfer of {t['amount']:,.0f} SAR is still unverified.",
                    "/finance")
    for b in stale_batches:
        _nudge_once(f"batch:{b['id']}", finance_ids,
                    "⏰ Purchase batch unreconciled 3+ days",
                    f"Batch #{b['id']} from {b['supplier_name']} ({b['total_cost']:,.0f}) still needs "
                    "invoice reconciliation.", "/finance/batches")


def expire_new_flags():
    """New Arrival flag automatically expires 30 days after a product is added."""
    conn = get_db()
    conn.execute("""UPDATE products SET is_new=0
                    WHERE is_new=1 AND added_at IS NOT NULL
                      AND added_at <= datetime('now','-30 days')""")
    conn.commit()
    conn.close()


# ── Database backups (v10 hardening) ──────────────────────────────

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backups')
BACKUP_RETENTION_DAYS = 14


def backup_database():
    """Consistent hot-backup of the live SQLite file via the sqlite3 backup
    API (safe to run while the app is serving requests — it doesn't lock out
    writers for more than a moment). Backups older than BACKUP_RETENTION_DAYS
    are pruned. Returns the backup file path.

    INTEGRATION NOTE: this covers "no backups at all" for the prototype.
    A real deployment should also ship these off-box (S3/blob storage) —
    a local backup next to the live DB doesn't survive a disk failure."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')
    dest_path = os.path.join(BACKUP_DIR, f'onecard_{stamp}.db')
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION_DAYS)
    for fname in os.listdir(BACKUP_DIR):
        if not (fname.startswith('onecard_') and fname.endswith('.db')):
            continue
        fpath = os.path.join(BACKUP_DIR, fname)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
            if mtime < cutoff:
                os.remove(fpath)
        except OSError:
            pass
    return dest_path


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if fname.startswith('onecard_') and fname.endswith('.db'):
            fpath = os.path.join(BACKUP_DIR, fname)
            out.append({'name': fname,
                       'size_kb': round(os.path.getsize(fpath) / 1024, 1),
                       'created_at': datetime.fromtimestamp(
                           os.path.getmtime(fpath), tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})
    return out


# ── Issuing Hub (v8): we issue partner gift cards & sell them ────

ISSUED_LOW_STOCK_THRESHOLD = 20

REGION_MAP_SIMPLE = {
    'Saudi Arabia': 'GCC', 'UAE': 'GCC', 'Kuwait': 'GCC', 'Qatar': 'GCC',
    'Bahrain': 'GCC', 'Oman': 'GCC', 'Egypt': 'North Africa', 'Jordan': 'Levant',
    'Global': 'Global',
}


def upsert_issuing_partner(pid, name, contact_person, email, phone, share_pct, status, notes, created_by):
    conn = get_db()
    if pid:
        conn.execute("""UPDATE issuing_partners SET name=?, contact_person=?, email=?, phone=?,
                        partner_share_pct=?, status=?, notes=? WHERE id=?""",
                     (name, contact_person, email, phone, share_pct, status, notes, pid))
    else:
        conn.execute("""INSERT INTO issuing_partners
                        (name, contact_person, email, phone, partner_share_pct, status, notes, created_by)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (name, contact_person, email, phone, share_pct, status, notes, created_by))
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return pid


def get_issuing_partners():
    conn = get_db()
    rows = conn.execute("""
        SELECT ip.*,
               (SELECT COUNT(*) FROM products p WHERE p.issuing_partner_id=ip.id) as product_count,
               (SELECT COUNT(*) FROM issued_vouchers v JOIN products p ON v.product_rowid=p.id
                WHERE p.issuing_partner_id=ip.id AND v.status='available') as stock_available,
               (SELECT COUNT(*) FROM issued_vouchers v JOIN products p ON v.product_rowid=p.id
                WHERE p.issuing_partner_id=ip.id AND v.status IN ('sold','redeemed')) as sold_total
        FROM issuing_partners ip ORDER BY ip.name""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_issued_product(partner_id, name, sku, face_value, selling_price, currency,
                          category, country, created_by):
    """An issued product IS a catalogue product (merchant = partner name) so tiers,
    orders, wallets and analytics all work unchanged.
    cost = partner payout per unit = selling_price x partner share %."""
    conn = get_db()
    partner = conn.execute("SELECT * FROM issuing_partners WHERE id=?", (partner_id,)).fetchone()
    if not partner:
        conn.close()
        return None, "Partner not found."
    payout = round(selling_price * partner['partner_share_pct'] / 100.0, 4)
    margin, pct = _margin_fields(payout, selling_price)
    conn.execute("""INSERT INTO products
                    (product_id, product_name, merchant, merchant_id, category, country, region,
                     currency, cost, default_price, face_value, oc_margin, oc_margin_pct,
                     popularity, is_new, is_active, added_at, is_issued, issuing_partner_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,55,1,1,CURRENT_TIMESTAMP,1,?)""",
                 (sku, name, partner['name'], f'ISSUED-{partner_id}', category, country,
                  REGION_MAP_SIMPLE.get(country, 'GCC'), currency, payout, selling_price,
                  face_value, margin, pct, partner_id))
    prow = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _log_price_change(conn, prow, name, partner['name'], 'created', field='default_price',
                      new=selling_price, source='issuing', user_id=created_by)
    conn.commit()
    conn.close()
    return prow, None


def generate_voucher_batch(product_rowid, quantity, generated_by, note=''):
    """Generate unique voucher codes + PINs for an issued product."""
    import secrets as _sec
    conn = get_db()
    p = conn.execute("SELECT product_name, is_issued FROM products WHERE id=?", (product_rowid,)).fetchone()
    if not p or not p['is_issued']:
        conn.close()
        return None, "Not an issued product."
    ref = f"GC-{datetime.now(timezone.utc).strftime('%y%m%d')}-{_sec.token_hex(2).upper()}"
    conn.execute("""INSERT INTO issued_batches (product_rowid, batch_ref, quantity, generated_by, note)
                    VALUES (?,?,?,?,?)""", (product_rowid, ref, quantity, generated_by, note))
    bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for _ in range(quantity):
        code = '-'.join(_sec.token_hex(2).upper() for _ in range(4))
        pin = str(_sec.randbelow(900000) + 100000)
        conn.execute("""INSERT INTO issued_vouchers (batch_id, product_rowid, code, code_hash, pin)
                        VALUES (?,?,?,?,?)""",
                     (bid, product_rowid, _enc(code), _code_hash(code), _enc(pin)))
    conn.commit()
    conn.close()
    return {'batch_id': bid, 'batch_ref': ref}, None


def get_issued_products(partner_id=None):
    conn = get_db()
    q = """SELECT p.id, p.product_name, p.product_id as sku, p.face_value, p.default_price,
                  p.cost as partner_payout, p.currency, p.category, p.country, p.is_active,
                  ip.name as partner_name, ip.partner_share_pct,
                  (SELECT COUNT(*) FROM issued_vouchers v WHERE v.product_rowid=p.id AND v.status='available') as available,
                  (SELECT COUNT(*) FROM issued_vouchers v WHERE v.product_rowid=p.id AND v.status='sold') as sold,
                  (SELECT COUNT(*) FROM issued_vouchers v WHERE v.product_rowid=p.id AND v.status='redeemed') as redeemed
           FROM products p JOIN issuing_partners ip ON p.issuing_partner_id=ip.id
           WHERE p.is_issued=1"""
    params = []
    if partner_id:
        q += " AND p.issuing_partner_id=?"
        params.append(partner_id)
    q += " ORDER BY ip.name, p.product_name"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def voucher_stock(conn, product_rowid):
    return conn.execute("""SELECT COUNT(*) FROM issued_vouchers
                           WHERE product_rowid=? AND status='available'""", (product_rowid,)).fetchone()[0]


def _assign_issued_codes(conn, order_id):
    """Attach actual voucher codes to every issued-product line of an order.
    Stock is validated before the order is accepted."""
    items = conn.execute("""SELECT oi.id, oi.product_rowid, oi.quantity FROM order_items oi
                            JOIN products p ON oi.product_rowid=p.id
                            WHERE oi.order_id=? AND p.is_issued=1""", (order_id,)).fetchall()
    touched = []
    for it in items:
        vids = [r['id'] for r in conn.execute(
            """SELECT id FROM issued_vouchers WHERE product_rowid=? AND status='available'
               ORDER BY id LIMIT ?""", (it['product_rowid'], it['quantity']))]
        for vid in vids:
            conn.execute("""UPDATE issued_vouchers SET status='sold', order_item_id=?,
                            sold_at=CURRENT_TIMESTAMP WHERE id=?""", (it['id'], vid))
        if vids:
            conn.execute("UPDATE order_items SET fulfillment_status='delivered' WHERE id=?",
                         (it['id'],))
        touched.append(it['product_rowid'])
    return touched


def notify_low_issued_stock(product_rowids):
    """After a sale, warn Ops once per day per product when stock runs low."""
    if not product_rowids:
        return
    conn = get_db()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for pid in set(product_rowids):
        left = voucher_stock(conn, pid)
        if left <= ISSUED_LOW_STOCK_THRESHOLD:
            p = conn.execute("SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
            _nudge_once(f"lowstock:{pid}:{today}", get_user_ids_by_role('ops'),
                        "⚠️ Gift card stock running low",
                        f"Only {left} codes left for '{p['product_name']}'. Generate a new batch.",
                        "/ops/issuing/products")
    conn.close()


def get_order_codes(order_id):
    """order_item_id -> list of {code, pin, status} for issued products in an order."""
    conn = get_db()
    rows = conn.execute("""SELECT v.order_item_id, v.code, v.pin, v.status
                           FROM issued_vouchers v
                           JOIN order_items oi ON v.order_item_id=oi.id
                           WHERE oi.order_id=? ORDER BY v.id""", (order_id,)).fetchall()
    conn.close()
    out = {}
    for r in rows:
        d = dict(r)
        d['code'], d['pin'] = _dec(d['code']), _dec(d['pin'])
        out.setdefault(r['order_item_id'], []).append(d)
    return out


def check_voucher(code):
    conn = get_db()
    row = conn.execute("""SELECT v.*, p.product_name, p.face_value, p.currency,
                                 ip.name as partner_name, b.batch_ref
                          FROM issued_vouchers v
                          JOIN products p ON v.product_rowid=p.id
                          LEFT JOIN issuing_partners ip ON p.issuing_partner_id=ip.id
                          JOIN issued_batches b ON v.batch_id=b.id
                          WHERE v.code_hash=?""", (_code_hash(code),)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d['code'], d['pin'] = _dec(d['code']), _dec(d['pin'])
    return d


def redeem_voucher(code):
    conn = get_db()
    # BEGIN IMMEDIATE: two near-simultaneous redemption attempts on the same
    # code (accidental double-scan, network retry) must not both see status
    # ='sold' and both succeed — the second must see 'redeemed' and be
    # rejected, not just get lucky sequencing.
    conn.execute("BEGIN IMMEDIATE")
    v = conn.execute("SELECT id, status FROM issued_vouchers WHERE code_hash=?",
                     (_code_hash(code),)).fetchone()
    if not v or v['status'] != 'sold':
        conn.rollback()
        conn.close()
        return False, ("Code not found." if not v else f"Cannot redeem - status is '{v['status']}'.")
    conn.execute("UPDATE issued_vouchers SET status='redeemed', redeemed_at=CURRENT_TIMESTAMP WHERE id=?",
                 (v['id'],))
    conn.commit()
    conn.close()
    return True, "Redeemed successfully."


def get_partner_report():
    """Per partner economics: sold units, gross revenue (SAR), partner payout, OneCard profit."""
    conn = get_db()
    rows = conn.execute("""
        SELECT ip.id, ip.name, ip.partner_share_pct,
               COUNT(DISTINCT p.id) as products,
               COALESCE(SUM(CASE WHEN v.status IN ('sold','redeemed') THEN 1 ELSE 0 END),0) as units_sold,
               COALESCE(SUM(CASE WHEN v.status='redeemed' THEN 1 ELSE 0 END),0) as units_redeemed,
               COALESCE(SUM(CASE WHEN v.status='available' THEN 1 ELSE 0 END),0) as stock_left
        FROM issuing_partners ip
        LEFT JOIN products p ON p.issuing_partner_id=ip.id
        LEFT JOIN issued_vouchers v ON v.product_rowid=p.id
        GROUP BY ip.id ORDER BY units_sold DESC""").fetchall()
    report = [dict(r) for r in rows]
    for r in report:
        money = conn.execute("""
            SELECT COALESCE(SUM(v.cnt * oi.unit_price * oi.fx_rate),0) as revenue,
                   COALESCE(SUM(v.cnt * p.cost * oi.fx_rate),0) as payout
            FROM (SELECT order_item_id, COUNT(*) as cnt FROM issued_vouchers
                  WHERE status IN ('sold','redeemed') AND order_item_id IS NOT NULL
                  GROUP BY order_item_id) v
            JOIN order_items oi ON oi.id=v.order_item_id
            JOIN products p ON oi.product_rowid=p.id
            WHERE p.issuing_partner_id=?""", (r['id'],)).fetchone()
        r['revenue_sar'] = round(money['revenue'], 2)
        r['payout_sar'] = round(money['payout'], 2)
        r['profit_sar'] = round(money['revenue'] - money['payout'], 2)
    conn.close()
    return report


# ── Integration API (v9) ─────────────────────────────────────────

def set_reseller_api(reseller_id, rotate_key=False, webhook_url=None):
    """Generate/rotate a reseller API key and/or set their webhook URL.
    Returns the (new) key when rotated, else None."""
    import secrets as _sec
    conn = get_db()
    new_key = None
    if rotate_key:
        new_key = f"rk_{_sec.token_hex(24)}"
        conn.execute("UPDATE reseller_profiles SET api_key=? WHERE id=?", (new_key, reseller_id))
    if webhook_url is not None:
        conn.execute("UPDATE reseller_profiles SET webhook_url=? WHERE id=?",
                     (webhook_url.strip() or None, reseller_id))
    conn.commit()
    conn.close()
    return new_key


def get_reseller_by_api_key(key):
    """Full reseller profile (tier + overrides) for a valid API key, else None."""
    if not key:
        return None
    conn = get_db()
    row = conn.execute("SELECT user_id FROM reseller_profiles WHERE api_key=?", (key,)).fetchone()
    conn.close()
    return get_reseller_profile(row['user_id']) if row else None


def idempotency_lookup(key, reseller_id):
    conn = get_db()
    row = conn.execute("SELECT order_id FROM api_idempotency WHERE key=? AND reseller_id=?",
                       (key, reseller_id)).fetchone()
    conn.close()
    return row['order_id'] if row else None


def idempotency_store(key, reseller_id, order_id):
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO api_idempotency (key, reseller_id, order_id) VALUES (?,?,?)",
                 (key, reseller_id, order_id))
    conn.commit()
    conn.close()


# ── Fulfillment adapters (v9) ────────────────────────────────────
# THE integration pattern for the technical team:
#   register_fulfillment_adapter('<Merchant Name>', fn)
#   fn(order_item: dict) -> list[{'code': str, 'pin': str|None}]  (raise on failure)
# When a reseller order contains that merchant's products, the adapter is
# called at checkout; returned codes are stored in external_codes and the
# line becomes fulfillment_status='delivered'. No adapter -> stays 'external'
# (your provisioning worker fulfills it later and calls deliver_external_codes).

FULFILLMENT_ADAPTERS = {}


def register_fulfillment_adapter(merchant, fn):
    FULFILLMENT_ADAPTERS[merchant] = fn


def deliver_external_codes(order_item_id, codes, provider):
    """Store provider codes for an order line and mark it delivered."""
    conn = get_db()
    for c in codes:
        conn.execute("""INSERT INTO external_codes (order_item_id, code, pin, provider)
                        VALUES (?,?,?,?)""",
                     (order_item_id, _enc(c['code']), _enc(c.get('pin')), provider))
    conn.execute("UPDATE order_items SET fulfillment_status='delivered' WHERE id=?", (order_item_id,))
    conn.commit()
    conn.close()


def _run_fulfillment_adapters(conn, order_id):
    """Called inside create_order's transaction for non-issued lines."""
    items = conn.execute("""SELECT oi.id, oi.product_rowid, oi.product_name, oi.merchant,
                                   oi.quantity, oi.currency
                            FROM order_items oi JOIN products p ON oi.product_rowid=p.id
                            WHERE oi.order_id=? AND COALESCE(p.is_issued,0)=0""",
                         (order_id,)).fetchall()
    for it in items:
        fn = FULFILLMENT_ADAPTERS.get(it['merchant'])
        if not fn:
            continue
        try:
            codes = fn(dict(it))
        except Exception:
            continue   # stays 'external'; the retry worker owns failures
        for c in codes[:it['quantity']]:
            conn.execute("""INSERT INTO external_codes (order_item_id, code, pin, provider)
                            VALUES (?,?,?,?)""",
                         (it['id'], _enc(c['code']), _enc(c.get('pin')), f"adapter:{it['merchant']}"))
        conn.execute("UPDATE order_items SET fulfillment_status='delivered' WHERE id=?", (it['id'],))


def _demo_adapter(item):
    """Reference adapter (see API_GUIDE.md). A real one calls the provider API:
    resp = requests.post(PROVIDER_URL, json={'sku': item['product_name'],
                                             'qty': item['quantity']}, ...)
    and returns resp.json()['codes']."""
    import secrets as _sec
    return [{'code': f"EXT-{_sec.token_hex(6).upper()}", 'pin': str(_sec.randbelow(9000) + 1000)}
            for _ in range(item['quantity'])]


# Demo registration so the pattern is visible end-to-end in this prototype
register_fulfillment_adapter('Nexon EU Store', _demo_adapter)


def get_all_codes_for_order(order_id):
    """Unified codes per order line: issuing-hub vouchers + external provider codes."""
    out = get_order_codes(order_id)                     # issued vouchers
    conn = get_db()
    rows = conn.execute("""SELECT ec.order_item_id, ec.code, ec.pin, 'delivered' as status
                           FROM external_codes ec
                           JOIN order_items oi ON ec.order_item_id=oi.id
                           WHERE oi.order_id=? ORDER BY ec.id""", (order_id,)).fetchall()
    conn.close()
    for r in rows:
        out.setdefault(r['order_item_id'], []).append(
            {'code': _dec(r['code']), 'pin': _dec(r['pin']), 'status': r['status']})
    return out


# ── Webhooks (v9): best-effort, logged ───────────────────────────

def send_webhook(reseller_id, event, payload):
    """POST an event to the reseller's webhook URL (3s timeout, never raises).
    INTEGRATION NOTE: production should queue + retry with signatures."""
    conn = get_db()
    row = conn.execute("SELECT webhook_url FROM reseller_profiles WHERE id=?", (reseller_id,)).fetchone()
    url = row['webhook_url'] if row else None
    conn.close()
    if not url:
        return
    import json as _json
    import urllib.request as _rq
    status = None
    try:
        req = _rq.Request(url, data=_json.dumps({'event': event, 'data': payload}).encode(),
                          headers={'Content-Type': 'application/json',
                                   'X-OneCard-Event': event})
        status = _rq.urlopen(req, timeout=3).status
    except Exception:
        status = 0
    conn = get_db()
    conn.execute("""INSERT INTO webhook_deliveries (reseller_id, event, url, status_code)
                    VALUES (?,?,?,?)""", (reseller_id, event, url, status))
    conn.commit()
    conn.close()


# ── Partner Portal (v8.1): the business we issue cards FOR ──────

def create_partner_login(partner_id, email, password, contact_name):
    """Ops creates the portal login for an issuing partner (role='partner')."""
    uid = create_user(email, password, contact_name, 'partner')
    if not uid:
        return None
    conn = get_db()
    conn.execute("UPDATE issuing_partners SET portal_user_id=? WHERE id=?", (uid, partner_id))
    conn.commit()
    conn.close()
    return uid


def get_partner_by_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM issuing_partners WHERE portal_user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_partner_stats(partner_id):
    """Everything the partner sees on their dashboard — scoped to THEM only."""
    conn = get_db()
    programs = [dict(r) for r in conn.execute("""
        SELECT p.id, p.product_name, p.face_value, p.currency,
               (SELECT COUNT(*) FROM issued_vouchers v WHERE v.product_rowid=p.id AND v.status='available') as stock,
               (SELECT COUNT(*) FROM issued_vouchers v WHERE v.product_rowid=p.id AND v.status='sold') as sold,
               (SELECT COUNT(*) FROM issued_vouchers v WHERE v.product_rowid=p.id AND v.status='redeemed') as redeemed
        FROM products p WHERE p.issuing_partner_id=? ORDER BY p.product_name""", (partner_id,))]
    money = conn.execute("""
        SELECT COALESCE(SUM(x.cnt * p.cost * oi.fx_rate),0) as payout,
               COALESCE(SUM(x.cnt),0) as units
        FROM (SELECT order_item_id, COUNT(*) as cnt FROM issued_vouchers
              WHERE status IN ('sold','redeemed') AND order_item_id IS NOT NULL
              GROUP BY order_item_id) x
        JOIN order_items oi ON oi.id=x.order_item_id
        JOIN products p ON oi.product_rowid=p.id
        WHERE p.issuing_partner_id=?""", (partner_id,)).fetchone()
    recent = [dict(r) for r in conn.execute("""
        SELECT v.code, v.redeemed_at, p.product_name, p.face_value, p.currency
        FROM issued_vouchers v JOIN products p ON v.product_rowid=p.id
        WHERE p.issuing_partner_id=? AND v.status='redeemed'
        ORDER BY v.redeemed_at DESC LIMIT 10""", (partner_id,))]
    for r in recent:
        r['code'] = _dec(r['code'])
    conn.close()
    totals = {
        'programs': len(programs),
        'stock': sum(x['stock'] for x in programs),
        'sold': sum(x['sold'] for x in programs),
        'redeemed': sum(x['redeemed'] for x in programs),
        'units_paid': money['units'],
        'earnings_sar': round(money['payout'], 2),
    }
    return programs, totals, recent


def get_partner_statement(partner_id):
    """Monthly settlement view: units sold, gross sales, the partner's payout (SAR)."""
    conn = get_db()
    rows = [dict(r) for r in conn.execute("""
        SELECT strftime('%Y-%m', v.sold_at) as ym,
               COUNT(*) as units,
               ROUND(SUM(oi.unit_price * oi.fx_rate), 2) as gross_sar,
               ROUND(SUM(p.cost * oi.fx_rate), 2) as payout_sar
        FROM issued_vouchers v
        JOIN order_items oi ON v.order_item_id=oi.id
        JOIN products p ON v.product_rowid=p.id
        WHERE p.issuing_partner_id=? AND v.status IN ('sold','redeemed')
        GROUP BY ym ORDER BY ym DESC""", (partner_id,))]
    conn.close()
    return rows


def partner_check_voucher(partner_id, code):
    """Look a code up — but ONLY if it belongs to this partner's programs."""
    v = check_voucher(code)
    if not v:
        return None, "Code not found."
    conn = get_db()
    owner = conn.execute("SELECT issuing_partner_id FROM products WHERE id=?",
                         (v['product_rowid'],)).fetchone()
    conn.close()
    if not owner or owner['issuing_partner_id'] != partner_id:
        return None, "This code does not belong to your gift cards."
    return v, None


def partner_redeem(partner_id, code, pin):
    """Cashier redemption: code + PIN must match, card must be sold and theirs."""
    v, err = partner_check_voucher(partner_id, code)
    if err:
        return False, err
    if str(v.get('pin') or '') != str(pin or '').strip():
        return False, "Wrong PIN — check the card details with the customer."
    if v['status'] == 'available':
        return False, "This card was never sold — it cannot be redeemed."
    if v['status'] == 'redeemed':
        return False, f"Already used on {v['redeemed_at']}."
    if v['status'] != 'sold':
        return False, f"Cannot redeem — card status is '{v['status']}'."
    return redeem_voucher(code)


# ── Tier Compliance Automation ───────────────────────────────────

def _month_bounds(d=None):
    d = d or datetime.now(timezone.utc).date()
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
    today = datetime.now(timezone.utc).date()
    last_run = conn.execute("SELECT value FROM app_meta WHERE key='compliance_last_run'").fetchone()
    if not force and last_run and last_run['value'] == today.isoformat():
        conn.close()
        return []
    conn.execute("INSERT INTO app_meta (key,value) VALUES ('compliance_last_run',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today.isoformat(),))
    conn.commit()

    # Daily housekeeping piggybacks on the same once-a-day gate
    run_sla_nudges()
    expire_new_flags()
    try:
        backup_database()
    except OSError:
        pass   # disk/permission issue shouldn't take the whole request down

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
        ('ops@onecard.com',     'Ops2025!',     'Operations Team', 'ops'),
        ('bd@onecard.com',      'Bd2025!',      'Business Development', 'bd'),
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
