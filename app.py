# AgriBridge Flask Backend v5.0 — Production Hardened
# Deploy: push to github.com/zealmugumya-creator/agribridge-app
# Render auto-deploys on push to main branch

import os, re, json, time, random, string, hashlib, datetime, logging, requests, jwt
from functools import wraps
from collections import defaultdict
from flask import Flask, request, jsonify, Response, send_from_directory, g
from flask_cors import CORS

try:
    import africastalking
    AT_AVAILABLE = True
except ImportError:
    AT_AVAILABLE = False

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('agribridge')

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# ── Config ──────────────────────────────────────────────────────────────────
JWT_SECRET           = os.environ.get('JWT_SECRET', 'agribridge-change-me-2026')
ADMIN_PASSWORD       = os.environ.get('ADMIN_PASSWORD', 'admin2026')
SUPABASE_URL         = os.environ.get('SUPABASE_URL', 'https://vyrctsiyaihsysgpozdm.supabase.co')
SUPABASE_KEY         = os.environ.get('SUPABASE_KEY', '')        # service_role key
AT_USERNAME          = os.environ.get('AT_USERNAME', 'sandbox')
AT_API_KEY           = os.environ.get('AT_API_KEY', '')
AT_SMS_SENDER        = os.environ.get('AT_SMS_SENDER', 'AgriBridge')
GEMINI_KEY           = os.environ.get('GEMINI_API_KEY', '')
OTP_EXPIRY_MINS      = int(os.environ.get('OTP_EXPIRY_MINUTES', '10'))
MAX_ACCOUNTS_PER_IP  = int(os.environ.get('MAX_ACCOUNTS_PER_IP', '3'))
MAX_LOGIN_ATTEMPTS   = int(os.environ.get('MAX_LOGIN_ATTEMPTS', '5'))

at_sms = None
if AT_AVAILABLE and AT_API_KEY:
    try:
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        at_sms = africastalking.SMS()
        log.info("Africa's Talking ready")
    except Exception as e:
        log.warning(f"AT init failed: {e}")

# ── In-memory security state ─────────────────────────────────────────────────
_rate_buckets    = defaultdict(list)
_otp_store       = {}
_signup_ip_log   = defaultdict(list)
_login_fail_log  = defaultdict(list)
_blocked_ips     = {}
_fraud_flags     = []
_last_cleanup    = 0

def _now(): return time.time()

def _ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    return fwd.split(',')[0].strip() if fwd else (request.remote_addr or 'unknown')

def _rate_ok(key, max_req, window_sec):
    now, bucket = _now(), _rate_buckets[key]
    cutoff = now - window_sec
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_req:
        return False
    bucket.append(now)
    return True

def _is_blocked(ip):
    until = _blocked_ips.get(ip)
    if until and until > _now():
        return True
    if until:
        del _blocked_ips[ip]
    return False

def _block(ip, secs):
    _blocked_ips[ip] = _now() + secs
    log.warning(f"IP blocked: {ip} for {secs}s")

def _flag(event, details):
    entry = {'type': event, 'details': details, 'ip': _ip(),
             'ts': datetime.datetime.utcnow().isoformat()}
    _fraud_flags.append(entry)
    if len(_fraud_flags) > 500:
        _fraud_flags.pop(0)
    log.warning(f"FRAUD [{event}]: {details}")

def _cleanup():
    global _last_cleanup
    now = _now()
    if now - _last_cleanup < 300:
        return
    _last_cleanup = now
    expired = [k for k, v in _otp_store.items() if v['expires'] < now]
    for k in expired:
        del _otp_store[k]
    for key in list(_rate_buckets.keys()):
        _rate_buckets[key] = [t for t in _rate_buckets[key] if t > now - 3600]
        if not _rate_buckets[key]:
            del _rate_buckets[key]

def limit(max_req=10, window=60, block_after=None):
    def dec(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            ip = _ip()
            if _is_blocked(ip):
                return jsonify({'error': 'Too many requests. Your IP is temporarily blocked.'}), 429
            key = f"{fn.__name__}:{ip}"
            if not _rate_ok(key, max_req, window):
                _flag('rate_limit', {'route': fn.__name__})
                if block_after:
                    vkey = f"violations:{ip}"
                    _rate_buckets[vkey].append(_now())
                    if len([t for t in _rate_buckets[vkey] if t > _now()-3600]) >= block_after:
                        _block(ip, 1800)
                return jsonify({'error': 'Too many requests. Please slow down.'}), 429
            return fn(*a, **kw)
        return wrapped
    return dec

@app.before_request
def _before():
    g._start = _now()
    _cleanup()

@app.after_request
def _after(response):
    try:
        ms = round((_now() - g._start) * 1000, 1)
        log.info(f"{request.method} {request.path} {response.status_code} {ms}ms ip={_ip()}")
    except Exception:
        pass
    # Security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

# ── Validation helpers ────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
PHONE_RE = re.compile(r'^\+?\d{9,15}$')
DISPOSABLE = {'mailinator.com','guerrillamail.com','tempmail.com','10minutemail.com',
              'throwawaymail.com','yopmail.com','getnada.com','trashmail.com'}

def val_email(e):
    if not e or not isinstance(e, str): return False, 'Email required'
    e = e.strip().lower()
    if not EMAIL_RE.match(e): return False, 'Invalid email format'
    if e.split('@')[-1] in DISPOSABLE: return False, 'Disposable emails not allowed'
    return True, e

def val_phone(p):
    if not p: return True, None   # phone optional in some flows
    p = re.sub(r'[\s\-()]', '', str(p))
    if not PHONE_RE.match(p): return False, 'Invalid phone number'
    return True, p

def clean(v, max_len=1000):
    if v is None: return ''
    v = re.sub(r'<script[^>]*>.*?</script>', '', str(v), flags=re.I|re.S)
    v = re.sub(r'on\w+\s*=\s*["\'][^"\']*["\']', '', v, flags=re.I)
    v = re.sub(r'<[^>]+>', '', v)
    return v.strip()[:max_len]

def val_num(v, mn=None, mx=None, name='value'):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return False, f'{name} must be a number', None
    if mn is not None and n < mn: return False, f'{name} min {mn}', None
    if mx is not None and n > mx: return False, f'{name} max {mx}', None
    return True, None, n

# ── Supabase helpers ──────────────────────────────────────────────────────────
def _hdrs():
    k = SUPABASE_KEY
    return {'apikey': k, 'Authorization': f'Bearer {k}',
            'Content-Type': 'application/json', 'Prefer': 'return=representation'}

def db_get(table, params=None, limit=100):
    if not SUPABASE_KEY: return []
    try:
        p = {'limit': str(limit)}
        if params: p.update(params)
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", params=p, headers=_hdrs(), timeout=8)
        return r.json() if r.ok else []
    except Exception as e:
        log.error(f"db_get {table}: {e}")
        return []

def db_count(table, params=None):
    if not SUPABASE_KEY: return 0
    try:
        p = {'select': 'count'}
        if params: p.update(params)
        h = {**_hdrs(), 'Prefer': 'count=exact'}
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", params=p, headers=h, timeout=8)
        return int(r.headers.get('content-range', '0/0').split('/')[-1])
    except Exception: return 0

def db_insert(table, data):
    if not SUPABASE_KEY: return None
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", json=data, headers=_hdrs(), timeout=8)
        return r.json() if r.ok else None
    except Exception as e:
        log.error(f"db_insert {table}: {e}")
        return None

def db_update(table, data, col, val):
    if not SUPABASE_KEY: return False
    try:
        h = {**_hdrs(), 'Prefer': 'return=minimal'}
        r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}", json=data,
                           params={col: f'eq.{val}'}, headers=h, timeout=8)
        return r.status_code < 300
    except Exception as e:
        log.error(f"db_update {table}: {e}")
        return False

def _sms(phone, msg):
    if at_sms and phone:
        try:
            at_sms.send(message=msg, recipients=[str(phone)], sender_id=AT_SMS_SENDER)
            return True
        except Exception as e:
            log.error(f"SMS to {phone}: {e}")
    return False

def fmt(n):
    try: return f"{int(float(n)):,}"
    except: return str(n)

# ── JWT ───────────────────────────────────────────────────────────────────────
def make_token(payload, hours=24):
    payload.update({'iat': datetime.datetime.utcnow(),
                    'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=hours)})
    t = jwt.encode(payload, JWT_SECRET, algorithm='HS256')
    return t if isinstance(t, str) else t.decode()

def check_token(role=None):
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None, (jsonify({'error': 'Unauthorized'}), 401)
    try:
        p = jwt.decode(auth.split(' ', 1)[1], JWT_SECRET, algorithms=['HS256'])
        if role and p.get('role') != role:
            return None, (jsonify({'error': 'Forbidden'}), 403)
        return p, None
    except jwt.ExpiredSignatureError:
        return None, (jsonify({'error': 'Token expired'}), 401)
    except jwt.InvalidTokenError:
        return None, (jsonify({'error': 'Invalid token'}), 401)

# ── Static ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    try: return send_from_directory('static', path)
    except: return send_from_directory('static', 'index.html')

# ── Health ────────────────────────────────────────────────────────────────────
@app.route('/health')
@app.route('/api/health')
def health():
    supa_ok, latency = False, None
    if SUPABASE_KEY:
        try:
            t0 = _now()
            r = requests.get(f"{SUPABASE_URL}/rest/v1/", headers=_hdrs(), timeout=5)
            latency = round((_now() - t0) * 1000, 1)
            supa_ok = r.status_code < 500
        except Exception: pass
    status = 'ok' if supa_ok else 'degraded'
    return jsonify({
        'status': status, 'version': '5.0.0', 'service': 'AgriBridge API',
        'timestamp': datetime.datetime.utcnow().isoformat(),
        'supabase_connected': supa_ok, 'supabase_latency_ms': latency,
        'at_enabled': at_sms is not None, 'gemini_enabled': bool(GEMINI_KEY),
        'blocked_ips': len(_blocked_ips), 'fraud_flags': len(_fraud_flags),
    }), 200 if supa_ok else 503

# ── OTP ───────────────────────────────────────────────────────────────────────
def _otp_key(v): return hashlib.sha256(v.strip().lower().encode()).hexdigest()

@app.route('/api/otp/send', methods=['POST'])
@limit(max_req=3, window=300, block_after=10)
def otp_send():
    body = request.get_json(force=True, silent=True) or {}
    phone = clean(body.get('phone', ''), 20)
    ok, p = val_phone(phone)
    if not ok: return jsonify({'error': p}), 400
    phone = p or phone

    if not _rate_ok(f"otp_phone:{_otp_key(phone)}", 3, 600):
        return jsonify({'error': 'Too many OTP requests for this number.'}), 429

    code = ''.join(random.choices(string.digits, k=6))
    _otp_store[_otp_key(phone)] = {
        'code': code, 'expires': _now() + OTP_EXPIRY_MINS * 60,
        'attempts': 0, 'phone': phone
    }
    msg = f"AgriBridge code: {code}. Expires in {OTP_EXPIRY_MINS} min. Do not share."
    sent = _sms(phone, msg)
    if not sent:
        log.warning(f"[DEV] OTP for {phone}: {code}")
        return jsonify({'message': 'Code generated (SMS not configured)', 'dev_mode': True}), 200
    return jsonify({'message': f'Verification code sent to {phone}'}), 200

@app.route('/api/otp/verify', methods=['POST'])
@limit(max_req=10, window=300)
def otp_verify():
    body  = request.get_json(force=True, silent=True) or {}
    phone = clean(body.get('phone', ''), 20)
    code  = clean(body.get('code', ''), 10)
    key   = _otp_key(phone)
    entry = _otp_store.get(key)

    if not entry:
        return jsonify({'error': 'No code found. Please request a new one.'}), 400
    if _now() > entry['expires']:
        del _otp_store[key]
        return jsonify({'error': 'Code expired. Please request a new one.'}), 400

    entry['attempts'] += 1
    if entry['attempts'] > 5:
        del _otp_store[key]
        _flag('otp_brute_force', {'phone': phone})
        return jsonify({'error': 'Too many wrong attempts. Request a new code.'}), 429
    if entry['code'] != code:
        return jsonify({'error': 'Wrong code', 'attempts_remaining': 5 - entry['attempts']}), 400

    del _otp_store[key]
    token = make_token({'sub': phone, 'role': 'phone_verified'}, hours=1)
    return jsonify({'verified': True, 'token': token}), 200

# ── Signup abuse prevention ───────────────────────────────────────────────────
@app.route('/api/auth/check-signup-eligibility', methods=['POST'])
@limit(max_req=10, window=60)
def signup_eligibility():
    body  = request.get_json(force=True, silent=True) or {}
    email = clean(body.get('email', ''), 254)
    phone = clean(body.get('phone', ''), 20)
    ip    = _ip()

    ok, result = val_email(email)
    if not ok: return jsonify({'eligible': False, 'error': result}), 400
    email = result

    now, cutoff = _now(), _now() - 86400
    recent = [t for t in _signup_ip_log[ip] if t > cutoff]
    _signup_ip_log[ip] = recent
    if len(recent) >= MAX_ACCOUNTS_PER_IP:
        _flag('excessive_signups', {'ip': ip, 'count': len(recent)})
        return jsonify({'eligible': False,
                        'error': f'Max {MAX_ACCOUNTS_PER_IP} accounts per network per day. Contact hello@agribridge.ug'}), 429

    if phone:
        ok, p = val_phone(phone)
        if not ok: return jsonify({'eligible': False, 'error': p}), 400
        if p and SUPABASE_KEY:
            existing = db_get('farmers', {'phone': f'eq.{p}'}, limit=1)
            if existing:
                return jsonify({'eligible': False,
                                'error': 'This phone number is already registered. Try logging in.'}), 409

    return jsonify({'eligible': True}), 200

@app.route('/api/auth/record-signup', methods=['POST'])
@limit(max_req=10, window=60)
def record_signup():
    _signup_ip_log[_ip()].append(_now())
    return jsonify({'recorded': True}), 200

# ── Login protection ──────────────────────────────────────────────────────────
@app.route('/api/auth/check-login-allowed', methods=['POST'])
@limit(max_req=20, window=60)
def login_allowed():
    body  = request.get_json(force=True, silent=True) or {}
    email = clean(body.get('email', ''), 254).lower()
    ip    = _ip()
    now, window = _now(), 900

    for key in (f"lf:ip:{ip}", f"lf:em:{email}"):
        fails = [t for t in _login_fail_log[key] if t > now - window]
        _login_fail_log[key] = fails
        if len(fails) >= MAX_LOGIN_ATTEMPTS:
            retry = int(fails[0] + window - now)
            return jsonify({'allowed': False,
                            'error': f'Too many failed attempts. Retry in {max(1, retry//60)} minute(s).',
                            'retry_after_seconds': max(1, retry)}), 429
    return jsonify({'allowed': True}), 200

@app.route('/api/auth/report-login-failure', methods=['POST'])
@limit(max_req=20, window=60)
def login_failure():
    body  = request.get_json(force=True, silent=True) or {}
    email = clean(body.get('email', ''), 254).lower()
    ip    = _ip(); now = _now()
    _login_fail_log[f"lf:ip:{ip}"].append(now)
    _login_fail_log[f"lf:em:{email}"].append(now)
    ip_fails = len([t for t in _login_fail_log[f"lf:ip:{ip}"] if t > now - 900])
    if ip_fails >= MAX_LOGIN_ATTEMPTS * 3:
        _flag('brute_force', {'ip': ip, 'email': email, 'fails': ip_fails})
    return jsonify({'recorded': True}), 200

@app.route('/api/auth/report-login-success', methods=['POST'])
@limit(max_req=20, window=60)
def login_success():
    body  = request.get_json(force=True, silent=True) or {}
    email = clean(body.get('email', ''), 254).lower()
    ip    = _ip()
    _login_fail_log.pop(f"lf:ip:{ip}", None)
    _login_fail_log.pop(f"lf:em:{email}", None)
    return jsonify({'recorded': True}), 200

# ── Admin ─────────────────────────────────────────────────────────────────────
@app.route('/api/admin/login', methods=['POST'])
@limit(max_req=5, window=300, block_after=15)
def admin_login():
    data = request.get_json(force=True, silent=True) or {}
    if data.get('password', '') != ADMIN_PASSWORD:
        _flag('admin_fail', {'ip': _ip()})
        return jsonify({'error': 'Invalid password'}), 401
    return jsonify({'token': make_token({'sub': 'admin', 'role': 'admin'})}), 200

@app.route('/api/admin/verify')
def admin_verify():
    p, err = check_token('admin')
    return err if err else jsonify({'valid': True})

@app.route('/api/admin/stats')
def admin_stats():
    p, err = check_token('admin')
    if err: return err
    return jsonify({
        'farmers': db_count('farmers'), 'listings': db_count('listings'),
        'orders': db_count('orders'), 'animals': db_count('animal_listings'),
        'fraud_flags': len(_fraud_flags), 'blocked_ips': len(_blocked_ips),
    })

@app.route('/api/admin/fraud-flags')
def admin_fraud():
    p, err = check_token('admin')
    if err: return err
    return jsonify(_fraud_flags[-100:])

@app.route('/api/admin/orders')
def admin_orders():
    p, err = check_token('admin')
    if err: return err
    return jsonify(db_get('orders', {'order': 'created_at.desc'}, limit=200))

@app.route('/api/admin/farmers')
def admin_farmers():
    p, err = check_token('admin')
    if err: return err
    return jsonify(db_get('farmers', {'order': 'created_at.desc'}, limit=500))

@app.route('/api/admin/order/<oid>/status', methods=['PATCH'])
def admin_order_status(oid):
    p, err = check_token('admin')
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    status = clean(data.get('status', ''), 30)
    valid = ('pending', 'confirmed', 'in_transit', 'delivered', 'cancelled')
    if status not in valid:
        return jsonify({'error': f'Status must be one of: {", ".join(valid)}'}), 400
    ok = db_update('orders', {'status': status}, 'id', oid)
    return jsonify({'ok': ok}), 200 if ok else 500

# ── Public API ────────────────────────────────────────────────────────────────
@app.route('/api/prices')
@limit(max_req=60, window=60)
def prices():
    return jsonify(db_get('market_prices', {'order': 'price_date.desc'}, limit=100))

@app.route('/api/listings')
@limit(max_req=60, window=60)
def listings_get():
    return jsonify(db_get('listings', {'is_available': 'eq.true', 'order': 'created_at.desc'}, limit=100))

@app.route('/api/listings', methods=['POST'])
@limit(max_req=10, window=300, block_after=20)
def listings_post():
    body = request.get_json(force=True, silent=True) or {}
    crop = clean(body.get('crop_name', ''), 100)
    dist = clean(body.get('district', ''), 100)
    if not crop: return jsonify({'error': 'Crop name required'}), 400
    if not dist: return jsonify({'error': 'District required'}), 400
    ok, err, qty   = val_num(body.get('quantity_kg'), 0.01, 1e6, 'Quantity')
    if not ok: return jsonify({'error': err}), 400
    ok, err, price = val_num(body.get('price_per_kg'), 1, 1e7, 'Price')
    if not ok: return jsonify({'error': err}), 400
    phone = clean(body.get('farmer_phone', ''), 20)
    ok, p = val_phone(phone)
    if not ok: return jsonify({'error': p}), 400
    rec = {'crop_name': crop, 'district': dist, 'quantity_kg': qty, 'price_per_kg': price,
           'farmer_phone': p, 'description': clean(body.get('description', ''), 1000),
           'is_available': True, 'created_at': datetime.datetime.utcnow().isoformat()}
    result = db_insert('listings', rec)
    if result:
        if p: _sms(p, f"AgriBridge: Your {crop} listing is live!")
        return jsonify(result), 201
    return jsonify({'error': 'Could not create listing. Try again.'}), 503

@app.route('/api/orders', methods=['POST'])
@limit(max_req=10, window=300, block_after=20)
def orders_post():
    body = request.get_json(force=True, silent=True) or {}
    addr = clean(body.get('delivery_address', ''), 500)
    pay  = clean(body.get('payment_method', ''), 30)
    if not body.get('listing_id'): return jsonify({'error': 'Listing ID required'}), 400
    if not addr: return jsonify({'error': 'Delivery address required'}), 400
    if pay not in ('momo', 'mtn_momo', 'airtel', 'cash', 'bank'):
        return jsonify({'error': 'Invalid payment method'}), 400
    ok, err, qty = val_num(body.get('quantity_kg'), 0.01, 1e6, 'Quantity')
    if not ok: return jsonify({'error': err}), 400
    rec = {'listing_id': body['listing_id'], 'quantity_kg': qty, 'delivery_address': addr,
           'payment_method': pay, 'status': 'pending', 'payment_status': 'unpaid',
           'created_at': datetime.datetime.utcnow().isoformat()}
    result = db_insert('orders', rec)
    if result:
        return jsonify(result[0] if isinstance(result, list) else result), 201
    return jsonify({'error': 'Could not create order. Try again.'}), 503

@app.route('/api/contact', methods=['POST'])
@limit(max_req=5, window=600, block_after=10)
def contact():
    body = request.get_json(force=True, silent=True) or {}
    name = clean(body.get('full_name', ''), 100)
    ctct = clean(body.get('contact', ''), 200)
    msg  = clean(body.get('message', ''), 2000)
    if not all([name, ctct, msg]):
        return jsonify({'error': 'Name, contact, and message required'}), 400
    db_insert('contact_messages', {'full_name': name, 'contact': ctct, 'message': msg,
              'created_at': datetime.datetime.utcnow().isoformat()})
    _sms('+256755966690', f"Contact from {name} ({ctct}): {msg[:100]}")
    return jsonify({'message': 'Message received! We will reply within 24 hours.'}), 200

@app.route('/api/crop-doctor', methods=['POST'])
@limit(max_req=15, window=300)
def crop_doctor():
    body = request.get_json(force=True, silent=True) or {}
    desc = clean(body.get('description', ''), 1000)
    crop = clean(body.get('crop', 'crop'), 50)
    if not desc: return jsonify({'error': 'Please describe the problem'}), 400
    if not GEMINI_KEY:
        return jsonify({'diagnosis': 'Fungal or bacterial infection likely',
                        'treatment': 'Apply Mancozeb 80WP every 10-14 days.',
                        'prevention': 'Use certified seed. Crop rotation.',
                        'confidence': 60}), 200
    try:
        prompt = (f"Expert agronomist Uganda. Farmer's {crop} problem: \"{desc}\". "
                  f"Respond ONLY in valid JSON: diagnosis, treatment, prevention, confidence(0-100). "
                  f"Max 120 words each. Practical for Uganda smallholder farmers.")
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
        text = r.json()['candidates'][0]['content']['parts'][0]['text']
        text = text.strip().lstrip('```json').lstrip('```').rstrip('```').strip()
        return jsonify(json.loads(text))
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return jsonify({'error': 'AI temporarily unavailable'}), 503

# ── USSD ──────────────────────────────────────────────────────────────────────
CROP_P = {'1':('Maize',750,1000,'kg'),'2':('Matooke',950,1400,'bunch'),
          '3':('Tomatoes',1800,2500,'kg'),'4':('Coffee',12000,15000,'kg'),
          '5':('Beans',3400,4200,'kg'),'6':('Cassava',500,800,'kg'),
          '7':('Irish Potato',1200,1800,'kg')}
ANIM_P = {'1':('Cattle',2500000,3500000,'head'),'2':('Goats',280000,400000,'head'),
          '3':('Chickens',25000,45000,'bird'),'4':('Tilapia',12000,18000,'kg')}

def ussd(parts, phone, sid):
    m = parts[0] if parts else ''
    d = len(parts)
    l = parts[-1] if parts else ''
    if d == 0:
        return ("CON Welcome to AgriBridge *789#\n\n"
                "1. Crop Prices Today\n2. Animal Prices\n3. Marketplace\n"
                "4. List My Produce\n5. Farming Tips\n6. AI Doctor\n"
                "7. Register\n8. Support")
    if m == '1':
        if d == 1:
            return "CON Select crop:\n" + "\n".join(f"{k}. {v[0]}" for k,v in CROP_P.items()) + "\n0. Back"
        p = CROP_P.get(l)
        if not p: return "END Invalid. Dial *789# again."
        n,ws,rt,u = p
        return f"END {n.upper()}\nWholesale: UGX {fmt(ws)}/{u}\nRetail: UGX {fmt(rt)}/{u}\nagribridge.com"
    if m == '2':
        if d == 1:
            return "CON Select animal:\n" + "\n".join(f"{k}. {v[0]}" for k,v in ANIM_P.items()) + "\n0. Back"
        p = ANIM_P.get(l)
        if not p: return "END Invalid."
        n,mn,mx,u = p
        return f"END {n.upper()}\nMin: UGX {fmt(mn)}/{u}\nMax: UGX {fmt(mx)}/{u}\nagribridge.com"
    if m == '7':
        if d == 1: return "CON Register as:\n1. Farmer\n2. Buyer\n3. Vendor\n0. Back"
        return f"END Register FREE:\nagribridge.com\nOr call: +256 755 966 690"
    if m == '8':
        return "END AgriBridge Support:\n+256 755 966 690\nhello@agribridge.ug\nMon-Sat 7am-8pm"
    return "END Invalid option.\nDial *789# to start.\n+256 755 966 690"

@app.route('/api/ussd', methods=['POST'])
def ussd_ep():
    sid   = request.form.get('sessionId', '')
    phone = request.form.get('phoneNumber', '')
    text  = request.form.get('text', '').strip()
    parts = [p.strip() for p in text.split('*') if p.strip()] if text else []
    try:
        resp = ussd(parts, phone, sid)
    except Exception as e:
        log.error(f"USSD error: {e}")
        resp = "END Error. Dial *789# again.\n+256 755 966 690"
    return Response(resp, mimetype='text/plain')

# ── Error handlers ────────────────────────────────────────────────────────────
@app.errorhandler(404)
def e404(e):
    if request.path.startswith('/api/'): return jsonify({'error': 'Not found'}), 404
    try: return send_from_directory('static', 'index.html')
    except: return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def e500(e):
    log.error(f"500 on {request.path}: {e}")
    return jsonify({'error': 'Something went wrong. Please try again.'}), 500

@app.errorhandler(429)
def e429(e):
    return jsonify({'error': 'Too many requests. Please slow down.'}), 429

@app.errorhandler(Exception)
def eall(e):
    log.error(f"Unhandled on {request.path}: {e}", exc_info=True)
    return jsonify({'error': 'An unexpected error occurred.'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    log.info(f"AgriBridge v5.0 port={port} supa={'YES' if SUPABASE_KEY else 'NO'} at={'YES' if at_sms else 'NO'}")
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') == 'development')
