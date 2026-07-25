"""E2E test for v21 (Phase 1 buy-side): supplier account models
(prepaid/credit/consignment), our credit limit with a supplier, payables accrual
on purchase, the limit check, recording payments, and the Finance Payables view."""
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


def post(op, path, data):
    data = dict(data); data.setdefault('_csrf', getattr(op, '_csrf', ''))
    r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
    return r.status, r.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


tag = uuid.uuid4().hex[:8]
prod = q("SELECT id, merchant FROM products WHERE COALESCE(is_issued,0)=0 AND is_active=1 LIMIT 1")[0]
ops_uid = q("SELECT id FROM users WHERE email='ops@onecard.com'")[0]['id']

# ── a CREDIT supplier with a 100k limit they grant us ──
credit_sid = models.upsert_supplier(None, f'V21 Credit Supplier {tag}', 'C', 'c@sup.test', '',
                                    'Net 30', '', [prod['merchant']],
                                    account_type='credit', our_credit_limit=100000,
                                    settlement_terms_days=30)
s = models.get_supplier(credit_sid)
check('credit supplier stored with our limit + terms',
      s['account_type'] == 'credit' and abs(s['our_credit_limit'] - 100000) < 1 and s['settlement_terms_days'] == 30)
check('supplier_available_to_buy = limit initially', models.supplier_available_to_buy(s) == 100000)

# ── buying on credit accrues what we owe ──
bid, err = models.create_batch(credit_sid, prod['id'], 100, 300, invoice_ref='INV-1', created_by=ops_uid)
check('credit purchase recorded', bid and not err, str(err))
s = models.get_supplier(credit_sid)
check('credit purchase accrues our_outstanding (100*300=30000)', abs(s['our_outstanding'] - 30000) < 1,
      f"outstanding={s['our_outstanding']}")
check('headroom dropped to 70000', models.supplier_available_to_buy(s) == 70000)

# ── a purchase beyond our headroom is refused ──
bid2, err2 = models.create_batch(credit_sid, prod['id'], 1000, 100, created_by=ops_uid)  # 100000 > 70000 left
check('purchase beyond our credit limit is refused', bid2 is None and err2 and 'exceeds our available credit' in err2.lower())

# ── prepaid supplier: no payable accrues ──
prepaid_sid = models.upsert_supplier(None, f'V21 Prepaid Supplier {tag}', 'P', 'p@sup.test', '',
                                     'Prepaid', '', [prod['merchant']], account_type='prepaid')
models.create_batch(prepaid_sid, prod['id'], 50, 200, created_by=ops_uid)
check('prepaid purchase does NOT accrue a payable',
      abs(models.get_supplier(prepaid_sid)['our_outstanding']) < 0.01)

# ── recording a payment reduces what we owe ──
ok, perr = models.pay_supplier(credit_sid, 20000, method='bank', reference='PAY-1',
                               paid_by=q("SELECT id FROM users WHERE email='finance@onecard.com'")[0]['id'])
s = models.get_supplier(credit_sid)
check('payment reduces our_outstanding (30000-20000=10000)', ok and abs(s['our_outstanding'] - 10000) < 1)
check('payment recorded in supplier_payments',
      len(models.get_supplier_payments(supplier_id=credit_sid)) == 1)

# ── payables summary + list ──
summ = models.get_payables_summary()
check('payables summary reflects outstanding', summ['total_outstanding'] >= 10000 and summ['accounts'] >= 1)
payables = {p['id']: p for p in models.get_supplier_payables()}
check('credit supplier appears in payables, prepaid does not',
      credit_sid in payables and prepaid_sid not in payables)

# ── HTTP: Finance payables page + record a payment via the route ──
fin = login('finance@onecard.com', 'Finance2025!')
s_, b = get(fin, '/finance/payables')
check('Finance payables page renders with the supplier',
      s_ == 200 and f'V21 Credit Supplier {tag}' in b and 'We Owe' in b)
post(fin, f'/finance/payables/{credit_sid}/pay', {'amount': '10000', 'reference': 'PAY-2'})
check('paying the rest via the route settles the supplier',
      abs(models.get_supplier(credit_sid)['our_outstanding']) < 0.01)

# ── HTTP: Ops suppliers page shows the account model ──
ops = login('ops@onecard.com', 'Ops2025!')
s_, b = get(ops, '/ops/suppliers')
check('Ops suppliers page shows the settlement model',
      s_ == 200 and 'How we settle them' in b and f'V21 Credit Supplier {tag}' in b)

# ── cleanup ──
for sid in (credit_sid, prepaid_sid):
    execu("DELETE FROM supplier_payments WHERE supplier_id=?", sid)
    execu("DELETE FROM order_item_allocations WHERE batch_id IN (SELECT id FROM purchase_batches WHERE supplier_id=?)", sid)
    execu("DELETE FROM purchase_batches WHERE supplier_id=?", sid)
    execu("DELETE FROM supplier_merchants WHERE supplier_id=?", sid)
    execu("DELETE FROM suppliers WHERE id=?", sid)
execu("DELETE FROM notifications WHERE title LIKE '%purchase batch%' AND body LIKE '%V21 %'")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
