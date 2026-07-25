"""E2E test for v22 (Phase 2 buy-side): supplier period statements + overdue
cycle + FIFO settlement, and the api/offline buying method on purchases."""
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
prod = q("SELECT id, merchant FROM products WHERE COALESCE(is_issued,0)=0 AND is_active=1 LIMIT 1")[0]
ops_uid = q("SELECT id FROM users WHERE email='ops@onecard.com'")[0]['id']
fin_uid = q("SELECT id FROM users WHERE email='finance@onecard.com'")[0]['id']

sid = models.upsert_supplier(None, f'V22 Supplier {tag}', 'C', 'c@sup.test', '', 'Net 30', '',
                             [prod['merchant']], account_type='credit', our_credit_limit=200000,
                             settlement_terms_days=30)

# ── buy on credit (accrues payable), record the buying method ──
models.create_batch(sid, prod['id'], 100, 300, created_by=ops_uid, method='offline')   # 30000
models.create_batch(sid, prod['id'], 50, 200, created_by=ops_uid, method='api')        # 10000
check('offline + api purchases both recorded with method',
      {'offline', 'api'} <= {b['method'] for b in q("SELECT method FROM purchase_batches WHERE supplier_id=?", sid)})
check('our_outstanding accrued to 40000', abs(models.get_supplier(sid)['our_outstanding'] - 40000) < 1)

# ── issue a supplier statement for the unbilled amount ──
check('unbilled = full outstanding before any statement', abs(models.unbilled_supplier_amount(sid) - 40000) < 1)
st1 = models.issue_supplier_statement(sid, actor_id=fin_uid)
srow = q("SELECT * FROM supplier_statements WHERE id=?", st1)[0]
check('supplier statement issued for 40000 with a due date',
      srow['status'] == 'issued' and abs(srow['amount'] - 40000) < 1 and srow['due_at'])
check('nothing left unbilled after issuing', abs(models.unbilled_supplier_amount(sid)) < 1)
check('re-issuing with nothing new returns None', models.issue_supplier_statement(sid, actor_id=fin_uid) is None)

# ── a further purchase is unbilled again ──
models.create_batch(sid, prod['id'], 100, 100, created_by=ops_uid)   # +10000
check('new purchase becomes unbilled', abs(models.unbilled_supplier_amount(sid) - 10000) < 1)

# ── overdue detection: push the statement due date into the past ──
execu("UPDATE supplier_statements SET due_at='2000-01-01' WHERE id=?", st1)
models.run_supplier_statement_cycle()
check('past-due supplier statement flips to overdue',
      q("SELECT status FROM supplier_statements WHERE id=?", st1)[0]['status'] == 'overdue')

# ── paying settles statements FIFO + reduces outstanding ──
ok, _ = models.pay_supplier(sid, 40000, reference='PAY-A', paid_by=fin_uid)
check('paying 40000 clears the overdue statement (FIFO)',
      ok and q("SELECT status FROM supplier_statements WHERE id=?", st1)[0]['status'] == 'paid')
check('outstanding drops to 10000 after the payment',
      abs(models.get_supplier(sid)['our_outstanding'] - 10000) < 1)

# ── HTTP: Finance payables shows open statements + Ops batches shows API tag ──
fin = login('finance@onecard.com', 'Finance2025!')
s_, b = get(fin, '/finance/payables')
check('payables page renders the statements section', s_ == 200 and 'Open Supplier Statements' in b)
ops = login('ops@onecard.com', 'Ops2025!')
s_, b = get(ops, '/ops/batches')
check('batches page shows the API-pull tag', s_ == 200 and 'API' in b)

# ── cleanup ──
execu("DELETE FROM supplier_statements WHERE supplier_id=?", sid)
execu("DELETE FROM supplier_payments WHERE supplier_id=?", sid)
execu("DELETE FROM order_item_allocations WHERE batch_id IN (SELECT id FROM purchase_batches WHERE supplier_id=?)", sid)
execu("DELETE FROM purchase_batches WHERE supplier_id=?", sid)
execu("DELETE FROM supplier_merchants WHERE supplier_id=?", sid)
execu("DELETE FROM suppliers WHERE id=?", sid)
execu("DELETE FROM notifications WHERE title LIKE '%Supplier payment overdue%'")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
