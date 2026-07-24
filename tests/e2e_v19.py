"""E2E test for v19: budget-based onboarding forecast, competitor-price intel
(Sales -> BD with attachment), and the NDA confidentiality gate on first login."""
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


def opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj.CookieJar()))


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
        r = op.open(BASE + path); return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def post(op, path, data):
    data = dict(data); data.setdefault('_csrf', getattr(op, '_csrf', ''))
    r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
    return r.status, r.read().decode('utf-8', 'replace')


def post_file(op, path, fields, field='attachment', filename='shot.png'):
    b = '----v19b' + uuid.uuid4().hex
    png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 20
    body = b''
    fields = dict(fields); fields.setdefault('_csrf', getattr(op, '_csrf', ''))
    for k, v in fields.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n').encode() + png + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    return op.open(req).read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


sales = login('sales@onecard.com', 'Sales2025!')
tag = uuid.uuid4().hex[:8]
em = f"v19_{tag}@test.com"
post(sales, '/sales/register', {
    'company_name': f'V19 New Client {tag}', 'contact_name': 'N', 'contact_email': em,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8], 'password': 'Test123!',
    'expected_sales': '50000', 'client_types': 'Retail Chain', 'countries': 'Saudi Arabia'})
row = q("SELECT cp.id, cp.user_id, cp.nda_accepted_at FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]
rid, ruid = row['id'], row['user_id']

# ── 1. NDA gate ──
check('a new reseller starts with NDA pending', row['nda_accepted_at'] is None)
res = login(em, 'Test123!')
s, b = get(res, '/reseller')
check('portal is gated behind the NDA until accepted', 'Confidentiality Notice' in b)
s, b = get(res, '/reseller/products')
check('every reseller page redirects to the NDA', 'Confidentiality Notice' in b)
# accepting without ticking the box is rejected
s, b = post(res, '/reseller/accept-nda', {'agree': ''})
check('NDA acceptance requires ticking the box',
      q("SELECT nda_accepted_at FROM reseller_profiles WHERE id=?", rid)[0]['nda_accepted_at'] is None)
# accept properly
s, b = post(res, '/reseller/accept-nda', {'agree': 'yes'})
check('accepting the NDA records a timestamp',
      q("SELECT nda_accepted_at FROM reseller_profiles WHERE id=?", rid)[0]['nda_accepted_at'] is not None)
s, b = get(res, '/reseller')
check('after acceptance the portal opens', 'Confidentiality Notice' not in b and s == 200)

# ── 2. budget-based onboarding forecast ──
s, b = post(res, '/reseller/forecast/budget', {'amount': '100000', 'note': 'just starting'})
bf = q("""SELECT fi.* FROM forecast_items fi JOIN forecasts f ON fi.forecast_id=f.id
          WHERE f.reseller_id=? AND fi.merchant=?""", rid, models.BUDGET_MERCHANT)
check('budget forecast is created as a starting-budget line', len(bf) == 1 and abs(bf[0]['est_value'] - 100000) < 1)
check('sales is notified of the starting budget',
      q("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND title LIKE '%starting budget%'",
        q("SELECT registered_by FROM reseller_profiles WHERE id=?", rid)[0]['registered_by'])[0]['n'] >= 1)
s, b = get(sales, '/sales/forecasts')
check('the budget forecast shows up for sales', f'V19 New Client {tag}' in b)

# ── 3. competitor intel -> BD ──
bd_uid = q("SELECT id FROM users WHERE email='bd@onecard.com'")[0]['id']
before = q("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND title LIKE 'Competitor pricing intel%'", bd_uid)[0]['n']
merch = models.get_all_merchants()[0]['merchant']
post_file(sales, '/sales/competitor-intel',
          {'merchant': merch, 'product_name': 'Some Card', 'competitor_name': 'RivalCo',
           'competitor_price': '20', 'our_price': '22.5', 'note': 'client mentioned this'})
intel = q("SELECT * FROM competitor_intel WHERE submitted_by=(SELECT id FROM users WHERE email='sales@onecard.com') ORDER BY id DESC LIMIT 1")
check('competitor intel is recorded with the attachment',
      intel and intel[0]['merchant'] == merch and intel[0]['attachment_file']
      and abs(intel[0]['competitor_price'] - 20) < 0.01)
iid = intel[0]['id']
after = q("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND title LIKE 'Competitor pricing intel%'", bd_uid)[0]['n']
check('BD is notified of new competitor intel', after > before)

bd = login('bd@onecard.com', 'Bd2025!')
s, b = get(bd, '/bd/intel')
check('BD inbox lists the intel', s == 200 and 'RivalCo' in b and merch in b)
# attachment access control
s, _ = get(bd, f'/intel/{iid}/file')
check('BD can open the source attachment', s == 200)
s, _ = get(res, f'/intel/{iid}/file')   # a reseller is not allowed
check('an unrelated user cannot open the attachment', s == 403)
# BD updates status
post(bd, f'/bd/intel/{iid}/status', {'status': 'actioned', 'bd_note': 'renegotiating sourcing'})
check('BD can action the intel + notify sales',
      q("SELECT status FROM competitor_intel WHERE id=?", iid)[0]['status'] == 'actioned'
      and q("""SELECT COUNT(*) n FROM notifications nt JOIN users u ON nt.user_id=u.id
               WHERE u.email='sales@onecard.com' AND nt.title LIKE '%competitor intel%'""")[0]['n'] >= 1)

# ── cleanup ──
uid = q("SELECT id FROM users WHERE email=?", em)[0]['id']
for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
    r_ = pr['id']
    execu("DELETE FROM forecast_items WHERE forecast_id IN (SELECT id FROM forecasts WHERE reseller_id=?)", r_)
    execu("DELETE FROM forecasts WHERE reseller_id=?", r_)
    execu("DELETE FROM reseller_countries WHERE reseller_id=?", r_)
    execu("DELETE FROM reseller_client_types WHERE reseller_id=?", r_)
    execu("DELETE FROM reseller_profiles WHERE id=?", r_)
execu("DELETE FROM notifications WHERE user_id=?", uid)
execu("DELETE FROM users WHERE id=?", uid)
execu("DELETE FROM competitor_intel WHERE id=?", iid)
execu("DELETE FROM notifications WHERE title LIKE 'Competitor pricing intel%'")
execu("DELETE FROM notifications WHERE title LIKE '%starting budget%' OR title LIKE '%competitor intel%'")

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
