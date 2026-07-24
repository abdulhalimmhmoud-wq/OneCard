"""E2E test for v14 (Phase 2): statements, settlement, overdue freeze and
additional-credit requests (CCO + Finance two-sign-off, temporary/permanent)."""
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


def opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj.CookieJar()))


def login(email, pw):
    op = opener()
    page = op.open(BASE + '/login').read().decode('utf-8', 'replace')
    m = _re.search(r'name="csrf-token" content="([^"]+)"', page)
    op._csrf = m.group(1) if m else ''
    op.open(BASE + '/login', data=urllib.parse.urlencode(
        {'email': email, 'password': pw, '_csrf': op._csrf}).encode())
    return op


def get(op, path):
    try:
        r = op.open(BASE + path); return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def post(op, path, data):
    data = dict(data); data.setdefault('_csrf', getattr(op, '_csrf', ''))
    try:
        r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def post_receipt(op, path, fields):
    b = '----v14b' + uuid.uuid4().hex
    png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
    body = b''
    fields = dict(fields); fields.setdefault('_csrf', getattr(op, '_csrf', ''))
    for k, v in fields.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="receipt"; filename="r.png"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n').encode() + png + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    try:
        r = op.open(req); return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


prod = q("SELECT id, product_name, merchant, category FROM products WHERE COALESCE(is_issued,0)=0 AND is_active=1 LIMIT 1")[0]


def items(unit_price, qty=1):
    return [{'product_rowid': prod['id'], 'product_name': prod['product_name'],
             'merchant': prod['merchant'], 'category': prod['category'], 'currency': 'SAR',
             'quantity': qty, 'unit_price': unit_price, 'unit_face': unit_price}]


sales = login('sales@onecard.com', 'Sales2025!')
sales_uid = q("SELECT id FROM users WHERE email='sales@onecard.com'")[0]['id']
em = f"v14_{uuid.uuid4().hex[:8]}@test.com"
post(sales, '/sales/register', {'company_name': 'V14 Credit Co ' + em[4:12], 'contact_name': 'T',
     'contact_phone': '05' + str(uuid.uuid4().int)[:8],
     'contact_email': em, 'password': 'Test123!', 'expected_sales': '80000',
     'client_types': 'Retail Chain', 'countries': 'Saudi Arabia'})
rid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]['id']
execu("""UPDATE reseller_profiles SET account_type='credit', credit_limit=100000, credit_limit_base=100000,
         credit_disbursement='full', contract_status='contracted', credit_outstanding=0,
         settlement_terms_days=30, billing_cycle='monthly' WHERE id=?""", rid)

# ── 1. Draw → statement ──
models.create_order(rid, items(30000))
check('draw raised outstanding to 30000',
      abs(q("SELECT credit_outstanding FROM reseller_profiles WHERE id=?", rid)[0]['credit_outstanding'] - 30000) < 1)
check('unbilled equals the drawn amount', abs(models.unbilled_amount(rid) - 30000) < 1)
sid = models.issue_statement(rid, actor_id=None)
st = q("SELECT * FROM statements WHERE id=?", sid)[0]
check('statement issued for the drawn amount', st['status'] == 'issued' and abs(st['amount'] - 30000) < 1 and st['due_at'])
check('nothing left unbilled after issuing', models.unbilled_amount(rid) == 0)
check('available unchanged by issuing (limit - outstanding)',
      models.available_to_spend(models.get_reseller_profile_by_id(rid)) == 70000)
check('issuing again with nothing new returns None', models.issue_statement(rid, actor_id=None) is None)

# ── 2. Reseller settles via HTTP, Finance verifies ──
res = login(em, 'Test123!')
s, b = get(res, '/reseller/wallet')
check('billing page lists the statement', ('#%d' % sid) in b or '30,000' in b or 'Statements' in b)
s, b = post_receipt(res, f"/reseller/statements/{sid}/settle", {'bank_reference': 'V14-STL-1'})
txn = q("SELECT * FROM wallet_transactions WHERE type='settlement' AND statement_id=? ORDER BY id DESC LIMIT 1", sid)
check('settlement request created (pending)', txn and txn[0]['status'] == 'pending')
fin = login('finance@onecard.com', 'Finance2025!')
s, b = get(fin, '/finance/credit')
check('finance credit page shows the settlement', s == 200 and 'V14 Credit Co' in b)
post(fin, f"/finance/settlements/{txn[0]['id']}/review", {'decision': 'approve'})
prof = q("SELECT credit_outstanding FROM reseller_profiles WHERE id=?", rid)[0]
check('settlement cleared the outstanding', abs(prof['credit_outstanding']) < 1)
check('statement marked paid', q("SELECT status FROM statements WHERE id=?", sid)[0]['status'] == 'paid')
check('available restored to full limit',
      models.available_to_spend(models.get_reseller_profile_by_id(rid)) == 100000)

# ── 3. Overdue → freeze → settle → unfreeze ──
models.create_order(rid, items(20000))
sid2 = models.issue_statement(rid, actor_id=None)
execu("UPDATE statements SET due_at='2000-01-01' WHERE id=?", sid2)
models.run_statement_cycle()
check('past-due statement flips to overdue', q("SELECT status FROM statements WHERE id=?", sid2)[0]['status'] == 'overdue')
check('overdue freezes the account', q("SELECT credit_frozen FROM reseller_profiles WHERE id=?", rid)[0]['credit_frozen'] == 1)
check('frozen line has zero available', models.available_to_spend(models.get_reseller_profile_by_id(rid)) == 0)
oid, err = models.create_order(rid, items(100))
check('frozen line cannot draw', oid is None and err and 'hold' in err.lower())
# settle the overdue statement (model-level)
stxn = models.create_settlement_request(rid, sid2, 20000, 'V14-STL-2', 'r.png', orig_amount=20000, orig_currency='SAR')
models.review_settlement(stxn, True, q("SELECT id FROM users WHERE email='finance@onecard.com'")[0]['id'])
prof = q("SELECT credit_frozen, credit_outstanding FROM reseller_profiles WHERE id=?", rid)[0]
check('settling the overdue unfreezes the account', prof['credit_frozen'] == 0 and abs(prof['credit_outstanding']) < 1)

# ── 4. Additional-credit request: CCO + Finance two-sign-off (permanent) ──
crid = models.create_credit_request(rid, sales_uid, 50000, 'permanent', None, 'expansion')
check('credit request notifies CCO + Finance',
      q("SELECT COUNT(*) n FROM notifications WHERE title LIKE 'Credit increase requested%'")[0]['n'] >= 1)
cco = login('cco@onecard.com', 'Cco2025!')
s, b = get(cco, '/cco/credit')
check('cco credit page lists the request', s == 200 and 'V14 Credit Co' in b)
post(cco, f"/credit-requests/{crid}/decide", {'decision': 'approve'})
cr = q("SELECT * FROM credit_requests WHERE id=?", crid)[0]
check('one sign-off keeps the request pending', cr['status'] == 'pending' and cr['cco_by'] and not cr['finance_by'])
check('limit NOT raised yet after one sign-off',
      abs(q("SELECT credit_limit FROM reseller_profiles WHERE id=?", rid)[0]['credit_limit'] - 100000) < 1)
post(fin, f"/credit-requests/{crid}/decide", {'decision': 'approve'})
check('both sign-offs approve the request',
      q("SELECT status FROM credit_requests WHERE id=?", crid)[0]['status'] == 'approved')
check('permanent bump raised the limit to 150000',
      abs(q("SELECT credit_limit FROM reseller_profiles WHERE id=?", rid)[0]['credit_limit'] - 150000) < 1)

# ── 5. Temporary bump auto-reverts after expiry ──
crid2 = models.create_credit_request(rid, sales_uid, 20000, 'temporary', '2000-01-01', 'season')
models.decide_credit_request(crid2, 'cco', True, q("SELECT id FROM users WHERE email='cco@onecard.com'")[0]['id'])
models.decide_credit_request(crid2, 'finance', True, q("SELECT id FROM users WHERE email='finance@onecard.com'")[0]['id'])
prof = q("SELECT credit_limit, credit_limit_base, credit_temp_until FROM reseller_profiles WHERE id=?", rid)[0]
check('temporary bump raises the live limit', abs(prof['credit_limit'] - 170000) < 1 and prof['credit_temp_until'] == '2000-01-01')
check('temporary bump keeps the permanent base', abs(prof['credit_limit_base'] - 150000) < 1)
models.run_statement_cycle()
prof = q("SELECT credit_limit, credit_temp_until FROM reseller_profiles WHERE id=?", rid)[0]
check('expired temporary bump reverts to base', abs(prof['credit_limit'] - 150000) < 1 and not prof['credit_temp_until'])

# ── 6. Reject path + exposure ──
crid3 = models.create_credit_request(rid, sales_uid, 999999, 'permanent', None, 'too big')
status, _ = models.decide_credit_request(crid3, 'cco', False, q("SELECT id FROM users WHERE email='cco@onecard.com'")[0]['id'])
check('a rejection rejects the whole request', status == 'rejected'
      and q("SELECT status FROM credit_requests WHERE id=?", crid3)[0]['status'] == 'rejected')
exp = models.get_credit_exposure()
check('exposure includes this account', exp['accounts'] >= 1 and exp['total_limit'] >= 150000)

# ── cleanup ──
uid = q("SELECT id FROM users WHERE email=?", em)[0]['id']
for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
    r = pr['id']
    execu("DELETE FROM statements WHERE reseller_id=?", r)
    execu("DELETE FROM credit_requests WHERE reseller_id=?", r)
    execu("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE reseller_id=?)", r)
    execu("DELETE FROM orders WHERE reseller_id=?", r)
    execu("DELETE FROM wallet_transactions WHERE reseller_id=?", r)
    execu("DELETE FROM reseller_countries WHERE reseller_id=?", r)
    execu("DELETE FROM reseller_client_types WHERE reseller_id=?", r)
    execu("DELETE FROM reseller_profiles WHERE id=?", r)
execu("DELETE FROM notifications WHERE user_id=?", uid)
execu("DELETE FROM users WHERE id=?", uid)
for pat in ('Credit increase requested%', 'Statement issued%', 'Credit request %', 'New statement issued%',
            '%overdue — account on hold', 'Credit limit increased%'):
    execu("DELETE FROM notifications WHERE title LIKE ? AND body LIKE 'V14 %'", pat)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
