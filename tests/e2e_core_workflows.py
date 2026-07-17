"""End-to-end workflow test for OneCard Platform v4."""
import urllib.request, urllib.parse, http.cookiejar as cj
import re as _re
import json, io, uuid

BASE = 'http://127.0.0.1:8000'
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

# ── 1. All role logins + dashboards ──
admin = login('admin@onecard.com', 'OneCard2025!')
s, b = get(admin, '/admin')
check('admin dashboard', s == 200 and 'Compliance' in b)

sales = login('sales@onecard.com', 'Sales2025!')
s, b = get(sales, '/sales')
check('sales dashboard', s == 200 and 'Forecasts' in b)

cco = login('cco@onecard.com', 'Cco2025!')
s, b = get(cco, '/cco')
check('cco dashboard', s == 200 and 'Discount Approvals' in b)

fin = login('finance@onecard.com', 'Finance2025!')
s, b = get(fin, '/finance')
check('finance dashboard', s == 200 and 'Top-up' in b)

# ── 2. Sales registers reseller with client type + countries ──
email = f"e2e_{uuid.uuid4().hex[:8]}@test.com"
s, b = post(sales, '/sales/register', {
    'company_name': 'E2E Trading Co', 'contact_name': 'E2E Tester',
    'contact_email': email, 'password': 'Test123!',
    'expected_sales': '250000', 'notes': 'e2e', 'client_type': 'Bank',
    'countries': 'Saudi Arabia'})
check('register reseller (Gold expected)', 'Gold' in b or 'registered successfully' in b)

# ── 3. Reseller portal pages ──
res = login(email, 'Test123!')
for path, marker in [('/reseller', 'E2E Trading'), ('/reseller/merchants', 'Browse by Merchant'),
                     ('/reseller/products', 'Master Product'), ('/reseller/calculator', 'Basket'),
                     ('/reseller/recommended', 'Recommended'), ('/reseller/forecast', 'Purchase Plan'),
                     ('/reseller/wallet', 'Wallet'), ('/reseller/analysis', 'Analysis'),
                     ('/notifications', 'Notifications')]:
    s, b = get(res, path)
    check(f'reseller {path}', s == 200 and marker in b)

# recommended personalization check
s, b = get(res, '/reseller/recommended')
check('recommended personalized for Bank', 'Top Picks for Bank' in b)
check('recommended shows market section', 'Strong in Your Markets' in b)

# orders locked pre-contract
s, b = get(res, '/reseller/orders')
check('orders locked pre-contract', 'unlocks after your contract' in b)

# no export for reseller
s, b = get(res, '/reseller/products')
check('no export button (reseller products)', 'Export' not in b)
s, b = get(res, '/reseller/merchants')
check('no export button (reseller merchants)', 'Export' not in b)
s, b = get(sales, '/sales/catalogue')
check('no export button (sales catalogue)', 'exportMasterCSV' not in b)

# ── 4. Reseller submits forecast ──
items = json.dumps([{'type': 'merchant', 'merchant': 'FRiENDi mobile', 'value': 60000}])
s, b = post(res, '/reseller/forecast', {'items_json': items, 'note': 'e2e plan'})
check('forecast submitted', 'submitted to your account manager' in b)

s, b = get(sales, '/sales/forecasts')
check('sales sees forecast', 'E2E Trading Co' in b)
s, b = get(sales, '/notifications')
check('sales notified of forecast', 'purchase forecast' in b.lower())

# ── 5. Contract signing ──
import sqlite3
conn = sqlite3.connect('onecard.db'); conn.row_factory = sqlite3.Row
rid = conn.execute("""SELECT cp.id FROM reseller_profiles cp
    JOIN users u ON cp.user_id=u.id WHERE u.email=?""", (email,)).fetchone()['id']
conn.close()
s, b = post(sales, f'/resellers/{rid}/contract', {'status': 'contracted'})
check('contract signed', 'Contract marked as signed' in b or 'ordering is now enabled' in b.lower())

s, b = get(res, '/reseller/orders')
check('orders unlocked post-contract', 'Build New Order' in b)

# ── 6. Wallet top-up with receipt upload (multipart) ──
boundary = '----e2eboundary'
receipt_content = b'%PDF-1.4 fake e2e receipt'
body = (f'--{boundary}\r\nContent-Disposition: form-data; name="_csrf"\r\n\r\n{res._csrf}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="amount"\r\n\r\n100000\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="bank_reference"\r\n\r\nTRX-E2E-001\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="note"\r\n\r\ne2e topup\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="receipt"; filename="receipt.pdf"\r\n'
        f'Content-Type: application/pdf\r\n\r\n').encode() + receipt_content + f'\r\n--{boundary}--\r\n'.encode()
req = urllib.request.Request(BASE + '/reseller/wallet', data=body,
                             headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
r = res.open(req)
b = r.read().decode('utf-8', 'replace')
check('wallet receipt uploaded', 'finance team will verify' in b)

s, b = get(fin, '/finance')
check('finance sees pending topup', 'E2E Trading Co' in b and 'TRX-E2E-001' in b)

# finance approves
conn = sqlite3.connect('onecard.db'); conn.row_factory = sqlite3.Row
txn = conn.execute("SELECT id FROM wallet_transactions WHERE reseller_id=? AND type='topup' AND status='pending'", (rid,)).fetchone()
conn.close()
s, b = post(fin, f'/finance/review/{txn["id"]}', {'decision': 'approve', 'note': 'verified'})
check('finance approved topup', 'credited' in b)

s, b = get(res, '/reseller/wallet')
check('wallet balance credited', '100,000' in b)

# ── 7. Reseller places order ──
conn = sqlite3.connect('onecard.db'); conn.row_factory = sqlite3.Row
prod = conn.execute("SELECT id, product_name FROM products WHERE merchant='FRiENDi mobile' LIMIT 1").fetchone()
conn.close()
items = json.dumps([{'product_id': prod['id'], 'quantity': 5}])
s, b = post(res, '/reseller/orders', {'items_json': items})
check('order placed', 'placed successfully' in b)
check('forecast vs actual visible', 'Forecast vs Actual' in b and 'FRiENDi' in b)

s, b = get(res, '/reseller/analysis')
check('analysis shows data', 'Total Orders' in b and 'Insights' in b)

# order exceeding balance rejected
items = json.dumps([{'product_id': prod['id'], 'quantity': 999999}])
s, b = post(res, '/reseller/orders', {'items_json': items})
check('over-balance order rejected', 'Insufficient wallet balance' in b)

# ── 8. Discount request → CCO approval → auto-applied override ──
# reseller price before
conn = sqlite3.connect('onecard.db'); conn.row_factory = sqlite3.Row
before = conn.execute("SELECT COUNT(*) FROM merchant_share_overrides WHERE reseller_id=?", (rid,)).fetchone()[0]
conn.close()

s, b = post(sales, '/sales/discounts', {
    'reseller_id': rid, 'merchant': 'FRiENDi mobile', 'requested_share': '70',
    'current_sales': '60000', 'projected_sales': '120000', 'note': 'e2e discount'})
check('discount request sent', 'sent to the CCO' in b)

s, b = get(cco, '/cco')
check('cco sees request with economics', 'E2E Trading Co' in b and 'Net impact' in b)

conn = sqlite3.connect('onecard.db'); conn.row_factory = sqlite3.Row
dr = conn.execute("SELECT id FROM discount_requests WHERE reseller_id=? AND status='pending'", (rid,)).fetchone()
conn.close()
s, b = post(cco, f'/cco/decide/{dr["id"]}', {'decision': 'approve', 'decision_note': 'ok e2e'})
check('cco approved', 'override applied automatically' in b)

conn = sqlite3.connect('onecard.db'); conn.row_factory = sqlite3.Row
after = conn.execute("SELECT share_pct FROM merchant_share_overrides WHERE reseller_id=? AND merchant='FRiENDi mobile'", (rid,)).fetchone()
conn.close()
check('override in DB = 70%', after and after['share_pct'] == 70)

s, b = get(res, '/reseller/products')
check('reseller sees special rate star', 'Special approved rate' in b or 'has_override' in b)
s, b = get(res, '/notifications')
check('reseller notified of better pricing', 'Better pricing unlocked' in b)

# ── 9. Compliance check (force) ──
# set expected very high so it triggers warning, then run
conn = sqlite3.connect('onecard.db')
conn.execute("UPDATE reseller_profiles SET expected_monthly_sales=99999999 WHERE id=?", (rid,))
conn.commit(); conn.close()
s, b = post(admin, '/admin/compliance/run', {})
check('compliance run executed', 'Compliance check done' in b)
s, b = get(res, '/notifications')
check('reseller warned re commitment', 'commitment not met' in b)
conn = sqlite3.connect('onecard.db'); conn.row_factory = sqlite3.Row
st = conn.execute("SELECT compliance_status, grace_until FROM reseller_profiles WHERE id=?", (rid,)).fetchone()
conn.close()
check('grace period set', st['compliance_status'] == 'warning' and st['grace_until'] is not None,
      f"status={st['compliance_status']} until={st['grace_until']}")

# ── 10. Preview mode read-only ──
# sales enters preview of this reseller then tries to submit forecast
s, b = get(sales, f'/sales/preview/{rid and 0 or 0}')  # need user_id not rid
conn = sqlite3.connect('onecard.db'); conn.row_factory = sqlite3.Row
ruid = conn.execute("SELECT user_id FROM reseller_profiles WHERE id=?", (rid,)).fetchone()['user_id']
conn.close()
s, b = get(sales, f'/sales/preview/{ruid}')
check('preview entered', 'Preview Mode' in b or 'portal preview' in b.lower())
items = json.dumps([{'type': 'merchant', 'merchant': 'FRiENDi mobile', 'value': 1000}])
s, b = post(sales, '/reseller/forecast', {'items_json': items})
check('preview is read-only', 'read-only' in b)
get(sales, '/sales/preview/exit')

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
