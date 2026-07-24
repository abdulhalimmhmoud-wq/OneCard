"""E2E test for v13 (Phase 1): account models + contract signing.

Covers the contract workflow (Sales uploads draft -> reseller signs & uploads
-> activation, with CCO governance above the auto-approve cap), secured
contract-file serving, and the unified `available_to_spend` ordering gate for
prepaid / credit (full + staged) / consignment, plus the overdue freeze.
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
    r = op.open(BASE + path, data=urllib.parse.urlencode(data, doseq=True).encode())
    return r.status, r.read().decode('utf-8', 'replace')


def post_multipart(op, path, fields, filename='contract.pdf', field='draft'):
    b = '----v13boundary' + uuid.uuid4().hex
    body = b''
    fields = dict(fields)
    fields.setdefault('_csrf', getattr(op, '_csrf', ''))
    for k, v in fields.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
             f'Content-Type: application/pdf\r\n\r\n').encode()
    body += b'%PDF-1.4 test contract\n' + b'\r\n'
    body += f'--{b}--\r\n'.encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    try:
        r = op.open(req)
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]
    c.close()
    return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


def register(sales, email, company):
    post(sales, '/sales/register', {
        'company_name': company + ' ' + email[6:14], 'contact_name': 'T', 'contact_email': email,
        'contact_phone': '05' + str(uuid.uuid4().int)[:8],
        'password': 'Test123!', 'expected_sales': '80000',
        'client_types': 'Retail Chain', 'countries': 'Saudi Arabia', 'display_currency': ''})
    execu("UPDATE reseller_profiles SET nda_accepted_at=CURRENT_TIMESTAMP WHERE user_id=(SELECT id FROM users WHERE email=?)", email)
    return q("""SELECT cp.* FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id
                WHERE u.email=?""", email)[0]


created_emails = []
sales = login('sales@onecard.com', 'Sales2025!')

# ═══════════ 1. Contract workflow (small credit line, Sales activates) ═══════════

em1 = f"v13_cr_{uuid.uuid4().hex[:8]}@test.com"
created_emails.append(em1)
p1 = register(sales, em1, 'V13 Credit Co')

s, b = post_multipart(sales, f"/sales/resellers/{p1['id']}/contract/upload",
                      {'account_type': 'credit', 'credit_limit': '50000',
                       'credit_disbursement': 'full', 'settlement_terms_days': '30',
                       'billing_cycle': 'monthly'})
ct = q("SELECT * FROM contracts WHERE reseller_id=? ORDER BY id DESC LIMIT 1", p1['id'])
check('contract created in status sent', ct and ct[0]['status'] == 'sent' and ct[0]['file_draft'],
      f"status={ct[0]['status'] if ct else None}")
cid1 = ct[0]['id']
check('contract records proposed credit terms',
      abs(ct[0]['credit_limit'] - 50000) < 1 and ct[0]['account_type'] == 'credit')
check('contract "sent" event logged',
      q("SELECT COUNT(*) n FROM contract_events WHERE contract_id=? AND event='sent'", cid1)[0]['n'] == 1)
check('reseller notified to sign',
      q("""SELECT COUNT(*) n FROM notifications nt JOIN users u ON nt.user_id=u.id
           WHERE u.email=? AND nt.title LIKE '%ready to sign%'""", em1)[0]['n'] >= 1)

res1 = login(em1, 'Test123!')
s, b = get(res1, '/reseller/contract')
check('reseller sees the contract page with the draft', s == 200 and 'Download contract to sign' in b)
s, b = post_multipart(res1, f"/reseller/contract/{cid1}/sign", {}, field='signed',
                      filename='signed.pdf')
ct = q("SELECT * FROM contracts WHERE id=?", cid1)[0]
check('reseller signed upload sets signed_uploaded', ct['status'] == 'signed_uploaded' and ct['file_signed'])
check('client_signed event logged',
      q("SELECT COUNT(*) n FROM contract_events WHERE contract_id=? AND event='client_signed'", cid1)[0]['n'] == 1)

# small limit (<= cap) -> the sales owner can activate directly
s, b = post(sales, f"/contracts/{cid1}/activate", {})
prof = q("SELECT * FROM reseller_profiles WHERE id=?", p1['id'])[0]
check('sales activates a <=cap credit line', prof['account_type'] == 'credit'
      and prof['contract_status'] == 'contracted' and abs(prof['credit_limit'] - 50000) < 1)
check('contract marked active', q("SELECT status FROM contracts WHERE id=?", cid1)[0]['status'] == 'active')

# ═══════════ 2. Governance: large limit needs CCO ═══════════

em2 = f"v13_big_{uuid.uuid4().hex[:8]}@test.com"
created_emails.append(em2)
p2 = register(sales, em2, 'V13 Big Credit Co')
post_multipart(sales, f"/sales/resellers/{p2['id']}/contract/upload",
               {'account_type': 'credit', 'credit_limit': '500000',
                'credit_disbursement': 'staged', 'credit_tranche': '150000',
                'settlement_terms_days': '30', 'billing_cycle': 'monthly'})
cid2 = q("SELECT id FROM contracts WHERE reseller_id=? ORDER BY id DESC LIMIT 1", p2['id'])[0]['id']
res2 = login(em2, 'Test123!')
post_multipart(res2, f"/reseller/contract/{cid2}/sign", {}, field='signed', filename='s.pdf')
# sales tries to activate a >cap line -> blocked
post(sales, f"/contracts/{cid2}/activate", {})
prof2 = q("SELECT account_type, contract_status FROM reseller_profiles WHERE id=?", p2['id'])[0]
check('sales cannot activate a >cap credit line', prof2['account_type'] == 'prepaid'
      and prof2['contract_status'] != 'contracted')
check('big contract still awaiting activation',
      q("SELECT status FROM contracts WHERE id=?", cid2)[0]['status'] == 'signed_uploaded')
# CCO sees the queue and activates
cco = login('cco@onecard.com', 'Cco2025!')
s, b = get(cco, '/cco/contracts')
check('cco approval queue lists the pending contract', 'V13 Big Credit Co' in b)
post(cco, f"/contracts/{cid2}/activate", {})
prof2 = q("SELECT account_type, contract_status, credit_limit, credit_tranche, credit_disbursement FROM reseller_profiles WHERE id=?", p2['id'])[0]
check('cco activates the large credit line', prof2['account_type'] == 'credit'
      and prof2['contract_status'] == 'contracted' and abs(prof2['credit_limit'] - 500000) < 1
      and prof2['credit_disbursement'] == 'staged' and abs(prof2['credit_tranche'] - 150000) < 1)

# ═══════════ 3. Contract files are access-controlled ═══════════

s, b = get(res1, f"/contracts/{cid1}/file/draft")
check('owning reseller can fetch their contract file', s == 200, f"status={s}")
s, b = get(res2, f"/contracts/{cid1}/file/draft")   # different reseller
check('a stranger reseller is denied (403)', s == 403, f"status={s}")

# ═══════════ 4. available_to_spend + create_order per account type ═══════════

prod = q("SELECT id, product_name, merchant, category FROM products WHERE COALESCE(is_issued,0)=0 AND is_active=1 LIMIT 1")[0]


def order_items(unit_price, qty):
    return [{'product_rowid': prod['id'], 'product_name': prod['product_name'],
             'merchant': prod['merchant'], 'category': prod['category'], 'currency': 'SAR',
             'quantity': qty, 'unit_price': unit_price, 'unit_face': unit_price}]


def set_credit(rid, **kw):
    cols = ','.join(f"{k}=?" for k in kw)
    execu(f"UPDATE reseller_profiles SET {cols} WHERE id=?", *kw.values(), rid)

# prepaid unchanged
krow = q("SELECT cp.id, cp.wallet_balance FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email='khalid@alnoor-digital.com'")[0]
prepaid = {'wallet_balance': krow['wallet_balance']}
kid = krow['id']
oid, err = models.create_order(kid, order_items(1000, 1))
after = q("SELECT wallet_balance FROM reseller_profiles WHERE id=?", kid)[0]['wallet_balance']
check('prepaid order still debits wallet', err is None and abs((prepaid['wallet_balance'] - after) - 1000) < 1)
check('prepaid order logs an "order" ledger row',
      q("SELECT type FROM wallet_transactions WHERE reseller_id=? ORDER BY id DESC LIMIT 1", kid)[0]['type'] == 'order')
execu("UPDATE reseller_profiles SET wallet_balance=? WHERE id=?", prepaid['wallet_balance'], kid)  # restore

# credit FULL: available = limit - outstanding, draw -> outstanding
set_credit(p1['id'], account_type='credit', credit_limit=10000, credit_disbursement='full',
           credit_outstanding=0, credit_frozen=0)
prof = models.get_reseller_profile_by_id(p1['id'])
check('credit full available = limit - outstanding', models.available_to_spend(prof) == 10000)
oid, err = models.create_order(p1['id'], order_items(3000, 1))
prof = q("SELECT credit_outstanding, wallet_balance FROM reseller_profiles WHERE id=?", p1['id'])[0]
check('credit draw increases outstanding, not wallet',
      err is None and abs(prof['credit_outstanding'] - 3000) < 1 and prof['wallet_balance'] == 0)
check('credit draw logs a credit_draw ledger row',
      q("SELECT type FROM wallet_transactions WHERE reseller_id=? ORDER BY id DESC LIMIT 1", p1['id'])[0]['type'] == 'credit_draw')
check('credit full available now 7000',
      models.available_to_spend(models.get_reseller_profile_by_id(p1['id'])) == 7000)

# credit STAGED: available = min(tranche, headroom)
set_credit(p1['id'], credit_disbursement='staged', credit_tranche=2000, credit_outstanding=0)
prof = models.get_reseller_profile_by_id(p1['id'])
check('staged available capped at tranche', models.available_to_spend(prof) == 2000)
oid, err = models.create_order(p1['id'], order_items(2500, 1))
check('staged blocks a draw above the tranche', oid is None and err and 'available' in err.lower())
oid, err = models.create_order(p1['id'], order_items(1500, 1))
prof = q("SELECT credit_outstanding FROM reseller_profiles WHERE id=?", p1['id'])[0]
check('staged allows a draw within the tranche', err is None and abs(prof['credit_outstanding'] - 1500) < 1)
check('staged replenishes to a full tranche again',
      models.available_to_spend(models.get_reseller_profile_by_id(p1['id'])) == 2000)

# consignment
set_credit(p2['id'], account_type='consignment', credit_limit=5000, credit_outstanding=0,
           credit_disbursement='full', credit_frozen=0)
prof = models.get_reseller_profile_by_id(p2['id'])
check('consignment available = limit - outstanding', models.available_to_spend(prof) == 5000)
oid, err = models.create_order(p2['id'], order_items(4000, 1))
prof = q("SELECT credit_outstanding FROM reseller_profiles WHERE id=?", p2['id'])[0]
check('consignment draw accrues to outstanding', err is None and abs(prof['credit_outstanding'] - 4000) < 1)
check('consignment draw logs a consignment_draw ledger row',
      q("SELECT type FROM wallet_transactions WHERE reseller_id=? ORDER BY id DESC LIMIT 1", p2['id'])[0]['type'] == 'consignment_draw')

# frozen credit -> nothing available
set_credit(p1['id'], credit_frozen=1)
prof = models.get_reseller_profile_by_id(p1['id'])
check('frozen credit line has zero available', models.available_to_spend(prof) == 0)
oid, err = models.create_order(p1['id'], order_items(100, 1))
check('frozen credit line cannot order', oid is None and err and 'hold' in err.lower())

# limit reached notification (fill remaining consignment headroom)
set_credit(p2['id'], credit_frozen=0, credit_outstanding=0, credit_limit=1000, account_type='consignment')
models.create_order(p2['id'], order_items(1000, 1))
check('team notified when the limit is reached',
      q("""SELECT COUNT(*) n FROM notifications WHERE title LIKE 'Credit limit reached%'""")[0]['n'] >= 1)

# ── cleanup ──
for em in created_emails:
    urows = q("SELECT id FROM users WHERE email=?", em)
    if not urows:
        continue
    uid = urows[0]['id']
    for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
        rid = pr['id']
        execu("DELETE FROM contract_events WHERE contract_id IN (SELECT id FROM contracts WHERE reseller_id=?)", rid)
        execu("DELETE FROM contracts WHERE reseller_id=?", rid)
        execu("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE reseller_id=?)", rid)
        execu("DELETE FROM orders WHERE reseller_id=?", rid)
        execu("DELETE FROM wallet_transactions WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_countries WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_client_types WHERE reseller_id=?", rid)
        execu("DELETE FROM reseller_profiles WHERE id=?", rid)
    execu("DELETE FROM notifications WHERE user_id=?", uid)
    execu("DELETE FROM users WHERE id=?", uid)
execu("DELETE FROM notifications WHERE title LIKE 'Credit limit reached%' AND body LIKE 'V13 %'")
execu("DELETE FROM notifications WHERE title LIKE 'New credit line activated%' AND body LIKE 'V13 %'")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
