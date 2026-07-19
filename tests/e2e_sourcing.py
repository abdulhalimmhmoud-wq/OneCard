"""E2E test for OneCard v6 — multi-supplier sourcing + batch governance."""
import urllib.request, urllib.parse, http.cookiejar as cj
import re as _re
import json, sqlite3

BASE = 'http://127.0.0.1:8000'
DB = r'c:\Users\Abdel Halim.mahmoud\2025 Data\onecard.db'
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

def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]
    c.close()
    return rows

ops = login('ops@onecard.com', 'Ops2025!')
fin = login('finance@onecard.com', 'Finance2025!')
cco = login('cco@onecard.com', 'Cco2025!')
admin = login('admin@onecard.com', 'OneCard2025!')

# ── 1. Pages load with demo data ──
s, b = get(ops, '/ops/sourcing')
check('sourcing matrix loads with offers', s == 200 and 'Best:' in b and 'Add supplier offer' in b)
s, b = get(ops, '/ops/batches')
check('batches page + variance badges', s == 200 and 'Record New Batch' in b and 'best ✓' in b)
s, b = get(fin, '/finance/batches')
check('finance reconciliation queue', s == 200 and 'Awaiting Reconciliation' in b)
s, b = get(cco, '/sourcing-intel')
check('sourcing intelligence (cco)', s == 200 and 'Who Are We Selling From' in b and 'Supplier Report Card' in b)
check('margin improvements visible', 'Where Our Profit Improved' in b)
check('variance governance visible', 'Bought Above Best Price' in b)
s, b = get(admin, '/sourcing-intel')
check('sourcing intelligence (admin)', s == 200)
s, b = get(fin, '/finance')
check('finance dashboard batch card', 'Batches to Reconcile' in b)

# ── 2. Ops adds/edits a supplier offer ──
# pick a normal (non-issued) product with no existing stock so FIFO must hit our new batch
prod = q("""SELECT p.id, p.product_id FROM products p
            WHERE p.is_active=1 AND p.cost > 5 AND COALESCE(p.is_issued,0)=0
              AND NOT EXISTS (SELECT 1 FROM purchase_batches b WHERE b.product_rowid=p.id)
            ORDER BY p.id DESC LIMIT 1""")[0]
sup = q("SELECT id, name FROM suppliers ORDER BY id LIMIT 1")[0]
s, b = post(ops, '/ops/sourcing/price', {'supplier_id': sup['id'], 'product_rowid': prod['id'], 'cost': '11.5'})
check('add offer', 'Offer saved' in b)
s, b = post(ops, '/ops/sourcing/price', {'supplier_id': sup['id'], 'product_rowid': prod['id'], 'cost': '10.9'})
check('edit offer price', 'Offer saved' in b)
hist = q("SELECT * FROM supplier_price_history WHERE supplier_id=? AND product_rowid=? ORDER BY id", sup['id'], prod['id'])
check('price history logged old-to-new', len(hist) >= 2 and hist[-1]['old_value' if 'old_value' in hist[-1] else 'old_cost'] is not None)

# availability toggle
s, b = post(ops, '/ops/sourcing/availability', {'supplier_id': sup['id'], 'product_rowid': prod['id'], 'available': '0'})
check('offer marked unavailable', 'availability updated' in b)
avail = q("SELECT is_available FROM supplier_products WHERE supplier_id=? AND product_rowid=?", sup['id'], prod['id'])
check('DB availability=0', avail[0]['is_available'] == 0)
post(ops, '/ops/sourcing/availability', {'supplier_id': sup['id'], 'product_rowid': prod['id'], 'available': '1'})

# ── 3. Overpaid batch → variance + BD/CCO alert ──
sup2 = q("SELECT id, name FROM suppliers ORDER BY id DESC LIMIT 1")[0]
# create a competing cheaper offer from sup2 so buying from sup at higher price = variance
post(ops, '/ops/sourcing/price', {'supplier_id': sup2['id'], 'product_rowid': prod['id'], 'cost': '10.0'})
s, b = post(ops, '/ops/batches', {'supplier_id': sup['id'], 'product_rowid': prod['id'],
                                  'quantity': '100', 'unit_cost': '11.8',
                                  'invoice_ref': 'INV-E2E-77', 'reason': 'e2e relationship pricing'})
check('overpaid batch recorded', 'recorded' in b and 'Finance' in b)
batch = q("SELECT * FROM purchase_batches ORDER BY id DESC LIMIT 1")[0]
check('variance computed', batch['sourcing_variance'] > 0, f"variance={batch['sourcing_variance']}")
s, b = get(cco, '/notifications')
check('cco alerted about variance', 'Sourcing variance' in b)

# ── 4. Finance reconciles it ──
s, b = post(fin, f"/finance/batches/{batch['id']}/review", {'decision': 'reconcile', 'note': 'e2e matched'})
check('finance reconciled batch', 'reconciled against invoice' in b)
st = q("SELECT status FROM purchase_batches WHERE id=?", batch['id'])
check('DB status reconciled', st[0]['status'] == 'reconciled')
s, b = get(ops, '/notifications')
check('ops notified of reconciliation', f"Batch #{batch['id']}" in b)

# ── 5. FIFO allocation on a fresh order ──
res = login('khalid@alnoor-digital.com', 'Demo123!')
items = json.dumps([{'product_id': prod['id'], 'quantity': 30}])
s, b = post(res, '/reseller/orders', {'items_json': items})
check('order placed', 'placed successfully' in b)
alloc = q("""SELECT a.* FROM order_item_allocations a
             JOIN order_items oi ON a.order_item_id=oi.id
             WHERE oi.product_rowid=? ORDER BY a.id DESC LIMIT 1""", prod['id'])
check('FIFO allocation to batch', alloc and alloc[0]['batch_id'] == batch['id'],
      f"batch={alloc[0]['batch_id'] if alloc else None}")
rem = q("SELECT remaining_qty FROM purchase_batches WHERE id=?", batch['id'])
check('batch stock decremented', rem[0]['remaining_qty'] == 70, f"remaining={rem[0]['remaining_qty']}")

# sales-by-supplier now includes this supplier
s, b = get(cco, '/sourcing-intel')
check('sales-by-supplier shows supplier', sup['name'] in b)

# ── 6. Supplier price API endpoint ──
sup_key = q("SELECT api_key FROM suppliers WHERE id=?", sup2['id'])[0]['api_key']
if not sup_key:
    post(ops, f"/ops/suppliers/{sup2['id']}/apikey", {})
    sup_key = q("SELECT api_key FROM suppliers WHERE id=?", sup2['id'])[0]['api_key']
payload = json.dumps({'api_key': sup_key,
                      'items': [{'product_id': prod['product_id'], 'cost': 9.75}]}).encode()
req = urllib.request.Request(BASE + '/api/supplier-prices', data=payload,
                             headers={'Content-Type': 'application/json'})
resp = json.loads(urllib.request.urlopen(req).read())
check('API price sync works', resp.get('ok') and resp.get('updated') == 1, str(resp))
newp = q("SELECT supplier_cost, source FROM supplier_products WHERE supplier_id=? AND product_rowid=?",
         sup2['id'], prod['id'])
check('API price stored with source=api', newp[0]['supplier_cost'] == 9.75 and newp[0]['source'] == 'api')

# bad key rejected
req = urllib.request.Request(BASE + '/api/supplier-prices',
                             data=json.dumps({'api_key': 'wrong', 'items': []}).encode(),
                             headers={'Content-Type': 'application/json'})
try:
    urllib.request.urlopen(req)
    check('API rejects bad key', False)
except urllib.error.HTTPError as e:
    check('API rejects bad key', e.code == 401)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
