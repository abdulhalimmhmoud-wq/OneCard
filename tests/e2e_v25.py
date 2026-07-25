"""E2E test for v25 refinements: consignment settle-on sale/redemption, supplier
over-limit warns (no block), configurable buy-decision weights, redemption API."""
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


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


def api(key, path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={'X-API-Key': key, 'Content-Type': 'application/json'})
    try:
        r = urllib.request.urlopen(req); return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


tag = uuid.uuid4().hex[:8]
prod = q("SELECT id, product_name, merchant, category, cost FROM products WHERE COALESCE(is_issued,0)=0 AND is_active=1 LIMIT 1")[0]
ops_uid = q("SELECT id FROM users WHERE email='ops@onecard.com'")[0]['id']

# ── 1. supplier over-limit WARNS but does not block (credit) ──
credit_sid = models.upsert_supplier(None, f'V25 Credit {tag}', 'C', '', '', '', '',
                                    [prod['merchant']], account_type='credit', our_credit_limit=5000)
bid, err = models.create_batch(credit_sid, prod['id'], 100, 100, created_by=ops_uid)  # 10000 > 5000
check('over-limit purchase is NOT blocked (warns instead)', bid is not None and err is None)
check('over-limit purchase still accrues the payable', abs(models.get_supplier(credit_sid)['our_outstanding'] - 10000) < 1)
check('over-limit warning notification sent',
      q("SELECT COUNT(*) n FROM notifications WHERE title LIKE '%Over supplier credit limit%'")[0]['n'] >= 1)
check('payables view flags the supplier as over limit',
      next(p for p in models.get_supplier_payables() if p['id'] == credit_sid)['over_limit'] is True)

# ── 2. consignment 'sale' — no payable at purchase, accrues when sold ──
kid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email='khalid@alnoor-digital.com'")[0]['id']
psale = q("""SELECT id, product_name, merchant, category FROM products
             WHERE COALESCE(is_issued,0)=0 AND is_active=1
               AND id NOT IN (SELECT product_rowid FROM purchase_batches) LIMIT 1""")[0]
sale_sid = models.upsert_supplier(None, f'V25 ConsignSale {tag}', 'C', '', '', '', '',
                                  [psale['merchant']], account_type='consignment',
                                  our_credit_limit=100000, consignment_settle_on='sale')
models.create_batch(sale_sid, psale['id'], 200, 50, created_by=ops_uid)   # only batch for this product
check('consignment purchase adds NO payable at purchase', abs(models.get_supplier(sale_sid)['our_outstanding']) < 0.01)
items = [{'product_rowid': psale['id'], 'product_name': psale['product_name'], 'merchant': psale['merchant'],
          'category': psale['category'], 'currency': 'SAR', 'quantity': 30, 'unit_price': 200, 'unit_face': 200}]
before = models.get_supplier(sale_sid)['our_outstanding']
models.create_order(kid, items)   # 30 units from the consignment-sale batch
after = models.get_supplier(sale_sid)['our_outstanding']
check('consignment-sale accrues when the unit is sold (30 x 50 = 1500)', abs((after - before) - 1500) < 1,
      f"delta={after - before}")

# ── 3. consignment 'redemption' — accrues only when redeemed (via API) ──
redeem_sid = models.upsert_supplier(None, f'V25 ConsignRedeem {tag}', 'C', '', '', '', '',
                                    [prod['merchant']], account_type='consignment',
                                    our_credit_limit=100000, consignment_settle_on='redemption')
# fresh product with only this supplier's stock so FIFO draws from it
prod2 = q("""SELECT id, product_name, merchant, category FROM products
             WHERE COALESCE(is_issued,0)=0 AND is_active=1
               AND id NOT IN (SELECT product_rowid FROM purchase_batches) LIMIT 1""")[0]
models.create_batch(redeem_sid, prod2['id'], 100, 80, created_by=ops_uid)
i2 = [{'product_rowid': prod2['id'], 'product_name': prod2['product_name'], 'merchant': prod2['merchant'],
       'category': prod2['category'], 'currency': 'SAR', 'quantity': 10, 'unit_price': 200, 'unit_face': 200}]
oid, _ = models.create_order(kid, i2)
check('redemption supplier: selling does NOT accrue yet', abs(models.get_supplier(redeem_sid)['our_outstanding']) < 0.01)
line_id = q("SELECT id FROM order_items WHERE order_id=? ORDER BY id LIMIT 1", oid)[0]['id']
accrued, _ = models.record_redemption(line_id, 4)   # 4 x 80 = 320
check('redemption accrues to the consignment supplier (4 x 80 = 320)',
      abs(accrued - 320) < 1 and abs(models.get_supplier(redeem_sid)['our_outstanding'] - 320) < 1)
accrued2, _ = models.record_redemption(line_id, 100)  # only 6 units left of the 10 sold
check('redemption is capped at units sold (6 left -> 6 x 80 = 480)', abs(accrued2 - 480) < 1)

# ── 4. redemption via the API ──
key = models.set_reseller_api(kid, rotate_key=True)
# a fresh redemption-supplier product + order for a clean API test
prod3 = q("""SELECT id, product_name, merchant, category FROM products
             WHERE COALESCE(is_issued,0)=0 AND is_active=1
               AND id NOT IN (SELECT product_rowid FROM purchase_batches) LIMIT 1""")[0]
rs3 = models.upsert_supplier(None, f'V25 R3 {tag}', 'C', '', '', '', '', [prod3['merchant']],
                             account_type='consignment', our_credit_limit=50000, consignment_settle_on='redemption')
models.create_batch(rs3, prod3['id'], 50, 60, created_by=ops_uid)
i3 = [{'product_rowid': prod3['id'], 'product_name': prod3['product_name'], 'merchant': prod3['merchant'],
       'category': prod3['category'], 'currency': 'SAR', 'quantity': 5, 'unit_price': 150, 'unit_face': 150}]
oid3, _ = models.create_order(kid, i3)
line3 = q("SELECT id FROM order_items WHERE order_id=? ORDER BY id LIMIT 1", oid3)[0]['id']
s, r = api(key, '/api/v1/redemptions', {'redemptions': [{'line_id': line3, 'quantity': 2}]})
check('redemption API accrues (2 x 60 = 120)', s == 200 and abs(r.get('total_supplier_accrued_sar', 0) - 120) < 1)
s, r = api(key, '/api/v1/redemptions', {'redemptions': [{'line_id': 999999999, 'quantity': 1}]})
check('redemption API rejects a line that is not ours', s == 422)

# ── 5. configurable buy weights ──
models.set_setting('buy.new_client_forecast_weight', 25)
models.set_setting('buy.reorder_days', 3)
cfg = models.get_buy_settings()
check('buy settings are configurable + read back', cfg['new_client_forecast_weight'] == 25 and cfg['reorder_days'] == 3)
models.set_setting('buy.new_client_forecast_weight', 40)   # restore
models.set_setting('buy.reorder_days', 7)

# ── cleanup ──
execu("UPDATE reseller_profiles SET wallet_balance=500000 WHERE id=?", kid)
for sid in (credit_sid, sale_sid, redeem_sid, rs3):
    for t in ('supplier_statements', 'supplier_payments', 'supplier_merchants', 'supplier_products'):
        execu(f"DELETE FROM {t} WHERE supplier_id=?", sid)
    execu("DELETE FROM order_item_allocations WHERE batch_id IN (SELECT id FROM purchase_batches WHERE supplier_id=?)", sid)
    execu("DELETE FROM purchase_batches WHERE supplier_id=?", sid)
    execu("DELETE FROM suppliers WHERE id=?", sid)
for oid_ in [r['id'] for r in q("SELECT id FROM orders WHERE reseller_id=? ORDER BY id DESC LIMIT 3", kid)]:
    execu("DELETE FROM order_item_allocations WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=?)", oid_)
    execu("DELETE FROM order_items WHERE order_id=?", oid_)
    execu("DELETE FROM orders WHERE id=?", oid_)
    execu("DELETE FROM wallet_transactions WHERE reseller_id=? AND note LIKE ?", kid, f'Order #{oid_}')
execu("UPDATE reseller_profiles SET wallet_balance=500000 WHERE id=?", kid)
execu("DELETE FROM notifications WHERE title LIKE '%Over supplier credit limit%'")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
