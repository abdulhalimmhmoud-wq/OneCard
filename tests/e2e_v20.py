"""E2E test for v20 hardening: WAL + indexes, API blocks suspended resellers,
credit-limit validation, hidden merchants in the discount calculator, the
budget pseudo-merchant kept out of Ops demand, dedup override, and
attachment-only file serving."""
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


def post_multipart(op, path, fields):
    b = '----v20b' + uuid.uuid4().hex
    body = b''
    fields = dict(fields); fields.setdefault('_csrf', getattr(op, '_csrf', ''))
    for k, v in fields.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="draft"; filename="c.pdf"\r\n'
             f'Content-Type: application/pdf\r\n\r\n').encode() + b'%PDF-1.4 test\n\r\n' + f'--{b}--\r\n'.encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    return op.open(req).read().decode('utf-8', 'replace')


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


def register(op, company, email, **extra):
    d = {'company_name': company, 'contact_name': 'T', 'contact_email': email,
         'contact_phone': '05' + str(uuid.uuid4().int)[:8], 'password': 'Test123!',
         'expected_sales': '80000', 'client_types': 'Retail Chain', 'countries': 'Saudi Arabia'}
    d.update(extra)
    return post(op, '/sales/register', d)


# ── B1 / B2: WAL + indexes ──
check('DB is in WAL mode', q("PRAGMA journal_mode")[0]['journal_mode'].lower() == 'wal')
idx = {r['name'] for r in q("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reseller_profiles'")}
check('reseller_profiles is indexed on user_id + api_key',
      'idx_reseller_user' in idx and 'idx_reseller_apikey' in idx)

sales = login('sales@onecard.com', 'Sales2025!')
tag = uuid.uuid4().hex[:8]

# ── A1: a suspended reseller is blocked from the API ──
em = f"v20_{tag}@test.com"
register(sales, f'V20 Main {tag}', em)
rid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]['id']
execu("UPDATE reseller_profiles SET nda_accepted_at=CURRENT_TIMESTAMP WHERE id=?", rid)
key = models.set_reseller_api(rid, rotate_key=True)
check('active reseller can reach the API', api_ping(key) == 200)
models.set_reseller_suspended(rid, True)
check('suspended reseller is blocked from the API (401)', api_ping(key) == 401)
models.set_reseller_suspended(rid, False)   # restore
check('reactivated reseller can reach the API again', api_ping(key) == 200)

# ── A2: credit/consignment contract with no limit is rejected ──
b = post_multipart(sales, f"/sales/resellers/{rid}/contract/upload",
                   {'account_type': 'credit', 'credit_limit': '0'})
credit_contract = q("SELECT * FROM contracts WHERE reseller_id=? AND account_type='credit'", rid)
check('a zero-limit credit contract is rejected', not credit_contract and 'greater than zero' in b.lower())
# a prepaid contract (with a file) is fine — also used for the D5 attachment test
post_multipart(sales, f"/sales/resellers/{rid}/contract/upload", {'account_type': 'prepaid'})
cid = q("SELECT id FROM contracts WHERE reseller_id=? ORDER BY id DESC LIMIT 1", rid)[0]['id']

# ── D5: contract files are served as attachments (never rendered inline) ──
r = sales.open(BASE + f"/contracts/{cid}/file/draft")
disp = r.headers.get('Content-Disposition', '')
check('contract file is served as an attachment', 'attachment' in disp.lower(), f"disp={disp!r}")

# ── D1: a hidden merchant has no prices in the discount calculator ──
merch = models.get_all_merchants()[0]['merchant']
models.set_reseller_hidden_merchants(rid, [merch])
mp = models.get_merchant_pricing_for_reseller(rid, merch)
check('discount calculator returns no products for a hidden merchant',
      mp and mp.get('hidden') and not mp['products'])
models.set_reseller_hidden_merchants(rid, [])

# ── D2: exploratory budget is unallocated, not a fake merchant ──
models.create_budget_forecast(rid, 123456, 'v20 budget')
demand = models.get_forecast_demand_summary(days=90)
merchants_in_demand = {m['merchant'] for m in demand['by_merchant']}
check('budget is reported as unallocated, not a merchant',
      demand.get('unallocated_budget', 0) >= 123456 and models.BUDGET_MERCHANT not in merchants_in_demand)

# ── D3: admin/CCO can override a company-name dedup block ──
comp = f"V20 Dup {tag} Co"
em3 = f"v20d_{tag}@test.com"
register(sales, comp, em3)   # first registration
_, b = register(sales, comp, f"v20d2_{tag}@test.com")   # sales: blocked
check('a company-name duplicate is blocked for sales',
      not q("SELECT id FROM users WHERE email=?", f"v20d2_{tag}@test.com")
      and 'already registered' in b.lower())
cco = login('cco@onecard.com', 'Cco2025!')
em4 = f"v20d3_{tag}@test.com"
register(cco, comp, em4, override_dup='yes')   # CCO overrides
check('CCO can override a company-name duplicate', bool(q("SELECT id FROM users WHERE email=?", em4)))

# ── A4 smoke: issue_statement still works under BEGIN IMMEDIATE ──
execu("""UPDATE reseller_profiles SET account_type='credit', credit_limit=50000, credit_limit_base=50000,
         contract_status='contracted', credit_outstanding=8000 WHERE id=?""", rid)
sid = models.issue_statement(rid, actor_id=None)
check('issue_statement bills the outstanding under a lock',
      sid and abs(q("SELECT amount FROM statements WHERE id=?", sid)[0]['amount'] - 8000) < 1)
check('re-issuing with nothing new returns None', models.issue_statement(rid, actor_id=None) is None)

# ── cleanup ──
for email in (em, em3, em4):
    urows = q("SELECT id FROM users WHERE email=?", email)
    if not urows:
        continue
    uid = urows[0]['id']
    for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
        r_ = pr['id']
        for t in ('statements', 'credit_requests', 'api_idempotency', 'webhook_deliveries',
                  'wallet_transactions', 'reseller_hidden_merchants', 'reseller_countries',
                  'reseller_client_types', 'merchant_share_overrides'):
            execu(f"DELETE FROM {t} WHERE reseller_id=?", r_)
        execu("DELETE FROM contract_events WHERE contract_id IN (SELECT id FROM contracts WHERE reseller_id=?)", r_)
        execu("DELETE FROM contracts WHERE reseller_id=?", r_)
        execu("DELETE FROM forecast_items WHERE forecast_id IN (SELECT id FROM forecasts WHERE reseller_id=?)", r_)
        execu("DELETE FROM forecasts WHERE reseller_id=?", r_)
        execu("DELETE FROM reseller_profiles WHERE id=?", r_)
    execu("DELETE FROM notifications WHERE user_id=?", uid)
    execu("DELETE FROM users WHERE id=?", uid)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
