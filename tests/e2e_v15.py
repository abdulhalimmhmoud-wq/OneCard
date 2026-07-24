"""E2E test for v15 (Phase 3): credit AND consignment via the API — card-by-card
draws without a prepaid balance, the /account + /statements endpoints,
account-type error codes, and the Ops consignment activity view.

Locks the requirement that being API-connected + pulling product-by-product is
independent of the account model: a CREDIT client integrates over the API exactly
like a CONSIGNMENT one."""
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


def post(op, path, data):
    data = dict(data); data.setdefault('_csrf', getattr(op, '_csrf', ''))
    r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
    return r.status, r.read().decode('utf-8', 'replace')


def get(op, path):
    r = op.open(BASE + path); return r.status, r.read().decode('utf-8', 'replace')


def api_get(key, path):
    req = urllib.request.Request(BASE + path, headers={'X-API-Key': key})
    try:
        r = urllib.request.urlopen(req); return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def api_post(key, path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={'X-API-Key': key, 'Content-Type': 'application/json'})
    try:
        r = urllib.request.urlopen(req); return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


# ── set up a consignment reseller with an API key ──
sales = login('sales@onecard.com', 'Sales2025!')
em = f"v15_{uuid.uuid4().hex[:8]}@test.com"
post(sales, '/sales/register', {'company_name': 'V15 Bank Consign ' + em[4:12], 'contact_name': 'B',
     'contact_phone': '05' + str(uuid.uuid4().int)[:8],
     'contact_email': em, 'password': 'Test123!', 'expected_sales': '90000',
     'client_types': 'Bank', 'countries': 'Saudi Arabia'})
rid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]['id']
execu("""UPDATE reseller_profiles SET account_type='consignment', credit_limit=50000, credit_limit_base=50000,
         credit_disbursement='full', contract_status='contracted', credit_outstanding=0,
         settlement_terms_days=30, billing_cycle='monthly' WHERE id=?""", rid)
key = models.set_reseller_api(rid, rotate_key=True)

# ── 1. /account reflects the consignment line ──
s, acct = api_get(key, '/api/v1/account')
check('GET /account works for consignment', s == 200 and acct.get('account_type') == 'consignment')
check('/account exposes credit headroom (no prepaid balance needed)',
      acct.get('available_to_spend') == 50000 and acct.get('credit_limit') == 50000
      and 'wallet_balance' not in acct)

# ── 2. card-by-card draw via the API, without any prepaid balance ──
s, cat = api_get(key, '/api/v1/catalogue?page_size=200')
cheap = next(p for p in cat['items'] if not p.get('is_issued') and 1 <= p['your_price'] <= 400)
s, r1 = api_post(key, '/api/v1/orders',
                 {'idempotency_key': f'v15-{uuid.uuid4().hex[:8]}', 'items': [{'id': cheap['id'], 'quantity': 2}]})
check('consignment can order card-by-card via API', s == 201 and r1.get('ok'),
      f"status={s} err={r1.get('error')}")
drawn = round(cheap['your_price'] * 2, 2)
out = q("SELECT credit_outstanding FROM reseller_profiles WHERE id=?", rid)[0]['credit_outstanding']
check('the draw accrued to outstanding', abs(out - drawn) < 1.0, f"outstanding={out} expected~{drawn}")
s, acct2 = api_get(key, '/api/v1/account')
check('/account headroom dropped by the draw', abs(acct2['available_to_spend'] - (50000 - drawn)) < 1
      and abs(acct2['unbilled'] - drawn) < 1)

# ── 3. over-limit draw is refused with the right code ──
execu("UPDATE reseller_profiles SET credit_outstanding=50000 WHERE id=?", rid)   # at the ceiling
s, rov = api_post(key, '/api/v1/orders',
                  {'idempotency_key': f'v15-{uuid.uuid4().hex[:8]}', 'items': [{'id': cheap['id'], 'quantity': 1}]})
check('over-limit draw -> 409 credit_limit_reached',
      s == 409 and rov.get('error', {}).get('code') == 'credit_limit_reached', f"got {rov.get('error')}")

# ── 4. statements endpoint ──
execu("UPDATE reseller_profiles SET credit_outstanding=? WHERE id=?", drawn, rid)   # back to the real draw
sid = models.issue_statement(rid, actor_id=None)
s, stmts = api_get(key, '/api/v1/statements')
check('GET /statements lists the issued statement',
      s == 200 and any(st['statement_id'] == sid and st['status'] == 'issued' for st in stmts['statements']))
check('/statements reports unbilled = 0 after issuing', abs(stmts['unbilled']) < 1)

# ── 5. frozen (overdue) line refuses API draws ──
execu("UPDATE reseller_profiles SET credit_frozen=1 WHERE id=?", rid)
s, rfz = api_post(key, '/api/v1/orders',
                  {'idempotency_key': f'v15-{uuid.uuid4().hex[:8]}', 'items': [{'id': cheap['id'], 'quantity': 1}]})
check('frozen line -> 409 account_on_hold',
      s == 409 and rfz.get('error', {}).get('code') == 'account_on_hold', f"got {rfz.get('error')}")
execu("UPDATE reseller_profiles SET credit_frozen=0 WHERE id=?", rid)

# ── 6. Ops consignment activity view ──
ops = login('ops@onecard.com', 'Ops2025!')
s, b = get(ops, '/ops/consignment')
check('ops consignment page renders', s == 200 and 'Consignment & Credit Activity' in b)
check('ops page lists the consignment account + API channel',
      'V15 Bank Consign' in b and 'API' in b)

# ── 7. a CREDIT (staged) client pulls card-by-card via the API too ──
em2 = f"v15c_{uuid.uuid4().hex[:8]}@test.com"
post(sales, '/sales/register', {'company_name': 'V15 Credit API ' + em2[5:13], 'contact_name': 'C',
     'contact_phone': '05' + str(uuid.uuid4().int)[:8],
     'contact_email': em2, 'password': 'Test123!', 'expected_sales': '90000',
     'client_types': 'Retail Chain', 'countries': 'Saudi Arabia'})
rid2 = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em2)[0]['id']
execu("""UPDATE reseller_profiles SET account_type='credit', credit_limit=50000, credit_limit_base=50000,
         credit_disbursement='staged', credit_tranche=5000, contract_status='contracted',
         credit_outstanding=0, settlement_terms_days=30, billing_cycle='monthly' WHERE id=?""", rid2)
key2 = models.set_reseller_api(rid2, rotate_key=True)
s, acctc = api_get(key2, '/api/v1/account')
check('credit client /account works over the API',
      s == 200 and acctc.get('account_type') == 'credit' and 'credit_limit' in acctc)
check('staged credit exposes a tranche, not the full limit',
      acctc.get('available_to_spend') == 5000, f"available={acctc.get('available_to_spend')}")
s, rc = api_post(key2, '/api/v1/orders',
                 {'idempotency_key': f'v15c-{uuid.uuid4().hex[:8]}', 'items': [{'id': cheap['id'], 'quantity': 2}]})
check('credit client can pull card-by-card via API (no prepaid balance)',
      s == 201 and rc.get('ok'), f"status={s} err={rc.get('error')}")
outc = q("SELECT credit_outstanding FROM reseller_profiles WHERE id=?", rid2)[0]['credit_outstanding']
check('credit API draw accrued to outstanding (credit_draw)', abs(outc - drawn) < 1.0)
check('credit API draw logged as credit_draw',
      q("SELECT type FROM wallet_transactions WHERE reseller_id=? ORDER BY id DESC LIMIT 1", rid2)[0]['type'] == 'credit_draw')
s, actc2 = api_get(key2, '/api/v1/account')
check('staged tranche replenishes after a small draw', actc2['available_to_spend'] == 5000)
s, b = get(ops, '/ops/consignment')
check('ops activity view also covers the API-connected credit account',
      'V15 Credit API' in b)

# ── cleanup ──
for email in (em, em2):
    urows = q("SELECT id FROM users WHERE email=?", email)
    if not urows:
        continue
    uid = urows[0]['id']
    for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
        r = pr['id']
        execu("DELETE FROM statements WHERE reseller_id=?", r)
        execu("DELETE FROM credit_requests WHERE reseller_id=?", r)
        execu("DELETE FROM api_idempotency WHERE reseller_id=?", r)
        execu("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE reseller_id=?)", r)
        execu("DELETE FROM orders WHERE reseller_id=?", r)
        execu("DELETE FROM wallet_transactions WHERE reseller_id=?", r)
        execu("DELETE FROM reseller_countries WHERE reseller_id=?", r)
        execu("DELETE FROM reseller_client_types WHERE reseller_id=?", r)
        execu("DELETE FROM reseller_profiles WHERE id=?", r)
    execu("DELETE FROM notifications WHERE user_id=?", uid)
    execu("DELETE FROM users WHERE id=?", uid)
execu("DELETE FROM notifications WHERE title LIKE 'Statement issued%' AND body LIKE 'V15 %'")
execu("DELETE FROM notifications WHERE title LIKE 'New statement issued%'")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
