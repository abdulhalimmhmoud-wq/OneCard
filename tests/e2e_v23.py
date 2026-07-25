"""E2E test for v23 (Phase 3): the buy-decision engine — stock-vs-draw-vs-forecast
recommendations, with new/unproven-client forecast discounted vs proven active
clients (NEW_CLIENT_FORECAST_WEIGHT)."""
import urllib.request, urllib.parse, urllib.error, http.cookiejar as cj
import re as _re
import sqlite3, os, sys, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import models

BASE = 'http://127.0.0.1:8000'
DB = os.path.join(ROOT, 'onecard.db')
results = []


def check(name, ok, extra=''):
    results.append((name, ok, extra))
    print(('PASS ' if ok else 'FAIL ') + name + (' | ' + extra if extra else ''))


def login(email, pw):
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj.CookieJar()))
    page = op.open(BASE + '/login').read().decode('utf-8', 'replace')
    m = _re.search(r'name="csrf-token" content="([^"]+)"', page)
    op._csrf = m.group(1) if m else ''
    op.open(BASE + '/login', data=urllib.parse.urlencode(
        {'email': email, 'password': pw, '_csrf': op._csrf}).encode())
    return op


def get(op, path):
    r = op.open(BASE + path); return r.status, r.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


tag = uuid.uuid4().hex[:8]

# a non-issued active product with no batches and no existing product forecast → clean baseline
prod = q("""SELECT p.id, p.product_name, p.merchant FROM products p
            WHERE p.is_active=1 AND COALESCE(p.is_issued,0)=0
              AND p.id NOT IN (SELECT product_rowid FROM purchase_batches)
              AND p.id NOT IN (SELECT product_rowid FROM forecast_items WHERE product_rowid IS NOT NULL)
              AND p.id NOT IN (SELECT oi.product_rowid FROM order_items oi JOIN orders o ON oi.order_id=o.id
                               WHERE oi.product_rowid IS NOT NULL AND date(o.created_at) >= date('now','-30 day'))
            LIMIT 1""")[0]
pid = prod['id']


def rec_for(pid):
    return next((r for r in models.get_buy_recommendations() if r['product_rowid'] == pid), None)

check('product has no baseline recommendation (clean)', rec_for(pid) is None)

# ── an ACTIVE reseller (has an order) forecasts 100 of this product ──
sales = login('sales@onecard.com', 'Sales2025!')
def mk_reseller(email):
    from urllib.parse import urlencode
    sales.open(BASE + '/sales/register', data=urlencode({
        'company_name': 'V23 ' + email[:8], 'contact_name': 'T', 'contact_email': email,
        'contact_phone': '05' + str(uuid.uuid4().int)[:8], 'password': 'Test123!',
        'expected_sales': '60000', 'client_types': 'Retail Chain', 'countries': 'Saudi Arabia',
        '_csrf': sales._csrf}).encode())
    return q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", email)[0]['id']

active_rid = mk_reseller(f"v23a_{tag}@test.com")
new_rid = mk_reseller(f"v23n_{tag}@test.com")
# make active_rid genuinely active: give it an order
execu("INSERT INTO orders (reseller_id, total_cost, total_face, status) VALUES (?,100,100,'completed')", active_rid)

fc_item = [{'item_type': 'product', 'merchant': prod['merchant'], 'product_rowid': pid,
            'product_name': prod['product_name'], 'quantity': 100, 'est_value': 1000}]
models.create_forecast(active_rid, 'active fc', fc_item)
models.create_forecast(new_rid, 'new fc', fc_item)

r = rec_for(pid)
check('product now appears in the buy planner', r is not None)
check('active-client forecast counted in full', r and r['forecast_active'] == 100, str(r and r['forecast_active']))
check('new/unproven-client forecast tracked separately', r and r['forecast_new'] == 100, str(r and r['forecast_new']))
# weighted = active + 0.4*new = 100 + 40 = 140
check('new-client forecast is discounted in the weighted demand',
      r and abs(r['weighted_forecast'] - (100 + models.NEW_CLIENT_FORECAST_WEIGHT * 100)) < 0.5,
      f"weighted={r and r['weighted_forecast']}")
check('demand with zero stock recommends a reorder', r and r['recommended_qty'] > 0 and r['signal'] in ('out', 'reorder', 'watch'))

# ── Ops Buy Planner page renders ──
ops = login('ops@onecard.com', 'Ops2025!')
s, b = get(ops, '/ops/buying')
check('Buy Planner page renders with the engine', s == 200 and 'Reorder Recommendations' in b)
s, b = get(ops, '/ops/buying?signal=out')
check('Buy Planner signal filter works', s == 200 and 'Reorder Recommendations' in b)

# ── cleanup ──
for email in (f"v23a_{tag}@test.com", f"v23n_{tag}@test.com"):
    urows = q("SELECT id FROM users WHERE email=?", email)
    if not urows:
        continue
    uid = urows[0]['id']
    for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
        rid = pr['id']
        execu("DELETE FROM forecast_items WHERE forecast_id IN (SELECT id FROM forecasts WHERE reseller_id=?)", rid)
        execu("DELETE FROM forecasts WHERE reseller_id=?", rid)
        execu("DELETE FROM orders WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_countries WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_client_types WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_profiles WHERE id=?", rid)
    execu("DELETE FROM notifications WHERE user_id=?", uid)
    execu("DELETE FROM users WHERE id=?", uid)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
