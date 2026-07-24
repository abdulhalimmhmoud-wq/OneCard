"""E2E test for v8: multi-select UX, multi client types, CCO/BD roles,
Deal Pipeline, Sourcing Intelligence redesign, Issuing Hub."""
import urllib.request, urllib.parse, urllib.error, http.cookiejar as cj
import re as _re
import json, sqlite3, os, sys, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import models   # noqa: E402 — codes/PINs are encrypted at rest (v10)

BASE = 'http://127.0.0.1:8000'
DB = os.path.join(ROOT, 'onecard.db')
results = []

def check(name, ok, extra=''):
    results.append((name, ok, extra))
    print(('PASS ' if ok else 'FAIL ') + name + (' | ' + extra if extra else ''))

def login(email, pw):
    jar = cj.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = op.open(BASE + '/login').read().decode('utf-8', 'replace')
    m = _re.search(r'name="csrf-token" content="([^"]+)"', page)
    op._csrf = m.group(1) if m else ''
    op.open(BASE + '/login', data=urllib.parse.urlencode(
        {'email': email, 'password': pw, '_csrf': op._csrf}).encode())
    return op

def get(op, path):
    try:
        r = op.open(BASE + path)
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def post(op, path, data):
    data = dict(data)
    data.setdefault('_csrf', getattr(op, '_csrf', ''))
    try:
        r = op.open(BASE + path, data=urllib.parse.urlencode(data, doseq=True).encode())
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]
    c.close()
    return rows

# ── 1. Multi-select filters shipped ──
res = login('khalid@alnoor-digital.com', 'Demo123!')
s, b = get(res, '/reseller/products')
check('reseller products has multiselect divs', 'id="merchantFilter"' in b and 'msCreate' in b)
s, b = get(res, '/reseller/merchants')
check('reseller merchants multiselect', 'msCreate' in b)
sales = login('sales@onecard.com', 'Sales2025!')
s, b = get(sales, '/sales/catalogue')
check('sales catalogue multiselect', 'msCreate' in b)
admin = login('admin@onecard.com', 'OneCard2025!')
s, b = get(admin, '/admin/catalogue')
check('admin catalogue multiselect', 'msCreate' in b)
appjs = urllib.request.urlopen(BASE + '/static/app.js').read().decode()
check('app.js has msCreate component', 'function msCreate' in appjs and 'msMatch' in appjs)

# ── 2. Ops product form fixed ──
ops = login('ops@onecard.com', 'Ops2025!')
s, b = get(ops, '/ops/products/add')
check('merchant datalist present', 'id="merchantsList"' in b and b.count('<option value=') > 100)
check('eSIM countries in form', 'eSIM - ' in b)
check('canonical categories present', 'Health &amp; Fitness' in b or 'Health & Fitness' in b)

# ── 3. Multi client types ──
email = f"v8_{uuid.uuid4().hex[:8]}@test.com"
s, b = post(sales, '/sales/register', {
    'company_name': 'V8 Multi Co ' + email[3:11], 'contact_name': 'T', 'contact_email': email,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8],
    'password': 'Test123!', 'expected_sales': '80000',
    'client_types': ['Bank', 'Fintech / Wallet'], 'countries': 'Saudi Arabia'})
check('registered with 2 client types', 'registered successfully' in b)
rid = q("""SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id
           WHERE u.email=?""", email)[0]['id']
types = [r['client_type'] for r in q("SELECT client_type FROM reseller_client_types WHERE reseller_id=?", rid)]
check('both types stored', sorted(types) == ['Bank', 'Fintech / Wallet'], str(types))
r2 = login(email, 'Test123!')
s, b = get(r2, '/reseller/recommended')
check('recommended combines types', 'Bank / Fintech / Wallet' in b or 'Bank' in b and 'Fintech' in b)

# ── 4. Roles: CCO = full control ──
cco = login('cco@onecard.com', 'Cco2025!')
s, b = get(cco, '/')
check('cco lands on admin dashboard', 'Admin Dashboard' in b)
s, b = get(cco, '/admin/users')
check('cco can manage users', s == 200 and 'Create New User' in b)
s, b = get(cco, '/ops/issuing')
check('cco can open issuing hub', s == 200)

# ── 5. BD role + Deal Pipeline ──
bd = login('bd@onecard.com', 'Bd2025!')
s, b = get(bd, '/')
check('bd lands on bd dashboard', 'Business Development' in b)
s, b = get(bd, '/sourcing-intel')
check('bd can see sourcing intelligence', s == 200)
s, b = get(bd, '/admin')
check('bd blocked from admin', s != 200 or 'Admin Dashboard' not in b)

title = f'V8 deal {uuid.uuid4().hex[:6]}'
s, b = post(bd, '/deals', {'type': 'better_rate', 'title': title,
                           'merchant': 'PUBG', 'supplier_name': '',
                           'details': 'test', 'expected_terms': 'rate 5%'})
check('bd submitted deal', 'Operations were notified' in b)
s, b = get(ops, '/notifications')
check('ops notified of deal', 'Business Development' in b)
did = q("SELECT id FROM bd_requests WHERE title=?", title)[0]['id']
s, b = post(ops, f'/deals/{did}/status', {'status': 'done', 'note': 'entered'})
check('ops marked deal done', 'marked done' in b)
s, b = get(bd, '/notifications')
check('bd notified deal done', 'DONE' in b)
# bd cannot change status
s, b = post(bd, f'/deals/{did}/status', {'status': 'rejected'})
check('bd cannot update status', 'Only Operations' in b)

# ── 6. Sourcing Intelligence redesign ──
s, b = get(cco, '/sourcing-intel')
check('intel: Do This Now section', 'Do This Now' in b)
check('intel: glossary present', 'What the terms mean' in b)
check('intel: plain-language variance', 'Bought Above Best Price' in b)

# ── 7. Issuing Hub ──
s, b = get(ops, '/ops/issuing')
check('issuing overview + partner', 'Chef Burger KSA' in b and 'Codes in Stock' in b)
s, b = get(ops, '/ops/issuing/products')
check('program listed with stock', 'Chef Burger Gift Card' in b)

# order more than stock -> rejected
prow = q("SELECT id FROM products WHERE is_issued=1 LIMIT 1")[0]['id']
items = json.dumps([{'product_id': prow, 'quantity': 999999}])
s, b = post(res, '/reseller/orders', {'items_json': items})
check('over-stock issued order rejected', 'codes left' in b)

# buy 3 -> codes delivered
items = json.dumps([{'product_id': prow, 'quantity': 3}])
s, b = post(res, '/reseller/orders', {'items_json': items})
placed = 'placed successfully' in b
check('issued product order placed', placed)
if placed:
    s, b = get(res, '/reseller/orders')
    check('reseller sees gift-card codes', 'View your 3 gift-card codes' in b or 'gift-card codes' in b)

# checker + redeem
code_row = q("SELECT code FROM issued_vouchers WHERE status='sold' LIMIT 1")
if code_row:
    code = models._dec(code_row[0]['code'])   # encrypted at rest (v10)
    s, b = post(ops, '/ops/issuing/checker', {'code': code, 'action': 'check'})
    check('checker shows sold status', 'Sold' in b and code in b)
    s, b = post(ops, '/ops/issuing/checker', {'code': code, 'action': 'redeem'})
    check('code redeemed', 'Redeemed successfully' in b)
    st = q("SELECT status FROM issued_vouchers WHERE code_hash=?", models._code_hash(code))[0]['status']
    check('DB status redeemed', st == 'redeemed')
else:
    check('sold code exists for checker', False)

# partner economics on overview
s, b = get(ops, '/ops/issuing')
check('partner profit computed', 'OneCard Profit' in b)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
