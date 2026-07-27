"""E2E test for v12:
  1. Reseller forecasts also reach Operations (notification + /ops/forecasts
     demand-planning view with aggregated by-merchant / by-product demand).
  2. New resellers who neither sign a contract nor buy within
     PROSPECT_SUSPEND_DAYS are auto-suspended: they can't log in, an active
     session is kicked, they stay visible to Sales, and Sales can reactivate.
"""
import urllib.request, urllib.parse, urllib.error, http.cookiejar as cj
import re as _re
import json, sqlite3, os, sys, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import models

BASE = 'http://127.0.0.1:8000'
DB = os.path.join(ROOT, 'onecard.db')
results = []


def check(name, ok, extra=''):
    results.append((name, ok, extra))
    print(('PASS ' if ok else 'FAIL ') + name + (' | ' + extra if extra else ''))


def opener():
    jar = cj.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def login(email, pw):
    op = opener()
    page = op.open(BASE + '/login').read().decode('utf-8', 'replace')
    m = _re.search(r'name="csrf-token" content="([^"]+)"', page)
    op._csrf = m.group(1) if m else ''
    r = op.open(BASE + '/login', data=urllib.parse.urlencode(
        {'email': email, 'password': pw, '_csrf': op._csrf}).encode())
    op._login_body = r.read().decode('utf-8', 'replace')  # the POST response (holds flashes)
    return op


def get(op, path):
    r = op.open(BASE + path)
    return r.status, r.read().decode('utf-8', 'replace')


def post(op, path, data):
    data = dict(data)
    data.setdefault('_csrf', getattr(op, '_csrf', ''))
    r = op.open(BASE + path, data=urllib.parse.urlencode(data, doseq=True).encode())
    return r.status, r.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]
    c.close()
    return rows


def execu(sql, *a):
    c = sqlite3.connect(DB)
    c.execute(sql, a); c.commit(); c.close()


def register(sales, email, countries='Saudi Arabia'):
    post(sales, '/sales/register', {
        'company_name': 'V12 ' + email[:10], 'contact_name': 'T', 'contact_email': email,
        'contact_phone': '05' + str(uuid.uuid4().int)[:8],
        'password': 'Test123!', 'expected_sales': '60000',
        'client_types': 'Retail Chain', 'countries': countries, 'display_currency': ''})
    execu("UPDATE reseller_profiles SET nda_accepted_at=CURRENT_TIMESTAMP WHERE user_id=(SELECT id FROM users WHERE email=?)", email)
    return q("""SELECT cp.* FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id
                WHERE u.email=?""", email)[0]


# ═══════════════════════ Feature 1: Forecast → Operations ═══════════════════════

sales = login('sales@onecard.com', 'Sales2025!')
em_f = f"v12_fc_{uuid.uuid4().hex[:8]}@test.com"
prof_f = register(sales, em_f)

# Ops can open the demand-forecast page
ops = login('ops@onecard.com', 'Ops2025!')
s, b = get(ops, '/ops/forecasts')
check('ops can open /ops/forecasts', s == 200 and 'Forecast Intelligence' in b)
check('ops forecasts page has demand-by-merchant table', 'Demand by Merchant' in b)

# Reseller submits a forecast
res = login(em_f, 'Test123!')
enriched = models.enrich_products_for_reseller(models.get_reseller_profile(
    q("SELECT id FROM users WHERE email=?", em_f)[0]['id']))
picks = [p for p in enriched if not p.get('is_issued')][:2]
items = json.dumps([{'type': 'product', 'product_id': p['id'], 'quantity': 5} for p in picks])
ops_uid = q("SELECT id FROM users WHERE email='ops@onecard.com'")[0]['id']
ops_notes_before = q("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND link='/ops/forecasts'",
                     ops_uid)[0]['n']
s, b = post(res, '/reseller/forecast', {'items_json': items, 'note': 'v12 plan'})
check('forecast submitted', 'submitted to your account manager' in b)

fc = q("""SELECT f.* FROM forecasts f WHERE f.reseller_id=? ORDER BY f.id DESC LIMIT 1""",
       prof_f['id'])
check('forecast row created', len(fc) == 1)

# Operations got a notification pointing at the ops forecasts page
ops_notes_after = q("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND link='/ops/forecasts'",
                    ops_uid)[0]['n']
check('operations notified of the new forecast', ops_notes_after > ops_notes_before,
      f"ops forecast notifications {ops_notes_before} -> {ops_notes_after}")

# Sales manager still gets their notification too (unchanged behaviour)
sales_notes = q("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND title LIKE 'New purchase forecast%'",
                prof_f['registered_by'])[0]['n']
check('sales manager still notified', sales_notes >= 1)

# Demand summary aggregates the forecast for stock planning
demand = models.get_forecast_demand_summary(days=90)
check('demand summary has by_merchant + by_product', 'by_merchant' in demand and 'by_product' in demand)
check('demand summary totals include our forecast',
      demand['totals']['est_value'] > 0 and demand['totals']['forecasts'] >= 1)
picked_merchants = {p['merchant'] for p in picks}
demand_merchants = {m['merchant'] for m in demand['by_merchant']}
check('our forecast merchant appears in ops demand', bool(picked_merchants & demand_merchants),
      f"forecast merchants {picked_merchants} in demand")

# The new forecast is listed on the ops page
s, b = get(ops, '/ops/forecasts')
check('new forecast listed on ops page', prof_f['company_name'] in b)


# ═══════════════════════ Feature 2: Auto-suspension ═══════════════════════

# New reseller gets a 15-day conversion deadline in the future
em_s = f"v12_sus_{uuid.uuid4().hex[:8]}@test.com"
prof_s = register(sales, em_s)
check('new reseller has a future auto_suspend_at', bool(prof_s['auto_suspend_at'])
      and prof_s['auto_suspend_at'] > models.datetime.now(models.timezone.utc).date().isoformat(),
      f"deadline={prof_s['auto_suspend_at']}")
check('new reseller starts un-suspended', prof_s['is_suspended'] == 0)

# Before the deadline, the sweep leaves them alone
models.run_prospect_suspension()
still = q("SELECT is_suspended FROM reseller_profiles WHERE id=?", prof_s['id'])[0]['is_suspended']
check('prospect within window is NOT suspended', still == 0)

# Push the deadline into the past → the sweep suspends them
execu("UPDATE reseller_profiles SET auto_suspend_at='2000-01-01' WHERE id=?", prof_s['id'])
actions = models.run_prospect_suspension()
row = q("SELECT is_suspended, suspended_at FROM reseller_profiles WHERE id=?", prof_s['id'])[0]
check('overdue prospect auto-suspended', row['is_suspended'] == 1 and row['suspended_at'],
      f"actions={actions}")
check('sales manager notified of the suspension',
      q("""SELECT COUNT(*) n FROM notifications WHERE user_id=? AND title LIKE 'Account auto-suspended%'""",
        prof_s['registered_by'])[0]['n'] >= 1)

# A contracted reseller past the deadline is safe
em_c = f"v12_con_{uuid.uuid4().hex[:8]}@test.com"
prof_c = register(sales, em_c)
execu("UPDATE reseller_profiles SET contract_status='contracted', auto_suspend_at='2000-01-01' WHERE id=?",
      prof_c['id'])
models.run_prospect_suspension()
check('contracted reseller past deadline is NOT suspended',
      q("SELECT is_suspended FROM reseller_profiles WHERE id=?", prof_c['id'])[0]['is_suspended'] == 0)

# A prospect who bought (has an order) past the deadline is safe
em_b = f"v12_buy_{uuid.uuid4().hex[:8]}@test.com"
prof_b = register(sales, em_b)
execu("UPDATE reseller_profiles SET auto_suspend_at='2000-01-01' WHERE id=?", prof_b['id'])
execu("""INSERT INTO orders (reseller_id, total_cost, total_face, status)
         VALUES (?, 100, 100, 'completed')""", prof_b['id'])
models.run_prospect_suspension()
check('prospect who purchased is NOT suspended',
      q("SELECT is_suspended FROM reseller_profiles WHERE id=?", prof_b['id'])[0]['is_suspended'] == 0)

# is_user_suspended helper reflects the flag
check('is_user_suspended True for suspended reseller', models.is_user_suspended(prof_s['user_id']))
check('is_user_suspended False for active reseller', not models.is_user_suspended(prof_c['user_id']))

# Suspended reseller cannot log in
susp = login(em_s, 'Test123!')
check('suspended login shows an inactive-account message', 'inactive' in susp._login_body.lower())
s, b = get(susp, '/reseller')
check('suspended reseller is bounced to login', 'name="email"' in b and 'name="password"' in b)

# Active session gets kicked the moment the account is suspended mid-session
em_k = f"v12_kick_{uuid.uuid4().hex[:8]}@test.com"
prof_k = register(sales, em_k)
kick = login(em_k, 'Test123!')
s, b = get(kick, '/reseller')
check('fresh reseller can use the portal', 'name="password"' not in b or 'Dashboard' in b)
execu("UPDATE reseller_profiles SET is_suspended=1, suspended_at=CURRENT_TIMESTAMP WHERE id=?", prof_k['id'])
s, b = get(kick, '/reseller/wallet')
check('mid-session suspension kicks the reseller to login',
      'name="email"' in b and 'name="password"' in b)

# Suspended reseller stays visible to their sales manager on My Resellers
s, b = get(sales, '/sales/resellers')
check('suspended reseller visible to sales', prof_s['company_name'] in b)
check('my-resellers shows a Suspended badge + Reactivate action',
      'Suspended' in b and 'Reactivate' in b)

# Sales reactivates → fresh window, can log in again
s, b = post(sales, f"/sales/resellers/{prof_s['id']}/reactivate", {})
check('reactivate succeeds', 'reactivated' in b.lower())
after = q("SELECT is_suspended, auto_suspend_at FROM reseller_profiles WHERE id=?", prof_s['id'])[0]
check('reactivation clears suspension + grants fresh window',
      after['is_suspended'] == 0
      and after['auto_suspend_at'] > models.datetime.now(models.timezone.utc).date().isoformat(),
      f"deadline now {after['auto_suspend_at']}")
relog = login(em_s, 'Test123!')
s, b = get(relog, '/reseller')
check('reactivated reseller can log in again', 'name="password"' not in b or 'Dashboard' in b)

# ── cleanup: remove the v12 test resellers, users, forecasts, orders, notes ──
test_emails = [em_f, em_s, em_c, em_b, em_k]
for em in test_emails:
    urows = q("SELECT id FROM users WHERE email=?", em)
    if not urows:
        continue
    uid = urows[0]['id']
    prows = q("SELECT id FROM reseller_profiles WHERE user_id=?", uid)
    for pr in prows:
        rid = pr['id']
        execu("DELETE FROM forecast_items WHERE forecast_id IN (SELECT id FROM forecasts WHERE reseller_id=?)", rid)
        execu("DELETE FROM forecasts WHERE reseller_id=?", rid)
        execu("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE reseller_id=?)", rid)
        execu("DELETE FROM orders WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_countries WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_client_types WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_profiles WHERE id=?", rid)
    execu("DELETE FROM notifications WHERE user_id=?", uid)
    execu("DELETE FROM users WHERE id=?", uid)
execu("DELETE FROM notifications WHERE title LIKE 'Account auto-suspended%' AND body LIKE 'V12 %'")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
