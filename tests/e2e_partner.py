"""E2E test for v8.1: Issuing Partner Portal (dashboard, redeem station, statement)."""
import urllib.request, urllib.parse, urllib.error, http.cookiejar as cj
import re as _re
import sqlite3, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import models   # noqa: E402 — codes/PINs are encrypted at rest (v10); use
                # models._dec()/_code_hash() instead of the raw DB columns.

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
    try:
        r = op.open(BASE + path)
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def post(op, path, data):
    data = dict(data)
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

# ── 1. Partner login + isolation ──
p = login('portal@chefburger.sa', 'Partner2025!')
s, b = get(p, '/')
check('partner lands on their dashboard', 'Chef Burger KSA' in b and 'Your share' in b)
check('dashboard shows earnings', 'Your Earnings So Far' in b)
s, b = get(p, '/admin')
check('partner blocked from admin', 'Admin Dashboard' not in b)
s, b = get(p, '/reseller/products')
check('partner blocked from reseller catalogue', 'Master Product Catalogue' not in b)
s, b = get(p, '/ops/issuing')
check('partner blocked from ops issuing', 'Partners & Overview' not in b)

# ── 2. Statement ──
s, b = get(p, '/partner/statement')
check('statement page with monthly rows', s == 200 and 'Monthly Breakdown' in b)
check('statement shows share pct', '80' in b)

# ── 3. Redeem station ──
sold = q("""SELECT v.code, v.pin FROM issued_vouchers v
            JOIN products pr ON v.product_rowid=pr.id
            JOIN issuing_partners ip ON pr.issuing_partner_id=ip.id
            WHERE ip.name='Chef Burger KSA' AND v.status='sold' LIMIT 1""")
check('a sold card exists to test with', bool(sold))
if sold:
    # codes/PINs are encrypted at rest (v10) — decrypt for the HTTP calls,
    # and look status up by code_hash since the raw `code` column is now
    # ciphertext (different every read) and can't be matched with `=`.
    code, pin = models._dec(sold[0]['code']), models._dec(sold[0]['pin'])
    # wrong PIN rejected
    s, b = post(p, '/partner/redeem', {'code': code, 'pin': '000000', 'action': 'redeem'})
    check('wrong PIN rejected', 'Wrong PIN' in b)
    st = q("SELECT status FROM issued_vouchers WHERE code_hash=?", models._code_hash(code))[0]['status']
    check('card still sold after wrong PIN', st == 'sold')
    # check first shows value
    s, b = post(p, '/partner/redeem', {'code': code, 'pin': pin, 'action': 'check'})
    check('check shows valid + value', 'ready to redeem' in b)
    # correct redeem
    s, b = post(p, '/partner/redeem', {'code': code, 'pin': pin, 'action': 'redeem'})
    check('redeem succeeds with correct PIN', 'Redeemed successfully' in b)
    st = q("SELECT status FROM issued_vouchers WHERE code_hash=?", models._code_hash(code))[0]['status']
    check('DB shows redeemed', st == 'redeemed')
    # double redeem blocked
    s, b = post(p, '/partner/redeem', {'code': code, 'pin': pin, 'action': 'redeem'})
    check('double redeem blocked', 'Already used' in b)

# unsold card cannot be redeemed
avail = q("""SELECT v.code, v.pin FROM issued_vouchers v
             JOIN products pr ON v.product_rowid=pr.id
             JOIN issuing_partners ip ON pr.issuing_partner_id=ip.id
             WHERE ip.name='Chef Burger KSA' AND v.status='available' LIMIT 1""")
if avail:
    a_code, a_pin = models._dec(avail[0]['code']), models._dec(avail[0]['pin'])
    s, b = post(p, '/partner/redeem', {'code': a_code, 'pin': a_pin, 'action': 'redeem'})
    check('unsold card rejected', 'never sold' in b)

# ── 4. Ops-side login creation UI present ──
ops = login('ops@onecard.com', 'Ops2025!')
s, b = get(ops, '/ops/issuing')
check('ops sees portal login state', 'Portal login active' in b)
check('terminology fixed (Partner Brand)', 'Partner Brand Name' in b)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
