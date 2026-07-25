"""E2E test for v26: the API is an always-on channel — every reseller and supplier
gets a working API key automatically at creation (no manual 'generate' step)."""
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


def api_ping(key):
    req = urllib.request.Request(BASE + '/api/v1/ping', headers={'X-API-Key': key})
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


# ── every EXISTING reseller/supplier has a key (backfill) ──
check('no reseller is left without an API key',
      q("SELECT COUNT(*) n FROM reseller_profiles WHERE api_key IS NULL OR api_key=''")[0]['n'] == 0)
check('no supplier is left without an API key',
      q("SELECT COUNT(*) n FROM suppliers WHERE api_key IS NULL OR api_key=''")[0]['n'] == 0)

tag = uuid.uuid4().hex[:8]

# ── a NEW reseller gets a working key automatically at registration ──
sales = login('sales@onecard.com', 'Sales2025!')
em = f"v26_{tag}@test.com"
sales.open(BASE + '/sales/register', data=urllib.parse.urlencode({
    'company_name': f'V26 Co {tag}', 'contact_name': 'T', 'contact_email': em,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8], 'password': 'Test123!',
    'expected_sales': '60000', 'client_types': 'Retail Chain', 'countries': 'Saudi Arabia',
    '_csrf': sales._csrf}).encode())
row = q("SELECT cp.id, cp.api_key FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]
rid, rkey = row['id'], row['api_key']
check('new reseller auto-provisioned an rk_ key', bool(rkey) and rkey.startswith('rk_'))
check('the auto-provisioned reseller key works on the API immediately', api_ping(rkey) == 200)

# ── it's visible/copyable on My Resellers (no manual generate) ──
s, b = get(sales, '/sales/resellers')
check('My Resellers shows the always-on key + only a Rotate action',
      s == 200 and rkey in b and 'Rotate API Key' in b and 'Generate API Key' not in b)

# ── a NEW supplier gets a working key automatically at creation ──
merch = models.get_all_merchants()[0]['merchant']
sup_id = models.upsert_supplier(None, f'V26 Sup {tag}', 'C', '', '', '', '', [merch])
skey = models.get_supplier(sup_id)['api_key']
check('new supplier auto-provisioned an sk_ key', bool(skey) and skey.startswith('sk_'))
# the supplier key authenticates the supplier price feed
payload = json.dumps({'api_key': skey, 'items': []}).encode()
req = urllib.request.Request(BASE + '/api/supplier-prices', data=payload,
                             headers={'Content-Type': 'application/json'})
resp = json.loads(urllib.request.urlopen(req).read())
check('the auto-provisioned supplier key works on the price feed', resp.get('ok') is True)

# ── rotate still works + invalidates the old key ──
newkey = models.set_reseller_api(rid, rotate_key=True)
check('rotating issues a new working key', api_ping(newkey) == 200)
check('the old reseller key stops working after rotation', api_ping(rkey) == 401)

# ── cleanup ──
for t in ('supplier_merchants', 'supplier_products'):
    execu(f"DELETE FROM {t} WHERE supplier_id=?", sup_id)
execu("DELETE FROM suppliers WHERE id=?", sup_id)
uid = q("SELECT id FROM users WHERE email=?", em)[0]['id']
for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
    execu("DELETE FROM reseller_countries WHERE reseller_id=?", pr['id'])
    execu("DELETE FROM reseller_client_types WHERE reseller_id=?", pr['id'])
    execu("DELETE FROM reseller_profiles WHERE id=?", pr['id'])
execu("DELETE FROM notifications WHERE user_id=?", uid)
execu("DELETE FROM users WHERE id=?", uid)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
