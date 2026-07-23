"""E2E test for v7 production hardening: FX/SAR money, CSRF, rate-limit, error pages."""
import urllib.request, urllib.parse, urllib.error, http.cookiejar as cj
import re as _re
import json, sqlite3, os

BASE = 'http://127.0.0.1:8000'
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'onecard.db')
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

def post(op, path, data, with_csrf=True):
    data = dict(data)
    if with_csrf:
        data.setdefault('_csrf', getattr(op, '_csrf', ''))
    try:
        r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]
    c.close()
    return rows

# ── 1. CSRF: POST without token is rejected ──
res = login('khalid@alnoor-digital.com', 'Demo123!')
s, b = post(res, '/reseller/forecast', {'items_json': '[]', 'note': 'x'}, with_csrf=False)
check('POST without CSRF rejected (403)', s == 403)
s, b = post(res, '/reseller/wallet', {'amount': '1'}, with_csrf=False)
check('wallet POST without CSRF rejected', s == 403)

# with token works (validation error but not 403)
s, b = post(res, '/reseller/forecast', {'items_json': '[]', 'note': 'x'})
check('POST with CSRF accepted', s == 200)

# ── 2. Friendly error pages ──
try:
    opener().open(BASE + '/definitely-not-a-page')
    check('404 page', False)
except urllib.error.HTTPError as e:
    body = e.read().decode('utf-8', 'replace')
    check('404 friendly page', e.code == 404 and 'Back' in body)

# ── 3. Login rate limit ──
op2 = opener()
page = op2.open(BASE + '/login').read().decode()
tok = _re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
last = None
for i in range(6):
    try:
        r = op2.open(BASE + '/login', data=urllib.parse.urlencode(
            {'email': 'bruteforce@test.com', 'password': 'wrong', '_csrf': tok}).encode())
        last = r.status
        body = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        last = e.code
        body = e.read().decode('utf-8', 'replace')
check('login locked after repeated failures (429)', last == 429 and 'Too many' in body)

# ── 4. FX: orders record a SAR-consistent line + wallet deduction ──
# v11 note: each reseller sees the whole catalogue in ONE display currency,
# so an order's lines carry that currency (khalid is SAR here) rather than
# the product's original currency. Cross-currency FX is covered end to end
# in tests/e2e_currency.py (a USD reseller). Here we just assert the wallet
# math stays internally consistent.
rates = {r['currency']: r['rate_to_sar'] for r in q("SELECT * FROM currency_rates")}
check('FX rates seeded', len(rates) >= 8 and rates.get('SAR') == 1.0, f"{len(rates)} currencies")

prod = q("""SELECT id FROM products WHERE is_active=1 AND COALESCE(is_issued,0)=0
            AND default_price BETWEEN 5 AND 40 LIMIT 1""")
if prod:
    pid = prod[0]['id']
    prof = q("""SELECT cp.id, cp.wallet_balance, cp.display_currency FROM reseller_profiles cp
                JOIN users u ON cp.user_id=u.id WHERE u.email='khalid@alnoor-digital.com'""")[0]
    items = json.dumps([{'product_id': pid, 'quantity': 2}])
    s, b = post(res, '/reseller/orders', {'items_json': items})
    placed = 'placed successfully' in b
    check('order placed', placed or 'Insufficient' in b,
          'insufficient balance (acceptable)' if 'Insufficient' in b else '')
    if placed:
        oi = q("""SELECT oi.* FROM order_items oi JOIN orders o ON oi.order_id=o.id
                  WHERE o.reseller_id=? ORDER BY oi.id DESC LIMIT 1""", prof['id'])[0]
        check('order line currency is the reseller display currency',
              oi['currency'] == prof['display_currency'], f"line={oi['currency']}")
        check('line_total_sar = line_total * fx_rate (internally consistent)',
              abs(oi['line_total_sar'] - oi['line_total'] * oi['fx_rate']) < 0.05,
              f"sar={oi['line_total_sar']} line={oi['line_total']} fx={oi['fx_rate']}")
        o = q("SELECT total_cost FROM orders WHERE id=?", oi['order_id'])[0]
        wallet_after = q("SELECT wallet_balance FROM reseller_profiles WHERE id=?", prof['id'])[0]
        check('wallet deducted equals the order SAR total',
              abs((prof['wallet_balance'] - wallet_after['wallet_balance']) - o['total_cost']) < 0.05,
              f"deducted={prof['wallet_balance'] - wallet_after['wallet_balance']:.2f} SAR")
else:
    check('a normal product exists for the order test', False)

# backfill sanity: all order items have line_total_sar
missing = q("SELECT COUNT(*) as n FROM order_items WHERE line_total_sar IS NULL")[0]['n']
check('all historical order lines backfilled to SAR', missing == 0, f"missing={missing}")

# ── 5. Finance FX rates page ──
fin = login('finance@onecard.com', 'Finance2025!')
r = fin.open(BASE + '/finance/rates')
b = r.read().decode('utf-8', 'replace')
check('finance FX rates page', r.status == 200 and 'Rate → SAR' in b.replace('&#8594;', '→') or 'SAR' in b)
s, b = post(fin, '/finance/rates', {'currency': 'USD', 'rate': '3.76'})
check('finance can update a rate', 'FX rates saved' in b)
q2 = q("SELECT rate_to_sar FROM currency_rates WHERE currency='USD'")[0]
check('rate stored', abs(q2['rate_to_sar'] - 3.76) < 1e-9)
post(fin, '/finance/rates', {'currency': 'USD', 'rate': '3.75'})  # restore

# ── 6. Supplier API still exempt from CSRF ──
key = q("SELECT api_key FROM suppliers WHERE api_key IS NOT NULL LIMIT 1")
if key:
    payload = json.dumps({'api_key': key[0]['api_key'], 'items': []}).encode()
    req = urllib.request.Request(BASE + '/api/supplier-prices', data=payload,
                                 headers={'Content-Type': 'application/json'})
    resp = json.loads(urllib.request.urlopen(req).read())
    check('supplier API works without CSRF (key-authenticated)', resp.get('ok') is True)
else:
    check('supplier API key exists', False)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
