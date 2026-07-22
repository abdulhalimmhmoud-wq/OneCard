"""E2E test for v10 hardening: encryption at rest, wallet/voucher-stock race
fixes (real concurrent threads), redeem attempt limiter, session cookie
policy, receipt content sniffing, database backups.

Concurrency note: the dev server runs single-threaded (app.run() has no
threaded=True), so two HTTP requests can't genuinely overlap at the server.
The race tests below call models.create_order() directly from Python
threads instead — each gets its own sqlite3 connection via models.get_db(),
exactly like two simultaneous Flask requests would, which is the actual
condition BEGIN IMMEDIATE has to survive.
"""
import urllib.request, urllib.parse, urllib.error, http.cookiejar as cj
import re as _re
import sqlite3, os, sys, threading, uuid

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


def post(op, path, data):
    data = dict(data)
    data.setdefault('_csrf', getattr(op, '_csrf', ''))
    try:
        r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def post_multipart(op, path, fields, file_field, filename, content_type, content_bytes):
    boundary = '----hardening2boundary'
    parts = ''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                    for k, v in {**fields, '_csrf': getattr(op, '_csrf', '')}.items())
    body = (parts.encode() +
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'.encode() + content_bytes +
            f'\r\n--{boundary}--\r\n'.encode())
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        r = op.open(req)
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]
    c.close()
    return rows


CODE_RE = _re.compile(r'^[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$')

# ══════════════════ 1. Encryption at rest ══════════════════
row = q("SELECT code, pin, code_hash FROM issued_vouchers LIMIT 1")
check('a voucher exists to inspect', bool(row))
if row:
    row = row[0]
    check('stored code is NOT the plaintext XXXX-XXXX-XXXX-XXXX format',
          not CODE_RE.match(row['code']))
    dec_code = models._dec(row['code'])
    check('decrypted code matches the expected plaintext format', bool(CODE_RE.match(dec_code)))
    check('stored pin is NOT a raw 6-digit number',
          not (row['pin'].isdigit() and len(row['pin']) == 6))
    dec_pin = models._dec(row['pin'])
    check('decrypted pin is a 6-digit number', dec_pin.isdigit() and len(dec_pin) == 6)
    check('code_hash is a sha256 hex digest', len(row['code_hash']) == 64
          and all(c in '0123456789abcdef' for c in row['code_hash']))
    check('code_hash matches hash of the decrypted code', row['code_hash'] == models._code_hash(dec_code))

ext_row = q("SELECT code FROM external_codes LIMIT 1")
if ext_row:
    check('stored external code is NOT the plaintext EXT- format',
          not ext_row[0]['code'].startswith('EXT-'))
    check('decrypted external code has the expected EXT- prefix',
          models._dec(ext_row[0]['code']).startswith('EXT-'))

# Prep: a contracted reseller profile + its enriched (tier-priced) catalogue,
# used by both race tests below.
uid = q("SELECT id FROM users WHERE email='khalid@alnoor-digital.com'")[0]['id']
profile = models.get_reseller_profile(uid)
enriched = models.enrich_products_for_reseller(profile)

# ══════════════════ 2. Wallet overdraft race ══════════════════
normal = next(p for p in enriched if not p.get('is_issued')
             and p['merchant'] != 'Nexon EU Store' and 5 <= p['client_price'] <= 500)
qty = 5
rate = models.get_fx_rates().get(normal['currency'], 1.0)
order_cost_sar = round(normal['client_price'] * qty * rate, 2)
# Enough for exactly ONE of two identical concurrent orders, not both.
c = sqlite3.connect(DB)
c.execute("UPDATE reseller_profiles SET wallet_balance=? WHERE id=?",
         (round(order_cost_sar * 1.3, 2), profile['id']))
c.commit(); c.close()

wallet_race = {}
barrier1 = threading.Barrier(2)


def wallet_worker(idx):
    barrier1.wait()   # force both threads into create_order() at the same instant
    items = [{'product_rowid': normal['id'], 'product_name': normal['product_name'],
              'merchant': normal['merchant'], 'category': normal['category'],
              'currency': normal['currency'], 'quantity': qty,
              'unit_price': normal['client_price'], 'unit_face': normal['face_value']}]
    wallet_race[idx] = models.create_order(profile['id'], items)


t1, t2 = threading.Thread(target=wallet_worker, args=(1,)), threading.Thread(target=wallet_worker, args=(2,))
t1.start(); t2.start(); t1.join(); t2.join()

succeeded = [k for k, v in wallet_race.items() if v[0] is not None]
failed = [k for k, v in wallet_race.items() if v[0] is None]
check('exactly one of two concurrent orders succeeded (wallet race fixed)',
      len(succeeded) == 1, str(wallet_race))
check('the other was cleanly rejected as insufficient balance',
      len(failed) == 1 and 'Insufficient wallet balance' in (wallet_race[failed[0]][1] or ''),
      str(wallet_race))
final_balance = q("SELECT wallet_balance FROM reseller_profiles WHERE id=?", profile['id'])[0]['wallet_balance']
check('final wallet balance never went negative', final_balance >= -0.001, f"balance={final_balance}")

# ══════════════════ 3. Gift-card stock oversell race ══════════════════
issued_id = q("SELECT id FROM products WHERE is_issued=1 LIMIT 1")[0]['id']
ops_uid = q("SELECT id FROM users WHERE email='ops@onecard.com'")[0]['id']
avail_before = q("SELECT COUNT(*) as n FROM issued_vouchers WHERE product_rowid=? AND status='available'",
                 issued_id)[0]['n']
if avail_before < 10:
    models.generate_voucher_batch(issued_id, 10 - avail_before, ops_uid, 'hardening2 race topup')
    avail_before = q("SELECT COUNT(*) as n FROM issued_vouchers WHERE product_rowid=? AND status='available'",
                     issued_id)[0]['n']

c = sqlite3.connect(DB)
c.execute("UPDATE reseller_profiles SET wallet_balance=999999 WHERE id=?", (profile['id'],))
c.commit(); c.close()

enriched2 = models.enrich_products_for_reseller(profile)
issued_p = next(p for p in enriched2 if p['id'] == issued_id)
qty_each = avail_before - 4   # two requests of qty_each together exceed avail_before

stock_race = {}
barrier2 = threading.Barrier(2)


def stock_worker(idx):
    barrier2.wait()
    items = [{'product_rowid': issued_p['id'], 'product_name': issued_p['product_name'],
              'merchant': issued_p['merchant'], 'category': issued_p['category'],
              'currency': issued_p['currency'], 'quantity': qty_each,
              'unit_price': issued_p['client_price'], 'unit_face': issued_p['face_value']}]
    stock_race[idx] = models.create_order(profile['id'], items)


t3, t4 = threading.Thread(target=stock_worker, args=(1,)), threading.Thread(target=stock_worker, args=(2,))
t3.start(); t4.start(); t3.join(); t4.join()

succeeded2 = [k for k, v in stock_race.items() if v[0] is not None]
failed2 = [k for k, v in stock_race.items() if v[0] is None]
check('exactly one of two concurrent gift-card orders succeeded (stock race fixed)',
      len(succeeded2) == 1, f"avail_before={avail_before} qty_each={qty_each} {stock_race}")
check('the other was cleanly rejected as insufficient stock',
      len(failed2) == 1 and 'codes left' in (stock_race[failed2[0]][1] or ''), str(stock_race))
remaining = q("SELECT COUNT(*) as n FROM issued_vouchers WHERE product_rowid=? AND status='available'",
             issued_id)[0]['n']
check('remaining stock is exactly avail_before - qty_each (no double-spend, no under-delivery)',
      remaining == avail_before - qty_each, f"remaining={remaining} expected={avail_before - qty_each}")

# ══════════════════ 4. Redeem attempt limiter ══════════════════
one_item = [{'product_rowid': issued_p['id'], 'product_name': issued_p['product_name'],
            'merchant': issued_p['merchant'], 'category': issued_p['category'],
            'currency': issued_p['currency'], 'quantity': 1,
            'unit_price': issued_p['client_price'], 'unit_face': issued_p['face_value']}]
oid3, err3 = models.create_order(profile['id'], one_item)
check('setup order for redeem-limiter test succeeded', oid3 is not None, str(err3))
if oid3:
    codes3 = models.get_order_codes(oid3)
    test_code = next(iter(codes3.values()))[0]['code']

    partner = login('portal@chefburger.sa', 'Partner2025!')
    last_status, last_body, tries = None, '', 0
    for tries in range(1, 13):
        last_status, last_body = post(partner, '/partner/redeem',
                                      {'code': test_code, 'pin': '000000', 'action': 'redeem'})
        if last_status == 429:
            break
    check('repeated wrong-PIN attempts eventually get rate-limited (429)',
          last_status == 429, f"stopped after {tries} tries, last_status={last_status}")
    check('rate-limit message shown', 'Too many attempts' in last_body)

# ══════════════════ 5. Session cookie policy ══════════════════
jar = cj.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
page = op.open(BASE + '/login').read().decode('utf-8', 'replace')
tok = _re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
op._csrf = tok
r = op.open(BASE + '/login', data=urllib.parse.urlencode(
    {'email': 'cco@onecard.com', 'password': 'Cco2025!', '_csrf': tok}).encode())
set_cookie = '; '.join(r.headers.get_all('Set-Cookie') or [])
check('login response sets a session cookie', 'session=' in set_cookie)
check('session cookie is HttpOnly', 'HttpOnly' in set_cookie)
check('session cookie is SameSite=Lax', 'samesite=lax' in set_cookie.lower())

# ══════════════════ 6. Receipt content sniffing ══════════════════
res = login('khalid@alnoor-digital.com', 'Demo123!')

s, b = post_multipart(res, '/reseller/wallet',
                      {'amount': '1000', 'bank_reference': 'HARD2-FAKE'}, 'receipt',
                      'fake.png', 'image/png', b'this is plain text, not a real png at all')
check('fake content with a .png filename is rejected', 'finance team will verify' not in b)

png_bytes = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
s, b = post_multipart(res, '/reseller/wallet',
                      {'amount': '1000', 'bank_reference': 'HARD2-REAL'}, 'receipt',
                      'anything.txt', 'text/plain', png_bytes)
check('genuine PNG bytes are accepted regardless of filename/claimed content-type',
      'finance team will verify' in b)

# ══════════════════ 7. Database backups ══════════════════
admin = login('admin@onecard.com', 'OneCard2025!')
before = len(models.list_backups())
s, b = post(admin, '/admin/backup/run', {})
check('admin-triggered backup succeeds', 'Backup created' in b)
backups = models.list_backups()
check('a backup file exists after triggering', len(backups) >= 1)
check('backup file has real content (nonzero size)', bool(backups) and backups[0]['size_kb'] > 0)
check('backup count did not decrease', len(backups) >= before)

# ══════════════════ Restore shared fixtures ══════════════════
# The stock-race test above deliberately drains the shared demo issued
# product down to a handful of codes to prove the fix — other suites
# (e2e_api, e2e_ops_governance, ...) also draw a couple of units from the
# same singleton product and assume there's headroom. Top it back up and
# reset the demo wallet to a clean round number so this file stays
# re-runnable, and so run order across the whole tests/ directory doesn't
# matter.
models.generate_voucher_batch(issued_id, 500, ops_uid, 'hardening2 restore after race test')
c = sqlite3.connect(DB)
c.execute("UPDATE reseller_profiles SET wallet_balance=500000 WHERE id=?", (profile['id'],))
c.commit(); c.close()

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
