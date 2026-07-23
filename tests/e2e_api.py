"""E2E test for Integration API v1: auth, catalogue, wallet, orders with
idempotency, unified codes, fulfillment adapter demo, outbound webhooks."""
import urllib.request, urllib.parse, urllib.error, http.cookiejar as cj
import re as _re
import json, sqlite3, os, uuid, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = 'http://127.0.0.1:8000'
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'onecard.db')
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

def post_form(op, path, data):
    data = dict(data)
    data.setdefault('_csrf', getattr(op, '_csrf', ''))
    r = op.open(BASE + path, data=urllib.parse.urlencode(data).encode())
    return r.status, r.read().decode('utf-8', 'replace')

def api(path, key=None, body=None, headers=None):
    h = {'Content-Type': 'application/json'}
    if key:
        h['X-API-Key'] = key
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}

def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, a)]
    c.close()
    return rows

# ── Webhook receiver (local) ──
received = []
class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        ln = int(self.headers.get('Content-Length', 0))
        received.append({'event': self.headers.get('X-OneCard-Event'),
                         'body': json.loads(self.rfile.read(ln) or b'{}')})
        self.send_response(200); self.end_headers()
    def log_message(self, *a):
        pass

server = HTTPServer(('127.0.0.1', 9797), Hook)
threading.Thread(target=server.serve_forever, daemon=True).start()

# ── 0. Sales generates key + webhook for the demo reseller ──
sales = login('sales@onecard.com', 'Sales2025!')
rid = q("""SELECT cp.id FROM reseller_profiles cp JOIN users u ON cp.user_id=u.id
           WHERE u.email='khalid@alnoor-digital.com'""")[0]['id']
s, b = post_form(sales, f'/sales/resellers/{rid}/api', {'action': 'rotate'})
m = _re.search(r'(rk_[0-9a-f]+)', b)
check('key generated via UI', bool(m))
KEY = m.group(1) if m else ''
s, b = post_form(sales, f'/sales/resellers/{rid}/api',
                 {'action': 'webhook', 'webhook_url': 'http://127.0.0.1:9797/hook'})
check('webhook URL saved', 'Webhook URL saved' in b)

# ── 1. Auth ──
s, r = api('/api/v1/ping')
check('no key -> 401', s == 401 and r['error']['code'] == 'unauthorized')
s, r = api('/api/v1/ping', key='rk_wrong')
check('bad key -> 401', s == 401)
s, r = api('/api/v1/ping', key=KEY)
check('ping ok + identity', s == 200 and r['company'] == 'Al Noor Digital Trading')

# Bearer form works too
req = urllib.request.Request(BASE + '/api/v1/ping',
                             headers={'Authorization': f'Bearer {KEY}'})
check('Bearer auth accepted', json.loads(urllib.request.urlopen(req).read())['ok'] is True)

# ── 2. Catalogue ──
s, r = api('/api/v1/catalogue?page_size=50', key=KEY)
check('catalogue paginated', s == 200 and len(r['items']) == 50 and r['total'] > 2000,
      f"total={r.get('total')}")
first = r['items'][0]
check('item shape (sku, your_price, fulfillment fields)',
      all(k in first for k in ('sku', 'your_price', 'face_value', 'special_rate')))
s, r = api('/api/v1/catalogue?merchant=Chef%20Burger%20KSA', key=KEY)
check('merchant filter', s == 200 and r['total'] >= 1 and
      all(i['merchant'] == 'Chef Burger KSA' for i in r['items']))
s, r = api('/api/v1/catalogue?search=nexon', key=KEY)
check('search filter', s == 200 and r['total'] >= 1)

# ── 3. Wallet ──
s, r = api('/api/v1/wallet', key=KEY)
check('wallet balance SAR', s == 200 and r['currency'] == 'SAR' and 'balance' in r)
balance_before = r['balance']

# ── 4. Order with idempotency (issued product -> codes delivered) ──
issued = q("SELECT id FROM products WHERE is_issued=1 LIMIT 1")[0]['id']
idem = f'e2e-{uuid.uuid4().hex[:10]}'
body = {'idempotency_key': idem, 'items': [{'id': issued, 'quantity': 2}]}
s, r = api('/api/v1/orders', key=KEY, body=body)
check('order created 201', s == 201 and r['ok'], str(r)[:80])
oid = r.get('order_id')
line = r['items'][0]
check('issued line delivered with codes', line['fulfillment_status'] == 'delivered'
      and len(line['codes']) == 2 and line['codes'][0]['pin'])
# replay
s2, r2 = api('/api/v1/orders', key=KEY, body=body)
check('idempotent replay same order', r2.get('idempotent_replay') is True
      and r2['order_id'] == oid)
orders_count = q("SELECT COUNT(*) as n FROM orders WHERE reseller_id=?", rid)[0]['n']
s3, r3 = api('/api/v1/wallet', key=KEY)
check('wallet charged once', abs((balance_before - r3['balance']) - r['total_sar']) < 0.01,
      f"delta={balance_before - r3['balance']:.2f} vs {r['total_sar']}")

# ── 5. Fulfillment adapter demo (Nexon EU Store -> EXT- codes) ──
nexon = q("""SELECT id FROM products WHERE merchant='Nexon EU Store'
             AND is_active=1 LIMIT 1""")
if nexon:
    s, r = api('/api/v1/orders', key=KEY,
               body={'items': [{'id': nexon[0]['id'], 'quantity': 2}]})
    ln = r['items'][0]
    check('adapter fulfilled external product', s == 201 and
          ln['fulfillment_status'] == 'delivered' and
          ln['codes'] and ln['codes'][0]['code'].startswith('EXT-'),
          str(ln.get('codes'))[:60])
else:
    check('nexon demo product exists', False)

# normal product without adapter stays external
normal = q("""SELECT id FROM products WHERE COALESCE(is_issued,0)=0 AND is_active=1
              AND merchant NOT IN ('Nexon EU Store') AND default_price BETWEEN 5 AND 50
              LIMIT 1""")[0]['id']
s, r = api('/api/v1/orders', key=KEY, body={'items': [{'id': normal, 'quantity': 1}]})
check('no-adapter line stays external', s == 201 and
      r['items'][0]['fulfillment_status'] == 'external' and r['items'][0]['codes'] == [])

# ── 6. Errors ──
s, r = api('/api/v1/orders', key=KEY, body={'items': [{'id': 99999999, 'quantity': 1}]})
check('unknown product 422', s == 422 and r['error']['code'] == 'unknown_product')
s, r = api('/api/v1/orders', key=KEY,
           body={'items': [{'id': issued, 'quantity': 9999999}]})
check('oversell 409 insufficient_stock', s == 409 and r['error']['code'] == 'insufficient_stock')
s, r = api('/api/v1/orders', key=KEY, body={'items': [{'id': normal, 'quantity': 10000000}]})
check('balance 409 insufficient_balance', s == 409 and r['error']['code'] == 'insufficient_balance')

# ── 7. Detail + codes endpoints ──
s, r = api(f'/api/v1/orders/{oid}', key=KEY)
check('order detail', s == 200 and r['order_id'] == oid)
s, r = api(f'/api/v1/orders/{oid}/codes', key=KEY)
check('codes endpoint', s == 200 and len(r['codes']) == 2)
s, r = api('/api/v1/orders/999999', key=KEY)
check('foreign order 404', s == 404)

# ── 8. Webhooks received ──
# v16: delivery is now a durable async queue drained by a background worker.
# Flush it in-process so the test is deterministic (the server runs with the
# worker disabled under ONECARD_NO_WEBHOOK_WORKER=1).
import time, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import models
for _ in range(3):
    models.deliver_due_webhooks()
    time.sleep(0.2)
check('webhooks delivered', len(received) >= 3, f"got {len(received)}")
if received:
    ev = next((x for x in received if x['event'] == 'order.placed'), received[0])
    check('webhook shape', ev['event'] == 'order.placed' and 'order_id' in ev['body']['data'])
wl = q("SELECT COUNT(*) as n FROM webhook_deliveries WHERE status_code=200")[0]['n']
check('webhook deliveries logged', wl >= 3, f"logged={wl}")

server.shutdown()
print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"===== {passed}/{len(results)} PASSED =====")
for name, ok, extra in results:
    if not ok:
        print('  FAILED:', name, extra)
