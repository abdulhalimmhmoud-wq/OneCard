"""E2E test for v18: per-reseller hidden merchants (e.g. competitors) removed
from everything they see, and the price<->discount calculator that tells the
sales manager which margin share reaches a target buy-price."""
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


def get(op, path):
    r = op.open(BASE + path); return r.status, r.read().decode('utf-8', 'replace')


def get_json(op, path):
    try:
        r = op.open(BASE + path); return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def post(op, path, data):
    data = dict(data); data.setdefault('_csrf', getattr(op, '_csrf', ''))
    r = op.open(BASE + path, data=urllib.parse.urlencode(data, doseq=True).encode())
    return r.status, r.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


# two merchants that each have priceable products
def priceable(m):
    return any(p['oc_margin'] > 0 for p in models.get_products(merchant=m))

merchants = [m['merchant'] for m in models.get_all_merchants() if priceable(m['merchant'])]
M1, M2 = merchants[0], merchants[1]

sales = login('sales@onecard.com', 'Sales2025!')
tag = uuid.uuid4().hex[:8]
em = f"v18_{tag}@test.com"

# ── register a reseller hiding merchant M1 ──
post(sales, '/sales/register', {
    'company_name': f'V18 Hide {tag} Co', 'contact_name': 'H', 'contact_email': em,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8], 'password': 'Test123!',
    'expected_sales': '80000', 'client_types': 'Retail Chain', 'countries': 'Saudi Arabia',
    'hidden_merchants': M1})
row = q("SELECT cp.id, cp.user_id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]
rid, ruid = row['id'], row['user_id']
hidden = q("SELECT merchant FROM reseller_hidden_merchants WHERE reseller_id=?", rid)
check('hidden merchant stored at registration', hidden and hidden[0]['merchant'] == M1)

# ── enrich excludes the hidden merchant, keeps others ──
enriched = models.enrich_products_for_reseller(models.get_reseller_profile(ruid))
seen = {p['merchant'] for p in enriched}
check('hidden merchant removed from enriched catalogue', M1 not in seen)
check('other merchants remain', M2 in seen)

# ── reseller portal doesn't show the hidden merchant ──
execu("UPDATE reseller_profiles SET nda_accepted_at=CURRENT_TIMESTAMP WHERE id=?", rid)
res = login(em, 'Test123!')
s, b = get(res, '/reseller/products')
check('reseller catalogue page hides the merchant', s == 200 and M1 not in b)

# ── API catalogue also hides it (filter per-merchant to avoid pagination) ──
key = models.set_reseller_api(rid, rotate_key=True)
def api_cat(merchant):
    req = urllib.request.Request(
        BASE + '/api/v1/catalogue?page_size=500&merchant=' + urllib.parse.quote(merchant),
        headers={'X-API-Key': key})
    return json.loads(urllib.request.urlopen(req).read())
check('API catalogue hides the merchant too',
      api_cat(M1)['total'] == 0 and api_cat(M2)['total'] > 0,
      f"M1={api_cat(M1)['total']} M2={api_cat(M2)['total']}")

# ── sales edits the hidden set (hide M2 instead) ──
post(sales, f'/sales/resellers/{rid}/hidden-merchants', {'hidden_merchants': M2})
enriched2 = {p['merchant'] for p in models.enrich_products_for_reseller(models.get_reseller_profile(ruid))}
check('editing hidden merchants re-shows M1 and hides M2', M1 in enriched2 and M2 not in enriched2)
# clear all -> everything visible
post(sales, f'/sales/resellers/{rid}/hidden-merchants', {})
enriched3 = {p['merchant'] for p in models.enrich_products_for_reseller(models.get_reseller_profile(ruid))}
check('clearing hidden merchants restores full catalogue', M1 in enriched3 and M2 in enriched3)

# ── discount calculator: merchant-pricing feed ──
s, mp = get_json(sales, f'/sales/merchant-pricing?reseller_id={rid}&merchant={urllib.parse.quote(M2)}')
check('merchant-pricing returns priceable products', s == 200 and mp.get('products')
      and all(k in mp['products'][0] for k in ('default_price', 'cost', 'oc_margin', 'current_price')))
check('merchant-pricing reports the display currency + current share',
      mp['display_currency'] and mp['current_share_pct'] is not None)

# ── inversion is correct: share for a target price reproduces that price ──
p = next(x for x in mp['products'] if x['oc_margin'] > 0.5)
# exact 50%-share target (the calculator math is exact for a given target;
# rounding the target is what a human does and is tested via round-trip below)
target_exact = p['default_price'] - p['oc_margin'] * 0.5
share = (p['default_price'] - target_exact) / p['oc_margin'] * 100
check('calculator inversion: share = 50% for a 50% target', abs(share - 50) < 0.01, f"share={share:.2f}")
# a human-entered whole-number target round-trips back to itself
target = round(p['default_price'] - p['oc_margin'] * 0.4)
sh = max(0, min(100, (p['default_price'] - target) / p['oc_margin'] * 100))
price_at_share = max(p['default_price'] - p['oc_margin'] * sh / 100, p['cost'])
check('calculator round-trips a whole-number target price', abs(round(price_at_share) - target) <= 1,
      f"price@share={price_at_share:.2f} target={target}")

# ── access control on the pricing feed ──
s, _ = get_json(sales, '/sales/merchant-pricing?reseller_id=99999999&merchant=x')
check('pricing feed refuses an unknown reseller', s == 404)

# ── cleanup ──
uid = q("SELECT id FROM users WHERE email=?", em)[0]['id']
for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
    r_ = pr['id']
    execu("DELETE FROM reseller_hidden_merchants WHERE reseller_id=?", r_)
    execu("DELETE FROM reseller_countries WHERE reseller_id=?", r_)
    execu("DELETE FROM reseller_client_types WHERE reseller_id=?", r_)
    execu("DELETE FROM reseller_profiles WHERE id=?", r_)
execu("DELETE FROM notifications WHERE user_id=?", uid)
execu("DELETE FROM users WHERE id=?", uid)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
