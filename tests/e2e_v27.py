"""E2E test for v27: Forecast Intelligence for Operations.
Resellers now attach a light timing hint to each forecast line (when they need it,
one-off vs recurring, confidence). Ops gets a console that buckets demand by time,
detects spikes / brand-new demand / single-client concentration, flags coverage
shortfalls, and drills into each customer's forecast with fulfilment tracking.
The account manager can refine a line's timing. Old forecasts stay valid."""
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


def post(op, path, data):
    data = dict(data); data['_csrf'] = op._csrf
    r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
    return r.status, r.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


tag = uuid.uuid4().hex[:8]

# ── backward-compat: existing forecast lines were backfilled to a monthly baseline ──
old = q("SELECT period, confidence FROM forecast_items ORDER BY id LIMIT 1")
if old:
    check("existing (pre-v27) forecast lines default to monthly/medium",
          old[0]['period'] == 'monthly' and old[0]['confidence'] == 'medium')

# ── a fresh product with a unique merchant and NO stock (baseline sold=0, on_hand=0) ──
merch = f"V27 Merchant {tag}"
execu("""INSERT INTO products (product_id, product_name, merchant, category, country, region,
                               currency, cost, default_price, face_value, is_active)
         VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
      f"V27-{tag}", f"V27 Card {tag}", merch, "Games", "Global", "MENA", "SAR", 80, 100, 100)
pid = q("SELECT id FROM products WHERE product_name=?", f"V27 Card {tag}")[0]['id']

# ── register a NEW reseller (0 orders → forecast is risk-discounted) ──
sales = login('sales@onecard.com', 'Sales2025!')
em = f"v27_{tag}@test.com"
sales.open(BASE + '/sales/register', data=urllib.parse.urlencode({
    'company_name': f'V27 Co {tag}', 'contact_name': 'T', 'contact_email': em,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8], 'password': 'Test123!',
    'expected_sales': '60000', 'client_types': 'Retail Chain', 'countries': 'Saudi Arabia',
    '_csrf': sales._csrf}).encode())
rid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]['id']
uid = q("SELECT id FROM users WHERE email=?", em)[0]['id']
execu("UPDATE reseller_profiles SET nda_accepted_at=CURRENT_TIMESTAMP, is_suspended=0 WHERE id=?", rid)

# ── the reseller submits a forecast WITH timing via the portal ──
res = login(em, 'Test123!')
plan = [
    {'type': 'product', 'product_id': pid, 'quantity': 500,
     'when': 'this_week', 'period': 'one_off', 'confidence': 'high'},
    {'type': 'merchant', 'merchant': merch, 'value': 50000,
     'when': 'this_month', 'period': 'monthly', 'confidence': 'medium'},
]
post(res, '/reseller/forecast', {'items_json': json.dumps(plan), 'note': 'v27 timing test'})
fid = q("SELECT id FROM forecasts WHERE reseller_id=? ORDER BY id DESC LIMIT 1", rid)[0]['id']
lines = q("SELECT * FROM forecast_items WHERE forecast_id=? ORDER BY item_type", fid)  # merchant, product
by_type = {l['item_type']: l for l in lines}

check("timing captured: product line is one-off, high confidence, with a needed-by date",
      by_type.get('product') and by_type['product']['period'] == 'one_off'
      and by_type['product']['confidence'] == 'high' and bool(by_type['product']['needed_by']))
check("timing captured: merchant line is a recurring monthly baseline",
      by_type.get('merchant') and by_type['merchant']['period'] == 'monthly')

# ── forecast data: buckets, planned-vs-recent, planned-vs-stock (numbers only) ──
fi = models.get_forecast_intelligence()
mrow = next((m for m in fi['by_merchant'] if m['merchant'] == merch), None)
prow = next((p for p in fi['by_product'] if p['product_rowid'] == pid), None)

check("one-off product lands in the 'Next 7 days' bucket", fi['buckets']['week']['value'] > 0)
check("recurring merchant line lands in the 'Recurring monthly' bucket", fi['buckets']['recurring']['value'] > 0)
check("merchant row shows planned next to recent sales (no recent sales -> no ratio)",
      mrow is not None and mrow['planned'] > 0 and mrow['baseline'] == 0 and mrow['ratio'] is None)
check("product row shows the RAW planned quantity (500) — no weighting/judgement",
      prow is not None and prow['planned_qty'] == 500, str(prow and prow['planned_qty']))
check("stock gap is plain arithmetic: planned 500 - on hand 0 = 500",
      prow is not None and prow['on_hand'] == 0 and prow['gap'] == 500)
check("the data carries NO verdicts/alerts list (informational only)", 'signals' not in fi)

# the forecast shows up in the per-customer register with the right tier
regrow = next((r for r in fi['register'] if r['fid'] == fid), None)
check("forecast appears in the register tagged as a New client",
      regrow is not None and regrow['tier'] == 'new')

# ── Ops page renders as calm data tables — no alarm wall, no command language ──
ops = login('ops@onecard.com', 'Ops2025!')
s, b = get(ops, '/ops/forecasts')
check("Ops page renders the Forecasted Demand tables and lists the merchant",
      s == 200 and 'Forecasted Demand' in b and merch in b)
check("Ops page dropped the alarm wall + 'verify/act' command language",
      'Demand Signals' not in b and 'verify before committing' not in b and 'verify & act' not in b)
s2, b2 = get(ops, f'/ops/forecasts/{fid}')
check("Ops forecast detail shows timing + fulfilment (ordered-since)",
      s2 == 200 and 'Ordered since' in b2 and f'V27 Card {tag}' in b2 and 'one-off' in b2)

# ── the account manager refines a line's timing from the sales detail page ──
pl_id = by_type['product']['id']
post(sales, f'/sales/forecasts/{fid}', {'item_id': pl_id, 'needed_by': '2026-09-15', 'confidence': 'low'})
refined = q("SELECT needed_by, confidence FROM forecast_items WHERE id=?", pl_id)[0]
check("account manager can refine a line's needed-by date + confidence",
      refined['needed_by'] == '2026-09-15' and refined['confidence'] == 'low')

# ── cleanup ──
execu("DELETE FROM forecast_items WHERE forecast_id=?", fid)
execu("DELETE FROM forecasts WHERE id=?", fid)
execu("DELETE FROM products WHERE id=?", pid)
execu("DELETE FROM reseller_countries WHERE reseller_id=?", rid)
execu("DELETE FROM reseller_client_types WHERE reseller_id=?", rid)
execu("DELETE FROM reseller_profiles WHERE id=?", rid)
execu("DELETE FROM notifications WHERE user_id=?", uid)
execu("DELETE FROM users WHERE id=?", uid)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
