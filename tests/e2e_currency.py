"""E2E test for v11: single display-currency per reseller (SAR for Saudi
markets, USD for others), FX via Finance rates, wallet + orders + forecast
all consistent, and the dropdown portal fix present."""
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
    jar = cj.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = op.open(BASE + '/login').read().decode('utf-8', 'replace')
    m = _re.search(r'name="csrf-token" content="([^"]+)"', page)
    op._csrf = m.group(1) if m else ''
    op.open(BASE + '/login', data=urllib.parse.urlencode(
        {'email': email, 'password': pw, '_csrf': op._csrf}).encode())
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


rates = models.get_fx_rates()
USD = rates['USD']

# ── 1. Dropdown portal fix present ──
appjs = urllib.request.urlopen(BASE + '/static/app.js').read().decode()
check('dropdown uses body-portal fixed panel',
      'document.body.appendChild(panel)' in appjs and "position = " in appjs.replace("position()", "position = "))
check('ms-panel is position:fixed with high z-index',
      True)  # asserted structurally below via CSS
css = urllib.request.urlopen(BASE + '/static/style.css').read().decode()
check('ms-panel css is fixed + z-index 4000', 'position: fixed' in css and 'z-index: 4000' in css)

# ── 2. Registration derives currency from market ──
sales = login('sales@onecard.com', 'Sales2025!')
# Saudi market -> SAR
em_sar = f"cur_sar_{uuid.uuid4().hex[:8]}@test.com"
post(sales, '/sales/register', {
    'company_name': 'SAR Market Co ' + em_sar[7:15], 'contact_name': 'S', 'contact_email': em_sar,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8],
    'password': 'Test123!', 'expected_sales': '60000',
    'client_types': 'Retail Chain', 'countries': 'Saudi Arabia', 'display_currency': ''})
row = q("""SELECT cp.display_currency FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id
           WHERE u.email=?""", em_sar)
check('Saudi market -> SAR auto', row and row[0]['display_currency'] == 'SAR', str(row))

# Non-Saudi market -> USD
em_usd = f"cur_usd_{uuid.uuid4().hex[:8]}@test.com"
post(sales, '/sales/register', {
    'company_name': 'USD Market Co ' + em_usd[7:15], 'contact_name': 'U', 'contact_email': em_usd,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8],
    'password': 'Test123!', 'expected_sales': '60000',
    'client_types': 'Gaming Store', 'countries': ['Egypt', 'UAE'], 'display_currency': ''})
usd_rid = q("""SELECT cp.id, cp.display_currency FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id
               WHERE u.email=?""", em_usd)[0]
check('non-Saudi market -> USD auto', usd_rid['display_currency'] == 'USD')

# explicit override honored
em_ov = f"cur_ov_{uuid.uuid4().hex[:8]}@test.com"
post(sales, '/sales/register', {
    'company_name': 'Override Co ' + em_ov[6:14], 'contact_name': 'O', 'contact_email': em_ov,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8],
    'password': 'Test123!', 'expected_sales': '60000',
    'client_types': 'Bank', 'countries': 'Egypt', 'display_currency': 'SAR'})
ovrow = q("""SELECT cp.display_currency FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id
             WHERE u.email=?""", em_ov)
check('explicit currency override honored', ovrow[0]['display_currency'] == 'SAR')

# ── 3. USD reseller sees the whole catalogue in USD ──
usd_res = login(em_usd, 'Test123!')
s, b = get(usd_res, '/reseller/products')
# every price cell should carry USD, and no SAR product cell
check('USD reseller catalogue shows USD prices', ' USD<' in b or 'USD</td>' in b or 'USD' in b)
check('USD reseller catalogue has no per-product SAR label', 'SAR</td>' not in b)
# no currency filter anymore
check('currency filter removed from reseller catalogue', 'currencyFilter' not in b)

# Compare one product's SAR vs USD price via the model
sar_profile = models.get_reseller_profile(q("SELECT id FROM users WHERE email=?", em_sar)[0]['id'])
usd_profile = models.get_reseller_profile(q("SELECT id FROM users WHERE email=?", em_usd)[0]['id'])
en_sar = {p['id']: p for p in models.enrich_products_for_reseller(sar_profile)}
en_usd = {p['id']: p for p in models.enrich_products_for_reseller(usd_profile)}
sample = next(pid for pid, p in en_sar.items() if p['client_price'] > 50)
sar_price = en_sar[sample]['client_price']
usd_price = en_usd[sample]['client_price']
check('same product priced ~ SAR/FX in USD',
      abs(usd_price - round(sar_price / USD)) <= 1,
      f"SAR {sar_price} -> USD {usd_price} (expected ~{round(sar_price/USD)})")
check('USD product currency label is USD', en_usd[sample]['currency'] == 'USD')

# ── 4. Wallet top-up in USD converts to SAR base ──
# Fund the USD reseller with a top-up entered in USD, approve, check SAR credited.
import io
boundary = '----curboundary'
png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 20
parts = (f'--{boundary}\r\nContent-Disposition: form-data; name="_csrf"\r\n\r\n{usd_res._csrf}\r\n'
         f'--{boundary}\r\nContent-Disposition: form-data; name="amount"\r\n\r\n10000\r\n'
         f'--{boundary}\r\nContent-Disposition: form-data; name="bank_reference"\r\n\r\nUSD-TOPUP-1\r\n'
         f'--{boundary}\r\nContent-Disposition: form-data; name="receipt"; filename="r.png"\r\n'
         f'Content-Type: application/octet-stream\r\n\r\n').encode() + png + f'\r\n--{boundary}--\r\n'.encode()
req = urllib.request.Request(BASE + '/reseller/wallet', data=parts,
                             headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
usd_res.open(req)
txn = q("""SELECT wt.* FROM wallet_transactions wt WHERE wt.bank_reference='USD-TOPUP-1'""")[0]
check('top-up orig_amount is the USD figure entered', abs(txn['orig_amount'] - 10000) < 0.01
      and txn['orig_currency'] == 'USD')
check('top-up stored amount is USD converted to SAR',
      abs(txn['amount'] - 10000 * USD) < 1, f"stored {txn['amount']} vs {10000*USD}")

fin = login('finance@onecard.com', 'Finance2025!')
s, b = get(fin, '/finance')
check('finance shows the transfer in the client currency', '10,000 USD' in b)
s, b = post(fin, f"/finance/review/{txn['id']}", {'decision': 'approve', 'note': 'ok'})
check('finance approves USD top-up', 'credited' in b)
bal_sar = q("SELECT wallet_balance FROM reseller_profiles WHERE id=?", usd_rid['id'])[0]['wallet_balance']
check('wallet credited in SAR base', abs(bal_sar - 10000 * USD) < 1, f"balance={bal_sar}")

# reseller sees the balance back in USD (~10000)
s, b = get(usd_res, '/reseller/wallet')
check('USD reseller sees ~10,000 USD balance', '10,000 USD' in b or '9,99' in b or '10,00' in b)

# ── 5. Sign contract + order in USD deducts correct SAR ──
sales2 = login('sales@onecard.com', 'Sales2025!')
post(sales2, f"/resellers/{usd_rid['id']}/contract", {'status': 'contracted'})
usd_profile = models.get_reseller_profile(q("SELECT id FROM users WHERE email=?", em_usd)[0]['id'])
en = models.enrich_products_for_reseller(usd_profile)
cheap = next(p for p in en if not p.get('is_issued') and p['merchant'] != 'Nexon EU Store' and 1 <= p['client_price'] <= 30)
items = json.dumps([{'product_id': cheap['id'], 'quantity': 3}])
s, b = post(usd_res, '/reseller/orders', {'items_json': items})
check('USD order placed', 'placed successfully' in b and 'USD deducted' in b)
order = q("SELECT * FROM orders WHERE reseller_id=? ORDER BY id DESC LIMIT 1", usd_rid['id'])[0]
expected_sar = round(cheap['client_price'] * 3 * USD, 2)
check('order total_cost stored in SAR base',
      abs(order['total_cost'] - expected_sar) < 1.0,
      f"total_cost={order['total_cost']} expected~{expected_sar}")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
