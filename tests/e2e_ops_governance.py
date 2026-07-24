"""E2E test for OneCard Platform v5 — Operations role + governance layer."""
import urllib.request, urllib.parse, http.cookiejar as cj
import re as _re
import json, sqlite3, uuid, os

BASE = 'http://127.0.0.1:8000'
DB = 'onecard.db'
results = []

def check(name, ok, extra=''):
    results.append((name, ok, extra))
    print(('PASS ' if ok else 'FAIL ') + name + (' | ' + extra if extra else ''))

def login(email, pw):
    jar = cj.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # v7 hardening: fetch the CSRF token first, send it with every POST
    page = op.open(BASE + '/login').read().decode('utf-8', 'replace')
    m = _re.search(r'name="csrf-token" content="([^"]+)"', page)
    op._csrf = m.group(1) if m else ''
    op.open(BASE + '/login', data=urllib.parse.urlencode(
        {'email': email, 'password': pw, '_csrf': op._csrf}).encode())
    return op

def get(op, path):
    r = op.open(BASE + path)
    return r.status, r.read().decode('utf-8', 'replace')

def post(op, path, data):
    data = dict(data)
    data.setdefault('_csrf', getattr(op, '_csrf', ''))
    r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
    return r.status, r.read().decode('utf-8', 'replace')

def q(sql, *args):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return rows

# ── 1. Ops login + pages ──
ops = login('ops@onecard.com', 'Ops2025!')
s, b = get(ops, '/ops')
check('ops dashboard', s == 200 and 'Operations Dashboard' in b)
s, b = get(ops, '/ops/products')
check('ops products page', s == 200 and 'Manage Products' in b)
s, b = get(ops, '/ops/suppliers')
check('ops suppliers page', s == 200 and 'Suppliers' in b)
s, b = get(ops, '/ops/pricelog')
check('ops price log page', s == 200)
s, b = get(ops, '/ops/bulk')
check('ops bulk page', s == 200 and 'Upload Price File' in b)

# root redirect for ops
s, b = get(ops, '/')
check('ops home redirect', 'Operations Dashboard' in b)

# ── 2. Ops adds a product ──
pname = f'E2E Test Card $10 {uuid.uuid4().hex[:6]}'
s, b = post(ops, '/ops/products/add', {
    'product_id': 'E2E-9999', 'product_name': pname, 'merchant': 'E2E Merchant',
    'merchant_id': '', 'category': 'Gaming', 'country': 'Saudi Arabia',
    'region': 'GCC', 'currency': 'SAR', 'cost': '35', 'default_price': '37', 'face_value': '40'})
check('ops added product', 'Product added' in b)
prod = q("SELECT * FROM products WHERE product_name=?", pname)
check('product in DB, is_new + active + added_at', bool(prod) and prod[0]['is_new'] == 1
      and prod[0]['is_active'] == 1 and prod[0]['added_at'] is not None)
pid = prod[0]['id']
log = q("SELECT * FROM price_change_log WHERE product_rowid=? AND action='created'", pid)
check('creation logged', bool(log))

# ── 3. Ops edits price → margin alert ──
s, b = post(ops, f'/ops/products/{pid}/edit', {
    'product_name': pname, 'merchant': 'E2E Merchant', 'category': 'Gaming',
    'country': 'Saudi Arabia', 'region': 'GCC', 'currency': 'SAR',
    'cost': '36.9', 'default_price': '37', 'face_value': '40', 'is_new': '1'})
check('price edit saved', 'logged' in b)
log = q("SELECT * FROM price_change_log WHERE product_rowid=? AND field='cost'", pid)
check('cost change audited', bool(log) and log[0]['old_value'] == '35.0')

admin = login('admin@onecard.com', 'OneCard2025!')
s, b = get(admin, '/notifications')
check('margin alert notified admin', 'Low margin after price update' in b)

# ── 4. Deactivate product → hidden from reseller ──
s, b = post(ops, f'/ops/products/{pid}/toggle', {'active': '0'})
check('product deactivated', 'deactivated' in b.lower())
active = q("SELECT is_active FROM products WHERE id=?", pid)
check('DB is_active=0', active[0]['is_active'] == 0)

# reseller cannot see it (use existing reseller from v4 or create one)
sales = login('sales@onecard.com', 'Sales2025!')
email = f"e2e5_{uuid.uuid4().hex[:8]}@test.com"
s, b = post(sales, '/sales/register', {
    'company_name': 'E2E5 Co ' + email[5:13], 'contact_name': 'T', 'contact_email': email,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8],
    'password': 'Test123!', 'expected_sales': '60000', 'client_type': 'Gaming Store',
    'countries': 'Saudi Arabia'})
_nc = sqlite3.connect(DB); _nc.execute("UPDATE reseller_profiles SET nda_accepted_at=CURRENT_TIMESTAMP WHERE user_id=(SELECT id FROM users WHERE email=?)", (email,)); _nc.commit(); _nc.close()
res = login(email, 'Test123!')
s, b = get(res, '/reseller/products')
check('deactivated product hidden from reseller', pname not in b)

# reactivate & verify visible
post(ops, f'/ops/products/{pid}/toggle', {'active': '1'})
s, b = get(res, '/reseller/products')
check('reactivated product visible', pname in b)

# ── 5. Suppliers CRUD ──
s, b = post(ops, '/ops/suppliers', {
    'supplier_id': '', 'name': 'E2E Supplier Ltd', 'contact_person': 'Sam',
    'email': 's@e2e.com', 'phone': '+966500000000', 'payment_terms': 'Net 30',
    'notes': 'test', 'merchants': 'E2E Merchant'})
check('supplier created', 'Supplier saved' in b and 'E2E Supplier Ltd' in b)
sup = q("SELECT * FROM suppliers WHERE name='E2E Supplier Ltd'")
merch = q("SELECT merchant FROM supplier_merchants WHERE supplier_id=?", sup[0]['id'])
check('supplier-merchant link', merch and merch[0]['merchant'] == 'E2E Merchant')

# ── 6. Bulk price import ──
import pandas as pd
bulk_path = os.path.join(os.environ.get('TEMP', '.'), 'e2e_bulk.xlsx')
pd.DataFrame([{'Product ID': 'E2E-9999', 'Cost Price': 30, 'Default Reseller Price': 33,
               'Recommended Retail Price (Resellers currency)': 40}]).to_excel(bulk_path, index=False)
boundary = '----e2ebulk'
fc = open(bulk_path, 'rb').read()
body = (f'--{boundary}\r\nContent-Disposition: form-data; name="_csrf"\r\n\r\n{ops._csrf}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="pricefile"; filename="e2e_bulk.xlsx"\r\n'
        f'Content-Type: application/octet-stream\r\n\r\n').encode() + fc + f'\r\n--{boundary}--\r\n'.encode()
req = urllib.request.Request(BASE + '/ops/bulk', data=body,
                             headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
b = ops.open(req).read().decode('utf-8', 'replace')
check('bulk preview shows diff', 'changes that will be applied' in b.lower() or 'Preview' in b)
# extract token
import re
m = re.search(r'name="token" value="([^"]+)"', b)
check('bulk token present', bool(m))
if m:
    s, b = post(ops, '/ops/bulk/apply', {'token': m.group(1)})
    check('bulk applied', 'Bulk update applied' in b and '1 products' in b)
    p = q("SELECT cost, default_price FROM products WHERE id=?", pid)
    check('bulk prices in DB', p[0]['cost'] == 30 and p[0]['default_price'] == 33)
    log = q("SELECT * FROM price_change_log WHERE product_rowid=? AND source='bulk_import'", pid)
    check('bulk changes audited as bulk_import', bool(log))

# ── 7. Governance: contracted_at + team page + targets + scorecard ──
rid = q("""SELECT cp.id, cp.user_id FROM reseller_profiles cp
    JOIN users u ON cp.user_id=u.id WHERE u.email=?""", email)[0]
post(sales, f"/resellers/{rid['id']}/contract", {'status': 'contracted'})
ca = q("SELECT contracted_at FROM reseller_profiles WHERE id=?", rid['id'])
check('contracted_at recorded', ca[0]['contracted_at'] is not None)

cco = login('cco@onecard.com', 'Cco2025!')
s, b = get(cco, '/team')
check('cco team performance page', s == 200 and 'Sales Team Performance' in b and 'Sales Manager' in b)
s, b = get(admin, '/team')
check('admin can open team page', s == 200)

# set targets
sales_uid = q("SELECT id FROM users WHERE email='sales@onecard.com'")[0]['id']
from datetime import datetime
ym = datetime.now().strftime('%Y-%m')
s, b = post(cco, '/team/targets', {'ym': ym, 'sales_user_id': sales_uid,
                                   'target_new': '5', 'target_value': '500000'})
check('targets saved', 'Targets saved' in b)
s, b = get(cco, f'/team?ym={ym}')
check('targets visible with attainment', '/5' in b or '500,000' in b)

s, b = get(sales, '/sales/scorecard')
check('sales scorecard page', s == 200 and 'My Scorecard' in b and 'My Targets' in b)
check('scorecard shows portfolio lifecycle', 'lifecycle view' in b)

# lifecycle chips on my_resellers
s, b = get(sales, '/sales/resellers')
check('lifecycle chips on my resellers', 'Stage' in b)

# ── 8. New product notification to merchant buyers ──
# make E2E5 buy from E2E Merchant, then ops adds another product of that merchant
# top up wallet directly in DB for speed
c = sqlite3.connect(DB); c.execute("UPDATE reseller_profiles SET wallet_balance=10000 WHERE id=?", (rid['id'],)); c.commit(); c.close()
items = json.dumps([{'product_id': pid, 'quantity': 2}])
s, b = post(res, '/reseller/orders', {'items_json': items})
check('order placed for merchant', 'placed successfully' in b)
pname2 = f'E2E Followup Card {uuid.uuid4().hex[:6]}'
post(ops, '/ops/products/add', {
    'product_id': 'E2E-10000', 'product_name': pname2, 'merchant': 'E2E Merchant',
    'merchant_id': '', 'category': 'Gaming', 'country': 'Saudi Arabia',
    'region': 'GCC', 'currency': 'SAR', 'cost': '50', 'default_price': '53', 'face_value': '55'})
s, b = get(res, '/notifications')
check('buyer notified of new merchant product', 'New product from a merchant you buy' in b)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
