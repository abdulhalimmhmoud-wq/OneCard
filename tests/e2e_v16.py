"""E2E test for v16 (Phase 4): production webhook hardening (durable queue,
HMAC signatures, retry/backoff) + credit reporting (aging buckets + CSV export).

Spins up a local HTTP receiver to verify real signed delivery."""
import urllib.request, urllib.parse, urllib.error, http.cookiejar as cj
import re as _re
import json, sqlite3, os, sys, uuid, hmac, hashlib, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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


def login(email, pw):
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj.CookieJar()))
    page = op.open(BASE + '/login').read().decode('utf-8', 'replace')
    m = _re.search(r'name="csrf-token" content="([^"]+)"', page)
    op._csrf = m.group(1) if m else ''
    op.open(BASE + '/login', data=urllib.parse.urlencode(
        {'email': email, 'password': pw, '_csrf': op._csrf}).encode())
    return op


# ── local webhook receiver ──
received = []
RESP_CODE = [200]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        # HTTP header names are case-insensitive — normalise to lower for lookup.
        received.append({'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
        self.send_response(RESP_CODE[0]); self.end_headers()

    def log_message(self, *a):
        pass


srv = HTTPServer(('127.0.0.1', 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
HOOK = f"http://127.0.0.1:{port}/hook"

# ── set up a reseller with a webhook ──
sales = login('sales@onecard.com', 'Sales2025!')
em = f"v16_{uuid.uuid4().hex[:8]}@test.com"
sales.open(BASE + '/sales/register', data=urllib.parse.urlencode({
    'company_name': 'V16 Hook Co ' + em[4:12], 'contact_name': 'H', 'contact_email': em,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8],
    'password': 'Test123!', 'expected_sales': '80000', 'client_types': 'Retail Chain',
    'countries': 'Saudi Arabia', '_csrf': sales._csrf}).encode())
rid = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em)[0]['id']

# ── 1. setting a webhook URL mints a signing secret ──
models.set_reseller_api(rid, webhook_url=HOOK)
secret = q("SELECT webhook_secret FROM reseller_profiles WHERE id=?", rid)[0]['webhook_secret']
check('setting a webhook URL mints a signing secret', bool(secret) and secret.startswith('whsec_'))

# ── 2. enqueue + deliver, verify the receiver got a signed request ──
RESP_CODE[0] = 200
did = models.enqueue_webhook(rid, 'order.placed', {'order_id': 999, 'total_sar': 1264.0})
check('enqueue creates a pending delivery',
      q("SELECT status FROM webhook_deliveries WHERE id=?", did)[0]['status'] == 'pending')
received.clear()
d, f, r = models.deliver_due_webhooks()
row = q("SELECT * FROM webhook_deliveries WHERE id=?", did)[0]
check('delivery marks the row delivered', row['status'] == 'delivered' and row['status_code'] == 200)
check('the receiver actually got the POST', len(received) >= 1)
got = received[-1]
H = got['headers']   # keys already lower-cased (HTTP headers are case-insensitive)
check('event + delivery headers are present',
      H.get('x-onecard-event') == 'order.placed'
      and H.get('x-onecard-delivery') == str(did)
      and H.get('x-onecard-timestamp'))

# ── 3. the HMAC signature verifies with the shared secret ──
ts = H.get('x-onecard-timestamp')
expected = 'sha256=' + hmac.new(secret.encode(), f"{ts}.{got['body']}".encode(), hashlib.sha256).hexdigest()
check('X-OneCard-Signature is a valid HMAC-SHA256',
      hmac.compare_digest(expected, H.get('x-onecard-signature', '')))
check('payload body carries the event + data',
      json.loads(got['body'])['event'] == 'order.placed'
      and json.loads(got['body'])['data']['order_id'] == 999)

# ── 4. a failing endpoint is retried with backoff, not dropped ──
RESP_CODE[0] = 500
did2 = models.enqueue_webhook(rid, 'statement.issued', {'statement_id': 1, 'amount_sar': 100})
models.deliver_due_webhooks()
row2 = q("SELECT * FROM webhook_deliveries WHERE id=?", did2)[0]
check('a 5xx keeps the event pending for retry', row2['status'] == 'pending' and row2['attempts'] == 1)
check('a future retry is scheduled (backoff)', bool(row2['next_attempt_at']))

# ── 5. after max attempts it is marked failed ──
execu("UPDATE webhook_deliveries SET attempts=?, next_attempt_at='2000-01-01T00:00:00+00:00' WHERE id=?",
      models.WEBHOOK_MAX_ATTEMPTS - 1, did2)
models.deliver_due_webhooks()
row2 = q("SELECT status, attempts FROM webhook_deliveries WHERE id=?", did2)[0]
check('gives up after WEBHOOK_MAX_ATTEMPTS -> failed',
      row2['status'] == 'failed' and row2['attempts'] == models.WEBHOOK_MAX_ATTEMPTS)

# ── 6. no webhook URL -> nothing enqueued ──
em2 = f"v16b_{uuid.uuid4().hex[:8]}@test.com"
sales.open(BASE + '/sales/register', data=urllib.parse.urlencode({
    'company_name': 'V16 NoHook ' + em2[5:13], 'contact_name': 'N', 'contact_email': em2,
    'contact_phone': '05' + str(uuid.uuid4().int)[:8],
    'password': 'Test123!', 'expected_sales': '50000', 'client_types': 'Retail Chain',
    'countries': 'Saudi Arabia', '_csrf': sales._csrf}).encode())
rid2 = q("SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id WHERE u.email=?", em2)[0]['id']
check('enqueue for a reseller with no webhook is a no-op',
      models.enqueue_webhook(rid2, 'order.placed', {}) is None)

# ── 7. credit aging buckets ──
execu("UPDATE reseller_profiles SET account_type='credit', credit_limit=100000 WHERE id=?", rid)
today = models.datetime.now(models.timezone.utc).date()
def due(days):  # negative = overdue by N days
    from datetime import timedelta
    return (today - timedelta(days=days)).isoformat()
for amt, d, st in [(1000, -10, 'issued'), (2000, 10, 'overdue'), (3000, 45, 'overdue'),
                   (4000, 75, 'overdue'), (5000, 120, 'overdue')]:
    execu("""INSERT INTO statements (reseller_id, amount, status, due_at) VALUES (?,?,?,?)""",
          rid, amt, st, due(d))
aging = models.get_credit_aging()
check('aging: not-yet-due bucket', aging['not_due'] >= 1000)
check('aging: 1-30 / 31-60 / 61-90 / 90+ buckets',
      aging['d1_30'] >= 2000 and aging['d31_60'] >= 3000 and aging['d61_90'] >= 4000 and aging['d90p'] >= 5000,
      f"{aging}")

# ── 8. Finance CSV export ──
fin = login('finance@onecard.com', 'Finance2025!')
r = fin.open(BASE + '/finance/credit/export.csv')
ctype = r.headers.get('Content-Type', '')
body = r.read().decode()
check('CSV export is text/csv', 'text/csv' in ctype)
check('CSV export lists the credit account with headers',
      'Reseller,Account Type' in body and 'V16 Hook Co' in body)

# ── cleanup ──
for email in (em, em2):
    urows = q("SELECT id FROM users WHERE email=?", email)
    if not urows:
        continue
    uid = urows[0]['id']
    for pr in q("SELECT id FROM reseller_profiles WHERE user_id=?", uid):
        r_ = pr['id']
        execu("DELETE FROM statements WHERE reseller_id=?", r_)
        execu("DELETE FROM webhook_deliveries WHERE reseller_id=?", r_)
        execu("DELETE FROM reseller_countries WHERE reseller_id=?", r_)
        execu("DELETE FROM reseller_client_types WHERE reseller_id=?", r_)
        execu("DELETE FROM reseller_profiles WHERE id=?", r_)
    execu("DELETE FROM notifications WHERE user_id=?", uid)
    execu("DELETE FROM users WHERE id=?", uid)
srv.shutdown()

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
