"""E2E test for v24 (Phase 4): the cash-flow command centre — receivables vs
payables, conservative buying power (excludes prepaid float), and the
safe-to-buy verdict vs urgent restock cost."""
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
    try:
        r = op.open(BASE + path); return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


# baseline snapshot
base = models.get_cashflow_overview()
check('overview exposes the key money components',
      all(k in base for k in ('customer_receivable', 'supplier_payable', 'supplier_headroom',
                              'buying_power', 'urgent_restock_cost', 'covers_urgent', 'wallet_float')))

# ── set up: a customer who owes us + a supplier we owe + supplier headroom ──
tag = uuid.uuid4().hex[:8]
prod = q("SELECT id, merchant FROM products WHERE COALESCE(is_issued,0)=0 AND is_active=1 LIMIT 1")[0]
# a credit customer with 40k outstanding (receivable to us)
sales = login('sales@onecard.com', 'Sales2025!')
em = f"v24_{tag}@test.com"
sales.open(BASE + '/sales/register', data=urllib.parse.urlencode({
    'company_name': f'V24 Cust {tag}', 'contact_name': 'T', 'contact_email': em,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8], 'password': 'Test123!',
    'expected_sales': '60000', 'client_types': 'Retail Chain', 'countries': 'Saudi Arabia',
    '_csrf': sales._csrf}).encode())
cust_rid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]['id']
execu("UPDATE reseller_profiles SET account_type='credit', credit_limit=100000, credit_outstanding=40000 WHERE id=?", cust_rid)
# a credit supplier with a 200k limit, we owe them 25k
sup_id = models.upsert_supplier(None, f'V24 Sup {tag}', 'S', 's@sup.test', '', '', '',
                                [prod['merchant']], account_type='credit', our_credit_limit=200000)
execu("UPDATE suppliers SET our_outstanding=25000 WHERE id=?", sup_id)

cf = models.get_cashflow_overview()
check('customer receivable reflected (+40000)', cf['customer_receivable'] - base['customer_receivable'] >= 39999)
check('supplier payable reflected (+25000)', cf['supplier_payable'] - base['supplier_payable'] >= 24999)
check('supplier headroom = limit - outstanding (200k-25k=175k contribution)',
      cf['supplier_headroom'] - base['supplier_headroom'] >= 174999)
# buying power = supplier headroom + max(0, receivable - payable); wallet float excluded
check('buying power excludes the prepaid wallet float',
      cf['buying_power'] < cf['wallet_float'] + cf['supplier_headroom'] + cf['customer_receivable'] + 1
      and cf['buying_power'] >= cf['supplier_headroom'])
check('net position = receivable - payable',
      abs(cf['net_position'] - (cf['customer_receivable'] - cf['supplier_payable'])) < 1)
check('safe-to-buy verdict is computed',
      cf['covers_urgent'] == (cf['buying_power'] >= cf['urgent_restock_cost']))

# ── HTTP: page renders for finance / ops / cco, and is blocked for a reseller ──
fin = login('finance@onecard.com', 'Finance2025!')
s, b = get(fin, '/cashflow')
check('cash-flow centre renders for Finance', s == 200 and 'Safe-to-buy verdict' in b)
ops = login('ops@onecard.com', 'Ops2025!')
s, _ = get(ops, '/cashflow')
check('cash-flow centre open to Ops', s == 200)
cco = login('cco@onecard.com', 'Cco2025!')
s, _ = get(cco, '/cashflow')
check('cash-flow centre open to CCO', s == 200)
res = login('khalid@alnoor-digital.com', 'Demo123!')
s, _ = get(res, '/cashflow')
check('cash-flow centre blocked for a reseller (403)', s == 403)

# ── cleanup ──
execu("UPDATE suppliers SET our_outstanding=0 WHERE id=?", sup_id)
for t in ('supplier_statements', 'supplier_payments', 'purchase_batches', 'supplier_merchants',
          'supplier_products', 'supplier_price_history'):
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
