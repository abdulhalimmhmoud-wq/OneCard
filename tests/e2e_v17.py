"""E2E test for v17 (CRM): contact phone at registration, duplicate-customer
prevention (email / company / phone / commercial-reg-no), commercial reg number
at contract time, and the CCO Customers board (filters + dashboard)."""
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


def register(op, company, email, phone, **extra):
    data = {'company_name': company, 'contact_name': 'Rep', 'contact_email': email,
            'contact_phone': phone, 'password': 'Test123!', 'expected_sales': '60000',
            'client_types': 'Retail Chain', 'countries': 'Saudi Arabia', '_csrf': op._csrf}
    data.update(extra)
    r = op.open(BASE + '/sales/register', data=urllib.parse.urlencode(data).encode())
    return r.read().decode('utf-8', 'replace')


def post_multipart(op, path, fields):
    b = '----v17b' + uuid.uuid4().hex
    body = b''
    fields = dict(fields); fields.setdefault('_csrf', getattr(op, '_csrf', ''))
    for k, v in fields.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="draft"; filename="c.pdf"\r\n'
             f'Content-Type: application/pdf\r\n\r\n').encode() + b'%PDF-1.4 test\n\r\n' + f'--{b}--\r\n'.encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    return op.open(req).read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]; c.close(); return rows


def execu(sql, *a):
    c = sqlite3.connect(DB); c.execute(sql, a); c.commit(); c.close()


def exists(email):
    return bool(q("SELECT id FROM users WHERE email=?", email))


sales = login('sales@onecard.com', 'Sales2025!')
tag = uuid.uuid4().hex[:6]
_ph9 = str(uuid.uuid4().int)[:9]   # unique 9-digit base so we never collide with residue
A_co, A_em, A_ph = f"CRM Alpha {tag} Co", f"crm_a_{tag}@test.com", "+966" + _ph9

# ── 1. phone required ──
b = register(sales, f"CRM NoPhone {tag}", f"crm_np_{tag}@test.com", "")
check('registration without a phone is blocked', not exists(f"crm_np_{tag}@test.com")
      and 'phone number' in b.lower())

# ── 2. a clean registration stores the phone ──
register(sales, A_co, A_em, A_ph)
arow = q("SELECT cp.contact_phone FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", A_em)
check('clean registration succeeds + stores phone', arow and arow[0]['contact_phone'] == A_ph, str(arow))

# ── 3. duplicate by company name is blocked ──
b = register(sales, A_co.upper(), f"crm_dupco_{tag}@test.com", "0509998877")
check('duplicate company name is blocked', not exists(f"crm_dupco_{tag}@test.com")
      and 'already registered' in b.lower() and 'company name' in b.lower())

# ── 4. duplicate by phone (different format) is blocked ──
b = register(sales, f"CRM Different {tag} Co", f"crm_dupph_{tag}@test.com", "0" + _ph9)  # same digits as A_ph
check('duplicate phone (normalised) is blocked', not exists(f"crm_dupph_{tag}@test.com")
      and 'phone number' in b.lower())

# ── 5. duplicate by email is blocked ──
b = register(sales, f"CRM Another {tag} Co", A_em, "0505556677")
check('duplicate email is blocked', 'already registered' in b.lower())

# ── 6. a genuinely different customer is allowed ──
E_em = f"crm_e_{tag}@test.com"
register(sales, f"CRM Echo {tag} Co", E_em, "0533334444")
check('a distinct customer is allowed', exists(E_em))

# ── 7. commercial reg number at contract time ──
A_rid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", A_em)[0]['id']
CR = f"1010{tag}"
post_multipart(sales, f"/sales/resellers/{A_rid}/contract/upload",
               {'account_type': 'prepaid', 'commercial_reg_no': CR})
prof = q("SELECT commercial_reg_no FROM reseller_profiles WHERE id=?", A_rid)[0]
check('contract captures the commercial reg number on the profile', prof['commercial_reg_no'] == CR)
ctr = q("SELECT commercial_reg_no FROM contracts WHERE reseller_id=? ORDER BY id DESC LIMIT 1", A_rid)[0]
check('contract row records the commercial reg number', ctr['commercial_reg_no'] == CR)

# ── 8. duplicate commercial reg number is blocked at contract time ──
E_rid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", E_em)[0]['id']
b = post_multipart(sales, f"/sales/resellers/{E_rid}/contract/upload",
                   {'account_type': 'prepaid', 'commercial_reg_no': CR})
check('duplicate commercial reg number is blocked',
      q("SELECT commercial_reg_no FROM reseller_profiles WHERE id=?", E_rid)[0]['commercial_reg_no'] != CR)

# ── 9. CCO Customers board + filters ──
cco = login('cco@onecard.com', 'Cco2025!')
s, b = get(cco, '/cco/customers')
check('CCO customers board renders with the dashboard', s == 200 and 'Customers (filtered)' in b)
check('board lists customers across the book', A_co in b and f"CRM Echo {tag} Co" in b)
# search filter
s, b = get(cco, f'/cco/customers?q=alpha+{tag}')
check('search filter narrows the board', A_co in b and f"CRM Echo {tag} Co" not in b)
# account-type filter (all our test customers are prepaid)
s, b = get(cco, '/cco/customers?account_type=consignment')
check('account-type filter excludes prepaid test customers', A_co not in b)
# stage filter (prospects)
s, b = get(cco, '/cco/customers?stage=prospect')
check('stage filter works', 'Customers (filtered)' in b)

# ── cleanup ──
for em in (A_em, E_em):
    urows = q("SELECT id FROM users WHERE email=?", em)
    if not urows:
        continue
    uid = urows[0]['id']
    for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
        rid = pr['id']
        execu("DELETE FROM contract_events WHERE contract_id IN (SELECT id FROM contracts WHERE reseller_id=?)", rid)
        execu("DELETE FROM contracts WHERE reseller_id=?", rid)
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
