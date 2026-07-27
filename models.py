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
import secrets as _secrets_mod


def new_api_key(prefix='rk'):
    """A fresh API key. v26: the API is an always-on channel, so every reseller
    and supplier gets one automatically at creation."""
    return f"{prefix}_{_secrets_mod.token_hex(24)}"
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
    # v20: WAL lets readers run concurrently with a writer (default 'delete'
    # mode blocks all readers during any write). Persistent on the DB file.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
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

        -- ── v13: Contracts (upload draft → client signs → activate) ──
        CREATE TABLE IF NOT EXISTS contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            uploaded_by INTEGER REFERENCES users(id),
            file_draft TEXT,
            file_signed TEXT,
            status TEXT NOT NULL DEFAULT 'sent',
                -- sent | signed_uploaded | pending_cco | active | void
            account_type TEXT NOT NULL DEFAULT 'prepaid',
            credit_limit REAL NOT NULL DEFAULT 0,
            credit_disbursement TEXT NOT NULL DEFAULT 'full',
            credit_tranche REAL NOT NULL DEFAULT 0,
            settlement_terms_days INTEGER NOT NULL DEFAULT 30,
            billing_cycle TEXT NOT NULL DEFAULT 'monthly',
            note TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            signed_at TIMESTAMP,
            activated_at TIMESTAMP,
            activated_by INTEGER REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS contract_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
            actor_id INTEGER REFERENCES users(id),
            event TEXT NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_contracts_reseller ON contracts(reseller_id);
        CREATE INDEX IF NOT EXISTS idx_contract_events ON contract_events(contract_id);

        -- ── v14: Statements (credit/consignment settlement) ──
        CREATE TABLE IF NOT EXISTS statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'issued',   -- issued | paid | overdue | void
            period_start TEXT,
            period_end TEXT,
            due_at TEXT,
            issued_by INTEGER REFERENCES users(id),
            issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_statements_reseller ON statements(reseller_id, status);

        -- v14: additional-credit requests (Sales -> CCO + Finance approve)
        CREATE TABLE IF NOT EXISTS credit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            requested_by INTEGER REFERENCES users(id),
            amount REAL NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'permanent',  -- permanent | temporary
            expires_on TEXT,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
            cco_by INTEGER REFERENCES users(id),
            finance_by INTEGER REFERENCES users(id),
            decided_at TIMESTAMP,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_credit_requests ON credit_requests(reseller_id, status);

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
            est_value REAL NOT NULL DEFAULT 0,
            -- v27: timing dimension so Ops can plan WHEN stock is needed
            needed_by TEXT,                              -- date the reseller wants it available
            period TEXT NOT NULL DEFAULT 'monthly',      -- 'one_off' | 'monthly' (recurring run-rate)
            confidence TEXT NOT NULL DEFAULT 'medium'    -- 'high' | 'medium' | 'low'
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

        -- v18: merchants hidden from a specific reseller's catalogue (e.g. a
        -- competitor of that reseller). Products of these merchants are removed
        -- from everything that reseller sees (portal, API, recommended, forecast).
        CREATE TABLE IF NOT EXISTS reseller_hidden_merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
            merchant TEXT NOT NULL,
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

        -- ── v19: Competitor price intelligence (Sales -> BD) ──
        CREATE TABLE IF NOT EXISTS competitor_intel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submitted_by INTEGER NOT NULL REFERENCES users(id),
            merchant TEXT,
            product_name TEXT,
            competitor_name TEXT,
            competitor_price REAL,
            our_price REAL,
            currency TEXT DEFAULT 'SAR',
            note TEXT,
            attachment_file TEXT,
            status TEXT NOT NULL DEFAULT 'new',   -- new | reviewing | actioned | dismissed
            bd_note TEXT,
            reviewed_by INTEGER REFERENCES users(id),
            reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_competitor_intel ON competitor_intel(status, created_at);

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

        -- ── v21: Payments WE make to suppliers (settle what we owe) ──
        CREATE TABLE IF NOT EXISTS supplier_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
            amount REAL NOT NULL,                 -- SAR paid to the supplier
            method TEXT,                          -- bank transfer / etc.
            reference TEXT,
            note TEXT,
            paid_by INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_supplier_payments ON supplier_payments(supplier_id, created_at);

        -- ── v22: Supplier statements (period bill of what we owe them) ──
        CREATE TABLE IF NOT EXISTS supplier_statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
            amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'issued',   -- issued | paid | overdue | void
            period_start TEXT,
            period_end TEXT,
            due_at TEXT,
            issued_by INTEGER REFERENCES users(id),
            issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_supplier_statements ON supplier_statements(supplier_id, status);

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
        -- v20: reseller_profiles is looked up per-request by user_id (suspension +
        -- NDA guards) and per API call by api_key; index both to avoid table scans.
        CREATE INDEX IF NOT EXISTS idx_reseller_user ON reseller_profiles(user_id);
        CREATE INDEX IF NOT EXISTS idx_reseller_apikey ON reseller_profiles(api_key);
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
        # v12: auto-suspend prospects who neither sign a contract nor buy within
        # PROSPECT_SUSPEND_DAYS. Suspended accounts cannot log in but remain
        # visible to their sales manager, who can reactivate them.
        'is_suspended': "ALTER TABLE reseller_profiles ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0",
        'suspended_at': "ALTER TABLE reseller_profiles ADD COLUMN suspended_at TIMESTAMP",
        'auto_suspend_at': "ALTER TABLE reseller_profiles ADD COLUMN auto_suspend_at TEXT",
        # v13: account/payment model. prepaid = today's wallet; credit = draw
        # against a limit and settle per cycle; consignment = accrue per draw and
        # settle on a period statement. SAR base throughout.
        'account_type': "ALTER TABLE reseller_profiles ADD COLUMN account_type TEXT NOT NULL DEFAULT 'prepaid'",
        'credit_limit': "ALTER TABLE reseller_profiles ADD COLUMN credit_limit REAL NOT NULL DEFAULT 0",
        'credit_released': "ALTER TABLE reseller_profiles ADD COLUMN credit_released REAL NOT NULL DEFAULT 0",
        'credit_outstanding': "ALTER TABLE reseller_profiles ADD COLUMN credit_outstanding REAL NOT NULL DEFAULT 0",
        'credit_disbursement': "ALTER TABLE reseller_profiles ADD COLUMN credit_disbursement TEXT NOT NULL DEFAULT 'full'",
        'credit_tranche': "ALTER TABLE reseller_profiles ADD COLUMN credit_tranche REAL NOT NULL DEFAULT 0",
        'settlement_terms_days': "ALTER TABLE reseller_profiles ADD COLUMN settlement_terms_days INTEGER NOT NULL DEFAULT 30",
        'billing_cycle': "ALTER TABLE reseller_profiles ADD COLUMN billing_cycle TEXT NOT NULL DEFAULT 'monthly'",
        'credit_frozen': "ALTER TABLE reseller_profiles ADD COLUMN credit_frozen INTEGER NOT NULL DEFAULT 0",
        # v14: base limit (the permanent limit) so a TEMPORARY bump can revert to
        # it when it expires; last_statement_at drives the auto statement cycle.
        'credit_limit_base': "ALTER TABLE reseller_profiles ADD COLUMN credit_limit_base REAL NOT NULL DEFAULT 0",
        'credit_temp_until': "ALTER TABLE reseller_profiles ADD COLUMN credit_temp_until TEXT",
        'last_statement_at': "ALTER TABLE reseller_profiles ADD COLUMN last_statement_at TEXT",
        # v17 (CRM): contact phone (captured at registration) + commercial
        # registration number (the customer's primary identifier, captured at
        # contract time). Both also power duplicate-customer prevention.
        'contact_phone': "ALTER TABLE reseller_profiles ADD COLUMN contact_phone TEXT",
        'commercial_reg_no': "ALTER TABLE reseller_profiles ADD COLUMN commercial_reg_no TEXT",
        # v19: when the reseller accepted the confidentiality notice (NDA) on
        # first login. Null = must accept before using the portal.
        'nda_accepted_at': "ALTER TABLE reseller_profiles ADD COLUMN nda_accepted_at TIMESTAMP",
    }
    just_added_display_cur = 'display_currency' not in cols
    just_added_suspend = 'auto_suspend_at' not in cols
    just_added_nda = 'nda_accepted_at' not in cols
    for col, sql in add.items():
        if col not in cols:
            conn.execute(sql)
    # v19: existing resellers are treated as having accepted the NDA already —
    # only NEW clients (created after this upgrade) get the acceptance gate.
    if just_added_nda:
        conn.commit()
        conn.execute("UPDATE reseller_profiles SET nda_accepted_at=CURRENT_TIMESTAMP")
        conn.commit()
    # Backfill display_currency for existing resellers from their markets.
    if just_added_display_cur:
        conn.commit()
        for r in conn.execute("SELECT id FROM reseller_profiles").fetchall():
            countries = [x['country'] for x in conn.execute(
                "SELECT country FROM reseller_countries WHERE reseller_id=?", (r['id'],))]
            conn.execute("UPDATE reseller_profiles SET display_currency=? WHERE id=?",
                         (derive_display_currency(countries), r['id']))
        conn.commit()
    # Backfill the auto-suspend deadline for existing resellers. Give them a
    # fresh PROSPECT_SUSPEND_DAYS window from the upgrade date rather than their
    # original registration, so the migration never mass-suspends old prospects
    # on the first sweep (contracted/active ones are exempt at sweep time anyway).
    if just_added_suspend:
        conn.commit()
        conn.execute("UPDATE reseller_profiles SET auto_suspend_at=?", (_suspend_deadline(),))
        conn.commit()

    # v13: the wallet_transactions.type CHECK originally allowed only
    # topup/order/adjustment. Credit & consignment need credit_draw /
    # consignment_draw / settlement. Rebuild the table once to drop the CHECK
    # (SQLite can't ALTER a constraint) while preserving all rows.
    wt_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='wallet_transactions'").fetchone()
    if wt_sql and 'CHECK(type IN' in (wt_sql['sql'] or ''):
        # Ensure the v11 columns exist on the OLD table before we copy them over
        # (on a fresh DB the CREATE has the CHECK but not these columns yet).
        wtc = {r['name'] for r in conn.execute("PRAGMA table_info(wallet_transactions)")}
        if 'orig_amount' not in wtc:
            conn.execute("ALTER TABLE wallet_transactions ADD COLUMN orig_amount REAL")
        if 'orig_currency' not in wtc:
            conn.execute("ALTER TABLE wallet_transactions ADD COLUMN orig_currency TEXT DEFAULT 'SAR'")
        conn.commit()
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE wallet_transactions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reseller_id INTEGER NOT NULL REFERENCES reseller_profiles(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                bank_reference TEXT,
                receipt_file TEXT,
                note TEXT,
                reviewed_by INTEGER REFERENCES users(id),
                reviewed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                orig_amount REAL,
                orig_currency TEXT DEFAULT 'SAR'
            );
            INSERT INTO wallet_transactions_new
                (id, reseller_id, type, amount, status, bank_reference, receipt_file, note,
                 reviewed_by, reviewed_at, created_at, orig_amount, orig_currency)
                SELECT id, reseller_id, type, amount, status, bank_reference, receipt_file, note,
                       reviewed_by, reviewed_at, created_at,
                       COALESCE(orig_amount, amount), COALESCE(orig_currency, 'SAR')
                FROM wallet_transactions;
            DROP TABLE wallet_transactions;
            ALTER TABLE wallet_transactions_new RENAME TO wallet_transactions;
            PRAGMA foreign_keys=ON;
        """)
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
    # v21: how WE pay a supplier (mirror of the customer account models):
    #   prepaid     = we pay upfront (advance), no payable accrues
    #   credit      = the supplier grants US a limit; each purchase accrues to
    #                 our_outstanding and we settle per cycle
    #   consignment = they place stock with us; we owe as we buy/draw, settle later
    for col, ddl in (
        ('account_type', "ALTER TABLE suppliers ADD COLUMN account_type TEXT NOT NULL DEFAULT 'prepaid'"),
        ('our_credit_limit', "ALTER TABLE suppliers ADD COLUMN our_credit_limit REAL NOT NULL DEFAULT 0"),
        ('our_outstanding', "ALTER TABLE suppliers ADD COLUMN our_outstanding REAL NOT NULL DEFAULT 0"),
        ('settlement_terms_days', "ALTER TABLE suppliers ADD COLUMN settlement_terms_days INTEGER NOT NULL DEFAULT 30"),
        ('billing_cycle', "ALTER TABLE suppliers ADD COLUMN billing_cycle TEXT NOT NULL DEFAULT 'monthly'"),
        ('is_active', "ALTER TABLE suppliers ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"),
        # v22: cycle marker for auto-issuing supplier statements
        ('last_statement_at', "ALTER TABLE suppliers ADD COLUMN last_statement_at TEXT"),
        # v25: for a CONSIGNMENT supplier, when we owe them — 'sale' (when we sell
        # the unit) or 'redemption' (when the end customer actually uses the card).
        ('consignment_settle_on', "ALTER TABLE suppliers ADD COLUMN consignment_settle_on TEXT NOT NULL DEFAULT 'sale'"),
    ):
        if col not in scols:
            conn.execute(ddl)
    # v22: how a purchase batch was acquired (api pull vs offline stock)
    bcols = {r['name'] for r in conn.execute("PRAGMA table_info(purchase_batches)")}
    if 'method' not in bcols:
        conn.execute("ALTER TABLE purchase_batches ADD COLUMN method TEXT NOT NULL DEFAULT 'offline'")
    # v25: track how many of a sold allocation have been redeemed (drives
    # consignment 'redemption' accrual without double-counting)
    aacols = {r['name'] for r in conn.execute("PRAGMA table_info(order_item_allocations)")}
    if 'redeemed_qty' not in aacols:
        conn.execute("ALTER TABLE order_item_allocations ADD COLUMN redeemed_qty INTEGER NOT NULL DEFAULT 0")
    conn.commit()

    # v26: the API is an always-on channel — every reseller and supplier that
    # predates auto-provisioning gets a key now (unique per row).
    for r in conn.execute("SELECT id FROM reseller_profiles WHERE api_key IS NULL OR api_key=''").fetchall():
        conn.execute("UPDATE reseller_profiles SET api_key=? WHERE id=?", (new_api_key('rk'), r['id']))
    for r in conn.execute("SELECT id FROM suppliers WHERE api_key IS NULL OR api_key=''").fetchall():
        conn.execute("UPDATE suppliers SET api_key=? WHERE id=?", (new_api_key('sk'), r['id']))
    conn.commit()

    # v27: forecast lines gain a TIMING dimension so Operations can plan WHEN
    # stock is needed (not just how much). needed_by = the date the reseller
    # wants it available; period = one_off vs a recurring monthly run-rate;
    # confidence = how sure the reseller is. Existing lines predate timing, so
    # they default to a recurring monthly baseline at medium confidence — they
    # keep flowing through every Ops view exactly as before (no date attached).
    ficols = {r['name'] for r in conn.execute("PRAGMA table_info(forecast_items)")}
    for col, ddl in (
        ('needed_by', "ALTER TABLE forecast_items ADD COLUMN needed_by TEXT"),
        ('period', "ALTER TABLE forecast_items ADD COLUMN period TEXT NOT NULL DEFAULT 'monthly'"),
        ('confidence', "ALTER TABLE forecast_items ADD COLUMN confidence TEXT NOT NULL DEFAULT 'medium'"),
    ):
        if col not in ficols:
            conn.execute(ddl)
    conn.commit()

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
    # v16: per-reseller secret used to HMAC-sign webhook payloads
    if 'webhook_secret' not in rcols:
        conn.execute("ALTER TABLE reseller_profiles ADD COLUMN webhook_secret TEXT")

    # 7c-2. v16: turn webhook_deliveries into a durable retry queue
    wdcols = {r['name'] for r in conn.execute("PRAGMA table_info(webhook_deliveries)")}
    for col, ddl in (('payload', "ALTER TABLE webhook_deliveries ADD COLUMN payload TEXT"),
                     ('status', "ALTER TABLE webhook_deliveries ADD COLUMN status TEXT NOT NULL DEFAULT 'delivered'"),
                     ('attempts', "ALTER TABLE webhook_deliveries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
                     ('next_attempt_at', "ALTER TABLE webhook_deliveries ADD COLUMN next_attempt_at TEXT"),
                     ('last_error', "ALTER TABLE webhook_deliveries ADD COLUMN last_error TEXT"),
                     ('delivered_at', "ALTER TABLE webhook_deliveries ADD COLUMN delivered_at TIMESTAMP")):
        if col not in wdcols:
            conn.execute(ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_webhook_due ON webhook_deliveries(status, next_attempt_at)")

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
    # v14: link a settlement transaction to the statement it pays
    if 'statement_id' not in wtcols:
        conn.execute("ALTER TABLE wallet_transactions ADD COLUMN statement_id INTEGER")
    conn.commit()

    # v17 (CRM): commercial registration number captured on the contract
    ctcols = {r['name'] for r in conn.execute("PRAGMA table_info(contracts)")}
    if 'commercial_reg_no' not in ctcols:
        conn.execute("ALTER TABLE contracts ADD COLUMN commercial_reg_no TEXT")
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

# ── Customer de-duplication (v17 CRM) ────────────────────────────
# A customer may approach two sales managers who don't know about each other.
# We block a second registration that collides on any strong identifier —
# email, company name, phone, or commercial registration number.

def _norm_company(v):
    return ' '.join((v or '').lower().split())


def _norm_phone(v):
    digits = ''.join(ch for ch in (v or '') if ch.isdigit())
    return digits[-9:] if len(digits) >= 9 else digits   # ignore country-code prefixes


def _norm_id(v):
    return ''.join((v or '').split()).lower()


def find_duplicate_reseller(email=None, company_name=None, phone=None,
                            commercial_reg_no=None, exclude_id=None):
    """Return the first existing customer that collides on a strong identifier,
    as {matched, company_name, sales_name, reseller_id}, else None."""
    conn = get_db()
    rows = conn.execute("""SELECT cp.id, cp.company_name, cp.contact_phone, cp.commercial_reg_no,
                                  u.email, su.name as sales_name
                           FROM reseller_profiles cp
                           JOIN users u ON cp.user_id=u.id
                           LEFT JOIN users su ON cp.registered_by=su.id""").fetchall()
    conn.close()
    email_n = (email or '').lower().strip()
    comp_n = _norm_company(company_name)
    phone_n = _norm_phone(phone)
    crn_n = _norm_id(commercial_reg_no)
    for r in rows:
        if exclude_id and r['id'] == exclude_id:
            continue
        matched = None
        if email_n and r['email'] and r['email'].lower() == email_n:
            matched = 'email'
        elif comp_n and _norm_company(r['company_name']) == comp_n:
            matched = 'company name'
        elif phone_n and _norm_phone(r['contact_phone']) == phone_n:
            matched = 'phone number'
        elif crn_n and _norm_id(r['commercial_reg_no']) == crn_n:
            matched = 'commercial registration number'
        if matched:
            return {'matched': matched, 'company_name': r['company_name'],
                    'sales_name': r['sales_name'], 'reseller_id': r['id']}
    return None


def create_reseller(user_id, company_name, expected_sales, tier_id, registered_by,
                    notes='', client_type='', countries=None, client_types=None,
                    display_currency=None, contact_phone=None, hidden_merchants=None):
    """client_types: list (v8 multi-select). client_type stays as the primary/legacy value.
    display_currency (v11): defaults to the one derived from the reseller's markets.
    hidden_merchants (v18): merchants this reseller should never see (e.g. competitors)."""
    types = [t for t in (client_types or ([client_type] if client_type else [])) if t]
    primary = types[0] if types else (client_type or '')
    disp = display_currency or derive_display_currency(countries or [])
    conn = get_db()
    conn.execute("""INSERT INTO reseller_profiles
                    (user_id, company_name, expected_monthly_sales, assigned_tier_id,
                     registered_by, notes, client_type, display_currency, auto_suspend_at,
                     contact_phone, api_key)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                 (user_id, company_name, expected_sales, tier_id, registered_by, notes,
                  primary, disp, _suspend_deadline(), (contact_phone or '').strip() or None,
                  new_api_key('rk')))
    reseller_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for c in (countries or []):
        conn.execute("INSERT INTO reseller_countries (reseller_id, country) VALUES (?,?)", (reseller_id, c))
    for t in types:
        conn.execute("INSERT OR IGNORE INTO reseller_client_types (reseller_id, client_type) VALUES (?,?)",
                     (reseller_id, t))
    for mrc in (hidden_merchants or []):
        if mrc:
            conn.execute("INSERT OR IGNORE INTO reseller_hidden_merchants (reseller_id, merchant) VALUES (?,?)",
                         (reseller_id, mrc))
    conn.commit()
    conn.close()
    return reseller_id


def set_reseller_hidden_merchants(reseller_id, merchants):
    """Replace the reseller's hidden-merchant set (Sales edits this later)."""
    conn = get_db()
    conn.execute("DELETE FROM reseller_hidden_merchants WHERE reseller_id=?", (reseller_id,))
    for mrc in (merchants or []):
        if mrc:
            conn.execute("INSERT OR IGNORE INTO reseller_hidden_merchants (reseller_id, merchant) VALUES (?,?)",
                         (reseller_id, mrc))
    conn.commit()
    conn.close()


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
    p['hidden_merchants'] = [r['merchant'] for r in conn.execute(
        "SELECT merchant FROM reseller_hidden_merchants WHERE reseller_id=?", (p['id'],))]
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
    # v13: account model + the unified spending headroom, in SAR and display cur.
    p.setdefault('account_type', 'prepaid')
    p['account_type'] = p['account_type'] or 'prepaid'
    p['available_to_spend'] = available_to_spend(p)
    p['available_display'] = round(
        convert_amount(p['available_to_spend'], 'SAR', p['display_currency']))
    p['credit_outstanding_display'] = round(
        convert_amount(p.get('credit_outstanding', 0), 'SAR', p['display_currency']))
    p['credit_limit_display'] = round(
        convert_amount(p.get('credit_limit', 0), 'SAR', p['display_currency']))
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
    """Suspended → Prospect → Contracted → Active → At-Risk (chip shown everywhere)."""
    if r.get('is_suspended'):
        return 'suspended'
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
    'suspended':  ('Suspended', '#6b7280'),
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


# ── Prospect auto-suspension (v12) ───────────────────────────────

def is_user_suspended(user_id):
    """True if this user is a reseller whose account has been suspended.
    Cheap enough to call per-request in the login guard (indexed on user_id)."""
    conn = get_db()
    row = conn.execute("SELECT is_suspended FROM reseller_profiles WHERE user_id=?",
                       (user_id,)).fetchone()
    conn.close()
    return bool(row and row['is_suspended'])


def reseller_nda_pending(user_id):
    """True if this user is a reseller who hasn't yet accepted the confidentiality
    notice (NDA). Used by the per-request gate on the reseller portal."""
    conn = get_db()
    row = conn.execute("SELECT nda_accepted_at FROM reseller_profiles WHERE user_id=?",
                       (user_id,)).fetchone()
    conn.close()
    return bool(row) and not row['nda_accepted_at']


def set_nda_accepted(reseller_id):
    conn = get_db()
    conn.execute("""UPDATE reseller_profiles SET nda_accepted_at=CURRENT_TIMESTAMP
                    WHERE id=? AND nda_accepted_at IS NULL""", (reseller_id,))
    conn.commit()
    conn.close()


def set_reseller_suspended(reseller_id, suspended, reason='', actor_id=None):
    """Manually suspend or reactivate a reseller. Reactivating grants a fresh
    PROSPECT_SUSPEND_DAYS window so a still-unconverted prospect isn't
    re-suspended on the very next sweep. Returns the reseller's user_id."""
    conn = get_db()
    row = conn.execute("SELECT user_id, company_name FROM reseller_profiles WHERE id=?",
                       (reseller_id,)).fetchone()
    if not row:
        conn.close()
        return None
    if suspended:
        conn.execute("""UPDATE reseller_profiles
                        SET is_suspended=1, suspended_at=CURRENT_TIMESTAMP WHERE id=?""",
                     (reseller_id,))
    else:
        conn.execute("""UPDATE reseller_profiles
                        SET is_suspended=0, suspended_at=NULL, auto_suspend_at=? WHERE id=?""",
                     (_suspend_deadline(), reseller_id))
    conn.commit()
    conn.close()
    return row['user_id']


def run_prospect_suspension():
    """Auto-suspend every reseller who has neither signed a contract nor placed
    an order by their auto_suspend_at deadline. Suspended accounts can't log in
    but stay visible to their sales manager for reactivation.
    Returns a list of action strings. Notifies sales + admin/cco per account."""
    conn = get_db()
    today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute("""
        SELECT cp.id, cp.user_id, cp.company_name, cp.registered_by, cp.auto_suspend_at,
               (SELECT COUNT(*) FROM orders o WHERE o.reseller_id=cp.id) as orders_count
        FROM reseller_profiles cp
        WHERE cp.is_suspended=0
          AND cp.contract_status != 'contracted'
          AND cp.auto_suspend_at IS NOT NULL
          AND cp.auto_suspend_at < ?""", (today,)).fetchall()
    conn.close()

    actions = []
    admin_cco = get_user_ids_by_role('admin', 'cco')
    for r in rows:
        if r['orders_count'] > 0:
            continue  # bought something → keep active even if unsigned
        conn2 = get_db()
        conn2.execute("""UPDATE reseller_profiles
                         SET is_suspended=1, suspended_at=CURRENT_TIMESTAMP WHERE id=?""",
                      (r['id'],))
        conn2.commit()
        conn2.close()
        notify([r['registered_by']] + admin_cco,
               "Account auto-suspended ⏸️",
               f"{r['company_name']} did not sign a contract or purchase within "
               f"{PROSPECT_SUSPEND_DAYS} days and was auto-suspended. Reactivate them "
               f"from My Resellers if they are still in play.", "/sales/resellers")
        actions.append(f"SUSPENDED: {r['company_name']}")
    return actions


def update_reseller_profile(reseller_id, client_type=None, countries=None, expected_sales=None,
                            display_currency=None, contact_phone=None, commercial_reg_no=None):
    conn = get_db()
    if client_type is not None:
        conn.execute("UPDATE reseller_profiles SET client_type=? WHERE id=?", (client_type, reseller_id))
    if expected_sales is not None:
        conn.execute("UPDATE reseller_profiles SET expected_monthly_sales=? WHERE id=?", (expected_sales, reseller_id))
    if display_currency in DISPLAY_CURRENCIES:
        conn.execute("UPDATE reseller_profiles SET display_currency=? WHERE id=?", (display_currency, reseller_id))
    if contact_phone is not None:
        conn.execute("UPDATE reseller_profiles SET contact_phone=? WHERE id=?",
                     ((contact_phone or '').strip() or None, reseller_id))
    if commercial_reg_no is not None:
        conn.execute("UPDATE reseller_profiles SET commercial_reg_no=? WHERE id=?",
                     ((commercial_reg_no or '').strip() or None, reseller_id))
    if countries is not None:
        conn.execute("DELETE FROM reseller_countries WHERE reseller_id=?", (reseller_id,))
        for c in countries:
            conn.execute("INSERT INTO reseller_countries (reseller_id, country) VALUES (?,?)", (reseller_id, c))
    conn.commit()
    conn.close()


# ── Contracts & account provisioning (v13) ───────────────────────

def log_contract_event(contract_id, actor_id, event, note=''):
    conn = get_db()
    conn.execute("""INSERT INTO contract_events (contract_id, actor_id, event, note)
                    VALUES (?,?,?,?)""", (contract_id, actor_id, event, note))
    conn.commit()
    conn.close()


def create_contract(reseller_id, uploaded_by, file_draft, account_type='prepaid',
                    credit_limit=0, credit_disbursement='full', credit_tranche=0,
                    settlement_terms_days=30, billing_cycle='monthly', note='',
                    commercial_reg_no=None):
    """Sales uploads a draft contract with the proposed commercial terms. The
    commercial registration number (the customer's primary identifier) is
    usually captured here, at contract time, and copied onto the profile."""
    if account_type not in ACCOUNT_TYPES:
        account_type = 'prepaid'
    crn = (commercial_reg_no or '').strip() or None
    conn = get_db()
    conn.execute("""INSERT INTO contracts
                    (reseller_id, uploaded_by, file_draft, status, account_type, credit_limit,
                     credit_disbursement, credit_tranche, settlement_terms_days, billing_cycle,
                     note, commercial_reg_no)
                    VALUES (?,?,?,'sent',?,?,?,?,?,?,?,?)""",
                 (reseller_id, uploaded_by, file_draft, account_type, credit_limit or 0,
                  credit_disbursement, credit_tranche or 0, settlement_terms_days or 30,
                  billing_cycle, note, crn))
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if crn:
        conn.execute("UPDATE reseller_profiles SET commercial_reg_no=? WHERE id=?", (crn, reseller_id))
    conn.commit()
    conn.close()
    log_contract_event(cid, uploaded_by, 'sent',
                       f'Draft sent · {account_type}'
                       + (f' · limit {credit_limit:,.0f} SAR' if account_type != 'prepaid' else '')
                       + (f' · CR {crn}' if crn else ''))
    return cid


def get_contract(contract_id):
    conn = get_db()
    c = conn.execute("""SELECT ct.*, cp.company_name, cp.user_id as reseller_user_id,
                               cp.registered_by
                        FROM contracts ct JOIN reseller_profiles cp ON ct.reseller_id=cp.id
                        WHERE ct.id=?""", (contract_id,)).fetchone()
    if not c:
        conn.close()
        return None, []
    events = conn.execute("""SELECT ce.*, u.name as actor_name FROM contract_events ce
                             LEFT JOIN users u ON ce.actor_id=u.id
                             WHERE ce.contract_id=? ORDER BY ce.id""", (contract_id,)).fetchall()
    conn.close()
    return dict(c), [dict(e) for e in events]


def get_reseller_contracts(reseller_id):
    conn = get_db()
    rows = conn.execute("SELECT * FROM contracts WHERE reseller_id=? ORDER BY id DESC",
                        (reseller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_contract(reseller_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM contracts WHERE reseller_id=? ORDER BY id DESC LIMIT 1",
                       (reseller_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_latest_contracts_map(reseller_ids):
    """{reseller_id: latest contract dict} in ONE query (avoids N+1 on lists)."""
    ids = [r for r in reseller_ids if r]
    if not ids:
        return {}
    ph = ','.join('?' * len(ids))
    conn = get_db()
    rows = conn.execute(f"""SELECT c.* FROM contracts c
        JOIN (SELECT reseller_id, MAX(id) mid FROM contracts
              WHERE reseller_id IN ({ph}) GROUP BY reseller_id) m ON c.id=m.mid""", ids).fetchall()
    conn.close()
    return {r['reseller_id']: dict(r) for r in rows}


def get_hidden_merchants_map(reseller_ids):
    """{reseller_id: [merchant,...]} in ONE query (avoids N+1 on lists)."""
    ids = [r for r in reseller_ids if r]
    if not ids:
        return {}
    ph = ','.join('?' * len(ids))
    conn = get_db()
    rows = conn.execute(f"""SELECT reseller_id, merchant FROM reseller_hidden_merchants
                            WHERE reseller_id IN ({ph})""", ids).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r['reseller_id'], []).append(r['merchant'])
    return out


def contracts_awaiting_activation():
    """Signed contracts waiting to be activated — the CCO/Sales approval queue.
    Flags whether each needs CCO sign-off (large limit) or Sales can do it."""
    conn = get_db()
    rows = conn.execute("""SELECT ct.*, cp.company_name, cp.registered_by,
                                  su.name as sales_name
                           FROM contracts ct
                           JOIN reseller_profiles cp ON ct.reseller_id=cp.id
                           LEFT JOIN users su ON cp.registered_by=su.id
                           WHERE ct.status='signed_uploaded'
                           ORDER BY ct.signed_at DESC, ct.id DESC""").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['needs_cco'] = contract_needs_cco(d)
        out.append(d)
    return out


def contract_needs_cco(contract):
    """Credit/consignment lines above the auto-approve cap require CCO."""
    return (contract.get('account_type') in ('credit', 'consignment')
            and (contract.get('credit_limit') or 0) > AUTO_APPROVE_CAP)


def reseller_upload_signed(contract_id, file_signed, actor_id):
    conn = get_db()
    conn.execute("""UPDATE contracts SET file_signed=?, status='signed_uploaded',
                    signed_at=CURRENT_TIMESTAMP WHERE id=?""", (file_signed, contract_id))
    conn.commit()
    conn.close()
    log_contract_event(contract_id, actor_id, 'client_signed', 'Signed copy uploaded by reseller')


def activate_contract(contract_id, actor_id):
    """Apply the contract's commercial terms to the reseller and unlock ordering.
    Returns (ok, error). Governance (who may call this) is enforced in the route."""
    conn = get_db()
    ct = conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
    if not ct:
        conn.close()
        return False, "Contract not found."
    if ct['status'] not in ('signed_uploaded', 'sent'):
        conn.close()
        return False, "This contract is not awaiting activation."
    if (ct['account_type'] or 'prepaid') != 'prepaid' and (ct['credit_limit'] or 0) <= 0:
        conn.close()
        return False, "This credit/consignment contract has no credit limit set."
    rid = ct['reseller_id']
    conn.execute("""UPDATE reseller_profiles
                    SET account_type=?, credit_limit=?, credit_disbursement=?, credit_tranche=?,
                        settlement_terms_days=?, billing_cycle=?, credit_frozen=0,
                        contract_status='contracted',
                        contracted_at=COALESCE(contracted_at, CURRENT_TIMESTAMP)
                    WHERE id=?""",
                 (ct['account_type'], ct['credit_limit'], ct['credit_disbursement'],
                  ct['credit_tranche'], ct['settlement_terms_days'], ct['billing_cycle'], rid))
    conn.execute("""UPDATE contracts SET status='active', activated_at=CURRENT_TIMESTAMP,
                    activated_by=? WHERE id=?""", (actor_id, contract_id))
    conn.commit()
    conn.close()
    log_contract_event(contract_id, actor_id, 'activated',
                       f'Activated · {ct["account_type"]}')
    return True, None


def set_credit_terms(reseller_id, credit_limit=None, credit_tranche=None,
                     credit_disbursement=None, settlement_terms_days=None,
                     billing_cycle=None, credit_frozen=None):
    """Direct adjustment of a reseller's live credit terms (used by approved
    limit bumps and Finance/CCO actions)."""
    conn = get_db()
    sets, params = [], []
    for col, val in (('credit_limit', credit_limit), ('credit_tranche', credit_tranche),
                     ('credit_disbursement', credit_disbursement),
                     ('settlement_terms_days', settlement_terms_days),
                     ('billing_cycle', billing_cycle), ('credit_frozen', credit_frozen)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if sets:
        params.append(reseller_id)
        conn.execute(f"UPDATE reseller_profiles SET {', '.join(sets)} WHERE id=?", params)
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
    # v18: merchants hidden from this reseller (e.g. their competitors) are
    # removed from everything they see.
    hidden = set(profile.get('hidden_merchants') or [])
    rates = get_fx_rates()

    enriched = []
    for p in products:
        if p['merchant'] in hidden:
            continue
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


def get_merchant_pricing_for_reseller(reseller_id, merchant):
    """Everything the discount calculator needs for one merchant + reseller, in
    the reseller's DISPLAY currency. Because pricing is linear
    (client_price = default - oc_margin*share, floored at cost), the calculator
    can invert it live in the browser: share = (default - target) / oc_margin.
    Returns {display_currency, base_share_pct, current_share_pct, products:[...]}."""
    profile = get_reseller_profile_by_id(reseller_id)
    if not profile:
        return None
    # v20: a merchant hidden from this reseller has no prices for them.
    if merchant in set(profile.get('hidden_merchants') or []):
        return {'display_currency': profile['display_currency'] or 'SAR', 'base_share_pct': 0,
                'current_share_pct': 0, 'merchant': merchant, 'products': [], 'hidden': True}
    disp = profile['display_currency'] or 'SAR'
    rates = get_fx_rates()
    tier = profile.get('tier')
    base_share = (tier['margin_share_pct'] if tier else 20)
    override = profile.get('overrides', {}).get(merchant)
    current_share = override if override is not None else base_share
    prods = []
    for p in get_products(merchant=merchant):
        m = convert_amount(1.0, p['currency'], disp, rates)   # orig -> display multiplier
        if p['oc_margin'] <= 0:
            continue   # nothing to discount on this line
        default_d = round(p['default_price'] * m, 4)
        cost_d = round(p['cost'] * m, 4)
        margin_d = round(p['oc_margin'] * m, 4)
        cur_price = max(p['default_price'] - p['oc_margin'] * (current_share / 100.0), p['cost'])
        prods.append({
            'name': p['product_name'], 'face_value': round(p['face_value'] * m),
            'default_price': default_d, 'cost': cost_d, 'oc_margin': margin_d,
            'current_price': round(cur_price * m),
        })
    return {'display_currency': disp, 'base_share_pct': round(base_share, 1),
            'current_share_pct': round(current_share, 1),
            'merchant': merchant, 'products': prods}


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


# v12: how long a newly registered reseller has to sign a contract OR place an
# order before their account is auto-suspended (still visible to Sales).
PROSPECT_SUSPEND_DAYS = 15


def _suspend_deadline(created_at=None):
    """ISO date (YYYY-MM-DD) by which a prospect must convert or be suspended:
    PROSPECT_SUSPEND_DAYS after registration (or after a manual reactivation)."""
    base = None
    if created_at:
        try:
            base = datetime.fromisoformat(str(created_at)[:19]).date()
        except (ValueError, TypeError):
            base = None
    base = base or datetime.now(timezone.utc).date()
    return (base + timedelta(days=PROSPECT_SUSPEND_DAYS)).isoformat()


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


# ── Account / payment models (v13) ───────────────────────────────

ACCOUNT_TYPES = ['prepaid', 'credit', 'consignment']
CREDIT_DISBURSEMENTS = ['full', 'staged']
BILLING_CYCLES = ['monthly', 'weekly', 'custom']
# Above this credit/consignment limit (SAR), activation needs CCO approval;
# at or below it the sales manager can activate directly (Finance is notified).
AUTO_APPROVE_CAP = 100000

ACCOUNT_TYPE_LABELS = {
    'prepaid':     ('Prepaid (Cash)', '#10b981'),
    'credit':      ('Credit Line', '#f59e0b'),
    'consignment': ('Consignment', '#8b5cf6'),
}

# v21: how WE settle a SUPPLIER (same three arrangements, mirrored on the buy side).
SUPPLIER_ACCOUNT_TYPES = ['prepaid', 'credit', 'consignment']
SUPPLIER_ACCOUNT_LABELS = {
    'prepaid':     ('Prepaid — we pay upfront', '#10b981'),
    'credit':      ('Credit — they give us a limit', '#f59e0b'),
    'consignment': ('Consignment — pay as we sell', '#8b5cf6'),
}


def supplier_available_to_buy(supplier):
    """How much more we can buy from this supplier on account (SAR).
    prepaid = unbounded (we pay upfront each time); credit/consignment are capped
    by the limit they grant us, minus what we already owe."""
    at = supplier.get('account_type') or 'prepaid'
    if at == 'prepaid':
        return None   # not limited by a payable
    headroom = (supplier.get('our_credit_limit') or 0) - (supplier.get('our_outstanding') or 0)
    return round(max(0.0, headroom), 2)


def available_to_spend(profile):
    """The single ordering gate for every account type, in SAR.
       prepaid     -> wallet balance
       credit      -> full:   credit_limit - outstanding
                      staged: min(tranche, credit_limit - outstanding)
       consignment -> credit_limit - outstanding (accrues to the open statement)
    A frozen credit/consignment line (overdue) can spend nothing."""
    at = profile.get('account_type') or 'prepaid'
    if at == 'prepaid':
        return round(profile.get('wallet_balance') or 0, 2)
    if profile.get('credit_frozen'):
        return 0.0
    limit = profile.get('credit_limit') or 0
    outstanding = profile.get('credit_outstanding') or 0
    headroom = max(0.0, limit - outstanding)
    if at == 'credit' and (profile.get('credit_disbursement') or 'full') == 'staged':
        tranche = profile.get('credit_tranche') or 0
        if tranche > 0:
            return round(min(tranche, headroom), 2)
    return round(headroom, 2)


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


# ── Statements & settlement (v14) ────────────────────────────────

def _open_statements_total(conn, reseller_id):
    """Sum of statements already billed but not yet paid (issued + overdue)."""
    return conn.execute("""SELECT COALESCE(SUM(amount),0) FROM statements
                           WHERE reseller_id=? AND status IN ('issued','overdue')""",
                        (reseller_id,)).fetchone()[0]


def unbilled_amount(reseller_id):
    """Drawn-but-not-yet-billed amount = outstanding − already-open statements.
    This is what a new statement will bill."""
    conn = get_db()
    row = conn.execute("SELECT credit_outstanding FROM reseller_profiles WHERE id=?",
                       (reseller_id,)).fetchone()
    if not row:
        conn.close()
        return 0.0
    unbilled = (row['credit_outstanding'] or 0) - _open_statements_total(conn, reseller_id)
    conn.close()
    return round(max(0.0, unbilled), 2)


def issue_statement(reseller_id, actor_id=None, auto=False):
    """Bill the currently un-billed drawn amount as a new statement.
    due date = today + settlement_terms_days. Returns statement id or None.
    v20: BEGIN IMMEDIATE serializes the read-then-insert so a manual issue and
    the daily sweep can't both bill the same unbilled amount (double-billing)."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prof = conn.execute("""SELECT cp.*, u.id as uid FROM reseller_profiles cp
                               JOIN users u ON cp.user_id=u.id WHERE cp.id=?""", (reseller_id,)).fetchone()
        if not prof or (prof['account_type'] or 'prepaid') == 'prepaid':
            conn.rollback()
            return None
        unbilled = (prof['credit_outstanding'] or 0) - _open_statements_total(conn, reseller_id)
        unbilled = round(max(0.0, unbilled), 2)
        if unbilled <= 0.009:
            # nothing new to bill, but still advance the cycle marker
            conn.execute("UPDATE reseller_profiles SET last_statement_at=? WHERE id=?",
                         (datetime.now(timezone.utc).date().isoformat(), reseller_id))
            conn.commit()
            return None
        today = datetime.now(timezone.utc).date()
        due = (today + timedelta(days=int(prof['settlement_terms_days'] or 30))).isoformat()
        period_start = prof['last_statement_at'] or (prof['contracted_at'] or '')[:10] or today.isoformat()
        conn.execute("""INSERT INTO statements
                        (reseller_id, amount, status, period_start, period_end, due_at, issued_by)
                        VALUES (?,?,?,?,?,?,?)""",
                     (reseller_id, unbilled, 'issued', period_start, today.isoformat(), due, actor_id))
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE reseller_profiles SET last_statement_at=? WHERE id=?",
                     (today.isoformat(), reseller_id))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    disp = prof['display_currency'] or 'SAR'
    amt_disp = round(convert_amount(unbilled, 'SAR', disp))
    notify([prof['uid']], "New statement issued 🧾",
           f"A statement for {amt_disp:,.0f} {disp} was issued, due {due}. "
           f"Settle it from your Billing page.", "/reseller/wallet")
    notify(get_user_ids_by_role('finance'), "Statement issued",
           f"{prof['company_name']}: {unbilled:,.0f} SAR statement issued (due {due}).", "/finance/credit")
    send_webhook(reseller_id, 'statement.issued',
                 {'statement_id': sid, 'amount_sar': unbilled, 'due_at': due})
    return sid


def get_statements(reseller_id):
    conn = get_db()
    rows = conn.execute("SELECT * FROM statements WHERE reseller_id=? ORDER BY id DESC",
                        (reseller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_statement(sid):
    conn = get_db()
    row = conn.execute("""SELECT s.*, cp.company_name, cp.user_id as reseller_user_id,
                                 cp.display_currency, cp.registered_by
                          FROM statements s JOIN reseller_profiles cp ON s.reseller_id=cp.id
                          WHERE s.id=?""", (sid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_statements(status=None):
    conn = get_db()
    q = """SELECT s.*, cp.company_name, cp.account_type, u.email as reseller_email
           FROM statements s JOIN reseller_profiles cp ON s.reseller_id=cp.id
           JOIN users u ON cp.user_id=u.id"""
    params = []
    if status:
        q += " WHERE s.status=?"
        params.append(status)
    q += " ORDER BY s.due_at, s.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_settlement_request(reseller_id, statement_id, amount, bank_reference, receipt_file,
                              note='', orig_amount=None, orig_currency='SAR'):
    """Reseller uploads a bank-transfer receipt to settle a statement. amount is
    SAR. Finance approves via review_settlement (reduces outstanding)."""
    conn = get_db()
    conn.execute("""INSERT INTO wallet_transactions
                    (reseller_id, type, amount, status, bank_reference, receipt_file, note,
                     orig_amount, orig_currency, statement_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (reseller_id, 'settlement', amount, 'pending', bank_reference, receipt_file, note,
                  orig_amount if orig_amount is not None else amount, orig_currency or 'SAR',
                  statement_id))
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return txn_id


def get_pending_settlements():
    conn = get_db()
    rows = conn.execute("""SELECT wt.*, cp.company_name, cp.credit_outstanding, cp.account_type,
                                  u.email as reseller_email, s.due_at, s.amount as statement_amount
                           FROM wallet_transactions wt
                           JOIN reseller_profiles cp ON wt.reseller_id=cp.id
                           JOIN users u ON cp.user_id=u.id
                           LEFT JOIN statements s ON wt.statement_id=s.id
                           WHERE wt.type='settlement' AND wt.status='pending'
                           ORDER BY wt.created_at""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def review_settlement(txn_id, approve, reviewer_id, note=''):
    """Finance approves/rejects a settlement. On approval: reduce outstanding,
    mark the linked statement paid, and unfreeze the line if nothing else is
    overdue. Race-safe via BEGIN IMMEDIATE (mirrors review_topup)."""
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    txn = conn.execute("""SELECT * FROM wallet_transactions
                          WHERE id=? AND type='settlement' AND status='pending'""", (txn_id,)).fetchone()
    if not txn:
        conn.rollback()
        conn.close()
        return False
    status = 'approved' if approve else 'rejected'
    conn.execute("""UPDATE wallet_transactions SET status=?, reviewed_by=?,
                    reviewed_at=CURRENT_TIMESTAMP, note=COALESCE(NULLIF(?,''), note) WHERE id=?""",
                 (status, reviewer_id, note, txn_id))
    rid = txn['reseller_id']
    if approve:
        conn.execute("""UPDATE reseller_profiles
                        SET credit_outstanding = MAX(0, credit_outstanding - ?) WHERE id=?""",
                     (txn['amount'], rid))
        if txn['statement_id']:
            conn.execute("""UPDATE statements SET status='paid', paid_at=CURRENT_TIMESTAMP
                            WHERE id=? AND status IN ('issued','overdue')""", (txn['statement_id'],))
        # unfreeze once no statement remains overdue
        still_overdue = conn.execute("""SELECT COUNT(*) FROM statements
                                        WHERE reseller_id=? AND status='overdue'""", (rid,)).fetchone()[0]
        if not still_overdue:
            conn.execute("UPDATE reseller_profiles SET credit_frozen=0 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return True


def _cycle_days(billing_cycle):
    return 7 if billing_cycle == 'weekly' else 30   # 'custom' treated as monthly for now


def run_statement_cycle(force=False):
    """Daily housekeeping for credit/consignment lines:
       • auto-issue a statement when the billing cycle has elapsed and there is
         un-billed drawn amount,
       • flip past-due unpaid statements to 'overdue' and freeze the line,
       • revert an expired TEMPORARY credit bump to the base limit.
    Returns a list of action strings."""
    conn = get_db()
    today = datetime.now(timezone.utc).date()
    resellers = conn.execute("""SELECT id, company_name, account_type, billing_cycle,
                                       last_statement_at, contracted_at, credit_temp_until,
                                       credit_limit, credit_limit_base, registered_by, user_id
                                FROM reseller_profiles
                                WHERE account_type IN ('credit','consignment')""").fetchall()
    conn.close()
    actions = []
    admin_cco = get_user_ids_by_role('cco')
    for r in resellers:
        # 1. revert an expired temporary credit bump
        if r['credit_temp_until'] and r['credit_temp_until'] < today.isoformat():
            base = r['credit_limit_base'] or 0
            conn2 = get_db()
            conn2.execute("""UPDATE reseller_profiles SET credit_limit=?, credit_temp_until=NULL
                             WHERE id=?""", (base, r['id']))
            conn2.commit()
            conn2.close()
            notify([r['registered_by']] + admin_cco, "Temporary credit expired ⏲️",
                   f"{r['company_name']}'s temporary credit reverted to {base:,.0f} SAR.",
                   "/sales/resellers")
            actions.append(f"TEMP-REVERT: {r['company_name']}")

        # 2. mark overdue + freeze
        conn3 = get_db()
        overdue = conn3.execute("""SELECT id, amount FROM statements
                                   WHERE reseller_id=? AND status='issued' AND due_at < ?""",
                                (r['id'], today.isoformat())).fetchall()
        if overdue:
            conn3.execute("""UPDATE statements SET status='overdue'
                             WHERE reseller_id=? AND status='issued' AND due_at < ?""",
                          (r['id'], today.isoformat()))
            conn3.execute("UPDATE reseller_profiles SET credit_frozen=1 WHERE id=?", (r['id'],))
            conn3.commit()
            conn3.close()
            notify([r['user_id'], r['registered_by']] + get_user_ids_by_role('finance', 'cco'),
                   "⚠️ Statement overdue — account on hold",
                   f"{r['company_name']} has an overdue statement. Ordering is frozen until it is settled.",
                   "/finance/credit")
            for od in overdue:
                send_webhook(r['id'], 'statement.overdue',
                             {'statement_id': od['id'], 'amount_sar': od['amount']})
            actions.append(f"OVERDUE-FREEZE: {r['company_name']}")
        else:
            conn3.close()

        # 3. auto-issue when the cycle has elapsed
        base_date = r['last_statement_at'] or (r['contracted_at'] or '')[:10]
        due_for_cycle = True
        if base_date:
            try:
                due_for_cycle = (today - date.fromisoformat(base_date[:10])).days >= _cycle_days(r['billing_cycle'])
            except (ValueError, TypeError):
                due_for_cycle = True
        if force or due_for_cycle:
            if issue_statement(r['id'], actor_id=None, auto=True):
                actions.append(f"STATEMENT: {r['company_name']}")
    return actions


# ── Additional-credit requests (v14) ─────────────────────────────

def create_credit_request(reseller_id, requested_by, amount, kind='permanent',
                          expires_on=None, reason=''):
    """Sales asks CCO + Finance to raise a reseller's credit limit — either
    'permanent' or 'temporary' (with an expiry date)."""
    conn = get_db()
    conn.execute("""INSERT INTO credit_requests
                    (reseller_id, requested_by, amount, kind, expires_on, reason)
                    VALUES (?,?,?,?,?,?)""",
                 (reseller_id, requested_by, amount, kind,
                  expires_on if kind == 'temporary' else None, reason))
    crid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    r = conn.execute("SELECT company_name FROM reseller_profiles WHERE id=?", (reseller_id,)).fetchone()
    conn.commit()
    conn.close()
    kind_txt = (f"temporary until {expires_on}" if kind == 'temporary' else "permanent")
    notify(get_user_ids_by_role('cco', 'finance'), "Credit increase requested 💳",
           f"{r['company_name'] if r else '?'}: +{amount:,.0f} SAR ({kind_txt}). "
           f"Needs CCO + Finance approval.", "/cco/credit")
    return crid


def get_pending_credit_requests():
    conn = get_db()
    rows = conn.execute("""SELECT cr.*, cp.company_name, cp.credit_limit, cp.account_type,
                                  su.name as requested_by_name
                           FROM credit_requests cr
                           JOIN reseller_profiles cp ON cr.reseller_id=cp.id
                           LEFT JOIN users su ON cr.requested_by=su.id
                           WHERE cr.status='pending' ORDER BY cr.created_at""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def decide_credit_request(request_id, approver_role, approve, actor_id, note=''):
    """CCO and Finance each sign off. The bump applies only once BOTH approve;
    either rejection rejects the request. Returns (status, request_dict)."""
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    cr = conn.execute("SELECT * FROM credit_requests WHERE id=? AND status='pending'",
                      (request_id,)).fetchone()
    if not cr:
        conn.rollback()
        conn.close()
        return None, None
    if not approve:
        conn.execute("""UPDATE credit_requests SET status='rejected', decided_at=CURRENT_TIMESTAMP,
                        note=COALESCE(NULLIF(?,''), note) WHERE id=?""", (note, request_id))
        conn.commit()
        result = dict(cr)
        conn.close()
        _notify_credit_decision(result, 'rejected', note)
        return 'rejected', result
    col = 'cco_by' if approver_role in ('cco', 'admin') else 'finance_by'
    conn.execute(f"UPDATE credit_requests SET {col}=? WHERE id=?", (actor_id, request_id))
    cr = conn.execute("SELECT * FROM credit_requests WHERE id=?", (request_id,)).fetchone()
    status = 'pending'
    if cr['cco_by'] and cr['finance_by']:
        # both signed off -> apply the bump
        conn.execute("""UPDATE credit_requests SET status='approved', decided_at=CURRENT_TIMESTAMP,
                        note=COALESCE(NULLIF(?,''), note) WHERE id=?""", (note, request_id))
        prof = conn.execute("SELECT credit_limit, credit_limit_base, credit_temp_until FROM reseller_profiles WHERE id=?",
                            (cr['reseller_id'],)).fetchone()
        if cr['kind'] == 'temporary':
            # snapshot the permanent base once, then raise the live limit
            base = prof['credit_limit_base'] or prof['credit_limit'] or 0
            if not prof['credit_temp_until']:
                base = prof['credit_limit'] or 0
            new_limit = (prof['credit_limit'] or 0) + cr['amount']
            conn.execute("""UPDATE reseller_profiles SET credit_limit=?, credit_limit_base=?,
                            credit_temp_until=? WHERE id=?""",
                         (new_limit, base, cr['expires_on'], cr['reseller_id']))
        else:
            new_limit = (prof['credit_limit'] or 0) + cr['amount']
            conn.execute("""UPDATE reseller_profiles SET credit_limit=?, credit_limit_base=?
                            WHERE id=?""", (new_limit, new_limit, cr['reseller_id']))
        status = 'approved'
    conn.commit()
    result = dict(cr)
    conn.close()
    if status == 'approved':
        _notify_credit_decision(result, 'approved', note)
    return status, result


def _notify_credit_decision(cr, outcome, note=''):
    conn = get_db()
    r = conn.execute("""SELECT cp.company_name, cp.user_id FROM reseller_profiles cp
                        WHERE cp.id=?""", (cr['reseller_id'],)).fetchone()
    conn.close()
    if not r:
        return
    kind_txt = (f"temporary until {cr['expires_on']}" if cr['kind'] == 'temporary' else "permanent")
    recipients = [cr['requested_by']] + get_user_ids_by_role('cco', 'finance')
    notify(recipients, f"Credit request {outcome}",
           f"{r['company_name']}: +{cr['amount']:,.0f} SAR ({kind_txt}) was {outcome}."
           + (f" Note: {note}" if note else ""), "/sales/resellers")
    if outcome == 'approved':
        notify([r['user_id']], "Credit limit increased 🎉",
               f"Your credit limit was increased by {cr['amount']:,.0f} SAR ({kind_txt}).",
               "/reseller/wallet")


def get_credit_exposure():
    """Portfolio credit/consignment exposure for Finance/CCO dashboards (SAR)."""
    conn = get_db()
    row = conn.execute("""SELECT
            COALESCE(SUM(credit_limit),0) as total_limit,
            COALESCE(SUM(credit_outstanding),0) as total_outstanding,
            COUNT(*) as accounts,
            COALESCE(SUM(CASE WHEN credit_frozen=1 THEN 1 ELSE 0 END),0) as frozen
        FROM reseller_profiles WHERE account_type IN ('credit','consignment')""").fetchone()
    overdue = conn.execute("""SELECT COALESCE(SUM(amount),0) as amt, COUNT(*) as n
                              FROM statements WHERE status='overdue'""").fetchone()
    conn.close()
    d = dict(row)
    d['overdue_amount'] = overdue['amt']
    d['overdue_count'] = overdue['n']
    return d


def get_consignment_activity():
    """Credit/consignment accounts with their live draw-down, for Operations to
    anticipate restock (these clients pull in real time, often via API, rather
    than ordering stock up front). All amounts SAR."""
    conn = get_db()
    rows = conn.execute("""
        SELECT cp.id, cp.company_name, cp.account_type, cp.credit_limit,
               cp.credit_outstanding, cp.credit_frozen, cp.billing_cycle,
               (cp.api_key IS NOT NULL) as has_api,
               (SELECT COUNT(*) FROM orders o WHERE o.reseller_id=cp.id
                    AND o.created_at >= date('now','-30 day')) as draws_30d,
               (SELECT COALESCE(SUM(o.total_cost),0) FROM orders o WHERE o.reseller_id=cp.id
                    AND o.created_at >= date('now','-30 day')) as drawn_30d,
               (SELECT MAX(o.created_at) FROM orders o WHERE o.reseller_id=cp.id) as last_order_at,
               (SELECT COALESCE(SUM(s.amount),0) FROM statements s
                    WHERE s.reseller_id=cp.id AND s.status IN ('issued','overdue')) as open_billed
        FROM reseller_profiles cp
        WHERE cp.account_type IN ('credit','consignment')
        ORDER BY drawn_30d DESC""").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['unbilled'] = round(max(0.0, (d['credit_outstanding'] or 0) - (d['open_billed'] or 0)), 2)
        d['available'] = round(max(0.0, (d['credit_limit'] or 0) - (d['credit_outstanding'] or 0)), 2)
        out.append(d)
    return out


def get_credit_aging():
    """Open-statement receivables bucketed by how overdue they are (SAR), for
    Finance/CCO reporting. Buckets: not_due, 1-30, 31-60, 61-90, 90+ days."""
    conn = get_db()
    today = datetime.now(timezone.utc).date()
    rows = conn.execute("""SELECT s.amount, s.due_at FROM statements s
                           WHERE s.status IN ('issued','overdue')""").fetchall()
    conn.close()
    buckets = {'not_due': 0.0, 'd1_30': 0.0, 'd31_60': 0.0, 'd61_90': 0.0, 'd90p': 0.0}
    for r in rows:
        try:
            overdue_days = (today - date.fromisoformat((r['due_at'] or '')[:10])).days
        except (ValueError, TypeError):
            overdue_days = 0
        amt = r['amount'] or 0
        if overdue_days <= 0:
            buckets['not_due'] += amt
        elif overdue_days <= 30:
            buckets['d1_30'] += amt
        elif overdue_days <= 60:
            buckets['d31_60'] += amt
        elif overdue_days <= 90:
            buckets['d61_90'] += amt
        else:
            buckets['d90p'] += amt
    buckets = {k: round(v, 2) for k, v in buckets.items()}
    buckets['total'] = round(sum(buckets.values()), 2)
    return buckets


def get_credit_portfolio():
    """Per-reseller credit/consignment snapshot for the Finance CSV export (SAR)."""
    conn = get_db()
    today = datetime.now(timezone.utc).date()
    rows = conn.execute("""SELECT cp.id, cp.company_name, cp.account_type, cp.credit_limit,
                                  cp.credit_outstanding, cp.credit_frozen, cp.settlement_terms_days,
                                  cp.billing_cycle,
              (SELECT COALESCE(SUM(s.amount),0) FROM statements s
                   WHERE s.reseller_id=cp.id AND s.status IN ('issued','overdue')) as open_billed,
              (SELECT COALESCE(SUM(s.amount),0) FROM statements s
                   WHERE s.reseller_id=cp.id AND s.status='overdue') as overdue_amount,
              (SELECT MIN(s.due_at) FROM statements s
                   WHERE s.reseller_id=cp.id AND s.status='overdue') as oldest_overdue_due
                           FROM reseller_profiles cp
                           WHERE cp.account_type IN ('credit','consignment')
                           ORDER BY cp.company_name""").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['unbilled'] = round(max(0.0, (d['credit_outstanding'] or 0) - (d['open_billed'] or 0)), 2)
        d['available'] = round(max(0.0, (d['credit_limit'] or 0) - (d['credit_outstanding'] or 0)), 2)
        d['oldest_overdue_days'] = 0
        if d['oldest_overdue_due']:
            try:
                d['oldest_overdue_days'] = max(0, (today - date.fromisoformat(d['oldest_overdue_due'][:10])).days)
            except (ValueError, TypeError):
                d['oldest_overdue_days'] = 0
        out.append(d)
    return out


# ── Forecasts ────────────────────────────────────────────────────

BUDGET_MERCHANT = 'Starting budget (exploratory)'


def create_forecast(reseller_id, note, items):
    """items: list of dicts {item_type, merchant, product_rowid, product_name,
    quantity, est_value, needed_by, period, confidence}. The last three (v27)
    are optional and default to a recurring monthly baseline at medium confidence."""
    conn = get_db()
    try:
        conn.execute("INSERT INTO forecasts (reseller_id, note) VALUES (?,?)", (reseller_id, note))
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for it in items:
            conn.execute("""INSERT INTO forecast_items
                            (forecast_id, item_type, merchant, product_rowid, product_name,
                             quantity, est_value, needed_by, period, confidence)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (fid, it['item_type'], it['merchant'], it.get('product_rowid'),
                          it.get('product_name'), it.get('quantity'), it.get('est_value', 0),
                          it.get('needed_by'), _norm_period(it.get('period')),
                          _norm_confidence(it.get('confidence'))))
        conn.commit()
        return fid
    finally:
        conn.close()


# v27: timing-field normalisers so bad input from the form/API can't corrupt the
# NOT NULL columns or break the Ops bucketing that keys off these exact values.
def _norm_period(p):
    return 'one_off' if str(p or '').lower() in ('one_off', 'oneoff', 'one-off', 'once') else 'monthly'


def _norm_confidence(c):
    c = str(c or '').lower()
    return c if c in ('high', 'medium', 'low') else 'medium'


def _norm_needed_by(s):
    """Accept 'YYYY-MM-DD' (or the quick-select tokens) → an ISO date string or None."""
    from datetime import date as _date
    if not s:
        return None
    s = str(s).strip().lower()
    today = datetime.now(timezone.utc).date()
    if s in ('this_week', 'week'):
        return (today + timedelta(days=7)).isoformat()
    if s in ('this_month', 'month'):
        return (today + timedelta(days=30)).isoformat()
    if s in ('next_month', 'nextmonth'):
        return (today + timedelta(days=60)).isoformat()
    # explicit date
    try:
        return _date.fromisoformat(s[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def create_budget_forecast(reseller_id, amount_sar, note=''):
    """Brand-new clients who don't yet know what to buy just commit a starting
    budget (they'll connect via API and explore). Stored as a single merchant-type
    line under the special BUDGET_MERCHANT so it flows through the same Sales/Ops
    forecast views + actual tracking without a schema change."""
    return create_forecast(reseller_id, note, [{
        'item_type': 'merchant', 'merchant': BUDGET_MERCHANT,
        'est_value': round(amount_sar, 2)}])


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


def get_all_forecasts(limit=None):
    """Every reseller's forecast (no sales-owner filter) — used by Operations
    to see upcoming purchase intent across the whole book."""
    conn = get_db()
    q = """SELECT f.*, cp.company_name, cp.contract_status, u.name as contact_name,
                  (SELECT COUNT(*) FROM forecast_items fi WHERE fi.forecast_id = f.id) as item_count,
                  (SELECT COALESCE(SUM(est_value),0) FROM forecast_items fi WHERE fi.forecast_id = f.id) as total_value
           FROM forecasts f
           JOIN reseller_profiles cp ON f.reseller_id = cp.id
           JOIN users u ON cp.user_id = u.id
           ORDER BY f.created_at DESC"""
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_forecast_demand_summary(days=90):
    """Aggregate forecast demand (in SAR base) over the last `days` so Operations
    can plan stock. Returns dict: by_merchant [{merchant, forecasts, est_value}],
    by_product [{product_name, merchant, qty, est_value}], and headline totals."""
    conn = get_db()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    by_merchant = conn.execute("""
        SELECT fi.merchant,
               COUNT(DISTINCT f.id) as forecasts,
               COALESCE(SUM(fi.est_value),0) as est_value,
               COALESCE(SUM(CASE WHEN fi.item_type='product' THEN fi.quantity ELSE 0 END),0) as qty
        FROM forecast_items fi
        JOIN forecasts f ON fi.forecast_id = f.id
        WHERE date(f.created_at) >= ? AND fi.merchant != ?
        GROUP BY fi.merchant
        ORDER BY est_value DESC""", (since, BUDGET_MERCHANT)).fetchall()
    # v20: exploratory "starting budget" isn't a real merchant to stock — report
    # it separately as unallocated demand rather than polluting by-merchant.
    unallocated = conn.execute("""
        SELECT COALESCE(SUM(fi.est_value),0) FROM forecast_items fi
        JOIN forecasts f ON fi.forecast_id = f.id
        WHERE date(f.created_at) >= ? AND fi.merchant = ?""", (since, BUDGET_MERCHANT)).fetchone()[0]
    by_product = conn.execute("""
        SELECT fi.product_name, fi.merchant,
               COALESCE(SUM(fi.quantity),0) as qty,
               COALESCE(SUM(fi.est_value),0) as est_value,
               COUNT(DISTINCT f.reseller_id) as resellers
        FROM forecast_items fi
        JOIN forecasts f ON fi.forecast_id = f.id
        WHERE date(f.created_at) >= ? AND fi.item_type='product'
        GROUP BY fi.product_name, fi.merchant
        ORDER BY est_value DESC""", (since,)).fetchall()
    totals = conn.execute("""
        SELECT COUNT(DISTINCT f.id) as forecasts,
               COUNT(DISTINCT f.reseller_id) as resellers,
               COALESCE(SUM(fi.est_value),0) as est_value
        FROM forecast_items fi
        JOIN forecasts f ON fi.forecast_id = f.id
        WHERE date(f.created_at) >= ?""", (since,)).fetchone()
    conn.close()
    return {
        'by_merchant': [dict(r) for r in by_merchant],
        'by_product': [dict(r) for r in by_product],
        'totals': dict(totals),
        'unallocated_budget': round(unallocated, 2),
        'days': days,
    }


def _bucket_of(period, days_out):
    """v27: which time bucket a forecast line lands in. Recurring lines are the
    ongoing monthly baseline; one-off lines are placed by how far out they're needed."""
    if period == 'monthly':
        return 'recurring'
    if days_out is None:
        return 'undated'
    if days_out <= 7:
        return 'week'      # includes overdue (days_out < 0) — needs attention now
    if days_out <= 30:
        return 'month'
    if days_out <= 90:
        return 'quarter'
    return 'later'


BUCKET_LABELS = [
    ('week', 'Next 7 days'), ('month', '8-30 days'), ('quarter', '31-90 days'),
    ('later', 'Beyond 90 days'), ('recurring', 'Recurring monthly'), ('undated', 'Undated'),
]


def get_forecast_intelligence(lookback_days=120, window_days=None, spike_ratio=1.5,
                              concentration=0.6):
    """v27: turn raw forecast lines into an operational picture for Ops —
      • WHEN demand lands: time buckets + a 12-week timeline
      • WHERE the spikes are: near-term forecast vs the trailing actual run-rate
      • whether we can COVER it: stock on hand vs the needed-by window
      • WHO is driving it: per-customer register + single-client concentration risk
    New/unproven-client demand (reseller with no orders yet) is risk-discounted with
    the same weight the buy engine uses. Only forecasts submitted within
    `lookback_days` are considered live. All money in SAR."""
    cfg = get_buy_settings()
    new_weight = cfg['new_client_forecast_weight'] / 100.0
    window = int(window_days if window_days is not None else cfg['forecast_days'])
    conn = get_db()
    today = datetime.now(timezone.utc).date()
    since_created = (today - timedelta(days=lookback_days)).isoformat()

    lines = conn.execute("""
        SELECT fi.item_type, fi.merchant, fi.product_rowid, fi.product_name,
               COALESCE(fi.quantity,0) AS quantity, COALESCE(fi.est_value,0) AS est_value,
               fi.needed_by, COALESCE(fi.period,'monthly') AS period,
               COALESCE(fi.confidence,'medium') AS confidence,
               f.id AS fid, f.created_at, f.status, f.reseller_id,
               cp.company_name, cp.contract_status, u.name AS contact_name,
               (SELECT COUNT(*) FROM orders o WHERE o.reseller_id=f.reseller_id) AS orders_ct
        FROM forecast_items fi
        JOIN forecasts f ON fi.forecast_id=f.id
        JOIN reseller_profiles cp ON f.reseller_id=cp.id
        JOIN users u ON cp.user_id=u.id
        WHERE date(f.created_at) >= ? AND fi.merchant != ?
        ORDER BY f.created_at DESC""", (since_created, BUDGET_MERCHANT)).fetchall()

    # trailing actual run-rate over the same near-term window (the baseline demand)
    wsince = (today - timedelta(days=window)).isoformat()
    sold_val_m = {r['merchant']: r['v'] for r in conn.execute(
        """SELECT oi.merchant, COALESCE(SUM(oi.line_total_sar),0) v
           FROM order_items oi JOIN orders o ON oi.order_id=o.id
           WHERE date(o.created_at) >= ? GROUP BY oi.merchant""", (wsince,))}
    sold_qty_p = {r['product_rowid']: r['q'] for r in conn.execute(
        """SELECT oi.product_rowid, COALESCE(SUM(oi.quantity),0) q
           FROM order_items oi JOIN orders o ON oi.order_id=o.id
           WHERE date(o.created_at) >= ? AND oi.product_rowid IS NOT NULL
           GROUP BY oi.product_rowid""", (wsince,))}
    on_hand = {r['product_rowid']: r['q'] for r in conn.execute(
        "SELECT product_rowid, COALESCE(SUM(remaining_qty),0) q FROM purchase_batches GROUP BY product_rowid")}
    # exploratory "starting budget" is reported separately (not a real merchant to stock)
    unallocated = conn.execute(
        """SELECT COALESCE(SUM(fi.est_value),0) FROM forecast_items fi JOIN forecasts f ON fi.forecast_id=f.id
           WHERE date(f.created_at) >= ? AND fi.merchant = ?""", (since_created, BUDGET_MERCHANT)).fetchone()[0]
    conn.close()

    buckets = {k: {'value': 0.0, 'units': 0, 'lines': 0} for k, _ in BUCKET_LABELS}
    timeline = [{'week': i, 'value': 0.0, 'units': 0,
                 'start': (today + timedelta(days=7 * i)).isoformat()} for i in range(12)]
    merch, prod, reg = {}, {}, {}
    conc = {}   # merchant -> {reseller_id: near_term_value}

    for ln in lines:
        d = dict(ln)
        is_new = (d['orders_ct'] or 0) == 0
        w = new_weight if is_new else 1.0
        days_out = None
        if d['needed_by']:
            try:
                days_out = (date.fromisoformat(d['needed_by'][:10]) - today).days
            except (ValueError, TypeError):
                days_out = None
        bk = _bucket_of(d['period'], days_out)
        buckets[bk]['value'] += d['est_value']
        buckets[bk]['units'] += d['quantity']
        buckets[bk]['lines'] += 1
        # near-term = recurring monthly run-rate, or a one-off due within the window
        near = d['period'] == 'monthly' or (days_out is not None and days_out <= window)
        nt_val = d['est_value'] * w if near else 0.0
        nt_qty = d['quantity'] * w if near else 0.0

        # 12-week timeline (one-off dated demand; overdue folds into week 0)
        if d['period'] != 'monthly' and days_out is not None and days_out <= 83:
            wk = max(0, days_out // 7)
            timeline[wk]['value'] += d['est_value']
            timeline[wk]['units'] += d['quantity']

        m = merch.setdefault(d['merchant'], {
            'merchant': d['merchant'], 'forecasts': set(), 'resellers': set(),
            'est_value': 0.0, 'near_term': 0.0, 'near_term_raw': 0.0, 'qty': 0})
        m['forecasts'].add(d['fid']); m['resellers'].add(d['reseller_id'])
        m['est_value'] += d['est_value']; m['near_term'] += nt_val
        m['near_term_raw'] += (d['est_value'] if near else 0.0); m['qty'] += d['quantity']
        if near and nt_val > 0:
            cm = conc.setdefault(d['merchant'], {})
            cm[d['reseller_id']] = cm.get(d['reseller_id'], 0.0) + nt_val

        if d['item_type'] == 'product' and d['product_rowid']:
            p = prod.setdefault(d['product_rowid'], {
                'product_rowid': d['product_rowid'], 'product_name': d['product_name'],
                'merchant': d['merchant'], 'resellers': set(), 'est_value': 0.0,
                'qty': 0, 'qty_near': 0.0, 'earliest': None})
            p['resellers'].add(d['reseller_id']); p['est_value'] += d['est_value']
            p['qty'] += d['quantity']; p['qty_near'] += nt_qty
            if d['needed_by'] and (p['earliest'] is None or d['needed_by'] < p['earliest']):
                p['earliest'] = d['needed_by']

        r = reg.setdefault(d['fid'], {
            'fid': d['fid'], 'company_name': d['company_name'], 'contact_name': d['contact_name'],
            'contract_status': d['contract_status'], 'tier': 'new' if is_new else 'active',
            'created_at': d['created_at'], 'status': d['status'],
            'lines': 0, 'est_value': 0.0, 'earliest': None})
        r['lines'] += 1; r['est_value'] += d['est_value']
        if d['needed_by'] and (r['earliest'] is None or d['needed_by'] < r['earliest']):
            r['earliest'] = d['needed_by']

    # ── finalise merchants: spike signal + concentration ──
    signals = []
    by_merchant = []
    for m in merch.values():
        baseline = sold_val_m.get(m['merchant'], 0.0)
        nt = m['near_term']
        ratio = (nt / baseline) if baseline > 0 else None
        if baseline <= 0 and nt > 0:
            sig = 'new_demand'
        elif ratio is not None and ratio >= spike_ratio:
            sig = 'surge'
        elif ratio is not None and ratio < 0.5:
            sig = 'cooling'
        else:
            sig = 'steady'
        cm = conc.get(m['merchant'], {})
        top_share, top_new = 0.0, False
        if cm and nt > 0:
            top_rid = max(cm, key=cm.get)
            top_share = cm[top_rid] / nt
            # is that dominant reseller new/unproven? (weighted contribution < raw ⇒ discounted)
            top_new = any(x['reseller_id'] == top_rid and (x['orders_ct'] or 0) == 0 for x in lines)
        row = {
            'merchant': m['merchant'], 'forecasts': len(m['forecasts']),
            'resellers': len(m['resellers']), 'est_value': round(m['est_value'], 2),
            'near_term': round(nt, 2), 'near_term_raw': round(m['near_term_raw'], 2),
            'qty': m['qty'], 'baseline': round(baseline, 2),
            'ratio': round(ratio, 2) if ratio is not None else None,
            'signal': sig, 'top_share': round(top_share, 2), 'top_new': top_new,
        }
        by_merchant.append(row)
        if sig == 'surge':
            signals.append({'kind': 'surge', 'severity': 'high', 'merchant': m['merchant'],
                            'message': f"{m['merchant']}: near-term forecast is {row['ratio']}x the "
                                       f"last {window}-day run-rate (SAR {row['near_term']:,.0f} vs {row['baseline']:,.0f})."})
        elif sig == 'new_demand' and nt > 0:
            signals.append({'kind': 'new_demand', 'severity': 'medium', 'merchant': m['merchant'],
                            'message': f"{m['merchant']}: SAR {row['near_term']:,.0f} of near-term forecast with "
                                       f"no recent sales history - brand-new demand to source."})
        if top_share >= concentration and nt > 0 and len(cm) >= 1:
            who = 'NEW/unproven ' if top_new else ''
            signals.append({'kind': 'concentration', 'severity': 'high' if top_new else 'medium',
                            'merchant': m['merchant'],
                            'message': f"{m['merchant']}: {row['top_share']*100:.0f}% of near-term demand comes from a "
                                       f"single {who}client - verify before committing stock."})
    by_merchant.sort(key=lambda x: x['near_term'], reverse=True)

    # ── finalise products: coverage gap vs needed_by ──
    by_product = []
    for p in prod.values():
        need = p['qty_near']
        oh = on_hand.get(p['product_rowid'], 0)
        short = max(0, int(round(need)) - oh)
        daily = need / float(window) if need > 0 else 0
        days_cover = round(oh / daily, 1) if daily > 0 else None
        by_product.append({
            'product_rowid': p['product_rowid'], 'product_name': p['product_name'],
            'merchant': p['merchant'], 'resellers': len(p['resellers']),
            'est_value': round(p['est_value'], 2), 'qty': p['qty'],
            'qty_near': int(round(need)), 'on_hand': oh, 'short': short,
            'days_cover': days_cover, 'earliest': p['earliest'],
            'sold': sold_qty_p.get(p['product_rowid'], 0),
            'signal': 'short' if short > 0 else 'ok',
        })
        if short > 0:
            by_product_sig = f"{p['product_name']}: forecast needs {int(round(need))} units but only {oh} on hand"
            if p['earliest']:
                by_product_sig += f" - short {short} before {p['earliest']}"
            signals.append({'kind': 'short', 'severity': 'high', 'merchant': p['merchant'],
                            'product_rowid': p['product_rowid'], 'message': by_product_sig + '.'})
    by_product.sort(key=lambda x: (x['short'], x['qty_near']), reverse=True)

    register = sorted(reg.values(), key=lambda x: (x['earliest'] or '9999', x['created_at']))
    for r in register:
        r['est_value'] = round(r['est_value'], 2)
    # severity order for the signals panel
    sev_rank = {'high': 0, 'medium': 1, 'low': 2}
    signals.sort(key=lambda s: sev_rank.get(s['severity'], 3))

    totals = {
        'forecasts': len(reg), 'resellers': len({d['reseller_id'] for d in lines}),
        'est_value': round(sum(d['est_value'] for d in lines), 2),
        'near_term': round(sum(m['near_term'] for m in by_merchant), 2),
        'undated_value': round(buckets['undated']['value'], 2),
        'recurring_value': round(buckets['recurring']['value'], 2),
        'signals': len(signals),
    }
    return {
        'buckets': buckets, 'bucket_labels': BUCKET_LABELS, 'timeline': timeline,
        'by_merchant': by_merchant, 'by_product': by_product, 'signals': signals,
        'register': register, 'totals': totals, 'window': window,
        'lookback': lookback_days, 'unallocated_budget': round(unallocated, 2),
        'today': today.isoformat(),
    }


def get_forecast_detail(fid):
    conn = get_db()
    f = conn.execute("""SELECT f.*, cp.company_name, cp.registered_by, cp.user_id as reseller_user_id,
                               cp.contract_status,
                               (SELECT COUNT(*) FROM orders o WHERE o.reseller_id=f.reseller_id) AS orders_ct
                        FROM forecasts f JOIN reseller_profiles cp ON f.reseller_id=cp.id
                        WHERE f.id=?""", (fid,)).fetchone()
    if not f:
        conn.close()
        return None, []
    items = conn.execute("SELECT * FROM forecast_items WHERE forecast_id=? ORDER BY id", (fid,)).fetchall()
    # v27: fulfilment — how much of each forecast PRODUCT line the reseller has
    # actually ordered SINCE this forecast was submitted (intent → real orders).
    f = dict(f)
    out_items = []
    for it in items:
        it = dict(it)
        if it['item_type'] == 'product' and it['product_rowid']:
            row = conn.execute(
                """SELECT COALESCE(SUM(oi.quantity),0) q FROM order_items oi JOIN orders o ON oi.order_id=o.id
                   WHERE o.reseller_id=? AND oi.product_rowid=? AND o.created_at >= ?""",
                (f['reseller_id'], it['product_rowid'], f['created_at'])).fetchone()
            it['ordered_since'] = row['q'] or 0
        else:
            it['ordered_since'] = None
        out_items.append(it)
    # on-hand stock for the products in this forecast (Ops decision context)
    for it in out_items:
        if it['item_type'] == 'product' and it['product_rowid']:
            oh = conn.execute("SELECT COALESCE(SUM(remaining_qty),0) q FROM purchase_batches WHERE product_rowid=?",
                              (it['product_rowid'],)).fetchone()['q']
            it['on_hand'] = oh
    conn.close()
    return f, out_items


def set_forecast_line_timing(item_id, needed_by=None, confidence=None):
    """v27: let the account manager refine a line's timing/confidence from the
    sales forecast detail — they often know the real timing better than a
    self-serve client. Returns True if the line was updated."""
    sets, params = [], []
    if needed_by is not None:
        sets.append("needed_by=?"); params.append(_norm_needed_by(needed_by))
    if confidence is not None:
        sets.append("confidence=?"); params.append(_norm_confidence(confidence))
    if not sets:
        return False
    params.append(item_id)
    conn = get_db()
    conn.execute(f"UPDATE forecast_items SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    conn.close()
    return True


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
    bal = conn.execute("""SELECT wallet_balance, display_currency, account_type, credit_limit,
                                 credit_released, credit_outstanding, credit_disbursement,
                                 credit_tranche, credit_frozen
                          FROM reseller_profiles WHERE id=?""", (reseller_id,)).fetchone()
    if not bal:
        conn.rollback()
        conn.close()
        return None, "Reseller not found."
    disp = bal['display_currency'] or 'SAR'
    account_type = bal['account_type'] or 'prepaid'
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
    # v13: one gate for all three account types.
    available = available_to_spend(dict(bal))
    if available < total_cost:
        conn.rollback()
        conn.close()
        total_disp = round(convert_amount(total_cost, 'SAR', disp, rates))
        avail_disp = round(convert_amount(available, 'SAR', disp, rates))
        if account_type == 'prepaid':
            return None, (f"Insufficient wallet balance. Order total is {total_disp:,.0f} {disp} "
                          f"but wallet has {avail_disp:,.0f} {disp}.")
        if bal['credit_frozen']:
            return None, ("Your account is on hold pending settlement of an overdue "
                          "statement. Please contact your account manager.")
        noun = 'credit' if account_type == 'credit' else 'consignment limit'
        return None, (f"Order total is {total_disp:,.0f} {disp} but only {avail_disp:,.0f} {disp} "
                      f"of {noun} is available right now.")
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
    if account_type == 'prepaid':
        conn.execute("UPDATE reseller_profiles SET wallet_balance = wallet_balance - ? WHERE id=?",
                     (total_cost, reseller_id))
        conn.execute("""INSERT INTO wallet_transactions (reseller_id, type, amount, status, note)
                        VALUES (?,?,?,?,?)""",
                     (reseller_id, 'order', -total_cost, 'approved', f'Order #{oid}'))
    else:
        # credit / consignment: draw against the limit; settled per statement.
        draw_type = 'credit_draw' if account_type == 'credit' else 'consignment_draw'
        conn.execute("""UPDATE reseller_profiles
                        SET credit_outstanding = credit_outstanding + ? WHERE id=?""",
                     (total_cost, reseller_id))
        conn.execute("""INSERT INTO wallet_transactions (reseller_id, type, amount, status, note)
                        VALUES (?,?,?,?,?)""",
                     (reseller_id, draw_type, -total_cost, 'approved', f'Order #{oid}'))
    # v20: any failure in allocation / code assignment / a provider adapter must
    # roll the whole order back and release the write lock, never leak it.
    try:
        # v6: tie every sold unit to the supplier batch it came from (FIFO)
        _allocate_order_fifo(conn, oid)
        # v8: hand actual gift-card codes to the buyer for issued products
        touched_issued = _assign_issued_codes(conn, oid)
        # v9: run provider adapters for merchants that have one registered
        _run_fulfillment_adapters(conn, oid)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return None, "Order could not be completed due to a system error. Please try again."
    conn.close()
    notify_low_issued_stock(touched_issued)
    if account_type in ('credit', 'consignment'):
        _notify_if_limit_reached(reseller_id)
    send_webhook(reseller_id, 'order.placed',
                 {'order_id': oid, 'total_sar': total_cost})
    return oid, None


def _notify_if_limit_reached(reseller_id):
    """When a credit/consignment reseller can no longer draw (available == 0),
    alert the sales manager + CCO + Finance so a limit bump can be considered."""
    conn = get_db()
    r = conn.execute("""SELECT cp.*, u.name as contact_name, u.id as uid
                        FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id
                        WHERE cp.id=?""", (reseller_id,)).fetchone()
    conn.close()
    if not r:
        return
    if available_to_spend(dict(r)) > 0.01:
        return
    team = [r['registered_by']] + get_user_ids_by_role('cco', 'finance')
    notify(team, "Credit limit reached 🚧",
           f"{r['company_name']} has used all available "
           f"{('credit' if r['account_type'] == 'credit' else 'consignment')} "
           f"(limit {r['credit_limit']:,.0f} SAR). Review whether to raise the limit.",
           "/sales/resellers")


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


def get_reseller_order_line_ids(reseller_id):
    """order_item ids belonging to a reseller (v25: used to validate redemptions)."""
    conn = get_db()
    rows = conn.execute("""SELECT oi.id FROM order_items oi JOIN orders o ON oi.order_id=o.id
                           WHERE o.reseller_id=?""", (reseller_id,)).fetchall()
    conn.close()
    return [r['id'] for r in rows]


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


# ── Competitor price intelligence (Sales -> BD) (v19) ────────────

def create_competitor_intel(submitted_by, merchant, product_name, competitor_name,
                            competitor_price, our_price, currency, note, attachment_file):
    conn = get_db()
    conn.execute("""INSERT INTO competitor_intel
                    (submitted_by, merchant, product_name, competitor_name, competitor_price,
                     our_price, currency, note, attachment_file)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (submitted_by, merchant or None, product_name or None, competitor_name or None,
                  competitor_price, our_price, currency or 'SAR', note or None, attachment_file))
    iid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = conn.execute("SELECT name FROM users WHERE id=?", (submitted_by,)).fetchone()
    conn.commit()
    conn.close()
    notify(get_user_ids_by_role('bd', 'cco'),
           "Competitor pricing intel 🔍",
           f"{row['name'] if row else 'Sales'} flagged a better competitor price"
           + (f" on {merchant}" if merchant else "") + ". Review it in Price Intel.",
           "/bd/intel")
    return iid


def get_competitor_intel(submitted_by=None, status=None):
    conn = get_db()
    q = """SELECT ci.*, su.name as submitted_by_name, ru.name as reviewed_by_name
           FROM competitor_intel ci
           JOIN users su ON ci.submitted_by=su.id
           LEFT JOIN users ru ON ci.reviewed_by=ru.id WHERE 1=1"""
    params = []
    if submitted_by:
        q += " AND ci.submitted_by=?"
        params.append(submitted_by)
    if status:
        q += " AND ci.status=?"
        params.append(status)
    q += " ORDER BY CASE ci.status WHEN 'new' THEN 0 WHEN 'reviewing' THEN 1 ELSE 2 END, ci.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_competitor_intel_one(intel_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM competitor_intel WHERE id=?", (intel_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_competitor_intel(intel_id, status, bd_note, reviewer_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM competitor_intel WHERE id=?", (intel_id,)).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("""UPDATE competitor_intel SET status=?, bd_note=COALESCE(NULLIF(?,''), bd_note),
                    reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?""",
                 (status, bd_note, reviewer_id, intel_id))
    conn.commit()
    conn.close()
    labels = {'reviewing': 'is under review 🔍', 'actioned': 'was actioned ✅', 'dismissed': 'was dismissed'}
    notify(row['submitted_by'], f"Your competitor intel {labels.get(status, status)}",
           bd_note or '', "/sales/competitor-intel")
    return dict(row)


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


# v23: forecast from a brand-new/unproven client (no orders yet) is speculative,
# so it's discounted vs a proven active client's forecast in buy planning.
# v25: these are now defaults; the live values are editable (stored in app_meta).
NEW_CLIENT_FORECAST_WEIGHT = 0.4

BUY_SETTING_DEFAULTS = {
    'new_client_forecast_weight': 40,   # % weight of a new/unproven client's forecast
    'cover_days': 14,                   # target days of stock cover to reorder toward
    'reorder_days': 7,                  # below this many days of cover → "reorder now"
    'draw_days': 30,                    # window used to measure the draw-down rate
    'forecast_days': 30,                # window/horizon used for forecast demand
}


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute("""INSERT INTO app_meta (key,value) VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, str(value)))
    conn.commit()
    conn.close()


def get_buy_settings():
    """Live buy-decision weights (editable by Ops), falling back to defaults."""
    out = {}
    for k, dv in BUY_SETTING_DEFAULTS.items():
        v = get_setting('buy.' + k)
        try:
            out[k] = float(v) if v is not None else float(dv)
        except (ValueError, TypeError):
            out[k] = float(dv)
    return out


def get_buy_recommendations(cover_days=None, draw_days=None, forecast_days=None):
    """The buy-decision engine: for each supplier-bought (non-issued) product, weigh
    stock on hand vs the recent draw-down rate and forecast demand (active-client
    forecast counts fully; new/unproven-client forecast is discounted) to recommend
    what to reorder now. All money in SAR. Returns a list of per-product dicts.
    Weights come from Ops-editable settings (get_buy_settings), overridable per call."""
    cfg = get_buy_settings()
    cover_days = cover_days if cover_days is not None else cfg['cover_days']
    draw_days = int(draw_days if draw_days is not None else cfg['draw_days'])
    forecast_days = int(forecast_days if forecast_days is not None else cfg['forecast_days'])
    new_weight = cfg['new_client_forecast_weight'] / 100.0
    reorder_days = cfg['reorder_days']
    conn = get_db()
    since = (datetime.now(timezone.utc).date() - timedelta(days=draw_days)).isoformat()
    fsince = (datetime.now(timezone.utc).date() - timedelta(days=forecast_days)).isoformat()

    # stock on hand (remaining across purchase batches)
    on_hand = {r['product_rowid']: r['q'] for r in conn.execute(
        "SELECT product_rowid, COALESCE(SUM(remaining_qty),0) q FROM purchase_batches GROUP BY product_rowid")}
    # draw-down: units sold in the window
    sold = {r['product_rowid']: r['q'] for r in conn.execute(
        """SELECT oi.product_rowid, COALESCE(SUM(oi.quantity),0) q
           FROM order_items oi JOIN orders o ON oi.order_id=o.id
           WHERE date(o.created_at) >= ? AND oi.product_rowid IS NOT NULL
           GROUP BY oi.product_rowid""", (since,))}
    # forecast qty split by whether the forecasting reseller is already active
    # (has >=1 order) or is new/unproven (no orders yet)
    fc_active, fc_new = {}, {}
    for r in conn.execute(
        """SELECT fi.product_rowid,
                  (SELECT COUNT(*) FROM orders o WHERE o.reseller_id=f.reseller_id) as ord,
                  COALESCE(SUM(fi.quantity),0) as q
           FROM forecast_items fi JOIN forecasts f ON fi.forecast_id=f.id
           WHERE fi.item_type='product' AND fi.product_rowid IS NOT NULL
                 AND date(f.created_at) >= ?
           GROUP BY fi.product_rowid, (ord>0)""", (fsince,)):
        (fc_active if r['ord'] and r['ord'] > 0 else fc_new)[r['product_rowid']] = \
            (fc_active if r['ord'] and r['ord'] > 0 else fc_new).get(r['product_rowid'], 0) + (r['q'] or 0)

    pids = set(on_hand) | set(sold) | set(fc_active) | set(fc_new)
    if not pids:
        conn.close()
        return []
    ph = ','.join('?' * len(pids))
    prods = {r['id']: dict(r) for r in conn.execute(
        f"""SELECT id, product_name, merchant, currency, cost
            FROM products WHERE id IN ({ph}) AND is_active=1 AND COALESCE(is_issued,0)=0""",
        list(pids))}
    # cheapest current supplier cost per product (what a reorder would cost)
    best = {r['product_rowid']: r['c'] for r in conn.execute(
        f"""SELECT product_rowid, MIN(supplier_cost) c FROM supplier_products
            WHERE product_rowid IN ({ph}) AND is_available=1 GROUP BY product_rowid""", list(pids))}
    conn.close()

    out = []
    for pid, p in prods.items():
        oh = on_hand.get(pid, 0)
        daily_draw = sold.get(pid, 0) / float(draw_days)
        fa, fn = fc_active.get(pid, 0), fc_new.get(pid, 0)
        weighted_fc = fa + new_weight * fn
        daily_fc = weighted_fc / float(forecast_days)
        daily_expected = max(daily_draw, daily_fc)
        days_cover = round(oh / daily_expected, 1) if daily_expected > 0 else None
        target = daily_expected * cover_days
        rec_qty = max(0, int(round(target - oh)))
        unit_cost = best.get(pid, p['cost'] or 0)
        if daily_expected <= 0 and oh > 0:
            signal = 'ok'
        elif oh <= 0 and daily_expected > 0:
            signal = 'out'
        elif days_cover is not None and days_cover < reorder_days:
            signal = 'reorder'
        elif days_cover is not None and days_cover < cover_days:
            signal = 'watch'
        else:
            signal = 'ok'
        out.append({
            'product_rowid': pid, 'product_name': p['product_name'], 'merchant': p['merchant'],
            'on_hand': oh, 'sold': sold.get(pid, 0), 'daily_draw': round(daily_draw, 2),
            'forecast_active': fa, 'forecast_new': fn, 'weighted_forecast': round(weighted_fc, 1),
            'days_cover': days_cover, 'recommended_qty': rec_qty,
            'est_cost': round(rec_qty * unit_cost, 2), 'unit_cost': round(unit_cost, 2),
            'currency': p['currency'], 'signal': signal,
        })
    order = {'out': 0, 'reorder': 1, 'watch': 2, 'ok': 3}
    out.sort(key=lambda x: (order.get(x['signal'], 9), -x['est_cost']))
    return out


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


def upsert_supplier(sid, name, contact_person, email, phone, payment_terms, notes, merchants,
                    account_type='prepaid', our_credit_limit=0, settlement_terms_days=30,
                    billing_cycle='monthly', consignment_settle_on='sale'):
    if account_type not in SUPPLIER_ACCOUNT_TYPES:
        account_type = 'prepaid'
    settle_on = 'redemption' if consignment_settle_on == 'redemption' else 'sale'
    conn = get_db()
    if sid:
        conn.execute("""UPDATE suppliers SET name=?, contact_person=?, email=?, phone=?,
                        payment_terms=?, notes=?, account_type=?, our_credit_limit=?,
                        settlement_terms_days=?, billing_cycle=?, consignment_settle_on=? WHERE id=?""",
                     (name, contact_person, email, phone, payment_terms, notes, account_type,
                      our_credit_limit or 0, settlement_terms_days or 30, billing_cycle, settle_on, sid))
    else:
        conn.execute("""INSERT INTO suppliers (name, contact_person, email, phone, payment_terms,
                        notes, account_type, our_credit_limit, settlement_terms_days, billing_cycle,
                        consignment_settle_on, api_key)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (name, contact_person, email, phone, payment_terms, notes, account_type,
                      our_credit_limit or 0, settlement_terms_days or 30, billing_cycle, settle_on,
                      new_api_key('sk')))
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


def get_supplier(sid):
    conn = get_db()
    row = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def pay_supplier(supplier_id, amount, method='', reference='', note='', paid_by=None):
    """Record a payment WE make to a supplier and reduce what we owe them.
    Race-safe: the read-and-reduce runs under BEGIN IMMEDIATE."""
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        return False, "Enter a valid payment amount."
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute("SELECT name, our_outstanding FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not s:
            conn.rollback()
            return False, "Supplier not found."
        conn.execute("""INSERT INTO supplier_payments
                        (supplier_id, amount, method, reference, note, paid_by)
                        VALUES (?,?,?,?,?,?)""",
                     (supplier_id, amount, method or None, reference or None, note or None, paid_by))
        conn.execute("UPDATE suppliers SET our_outstanding = MAX(0, our_outstanding - ?) WHERE id=?",
                     (amount, supplier_id))
        # v22: apply the payment to open statements oldest-first (mark paid when covered)
        left = amount
        for st in conn.execute("""SELECT id, amount FROM supplier_statements
                                  WHERE supplier_id=? AND status IN ('issued','overdue')
                                  ORDER BY due_at, id""", (supplier_id,)).fetchall():
            if left < st['amount'] - 0.01:
                break
            conn.execute("UPDATE supplier_statements SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE id=?",
                         (st['id'],))
            left -= st['amount']
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return True, None


def get_supplier_payments(supplier_id=None, limit=100):
    conn = get_db()
    q = """SELECT sp.*, s.name as supplier_name, u.name as paid_by_name
           FROM supplier_payments sp JOIN suppliers s ON sp.supplier_id=s.id
           LEFT JOIN users u ON sp.paid_by=u.id WHERE 1=1"""
    params = []
    if supplier_id:
        q += " AND sp.supplier_id=?"
        params.append(supplier_id)
    q += " ORDER BY sp.created_at DESC, sp.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_redemption(order_item_id, quantity, actor_id=None):
    """The end customer actually used `quantity` cards from a sold order line.
    For units drawn from a CONSIGNMENT supplier who settles on 'redemption', this
    is the moment we owe them. Tracks redeemed_qty per allocation so we never
    double-count. Returns (accrued_sar, error)."""
    quantity = int(quantity or 0)
    if quantity <= 0:
        return 0.0, "Redeemed quantity must be positive."
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        allocs = conn.execute("""SELECT a.id, a.quantity, a.redeemed_qty, a.unit_cost, a.batch_id,
                                        b.supplier_id, s.account_type, s.consignment_settle_on
                                 FROM order_item_allocations a
                                 LEFT JOIN purchase_batches b ON a.batch_id=b.id
                                 LEFT JOIN suppliers s ON b.supplier_id=s.id
                                 WHERE a.order_item_id=? ORDER BY a.id""", (order_item_id,)).fetchall()
        left = quantity
        accrued = 0.0
        for a in allocs:
            if left <= 0:
                break
            room = (a['quantity'] or 0) - (a['redeemed_qty'] or 0)
            if room <= 0:
                continue
            take = min(left, room)
            conn.execute("UPDATE order_item_allocations SET redeemed_qty = redeemed_qty + ? WHERE id=?",
                         (take, a['id']))
            if a['account_type'] == 'consignment' and (a['consignment_settle_on'] or 'sale') == 'redemption':
                amt = round(take * (a['unit_cost'] or 0), 2)
                conn.execute("UPDATE suppliers SET our_outstanding = our_outstanding + ? WHERE id=?",
                             (amt, a['supplier_id']))
                accrued += amt
            left -= take
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return round(accrued, 2), None


def get_supplier_payables():
    """Per-supplier payables for Finance: what we owe, our limit + headroom, and
    the age of the oldest unpaid purchase (a simple aging signal). SAR."""
    conn = get_db()
    today = datetime.now(timezone.utc).date()
    rows = conn.execute("""
        SELECT s.id, s.name, s.account_type, s.our_credit_limit, s.our_outstanding,
               s.settlement_terms_days, s.billing_cycle, s.consignment_settle_on,
               (SELECT COALESCE(SUM(total_cost),0) FROM purchase_batches b
                    WHERE b.supplier_id=s.id AND b.created_at >= date('now','-30 day')) as bought_30d,
               (SELECT MIN(created_at) FROM purchase_batches b
                    WHERE b.supplier_id=s.id AND b.status!='reconciled') as oldest_open_purchase
        FROM suppliers s
        WHERE s.account_type IN ('credit','consignment')
        ORDER BY s.our_outstanding DESC""").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['available'] = round(max(0.0, (d['our_credit_limit'] or 0) - (d['our_outstanding'] or 0)), 2)
        d['over_limit'] = (d['our_outstanding'] or 0) > (d['our_credit_limit'] or 0) + 0.01
        d['oldest_days'] = 0
        if d['oldest_open_purchase']:
            try:
                d['oldest_days'] = max(0, (today - date.fromisoformat(d['oldest_open_purchase'][:10])).days)
            except (ValueError, TypeError):
                d['oldest_days'] = 0
        out.append(d)
    return out


def get_payables_summary():
    """Portfolio payables totals for the Finance/Ops dashboards (SAR)."""
    conn = get_db()
    row = conn.execute("""SELECT
            COALESCE(SUM(our_credit_limit),0) as total_limit,
            COALESCE(SUM(our_outstanding),0) as total_outstanding,
            COUNT(*) as accounts
        FROM suppliers WHERE account_type IN ('credit','consignment')""").fetchone()
    conn.close()
    d = dict(row)
    d['available'] = round(max(0.0, (d['total_limit'] or 0) - (d['total_outstanding'] or 0)), 2)
    return d


def get_cashflow_overview():
    """Cross-functional money picture (SAR): what's owed to us (customers) vs what
    we owe (suppliers), the prepaid float we hold, and a conservative 'safe-to-buy'
    that our thin margins / fast cash-cycle demand. Ties into the Buy Planner's
    urgent restock cost so Ops+Finance can decide when to deploy cash."""
    cust = get_credit_exposure()          # customers who owe us (credit/consignment)
    pay = get_payables_summary()          # suppliers we owe
    conn = get_db()
    wallet_float = conn.execute("SELECT COALESCE(SUM(wallet_balance),0) FROM reseller_profiles").fetchone()[0]
    supplier_overdue = conn.execute("""SELECT COALESCE(SUM(amount),0) FROM supplier_statements
                                       WHERE status='overdue'""").fetchone()[0]
    conn.close()
    recs = get_buy_recommendations()
    urgent_cost = round(sum(r['est_cost'] for r in recs if r['signal'] in ('out', 'reorder')), 2)
    watch_cost = round(sum(r['est_cost'] for r in recs if r['signal'] == 'watch'), 2)

    receivable_in = round(cust['total_outstanding'], 2)     # customers owe us
    payable_out = round(pay['total_outstanding'], 2)        # we owe suppliers
    net_position = round(receivable_in - payable_out, 2)
    # Conservative buying power: what we can commit on supplier credit headroom
    # plus any net receivables — WITHOUT dipping into customers' prepaid float
    # (that money is earmarked for their orders).
    buying_power = round(pay['available'] + max(0.0, net_position), 2)
    return {
        'customer_receivable': receivable_in,
        'customer_overdue': round(cust['overdue_amount'], 2),
        'supplier_payable': payable_out,
        'supplier_overdue': round(supplier_overdue, 2),
        'supplier_headroom': pay['available'],
        'wallet_float': round(wallet_float, 2),
        'net_position': net_position,
        'buying_power': buying_power,
        'urgent_restock_cost': urgent_cost,
        'watch_restock_cost': watch_cost,
        'covers_urgent': buying_power >= urgent_cost,
        'surplus_after_urgent': round(buying_power - urgent_cost, 2),
    }


# ── Supplier statements (period bill of what we owe them) (v22) ──

def _open_supplier_statements_total(conn, supplier_id):
    return conn.execute("""SELECT COALESCE(SUM(amount),0) FROM supplier_statements
                           WHERE supplier_id=? AND status IN ('issued','overdue')""",
                        (supplier_id,)).fetchone()[0]


def unbilled_supplier_amount(supplier_id):
    """What we owe that isn't yet on a statement = outstanding − open statements."""
    conn = get_db()
    row = conn.execute("SELECT our_outstanding FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not row:
        conn.close()
        return 0.0
    v = (row['our_outstanding'] or 0) - _open_supplier_statements_total(conn, supplier_id)
    conn.close()
    return round(max(0.0, v), 2)


def issue_supplier_statement(supplier_id, actor_id=None, auto=False):
    """Record the supplier's period invoice for what we owe but haven't billed yet.
    due date = today + settlement_terms_days. Race-safe. Returns id or None."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not s or (s['account_type'] or 'prepaid') == 'prepaid':
            conn.rollback()
            return None
        unbilled = round(max(0.0, (s['our_outstanding'] or 0)
                              - _open_supplier_statements_total(conn, supplier_id)), 2)
        today = datetime.now(timezone.utc).date()
        if unbilled <= 0.009:
            conn.execute("UPDATE suppliers SET last_statement_at=? WHERE id=?",
                         (today.isoformat(), supplier_id))
            conn.commit()
            return None
        due = (today + timedelta(days=int(s['settlement_terms_days'] or 30))).isoformat()
        period_start = s['last_statement_at'] or today.isoformat()
        conn.execute("""INSERT INTO supplier_statements
                        (supplier_id, amount, status, period_start, period_end, due_at, issued_by)
                        VALUES (?,?,?,?,?,?,?)""",
                     (supplier_id, unbilled, 'issued', period_start, today.isoformat(), due, actor_id))
        sid_ = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE suppliers SET last_statement_at=? WHERE id=?", (today.isoformat(), supplier_id))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return sid_


def get_supplier_statements(supplier_id):
    conn = get_db()
    rows = conn.execute("SELECT * FROM supplier_statements WHERE supplier_id=? ORDER BY id DESC",
                        (supplier_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_supplier_statements(status=None):
    conn = get_db()
    q = """SELECT ss.*, s.name as supplier_name FROM supplier_statements ss
           JOIN suppliers s ON ss.supplier_id=s.id"""
    params = []
    if status:
        q += " WHERE ss.status=?"
        params.append(status)
    q += " ORDER BY ss.due_at, ss.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run_supplier_statement_cycle(force=False):
    """Daily: auto-issue supplier statements per billing cycle and flag ones we've
    let go past due (we're late paying → supply risk). Returns action strings."""
    conn = get_db()
    today = datetime.now(timezone.utc).date()
    rows = conn.execute("""SELECT id, name, account_type, billing_cycle, last_statement_at,
                                  our_outstanding
                           FROM suppliers WHERE account_type IN ('credit','consignment')""").fetchall()
    conn.close()
    actions = []
    for r in rows:
        # mark overdue + alert (we owe and are late)
        conn2 = get_db()
        overdue = conn2.execute("""SELECT id FROM supplier_statements
                                   WHERE supplier_id=? AND status='issued' AND due_at < ?""",
                                (r['id'], today.isoformat())).fetchall()
        if overdue:
            conn2.execute("""UPDATE supplier_statements SET status='overdue'
                             WHERE supplier_id=? AND status='issued' AND due_at < ?""",
                          (r['id'], today.isoformat()))
            conn2.commit()
            notify(get_user_ids_by_role('finance', 'ops', 'cco'),
                   "⚠️ Supplier payment overdue",
                   f"We have an overdue balance with {r['name']}. Pay it to protect supply.",
                   "/finance/payables")
            actions.append(f"SUPPLIER-OVERDUE: {r['name']}")
        conn2.close()
        # auto-issue when the cycle elapsed
        base = r['last_statement_at']
        due_cycle = True
        if base:
            try:
                due_cycle = (today - date.fromisoformat(base[:10])).days >= _cycle_days(r['billing_cycle'])
            except (ValueError, TypeError):
                due_cycle = True
        if (force or due_cycle) and (r['our_outstanding'] or 0) > 0.01:
            if issue_supplier_statement(r['id'], auto=True):
                actions.append(f"SUPPLIER-STATEMENT: {r['name']}")
    return actions


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
                 reason='', created_by=None, method='offline'):
    """Ops records a stock purchase. method: 'offline' (stock we hold) or 'api'
    (pulled from the supplier's API on demand). Captures the best available cost
    at this moment; overpaying beyond tolerance alerts BD + CCO (governance)."""
    method = 'api' if method == 'api' else 'offline'
    best = get_best_source(product_rowid)
    best_cost = best['supplier_cost'] if best else None
    variance = round((unit_cost - best_cost) * quantity, 2) if best_cost is not None else 0

    total = round(unit_cost * quantity, 2)
    conn = get_db()
    p = conn.execute("SELECT product_name, merchant FROM products WHERE id=?", (product_rowid,)).fetchone()
    s = conn.execute("""SELECT name, account_type, our_credit_limit, our_outstanding
                        FROM suppliers WHERE id=?""", (supplier_id,)).fetchone()
    if not p or not s:
        conn.close()
        return None, "Product or supplier not found."
    account_type = s['account_type'] or 'prepaid'
    # v25: CREDIT accrues at purchase; CONSIGNMENT accrues later (at sale or
    # redemption), so a consignment purchase adds no payable here.
    accrue_now = account_type == 'credit'
    # v25: exceeding our limit with a supplier WARNS but never blocks — the limit
    # may have been extended informally (busy season, invoice delays, etc.).
    over_limit = accrue_now and ((s['our_outstanding'] or 0) + total) > (s['our_credit_limit'] or 0) + 0.01
    conn.execute("""INSERT INTO purchase_batches
                    (supplier_id, product_rowid, quantity, remaining_qty, unit_cost, total_cost,
                     best_cost_at_purchase, sourcing_variance, reason, invoice_ref, created_by, method)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (supplier_id, product_rowid, quantity, quantity, unit_cost,
                  total, best_cost, max(variance, 0),
                  reason, invoice_ref, created_by, method))
    bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if accrue_now:
        conn.execute("UPDATE suppliers SET our_outstanding = our_outstanding + ? WHERE id=?",
                     (total, supplier_id))
    conn.commit()
    conn.close()

    if over_limit:
        notify(get_user_ids_by_role('finance', 'ops', 'cco'),
               "⚠️ Over supplier credit limit (allowed)",
               f"Purchase #{bid} pushed what we owe {s['name']} over their "
               f"{s['our_credit_limit']:,.0f} SAR limit — the buy went through; please confirm "
               f"the limit was extended.", "/finance/payables")

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
    Units without recorded stock get a NULL-batch allocation at standard cost.
    v25: a unit drawn from a CONSIGNMENT supplier who settles on 'sale' becomes
    payable to that supplier the moment it is sold here."""
    items = conn.execute("""SELECT oi.id, oi.product_rowid, oi.quantity, p.cost as std_cost
                            FROM order_items oi LEFT JOIN products p ON oi.product_rowid=p.id
                            WHERE oi.order_id=?""", (order_id,)).fetchall()
    for it in items:
        need = it['quantity']
        batches = conn.execute("""SELECT b.id, b.remaining_qty, b.unit_cost, b.supplier_id,
                                          s.account_type, s.consignment_settle_on
                                   FROM purchase_batches b JOIN suppliers s ON b.supplier_id=s.id
                                   WHERE b.product_rowid=? AND b.remaining_qty>0
                                   ORDER BY b.created_at, b.id""", (it['product_rowid'],)).fetchall()
        for b in batches:
            if need <= 0:
                break
            take = min(need, b['remaining_qty'])
            conn.execute("""INSERT INTO order_item_allocations (order_item_id, batch_id, quantity, unit_cost)
                            VALUES (?,?,?,?)""", (it['id'], b['id'], take, b['unit_cost']))
            conn.execute("UPDATE purchase_batches SET remaining_qty=remaining_qty-? WHERE id=?",
                         (take, b['id']))
            if b['account_type'] == 'consignment' and (b['consignment_settle_on'] or 'sale') == 'sale':
                conn.execute("UPDATE suppliers SET our_outstanding = our_outstanding + ? WHERE id=?",
                             (round(take * b['unit_cost'], 2), b['supplier_id']))
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
        url = webhook_url.strip() or None
        conn.execute("UPDATE reseller_profiles SET webhook_url=? WHERE id=?", (url, reseller_id))
        # v16: mint a signing secret the first time a webhook URL is configured
        if url:
            cur = conn.execute("SELECT webhook_secret FROM reseller_profiles WHERE id=?",
                               (reseller_id,)).fetchone()
            if not (cur and cur['webhook_secret']):
                conn.execute("UPDATE reseller_profiles SET webhook_secret=? WHERE id=?",
                             (f"whsec_{_sec.token_hex(24)}", reseller_id))
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

# ── Webhooks: durable queue + retries + HMAC signatures (v16) ────

WEBHOOK_MAX_ATTEMPTS = 6
# backoff after each failed attempt (seconds): 30s, 2m, 10m, 30m, 2h, 6h
WEBHOOK_BACKOFF = [30, 120, 600, 1800, 7200, 21600]
_webhook_worker_started = False


def _sign_webhook(secret, timestamp, body):
    """HMAC-SHA256 over "<timestamp>.<body>" — the client recomputes this with
    their shared secret to prove the call really came from OneCard."""
    import hmac
    mac = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256)
    return mac.hexdigest()


def send_webhook(reseller_id, event, payload):
    """Public entry point (kept for all existing call sites): enqueue an event
    for durable, retried, signed delivery. Non-blocking — never touches the
    network on the request path."""
    enqueue_webhook(reseller_id, event, payload)


def enqueue_webhook(reseller_id, event, payload):
    import json as _json
    conn = get_db()
    row = conn.execute("SELECT webhook_url FROM reseller_profiles WHERE id=?", (reseller_id,)).fetchone()
    url = row['webhook_url'] if row else None
    if not url:
        conn.close()
        return None
    conn.execute("""INSERT INTO webhook_deliveries
                    (reseller_id, event, url, payload, status, attempts, next_attempt_at)
                    VALUES (?,?,?,?,'pending',0,?)""",
                 (reseller_id, event, url, _json.dumps({'event': event, 'data': payload}),
                  datetime.now(timezone.utc).isoformat()))
    did = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return did


def deliver_due_webhooks(limit=50):
    """Deliver every pending webhook whose next_attempt_at is due. On a non-2xx
    or network error, retry with exponential backoff up to WEBHOOK_MAX_ATTEMPTS,
    then mark it 'failed'. Synchronous + idempotent — safe to call from the
    background worker or directly (tests). Returns (delivered, failed, retried)."""
    import json as _json
    import urllib.request as _rq
    now = datetime.now(timezone.utc)
    conn = get_db()
    rows = conn.execute("""SELECT wd.*, cp.webhook_secret FROM webhook_deliveries wd
                           JOIN reseller_profiles cp ON wd.reseller_id=cp.id
                           WHERE wd.status='pending' AND (wd.next_attempt_at IS NULL OR wd.next_attempt_at<=?)
                           ORDER BY wd.id LIMIT ?""",
                        (now.isoformat(), limit)).fetchall()
    conn.close()
    delivered = failed = retried = 0
    for wd in rows:
        body = wd['payload'] or _json.dumps({'event': wd['event'], 'data': {}})
        ts = str(int(now.timestamp()))
        headers = {'Content-Type': 'application/json', 'X-OneCard-Event': wd['event'],
                   'X-OneCard-Timestamp': ts, 'X-OneCard-Delivery': str(wd['id'])}
        if wd['webhook_secret']:
            headers['X-OneCard-Signature'] = 'sha256=' + _sign_webhook(wd['webhook_secret'], ts, body)
        status_code, err = 0, None
        try:
            req = _rq.Request(wd['url'], data=body.encode(), headers=headers)
            status_code = _rq.urlopen(req, timeout=5).status
        except Exception as e:  # noqa: BLE001 - never let a bad endpoint break us
            err = str(e)[:200]
            status_code = getattr(e, 'code', 0) or 0
        ok = 200 <= (status_code or 0) < 300
        attempts = (wd['attempts'] or 0) + 1
        conn2 = get_db()
        if ok:
            conn2.execute("""UPDATE webhook_deliveries SET status='delivered', status_code=?,
                             attempts=?, delivered_at=CURRENT_TIMESTAMP, last_error=NULL WHERE id=?""",
                          (status_code, attempts, wd['id']))
            delivered += 1
        elif attempts >= WEBHOOK_MAX_ATTEMPTS:
            conn2.execute("""UPDATE webhook_deliveries SET status='failed', status_code=?,
                             attempts=?, last_error=? WHERE id=?""",
                          (status_code, attempts, err or f'HTTP {status_code}', wd['id']))
            failed += 1
        else:
            delay = WEBHOOK_BACKOFF[min(attempts - 1, len(WEBHOOK_BACKOFF) - 1)]
            nxt = (now + timedelta(seconds=delay)).isoformat()
            conn2.execute("""UPDATE webhook_deliveries SET status='pending', status_code=?,
                             attempts=?, next_attempt_at=?, last_error=? WHERE id=?""",
                          (status_code, attempts, nxt, err or f'HTTP {status_code}', wd['id']))
            retried += 1
        conn2.commit()
        conn2.close()
    return delivered, failed, retried


def start_webhook_worker(interval=5):
    """Start a single background daemon that drains the webhook queue. Guarded
    so it only ever starts once per process."""
    global _webhook_worker_started
    if _webhook_worker_started:
        return
    _webhook_worker_started = True
    import threading, time as _time

    def _loop():
        while True:
            try:
                deliver_due_webhooks()
            except Exception:  # noqa: BLE001 - worker must never die
                pass
            _time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name='webhook-worker').start()


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
    run_prospect_suspension()
    run_statement_cycle()
    run_supplier_statement_cycle()
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
