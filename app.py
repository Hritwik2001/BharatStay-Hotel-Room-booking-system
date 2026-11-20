"""BharatStay - Hotel Booking Management System (Python + MySQL)
Setup:  pip install -r requirements.txt   ->  set MYSQL_PASSWORD below or as env var  ->  python app.py
Open :  http://localhost:8000      Admin password: admin123
Your hotels.sql is the single source of truth: this file only reads and executes it."""
import csv, hmac, io, json, os, re, secrets
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import mysql.connector

CFG = dict(
    host=os.getenv('MYSQL_HOST', 'localhost'),
    port=int(os.getenv('MYSQL_PORT', '3306')),
    user=os.getenv('MYSQL_USER', 'root'),
    password=os.getenv('MYSQL_PASSWORD', 'hri123')
)  # <- your MySQL password
APP_DB, DEMO_DB = 'bharatstay_app', 'bharatstay_sql_demo'   # separate from the 'hotels' DB your script creates
ADMIN_PASSWORD = 'admin123'
BASE = os.path.dirname(os.path.abspath(__file__))
SQL_TEXT = open(os.path.join(BASE, 'hotels.sql'), encoding='utf8').read()
COLS = ['full_name', 'phone', 'email', 'city', 'id_proof']
TOKENS = set()          # kept in memory only -> every server restart / page reload needs a new login
TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.png': 'image/png'}

# ---------- run hotels.sql (handles DELIMITER, '-- ## n. title' sections) ----------
def parse_script(text):
    out, delim, buf, sec = [], ';', [], '1. Create tables and TRIGGERS'
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'--\s*##\s*(\d+\..*)', s)
        if m: sec = m.group(1).strip(); continue
        if s.upper().startswith('DELIMITER'): delim = s.split()[1]; continue
        if s.startswith('-- ==='): continue
        buf.append(line)
        if not s.startswith('--') and s.endswith(delim):
            out.append((sec, '\n'.join(buf).strip()[:-len(delim)].rstrip())); buf = []
    return out

def code(sql): return '\n'.join(l for l in sql.splitlines() if not l.strip().startswith('--')).strip()
SKIP = re.compile(r'^(DROP DATABASE|CREATE DATABASE|USE)\b', re.I)

def run_script(conn, keep=None):
    """Execute statements; returns [{section, sql, sets:[{cols,rows}], affected, error}] for the HR page."""
    out, cur = [], conn.cursor()
    for sec, sql in parse_script(SQL_TEXT):
        c = code(sql)
        if (keep and sec.split('.')[0] not in keep) or not c or SKIP.match(c): continue
        item = {'section': sec, 'sql': sql, 'sets': []}
        try:
            call = re.match(r'CALL\s+(\w+)', c, re.I)
            if call:
                cur.callproc(call.group(1))
                for rs in cur.stored_results():
                    item['sets'].append({'cols': list(rs.column_names), 'rows': [list(r) for r in rs.fetchall()]})
            else:
                cur.execute(sql)
                if cur.with_rows: item['sets'].append({'cols': list(cur.column_names), 'rows': [list(r) for r in cur.fetchall()]})
                else: item['affected'] = cur.rowcount
        except mysql.connector.Error as e:
            item['error'] = e.msg
        out.append(item)
    cur.close()
    return out

def server():
    c = mysql.connector.connect(**CFG); c.autocommit = True; return c

def fresh(dbname, keep=None):
    c = server(); cur = c.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS {dbname}'); cur.execute(f'CREATE DATABASE {dbname}'); cur.execute(f'USE {dbname}'); cur.close()
    return c, run_script(c, keep)

def ensure_app_db():     # first run only: schema + triggers + data + views (sections 1, 2, 8, 13)
    c = server(); cur = c.cursor()
    cur.execute(f'CREATE DATABASE IF NOT EXISTS {APP_DB}'); cur.execute(f'USE {APP_DB}'); cur.execute("SHOW TABLES LIKE 'hotels'")
    if not cur.fetchall(): run_script(c, keep={'1', '2', '8', '13'})
    c.close()

def db():
    c = mysql.connector.connect(database=APP_DB, **CFG); c.autocommit = True; return c

def q(c, sql, a=()):
    cur = c.cursor(dictionary=True); cur.execute(sql, a); r = cur.fetchall(); cur.close(); return r

def js(o):
    if isinstance(o, Decimal): return float(o)
    return str(o)

# ---------- business logic ----------
def book(c, d):
    ci, co = d['check_in'], d['check_out']
    nights = (date.fromisoformat(co) - date.fromisoformat(ci)).days
    if nights <= 0: raise ValueError('Check-out must be after check-in')
    cur = c.cursor(dictionary=True); cur.execute('START TRANSACTION')
    try:
        cur.execute("""SELECT r.room_id, h.base_rate*t.multiplier AS rate FROM rooms r
            JOIN hotels h ON h.hotel_id=r.hotel_id JOIN room_types t ON t.type_id=r.type_id
            WHERE r.hotel_id=%s AND r.type_id=%s AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.room_id=r.room_id
            AND b.status='Booked' AND %s<b.check_out AND %s>b.check_in) LIMIT 1""", (d['hotel_id'], d['type_id'], ci, co))
        r = cur.fetchone()
        if not r: raise ValueError('No room of this type is free for these dates')
        extra = 0
        if d.get('car_id'):
            cur.execute('SELECT pickup_tariff+drop_tariff AS t FROM v_car_tariff WHERE hotel_id=%s AND car_id=%s', (d['hotel_id'], d['car_id']))
            extra = float(cur.fetchone()['t'])
        total = round(nights * float(r['rate']) + extra)
        cur.execute('INSERT INTO bookings(guest_id,room_id,check_in,check_out,total_amount) VALUES (%s,%s,%s,%s,%s)', (d['guest_id'], r['room_id'], ci, co, total))
        bid, adv = cur.lastrowid, round(total * 0.3)
        cur.execute('INSERT INTO payments(booking_id,amount,method) VALUES (%s,%s,%s)', (bid, adv, d.get('method', 'UPI')))
        cur.execute('COMMIT')
        return {'booking_id': bid, 'total': total, 'advance_paid': adv}
    except Exception:
        cur.execute('ROLLBACK'); raise

def archive_guest(g):   # deleted guests are appended to deleted_guests.csv
    path = os.path.join(BASE, 'deleted_guests.csv'); new = not os.path.exists(path)
    with open(path, 'a', newline='', encoding='utf8') as f:
        w = csv.writer(f)
        if new: w.writerow(COLS + ['deleted_at'])
        w.writerow([g[k] for k in COLS] + [datetime.now().isoformat(' ', timespec='seconds')])

def to_csv(c, sql):
    cur = c.cursor(); cur.execute(sql); buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(cur.column_names); w.writerows(cur.fetchall()); cur.close(); return buf.getvalue()

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def out(s, body, code=200, ct='application/json'):
        if not isinstance(body, (bytes, str)): body = json.dumps(body, default=js)
        body = body.encode() if isinstance(body, str) else body
        s.send_response(code); s.send_header('Content-Type', ct); s.send_header('Content-Length', str(len(body))); s.end_headers(); s.wfile.write(body)

    def jbody(s): return json.loads(s.rfile.read(int(s.headers.get('Content-Length', 0))) or b'{}')

    def go(s, m):
        p = urlparse(s.path).path
        if m == 'GET' and not p.startswith('/api/'):               # static files (public: login page needs them)
            f = os.path.normpath(os.path.join(BASE, 'index.html' if p == '/' else p.lstrip('/')))
            if f.startswith(BASE) and os.path.isfile(f) and not f.endswith(('.py', '.sql')):
                return s.out(open(f, 'rb').read(), ct=TYPES.get(os.path.splitext(f)[1], 'application/octet-stream'))
            return s.out({'error': 'not found'}, 404)
        if m == 'POST' and p == '/api/login':
            if hmac.compare_digest(str(s.jbody().get('password', '')), ADMIN_PASSWORD):
                t = secrets.token_hex(16); TOKENS.add(t); return s.out({'token': t})
            return s.out({'error': 'Wrong password'}, 401)
        if s.headers.get('X-Token') not in TOKENS: return s.out({'error': 'Admin login required'}, 401)
        if p == '/api/logout': TOKENS.discard(s.headers.get('X-Token')); return s.out({'ok': True})
        c, n = None, re.findall(r'\d+', p)
        try:
            if m == 'GET' and p == '/api/sql-demo':                  # whole hotels.sql on a scratch DB -> HR page
                dc, res = fresh(DEMO_DB); dc.close(); return s.out(res)
            c = db()
            if m == 'GET' and p == '/api/hotels': return s.out(q(c, 'SELECT * FROM hotels'))
            if m == 'GET' and p.startswith('/api/hotels/'):
                a = (n[0],)
                return s.out({'hotel': q(c, 'SELECT * FROM hotels WHERE hotel_id=%s', a)[0],
                  'rooms': q(c, """SELECT t.type_id, t.type_name, ROUND(h.base_rate*t.multiplier) AS tariff,
                     (SELECT COUNT(*) FROM rooms r WHERE r.hotel_id=h.hotel_id AND r.type_id=t.type_id) AS total_rooms
                     FROM hotels h CROSS JOIN room_types t WHERE h.hotel_id=%s""", a),
                  'sights': q(c, 'SELECT place, price_per_person FROM sightseeing WHERE hotel_id=%s', a),
                  'cars': q(c, 'SELECT car_id, car_name, seats, pickup_tariff, drop_tariff FROM v_car_tariff WHERE hotel_id=%s', a)})
            if p == '/api/guests' and m == 'GET':
                return s.out(q(c, "SELECT g.*, (SELECT COUNT(*) FROM bookings b WHERE b.guest_id=g.guest_id AND b.status='Booked') AS active FROM guests g ORDER BY guest_id DESC"))
            if p == '/api/guests' and m == 'POST':
                d = s.jbody(); cur = c.cursor()
                cur.execute('INSERT INTO guests(full_name,phone,email,city,id_proof) VALUES (%s,%s,%s,%s,%s)', [d.get(k, '') for k in COLS])
                return s.out({'ok': True})
            if m == 'DELETE' and p.startswith('/api/guests/'):
                g = q(c, 'SELECT * FROM guests WHERE guest_id=%s', (n[0],))[0]
                c.cursor().execute('DELETE FROM guests WHERE guest_id=%s', (n[0],))   # trg_guest_guard blocks active guests
                archive_guest(g); return s.out({'ok': True})
            if m == 'GET' and p == '/api/bookings': return s.out(q(c, 'SELECT * FROM v_booking_list ORDER BY booking_id DESC'))
            if m == 'POST' and p == '/api/book': return s.out(book(c, s.jbody()))
            if m == 'POST' and p.startswith('/api/checkout/'):
                c.cursor().execute("UPDATE bookings SET status='CheckedOut' WHERE booking_id=%s", (n[0],)); return s.out({'ok': True})
            if m == 'GET' and p == '/api/export/bookings.csv': return s.out(to_csv(c, 'SELECT * FROM v_booking_list'), ct='text/csv')
            if m == 'GET' and p == '/api/export/guests.csv': return s.out(to_csv(c, 'SELECT * FROM guests'), ct='text/csv')
            if m == 'GET' and p == '/api/export/deleted.csv':
                f = os.path.join(BASE, 'deleted_guests.csv')
                return s.out(open(f, encoding='utf8').read() if os.path.exists(f) else ','.join(COLS + ['deleted_at']) + '\n', ct='text/csv')
            if m == 'POST' and p == '/api/import/guests':
                rows = list(csv.DictReader(io.StringIO(s.rfile.read(int(s.headers['Content-Length'])).decode('utf-8-sig')))); cur = c.cursor(); added = 0
                for r in rows:
                    cur.execute('INSERT IGNORE INTO guests(full_name,phone,email,city,id_proof) VALUES (%s,%s,%s,%s,%s)', [r.get(k, '') for k in COLS]); added += cur.rowcount
                return s.out({'added': added})
            return s.out({'error': 'not found'}, 404)
        except mysql.connector.Error as e: s.out({'error': e.msg}, 409)
        except (ValueError, KeyError, IndexError) as e: s.out({'error': str(e) or 'bad request'}, 409)
        finally:
            if c: c.close()

    def do_GET(s): s.go('GET')
    def do_POST(s): s.go('POST')
    def do_DELETE(s): s.go('DELETE')
    def log_message(s, *a): pass

if __name__ == '__main__':
    ensure_app_db()
    print('BharatStay running -> http://localhost:8000   (admin password: admin123)')
    ThreadingHTTPServer(('', 8000), H).serve_forever()
