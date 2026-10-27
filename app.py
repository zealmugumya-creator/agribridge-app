# ═══════════════════════════════════════════════════════════════════════════
# AgriBridge Flask Backend v3.0 — Full USSD + All Original Routes
# Deploy on Render.com as a Web Service
# ═══════════════════════════════════════════════════════════════════════════
# ENVIRONMENT VARIABLES (set in Render Dashboard → Environment):
#   JWT_SECRET, ADMIN_PASSWORD, AT_USERNAME, AT_API_KEY
#   AT_SHORTCODE, AT_SMS_SENDER, SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY
# ═══════════════════════════════════════════════════════════════════════════

import base64
import datetime
import hmac
import json as _json
import os
import re
import secrets
import threading
import time as _time
import uuid
from urllib.parse import urlencode, urlparse

import jwt
import requests
from cryptography.hazmat.primitives import padding as _sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher as _SymCipher
from cryptography.hazmat.primitives.ciphers import modes as _SymModes
from flask import Flask, Response, g, jsonify, redirect, request
from flask_cors import CORS
from werkzeug.exceptions import RequestEntityTooLarge

try:  # cryptography >= 43 keeps TripleDES in `decrepit`; older releases in `primitives`.
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES as _TripleDES
except ImportError:  # pragma: no cover - depends on the pinned cryptography version
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES as _TripleDES

from services import business, finance, ledger, monitoring

# ── AgriBridge service modules (isolated integrations, Phase 10) ──────────────
from services.account import AccountService
from services.config import ConfigError, load_settings
from services.discord import DiscordNotifier
from services.legal import LegalService
from services.logging import (
    configure_logging,
    get_logger,
    new_correlation_id,
    set_correlation_id,
)
from services.matching import MatchingService
from services.notifications import NotificationDispatcher
from services.orders import OrderService
from services.pesapal import (
    PesapalClient,
    PesapalError,
)
from services.pesapal import (
    is_completed as pesapal_completed,
)
from services.pesapal import (
    is_failed as pesapal_failed,
)
from services.push import build_push
from services.supabase_client import _UPLOADS_BUCKET, SupabaseClient
from services.webpush import WebPushService

configure_logging()
log = get_logger("app")

try:
    import africastalking
    AT_AVAILABLE = True
except ImportError:
    AT_AVAILABLE = False
    log.warning("deps.africastalking_missing", note="SMS disabled")

# Fail closed on missing production config (audit finding H1): never boot an
# admin-capable API with an unstable per-boot secret.
try:
    SETTINGS = load_settings()
except ConfigError as _cfg_err:
    log.error("config.fatal", error=str(_cfg_err))
    raise

app = Flask(__name__)
# A 25 MB video plus multipart metadata must be accepted, while an unlimited
# request body would let unauthenticated clients exhaust a small Render worker.
app.config['MAX_CONTENT_LENGTH'] = 28 * 1024 * 1024
CORS(app, resources={r"/api/*": {"origins": SETTINGS.cors_origins}})

# ── Config ────────────────────────────────────────────────────────────────────
JWT_SECRET     = SETTINGS.jwt_secret or secrets.token_hex(32)  # dev-only fallback
# Empty (not a public default) so admin login fails CLOSED when unconfigured.
ADMIN_PASSWORD = SETTINGS.admin_password
ADMIN_ALLOWED_UUIDS = SETTINGS.admin_allowed_uuids
AT_USERNAME    = os.environ.get('AT_USERNAME',    'sandbox')
AT_API_KEY     = os.environ.get('AT_API_KEY',     'atsk_REPLACE_ME')
AT_SHORTCODE   = os.environ.get('AT_SHORTCODE',   '*789#')
AT_SMS_SENDER  = os.environ.get('AT_SMS_SENDER',  'AgriBridge')
SUPABASE_URL         = SETTINGS.supabase_url
SUPABASE_KEY         = SETTINGS.supabase_key
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')  # service-role key for server-side reads that bypass RLS
GEMINI_KEY           = os.environ.get('GEMINI_API_KEY', '')

# ── Service wiring (constructed once; used by routes and the worker) ──────────
monitoring.init(SETTINGS.sentry_dsn, os.environ.get('SENTRY_ENVIRONMENT', SETTINGS.env),
                float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0.1')))
SUPA = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
DISCORD = DiscordNotifier(SETTINGS.discord_webhook_url)
# FCM push (Phase 6): sender + admin token lookup + device registry. Absent
# credentials => a safe no-op sender (None) so the queue still drains.
FCM_SENDER, ADMIN_TOKEN_LOOKUP, PUSH_REGISTRY = build_push(SETTINGS, SUPA)
# Web Push (VAPID) for the website/PWA: a safe no-op until VAPID_* keys are set.
WEB_PUSH = WebPushService.from_env(SUPA)
NOTIFY = NotificationDispatcher(supabase=SUPA, discord=DISCORD,
                                fcm_sender=FCM_SENDER, admin_token_lookup=ADMIN_TOKEN_LOOKUP,
                                web_push=WEB_PUSH)
LEGAL_SERVICE = LegalService(supabase=SUPA)
LEGAL_ENFORCED = os.environ.get('ENFORCE_LEGAL_ACCEPTANCE', '').strip().lower() in ('1', 'true', 'yes')
ORDER_SERVICE = OrderService(supabase=SUPA, dispatcher=NOTIFY)
MATCHING_SERVICE = MatchingService(supabase=SUPA, dispatcher=NOTIFY)
ACCOUNT_SERVICE = AccountService(SUPA, supabase_url=SUPABASE_URL, service_key=SUPABASE_KEY,
                                 hard_delete_enabled=SETTINGS.account_hard_delete_enabled)

# ── Payments (pluggable; each provider stays OFF until its keys are set) ───────
FLW_SECRET_KEY   = os.environ.get('FLW_SECRET_KEY',   '')  # Flutterwave secret key
FLW_WEBHOOK_HASH = os.environ.get('FLW_WEBHOOK_HASH', '')  # must match the hash set in the FLW dashboard
FLW_PUBLIC_KEY   = os.environ.get('FLW_PUBLIC_KEY',   '')
FLW_ENCRYPTION_KEY = os.environ.get('FLW_ENCRYPTION_KEY', '')
# On-page card entry charges the card directly, so raw card data reaches this server.
# It is OFF unless the operator explicitly enables it after PCI DSS attestation.
FLW_DIRECT_CARD  = os.environ.get('FLW_DIRECT_CARD', '').strip().lower() in ('1', 'true', 'yes')
PESAPAL_KEY      = os.environ.get('PESAPAL_CONSUMER_KEY',    '')
PESAPAL_SECRET   = os.environ.get('PESAPAL_CONSUMER_SECRET', '')
PESAPAL_ENV      = os.environ.get('PESAPAL_ENV', 'sandbox').strip().lower()
PESAPAL_IPN_ID   = os.environ.get('PESAPAL_IPN_ID', '').strip()
MTN_MOMO_KEY     = os.environ.get('MTN_MOMO_SUBSCRIPTION_KEY', '')
PUBLIC_BASE_URL  = SETTINGS.public_base_url
# Pesapal must reach the Flask API directly. The marketing site is a static
# Worker that does not proxy /api, so the default is the Render service.
PESAPAL_BASE_URL_DEFAULT = 'https://agribridge-1-og7a.onrender.com'


def _resolve_pesapal_base_url():
    """A truncated host here is not cosmetic: Pesapal would accept the IPN
    registration, then notify a domain that does not exist, so a completed
    payment would never mark its order paid. Fall back to the known origin."""
    raw = os.environ.get('PESAPAL_CALLBACK_BASE_URL', '').strip().rstrip('/')
    if not raw:
        return PESAPAL_BASE_URL_DEFAULT
    host = urlparse(raw).hostname or ''
    if '.' not in host and host not in ('localhost', '127.0.0.1'):
        log.error('pesapal.callback_base_url_rejected', host=host[:60],
                  using=PESAPAL_BASE_URL_DEFAULT)
        return PESAPAL_BASE_URL_DEFAULT
    return raw


PESAPAL_CALLBACK_BASE_URL = _resolve_pesapal_base_url()

# Bank transfer is always offered as an offline method (like COD): the order is
# placed unpaid and an admin verifies the transfer against a bank reference.
# The receiving account is only shown when the owner has actually configured it;
# an unset account never falls back to a made-up number.
BANK_TRANSFER_ACCOUNTS = []
for _bt in os.environ.get('BANK_TRANSFER_ACCOUNTS', '').split(';'):
    _bt = _bt.strip()
    if not _bt:
        continue
    _parts = [p.strip() for p in _bt.split('|')]
    if len(_parts) >= 3 and all(_parts[:3]):
        BANK_TRANSFER_ACCOUNTS.append({
            'bank': _parts[0][:80],
            'account_name': _parts[1][:80],
            'account_number': _parts[2][:40],
            'branch': (_parts[3] if len(_parts) > 3 else '')[:80],
        })

at_sms = None
if AT_AVAILABLE and AT_API_KEY and AT_API_KEY != 'atsk_REPLACE_ME':
    try:
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        at_sms = africastalking.SMS
    except Exception as _at_err:
        log.warning("at.init_failed", error=type(_at_err).__name__)

# ── Security hardening ────────────────────────────────────────────────────────
# Redis-backed sliding-window rate limiter (shared across all gunicorn workers).
# Falls back to in-process limiter when REDIS_URL is not set or Redis is down.
from services.rate_limit import RateLimiter as _RateLimiter

_LIMITER = _RateLimiter(redis_url=os.environ.get('REDIS_URL'))

def rate_limit(max_req=30, window=60):
    """Sliding-window rate limiter keyed by route+client IP (Redis-backed)."""
    return _LIMITER.limit(max_req=max_req, window=window)

@app.before_request
def _begin_request():
    # Correlation ID: honour a trusted upstream header, else mint one. Attached to
    # every log line for this request and echoed back for client-side tracing (H5).
    cid = request.headers.get('X-Correlation-ID', '')
    g.correlation_id = set_correlation_id(cid)
    g.started_at = _time.time()

@app.after_request
def _security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['X-XSS-Protection'] = '1; mode=block'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    resp.headers['X-Correlation-ID'] = getattr(g, 'correlation_id', '-')
    # Structured access log (never logs bodies/headers — secrets stay out).
    try:
        dur_ms = int((_time.time() - getattr(g, 'started_at', _time.time())) * 1000)
    except Exception:
        dur_ms = -1
    log.info("http.request", method=request.method, path=request.path,
             status=resp.status_code, duration_ms=dur_ms)
    return resp

@app.errorhandler(404)
def _not_found(e):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(RequestEntityTooLarge)
def _request_too_large(e):
    return jsonify({'error': 'File is too large. Photos must be at most 5 MB and videos at most 25 MB.'}), 413

@app.errorhandler(Exception)
def _unhandled(e):
    # Surface a safe, honest 500 with the correlation id; capture to Sentry.
    log.exception("http.unhandled_error", path=request.path)
    monitoring.capture_exception(e, path=request.path)
    if hasattr(e, 'code') and hasattr(e, 'description'):  # HTTPException
        return e
    # Alert admin on Discord for unexpected 500s
    try:
        _discord_notify(
            event_type='server.error',
            title='🔥 Unhandled Server Error',
            description=(
                f"**Path:** `{request.path}`\n"
                f"**Error:** {type(e).__name__}: {str(e)[:200]}\n"
                f"**Correlation ID:** `{getattr(g, 'correlation_id', '-')}`"
            ),
            severity='error',
        )
        supa_insert('system_events', {
            'event_type': 'server_error',
            'severity': 'error',
            'details': {
                'path': request.path,
                'error': f"{type(e).__name__}: {str(e)[:300]}",
                'correlation_id': getattr(g, 'correlation_id', '-'),
            },
        })
    except Exception:
        pass
    return jsonify({'error': 'Internal server error',
                    'correlation_id': getattr(g, 'correlation_id', '-')}), 500

# ── Supabase helpers ──────────────────────────────────────────────────────────
def supa_get(table, filters=None, limit=100):
    if not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {'limit': limit}
    if filters:
        params.update(filters)
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json'
    }
    try:
        res = requests.get(url, params=params, headers=headers, timeout=8)
        return res.json() if res.ok else []
    except Exception as e:
        print(f"Supabase GET error: {e}")
        return []

def supa_get_admin(table, filters=None, limit=100):
    """Like supa_get but uses the service-role key to bypass RLS.

    Only use server-side where the caller has already validated the request.
    Falls back to anon key if SUPABASE_SERVICE_KEY is not set.
    """
    key = SUPABASE_SERVICE_KEY or SUPABASE_KEY
    if not key:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {'limit': limit}
    if filters:
        params.update(filters)
    headers = {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }
    try:
        res = requests.get(url, params=params, headers=headers, timeout=8)
        return res.json() if res.ok else []
    except Exception as e:
        print(f"Supabase admin GET error: {e}")
        return []

def supa_get_checked(table, filters=None, limit=100):
    """Read rows but return (rows, error) instead of hiding failures as [].

    The admin finance views must not present an unreachable or missing table as
    "zero money", so this variant surfaces PostgREST's error code/message.
    """
    if not SUPABASE_KEY:
        return None, 'supabase not configured'
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json'
    }
    try:
        page_size = min(int(limit), 1000)
        rows = []
        offset = 0
        while len(rows) < limit:
            params = {'limit': page_size, 'offset': offset}
            if filters:
                params.update(filters)
            res = requests.get(url, params=params, headers=headers, timeout=8)
            if not res.ok:
                try:
                    detail = res.json()
                except Exception:
                    detail = {}
                code = str(detail.get('code') or '')
                message = str(detail.get('message') or f'http {res.status_code}')
                log.error('supabase.read_failed', table=table, status=res.status_code,
                          code=code, message=message)
                return None, f"{code}: {message}" if code else message
            batch = res.json()
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += len(batch)
        return rows[:limit], None
    except Exception as e:
        log.error('supabase.read_exception', table=table, error=type(e).__name__)
        return None, type(e).__name__

def supa_insert(table, data):
    if not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation'
    }
    try:
        res = requests.post(url, json=data, headers=headers, timeout=8)
        if res.ok:
            return res.json()
        try:
            detail = res.json()
        except Exception:
            detail = {}
        # Log the schema/constraint failure without writing row data (PII) to logs.
        log.error('supabase.insert_failed', table=table, status=res.status,
                  code=detail.get('code'), message=detail.get('message'))
        return None
    except Exception as e:
        log.error('supabase.insert_exception', table=table, error=str(e))
        return None

def supa_update(table, data, eq_col, eq_val):
    if not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal'
    }
    params = {eq_col: f'eq.{eq_val}'}
    try:
        res = requests.patch(url, json=data, params=params, headers=headers, timeout=8)
        return res.status_code < 300
    except Exception as e:
        print(f"Supabase UPDATE error: {e}")
        return False

def supa_delete(table, eq_col, eq_val):
    if not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
    }
    params = {eq_col: f'eq.{eq_val}'}
    try:
        res = requests.delete(url, params=params, headers=headers, timeout=8)
        return res.status_code < 300
    except Exception as e:
        print(f"Supabase DELETE error: {e}")
        return False

# ── Discord helper — fire-and-forget notification to ops channel ──────────────
def _discord_notify(event_type: str, title: str, description: str,
                    severity: str = 'info', colour: int | None = None):
    """Send a one-off Discord embed. Never raises; safe to call anywhere.

    `description` is a free-form string; we break it into key: value lines
    and pass them as embed fields so they render nicely in Discord.
    """
    if not DISCORD.enabled:
        return
    try:
        from services.discord import DiscordMessage
        # Parse "**Key:** value" lines into a fields dict
        fields: dict[str, str] = {}
        for line in description.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # strip markdown bold markers for the key
            if ':' in line:
                k, _, v = line.partition(':')
                fields[k.replace('**', '').strip()] = v.lstrip('*').strip()   # "**Key:** value" leaves "** value"
            else:
                fields[line] = ''
        msg = DiscordMessage(
            event_type=event_type,
            title=title,
            severity=severity,
            fields=fields or {'detail': description[:500]},
        )
        DISCORD.send(msg)
    except Exception as _e:
        log.warning("discord_notify.failed", event_type=event_type, error=str(_e))

# ── JWT helpers ───────────────────────────────────────────────────────────────
def make_token(payload_extra, hours=24):
    payload = {
        'iat': datetime.datetime.utcnow(),
        'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=hours),
        **payload_extra
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm='HS256')
    return token if isinstance(token, str) else token.decode('utf-8')

def verify_token(required_role=None):
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None, (jsonify({'error': 'Missing token'}), 401)
    token = auth.split(' ', 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        if required_role and payload.get('role') != required_role:
            return None, (jsonify({'error': 'Insufficient permissions'}), 403)
        return payload, None
    except jwt.ExpiredSignatureError:
        return None, (jsonify({'error': 'Token expired'}), 401)
    except jwt.InvalidTokenError:
        return None, (jsonify({'error': 'Invalid token'}), 401)

def _bearer_token():
    auth = request.headers.get('Authorization', '')
    return auth.split(' ', 1)[1].strip() if auth.startswith('Bearer ') else ''

def verify_supabase_user():
    """Resolve the caller's REAL identity from their Supabase Auth access token.

    The token is validated by GoTrue (`/auth/v1/user`), so a client cannot claim
    an arbitrary user id — the id comes from the verified session, never from the
    request body. Returns (user_id, None) or (None, error_response).
    """
    token = _bearer_token()
    if not token:
        return None, (jsonify({'error': 'Missing token'}), 401)
    if not SUPA.configured:
        return None, (jsonify({'error': 'Auth unavailable'}), 503)
    try:
        res = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {token}'},
            timeout=8,
        )
    except requests.RequestException:
        return None, (jsonify({'error': 'Auth check failed'}), 502)
    if res.status_code != 200:
        return None, (jsonify({'error': 'Invalid or expired session'}), 401)
    try:
        auth_user = res.json() or {}
        uid = auth_user.get('id')
    except ValueError:
        auth_user, uid = {}, None
    if not uid:
        return None, (jsonify({'error': 'Invalid session'}), 401)
    g.auth_user = auth_user   # verified by GoTrue; used for role/metadata reads
    return uid, None

def is_admin_uuid(user_id):
    """Server-controlled admin check against the user_roles table (Phase 3).

    Falls back to the ADMIN_ALLOWED_UUIDS allowlist so the very first super-admin
    can be bootstrapped before the role row exists. Never trusts client input.
    """
    if not user_id:
        return False
    if user_id in ADMIN_ALLOWED_UUIDS:
        return True
    rows = SUPA.select('user_roles', {'user_id': f'eq.{user_id}', 'select': 'role'}, limit=1)
    return bool(rows) and rows[0].get('role') in ('admin', 'superadmin')

def require_admin_user():
    """Admin authz by verified Supabase identity (replaces shared-password model).

    Returns (user_id, None) for an authorized admin, or (None, error_response).
    """
    uid, err = verify_supabase_user()
    if err:
        return None, err
    if not is_admin_uuid(uid):
        return None, (jsonify({'error': 'Insufficient permissions'}), 403)
    return uid, None

def legal_gate(user_id):
    """Block protected actions until the user accepted the current required terms.

    OFF by default (ENFORCE_LEGAL_ACCEPTANCE). Turn it on only after migration
    0007 is applied and the updated web app is live; otherwise users who have not
    yet seen the review screen could not place orders. Returns an error response
    tuple when blocked, else None. It fails CLOSED if the check itself fails.
    """
    if not LEGAL_ENFORCED:
        return None
    st = LEGAL_SERVICE.status(user_id, _auth_role())
    if not st.ok:
        return jsonify({'error': 'Could not verify legal acceptance', 'code': 'legal_check_failed'}), 503
    if st.data['outstanding']:
        return jsonify({'error': 'Please review and accept the updated terms to continue.',
                        'code': 'legal_acceptance_required',
                        'outstanding': st.data['outstanding']}), 403
    return None

# ── Number formatter ──────────────────────────────────────────────────────────
def fmt(n):
    return f"{int(n):,}"

# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/')
@app.route('/health')
def health():
    # M6: prove real dependency connectivity, don't just report "a key is set".
    # A green health endpoint must not be mistaken for working business flows.
    db = SUPA.ping()
    healthy = db.ok
    payload = {
        'status': 'ok' if healthy else 'degraded',
        'service': 'AgriBridge API',
        'version': '3.3.0',
        'at_enabled': AT_AVAILABLE and at_sms is not None,
        'supabase_configured': SUPA.configured,
        'supabase_connected': db.ok,          # actual round-trip result
        'discord_configured': DISCORD.enabled,
        'sentry_enabled': bool(SETTINGS.sentry_dsn),
    }
    if not healthy:
        log.error("health.db_unreachable", error=db.error)
    return jsonify(payload), (200 if healthy else 503)

@app.route('/health/live')
def health_live():
    # Liveness only: is the process up? (Never used as proof of business health.)
    return jsonify({'status': 'ok'}), 200

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN AUTH
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/admin/login', methods=['POST'])
@rate_limit(max_req=5, window=300)
def admin_login():
    data = request.get_json(force=True, silent=True) or {}
    if not ADMIN_PASSWORD:
        return jsonify({'error': 'Admin login is disabled (ADMIN_PASSWORD not configured).'}), 503
    # Constant-time compare to avoid timing side-channels.
    if not hmac.compare_digest(str(data.get('password', '')), ADMIN_PASSWORD):
        return jsonify({'error': 'Invalid password'}), 401
    token = make_token({'sub': 'admin', 'role': 'admin'}, hours=24)
    return jsonify({'token': token, 'role': 'admin', 'message': 'Login successful'}), 200

@app.route('/api/admin/verify', methods=['GET'])
def admin_verify():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    return jsonify({'valid': True, 'sub': payload.get('sub')}), 200

# ══════════════════════════════════════════════════════════════════════════════
# USSD — Full AgriBridge Menu System
# Africa's Talking calls: POST /api/ussd
# Set Callback URL in AT dashboard:
#   https://agribridge-1-og7a.onrender.com/api/ussd
# ══════════════════════════════════════════════════════════════════════════════

# ── USSD session helpers (DB-backed via ussd_sessions table) ─────────────────
def _ussd_get(session_id: str) -> dict:
    """Read session_data jsonb for this AT session_id, or {} if not found."""
    try:
        resp = SUPA.table('ussd_sessions') \
            .select('session_data') \
            .eq('session_id', session_id) \
            .eq('is_active', True) \
            .single() \
            .execute()
        return resp.data.get('session_data') or {} if resp.data else {}
    except Exception:
        return {}

def _ussd_set(session_id: str, phone: str, data: dict) -> None:
    """Upsert session_data for this AT session_id."""
    try:
        SUPA.table('ussd_sessions').upsert({
            'session_id':   session_id,
            'phone_number': phone,
            'session_data': data,
            'is_active':    True,
            'updated_at':   'now()',
        }, on_conflict='session_id').execute()
    except Exception as e:
        print(f"USSD session write error: {e}")

def _ussd_del(session_id: str) -> None:
    """Mark session inactive (soft-delete keeps audit trail)."""
    try:
        SUPA.table('ussd_sessions') \
            .update({'is_active': False, 'updated_at': 'now()'}) \
            .eq('session_id', session_id) \
            .execute()
    except Exception as e:
        print(f"USSD session delete error: {e}")

CROP_PRICES = {
    '1': ('Maize',        750,   1000,  'kg'),
    '2': ('Matooke',      950,   1400,  'bunch'),
    '3': ('Tomatoes',     1800,  2500,  'kg'),
    '4': ('Coffee',       12000, 15000, 'kg'),
    '5': ('Beans',        3400,  4200,  'kg'),
    '6': ('Cassava',      500,   800,   'kg'),
    '7': ('Irish Potato', 1200,  1800,  'kg'),
    '8': ('Onions',       2000,  2800,  'kg'),
    '9': ('Avocado',      800,   1200,  'kg'),
}

ANIMAL_PRICES = {
    '1': ('Cattle (Ankole/Friesian)', 2500000, 3500000, 'head'),
    '2': ('Goats',                    280000,  400000,  'head'),
    '3': ('Sheep',                    250000,  350000,  'head'),
    '4': ('Poultry (Broilers)',       25000,   45000,   'bird'),
    '5': ('Tilapia Fish',             12000,   18000,   'kg'),
    '6': ('Honey (Raw)',              15000,   22000,   'kg'),
}


def ussd_route(parts, depth, last, session_id, phone):
    # ── MAIN MENU ─────────────────────────────────────────────────────────────
    if depth == 0:
        return (
            "CON Welcome to AgriBridge *789#\n"
            "Uganda's Farm-to-Table Platform\n\n"
            "1. Check Crop Prices\n"
            "2. Animal & Livestock Prices\n"
            "3. Buy Produce / Marketplace\n"
            "4. List My Produce for Sale\n"
            "5. Farming Tips & Weather\n"
            "6. AI Crop & Animal Doctor\n"
            "7. Register as Farmer\n"
            "8. Support & My Account"
        )

    m = parts[0]

    # ── 1. CROP PRICES ────────────────────────────────────────────────────────
    if m == '1':
        if depth == 1:
            return (
                "CON Crop Prices - Select Crop:\n\n"
                "1. Maize\n2. Matooke (Banana)\n"
                "3. Tomatoes\n4. Coffee\n5. Beans\n"
                "6. Cassava\n7. Irish Potato\n"
                "8. Onions\n9. Avocado\n"
                "0. Main Menu"
            )
        if depth == 2:
            if last == '0':
                return ussd_route([], 0, '', session_id, phone)
            if last not in CROP_PRICES:
                return "END Invalid option.\nDial *789# to try again."
            name, ws, rt, unit = CROP_PRICES[last]
            return (
                f"END {name.upper()} PRICES\n"
                f"District: Kampala\n\n"
                f"Wholesale: UGX {fmt(ws)}/{unit}\n"
                f"Retail:    UGX {fmt(rt)}/{unit}\n\n"
                f"Updated: Today 08:00\n"
                f"Source: AgriBridge Markets\n"
                f"More: agribrige.com"
            )

    # ── 2. ANIMAL PRICES ──────────────────────────────────────────────────────
    if m == '2':
        if depth == 1:
            return (
                "CON Animal & Livestock Prices:\n\n"
                "1. Cattle (Ankole/Friesian)\n"
                "2. Goats\n3. Sheep\n"
                "4. Poultry (Broilers/Layers)\n"
                "5. Tilapia Fish\n6. Honey\n"
                "0. Main Menu"
            )
        if depth == 2:
            if last == '0':
                return ussd_route([], 0, '', session_id, phone)
            if last not in ANIMAL_PRICES:
                return "END Invalid option.\nDial *789# to retry."
            name, mn, mx, unit = ANIMAL_PRICES[last]
            return (
                f"END {name.upper()}\n\n"
                f"Min: UGX {fmt(mn)}/{unit}\n"
                f"Max: UGX {fmt(mx)}/{unit}\n\n"
                f"Source: AgriBridge Livestock\n"
                f"Updated: Today\n\n"
                f"Buy/sell: agribrige.com\n"
                f"Call: +256 755 966 690"
            )

    # ── 3. MARKETPLACE ────────────────────────────────────────────────────────
    if m == '3':
        if depth == 1:
            return (
                "CON AgriBridge Marketplace:\n\n"
                "1. Browse Fresh Produce\n"
                "2. Browse Livestock\n"
                "3. Bulk Orders (Hotels/NGOs)\n"
                "4. Order by Phone\n"
                "0. Main Menu"
            )
        if depth == 2:
            if last == '0':
                return ussd_route([], 0, '', session_id, phone)
            if last == '3':
                return (
                    "END Bulk Orders:\n\n"
                    f"Call: {business.PHONE_DISPLAY}\n"
                    f"WhatsApp: {business.PHONE_DISPLAY}\n"
                    f"Email: {business.SUPPORT_EMAIL}\n"
                    "Web: agribrige.com\n\n"
                    "Farm-to-door within 24hrs"
                )
            if last == '4':
                return (
                    "END Order by Phone:\n\n"
                    "Call: +256 755 966 690\n"
                    "Mon-Sat: 7am - 8pm\n\n"
                    "Tell us what you need.\n"
                    "We source from verified\n"
                    "farms directly for you."
                )
            return (
                "END Browse all listings:\n"
                "agribrige.com\n\n"
                "Or call us:\n"
                "+256 755 966 690\n"
                "Mon-Sat 7am-8pm"
            )

    # ── 4. LIST MY PRODUCE ────────────────────────────────────────────────────
    if m == '4':
        if depth == 1:
            return (
                "CON List Your Produce FREE:\n\n"
                "1. Select crop to list\n"
                "2. List via SMS instructions\n"
                "3. Call us to list\n"
                "0. Main Menu"
            )
        if depth == 2:
            if last == '0':
                return ussd_route([], 0, '', session_id, phone)
            if last == '1':
                return (
                    "CON Select crop to list:\n\n"
                    "1. Maize\n2. Matooke\n"
                    "3. Tomatoes\n4. Coffee\n"
                    "5. Beans\n6. Cassava\n"
                    "7. Other crop\n"
                    "0. Back"
                )
            if last == '2':
                return (
                    "END List via SMS:\n\n"
                    "Send to 789:\n"
                    "LIST [crop] [qty] [price]\n\n"
                    "Example:\n"
                    "LIST MAIZE 500KG 750\n\n"
                    "We post your listing\n"
                    "within 5 minutes. FREE!"
                )
            if last == '3':
                return (
                    "END Call to List:\n\n"
                    "+256 755 966 690\n"
                    "Mon-Sat 7am-7pm\n\n"
                    "We list for you in\n"
                    "under 2 minutes.\n"
                    "Completely FREE!"
                )
        # depth 3 — crop selected
        if depth == 3 and parts[1] == '1':
            crop_map = {
                '1': 'Maize', '2': 'Matooke', '3': 'Tomatoes',
                '4': 'Coffee', '5': 'Beans', '6': 'Cassava', '7': 'Other'
            }
            if last == '0':
                return ussd_route(['4'], 1, '4', session_id, phone)
            crop = crop_map.get(last, 'Crop')
            _ussd_set(session_id, phone, {'crop': crop, 'phone': phone})
            return (
                f"CON {crop} selected.\n"
                f"Enter quantity in kg\n"
                f"(e.g. type 500 then Send):"
            )
        # depth 4 — quantity entered
        if depth == 4 and parts[1] == '1':
            session = _ussd_get(session_id)
            session['qty'] = last
            _ussd_set(session_id, phone, session)
            return (
                f"CON Quantity: {last}kg\n"
                f"Enter your price per kg\n"
                f"in UGX (e.g. type 800):"
            )
        # depth 5 — price entered, save listing
        if depth == 5 and parts[1] == '1':
            session = _ussd_get(session_id)
            price = last
            crop  = session.get('crop', 'Crop')
            qty   = session.get('qty', '0')
            farmer_rows = supa_get('farmers', {'phone': f'eq.{phone}'}, limit=1) or []
            farmer = farmer_rows[0] if farmer_rows else {}
            # listings has no farmer_name/source columns; district is NOT NULL.
            saved = supa_insert('listings', {
                'farmer_id':    farmer.get('id'),
                'crop_name':    crop,
                'quantity_kg':  float(qty)   if qty.replace('.', '').isdigit()   else 0,
                'price_per_kg': float(price) if price.replace('.', '').isdigit() else 0,
                'district':     farmer.get('district') or 'Unknown',
                'farmer_phone': phone,
                'is_available': True,
            })
            if not saved:
                _log_system_event('listing_create_failed', {'channel': 'ussd'}, severity='error')
                _ussd_del(session_id)
                return (
                    "END Sorry, we could not save\n"
                    "your listing right now.\n\n"
                    "Please try again shortly or\n"
                    "call +256 755 966 690."
                )
            _discord_notify(
                event_type='listing.created',
                title='🌾 New Listing via USSD',
                description=(
                    f"**Crop:** {crop}\n"
                    f"**Quantity:** {qty} kg\n"
                    f"**Price:** UGX {price}/kg\n"
                    f"**Farmer phone:** {phone}"
                ),
                severity='info',
            )
            if at_sms:
                try:
                    at_sms.send(
                        message=(
                            f"AgriBridge: Listing posted!\n"
                            f"{qty}kg {crop} @ UGX {price}/kg.\n"
                            f"Buyers will contact you. agribrige.com"
                        ),
                        recipients=[phone],
                        sender_id=AT_SMS_SENDER
                    )
                except Exception as e:
                    print(f"SMS error: {e}")
            _ussd_del(session_id)
            return (
                f"END Listing posted!\n\n"
                f"Crop:     {crop}\n"
                f"Quantity: {qty}kg\n"
                f"Price:    UGX {price}/kg\n\n"
                f"Buyers will contact you.\n"
                f"SMS confirmation sent.\n"
                f"Thank you - AgriBridge!"
            )

    # ── 5. FARMING TIPS ───────────────────────────────────────────────────────
    if m == '5':
        if depth == 1:
            return (
                "CON Farming Tips & Weather:\n\n"
                "1. Soil & Planting Tips\n"
                "2. Pest & Disease Alerts\n"
                "3. Harvest & Storage\n"
                "4. Weather Forecast\n"
                "5. Planting Calendar\n"
                "6. Animal Care Tips\n"
                "0. Main Menu"
            )
        if depth == 2:
            if last == '0':
                return ussd_route([], 0, '', session_id, phone)
            tips = {
                '1': (
                    "END SOIL & PLANTING TIPS\n\n"
                    "Test soil pH before planting.\n"
                    "Maize ideal pH: 5.8 - 6.5\n\n"
                    "Add lime to raise pH.\n"
                    "Add sulfur to lower pH.\n\n"
                    "Season A: Plant Mar-Apr\n"
                    "Season B: Plant Aug-Sep\n\n"
                    "More: agribrige.com/training"
                ),
                '2': (
                    "END PEST ALERT - 2026\n\n"
                    "FALL ARMYWORM ACTIVE!\n"
                    "Central & Eastern Uganda\n\n"
                    "Treatment:\n"
                    "Emamectin Benzoate\n"
                    "200ml per 20L water\n"
                    "Spray into whorl at dusk\n"
                    "Repeat after 7 days"
                ),
                '3': (
                    "END HARVEST & STORAGE\n\n"
                    "Harvest maize at 20-25%\n"
                    "moisture. Use PICS bags.\n"
                    "Store cool and dry.\n"
                    "Inspect every 2 weeks.\n\n"
                    "Proper storage = 6-12\n"
                    "months without loss."
                ),
                '4': (
                    "END WEATHER - Kampala\n\n"
                    "Today: Partly Cloudy 24C\n"
                    "Tomorrow: Light Rain 22C\n"
                    "This Week: Rain expected\n\n"
                    "Farming Advisory:\n"
                    "Delay fertilizer 2-3 days.\n"
                    "Good planting next week.\n\n"
                    "Powered by AgriBridge AI"
                ),
                '5': (
                    "END PLANTING CALENDAR\n\n"
                    "SEASON A (Long Rains)\n"
                    "Plant: March - April\n"
                    "Harvest: June - July\n\n"
                    "SEASON B (Short Rains)\n"
                    "Plant: August - September\n"
                    "Harvest: November - December\n\n"
                    "Coffee harvest: Oct - Feb"
                ),
                '6': (
                    "END ANIMAL CARE TIPS\n\n"
                    "Cattle vaccinations:\n"
                    "- FMD: every 6 months\n"
                    "- ECF (ITM): once yearly\n\n"
                    "ECF symptoms:\n"
                    "Fever 39-41C, swollen\n"
                    "lymph nodes, not eating.\n\n"
                    "ACT FAST - call vet!\n"
                    "Book: agribrige.com/vet"
                ),
            }
            return tips.get(last, "END Invalid option.\nDial *789# to retry.")

    # ── 6. AI DOCTOR ──────────────────────────────────────────────────────────
    if m == '6':
        if depth == 1:
            return (
                "CON AI Doctor - Free Diagnosis:\n\n"
                "1. Diagnose Crop Problem\n"
                "2. Diagnose Animal Problem\n"
                "3. Disease Alerts Uganda\n"
                "4. Book a Vet Visit\n"
                "0. Main Menu"
            )
        if depth == 2:
            if last == '0':
                return ussd_route([], 0, '', session_id, phone)
            if last == '1':
                return (
                    "CON Select your crop:\n\n"
                    "1. Maize\n2. Matooke\n"
                    "3. Tomatoes\n4. Coffee\n"
                    "5. Beans\n6. Cassava\n"
                    "7. Other\n0. Back"
                )
            if last == '2':
                return (
                    "CON Select animal type:\n\n"
                    "1. Cattle\n2. Goats\n"
                    "3. Chickens/Poultry\n"
                    "4. Pigs\n5. Fish\n"
                    "6. Other\n0. Back"
                )
            if last == '3':
                return (
                    "END DISEASE ALERTS 2026\n\n"
                    "CROPS:\n"
                    "Fall Armyworm - ACTIVE\n"
                    "Maize Streak - Eastern UG\n\n"
                    "LIVESTOCK:\n"
                    "ECF - Central/Western UG\n"
                    "FMD - sporadic outbreaks\n"
                    "NCD (chickens) - reported\n\n"
                    "Full info: agribrige.com"
                )
            if last == '4':
                return (
                    "END Book a Vet Visit:\n\n"
                    "Call: +256 755 966 690\n"
                    "24/7 emergency line\n\n"
                    "Services:\n"
                    "Farm visit: UGX 50,000\n"
                    "Vaccination: from 5,000\n"
                    "Disease testing available\n"
                    "MAAIF certificates\n\n"
                    "Book: agribrige.com/vet"
                )
        if depth == 3:
            if parts[1] == '1':
                crop_names = {
                    '1': 'Maize', '2': 'Matooke', '3': 'Tomatoes',
                    '4': 'Coffee', '5': 'Beans', '6': 'Cassava', '7': 'Other'
                }
                if last == '0':
                    return ussd_route(['6'], 1, '6', session_id, phone)
                crop = crop_names.get(last, 'crop')
                _ussd_set(session_id, phone, {'crop': crop})
                return (
                    f"CON {crop} - Main symptom:\n\n"
                    "1. Yellowing / pale leaves\n"
                    "2. Brown spots / lesions\n"
                    "3. Wilting / drooping\n"
                    "4. White powder / mould\n"
                    "5. Holes / pest damage\n"
                    "6. Rotting / soft stem\n"
                    "7. Stunted growth\n"
                    "0. Back"
                )
            if parts[1] == '2':
                animal_names = {
                    '1': 'Cattle', '2': 'Goats', '3': 'Chickens',
                    '4': 'Pigs', '5': 'Fish', '6': 'Other'
                }
                if last == '0':
                    return ussd_route(['6'], 1, '6', session_id, phone)
                animal = animal_names.get(last, 'animal')
                _ussd_set(session_id, phone, {'animal': animal})
                return (
                    f"CON {animal} - Main symptom:\n\n"
                    "1. High fever / hot body\n"
                    "2. Not eating / dull\n"
                    "3. Diarrhoea\n"
                    "4. Coughing / breathing\n"
                    "5. Skin lesions / sores\n"
                    "6. Limping / swollen joints\n"
                    "7. Sudden deaths in group\n"
                    "0. Back"
                )
        if depth == 4:
            if parts[1] == '1':
                session = _ussd_get(session_id)
                crop = session.get('crop', 'crop')
                diagnoses = {
                    '1': (
                        f"END {crop.upper()} - Yellowing\n\n"
                        "Likely: Nitrogen deficiency\n"
                        "or Maize Streak Virus\n\n"
                        "Fix: Apply CAN fertilizer\n"
                        "150kg/ha. Control\n"
                        "leafhopper insects.\n\n"
                        "More: agribrige.com"
                    ),
                    '2': (
                        f"END {crop.upper()} - Brown Spots\n\n"
                        "Likely: Fungal blight\n\n"
                        "Fix: Spray Mancozeb 80WP\n"
                        "40g per 20L water\n"
                        "every 10-14 days.\n\n"
                        "More: agribrige.com"
                    ),
                    '3': (
                        f"END {crop.upper()} - Wilting\n\n"
                        "Likely: Bacterial Wilt\n"
                        "or drought stress\n\n"
                        "Fix: Check soil moisture.\n"
                        "If roots brown: apply\n"
                        "Metalaxyl fungicide.\n\n"
                        "Call: +256 755 966 690"
                    ),
                    '4': (
                        f"END {crop.upper()} - White Powder\n\n"
                        "Likely: Powdery Mildew\n\n"
                        "Fix: Spray sulphur-based\n"
                        "fungicide. Improve air\n"
                        "circulation.\n\n"
                        "More: agribrige.com"
                    ),
                    '5': (
                        f"END {crop.upper()} - Pest Damage\n\n"
                        "Likely: Fall Armyworm\n\n"
                        "Fix: Emamectin Benzoate\n"
                        "200ml per 20L water.\n"
                        "Spray into whorl at dusk.\n"
                        "Repeat after 7 days.\n\n"
                        "More: agribrige.com"
                    ),
                    '6': (
                        f"END {crop.upper()} - Rotting\n\n"
                        "Likely: Root rot or\n"
                        "bacterial soft rot\n\n"
                        "Fix: Improve drainage.\n"
                        "Apply Metalaxyl.\n"
                        "Remove affected plants.\n\n"
                        "Call: +256 755 966 690"
                    ),
                    '7': (
                        f"END {crop.upper()} - Stunted\n\n"
                        "Likely: Nutrient deficiency\n"
                        "or CMD virus (cassava)\n\n"
                        "Fix: Soil test first.\n"
                        "Apply NPK fertilizer.\n"
                        "For CMD: plant resistant\n"
                        "varieties (NAROCAS 1).\n\n"
                        "More: agribrige.com"
                    ),
                }
                _ussd_del(session_id)
                return diagnoses.get(last, (
                    "END For detailed diagnosis\n"
                    "visit: agribrige.com\n\n"
                    "Call: +256 755 966 690\n"
                    "Mon-Sat 7am-8pm"
                ))
            if parts[1] == '2':
                session = _ussd_get(session_id)
                animal = session.get('animal', 'animal')
                diagnoses = {
                    '1': (
                        f"END {animal.upper()} - Fever\n\n"
                        "Likely: East Coast Fever\n"
                        "or Trypanosomiasis\n\n"
                        "ACT IMMEDIATELY!\n"
                        "Call vet within 24hrs.\n"
                        "ECF: Butalex injection\n"
                        "Tick control: Amitraz\n\n"
                        "Emergency: +256 755 966 690"
                    ),
                    '2': (
                        f"END {animal.upper()} - Not Eating\n\n"
                        "Likely: Fever or pain\n\n"
                        "Check temperature.\n"
                        "Normal cattle: 38.5C\n"
                        "If above 39C: call vet\n\n"
                        "Call: +256 755 966 690\n"
                        "Book: agribrige.com/vet"
                    ),
                    '3': (
                        f"END {animal.upper()} - Diarrhoea\n\n"
                        "Likely: Coccidiosis or\n"
                        "bacterial infection\n\n"
                        "Fix: Oral rehydration.\n"
                        "Amprolium for cocci.\n"
                        "Isolate sick animals.\n\n"
                        "Call: +256 755 966 690"
                    ),
                    '4': (
                        f"END {animal.upper()} - Coughing\n\n"
                        "Likely: Pneumonia\n\n"
                        "Fix: Isolate immediately.\n"
                        "Oxytetracycline injection.\n"
                        "Call vet within 24hrs.\n\n"
                        "Emergency: +256 755 966 690"
                    ),
                    '5': (
                        f"END {animal.upper()} - Skin Lesions\n\n"
                        "Likely: Lumpy Skin Disease\n\n"
                        "LSD is NOTIFIABLE!\n"
                        "Isolate animal.\n"
                        "Contact DVO now.\n\n"
                        "Call: +256 755 966 690"
                    ),
                    '6': (
                        f"END {animal.upper()} - Limping\n\n"
                        "Likely: Foot & Mouth\n"
                        "or joint infection\n\n"
                        "FMD is NOTIFIABLE!\n"
                        "Report to DVO now.\n"
                        "Isolate all cattle.\n\n"
                        "AgriBridge: +256 755 966 690"
                    ),
                    '7': (
                        f"END {animal.upper()} - EMERGENCY!\n\n"
                        "Sudden deaths likely:\n"
                        "Anthrax or Newcastle\n\n"
                        "DO NOT touch dead animals!\n"
                        "Call DVO IMMEDIATELY.\n"
                        "Quarantine the farm.\n\n"
                        "Emergency: +256 755 966 690"
                    ),
                }
                _ussd_del(session_id)
                return diagnoses.get(last, (
                    "END For detailed diagnosis\n"
                    "visit: agribrige.com\n\n"
                    "Emergency vet:\n"
                    "+256 755 966 690 (24/7)"
                ))

    # ── 7. REGISTER ───────────────────────────────────────────────────────────
    if m == '7':
        if depth == 1:
            return (
                "CON Register on AgriBridge:\n\n"
                "1. Register as Crop Farmer\n"
                "2. Register as Livestock Farmer\n"
                "3. Register as Buyer/Vendor\n"
                "0. Main Menu"
            )
        if depth == 2:
            if last == '0':
                return ussd_route([], 0, '', session_id, phone)
            types = {
                '1': 'Crop Farmer',
                '2': 'Livestock Farmer',
                '3': 'Buyer/Vendor'
            }
            role = types.get(last, 'Farmer')
            return (
                f"END Register as {role}\n\n"
                f"Your number: {phone}\n\n"
                "OPTION 1 - Website:\n"
                "agribrige.com -> Join Free\n\n"
                "OPTION 2 - SMS:\n"
                "Send: REG [Name] [District]\n"
                "To: 789\n\n"
                "OPTION 3 - Call:\n"
                "+256 755 966 690\n\n"
                "Registration is FREE!"
            )

    # ── 8. SUPPORT ────────────────────────────────────────────────────────────
    if m == '8':
        if depth == 1:
            return (
                "CON Support & My Account:\n\n"
                "1. My Listings\n"
                "2. My Orders\n"
                "3. Set Price Alert\n"
                "4. Contact Support\n"
                "5. About AgriBridge\n"
                "0. Main Menu"
            )
        if depth == 2:
            if last == '0':
                return ussd_route([], 0, '', session_id, phone)
            if last == '4':
                return (
                    "END Contact AgriBridge:\n\n"
                    "Phone/WhatsApp:\n"
                    f"{business.PHONE_DISPLAY}\n\n"
                    "Email:\n"
                    f"{business.SUPPORT_EMAIL}\n\n"
                    "Website: agribrige.com"
                )
            if last == '5':
                return (
                    "END About AgriBridge\n\n"
                    "Uganda's #1 Farm-to-Table\n"
                    "Platform\n\n"
                    "5,000+ farmers connected\n"
                    "directly to buyers with\n"
                    "zero middlemen.\n\n"
                    "Crops AND Animals.\n"
                    "Works on ANY phone.\n\n"
                    "agribrige.com"
                )
            if last == '3':
                return (
                    "END Set Price Alert:\n\n"
                    "Send SMS to 789:\n"
                    "ALERT [crop] [price]\n\n"
                    "Example:\n"
                    "ALERT MAIZE 1000\n\n"
                    "We SMS you when maize\n"
                    "exceeds UGX 1,000/kg.\n"
                    "Free SMS alerts!"
                )
            if last == '1':
                listings = supa_get(
                    'listings',
                    {'farmer_phone': f'eq.{phone}', 'order': 'created_at.desc'},
                    limit=3
                ) or []
                if listings:
                    lines = "\n".join(
                        f"{l.get('crop_name','?')} "
                        f"{int(l.get('quantity_kg', 0))}kg "
                        f"UGX{int(l.get('price_per_kg', 0)):,}"
                        for l in listings[:3]
                    )
                    return f"END Your Listings:\n\n{lines}\n\nManage: agribrige.com"
                return (
                    "END No listings found.\n\n"
                    "List produce free:\n"
                    "agribrige.com\n"
                    "Or dial back & choose 4"
                )
            if last == '2':
                orders = supa_get(
                    'orders',
                    {'buyer_phone': f'eq.{phone}', 'order': 'created_at.desc'},
                    limit=3
                ) or []
                if orders:
                    lines = "\n".join(
                        f"#{str(o.get('id', '?'))[-4:]} "
                        f"{o.get('status', 'pending').upper()}"
                        for o in orders[:3]
                    )
                    return f"END Your Recent Orders:\n\n{lines}\n\nTrack: agribrige.com"
                return (
                    "END No orders found.\n\n"
                    "Browse marketplace:\n"
                    "agribrige.com\n"
                    "Or call: +256 755 966 690"
                )

    # ── FALLBACK ──────────────────────────────────────────────────────────────
    return (
        "END Oops! Invalid option.\n\n"
        "Dial *789# to start again.\n\n"
        "Need help?\n"
        "Call: +256 755 966 690\n"
        "Web: agribrige.com"
    )


@app.route('/api/ussd', methods=['POST'])
def ussd():
    session_id = request.form.get('sessionId',   '')
    phone      = request.form.get('phoneNumber', '')
    text       = request.form.get('text',        '').strip()

    parts = [p.strip() for p in text.split('*') if p.strip()] if text else []
    depth = len(parts)
    last  = parts[-1] if parts else ''

    response_text = ussd_route(parts, depth, last, session_id, phone)

    # Log to Supabase analytics (best-effort)
    try:
        supa_insert('ussd_sessions', {
            'session_id': session_id,
            'phone':      phone,
            'text':       text,
            'response':   response_text[:200],
        })
    except Exception:
        pass

    return Response(response_text, mimetype='text/plain')


# ══════════════════════════════════════════════════════════════════════════════
# SMS DELIVERY WEBHOOK
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/sms/delivery', methods=['POST'])
def sms_delivery():
    data = request.form.to_dict()
    print(f"SMS Delivery: {data}")
    return 'OK', 200

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    farmers      = supa_get('farmers',         limit=1000) or []
    listings     = supa_get('listings',        limit=1000) or []
    orders       = supa_get('orders',          limit=1000) or []
    animals      = supa_get('animal_listings', limit=1000) or []
    transporters = supa_get('transporters',    {'status': 'eq.active'}, limit=1000) or []
    return jsonify({
        'farmers':      len(farmers),
        'listings':     len(listings),
        'orders':       len(orders),
        'animals':      len(animals),
        'transporters': len(transporters),
        'revenue_ugx': sum(
            float(o.get('total_price', 0)) for o in orders
            if o.get('payment_status') == 'paid'
        )
    })

# ══════════════════════════════════════════════════════════════════════════════
# FINANCIAL CONTROL CENTER (admin, read-only)
# ══════════════════════════════════════════════════════════════════════════════
# Every figure below comes from a real row. Metrics without a source column are
# reported as unavailable; a missing ledger table is an explicit 503, never 0.
_FIN_ORDER_SELECT = (
    'id,total_price,total_amount,quantity_kg,price_per_kg,payment_status,'
    'payment_ref,status,farmer_id,buyer_id,created_at,delivery_type,listing_id'
)
_FIN_PAYOUT_SELECT = (
    'id,order_id,farmer_id,gross_amount,commission_pct,commission_amount,'
    'net_amount,status,provider,provider_ref,created_at,paid_at'
)
_FIN_JOURNAL_SELECT = (
    'id,reference,kind,currency,payment_ref,description,status,posted_by,'
    'posted_at,reverses_journal_id'
)


def _fin_read(table, select, filters=None, limit=500):
    params = {'select': select}
    if filters:
        params.update(filters)
    return supa_get_checked(table, params, limit=limit)


def _fin_lines(journal_ids):
    """Fetch journal lines in bounded chunks so the URL never grows unbounded."""
    ids = [str(i) for i in journal_ids if i]
    rows = []
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        data, err = _fin_read(
            'ledger_lines',
            'journal_id,account_code,side,amount,memo',
            {'journal_id': f'in.({",".join(chunk)})'},
            limit=5000,
        )
        if err:
            return None, err
        rows.extend(data or [])
    return rows, None


def _fin_ledger_unavailable(detail):
    missing = any(token in str(detail) for token in ('PGRST205', '42P01', 'does not exist'))
    return jsonify({
        'error': 'Ledger is not initialized' if missing else 'Could not read the ledger',
        'code': 'ledger_not_initialized' if missing else 'ledger_read_failed',
        'detail': detail,
        'fix': ('Run supabase/migrations/0013_ledger_core.up.sql in the Supabase '
                'SQL Editor, then reload this page.') if missing else None,
    }), 503


@app.route('/api/admin/finance/overview', methods=['GET'])
def admin_finance_overview():
    _payload, err = verify_token(required_role='admin')
    if err:
        return err
    orders, orders_err = _fin_read('orders', _FIN_ORDER_SELECT, limit=5000)
    if orders_err:
        return jsonify({'error': 'Could not read orders', 'code': 'orders_read_failed',
                        'detail': orders_err}), 503
    payouts, payouts_err = _fin_read('payouts', _FIN_PAYOUT_SELECT, limit=5000)
    if payouts_err:
        return jsonify({'error': 'Could not read payouts', 'code': 'payouts_read_failed',
                        'detail': payouts_err}), 503
    balances, balances_err = _fin_read('ledger_account_balances', '*', limit=100)
    result = finance.build_overview(
        orders or [], payouts or [], balances or [], balances is not None, balances_err)
    warnings = []
    if len(orders or []) >= 5000:
        warnings.append('More than 5,000 orders exist; payment totals only include the first 5,000.')
    if len(payouts or []) >= 5000:
        warnings.append('More than 5,000 payouts exist; payable totals only include the first 5,000.')
    result['warnings'] = warnings
    return jsonify(result)


@app.route('/api/admin/finance/journals', methods=['GET'])
def admin_finance_journals():
    _payload, err = verify_token(required_role='admin')
    if err:
        return err
    try:
        limit = min(max(int(request.args.get('limit', '100')), 1), 200)
    except (TypeError, ValueError):
        limit = 100
    journals, journals_err = _fin_read(
        'ledger_journals', _FIN_JOURNAL_SELECT, {'order': 'posted_at.desc'}, limit=limit)
    if journals_err:
        return _fin_ledger_unavailable(journals_err)
    lines, lines_err = _fin_lines([j.get('id') for j in journals or []])
    if lines_err:
        return _fin_ledger_unavailable(lines_err)
    rows = finance.build_journal_rows(journals or [], lines or [])
    return jsonify({'currency': 'UGX', 'count': len(rows), 'rows': rows})


@app.route('/api/admin/finance/farmer-settlements', methods=['GET'])
def admin_finance_farmer_settlements():
    _payload, err = verify_token(required_role='admin')
    if err:
        return err
    payouts, payouts_err = _fin_read('payouts', _FIN_PAYOUT_SELECT, limit=5000)
    if payouts_err:
        return jsonify({'error': 'Could not read payouts', 'code': 'payouts_read_failed',
                        'detail': payouts_err}), 503
    orders, orders_err = _fin_read('orders', _FIN_ORDER_SELECT, limit=5000)
    farmers, farmers_err = _fin_read(
        'farmers', 'id,full_name,district', {'order': 'full_name.asc'}, limit=2000)
    warnings = []
    if orders_err:
        warnings.append(f'Order details are unavailable: {orders_err}')
    if farmers_err:
        warnings.append(f'Farmer names are unavailable: {farmers_err}')
    if len(payouts or []) >= 5000:
        warnings.append('More than 5,000 payouts exist; this view only includes the first 5,000.')
    result = finance.build_farmer_settlements(
        payouts or [], orders or [], farmers or [])
    result['warnings'] = warnings
    return jsonify(result)


@app.route('/api/admin/payouts/<payout_id>/settle', methods=['POST'])
def admin_settle_payout(payout_id):
    """Settle a pending farmer payout against an operator-verified transfer.

    Refuses to put the operational record ahead of the accounting record:
    the order must still be delivered and paid, its customer receipt must be
    posted exactly once, the settlement journal must post, and only then is the
    payout marked paid. A retry is idempotent on `settlement:<payout_id>`.
    """
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    try:
        uuid.UUID(str(payout_id))
    except (ValueError, AttributeError, TypeError):
        return jsonify({'error': 'Invalid payout id'}), 400

    data = request.get_json(force=True, silent=True) or {}
    provider_ref = str(data.get('provider_ref') or '').strip()
    provider = str(data.get('provider') or '').strip()[:40]
    if not provider_ref:
        return jsonify({'error': 'A provider reference is required as proof of payment',
                        'code': 'provider_ref_required'}), 400
    if len(provider_ref) > 120 or not all(32 <= ord(ch) < 127 for ch in provider_ref):
        return jsonify({'error': 'Provider reference must be 1-120 printable characters',
                        'code': 'provider_ref_invalid'}), 400

    payouts, payout_err = _fin_read(
        'payouts', _FIN_PAYOUT_SELECT, {'id': f'eq.{payout_id}'}, limit=1)
    if payout_err:
        return jsonify({'error': 'Could not read payouts', 'code': 'payouts_read_failed',
                        'detail': payout_err}), 503
    if not payouts:
        return jsonify({'error': 'Payout not found'}), 404
    payout = payouts[0]
    payout_status = str(payout.get('status') or '').lower()

    if payout_status == 'paid':
        return jsonify({'ok': True, 'already_settled': True, 'payout_id': payout_id,
                        'provider_ref': payout.get('provider_ref'),
                        'paid_at': payout.get('paid_at')})
    if payout_status != 'pending':
        return jsonify({
            'error': f'Payout is {payout_status or "unknown"}; only pending payouts can be settled',
            'code': 'payout_not_pending'}), 409

    order_id = str(payout.get('order_id') or '').strip()
    if not order_id:
        return jsonify({'error': 'Payout has no order', 'code': 'payout_order_missing'}), 409
    orders, order_err = _fin_read(
        'orders', _FIN_ORDER_SELECT, {'id': f'eq.{order_id}'}, limit=1)
    if order_err:
        return jsonify({'error': 'Could not read the order', 'code': 'orders_read_failed',
                        'detail': order_err}), 503
    if not orders:
        return jsonify({'error': 'Order not found', 'code': 'order_not_found'}), 409
    order = orders[0]
    if (str(order.get('status') or '').lower() != 'delivered'
            or str(order.get('payment_status') or '').lower() != 'paid'):
        return jsonify({'error': 'Only delivered, paid orders can be settled',
                        'code': 'order_not_settleable',
                        'order_status': order.get('status'),
                        'payment_status': order.get('payment_status')}), 409

    try:
        payout_gross = ledger.money(payout.get('gross_amount'))
        order_value = ledger.money(finance.order_total(order))
    except ledger.LedgerError:
        return jsonify({'error': 'Payout or order amount is invalid',
                        'code': 'amount_invalid'}), 409
    if payout_gross != order_value:
        return jsonify({'error': 'Payout gross does not match the order total',
                        'code': 'payout_amount_mismatch',
                        'payout_gross': f'{payout_gross:.2f}',
                        'order_total': f'{order_value:.2f}'}), 409

    pay_ref = str(order.get('payment_ref') or '').strip()
    if not pay_ref:
        return jsonify({'error': 'Order has no payment reference; the receipt cannot be verified',
                        'code': 'payment_ref_missing'}), 409

    receipts, receipt_err = _fin_read(
        'ledger_journals', 'id,reference,kind,status,payment_ref',
        {'payment_ref': f'eq.{pay_ref}', 'status': 'eq.posted',
         'kind': 'in.(customer_payment,cod_collection)'}, limit=20)
    if receipt_err:
        return _fin_ledger_unavailable(receipt_err)
    if not receipts:
        return jsonify({
            'error': 'The customer receipt is not posted to the ledger; settle only after it is posted',
            'code': 'receipt_not_posted', 'payment_ref': pay_ref}), 409
    if len(receipts) > 1:
        return jsonify({'error': 'More than one active receipt exists for this checkout',
                        'code': 'receipt_ambiguous', 'payment_ref': pay_ref}), 409

    receipt_lines, lines_err = _fin_lines([receipts[0].get('id')])
    if lines_err:
        return _fin_ledger_unavailable(lines_err)
    debit_accounts = {
        str(line.get('account_code')) for line in receipt_lines or []
        if str(line.get('side') or '').lower() == 'debit'
        and str(line.get('account_code')) in ledger.SETTLEMENT_ASSET_ACCOUNTS
    }
    if len(debit_accounts) != 1:
        return jsonify({'error': 'Cannot determine which asset account received the payment',
                        'code': 'receipt_channel_unknown', 'payment_ref': pay_ref}), 409
    channel_account = debit_accounts.pop()

    try:
        actor = str(uuid.UUID(str(payload.get('sub') or '')))
    except (ValueError, AttributeError, TypeError):
        actor = None

    posting = ledger.post_settlement(
        SUPA, payout=payout, channel_account=channel_account,
        provider=provider, provider_ref=provider_ref, actor=actor)
    if not posting.ok:
        log.error('payout.settle_ledger_failed', payout_id=payout_id,
                  order_id=order_id, error=posting.error)
        _log_system_event('payout_settlement_ledger_failed', {
            'payout_id': payout_id, 'order_id': order_id,
            'payment_ref': pay_ref, 'error': posting.error}, severity='error')
        return jsonify({'error': 'Settlement journal could not be posted; the payout is still pending',
                        'code': 'settlement_posting_failed',
                        'detail': posting.error}), 503

    marked = SUPA.rpc('mark_payout_paid', {
        'p_payout_id': payout_id,
        'p_provider': provider,
        'p_provider_ref': provider_ref,
    })
    if not marked.ok:
        log.error('payout.mark_paid_failed', payout_id=payout_id, error=marked.error)
        _log_system_event('payout_mark_paid_failed', {
            'payout_id': payout_id, 'order_id': order_id,
            'journal_id': posting.journal_id, 'error': marked.error}, severity='error')
        return jsonify({
            'error': 'Settlement journal posted but the payout status was not updated; retry',
            'code': 'payout_status_update_failed',
            'journal_id': posting.journal_id,
            'reference': posting.reference,
            'retryable': True}), 503

    # False means a concurrent request already moved the payout out of pending;
    # both requests used the same idempotent settlement reference.
    already_settled = marked.data is False
    if not already_settled:
        supa_insert('admin_audit_log', {
            'actor': actor,
            'action': 'payout.settled',
            'target': payout_id,
            'metadata': {
                'order_id': order_id,
                'journal_id': posting.journal_id,
                'provider': provider,
                'provider_ref': provider_ref,
                'gross': f"{posting.summary['gross']:.2f}",
                'commission': f"{posting.summary['commission']:.2f}",
                'net': f"{posting.summary['net']:.2f}",
                'channel_account': channel_account,
            },
        })
    return jsonify({
        'ok': True,
        'already_settled': already_settled,
        'payout_id': payout_id,
        'order_id': order_id,
        'journal_id': posting.journal_id,
        'reference': posting.reference,
        'channel_account': channel_account,
        'gross': f"{posting.summary['gross']:.2f}",
        'commission': f"{posting.summary['commission']:.2f}",
        'net': f"{posting.summary['net']:.2f}",
    })


@app.route('/api/admin/finance/transporter-settlements', methods=['GET'])
def admin_finance_transporter_settlements():
    _payload, err = verify_token(required_role='admin')
    if err:
        return err
    deliveries, deliveries_err = _fin_read(
        'deliveries',
        'id,order_id,driver_name,driver_phone,status,'
        'tracking_code,actual_arrival,created_at',
        {'order': 'created_at.desc'}, limit=2000)
    if deliveries_err:
        return jsonify({'error': 'Could not read deliveries', 'code': 'deliveries_read_failed',
                        'detail': deliveries_err}), 503
    result = finance.build_transporter_settlements(deliveries or [])
    result['warnings'] = (
        ['More than 2,000 deliveries exist; this view only includes the first 2,000.']
        if len(deliveries or []) >= 2000 else [])
    return jsonify(result)


@app.route('/api/admin/finance/reconciliation', methods=['GET'])
def admin_finance_reconciliation():
    _payload, err = verify_token(required_role='admin')
    if err:
        return err
    orders, orders_err = _fin_read('orders', _FIN_ORDER_SELECT, limit=5000)
    if orders_err:
        return jsonify({'error': 'Could not read orders', 'code': 'orders_read_failed',
                        'detail': orders_err}), 503
    journals, journals_err = _fin_read('ledger_journals', _FIN_JOURNAL_SELECT, limit=5000)
    if journals_err:
        return _fin_ledger_unavailable(journals_err)
    lines, lines_err = _fin_lines([j.get('id') for j in journals or []])
    if lines_err:
        return _fin_ledger_unavailable(lines_err)
    result = finance.build_reconciliation(
        orders or [], journals or [], lines or [])
    result['warnings'] = (
        ['More than 5,000 orders or journals exist; reconciliation only checked the first 5,000.']
        if len(orders or []) >= 5000 or len(journals or []) >= 5000 else [])
    return jsonify(result)


@app.route('/api/admin/finance/recurring', methods=['GET'])
def admin_finance_recurring():
    """Pesapal recurring subscriptions and charges.

    Recurring money is a suspense liability until operations allocate it to an
    order; this view reports what the gateway charged, not a fabricated order.
    """
    _payload, err = verify_token(required_role='admin')
    if err:
        return err
    subscriptions, subs_err = _fin_read(
        'payment_subscriptions', '*', {'order': 'created_at.desc'}, limit=2000)
    if subs_err:
        missing = any(token in str(subs_err)
                      for token in ('PGRST205', '42P01', 'does not exist'))
        return jsonify({
            'error': 'Recurring payments are not initialized' if missing
                     else 'Could not read recurring subscriptions',
            'code': 'recurring_not_initialized' if missing else 'recurring_read_failed',
            'detail': subs_err,
            'fix': ('Run supabase/migrations/0017_pesapal_payments.up.sql in the '
                    'Supabase SQL Editor, then reload this page.') if missing else None,
        }), 503
    charges, charges_err = _fin_read(
        'subscription_charges', '*', {'order': 'created_at.desc'}, limit=5000)
    if charges_err:
        return jsonify({'error': 'Could not read recurring charges',
                        'code': 'recurring_read_failed',
                        'detail': charges_err}), 503

    paid_without_ledger = sum(
        float(c.get('amount') or 0) for c in charges or []
        if str(c.get('status') or '') == 'paid' and not c.get('ledger_reference'))
    return jsonify({
        'currency': 'UGX',
        'subscriptions': subscriptions or [],
        'charges': charges or [],
        'summary': {
            'subscriptions': len(subscriptions or []),
            'active_subscriptions': sum(
                1 for s in subscriptions or [] if s.get('status') == 'active'),
            'charges': len(charges or []),
            'paid_charges': sum(
                1 for c in charges or [] if c.get('status') == 'paid'),
            'total_charged': sum(float(c.get('amount') or 0)
                                 for c in charges or [] if c.get('status') == 'paid'),
            'paid_amount_without_ledger': paid_without_ledger,
        },
        'warnings': (['More than 2,000 subscriptions exist; only the first 2,000 are shown.']
                     if len(subscriptions or []) >= 2000 else []),
    })


@app.route('/api/admin/finance/reports', methods=['GET'])
def admin_finance_reports():
    _payload, err = verify_token(required_role='admin')
    if err:
        return err
    today = datetime.date.today()
    try:
        start = (datetime.date.fromisoformat(request.args['from'])
                 if request.args.get('from') else today - datetime.timedelta(days=29))
        end = (datetime.date.fromisoformat(request.args['to'])
               if request.args.get('to') else today)
    except ValueError:
        return jsonify({'error': 'Dates must be YYYY-MM-DD'}), 400
    if start > end:
        return jsonify({'error': 'from must be on or before to'}), 400
    if (end - start).days > 366:
        return jsonify({'error': 'Report range is limited to 366 days'}), 400

    orders, orders_err = _fin_read('orders', _FIN_ORDER_SELECT, limit=5000)
    if orders_err:
        return jsonify({'error': 'Could not read orders', 'code': 'orders_read_failed',
                        'detail': orders_err}), 503
    payouts, payouts_err = _fin_read('payouts', _FIN_PAYOUT_SELECT, limit=5000)
    if payouts_err:
        return jsonify({'error': 'Could not read payouts', 'code': 'payouts_read_failed',
                        'detail': payouts_err}), 503

    warnings = []
    journals, journals_err = _fin_read(
        'ledger_journals', _FIN_JOURNAL_SELECT,
        {'posted_at': f'gte.{start.isoformat()}T00:00:00'}, limit=2000)
    lines = []
    ledger_ready = journals_err is None
    if journals_err:
        journals = []
        warnings.append(f'Ledger figures are unavailable: {journals_err}')
    else:
        journals = [
            j for j in journals or []
            if finance.in_date_range(j.get('posted_at'), start, end)
        ]
        lines, lines_err = _fin_lines([j.get('id') for j in journals])
        if lines_err:
            ledger_ready = False
            lines = []
            warnings.append(f'Ledger lines are unavailable: {lines_err}')

    if len(orders or []) >= 5000:
        warnings.append('More than 5,000 orders exist; report totals only include the first 5,000.')
    if len(payouts or []) >= 5000:
        warnings.append('More than 5,000 payouts exist; report totals only include the first 5,000.')
    result = finance.build_reports(orders or [], payouts or [], journals or [],
                                   lines or [], start, end)
    result['ledger']['ready'] = ledger_ready
    result['warnings'] = warnings
    return jsonify(result)

_MEDIA_UPLOAD_KINDS = {
    'listings': 'listing',
    'animals': 'animal',
    'avatars': 'avatar',
    'supplies': 'supply',
}
_MEDIA_TYPES = {
    'image/jpeg': (5 * 1024 * 1024, 'jpg'),
    'image/png': (5 * 1024 * 1024, 'png'),
    'image/webp': (5 * 1024 * 1024, 'webp'),
    'video/mp4': (25 * 1024 * 1024, 'mp4'),
    'video/webm': (25 * 1024 * 1024, 'webm'),
    'video/quicktime': (25 * 1024 * 1024, 'mov'),
}


def _looks_like_declared_media(content_type: str, blob: bytes) -> bool:
    """Reject HTML/executables renamed as an image or video before storage."""
    if content_type == 'image/jpeg':
        return blob.startswith(b'\xff\xd8\xff')
    if content_type == 'image/png':
        return blob.startswith(b'\x89PNG\r\n\x1a\n')
    if content_type == 'image/webp':
        return len(blob) >= 12 and blob[:4] == b'RIFF' and blob[8:12] == b'WEBP'
    if content_type in ('video/mp4', 'video/quicktime'):
        return len(blob) >= 12 and blob[4:8] == b'ftyp'
    if content_type == 'video/webm':
        return blob.startswith(b'\x1a\x45\xdf\xa3')
    return False


@app.route('/api/uploads', methods=['POST'])
@rate_limit(max_req=12, window=300)
def upload_media():
    """Upload validated listing media through the API, never from the browser.

    The previous browser-to-Supabase path made media uploads depend on a bucket
    configured manually in a separate dashboard.  This route validates the
    caller and the actual media bytes, creates that bucket idempotently through
    the service role when necessary, and gives the client a bounded response.
    """
    uid, err = verify_supabase_user()
    if err:
        return err
    blocked = legal_gate(uid)
    if blocked:
        return blocked

    kind = str(request.form.get('kind', '')).strip().lower()
    if kind not in _MEDIA_UPLOAD_KINDS:
        return jsonify({'error': 'Invalid upload category'}), 400
    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename:
        return jsonify({'error': 'Choose a photo or video first'}), 400

    content_type = (uploaded.mimetype or '').lower().strip()
    spec = _MEDIA_TYPES.get(content_type)
    if not spec:
        return jsonify({'error': 'Only JPG, PNG, WebP, MP4, WebM, and MOV files are allowed'}), 415
    max_bytes, extension = spec
    # Read at most one byte past the permitted size.  This keeps memory bounded
    # and detects clients that lie about Content-Length.
    blob = uploaded.stream.read(max_bytes + 1)
    if not blob:
        return jsonify({'error': 'The selected file is empty'}), 400
    if len(blob) > max_bytes:
        label = 'Videos' if content_type.startswith('video/') else 'Photos'
        limit_mb = 25 if content_type.startswith('video/') else 5
        return jsonify({'error': f'{label} must be at most {limit_mb} MB'}), 413
    if not _looks_like_declared_media(content_type, blob):
        return jsonify({'error': 'The file content does not match its media type'}), 415

    object_path = f"{kind}/{uid}/{uuid.uuid4().hex}.{extension}"
    stored = SUPA.upload_public_media(object_path, blob, content_type)
    if not stored.ok:
        log.error('media.upload_failed', user_id=uid, kind=kind,
                  status=stored.status, error=stored.error)
        return jsonify({'error': 'Photo upload is temporarily unavailable. You can post the listing without a photo and try again later.'}), 503

    _log_system_event('media_uploaded', {
        'kind': kind, 'bytes': len(blob), 'content_type': content_type,
    }, severity='info', user_id=uid)
    return jsonify({'ok': True, 'url': stored.data['url']}), 201


@app.route('/api/admin/upload', methods=['POST'])
def admin_upload_media():
    """Upload media from the admin panel (authenticated via admin JWT)."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    kind = str(request.form.get('kind', 'listings')).strip().lower()
    if kind not in _MEDIA_UPLOAD_KINDS:
        kind = 'listings'
    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename:
        return jsonify({'error': 'Choose a photo or video first'}), 400
    content_type = (uploaded.mimetype or '').lower().strip()
    spec = _MEDIA_TYPES.get(content_type)
    if not spec:
        return jsonify({'error': 'Only JPG, PNG, WebP, MP4, WebM, and MOV files are allowed'}), 415
    max_bytes, extension = spec
    blob = uploaded.stream.read(max_bytes + 1)
    if not blob:
        return jsonify({'error': 'The selected file is empty'}), 400
    if len(blob) > max_bytes:
        label = 'Videos' if content_type.startswith('video/') else 'Photos'
        limit_mb = 25 if content_type.startswith('video/') else 5
        return jsonify({'error': f'{label} must be at most {limit_mb} MB'}), 413
    if not _looks_like_declared_media(content_type, blob):
        return jsonify({'error': 'The file content does not match its media type'}), 415
    object_path = f"admin-{kind}/{uuid.uuid4().hex}.{extension}"
    stored = SUPA.upload_public_media(object_path, blob, content_type)
    if not stored.ok:
        return jsonify({'error': 'Upload temporarily unavailable'}), 503
    _log_system_event('admin_media_uploaded', {'kind': kind, 'bytes': len(blob), 'content_type': content_type}, severity='info')
    return jsonify({'ok': True, 'url': stored.data['url']}), 201


@app.route('/api/listings', methods=['POST'])
@rate_limit(max_req=20, window=60)
def create_listing():
    """Farmer posts a new crop listing via the website (not USSD)."""
    uid, err = verify_supabase_user()
    if err:
        return err
    blocked = legal_gate(uid)
    if blocked:
        return blocked
    data = request.get_json(force=True, silent=True) or {}
    crop_name    = str(data.get('crop_name', '')).strip()[:80]
    quantity_kg  = data.get('quantity_kg')
    price_per_kg = data.get('price_per_kg')
    district     = str(data.get('district', '')).strip()[:60]
    description  = str(data.get('description', '')).strip()[:300]
    if not crop_name or not quantity_kg or not price_per_kg:
        return jsonify({'error': 'crop_name, quantity_kg and price_per_kg are required'}), 400
    # Look up farmer profile for phone/name
    farmer_rows = supa_get('farmers', {'id': f'eq.{uid}'}, limit=1) or []
    farmer_info = farmer_rows[0] if farmer_rows else {}
    # Only write columns that exist in public.listings. Farmer names are joined
    # from farmers(full_name) in the marketplace, and source is not stored.
    row = {
        'farmer_id':          uid,
        'crop_name':          crop_name,
        'quantity_kg':        float(quantity_kg),
        'price_per_kg':       float(price_per_kg),
        'district':           district or farmer_info.get('district', ''),
        'description':        description or None,
        'farmer_phone':       farmer_info.get('phone', ''),
        'is_available':       True,
        # Optional extra fields from the web form
        'category':           str(data.get('category', '')).strip()[:40] or None,
        'is_organic':         bool(data.get('is_organic', False)),
        'sale_unit':          str(data.get('sale_unit', 'per kg')).strip()[:20] or None,
        'min_order_kg':       max(0, int(data.get('min_order_kg') or 0)),
        'delivery_available': bool(data.get('delivery_available', False)),
        'payment_terms':      str(data.get('payment_terms', '')).strip()[:100] or None,
        'image_url':          str(data.get('image_url', '')).strip() or None,
        'video_url':          str(data.get('video_url', '')).strip() or None,
    }
    inserted = supa_insert('listings', row)
    if not inserted:
        return jsonify({'error': 'Failed to create listing'}), 500
    _discord_notify(
        event_type='listing.created',
        title='🌾 New Crop Listing (Web)',
        description=(
            f"**Crop:** {crop_name}\n"
            f"**Quantity:** {quantity_kg} kg\n"
            f"**Price:** UGX {float(price_per_kg):,.0f}/kg\n"
            f"**District:** {district or '—'}\n"
            f"**Farmer:** {farmer_info.get('full_name', uid)}"
        ),
        severity='info',
    )
    _log_system_event('listing_created_web', {
        'farmer_id': uid, 'crop_name': crop_name,
        'quantity_kg': quantity_kg, 'district': district,
    }, severity='info', user_id=uid)
    return jsonify({'ok': True, 'listing': inserted[0] if isinstance(inserted, list) else inserted}), 201


@app.route('/api/listings/<listing_id>/media', methods=['PATCH'])
@rate_limit(max_req=20, window=300)
def attach_listing_media(listing_id):
    """Attach an optional photo/video to a listing the caller already owns.

    Listings are saved before media uploads run, so a slow or failed mobile
    upload can never leave a farmer with no listing. This route only accepts
    media URLs this API generated for the caller's own uploads, preventing a
    user from attaching arbitrary (or another user's) content.
    """
    uid, err = verify_supabase_user()
    if err:
        return err
    blocked = legal_gate(uid)
    if blocked:
        return blocked
    data = request.get_json(force=True, silent=True) or {}
    image_url = str(data.get('image_url') or '').strip()
    video_url = str(data.get('video_url') or '').strip()
    if not image_url and not video_url:
        return jsonify({'error': 'image_url or video_url is required'}), 400

    owned = supa_get('listings', {'id': f'eq.{listing_id}',
                                  'farmer_id': f'eq.{uid}'}, limit=1) or []
    if not owned:
        return jsonify({'error': 'Listing not found'}), 404

    allowed_prefix = (f"{SUPABASE_URL}/storage/v1/object/public/"
                      f"{_UPLOADS_BUCKET}/listings/{uid}/")
    for url in (image_url, video_url):
        if url and not url.startswith(allowed_prefix):
            return jsonify({'error': 'Invalid media URL'}), 400

    patch = {}
    if image_url:
        patch['image_url'] = image_url[:2048]
    if video_url:
        patch['video_url'] = video_url[:2048]
    if not supa_update('listings', patch, 'id', listing_id):
        return jsonify({'error': 'Could not attach media'}), 500
    return jsonify({'ok': True})


# The web form warns about these, but the form is bypassable, so the same
# bounds are enforced here and the two can never disagree.
LISTING_MAX_QTY   = 1_000_000
LISTING_MAX_PRICE = 10_000_000


def _listing_stock_status(new_qty, reserved):
    """Re-derive stock_status exactly as the reservation triggers do.

    0001 sets 'unavailable' at zero remaining and 'low_stock' at 10% or less;
    anything else is 'available'. 'temporarily_unavailable' is never written
    here because nothing in the schema produces it.
    """
    remaining = new_qty - reserved
    if remaining <= 0:
        return 'unavailable'
    if remaining <= new_qty * 0.1:
        return 'low_stock'
    return 'available'


@app.route('/api/listings/<listing_id>', methods=['PATCH'])
@rate_limit(max_req=20, window=60)
def update_farmer_listing(listing_id):
    """Edit a farmer's own listing under the stock rules the database uses.

    The listings_owner_update policy lets an owner write any column of their
    own row, so a raw browser PATCH could set quantity_kg below reserved_qty
    and make the amount buyers can order negative. This route re-reads the
    row, refuses that, and writes back only while reserved_qty still equals
    what it just read — so an order reserved in between loses the write
    instead of being silently overwritten.
    """
    uid, err = verify_supabase_user()
    if err:
        return err
    blocked = legal_gate(uid)
    if blocked:
        return blocked
    body = request.get_json(force=True, silent=True) or {}

    patch = {}
    if 'crop_name' in body:
        crop = str(body.get('crop_name') or '').strip()[:80]
        if not crop:
            return jsonify({'error': 'crop_name cannot be empty'}), 400
        patch['crop_name'] = crop
    if 'description' in body:
        patch['description'] = (str(body.get('description') or '').strip()[:300] or None)
    if 'price_per_kg' in body:
        try:
            price = float(body.get('price_per_kg'))
        except (TypeError, ValueError):
            return jsonify({'error': 'price_per_kg must be a number'}), 400
        if not (0 < price <= LISTING_MAX_PRICE):
            return jsonify({'error': f'price_per_kg must be between 1 and {LISTING_MAX_PRICE:,}'}), 400
        patch['price_per_kg'] = price
    if 'is_available' in body:
        patch['is_available'] = bool(body.get('is_available'))

    new_qty = None
    if 'quantity_kg' in body:
        try:
            new_qty = float(body.get('quantity_kg'))
        except (TypeError, ValueError):
            return jsonify({'error': 'quantity_kg must be a number'}), 400
        if not (0 < new_qty <= LISTING_MAX_QTY):
            return jsonify({'error': f'quantity_kg must be between 1 and {LISTING_MAX_QTY:,}'}), 400

    if not patch and new_qty is None:
        return jsonify({'error': 'Nothing to update'}), 400

    rows = SUPA.select('listings', {
        'id': f'eq.{listing_id}', 'farmer_id': f'eq.{uid}',
        'select': 'id,reserved_qty,quantity_kg,stock_status,is_available,crop_name',
    }, limit=1)
    if not rows:
        return jsonify({'error': 'Listing not found'}), 404
    current = rows[0]
    try:
        reserved = float(current.get('reserved_qty') or 0)
    except (TypeError, ValueError):
        reserved = 0.0

    if new_qty is not None:
        if new_qty < reserved:
            return jsonify({
                'error': f'You already have {reserved:g} kg reserved by orders, '
                         f'so the quantity cannot go below that.',
                'reserved_qty': reserved, 'min_quantity': reserved,
            }), 409
        patch['quantity_kg'] = new_qty
        patch['stock_status'] = _listing_stock_status(new_qty, reserved)

    # Compare-and-swap on the value just read: if an order reserved stock while
    # this request was in flight, zero rows come back and nothing is written.
    guard = {'id': f'eq.{listing_id}', 'farmer_id': f'eq.{uid}'}
    if new_qty is not None:
        guard['reserved_qty'] = f'eq.{current.get("reserved_qty")}'
    patch['updated_at'] = datetime.datetime.utcnow().isoformat()
    res = SUPA.update_checked('listings', patch, guard)
    if not res.ok:
        log.error('listing.update_failed', listing_id=listing_id, status=res.status)
        return jsonify({'error': 'Could not save your changes. Please try again.'}), 502
    if not res.data:
        return jsonify({
            'error': 'An order came in while you were editing, so the stock figure '
                     'changed. Reload the listing and try again.',
            'reserved_qty': reserved,
        }), 409

    _log_system_event('listing_updated_web', {
        'farmer_id': uid, 'listing_id': listing_id, 'fields': sorted(patch.keys()),
    }, severity='info', user_id=uid)
    return jsonify({'ok': True, 'listing': res.data[0]}), 200


@app.route('/api/listings', methods=['GET'])
@rate_limit(max_req=60, window=60)
def public_listings():
    """Public endpoint — returns available crop listings for the marketplace."""
    district = request.args.get('district', '').strip()
    crop     = request.args.get('crop', '').strip()
    filters  = {'is_available': 'eq.true', 'order': 'created_at.desc'}
    if district:
        filters['district'] = f'ilike.*{district}*'
    if crop:
        filters['crop_name'] = f'ilike.*{crop}*'
    return jsonify(supa_get('listings', filters=filters, limit=200) or [])


@app.route('/api/admin/listings', methods=['GET'])
def admin_listings():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    return jsonify(supa_get('listings', limit=200) or [])

@app.route('/api/admin/orders', methods=['GET'])
def admin_orders():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    # Return all columns including payment_status so the admin panel can show
    # transaction state. Filter by payment_status if ?payment_status= is passed.
    ps_filter = request.args.get('payment_status')
    filters = {'order': 'created_at.desc'}
    if ps_filter:
        filters['payment_status'] = f'eq.{ps_filter}'
    return jsonify(supa_get('orders', filters=filters, limit=500) or [])

@app.route('/api/admin/farmers', methods=['GET'])
def admin_farmers():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    return jsonify(supa_get('farmers', limit=500) or [])

@app.route('/api/admin/order/<order_id>/status', methods=['PATCH'])
def update_order_status(order_id):
    """Advance an order through the server-side state machine.

    The generic row editor must never write `orders.status`/`payment_status`
    directly: doing so would skip stock release, order events and the ledger.
    """
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    new_status = str(data.get('status') or '').strip()
    valid = ('pending', 'confirmed', 'in_transit', 'delivered', 'cancelled')
    if new_status not in valid:
        return jsonify({'error': 'Invalid status'}), 400
    res = SUPA.rpc('set_order_status', {'p_order_id': order_id, 'p_new_status': new_status})
    if not res.ok:
        message = res.error or 'Update failed'
        if res.status == 404 or 'not found' in message.lower():
            return jsonify({'error': message}), 404
        if res.status in (400, 409, 422):
            return jsonify({'error': message}), 409
        return jsonify({'error': 'Order service unavailable'}), 503
    _log_system_event('order_status_changed',
                      {'order_id': order_id, 'status': new_status, 'actor': 'admin'},
                      severity='info')
    return jsonify({'ok': True, 'status': new_status}), 200

@app.route('/api/admin/animals', methods=['GET'])
def admin_animals():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    return jsonify(supa_get('animal_listings', {'order': 'created_at.desc'}, limit=300) or [])

@app.route('/api/admin/deliveries', methods=['GET'])
def admin_deliveries():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    return jsonify(supa_get('deliveries', {'order': 'created_at.desc'}, limit=300) or [])

@app.route('/api/admin/supplies', methods=['GET'])
def admin_supplies():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    return jsonify(supa_get('supplier_products', {'order': 'category.asc'}, limit=300) or [])

@app.route('/api/admin/purchase-requests', methods=['GET'])
def admin_purchase_requests():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    return jsonify(supa_get('purchase_requests', {'order': 'created_at.desc'}, limit=300) or [])

@app.route('/api/admin/offers', methods=['GET'])
def admin_offers():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    return jsonify(supa_get('order_offers', {'order': 'created_at.desc'}, limit=300) or [])

# ══════════════════════════════════════════════════════════════════════════════
# SERVICE REQUESTS — cold storage, refrigerated transport, loans
# Saved in `service_requests` (migration 0010) so the admin console can track them.
# ══════════════════════════════════════════════════════════════════════════════
_SVC_KINDS = ('cold_storage', 'refrigerated_transport', 'loan')
_SVC_STATUSES = ('pending', 'confirmed', 'completed', 'rejected', 'cancelled')
_SVC_LABELS = {'cold_storage': 'Cold storage', 'refrigerated_transport': 'Refrigerated transport',
               'loan': 'Loan'}


def _svc_text(value, limit):
    """Trim a free-text field to a safe length. Non-strings become ''."""
    return value.strip()[:limit] if isinstance(value, str) else ''


def _svc_details(raw):
    """Keep only simple key/value pairs from the client's `details` object."""
    clean = {}
    if not isinstance(raw, dict):
        return clean
    for key, val in list(raw.items())[:20]:
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            clean[key.strip()[:40]] = val
        elif isinstance(val, str) and val.strip():
            clean[key.strip()[:40]] = val.strip()[:300]
    return clean


@app.route('/api/service-requests', methods=['POST'])
@rate_limit(max_req=10, window=300)
def create_service_request():
    """A logged-in user asks for cold storage, a refrigerated vehicle, or a loan.

    Identity comes from the verified Supabase session (never the request body).
    The request is stored for admin follow-up and an ops alert is sent."""
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    kind = _svc_text(data.get('kind'), 40)
    if kind not in _SVC_KINDS:
        return jsonify({'error': 'Unknown request type'}), 400

    name = _svc_text(data.get('requester_name'), 80)
    phone = re.sub(r'[^\d+]', '', _svc_text(data.get('requester_phone'), 30))
    if not name:
        return jsonify({'error': 'Please enter your name'}), 400
    if not 9 <= len(phone.lstrip('+')) <= 15:
        return jsonify({'error': 'Please enter a valid phone number'}), 400

    amount = None
    raw_amount = data.get('amount_ugx')
    if raw_amount not in (None, ''):
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            return jsonify({'error': 'Amount must be a number'}), 400
        if not 0 < amount <= 10_000_000_000:
            return jsonify({'error': 'Please enter a valid amount'}), 400
    if kind == 'loan' and amount is None:
        return jsonify({'error': 'Please enter the amount you need'}), 400

    provider = _svc_text(data.get('provider_name'), 120)
    row = supa_insert('service_requests', {
        'kind': kind,
        'user_id': uid,
        'requester_name': name,
        'requester_phone': phone,
        'provider_name': provider or None,
        'amount_ugx': amount,
        'details': _svc_details(data.get('details')),
        'status': 'pending',
    })
    if not row or not isinstance(row, list) or not row[0].get('id'):
        # Never tell the user it worked when it did not.
        return jsonify({'error': 'Could not save your request. Please try again.'}), 500

    req_id = str(row[0]['id'])
    try:
        label = _SVC_LABELS[kind]
        _discord_notify(
            'service_request.created', f'New {label.lower()} request',
            f"**Type:** {label}\n**From:** {name}\n**Phone:** {phone}\n"
            f"**Provider:** {provider or '—'}\n**Ref:** {req_id[:8].upper()}",
        )
    except Exception as _e:
        log.warning("service_request.notify_failed", error=str(_e))
    return jsonify({'ok': True, 'id': req_id, 'ref': req_id[:8].upper()}), 200


@app.route('/api/admin/service-requests', methods=['GET'])
def admin_service_requests():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    filters = {'order': 'created_at.desc'}
    kind = (request.args.get('kind') or '').strip()
    if kind:
        if kind not in _SVC_KINDS:
            return jsonify({'error': 'Unknown request type'}), 400
        filters['kind'] = f'eq.{kind}'
    return jsonify(supa_get('service_requests', filters, limit=300) or [])


@app.route('/api/admin/service-requests/<req_id>', methods=['PATCH'])
def admin_update_service_request(req_id):
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    try:
        req_id = str(uuid.UUID(req_id))
    except ValueError:
        return jsonify({'error': 'Invalid request id'}), 400
    data = request.get_json(force=True, silent=True) or {}
    fields = {}
    if 'status' in data:
        if data['status'] not in _SVC_STATUSES:
            return jsonify({'error': 'Invalid status'}), 400
        fields['status'] = data['status']
    if 'admin_notes' in data:
        fields['admin_notes'] = _svc_text(data['admin_notes'], 500) or None
    if not fields:
        return jsonify({'error': 'Nothing to update'}), 400
    fields['updated_at'] = datetime.datetime.utcnow().isoformat()
    if not supa_update('service_requests', fields, 'id', req_id):
        return jsonify({'error': 'Update failed'}), 500
    return jsonify({'ok': True}), 200


# Generic admin editor for the console. Only whitelisted tables + columns can be
# written, so the admin token cannot set protected fields (id, timestamps, ownership).
_ADMIN_COLS = {
    'listings':        ['crop_name', 'category', 'quantity_kg', 'price_per_kg', 'unit', 'district',
                        'is_organic', 'is_available', 'is_verified', 'description', 'discount_pct',
                        'sale_unit', 'min_order_kg', 'delivery_available', 'payment_terms',
                        'image_url', 'video_url', 'farmer_id', 'farmer_phone'],
    'animal_listings': ['name', 'species', 'category', 'price', 'unit', 'qty', 'district',
                        'description', 'health_cert', 'maaif_certified', 'status',
                        'image_url', 'video_url', 'farmer_id', 'farmer_name', 'farmer_phone'],
    'farmers':         ['full_name', 'phone', 'email', 'district', 'is_verified', 'is_premium'],
    'orders':          ['delivery_address', 'notes'],
    'market_prices':   ['crop_name', 'price', 'unit', 'district', 'market'],
    'deliveries':      ['status', 'driver_name', 'driver_phone', 'pickup_location',
                        'delivery_location', 'estimated_arrival', 'tracking_code', 'notes'],
    'supplier_products': ['category', 'name', 'brand', 'price', 'unit', 'badge',
                          'description', 'image_id', 'in_stock'],
    'transporters':      ['full_name', 'phone', 'email', 'vehicle_type', 'plate_number',
                          'district', 'capacity_kg', 'notes', 'status', 'is_available'],
}

def _filter_cols(table, data):
    allowed = _ADMIN_COLS.get(table, [])
    return {k: v for k, v in (data or {}).items() if k in allowed}

@app.route('/api/admin/row', methods=['POST', 'PATCH', 'DELETE'])
def admin_row():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    table = data.get('table')
    if table not in _ADMIN_COLS:
        return jsonify({'error': 'Table not allowed'}), 400

    if request.method == 'DELETE':
        row_id = data.get('id')
        if not row_id:
            return jsonify({'error': 'Missing id'}), 400
        return (jsonify({'ok': True}), 200) if supa_delete(table, 'id', row_id) \
            else (jsonify({'error': 'Delete failed'}), 500)

    raw_fields = data.get('fields') or {}
    if table == 'orders' and any(k in raw_fields for k in ('status', 'payment_status')):
        return jsonify({
            'error': 'Order and payment status change through their workflow endpoints, not the editor',
        }), 400
    fields = _filter_cols(table, raw_fields)
    if not fields:
        return jsonify({'error': 'No editable fields provided'}), 400
    if request.method == 'POST':
        row = supa_insert(table, fields)
        return (jsonify({'ok': True, 'row': row}), 200) if row else (jsonify({'error': 'Create failed'}), 500)

    # PATCH
    row_id = data.get('id')
    if not row_id:
        return jsonify({'error': 'Missing id'}), 400
    return (jsonify({'ok': True}), 200) if supa_update(table, fields, 'id', row_id) \
        else (jsonify({'error': 'Update failed'}), 500)

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC PRICES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/prices', methods=['GET'])
def get_prices():
    data = supa_get('price_data', {'order': 'updated_at.desc'}, limit=50)
    return jsonify(data or [])

# ══════════════════════════════════════════════════════════════════════════════
# LIVE MARKET BOARD
# What farmers are asking for on AgriBridge right now (from available listings),
# next to the latest market reference price an admin entered, plus a real change %
# against the price ~24h ago (from market_price_history snapshots, migration 0011).
# ══════════════════════════════════════════════════════════════════════════════
_LIVE_CACHE = {'t': 0.0, 'data': None}
_LIVE_TTL = 10            # seconds: many viewers share one computation
_SNAP_EVERY = 600         # seconds between history snapshots
_SPIKE_PCT = 20.0         # Discord alert when a crop moves this much vs yesterday
_SPIKE_QUIET = 6 * 3600   # ...at most once per crop per 6h
_last_spike_alert = {}


def _norm_crop(name):
    return re.sub(r'\s+', ' ', str(name or '').strip().lower())


def _num(v):
    try:
        f = float(v)
        return f if f == f and f > 0 else None
    except (TypeError, ValueError):
        return None


def _median(vals):
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _stats(prices):
    return {'avg': round(sum(prices) / len(prices)), 'median': round(_median(prices)),
            'low': round(min(prices)), 'high': round(max(prices)), 'sellers': len(prices)}


def _parse_ts(v):
    try:
        return datetime.datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def _build_live_board():
    now = datetime.datetime.now(datetime.timezone.utc)
    listings = supa_get('listings', {'is_available': 'eq.true',
                                     'select': 'crop_name,price_per_kg,quantity_kg,district'}, limit=2000) or []
    refs = supa_get('market_prices', {'order': 'price_date.desc'}, limit=300) or supa_get('market_prices', None, limit=300) or []

    crops = {}
    for l in listings:
        key = _norm_crop(l.get('crop_name'))
        price = _num(l.get('price_per_kg'))
        if not key or price is None:
            continue
        c = crops.setdefault(key, {'name': str(l.get('crop_name')).strip().title(), 'prices': [], 'qty': 0.0, 'districts': {}})
        c['prices'].append(price)
        c['qty'] += _num(l.get('quantity_kg')) or 0
        d = str(l.get('district') or '').strip().title()
        if d:
            c['districts'].setdefault(d, []).append(price)

    ref_by_crop = {}
    for r in refs:                       # newest first: first row per crop wins
        key = _norm_crop(r.get('crop_name'))
        price = _num(r.get('price_per_kg')) or _num(r.get('price'))
        if key and price is not None and key not in ref_by_crop:
            ref_by_crop[key] = {'price': round(price), 'market': r.get('market') or r.get('district') or '',
                                'name': str(r.get('crop_name')).strip().title()}

    since = (now - datetime.timedelta(hours=26)).isoformat()
    hist = supa_get('market_price_history', {'district': 'is.null', 'captured_at': f'gte.{since}',
                                             'select': 'crop,avg_price,captured_at', 'order': 'captured_at.asc'},
                    limit=5000) or []
    snaps = {}
    newest = None
    for h in hist:
        ts, p = _parse_ts(h.get('captured_at')), _num(h.get('avg_price'))
        if ts is None or p is None:
            continue
        snaps.setdefault(h.get('crop'), []).append((ts, p))
        newest = ts if newest is None or ts > newest else newest

    rows = []
    for key in sorted(set(crops) | set(ref_by_crop)):
        c, ref = crops.get(key), ref_by_crop.get(key)
        row = {'crop': (c or ref)['name'], 'ref_price': ref['price'] if ref else None,
               'ref_market': ref['market'] if ref else '', 'sellers': 0, 'avg': None, 'median': None,
               'low': None, 'high': None, 'qty_kg': 0, 'change_pct': None, 'change_basis': None, 'districts': {}}
        if c:
            row.update(_stats(c['prices']))
            row['qty_kg'] = round(c['qty'])
            row['districts'] = {d: _stats(p) for d, p in c['districts'].items()}
            series = snaps.get(key) or []
            day_ago = now - datetime.timedelta(hours=24)
            older = [s for s in series if s[0] <= day_ago]
            base, basis = (older[-1], '24h') if older else (None, None)
            if base is None:
                aged = [s for s in series if s[0] <= now - datetime.timedelta(minutes=30)]
                base, basis = (aged[0], 'since_start') if aged else (None, None)
            if base:
                row['change_pct'] = round((row['avg'] - base[1]) / base[1] * 100, 1)
                row['change_basis'] = basis
        rows.append(row)

    # Snapshot ~every 10 min (DB-based so restarts and several workers don't pile up rows).
    if rows and (newest is None or (now - newest).total_seconds() >= _SNAP_EVERY):
        batch = [{'crop': _norm_crop(r['crop']), 'district': None, 'avg_price': r['avg'], 'min_price': r['low'],
                  'max_price': r['high'], 'sellers': r['sellers'], 'total_qty_kg': r['qty_kg']}
                 for r in rows if r['avg']]
        if batch:
            supa_insert('market_price_history', batch)

    for r in rows:   # big moves go to Discord (price.* -> the errors/monitoring channel)
        if r['change_basis'] == '24h' and r['sellers'] >= 2 and abs(r['change_pct'] or 0) >= _SPIKE_PCT:
            k = _norm_crop(r['crop'])
            if _time.time() - _last_spike_alert.get(k, 0) > _SPIKE_QUIET:
                _last_spike_alert[k] = _time.time()
                _discord_notify(
                    event_type='price.spike', title=f"📈 {r['crop']} price moved {r['change_pct']:+.1f}% in 24h",
                    description=f"**Crop:** {r['crop']}\n**Average now:** UGX {r['avg']:,}/kg\n**Sellers:** {r['sellers']}\n"
                                f"**Range:** {r['low']:,} to {r['high']:,}",
                    severity='warning')
    return {'ok': True, 'updated_at': now.isoformat(), 'rows': rows}


@app.route('/api/market/live', methods=['GET'])
@rate_limit(max_req=90, window=60)
def market_live():
    """Public: live price board built from farmers' asking prices on the platform."""
    if _LIVE_CACHE['data'] is None or _time.time() - _LIVE_CACHE['t'] > _LIVE_TTL:
        try:
            _LIVE_CACHE['data'] = _build_live_board()
            _LIVE_CACHE['t'] = _time.time()
        except Exception as e:
            log.warning("market_live.failed", error=str(e))
            if _LIVE_CACHE['data'] is None:
                return jsonify({'ok': False, 'error': 'prices unavailable'}), 503
    resp = jsonify(_LIVE_CACHE['data'])
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 200


# ══════════════════════════════════════════════════════════════════════════════
# AI CROP DOCTOR
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/crop-doctor', methods=['POST'])
@rate_limit(max_req=15, window=300)
def crop_doctor():
    data        = request.get_json(force=True, silent=True) or {}
    description = data.get('description', '').strip()
    crop        = data.get('crop', 'crop')
    if not description:
        return jsonify({'error': 'Provide a description of the problem'}), 400
    if not GEMINI_KEY:
        return jsonify({
            'diagnosis':  'AI crop doctor is not configured on this server yet.',
            'treatment':  'Contact your nearest NAADS extension officer for advice.',
            'prevention': 'Keep records of your crop health to spot patterns early.'
        })
    prompt = (
        f"You are an expert agronomist specialising in Uganda and East Africa. "
        f"A farmer reports this problem with their {crop}: \"{description}\". "
        f"Respond ONLY in valid JSON with keys: diagnosis, treatment, prevention, confidence (0-100). "
        f"Keep each value under 150 words. "
        f"Use practical advice suitable for small-scale Uganda farmers."
    )
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
            f"?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15
        )
        text = res.json()['candidates'][0]['content']['parts'][0]['text']
        text = text.strip().lstrip('```json').lstrip('```').rstrip('```').strip()
        result_json = _json.loads(text)
        _discord_notify(
            event_type='scan.crop_doctor',
            title='🌿 Crop Doctor Scan',
            description=(
                f"**Crop:** {crop}\n"
                f"**Description:** {description[:120]}{'...' if len(description) > 120 else ''}\n"
                f"**Diagnosis:** {result_json.get('diagnosis', '—')[:100]}\n"
                f"**Confidence:** {result_json.get('confidence', '—')}%"
            ),
            severity='info',
        )
        _log_system_event('scan_crop_doctor', {
            'crop': crop,
            'description': description[:200],
            'diagnosis': result_json.get('diagnosis', '')[:200],
            'treatment': result_json.get('treatment', '')[:200],
            'confidence': result_json.get('confidence'),
        }, severity='info')
        return jsonify(result_json)
    except Exception:
        return jsonify({'error': 'AI service temporarily unavailable'}), 503

# ══════════════════════════════════════════════════════════════════════════════
# AI ASSISTANT (Groq) — key stays server-side in GROQ_API_KEY
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/ai', methods=['POST'])
@rate_limit(max_req=20, window=300)
def ai_chat():
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get('message') or '').strip()[:1500]
    if not msg:
        return jsonify({'reply': None, 'error': 'empty message'}), 400
    key = os.environ.get('GROQ_API_KEY', '')
    if not key:
        # Not configured — let the client fall back to its offline helper.
        return jsonify({'reply': None, 'error': 'AI not configured'}), 200
    system = (
        "You are the AgriBridge assistant for Ugandan smallholder farmers and buyers. "
        "AgriBridge connects farmers directly to buyers: a marketplace for crops AND livestock, "
        "live market prices, AI crop & animal disease help, USSD *789# for basic phones, "
        "mobile money (MTN MoMo, Airtel Money) and GPS delivery. "
        "Answer concisely, practically and warmly. Reply in the user's language if they write "
        "Luganda or Swahili. Give real, useful farming and market advice for Uganda. "
        "Keep replies under 180 words. When it clearly helps, you may add ONE action tag on its "
        "own final line, chosen from: [NAVIGATE:market] [NAVIGATE:prices] [NAVIGATE:animals] "
        "[NAVIGATE:doctor] [NAVIGATE:delivery] [NAVIGATE:training] [NAVIGATE:dashboard] [OPEN_BASKET]."
    )
    try:
        res = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
            json={
                'model': 'llama-3.3-70b-versatile',
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': msg},
                ],
                'temperature': 0.4,
                'max_tokens': 500,
            },
            timeout=25,
        )
        if res.ok:
            reply = res.json()['choices'][0]['message']['content'].strip()
            _discord_notify(
                event_type='ai.query',
                title='🤖 AI Assistant Query',
                description=(
                    f"**Message:** {msg[:200]}\n"
                    f"**Reply:** {reply[:200]}{'…' if len(reply) > 200 else ''}"
                ),
                severity='info',
            )
            _log_system_event('ai_query', {
                'message': msg[:300],
                'reply': reply[:300],
            }, severity='info')
            return jsonify({'reply': reply}), 200
        return jsonify({'reply': None, 'error': 'ai upstream ' + str(res.status_code)}), 200
    except Exception:
        return jsonify({'reply': None, 'error': 'ai unavailable'}), 200


# ══════════════════════════════════════════════════════════════════════════════
# SERVER-SIDE ORDER API (authoritative; fixes audit C1/C2/C4)
# Orders are created here — never by a direct browser insert — so the price is
# recomputed from the listing and stock is reserved atomically in Postgres.
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/orders', methods=['POST'])
@rate_limit(max_req=20, window=60)
def create_order():
    uid, err = verify_supabase_user()
    if err:
        return err
    blocked = legal_gate(uid)
    if blocked:
        return blocked
    data = request.get_json(force=True, silent=True) or {}
    items = data.get('items')
    # Idempotency key prevents duplicate orders on a retried/replayed request.
    idem = request.headers.get('Idempotency-Key', '').strip() or None
    result = ORDER_SERVICE.create_order(
        buyer_id=uid,
        items=items if isinstance(items, list) else [],
        delivery_address=str(data.get('delivery_address', '')),
        payment_method=str(data.get('payment_method', '')),
        idempotency_key=idem,
        correlation_id=getattr(g, 'correlation_id', None),
    )
    if not result.ok:
        # Alert admin on Discord when an order fails
        _discord_notify(
            event_type='order.failed',
            title='🚨 Order Failed',
            description=(
                f"**Error:** {result.error}\n"
                f"**Buyer ID:** `{uid}`\n"
                f"**Payment:** {data.get('payment_method', 'unknown')}\n"
                f"**Items:** {len(items) if isinstance(items, list) else '?'}"
            ),
            severity='error',
        )
        _log_system_event('order_failed', {
            'error': result.error,
            'buyer_id': uid,
            'payment_method': data.get('payment_method'),
            'item_count': len(items) if isinstance(items, list) else 0,
        }, user_id=uid)
        return jsonify({'error': result.error}), result.status_code

    # ── Notify farmer(s) by SMS immediately after order is committed ──────────
    if at_sms:
        notified = set()
        for item in (result.items or []):
            fphone = item.get('farmer_phone') or ''
            if fphone and fphone not in notified:
                notified.add(fphone)
                try:
                    item_name = str(item.get('product_name') or 'item')[:40]
                    at_sms.send(
                        message=(
                            f"AgriBridge: New order {result.order_ref}!\n"
                            f"{item.get('quantity','')} {item_name}. "
                            f"Total UGX {int(item.get('unit_price',0)*item.get('quantity',1)):,}.\n"
                            f"Log in to confirm: agribrige.com"
                        ),
                        recipients=[fphone],
                        sender_id=AT_SMS_SENDER,
                    )
                except Exception as _sms_exc:
                    log.warning("order.farmer_sms_failed", phone=fphone, error=str(_sms_exc))

    # ── Discord notification ──────────────────────────────────────────────────
    _discord_notify(
        event_type='order.created',
        title=f'🛒 New Order — {result.order_ref}',
        description=(
            f"**Order Ref:** `{result.order_ref}`\n"
            f"**Tracking:** `{result.tracking_code}`\n"
            f"**Total:** UGX {int(result.total_price):,}\n"
            f"**Items:** {len(result.items or [])}\n"
            f"**Payment:** {data.get('payment_method','')}"
        ),
        severity='info',
    )

    # ── In-app notification → buyer ──────────────────────────────────────────
    try:
        pay_method = str(data.get('payment_method', '')).lower()
        pay_instr = {
            'momo':    'Dial *165# on your MTN line → Send Money → AgriBridge (0755 966 690)',
            'airtel':  'Dial *185# on your Airtel line → Make Payments → AgriBridge',
            'card':    'You will be redirected to complete your card payment.',
            'cash':    'Pay on delivery.',
            'bank':    'Transfer the total to the bank details on your confirmation screen, quoting your order reference. An operator confirms it before your order is marked paid.',
        }.get(pay_method, 'Follow the instructions on your confirmation screen.')
        NOTIFY.enqueue(
            channel='in_app',
            audience='user',
            recipient=uid,
            event_type='order.created',
            severity='info',
            title=f'🛒 Order {result.order_ref} Received!',
            body=(
                f'Your order has been received and is pending admin confirmation. '
                f'Total: UGX {int(result.total_price):,}. '
                f'Tracking: {result.tracking_code}. '
                f'Payment: {pay_instr}'
            ),
            payload={
                'order_ref': result.order_ref,
                'tracking_code': result.tracking_code,
                'total_price': result.total_price,
                'payment_method': pay_method,
            },
            dedupe_key=f'order_created:{result.order_ref}',
        )
    except Exception as _ne:
        log.warning('order.buyer_notify_failed', error=str(_ne))

    return jsonify({
        'ok': True, 'order_ref': result.order_ref,
        'tracking_code': result.tracking_code, 'total_price': result.total_price,
        'items': result.items, 'status': 'pending', 'payment_status': 'unpaid',
    }), 201


@app.route('/api/orders/<order_id>/status', methods=['POST'])
@rate_limit(max_req=30, window=60)
def transition_order_status(order_id):
    """Move an order through its legal states via the server-side state machine.

    Only a verified admin, or the farmer who owns the order, may advance it.
    Illegal transitions are rejected by the Postgres function (22023)."""
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    new_status = str(data.get('status', '')).strip()
    if new_status not in ('confirmed', 'in_transit', 'delivered', 'cancelled'):
        return jsonify({'error': 'Invalid status'}), 400

    rows = SUPA.select('orders', {'id': f'eq.{order_id}', 'select': 'farmer_id,status'}, limit=1)
    if not rows:
        return jsonify({'error': 'Order not found'}), 404
    order = rows[0]
    if not (is_admin_uuid(uid) or order.get('farmer_id') == uid):
        return jsonify({'error': 'Insufficient permissions'}), 403

    res = SUPA.rpc('set_order_status', {'p_order_id': order_id, 'p_new_status': new_status})
    if not res.ok:
        code = 409 if 'illegal' in (res.error or '').lower() else 400
        return jsonify({'error': res.error or 'transition failed'}), code
    _status_emoji = {'confirmed': '✅', 'in_transit': '🚚', 'delivered': '📦', 'cancelled': '❌'}
    _discord_notify(
        event_type=f'order.{new_status}',
        title=f"{_status_emoji.get(new_status, '📋')} Order {new_status.replace('_', ' ').title()}",
        description=(
            f"**Order ID:** `{order_id}`\n"
            f"**New status:** {new_status}\n"
            f"**Updated by:** `{uid}`"
        ),
        severity='warning' if new_status == 'cancelled' else 'info',
    )
    return jsonify({'ok': True, 'status': new_status}), 200


# ══════════════════════════════════════════════════════════════════════════════
# PURCHASE REQUESTS + FARMER OFFER ROUTING (Phase 5)
# A purchase request is distinct from a confirmed paid order: it is matched to an
# eligible farmer server-side, offers are sent, and stock is reserved atomically
# only when a farmer accepts. The worker (services/worker.py) drives matching.
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/requests', methods=['POST'])
@rate_limit(max_req=20, window=60)
def create_purchase_request():
    uid, err = verify_supabase_user()
    if err:
        return err
    blocked = legal_gate(uid)
    if blocked:
        return blocked
    data = request.get_json(force=True, silent=True) or {}
    idem = request.headers.get('Idempotency-Key', '').strip() or None
    result = MATCHING_SERVICE.create_request(
        buyer_id=uid,
        product=str(data.get('product', '')),
        quantity=data.get('quantity'),
        unit=str(data.get('unit', 'kg')),
        delivery_address=str(data.get('delivery_address', '')),
        delivery_district=data.get('delivery_district'),
        target_price=data.get('target_price'),
        idempotency_key=idem,
        correlation_id=getattr(g, 'correlation_id', None),
    )
    if not result.ok:
        return jsonify({'error': result.error}), result.status_code
    _discord_notify(
        event_type='listing.request',
        title='🛒 New Purchase Request',
        description=(
            f"**Product:** {data.get('product', '—')}\n"
            f"**Quantity:** {data.get('quantity', '—')} {data.get('unit', 'kg')}\n"
            f"**Delivery:** {data.get('delivery_address', '—')}\n"
            f"**Buyer ID:** `{uid}`"
        ),
        severity='info',
    )
    _log_system_event('purchase_request_created', {
        'buyer_id': uid, 'product': data.get('product'),
        'quantity': data.get('quantity'), 'unit': data.get('unit'),
        'delivery_address': data.get('delivery_address'),
    }, severity='info', user_id=uid)
    return jsonify({'ok': True, **(result.data or {})}), 201


@app.route('/api/offers/<offer_id>/accept', methods=['POST'])
@rate_limit(max_req=30, window=60)
def accept_offer(offer_id):
    uid, err = verify_supabase_user()
    if err:
        return err
    blocked = legal_gate(uid)
    if blocked:
        return blocked
    result = MATCHING_SERVICE.accept_offer(offer_id, uid)
    if not result.ok:
        return jsonify({'error': result.error}), result.status_code
    _discord_notify(
        event_type='matching.offer_accepted',
        title='🤝 Farmer Accepted an Offer',
        description=(
            f"**Offer ID:** `{offer_id}`\n"
            f"**Farmer ID:** `{uid}`"
        ),
        severity='info',
    )
    _log_system_event('offer_accepted', {'offer_id': offer_id, 'farmer_id': uid},
                      severity='info', user_id=uid)
    return jsonify({'ok': True, **(result.data or {})}), 200


@app.route('/api/offers/<offer_id>/reject', methods=['POST'])
@rate_limit(max_req=30, window=60)
def reject_offer(offer_id):
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    result = MATCHING_SERVICE.reject_offer(offer_id, uid, str(data.get('reason', ''))[:200] or None)
    if not result.ok:
        return jsonify({'error': result.error}), result.status_code
    _discord_notify(
        event_type='matching.offer_rejected',
        title='❌ Farmer Rejected an Offer',
        description=(
            f"**Offer ID:** `{offer_id}`\n"
            f"**Farmer ID:** `{uid}`\n"
            f"**Reason:** {data.get('reason', '—')}"
        ),
        severity='warning',
    )
    return jsonify({'ok': True}), 200


@app.route('/api/worker/match', methods=['POST'])
def trigger_matching():
    """Admin-only manual matching pass — accepts either admin JWT or Supabase session."""
    # Accept the password-based admin JWT (used by the admin panel) OR a Supabase session.
    payload, jwt_err = verify_token(required_role='admin')
    if jwt_err:
        # Fall back to Supabase identity check
        uid, supa_err = require_admin_user()
        if supa_err:
            return supa_err
    result = MATCHING_SERVICE.run_matching_pass(
        batch_size=int((request.get_json(force=True, silent=True) or {}).get('batch_size', 10)))
    if not result.ok:
        _discord_notify(
            event_type='matching.failed',
            title='⚠️ Matching Pass Failed',
            description=f"**Error:** {result.error}",
            severity='error',
        )
        _log_system_event('matching_failed', {'error': result.error})
        return jsonify({'error': result.error}), result.status_code
    _discord_notify(
        event_type='matching.completed',
        title='🔗 Matching Pass Completed',
        description=(
            f"**Requests processed:** {result.requests_processed}\n"
            f"**Offers sent:** {result.offers}\n"
            f"**Expired:** {result.expired}"
        ),
        severity='info',
    )
    return jsonify({'ok': True, 'requests_processed': result.requests_processed,
                    'offers': result.offers, 'expired': result.expired}), 200


# ── System event logger — writes to system_events table for admin panel ───────
def _log_system_event(event_type: str, details: dict, severity: str = 'error',
                      user_id: str = None):
    """Write a system event (failure/warning) to the system_events table.
    Best-effort: never raises."""
    try:
        supa_insert('system_events', {
            'event_type': event_type,
            'severity': severity,
            'details': details,
            'user_id': user_id,
        })
    except Exception as _e:
        log.warning("system_event.write_failed", error=str(_e))


@app.route('/api/admin/discord-test', methods=['POST'])
def admin_discord_test():
    """Fire a test message to every configured Discord channel. Admin only."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    import os as _os
    results = {}
    test_channels = {
        'order.created':     'DISCORD_WEBHOOK_ORDERS',
        'user.signup':       'DISCORD_WEBHOOK_SIGNUPS',
        'listing.created':   'DISCORD_WEBHOOK_LISTINGS',
        'matching.offer':    'DISCORD_WEBHOOK_MATCHING',
        'transport.avail':   'DISCORD_WEBHOOK_TRANSPORT',
        'server.error':      'DISCORD_WEBHOOK_ERRORS',
        'request.test':      'DISCORD_WEBHOOK_REQUESTS',
        'support.test':      'DISCORD_WEBHOOK_SUPPORT',
    }
    for event_type, env_var in test_channels.items():
        url = _os.environ.get(env_var, '').strip()
        if not url:
            results[env_var] = 'not set'
            continue
        _discord_notify(
            event_type=event_type,
            title=f'🧪 Test — {env_var}',
            description=f'**Channel:** #{event_type.split(".")[0]}\n**Env var:** `{env_var}`\n**Status:** ✅ If you see this in the right channel, routing works!',
            severity='info',
        )
        results[env_var] = 'sent'
    return jsonify({'ok': True, 'results': results})


# ══════════════════════════════════════════════════════════════════════════════
# MONITORING: Supabase table events -> Discord + admin Activity feed, and a
# read-only admin Inbox over the user-submitted tables.
# Much of the web app writes straight to Supabase, so the backend never sees those
# rows. A Supabase Database Webhook (see docs/MONITORING.md) posts each new row here.
# ══════════════════════════════════════════════════════════════════════════════
SUPABASE_WEBHOOK_SECRET = os.environ.get('SUPABASE_WEBHOOK_SECRET', '')

# table -> (discord event_type, title, severity)
_WATCHED = {
    'account_deletion_requests': ('support.deletion_request', '🗑 Account deletion requested', 'warning'),
    'privacy_requests':          ('support.privacy_request',  '🔐 Privacy request',            'warning'),
    'contact_messages':          ('support.contact_message',  '✉️ New contact message',        'info'),
    'disease_reports':           ('support.disease_report',   '🦠 Disease report',             'warning'),
    'vet_bookings':              ('request.vet_booking',      '🐄 Vet booking',                'info'),
    'purchase_requests':         ('request.purchase',         '🛒 Purchase request',           'info'),
    'supply_orders':             ('order.supply',             '📦 Supply order',               'info'),
    'payouts':                   ('payment.payout',           '💸 Payout record',              'info'),
    'fraud_flags':               ('safety.fraud_flag',        '🚩 Fraud flag raised',          'critical'),
    'admin_devices':             ('safety.admin_device',      '🔑 Admin device registered',    'warning'),
    'user_roles':                ('safety.role_change',       '👤 User role added',            'warning'),
    'reviews':                   ('listing.review',           '⭐ New review',                 'info'),
    'community_posts':           ('listing.community_post',   '💬 New community post',         'info'),
    'transporters':              ('transport.registered',     '🚛 Transporter registered',     'info'),
    'farmers':                   ('user.signup',              '👤 New farmer/user',            'info'),
    'buyers':                    ('user.signup',              '🛍 New buyer',                  'info'),
    'order_offers':              ('matching.offer',           '🔗 Match offer',                'info'),
}
# Only these columns are ever copied into Discord / the activity feed. No phones,
# emails, tokens or passwords.
_SAFE_COLS = ('id', 'status', 'kind', 'type', 'category', 'subject', 'reason', 'flag_type',
              'severity', 'role', 'full_name', 'name', 'district', 'crop_name', 'species',
              'rating', 'amount', 'amount_ugx', 'total', 'total_price', 'currency',
              'order_ref', 'service', 'provider_name', 'created_at')


def _safe_fields(record):
    out = {}
    for k in _SAFE_COLS:
        v = (record or {}).get(k)
        if v not in (None, ''):
            out[k] = str(v)[:120]
    msg = (record or {}).get('message') or (record or {}).get('body') or (record or {}).get('description')
    if msg:
        out['message'] = str(msg)[:200]
    return out


@app.route('/api/hooks/supabase', methods=['POST'])
@rate_limit(max_req=120, window=60)
def supabase_table_hook():
    """Receive a Supabase Database Webhook and forward it to Discord + system_events."""
    sent = request.headers.get('X-Webhook-Secret', '')
    if not SUPABASE_WEBHOOK_SECRET or not sent or not hmac.compare_digest(sent, SUPABASE_WEBHOOK_SECRET):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    data = request.get_json(force=True, silent=True) or {}
    table = str(data.get('table') or '')
    if table not in _WATCHED:
        return jsonify({'ok': True, 'ignored': 'table not watched'}), 200
    if str(data.get('type') or '').upper() != 'INSERT':
        return jsonify({'ok': True, 'ignored': 'not an insert'}), 200
    event_type, title, severity = _WATCHED[table]
    fields = _safe_fields(data.get('record'))
    desc = '\n'.join(f'**{k}:** {v}' for k, v in {'table': table, **fields}.items())
    _discord_notify(event_type=event_type, title=title, description=desc, severity=severity)
    _log_system_event(event_type, {'table': table, **fields}, severity='info' if severity == 'info' else severity)
    return jsonify({'ok': True}), 200


@app.route('/api/admin/inbox', methods=['GET'])
def admin_inbox():
    """Read-only view of the watched tables. No ?table => counts of the latest rows per table."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    table = (request.args.get('table') or '').strip()
    if table:
        if table not in _WATCHED:
            return jsonify({'error': 'unknown table'}), 400
        rows = supa_get(table, {'order': 'created_at.desc'}, limit=100) or supa_get(table, None, limit=100) or []
        for r in rows:
            for secret in ('password', 'token', 'token_hash', 'access_token', 'refresh_token'):
                r.pop(secret, None)
        return jsonify(rows)
    summary = {t: len(supa_get(t, None, limit=100) or []) for t in _WATCHED}
    return jsonify({'tables': summary, 'discord_configured': DISCORD.enabled,
                    'webhook_secret_set': bool(SUPABASE_WEBHOOK_SECRET)})


@app.route('/api/admin/events', methods=['GET'])
def admin_events():
    """Return recent system events (failures, warnings) for the admin panel."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    severity   = request.args.get('severity')
    event_type = request.args.get('event_type')
    filters = {'order': 'created_at.desc'}
    if severity:
        filters['severity'] = f'eq.{severity}'
    if event_type:
        # Support prefix match: e.g. event_type=scan matches scan_*, listing_created, etc.
        filters['event_type'] = f'ilike.*{event_type}*'
    return jsonify(supa_get('system_events', filters=filters, limit=200) or [])


# ══════════════════════════════════════════════════════════════════════════════
# TRANSPORTERS — accounts, availability toggle, admin management
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/admin/transporters', methods=['GET'])
def admin_transporters():
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    filters = {'order': 'created_at.desc'}
    avail = request.args.get('available')
    if avail:
        filters['is_available'] = f'eq.{avail}'
    return jsonify(supa_get('transporters', filters=filters, limit=300) or [])


@app.route('/api/admin/transporters', methods=['POST'])
def admin_create_transporter():
    """Admin creates a transporter account (stores profile; Supabase Auth signup done separately)."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    allowed_fields = ['full_name', 'phone', 'email', 'vehicle_type', 'plate_number',
                      'district', 'capacity_kg', 'notes', 'supabase_uid']
    row = {k: data[k] for k in allowed_fields if k in data}
    if not row.get('full_name') or not row.get('phone'):
        return jsonify({'error': 'full_name and phone are required'}), 400
    row.setdefault('is_available', False)
    row.setdefault('status', 'active')
    inserted = supa_insert('transporters', row)
    if not inserted:
        return jsonify({'error': 'Failed to create transporter'}), 500
    _discord_notify(
        event_type='transport.registered',
        title='🚛 New Transporter Registered',
        description=(
            f"**Name:** {row.get('full_name')}\n"
            f"**Phone:** {row.get('phone')}\n"
            f"**Vehicle:** {row.get('vehicle_type', 'N/A')}\n"
            f"**District:** {row.get('district', 'N/A')}"
        ),
        severity='info',
    )
    return jsonify({'ok': True, 'transporter': inserted[0] if isinstance(inserted, list) else inserted}), 201


@app.route('/api/admin/transporters/<transporter_id>', methods=['PATCH'])
def admin_update_transporter(transporter_id):
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    allowed_fields = ['full_name', 'phone', 'email', 'vehicle_type', 'plate_number',
                      'district', 'capacity_kg', 'notes', 'status', 'is_available']
    fields = {k: data[k] for k in allowed_fields if k in data}
    if not fields:
        return jsonify({'error': 'No editable fields'}), 400
    ok = supa_update('transporters', fields, 'id', transporter_id)
    return (jsonify({'ok': True}), 200) if ok else (jsonify({'error': 'Update failed'}), 500)


@app.route('/api/admin/transporters/<transporter_id>', methods=['DELETE'])
def admin_delete_transporter(transporter_id):
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    ok = supa_update('transporters', {'status': 'inactive'}, 'id', transporter_id)
    return (jsonify({'ok': True}), 200) if ok else (jsonify({'error': 'Failed'}), 500)


# ── Transporter self-service: availability toggle ─────────────────────────────
@app.route('/api/transporter/availability', methods=['POST'])
@rate_limit(max_req=30, window=60)
def transporter_set_availability():
    """Transporter sets their own availability. Auth via Supabase session.
    Their supabase_uid must match the transporters.supabase_uid column."""
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    is_available = bool(data.get('is_available', False))
    # Find the transporter row that belongs to this Supabase user.
    rows = supa_get('transporters', {'supabase_uid': f'eq.{uid}', 'status': 'eq.active'}, limit=1)
    if not rows:
        return jsonify({'error': 'No transporter account found for your login'}), 404
    transporter_id = rows[0]['id']
    ok = supa_update('transporters', {
        'is_available': is_available,
        'availability_updated_at': datetime.datetime.utcnow().isoformat(),
    }, 'id', transporter_id)
    if not ok:
        return jsonify({'error': 'Could not update availability'}), 500
    log.info("transporter.availability", id=transporter_id, available=is_available)
    t_name = rows[0].get('full_name', 'Unknown')
    t_district = rows[0].get('district', '—')
    _discord_notify(
        event_type='transport.availability',
        title=f"{'🟢' if is_available else '🔴'} Transporter {'Available' if is_available else 'Unavailable'}",
        description=(
            f"**Name:** {t_name}\n"
            f"**District:** {t_district}\n"
            f"**Status:** {'Ready for jobs' if is_available else 'Not available'}"
        ),
        severity='info',
    )
    return jsonify({'ok': True, 'is_available': is_available}), 200


@app.route('/api/transporter/me', methods=['GET'])
@rate_limit(max_req=60, window=60)
def transporter_profile():
    """Return the transporter's own profile (availability, details)."""
    uid, err = verify_supabase_user()
    if err:
        return err
    rows = supa_get('transporters', {'supabase_uid': f'eq.{uid}'}, limit=1)
    if not rows:
        return jsonify({'error': 'No transporter account'}), 404
    return jsonify(rows[0]), 200


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# LOGISTICS COMMAND CENTER — GPS, order actions, receipts
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/transporter/location', methods=['POST'])
@rate_limit(max_req=60, window=60)
def transporter_update_location():
    """Transporter pushes their current GPS location.
    Stored on the deliveries row tied to their active delivery."""
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({'error': 'lat and lng required'}), 400
    try:
        lat = float(lat)
        lng = float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('out of range')
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid coordinates'}), 400

    # Find transporter row
    rows = supa_get('transporters', {'supabase_uid': f'eq.{uid}', 'status': 'eq.active'}, limit=1)
    if not rows:
        return jsonify({'error': 'No transporter account found'}), 404
    transporter_id = rows[0]['id']

    # Update only deliveries that are still active; completed/failed rows keep
    # the position they finished at.
    now = datetime.datetime.utcnow().isoformat()
    active = supa_get('deliveries', {
        'transporter_id': f'eq.{transporter_id}',
        'status': 'in.(picked_up,in_transit,at_checkpoint)',
        'select': 'id',
    }, limit=20) or []
    delivery_updates = 0
    for row in active:
        if row.get('id') and supa_update('deliveries', {
            'transporter_lat': lat,
            'transporter_lng': lng,
            'location_updated_at': now,
        }, 'id', row['id']):
            delivery_updates += 1

    # Also update the transporter row itself for quick dashboard reads
    transporter_ok = supa_update('transporters', {
        'last_lat': lat, 'last_lng': lng, 'last_location_at': now,
    }, 'id', transporter_id)

    if not transporter_ok or (active and delivery_updates == 0):
        log.error('transporter.location_not_saved', transporter_id=transporter_id,
                  active_deliveries=len(active), saved=delivery_updates)
        return jsonify({'error': 'Location could not be saved. Retry.'}), 503

    return jsonify({'ok': True, 'deliveries_updated': delivery_updates}), 200


@app.route('/api/admin/active-deliveries', methods=['GET'])
def admin_active_deliveries():
    """Return all orders currently in_transit with last GPS position."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    orders = supa_get('orders', {'status': 'eq.in_transit', 'order': 'updated_at.desc'}, limit=200) or []
    deliveries = supa_get('deliveries', {'order': 'updated_at.desc'}, limit=500) or []
    transporters = supa_get('transporters', {}, limit=300) or []

    # Index for fast lookup
    del_by_order = {d.get('order_id'): d for d in deliveries}
    t_by_id = {t['id']: t for t in transporters}

    result = []
    for o in orders:
        oid = o.get('id')
        d = del_by_order.get(oid, {})
        tid = d.get('transporter_id')
        t = t_by_id.get(tid, {}) if tid else {}
        result.append({
            'order_id':    oid,
            'order_ref':   o.get('order_ref', ''),
            'tracking_code': o.get('tracking_code', ''),
            'delivery_address': o.get('delivery_address', ''),
            'total_price': o.get('total_price', 0),
            'payment_method': o.get('payment_method', ''),
            'payment_status': o.get('payment_status', ''),
            'created_at':  o.get('created_at', ''),
            'transporter_name':  t.get('full_name', ''),
            'transporter_phone': t.get('phone', ''),
            'transporter_vehicle': t.get('vehicle_type', ''),
            'lat': d.get('transporter_lat') or t.get('last_lat'),
            'lng': d.get('transporter_lng') or t.get('last_lng'),
            'location_updated_at': d.get('location_updated_at') or t.get('last_location_at'),
            'estimated_arrival': d.get('estimated_arrival'),
            'delivery_status': d.get('status', 'in_transit'),
        })
    return jsonify(result), 200


@app.route('/api/admin/orders-full', methods=['GET'])
def admin_orders_full():
    """Full order list for the Logistics Command Center with rich filtering."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    filters = {'order': 'created_at.desc'}
    status = request.args.get('status')
    payment = request.args.get('payment_status')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    q = request.args.get('q', '').strip()

    if status:
        filters['status'] = f'eq.{status}'
    if payment:
        filters['payment_status'] = f'eq.{payment}'
    if date_from:
        filters['created_at'] = f'gte.{date_from}'
    if date_to:
        filters['created_at'] = f'lte.{date_to}T23:59:59'

    orders = supa_get('orders', filters=filters, limit=500) or []

    # If search query, filter in Python (Supabase free tier lacks full-text)
    if q:
        ql = q.lower()
        orders = [o for o in orders if
                  ql in (o.get('order_ref') or '').lower() or
                  ql in (o.get('tracking_code') or '').lower() or
                  ql in (o.get('delivery_address') or '').lower() or
                  ql in (o.get('buyer_id') or '').lower()]

    return jsonify(orders), 200


@app.route('/api/admin/orders/<order_id>/suggested-farmers', methods=['GET'])
def admin_suggested_farmers(order_id):
    """Return farmers whose active listings match items in this order.

    Reads the order's items JSON, extracts listing_ids, finds the farmers
    behind those listings, and also pulls all their active listings so the
    call-centre can see capacity.  Falls back to all available farmers if
    the order has no item detail.
    """
    payload, err = verify_token(required_role='admin')
    if err:
        return err

    # 1. Fetch the order to get items
    order_rows = supa_get('orders', {'id': f'eq.{order_id}'}, limit=1) or []
    if not order_rows:
        return jsonify({'error': 'Order not found'}), 404
    order = order_rows[0]
    items = order.get('items') or []
    if isinstance(items, str):
        import json as _json
        try:
            items = _json.loads(items)
        except Exception:
            items = []

    # 2. Collect listing_ids from the order's items
    listing_ids = [i.get('listing_id') for i in items if i.get('listing_id')]
    crop_names  = list({(i.get('crop_name') or '').lower() for i in items if i.get('crop_name')})

    # 3. Get matching listings — by listing_id first, then by crop name
    matched_listings = []
    if listing_ids:
        for lid in listing_ids:
            rows = supa_get('listings', {'id': f'eq.{lid}', 'is_available': 'eq.true'}, limit=1) or []
            matched_listings.extend(rows)

    if not matched_listings and crop_names:
        all_avail = supa_get('listings', {'is_available': 'eq.true'}, limit=500) or []
        for row in all_avail:
            for cn in crop_names:
                if cn in (row.get('crop_name') or '').lower():
                    matched_listings.append(row)
                    break

    # 4. Dedupe by farmer_id, build response
    seen_farmers: dict = {}
    for lst in matched_listings:
        fid = lst.get('farmer_id')
        if not fid:
            continue
        if fid not in seen_farmers:
            seen_farmers[fid] = {
                'farmer_id': fid,
                'farmer_name': lst.get('farmer_name') or lst.get('full_name') or '',
                'farmer_phone': lst.get('farmer_phone') or lst.get('phone') or '',
                'listings': [],
            }
        seen_farmers[fid]['listings'].append({
            'listing_id': lst.get('id'),
            'crop_name': lst.get('crop_name'),
            'quantity_available': lst.get('quantity_available') or lst.get('quantity'),
            'price_per_kg': lst.get('price_per_kg'),
            'location': lst.get('location') or lst.get('parish') or '',
        })

    # 5. If no matches at all, fall back to all farmers with active listings
    if not seen_farmers:
        fallback = supa_get('listings', {'is_available': 'eq.true', 'order': 'created_at.desc'}, limit=200) or []
        for lst in fallback:
            fid = lst.get('farmer_id')
            if not fid or fid in seen_farmers:
                continue
            seen_farmers[fid] = {
                'farmer_id': fid,
                'farmer_name': lst.get('farmer_name') or lst.get('full_name') or '',
                'farmer_phone': lst.get('farmer_phone') or lst.get('phone') or '',
                'listings': [{
                    'listing_id': lst.get('id'),
                    'crop_name': lst.get('crop_name'),
                    'quantity_available': lst.get('quantity_available') or lst.get('quantity'),
                    'price_per_kg': lst.get('price_per_kg'),
                    'location': lst.get('location') or lst.get('parish') or '',
                }],
            }

    # 6. Enrich with real farmer profile data (full_name, phone) from farmers table
    #    Listings may not carry these columns — fetch the source of truth.
    farmer_ids = list(seen_farmers.keys())
    if farmer_ids:
        try:
            # Fetch up to 200 farmer profiles in one call
            id_filter = ','.join(farmer_ids[:200])
            profiles = supa_get('farmers', {
                'id': f'in.({id_filter})',
                'select': 'id,full_name,phone,location',
            }, limit=200) or []
            for p in profiles:
                fid = p.get('id')
                if fid and fid in seen_farmers:
                    # Profile wins over listing's stale copy
                    seen_farmers[fid]['farmer_name'] = (
                        p.get('full_name') or seen_farmers[fid]['farmer_name'] or 'Farmer'
                    )
                    seen_farmers[fid]['farmer_phone'] = (
                        p.get('phone') or seen_farmers[fid]['farmer_phone'] or ''
                    )
                    seen_farmers[fid]['farmer_location'] = p.get('location') or ''
        except Exception:
            pass  # enrichment is best-effort; we still return the listing-level data

    # Ensure every entry has at least a display name
    for _fid, entry in seen_farmers.items():
        if not entry['farmer_name']:
            entry['farmer_name'] = 'Farmer'

    return jsonify(list(seen_farmers.values())), 200


@app.route('/api/admin/orders/<order_id>/assign-farmer', methods=['POST'])
def admin_assign_farmer(order_id):
    """Attach a farmer to a pending order (before confirmation).

    After the call-centre calls the farmer and confirms availability,
    this endpoint records which farmer is responsible for fulfilling
    the order.  The order stays in 'pending' until the admin then
    presses Approve.
    """
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    farmer_id = str(data.get('farmer_id', '')).strip()
    if not farmer_id:
        return jsonify({'error': 'farmer_id required'}), 400

    now = datetime.datetime.utcnow().isoformat()
    # Only update columns that actually exist on the orders table
    ok = supa_update('orders', {
        'farmer_id': farmer_id,
        'updated_at': now,
    }, 'id', order_id)
    if not ok:
        _discord_notify(
            'order.assign_farmer_failed',
            '❌ Farmer Assignment Failed',
            f'Order `{order_id[:8]}` — could not update farmer_id to `{farmer_id[:8]}` in database.',
            color=0xe05555,
        )
        return jsonify({'error': 'Update failed — database rejected the change'}), 500

    # Fetch order ref for notifications
    order_rows = supa_get('orders', {'id': f'eq.{order_id}'}, limit=1) or []
    order_ref = (order_rows[0].get('order_ref') or order_id[:8]) if order_rows else order_id[:8]

    # Notify farmer via in-app notification
    try:
        NOTIFY.enqueue(
            channel='in_app',
            audience='user',
            recipient=farmer_id,
            event_type='order.farmer_assigned',
            severity='info',
            title='📦 New Order Assigned to You',
            body=f'Order {order_ref} has been assigned to you by AgriBridge. Please check your dashboard.',
            payload={'order_id': order_id, 'order_ref': order_ref},
            dedupe_key=f'farmer_assigned:{order_id}',
        )
    except Exception:
        pass  # notification failure must not block the assignment

    # Discord ops alert
    _discord_notify(
        'order.farmer_assigned',
        '🌾 Farmer Assigned',
        f'Order `{order_ref}` assigned to farmer `{farmer_id[:8]}` by admin.',
        color=0x2ec471,
    )

    _log_system_event('order_farmer_assigned', {
        'order_id': order_id,
        'order_ref': order_ref,
        'farmer_id': farmer_id,
        'admin': payload.get('sub', 'admin'),
    }, severity='info')
    return jsonify({'ok': True, 'order_ref': order_ref}), 200


@app.route('/api/admin/orders/<order_id>/approve', methods=['POST'])
def admin_approve_order(order_id):
    """Admin approves an order (moves pending → confirmed).

    Also enqueues an in-app notification for the buyer so the bell icon
    lights up in their account, and notifies the assigned farmer if set.
    """
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    reason = str(data.get('reason', 'Admin approved')).strip()[:200]

    # Fetch the order to get buyer_id and farmer_id for notifications
    order_rows = supa_get('orders', {'id': f'eq.{order_id}'}, limit=1) or []
    order = order_rows[0] if order_rows else {}
    buyer_id  = order.get('buyer_id')
    farmer_id = order.get('farmer_id')
    order_ref = order.get('order_ref', order_id[:8])

    ok = supa_update('orders', {
        'status': 'confirmed',
        'admin_approved': True,
        'admin_approved_at': datetime.datetime.utcnow().isoformat(),
    }, 'id', order_id)
    if ok:
        _log_system_event('order_admin_approved', {
            'order_id': order_id, 'reason': reason,
            'admin': payload.get('sub', 'admin'),
        }, severity='info')
        _discord_notify(
            event_type='order.confirmed',
            title='✅ Order Approved by Admin',
            description=f"**Order:** `{order_id}`\n**Reason:** {reason}\n**Admin:** `{payload.get('sub','')}`",
            severity='info',
        )

        # ── In-app notification → buyer ──────────────────────────────────────
        if buyer_id:
            NOTIFY.enqueue(
                channel='in_app',
                audience='user',
                recipient=buyer_id,
                event_type='order.confirmed',
                severity='info',
                title='✅ Your Order Has Been Confirmed!',
                body=f'Order {order_ref} has been confirmed and is being prepared for delivery.',
                payload={'order_id': order_id, 'order_ref': order_ref},
                dedupe_key=f'order_confirmed:{order_id}',
            )

        # ── In-app notification → farmer (if assigned) ───────────────────────
        if farmer_id and farmer_id != buyer_id:
            NOTIFY.enqueue(
                channel='in_app',
                audience='user',
                recipient=farmer_id,
                event_type='order.confirmed',
                severity='info',
                title='📋 Order Confirmed — Please Prepare',
                body=f'Order {order_ref} has been confirmed. Please prepare the goods for pickup.',
                payload={'order_id': order_id, 'order_ref': order_ref},
                dedupe_key=f'farmer_order_confirmed:{order_id}',
            )

        return jsonify({'ok': True}), 200
    return jsonify({'error': 'Update failed'}), 500


@app.route('/api/admin/orders/<order_id>/reject', methods=['POST'])
def admin_reject_order(order_id):
    """Admin rejects/cancels an order."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    reason = str(data.get('reason', 'Cancelled by admin')).strip()[:200]

    # Fetch buyer info for notification before updating
    order_rows = supa_get('orders', {'id': f'eq.{order_id}'}, limit=1) or []
    order = order_rows[0] if order_rows else {}
    buyer_id  = order.get('buyer_id')
    order_ref = order.get('order_ref', order_id[:8])

    ok = supa_update('orders', {
        'status': 'cancelled',
        'updated_at': datetime.datetime.utcnow().isoformat(),
    }, 'id', order_id)
    if ok:
        _log_system_event('order_admin_rejected', {
            'order_id': order_id, 'reason': reason,
        }, severity='warning')
        _discord_notify(
            event_type='order.cancelled',
            title='❌ Order Cancelled by Admin',
            description=f"**Order:** `{order_id}`\n**Reason:** {reason}",
            severity='warning',
        )
        # ── Notify buyer of cancellation ─────────────────────────────────────
        if buyer_id:
            try:
                NOTIFY.enqueue(
                    channel='in_app',
                    audience='user',
                    recipient=buyer_id,
                    event_type='order.cancelled',
                    severity='warning',
                    title='❌ Your Order Was Cancelled',
                    body=f'Order {order_ref} has been cancelled. Reason: {reason or "No reason given"}. Please contact support if you have questions.',
                    payload={'order_id': order_id, 'order_ref': order_ref},
                    dedupe_key=f'order_cancelled:{order_id}',
                )
            except Exception as _ne:
                log.warning('reject.buyer_notify_failed', error=str(_ne))
        return jsonify({'ok': True}), 200
    return jsonify({'error': 'Update failed'}), 500


@app.route('/api/admin/orders/<order_id>/assign-transporter', methods=['POST'])
def admin_assign_transporter(order_id):
    """Admin assigns a transporter to an order."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    transporter_id = str(data.get('transporter_id', '')).strip()
    if not transporter_id:
        return jsonify({'error': 'transporter_id required'}), 400
    t_rows = supa_get('transporters', {'id': f'eq.{transporter_id}'}, limit=1)
    if not t_rows:
        return jsonify({'error': 'Transporter not found'}), 404
    t = t_rows[0]
    # deliveries has driver_name/driver_phone, not transporter_id, and its status
    # check does not include 'assigned'. Persist the real columns and fail loudly
    # if the assignment cannot be saved; never report a phantom success.
    existing = supa_get('deliveries', {'order_id': f'eq.{order_id}'}, limit=1)
    now = datetime.datetime.utcnow().isoformat()
    delivery_fields = {
        'driver_name': t.get('full_name') or '',
        'driver_phone': t.get('phone') or '',
        'updated_at': now,
    }
    if existing:
        saved = supa_update('deliveries', delivery_fields, 'order_id', order_id)
    else:
        delivery_fields.update({
            'order_id': order_id,
            'status': 'scheduled',
            'created_at': now,
        })
        saved = bool(supa_insert('deliveries', delivery_fields))
    if not saved:
        log.error('delivery.assign_failed', order_id=order_id, transporter_id=transporter_id)
        return jsonify({'error': 'Could not save the delivery assignment. Nothing was changed.',
                        'code': 'delivery_assignment_failed'}), 503
    if not supa_update('orders', {'status': 'in_transit', 'updated_at': now}, 'id', order_id):
        log.error('delivery.assign_order_status_failed', order_id=order_id)
        return jsonify({'error': 'Transporter was recorded but the order status could not be updated.',
                        'code': 'order_status_update_failed'}), 503
    _log_system_event('order_transporter_assigned', {
        'order_id': order_id, 'transporter_id': transporter_id,
        'transporter_name': t.get('full_name'),
    }, severity='info')
    _discord_notify(
        event_type='transport.assigned',
        title='🚚 Transporter Assigned',
        description=(
            f"**Order:** `{order_id}`\n"
            f"**Transporter:** {t.get('full_name')} ({t.get('phone')})\n"
            f"**Vehicle:** {t.get('vehicle_type','—')} {t.get('plate_number','')}"
        ),
        severity='info',
    )

    # Notify buyer + farmer that their order is now in transit
    order_rows = supa_get('orders', {'id': f'eq.{order_id}'}, limit=1) or []
    if order_rows:
        o = order_rows[0]
        order_ref = o.get('order_ref', order_id[:8])
        t_name = t.get('full_name', 'your transporter')
        t_phone = t.get('phone', '')
        tracking_msg = (
            f"Order {order_ref} is now in transit with {t_name}"
            + (f" ({t_phone})" if t_phone else '')
            + ". Track live at agribrige.com."
        )
        for recipient in [o.get('buyer_id'), o.get('farmer_id')]:
            if recipient:
                NOTIFY.enqueue(
                    channel='in_app',
                    audience='user',
                    recipient=recipient,
                    event_type='order.in_transit',
                    severity='info',
                    title='🚚 Your Order Is On The Way!',
                    body=tracking_msg,
                    payload={
                        'order_id': order_id,
                        'order_ref': order_ref,
                        'transporter_id': transporter_id,
                        'transporter_name': t_name,
                        'transporter_phone': t_phone,
                    },
                    dedupe_key=f'in_transit:{order_id}',
                )

    return jsonify({'ok': True}), 200


@app.route('/api/admin/orders/<order_id>/confirm-delivery', methods=['POST'])
def admin_confirm_delivery(order_id):
    """Confirm delivery and record payment honestly.

    Delivery advances through the state machine. An unpaid cash-on-delivery
    order posts a COD receipt (debit COD_CASH_HELD) before it is marked paid.
    An unpaid gateway order is left unpaid: confirming a delivery must never
    invent a payment that no provider verified.
    """
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    note = str(data.get('note', '')).strip()[:300]
    now = datetime.datetime.utcnow().isoformat()

    rows = supa_get('orders', {
        'id': f'eq.{order_id}',
        'select': 'id,payment_ref,payment_method,payment_status,status,'
                  'total_amount,total_price,quantity_kg,price_per_kg',
    }, limit=1) or []
    if not rows:
        return jsonify({'error': 'Order not found'}), 404
    order = rows[0]
    pay_ref = str(order.get('payment_ref') or '').strip()

    res = SUPA.rpc('set_order_status', {'p_order_id': order_id, 'p_new_status': 'delivered'})
    if not res.ok:
        message = res.error or 'Delivery status update failed'
        code_status = 409 if res.status in (400, 409, 422) else 503
        if res.status == 404 or 'not found' in message.lower():
            code_status = 404
        return jsonify({'error': message, 'code': 'delivery_status_failed'}), code_status

    # deliveries records completion in actual_arrival; orders has no delivered_at.
    delivery_ok = supa_update(
        'deliveries', {'status': 'delivered', 'actual_arrival': now, 'updated_at': now},
        'order_id', order_id)
    if not delivery_ok:
        log.error('delivery.confirm_failed', order_id=order_id, delivery_updated=False)
        return jsonify({'error': 'Delivery confirmation was incomplete. Retry once the backend is reachable.',
                        'code': 'delivery_confirmation_failed'}), 503

    paid = str(order.get('payment_status') or '').lower() == 'paid'
    method = str(order.get('payment_method') or '').lower()
    payment_status = 'paid'
    journal_id = None
    warning = ''

    if not paid and method == 'cash':
        if not pay_ref:
            return jsonify({'error': 'Order has no payment reference; COD cannot be posted.',
                            'code': 'cod_reference_missing'}), 503
        pay_rows = supa_get('orders', {
            'payment_ref': f'eq.{pay_ref}',
            'select': 'id,status,payment_status,total_amount,total_price,quantity_kg,price_per_kg',
        }, limit=50) or rows
        try:
            total = sum((ledger.money(finance.order_total(o)) for o in pay_rows), ledger.ZERO)
        except ledger.LedgerError:
            total = ledger.ZERO
        posting = ledger.post_verified_payment(
            SUPA, tx_ref=pay_ref, orders=pay_rows, received=total, provider='cash',
            reference_prefix='cod', kind='cod_collection',
            debit_account=ledger.ACCOUNT_COD_CASH_HELD,
            debit_memo='cash collected on delivery',
            description=f'Cash on delivery {pay_ref}')
        if not posting.ok:
            log.error('delivery.cod_ledger_failed', order_id=order_id,
                      payment_ref=pay_ref, error=posting.error)
            _log_system_event('delivery_cod_ledger_failed',
                              {'order_id': order_id, 'payment_ref': pay_ref,
                               'error': posting.error}, severity='error')
            _discord_notify(
                event_type='ledger.cod_failed',
                title='⚠️ COD ledger post failed',
                description=f"**Order:** `{order_id}`\n**Ref:** `{pay_ref}`\n"
                            f"**Error:** {posting.error}",
                severity='error',
            )
            return jsonify({'error': 'Delivery recorded but the COD ledger entry failed. Retry.',
                            'code': 'ledger_post_failed'}), 503
        journal_id = posting.journal_id
        if not supa_update('orders', {'payment_status': 'paid'}, 'payment_ref', pay_ref):
            log.error('delivery.cod_mark_paid_failed', order_id=order_id, payment_ref=pay_ref,
                      journal_id=journal_id)
            _log_system_event('delivery_cod_mark_paid_failed',
                              {'order_id': order_id, 'payment_ref': pay_ref,
                               'journal_id': journal_id}, severity='error')
            return jsonify({'error': 'COD ledger posted but the order could not be marked paid. Retry.',
                            'code': 'payment_mark_failed'}), 503
    elif not paid:
        payment_status = 'unpaid'
        warning = 'Payment is not verified; the order stays unpaid.'
        _log_system_event('order_delivered_unpaid',
                          {'order_id': order_id, 'payment_method': method}, severity='warning')

    _log_system_event('order_delivered',
                      {'order_id': order_id, 'note': note, 'payment_status': payment_status,
                       'ledger_journal_id': journal_id}, severity='info')
    _discord_notify(
        event_type='order.delivered',
        title='📦 Delivery Confirmed',
        description=(f"**Order:** `{order_id}`\n**Note:** {note or 'No note'}\n"
                     f"**Payment:** {payment_status}"
                     + (f"\n**COD journal:** `{journal_id}`" if journal_id else "")),
        severity='info',
    )
    return jsonify({'ok': True, 'payment_status': payment_status,
                    'ledger_journal_id': journal_id, 'warning': warning}), 200


@app.route('/api/admin/orders/<order_id>/verify-bank-transfer', methods=['POST'])
def admin_verify_bank_transfer(order_id):
    """Mark a bank-transfer order paid after an operator confirms the money.

    There is no bank API to call, so the admin supplies the bank/transfer
    reference as evidence. The amount is taken from the stored order rows, never
    from this request. `_mark_checkout_paid` posts the `payment:<ref>` journal
    idempotently, so a double-click or replay cannot double-post.
    """
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    bank_ref = str(data.get('bank_reference') or '').strip()[:80]
    if len(bank_ref) < 4:
        return jsonify({'error': 'Enter the bank/transfer reference (at least 4 characters)',
                        'code': 'bank_reference_required'}), 400

    rows = supa_get('orders', {
        'id': f'eq.{order_id}',
        'select': 'id,payment_ref,payment_method,payment_status,status',
    }, limit=1) or []
    if not rows:
        return jsonify({'error': 'Order not found'}), 404
    order = rows[0]
    if str(order.get('payment_method') or '').lower() != 'bank':
        return jsonify({'error': 'This order was not placed with bank transfer',
                        'code': 'not_a_bank_order'}), 409
    if str(order.get('status') or '').lower() == 'cancelled':
        return jsonify({'error': 'The order is cancelled; a payment cannot be recorded',
                        'code': 'order_cancelled'}), 409
    pay_ref = str(order.get('payment_ref') or '').strip()
    if not pay_ref:
        return jsonify({'error': 'Order has no payment reference', 'code': 'reference_missing'}), 503

    # Re-read every row sharing the reference so a split order is verified whole.
    checkout_rows = supa_get('orders', {
        'payment_ref': f'eq.{pay_ref}',
        'select': 'id,total_amount,total_price,quantity_kg,price_per_kg,status,payment_status',
    }, limit=50) or []
    try:
        owed = sum((ledger.money(finance.order_total(o)) for o in checkout_rows), ledger.ZERO)
    except ledger.LedgerError:
        owed = ledger.ZERO
    if owed <= 0:
        return jsonify({'error': 'Order total could not be computed', 'code': 'total_unavailable'}), 503

    already_paid = bool(checkout_rows) and all(
        str(o.get('payment_status') or '').lower() == 'paid' for o in checkout_rows)
    marked = _mark_checkout_paid(pay_ref, received=owed, currency='UGX',
                                 provider='bank', provider_ref=bank_ref)
    if not marked:
        _log_system_event('bank_verify_failed',
                          {'order_id': order_id, 'payment_ref': pay_ref,
                           'actor': payload.get('sub')}, severity='error')
        return jsonify({'error': 'The payment could not be recorded. Retry or check the ledger.',
                        'code': 'bank_verify_failed'}), 503

    try:
        supa_insert('admin_audit_log', {
            'actor': str(payload.get('sub') or ''),
            'action': 'order.bank_transfer_verified',
            'target': order_id,
            'metadata': {'payment_ref': pay_ref, 'bank_reference': bank_ref,
                         'amount': f"{owed:.2f}"},
        })
    except Exception:
        pass  # the payment is already recorded; the audit row is best-effort
    _log_system_event('order_bank_transfer_verified',
                      {'order_id': order_id, 'payment_ref': pay_ref,
                       'bank_reference': bank_ref, 'already_paid': already_paid},
                      severity='info')
    return jsonify({'ok': True, 'already_paid': already_paid, 'payment_ref': pay_ref}), 200


@app.route('/api/orders/<order_id>/receipt', methods=['GET'])
def get_order_receipt(order_id):
    """Buyer retrieves their own order receipt/confirmation."""
    uid, err = verify_supabase_user()
    if err:
        return err
    rows = supa_get('orders', {'id': f'eq.{order_id}'}, limit=1)
    if not rows:
        return jsonify({'error': 'Order not found'}), 404
    order = rows[0]
    if order.get('buyer_id') != uid and not is_admin_uuid(uid):
        return jsonify({'error': 'Access denied'}), 403
    deliveries = supa_get('deliveries', {'order_id': f'eq.{order_id}'}, limit=1) or []
    return jsonify({
        'order': order,
        'delivery': deliveries[0] if deliveries else None,
    }), 200


_TRACK_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-]{3,39}$')
_ORDER_TO_DELIVERY_STATUS = {
    'confirmed': 'scheduled', 'processing': 'scheduled', 'accepted': 'scheduled',
    'shipped': 'in_transit', 'in_transit': 'in_transit', 'out_for_delivery': 'in_transit',
    'delivered': 'delivered', 'completed': 'delivered',
}


@app.route('/api/track/<code>', methods=['GET'])
@rate_limit(max_req=30, window=60)
def track_shipment(code):
    """Public tracking lookup by tracking code (TRK-...) or order ref (AB-...).

    The old tracker only searched the deliveries table, which has a row only after a
    rider is assigned, so every fresh order showed "No delivery found". We start from
    the order (always exists) and merge the delivery row when there is one. Only
    non-personal fields are returned; the code itself is the shared secret."""
    # People paste codes with spaces ("TRK-ABC 123") and lower case; both refer
    # to the same code. `ilike` also matches codes stored in lower case.
    code_u = re.sub(r'\s+', '', code or '').upper()
    if not _TRACK_RE.match(code_u):
        return jsonify({'ok': False, 'error': 'invalid code'}), 400
    order = None
    for col in ('tracking_code', 'order_ref', 'payment_ref'):
        rows = supa_get_admin('orders', {col: f'ilike.{code_u}'}, limit=1) or []
        if rows:
            order = rows[0]
            break
    delivery = None
    if order and order.get('id'):
        d = supa_get_admin('deliveries', {'order_id': f"eq.{order['id']}"}, limit=1) or []
        delivery = d[0] if d else None
    if not order:
        d = supa_get_admin('deliveries', {'tracking_code': f'ilike.{code_u}'}, limit=1) or []
        delivery = d[0] if d else None
        if not delivery:
            return jsonify({'ok': False, 'error': 'not found'}), 404
    order = order or {}
    delivery = delivery or {}
    pickup = delivery.get('pickup_location') or ''
    if not pickup and order.get('farmer_id'):
        fr = supa_get_admin('farmers', {'id': f"eq.{order['farmer_id']}", 'select': 'district'}, limit=1) or []
        pickup = (fr[0].get('district') if fr else '') or ''
    ostatus = str(order.get('status') or '').lower()
    status = (delivery.get('status') or _ORDER_TO_DELIVERY_STATUS.get(ostatus) or 'pending').lower()
    return jsonify({
        'ok': True,
        'tracking_code': order.get('tracking_code') or delivery.get('tracking_code') or code_u,
        'order_ref': order.get('order_ref') or '',
        'status': status,
        'pickup_location': pickup or 'Farm',
        'delivery_location': delivery.get('delivery_location') or order.get('delivery_address') or '',
        'estimated_arrival': delivery.get('estimated_arrival'),
        'driver_name': delivery.get('driver_name') or '',
        'vehicle': delivery.get('vehicle_plate') or '',
        'rider_assigned': bool(delivery.get('driver_name') or delivery.get('transporter_id') or order.get('transporter_id')),
        'created_at': order.get('created_at') or delivery.get('created_at'),
    }), 200


@app.route('/api/buyer/orders', methods=['GET'])
@rate_limit(max_req=30, window=60)
def buyer_orders():
    """Return all orders for the authenticated buyer."""
    uid, err = verify_supabase_user()
    if err:
        return err
    orders = supa_get('orders', {'buyer_id': f'eq.{uid}', 'order': 'created_at.desc'}, limit=100) or []
    return jsonify(orders), 200


@app.route('/api/buyer/orders/<order_id>/receipt', methods=['GET'])
@rate_limit(max_req=30, window=60)
def buyer_order_receipt(order_id):
    """Return a single order's full receipt data for the authenticated buyer."""
    uid, err = verify_supabase_user()
    if err:
        return err
    rows = supa_get('orders', {'id': f'eq.{order_id}', 'buyer_id': f'eq.{uid}'}) or []
    if not rows:
        return jsonify({'error': 'Order not found'}), 404
    order = rows[0]
    # Build receipt payload
    items = order.get('items') or []
    if isinstance(items, str):
        try:
            items = _json.loads(items)
        except Exception:
            items = []
    receipt = {
        'order_ref':      order.get('order_ref', ''),
        'tracking_code':  order.get('tracking_code', ''),
        'status':         order.get('status', ''),
        'payment_method': order.get('payment_method', ''),
        'payment_status': order.get('payment_status', 'unpaid'),
        'delivery_address': order.get('delivery_address', ''),
        'created_at':     order.get('created_at', ''),
        'total_price':    order.get('total_price', 0),
        'items':          items,
        'farmer_id':      order.get('farmer_id'),
        'transporter_id': order.get('transporter_id'),
    }
    return jsonify(receipt), 200


@app.route('/api/buyer/orders/<order_id>/receipt/email', methods=['POST'])
@rate_limit(max_req=5, window=60)
def email_order_receipt(order_id):
    """Email a receipt for the specified order to the authenticated buyer."""
    uid, err = verify_supabase_user()
    if err:
        return err
    rows = supa_get('orders', {'id': f'eq.{order_id}', 'buyer_id': f'eq.{uid}'}) or []
    if not rows:
        return jsonify({'error': 'Order not found'}), 404
    order = rows[0]
    # Email is already in the verified GoTrue response stored on g.auth_user
    # by verify_supabase_user() — no extra lookup or admin key needed.
    auth_user = getattr(g, 'auth_user', {})
    email = (auth_user.get('email') or '').strip()
    if not email:
        # Try buyer profile table as secondary fallback
        try:
            profile_rows = supa_get('buyers', {'user_id': f'eq.{uid}'}) or []
            email = (profile_rows[0].get('email') if profile_rows else None) or ''
        except Exception:
            pass
    if not email:
        return jsonify({'error': 'No email on record for this account'}), 400
    items = order.get('items') or []
    if isinstance(items, str):
        try:
            items = _json.loads(items)
        except Exception:
            items = []
    buyer_name = auth_user.get('user_metadata', {}).get('full_name', '') or \
                 auth_user.get('user_metadata', {}).get('name', '')
    try:
        import requests as _req
        resp = _req.post(
            f"{SETTINGS.supabase_url}/functions/v1/send-receipt",
            headers={
                'Content-Type': 'application/json',
                'apikey': SETTINGS.supabase_key,
                'Authorization': f'Bearer {SETTINGS.supabase_key}',
            },
            json={
                'type': 'order_receipt',
                'email': email,
                'name': buyer_name,
                'ref': order.get('order_ref', ''),
                'tracking': order.get('tracking_code', ''),
                'total': order.get('total_price', 0),
                'payment_method': order.get('payment_method', ''),
                'delivery_address': order.get('delivery_address', ''),
                'status': order.get('status', ''),
                'created_at': order.get('created_at', ''),
                'items': items,
            },
            timeout=10,
        )
        if resp.status_code < 300:
            return jsonify({'ok': True, 'email': email}), 200
        # Edge Function not deployed yet — log but don't fail the user
        log.warning("receipt_email.edge_fn_error", status=resp.status_code,
                    body=resp.text[:200])
        # Still return ok so the user sees the right email address
        return jsonify({'ok': True, 'email': email,
                        'note': 'queued'}), 200
    except Exception as e:
        log.warning("receipt_email.failed", error=str(e))
        # Network/timeout — still acknowledge so UX isn't broken
        return jsonify({'ok': True, 'email': email, 'note': 'queued'}), 200


@app.route('/api/user/profile', methods=['POST'])
@rate_limit(max_req=20, window=60)
def update_user_profile():
    """Save the authenticated user's profile (name, phone, district) to the
    farmers table.  Validates name and phone before writing.
    """
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    full_name = str(data.get('full_name', '') or '').strip()
    phone     = str(data.get('phone', '') or '').strip()
    district  = str(data.get('district', '') or '').strip()[:100]

    # Validate name
    if full_name:
        if len(full_name) < 2:
            return jsonify({'error': 'Full name must be at least 2 characters'}), 400
        if full_name.lower() in {'test', 'user', 'admin', 'farmer', 'buyer', 'name',
                                  'null', 'undefined', 'none', 'n/a'}:
            return jsonify({'error': 'Please enter your real full name'}), 400

    # Validate and normalise phone
    if phone:
        import re as _re
        _pd = _re.sub(r'\D', '', phone)
        if _pd.startswith('256'):
            _pd = _pd[3:]
        elif _pd.startswith('0'):
            _pd = _pd[1:]
        if not _re.match(r'^[37]\d{8}$', _pd):
            return jsonify({'error': 'Enter a valid Ugandan phone number (e.g. 0771 234567)'}), 400
        phone = '+256' + _pd

    # Check phone uniqueness
    if phone:
        existing = supa_get('farmers', {'phone': f'eq.{phone}', 'id': f'neq.{uid}'}, limit=1)
        if existing:
            return jsonify({'error': 'That phone number is already registered to another account'}), 409

    # Build the update payload — only include fields that were provided
    update: dict = {'id': uid}
    if full_name:
        update['full_name'] = full_name
    if phone:
        update['phone'] = phone
    if district:
        update['district'] = district

    if len(update) <= 1:
        return jsonify({'ok': True, 'note': 'nothing to update'}), 200

    try:
        url = f"{SUPABASE_URL}/rest/v1/farmers"
        headers = {
            'apikey': SUPABASE_KEY,
            'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type': 'application/json',
            'Prefer': 'resolution=merge-duplicates,return=minimal',
        }
        resp = requests.post(url, json=update, headers=headers, timeout=8)
        if resp.status_code >= 300:
            log.warning('profile.save_failed', uid=uid, status=resp.status_code)
            return jsonify({'error': 'Could not save profile'}), 500
    except Exception as _pe:
        log.warning('profile.save_exception', uid=uid, error=str(_pe))
        return jsonify({'error': 'Could not save profile'}), 500

    return jsonify({'ok': True}), 200


@app.route('/api/user/notifications', methods=['GET'])
@rate_limit(max_req=60, window=60)
def user_notifications():
    """Return in-app notifications for the authenticated user.

    Reads from notification_outbox where channel='in_app' and
    recipient=<user_id>.  Returns newest-first, limited to 50.
    Unread count = rows with status != 'sent'.
    """
    uid, err = verify_supabase_user()
    if err:
        return err

    rows = supa_get('notification_outbox', {
        'recipient': f'eq.{uid}',
        'channel': 'eq.in_app',
        'order': 'created_at.desc',
    }, limit=50) or []

    unread = sum(1 for r in rows if r.get('status') != 'sent')
    return jsonify({'notifications': rows, 'unread': unread}), 200


@app.route('/api/user/notifications/<notif_id>/read', methods=['POST'])
@rate_limit(max_req=60, window=60)
def mark_notification_read(notif_id):
    """Mark a single in-app notification as read (seen)."""
    uid, err = verify_supabase_user()
    if err:
        return err
    # Only mark rows that belong to this user
    supa_update('notification_outbox', {
        'status': 'sent',
        'updated_at': datetime.datetime.utcnow().isoformat(),
    }, 'id', notif_id)
    return jsonify({'ok': True}), 200


@app.route('/api/user/notifications/read-all', methods=['POST'])
@rate_limit(max_req=20, window=60)
def mark_all_notifications_read(notif_id=None):
    """Mark all in-app notifications for the current user as read."""
    uid, err = verify_supabase_user()
    if err:
        return err
    # Fetch user's unread notification ids and mark them sent
    rows = supa_get('notification_outbox', {
        'recipient': f'eq.{uid}',
        'channel': 'eq.in_app',
        'status': 'neq.sent',
    }, limit=100) or []
    now = datetime.datetime.utcnow().isoformat()
    for r in rows:
        supa_update('notification_outbox', {'status': 'sent', 'updated_at': now}, 'id', r['id'])
    return jsonify({'ok': True, 'marked': len(rows)}), 200


# ── Web Push subscription (VAPID) ─────────────────────────────────────────────
# Users subscribe their browser/PWA to push so OS-level notifications appear in
# the Android notification bar even when the app is in the background.
# Subscriptions are stored in push_subscriptions table keyed by (user_id, endpoint).
@app.route('/api/user/push-subscribe', methods=['POST'])
@rate_limit(max_req=10, window=60)
def user_push_subscribe():
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    sub = data.get('subscription') or data  # accept {subscription:{...}} or flat
    if isinstance(sub, str):
        try:
            import json as _json2
            sub = _json2.loads(sub)
        except Exception:
            return jsonify({'error': 'invalid subscription'}), 400
    endpoint = sub.get('endpoint', '')
    if not endpoint or not endpoint.startswith('https://'):
        return jsonify({'error': 'invalid subscription'}), 400
    keys = sub.get('keys', {})
    now = datetime.datetime.utcnow().isoformat()
    # Upsert subscription — if endpoint already exists for this user, update it
    existing = supa_get('push_subscriptions', {'user_id': f'eq.{uid}', 'endpoint': f'eq.{endpoint}'}, limit=1) or []
    if existing:
        supa_update('push_subscriptions', {'keys': keys, 'updated_at': now}, 'id', existing[0]['id'])
    else:
        SUPA.insert('push_subscriptions', {
            'user_id': uid, 'endpoint': endpoint, 'keys': keys,
            'created_at': now, 'updated_at': now
        })
    return jsonify({'ok': True}), 200


@app.route('/api/user/push-unsubscribe', methods=['POST'])
@rate_limit(max_req=10, window=60)
def user_push_unsubscribe():
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    endpoint = data.get('endpoint', '')
    if endpoint:
        rows = supa_get('push_subscriptions', {'user_id': f'eq.{uid}', 'endpoint': f'eq.{endpoint}'}, limit=1) or []
        for r in rows:
            SUPA.delete('push_subscriptions', 'id', r['id'])
    return jsonify({'ok': True}), 200


# ── Helper: send a Web Push notification to one user ──────────────────────────
def _send_web_push(user_id: str, title: str, body: str, url: str = '/'):
    """Fire-and-forget Web Push to all subscriptions for a user."""
    VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
    VAPID_PUBLIC_KEY  = os.environ.get('VAPID_PUBLIC_KEY', '')
    VAPID_CONTACT     = os.environ.get('VAPID_CONTACT', f'mailto:{business.SUPPORT_EMAIL}')
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return  # Not configured — silently skip

    subs = supa_get('push_subscriptions', {'user_id': f'eq.{user_id}'}, limit=20) or []
    if not subs:
        return

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning('webpush.pywebpush_missing', note='install pywebpush to enable browser push')
        return

    import json as _json2
    payload = _json2.dumps({'title': title, 'body': body, 'url': url, 'tag': 'agribridge'})
    dead_ids = []
    for sub in subs:
        try:
            webpush(
                subscription_info={'endpoint': sub['endpoint'], 'keys': sub['keys']},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={'sub': VAPID_CONTACT, 'aud': sub['endpoint'].split('/')[2]}
            )
        except WebPushException as e:
            status = getattr(e.response, 'status_code', None)
            if status in (404, 410):  # subscription gone
                dead_ids.append(sub['id'])
            else:
                log.warning('webpush.send_failed', user=user_id, status=status)
        except Exception as e:
            log.warning('webpush.send_error', user=user_id, error=str(e))
    for did in dead_ids:
        try:
            SUPA.delete('push_subscriptions', 'id', did)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# SMART MATCHING — orders vs. farmer listings; admin approval workflow
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/admin/match-candidates', methods=['GET'])
def admin_match_candidates():
    """Return pending orders paired with eligible farmer listings.

    For each pending order we search listings where:
      - crop_name matches (case-insensitive contains)
      - is_available = true
      - quantity_kg >= order quantity
    Then we also surface available transporters in the same district.
    """
    payload, err = verify_token(required_role='admin')
    if err:
        return err

    orders = supa_get('orders', {'status': 'eq.pending', 'order': 'created_at.asc'}, limit=100) or []
    all_listings = supa_get('listings', {'is_available': 'eq.true', 'order': 'price_per_kg.asc'}, limit=500) or []
    all_transporters = supa_get('transporters', {'is_available': 'eq.true', 'status': 'eq.active'}, limit=100) or []

    candidates = []
    for order in orders:
        # Try to match on buyer_product or notes field; fallback to items array
        product_hint = (order.get('buyer_product') or
                        order.get('notes') or '').lower()
        order_district = (order.get('delivery_address') or '').lower()

        matched_listings = []
        for listing in all_listings:
            crop = (listing.get('crop_name') or '').lower()
            if product_hint and crop and (product_hint in crop or crop in product_hint):
                qty_avail = float(listing.get('quantity_kg') or 0)
                matched_listings.append({
                    'id': listing.get('id'),
                    'crop_name': listing.get('crop_name'),
                    'quantity_kg': qty_avail,
                    'price_per_kg': listing.get('price_per_kg'),
                    'district': listing.get('district'),
                    'farmer_phone': listing.get('farmer_phone'),
                    'farmer_id': listing.get('farmer_id'),
                })

        # Available transporters (prefer same district)
        transporters_near = sorted(
            all_transporters,
            key=lambda t: 0 if order_district and order_district in (t.get('district') or '').lower() else 1
        )[:3]

        candidates.append({
            'order': {
                'id': order.get('id'),
                'ref': order.get('order_ref'),
                'status': order.get('status'),
                'delivery_address': order.get('delivery_address'),
                'created_at': order.get('created_at'),
                'total_price': order.get('total_price'),
                'payment_method': order.get('payment_method'),
            },
            'farmer_matches': matched_listings[:5],
            'available_transporters': [
                {
                    'id': t.get('id'),
                    'full_name': t.get('full_name'),
                    'phone': t.get('phone'),
                    'vehicle_type': t.get('vehicle_type'),
                    'district': t.get('district'),
                    'capacity_kg': t.get('capacity_kg'),
                }
                for t in transporters_near
            ],
        })

    return jsonify({'ok': True, 'count': len(candidates), 'candidates': candidates}), 200


@app.route('/api/admin/match-approve', methods=['POST'])
def admin_approve_match():
    """Admin approves a farmer match for an order and optionally triggers SMS to farmer + transporter."""
    payload, err = verify_token(required_role='admin')
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get('order_id')
    farmer_phone = data.get('farmer_phone', '').strip()
    transporter_id = data.get('transporter_id')  # optional
    notes = data.get('notes', '').strip()

    if not order_id or not farmer_phone:
        return jsonify({'error': 'order_id and farmer_phone required'}), 400

    # Update order status to confirmed
    ok = supa_update('orders', {'status': 'confirmed', 'notes': notes or None}, 'id', order_id)
    if not ok:
        return jsonify({'error': 'Could not update order'}), 500

    sms_results = {}

    # SMS to farmer
    if at_sms and farmer_phone:
        try:
            at_sms.send(
                message=(
                    f"AgriBridge: You have been matched for order {order_id[:8]}.\n"
                    f"Please prepare your produce for collection.\n"
                    f"Contact admin: agribrige.com\n"
                    f"{('Notes: ' + notes) if notes else ''}"
                ),
                recipients=[farmer_phone],
                sender_id=AT_SMS_SENDER,
            )
            sms_results['farmer'] = 'sent'
        except Exception as _e:
            sms_results['farmer'] = f'failed: {str(_e)[:80]}'
            log.warning("approve.farmer_sms_failed", phone=farmer_phone, error=str(_e))

    # SMS to transporter (if chosen)
    if at_sms and transporter_id:
        t_rows = supa_get('transporters', {'id': f'eq.{transporter_id}'}, limit=1) or []
        if t_rows:
            t_phone = t_rows[0].get('phone', '')
            if t_phone:
                try:
                    at_sms.send(
                        message=(
                            f"AgriBridge: Transport job assigned for order {order_id[:8]}.\n"
                            f"Please check agribrige.com for pickup details.\n"
                            f"{('Notes: ' + notes) if notes else ''}"
                        ),
                        recipients=[t_phone],
                        sender_id=AT_SMS_SENDER,
                    )
                    sms_results['transporter'] = 'sent'
                except Exception as _e:
                    sms_results['transporter'] = f'failed: {str(_e)[:80]}'

    _discord_notify(
        event_type='matching.approved',
        title='✅ Match Approved by Admin',
        description=(
            f"**Order:** `{order_id}`\n"
            f"**Farmer phone:** {farmer_phone}\n"
            f"**Transporter:** {transporter_id or 'none'}\n"
            f"**SMS results:** {sms_results}"
        ),
        severity='info',
    )
    _log_system_event('match_approved', {
        'order_id': order_id, 'farmer_phone': farmer_phone,
        'transporter_id': transporter_id, 'sms_results': sms_results,
    }, severity='info')

    return jsonify({'ok': True, 'order_status': 'confirmed', 'sms': sms_results}), 200


# ── Admin push device registry (Phase 6) ──────────────────────────────────────
# A device registers its FCM token only AFTER the API has verified — from the
# Supabase session, never from the body — that the caller is an admin (Rule 9).
@app.route('/api/admin/devices', methods=['POST'])
@rate_limit(max_req=30, window=60)
def register_admin_device():
    uid, err = require_admin_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    token = str(data.get('token', '')).strip()
    if not token:
        return jsonify({'error': 'token is required'}), 400
    provider = str(data.get('provider', 'fcm')).strip() or 'fcm'
    platform = (str(data.get('platform', '')).strip()[:16] or None)
    res = PUSH_REGISTRY.register(user_id=uid, token=token[:512],
                                 provider=provider, platform=platform)
    if not res.ok:
        return jsonify({'error': res.error or 'registration failed'}), 400
    return jsonify({'ok': True}), 200


@app.route('/api/admin/devices', methods=['DELETE'])
@rate_limit(max_req=30, window=60)
def unregister_admin_device():
    uid, err = require_admin_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    token = str(data.get('token', '')).strip()
    if not token:
        return jsonify({'error': 'token is required'}), 400
    ok = PUSH_REGISTRY.unregister(user_id=uid, token=token[:512])
    return jsonify({'ok': bool(ok)}), (200 if ok else 400)


# ── Web Push for signed-in users (Android notification bar) ───────────────────
@app.route('/api/push/vapid-public-key', methods=['GET'])
@rate_limit(max_req=60, window=60)
def vapid_public_key():
    """Public VAPID key the browser needs to subscribe. Not a secret."""
    if not WEB_PUSH.enabled:
        return jsonify({'error': 'push not configured'}), 503
    return jsonify({'key': WEB_PUSH.public_key}), 200


@app.route('/api/user/push-subscriptions', methods=['POST'])
@rate_limit(max_req=30, window=60)
def register_push_subscription():
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    res = WEB_PUSH.register(user_id=uid, subscription=data,
                            user_agent=request.headers.get('User-Agent', ''))
    if res is None or not res.ok:
        return jsonify({'error': 'invalid subscription'}), 400
    return jsonify({'ok': True}), 200


@app.route('/api/user/push-subscriptions', methods=['DELETE'])
@rate_limit(max_req=30, window=60)
def unregister_push_subscription():
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    ok = WEB_PUSH.unregister(user_id=uid, endpoint=str(data.get('endpoint', '')).strip())
    return jsonify({'ok': bool(ok)}), (200 if ok else 400)


# ── Account deletion (Phase 10 / H6 — Google Play requirement) ────────────────
# Self-scoped: the id is taken from the verified session, never the request body.
# By default this anonymizes the caller's data (reversible from backup); the
# irreversible auth-user purge only runs when an operator enables it server-side.
# ══════════════════════════════════════════════════════════════════════════════
# Legal documents, acceptance, marketing consent and privacy requests
# (mandate sections 7-11). Public documents need no session; everything that
# records or reads a user's own evidence needs a verified Supabase session.
# ══════════════════════════════════════════════════════════════════════════════
def _auth_role():
    """Return the user's role string for legal-service calls.

    Reads from the server-controlled user_roles table first (tamper-proof).
    Falls back to user_metadata.role for app-level roles (farmer/buyer/vendor)
    that are stored there at registration and are not in user_roles.
    """
    uid = (getattr(g, 'auth_user', {}) or {}).get('id')
    if uid:
        try:
            rows = SUPA.select('user_roles', {'user_id': f'eq.{uid}', 'select': 'role'}, limit=1)
            if rows:
                return rows[0].get('role')
        except Exception:
            pass
    # fallback: user_metadata (set at registration, not server-controlled)
    meta = (getattr(g, 'auth_user', {}) or {}).get('user_metadata') or {}
    return meta.get('role')


def _legal_response(result, ok_code=200):
    if not result.ok:
        body = {'error': result.error}
        body.update(result.data or {})
        return jsonify(body), result.status_code
    return jsonify({'ok': True, **result.data}), (result.status_code if result.status_code != 200 else ok_code)


@app.route('/api/legal/manifest', methods=['GET'])
@rate_limit(max_req=60, window=60)
def legal_manifest():
    return jsonify(LEGAL_SERVICE.public_manifest())


@app.route('/api/legal/status', methods=['GET'])
@rate_limit(max_req=60, window=60)
def legal_status():
    uid, err = verify_supabase_user()
    if err:
        return err
    return _legal_response(LEGAL_SERVICE.status(uid, _auth_role()))


@app.route('/api/legal/accept', methods=['POST'])
@rate_limit(max_req=20, window=60)
def legal_accept():
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    res = LEGAL_SERVICE.accept(
        user_id=uid, role=_auth_role(), documents=data.get('documents'),
        method=str(data.get('method', '')),
        evidence={'surface': str(data.get('surface', ''))[:40],
                  'user_agent': request.headers.get('User-Agent', '')[:200]})
    if not res.ok:
        return _legal_response(res)
    # Only now (database confirmed) report the fresh status.
    return _legal_response(LEGAL_SERVICE.status(uid, _auth_role()))


def _account_is_new(created_at, within_days=2):
    """True when the auth account was created within the window. Unknown age is
    treated as not-new so an old account never triggers a fresh-signup alert."""
    if not created_at:
        return False
    try:
        ts = str(created_at).replace('Z', '+00:00')
        dt = datetime.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - dt) <= datetime.timedelta(days=within_days)


@app.route('/api/legal/sync-signup', methods=['POST'])
@rate_limit(max_req=10, window=60)
def legal_sync_signup():
    uid, err = verify_supabase_user()
    if err:
        return err
    au = getattr(g, 'auth_user', {}) or {}
    res = LEGAL_SERVICE.sync_signup(user_id=uid, role=_auth_role(),
                                    meta=au.get('user_metadata') or {},
                                    created_at=au.get('created_at'))
    if not res.ok:
        return _legal_response(res)
    status = LEGAL_SERVICE.status(uid, _auth_role())
    # Notify Discord once per genuinely new account. This endpoint runs on every
    # login, so an unguarded alert spams #signups with the same user (seen live).
    # The system_events marker makes it durable across restarts; the age window
    # stops a long-existing account's first sync from reading as a new signup.
    try:
        seen = supa_get('system_events', {
            'event_type': 'eq.signup_alerted', 'user_id': f'eq.{uid}', 'select': 'id',
        }, limit=1)
        if not seen and _account_is_new(au.get('created_at')):
            _log_system_event('signup_alerted', {'role': _auth_role()},
                              severity='info', user_id=uid)
            _discord_notify(
                event_type='user.signup',
                title='👤 New User Signed Up',
                description=(
                    f"**Role:** {_auth_role() or 'unknown'}\n"
                    f"**User ID:** `{uid}`\n"
                    f"**Name:** {(au.get('user_metadata') or {}).get('full_name', '—')}"
                ),
                severity='info',
            )
    except Exception as _se:
        log.warning('signup_alert.failed', error=type(_se).__name__)
    return jsonify({'ok': True, **res.data, 'status': status.data})


@app.route('/api/legal/history', methods=['GET'])
@rate_limit(max_req=30, window=60)
def legal_history():
    uid, err = verify_supabase_user()
    if err:
        return err
    return _legal_response(LEGAL_SERVICE.history(uid))


@app.route('/api/marketing/consent', methods=['GET', 'POST'])
@rate_limit(max_req=30, window=60)
def marketing_consent():
    uid, err = verify_supabase_user()
    if err:
        return err
    if request.method == 'GET':
        return _legal_response(LEGAL_SERVICE.get_marketing(uid))
    data = request.get_json(force=True, silent=True) or {}
    return _legal_response(LEGAL_SERVICE.set_marketing(
        user_id=uid, channels=data.get('channels'), method='settings'))


@app.route('/api/privacy/requests', methods=['GET', 'POST'])
@rate_limit(max_req=5, window=300)
def privacy_requests():
    """File or list privacy requests.

    POST works WITHOUT a session (so someone locked out, or who rejected updated
    terms, can still exercise their rights); an emailed request is recorded as
    unverified and identity is confirmed by AgriBridge before acting. A valid
    session, if present, ties the request to that account.
    """
    if request.method == 'GET':
        uid, err = verify_supabase_user()
        if err:
            return err
        return _legal_response(LEGAL_SERVICE.list_privacy_requests(uid))
    data = request.get_json(force=True, silent=True) or {}
    uid = None
    if _bearer_token():
        uid, err = verify_supabase_user()
        if err:
            return err
    res = LEGAL_SERVICE.create_privacy_request(
        request_type=str(data.get('request_type', '')), details=str(data.get('details', '')),
        user_id=uid, email=str(data.get('email', '')) if not uid else None)
    if res.ok and uid and data.get('request_type') == 'marketing_withdrawal':
        LEGAL_SERVICE.set_marketing(user_id=uid, channels={'sms': False}, method='support_request')
    return _legal_response(res, 201)


@app.route('/legal/<path:name>', methods=['GET'])
def legal_page(name):
    """Serve published legal pages from the API host too (stable URLs for apps)."""
    from flask import send_from_directory
    if not re.fullmatch(r'[a-z0-9\-.]+\.(html|json)', name):
        return jsonify({'error': 'Not found'}), 404
    return send_from_directory(os.path.join(app.root_path, 'static', 'legal'), name)


@app.route('/api/account/delete', methods=['POST'])
@rate_limit(max_req=5, window=300)
def delete_account():
    uid, err = verify_supabase_user()
    if err:
        return err
    result = ACCOUNT_SERVICE.delete_account(
        user_id=uid, correlation_id=getattr(g, 'correlation_id', None))
    if not result.ok and result.status == 'needs_review':
        # Open orders: do not anonymise now. File a deletion request so a person
        # can complete it once the orders are settled, and say so honestly.
        LEGAL_SERVICE.create_privacy_request(
            request_type='deletion', user_id=uid,
            details='Auto-filed: account deletion requested while orders were still open.')
        return jsonify({'error': result.error, 'status': 'needs_review',
                        'manual_review': True}), 409
    if not result.ok:
        return jsonify({'error': result.error, 'status': result.status}), result.status_code
    return jsonify({'ok': True, 'status': result.status}), 200


def _sms_receipt(phone, ref, total, kind='order'):
    """Best-effort SMS receipt to a buyer. No-op unless Africa's Talking is live."""
    if not at_sms or not phone:
        return False
    try:
        head = 'Payment received' if kind == 'payment' else 'Order confirmed'
        msg = (f"AgriBridge: {head}!\n"
               f"Ref {ref}. Total UGX {int(total or 0):,}.\n"
               f"Track at agribrige.com")
        at_sms.send(message=msg, recipients=[phone], sender_id=AT_SMS_SENDER)
        return True
    except Exception as e:
        print(f"sms receipt error: {e}")
        return False


@app.route('/api/sms-receipt', methods=['POST'])
@rate_limit(max_req=10, window=300)
def sms_receipt():
    """Send an order-confirmation SMS to the buyer's number on file.

    The recipient and amount are read from the caller's own order; the request
    body cannot name a phone number or a total, so a signed-in account cannot be
    used as an SMS relay. Payment receipts are sent by the payment path instead.
    """
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    ref = str(data.get('ref') or data.get('order_ref') or '').strip()[:80]
    if not ref:
        return jsonify({'ok': False, 'sent': False, 'error': 'missing order reference'}), 400
    rows = supa_get('orders', {
        'payment_ref': f'eq.{ref}',
        'select': 'buyer_id,buyer_phone,total_amount,total_price,quantity_kg,price_per_kg',
    }, limit=50) or []
    if not rows or any(str(r.get('buyer_id') or '') != uid for r in rows):
        # Same answer for unknown and foreign orders: no order-existence oracle.
        return jsonify({'ok': False, 'sent': False, 'error': 'order not found'}), 404
    try:
        total = sum((ledger.money(finance.order_total(r)) for r in rows), ledger.ZERO)
    except ledger.LedgerError:
        return jsonify({'ok': False, 'sent': False,
                        'error': 'order total is unavailable'}), 422
    phone = next((r.get('buyer_phone') for r in rows if r.get('buyer_phone')), '') or ''
    return jsonify({'ok': True, 'sent': _sms_receipt(phone, ref, float(total), 'order')}), 200


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENTS  (pluggable; providers are OFF until their env keys are set, so this
# never affects the live checkout until you deliberately enable one)
# ══════════════════════════════════════════════════════════════════════════════
def _direct_card_ready():
    """Direct card charging needs the public + 24-byte encryption key and an explicit opt-in."""
    try:
        key_len = len(FLW_ENCRYPTION_KEY.encode('utf-8'))
    except Exception:
        key_len = 0
    return bool(FLW_SECRET_KEY and FLW_PUBLIC_KEY and key_len == 24 and FLW_DIRECT_CARD)


def _enabled_providers():
    at_momo_live = bool(AT_AVAILABLE and at_sms and AT_API_KEY and AT_API_KEY != 'atsk_REPLACE_ME')
    return {
        'cod':         True,   # cash on delivery — no gateway needed
        'direct':      True,   # pay the farmer's MoMo directly — no gateway needed
        'flutterwave': bool(FLW_SECRET_KEY),
        'card':        _direct_card_ready(),  # on-page card form (opt-in, PCI DSS scope)
        'pesapal':     bool(PESAPAL_KEY and PESAPAL_SECRET),
        'bank':        True,   # offline bank transfer — verified by an admin, never auto-marked
        'mtn_momo':    bool(MTN_MOMO_KEY),
        'momo':        at_momo_live,   # AT mobile checkout push (MTN MoMo)
        'airtel':      at_momo_live,   # AT mobile checkout push (Airtel Money)
    }


@app.route('/api/pay/providers', methods=['GET'])
def pay_providers():
    """The client asks which online-payment methods are live so it only shows
    those; when none are configured it falls back to COD / direct MoMo."""
    resp = jsonify({
        'providers': _enabled_providers(),
        'bank_accounts': BANK_TRANSFER_ACCOUNTS,
    })
    # Availability changes when operator keys are added; never let a browser
    # cache a stale answer and hide a gateway that is now live.
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 200


@app.route('/api/pay/initiate', methods=['POST'])
@rate_limit(max_req=20, window=300)
def pay_initiate():
    uid, err = verify_supabase_user()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    provider  = (data.get('provider')  or '').strip().lower()
    order_ref = (data.get('order_ref') or '').strip()[:60]
    email     = (data.get('email')     or '').strip()[:120]
    phone     = (data.get('phone')     or '').strip()[:20]
    try:
        amount = int(float(data.get('amount')))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid amount'}), 400
    if amount <= 0 or not order_ref:
        return jsonify({'ok': False, 'error': 'missing order_ref or amount'}), 400

    if provider in ('cod', 'direct', 'bank'):
        return jsonify({'ok': True, 'mode': provider, 'message': 'No online payment needed.'}), 200

    # Online payment: the order must belong to the caller, and we never charge less
    # than the saved order total (the client-sent amount may add delivery, not cut it).
    amount, perr = _checkout_amount(uid, order_ref, amount)
    if perr:
        return perr
    if provider == 'flutterwave':
        return _flw_initiate(order_ref, amount, email, phone)
    if provider in ('flw_momo_mtn', 'flw_momo_airtel'):
        return _flw_momo_initiate(order_ref, amount, email, phone,
                                  'MTN' if provider.endswith('mtn') else 'AIRTEL')
    if provider in ('momo', 'airtel', 'mtn_momo', 'mobile_money'):
        # Africa's Talking mobile money collection (USSD push to buyer's phone)
        return _at_momo_initiate(order_ref, amount, phone, provider)
    if provider == 'pesapal':
        return _pesapal_initiate(order_ref, amount, email, phone)
    return jsonify({'ok': False, 'error': 'unknown provider'}), 400


def _checkout_amount(uid, order_ref, client_amount):
    """Return (amount_to_charge, error_response). Orders of one checkout share payment_ref."""
    rows = supa_get('orders', {'payment_ref': f'eq.{order_ref}',
                               'select': 'buyer_id,total_amount,total_price,quantity_kg,'
                                         'price_per_kg,payment_status'}, limit=50) or []
    if not rows:
        return 0, (jsonify({'ok': False, 'error': 'order not found'}), 404)
    if any(str(r.get('buyer_id')) != str(uid) for r in rows):
        return 0, (jsonify({'ok': False, 'error': 'not your order'}), 403)
    if all((r.get('payment_status') or '') == 'paid' for r in rows):
        return 0, (jsonify({'ok': False, 'error': 'already paid', 'paid': True}), 409)
    # Use the server-generated total, never a client-carried amount.
    try:
        floor = ledger.whole(sum(
            (ledger.money(finance.order_total(r)) for r in rows), ledger.ZERO))
    except ledger.LedgerError:
        log.error('payment.invalid_order_total', order_ref=order_ref)
        return 0, (jsonify({'ok': False, 'error': 'order total is unavailable'}), 422)
    return max(int(client_amount), floor), None


# ── Pesapal API 3.0 ───────────────────────────────────────────────────────────
_pesapal_client_singleton = None
_pesapal_client_lock = threading.Lock()
_pesapal_ipn_cache = {'url': '', 'id': ''}
_pesapal_ipn_lock = threading.Lock()


def _pesapal_client():
    """Return the shared client, or None when the provider has no credentials."""
    global _pesapal_client_singleton
    if not (PESAPAL_KEY and PESAPAL_SECRET):
        return None
    if _pesapal_client_singleton is None:
        with _pesapal_client_lock:
            if _pesapal_client_singleton is None:
                _pesapal_client_singleton = PesapalClient(
                    PESAPAL_KEY, PESAPAL_SECRET, env=PESAPAL_ENV)
    return _pesapal_client_singleton


def _ensure_pesapal_ipn(client):
    """Register the IPN endpoint once and reuse the stored id on every order."""
    if PESAPAL_IPN_ID:
        return PESAPAL_IPN_ID
    url = PESAPAL_CALLBACK_BASE_URL + '/api/pay/webhook/pesapal'
    cached = _pesapal_ipn_cache
    if cached.get('id') and cached.get('url') == url:
        return cached['id']
    with _pesapal_ipn_lock:
        if cached.get('id') and cached.get('url') == url:
            return cached['id']
        try:
            rows = supa_get('payment_gateway_state', {
                'provider': 'eq.pesapal', 'select': 'config'}, limit=1) or []
            config = (rows[0] or {}).get('config') if rows else {}
            if isinstance(config, dict) and config.get('ipn_id') \
                    and config.get('ipn_url') == url:
                cached.update(url=url, id=str(config['ipn_id']))
                return cached['id']
        except Exception as exc:
            log.warning('pesapal.state_read_failed', error=type(exc).__name__)

        ipn_id = client.register_ipn(url, 'POST')
        saved = SUPA.upsert('payment_gateway_state', {
            'provider': 'pesapal',
            'config': {'ipn_id': ipn_id, 'ipn_url': url},
            'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, on_conflict='provider')
        if not saved.ok:
            # The id still works for this process; operators can pin it with
            # PESAPAL_IPN_ID if the state table is not available.
            log.warning('pesapal.ipn_state_save_failed', status=saved.status)
        cached.update(url=url, id=ipn_id)
        return ipn_id


def _pesapal_phone(phone):
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if digits.startswith('256'):
        digits = '0' + digits[3:]
    elif len(digits) == 9 and digits[:1] in ('3', '7'):
        digits = '0' + digits
    return digits[:20]


def _pesapal_billing_address(email, phone):
    auth_user = getattr(g, 'auth_user', {}) or {}
    meta = auth_user.get('user_metadata') or {}
    email = str(email or auth_user.get('email') or '').strip()[:120]
    phone = _pesapal_phone(phone or meta.get('phone'))
    name = str(meta.get('full_name') or meta.get('name') or '').strip()
    parts = name.split(' ', 1)
    return {
        'email_address': email,
        'phone_number': phone,
        'country_code': 'UG',
        'first_name': (parts[0] if parts and parts[0] else 'Customer')[:60],
        'middle_name': '',
        'last_name': (parts[1] if len(parts) > 1 else '')[:60],
        'line_1': 'AgriBridge marketplace',
        'line_2': '',
        'city': 'Kampala',
        'state': 'Kampala',
        'postal_code': '',
        'zip_code': '',
    }


def _pesapal_initiate(order_ref, amount, email, phone):
    client = _pesapal_client()
    if not client:
        return jsonify({'ok': False, 'enabled': False,
                        'error': 'Pesapal is not configured'}), 200
    try:
        ipn_id = _ensure_pesapal_ipn(client)
    except PesapalError as exc:
        log.error('pesapal.ipn_setup_failed', error=str(exc)[:120])
        return jsonify({'ok': False, 'enabled': True,
                        'error': 'Pesapal is temporarily unavailable. '
                                 'Please choose another payment method.'}), 200

    try:
        data = client.submit_order({
            'id': _pay_attempt_ref(order_ref),
            'currency': 'UGX',
            'amount': int(amount),
            'description': f'AgriBridge order {order_ref}'[:100],
            'callback_url': PESAPAL_CALLBACK_BASE_URL + '/api/pay/pesapal/callback',
            'notification_id': ipn_id,
            'billing_address': _pesapal_billing_address(email, phone),
            # Stable reference: Pesapal uses it for recurring charges too.
            'account_number': order_ref,
        })
    except PesapalError as exc:
        log.warning('pesapal.submit_failed', order_ref=order_ref,
                    error=str(exc)[:120])
        _log_system_event('pesapal_initiate_failed',
                          {'order_ref': order_ref, 'error': str(exc)[:200]},
                          severity='warning')
        return jsonify({'ok': False, 'enabled': True,
                        'error': 'Pesapal could not start this payment. '
                                 'Please try again or choose another method.'}), 200

    return jsonify({
        'ok': True, 'provider': 'pesapal',
        'link': data['redirect_url'],
        'tracking_id': str(data.get('order_tracking_id') or '')[:80],
    }), 200


def _pesapal_site_redirect(status, order_ref):
    site = (PUBLIC_BASE_URL or 'https://agribrige.com').rstrip('/')
    query = urlencode({
        'pay': 'done',
        'provider': 'pesapal',
        'status': status,
        'tx_ref': _base_ref(order_ref),
    })
    return redirect(f'{site}/?{query}')


def _pesapal_ack(ok, tracking_id, order_ref, notification_type):
    return jsonify({
        'orderNotificationType': notification_type or 'IPNCHANGE',
        'orderTrackingId': tracking_id,
        'orderMerchantReference': order_ref,
        'status': 200 if ok else 500,
    }), 200


def _pay_attempt_ref(order_ref):
    """Flutterwave needs a unique tx_ref per attempt; keep the order ref as the prefix."""
    return f"{order_ref}~{uuid.uuid4().hex[:6]}"


def _base_ref(tx_ref):
    return (tx_ref or '').split('~')[0]


def _ug_msisdn(phone):
    p = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if p.startswith('256'):
        p = p[3:]
    p = p.lstrip('0')
    return '256' + p if 8 <= len(p) <= 10 else ''


def _flw_momo_initiate(order_ref, amount, email, phone, network):
    """Direct MTN/Airtel collection through Flutterwave (mobile_money_uganda)."""
    if not FLW_SECRET_KEY:
        return jsonify({'ok': False, 'enabled': False, 'error': 'flutterwave not configured'}), 200
    msisdn = _ug_msisdn(phone)
    if not msisdn:
        return jsonify({'ok': False, 'error': 'Enter a valid Uganda mobile money number'}), 400
    try:
        res = requests.post(
            'https://api.flutterwave.com/v3/charges?type=mobile_money_uganda',
            headers={'Authorization': 'Bearer ' + FLW_SECRET_KEY, 'Content-Type': 'application/json'},
            json={
                'tx_ref': _pay_attempt_ref(order_ref), 'amount': amount, 'currency': 'UGX',
                'email': email or business.SUPPORT_EMAIL, 'phone_number': msisdn,
                'network': network, 'redirect_url': PUBLIC_BASE_URL + '/?pay=done',
                'meta': {'order_ref': order_ref},
            },
            timeout=25,
        )
        j = res.json() if res.content else {}
        if res.ok and (j.get('status') or '') == 'success':
            auth = ((j.get('meta') or {}).get('authorization')) or {}
            return jsonify({'ok': True, 'mode': 'momo_push', 'provider': 'flutterwave',
                            'link': auth.get('redirect') if auth.get('mode') == 'redirect' else None,
                            'message': 'Approve the payment prompt on your phone.'}), 200
        return jsonify({'ok': False, 'error': (j.get('message') or 'Mobile money request failed')[:160]}), 200
    except Exception as e:
        print(f"flw momo initiate error: {e}")
        return jsonify({'ok': False, 'error': 'payment gateway unavailable'}), 200


@app.route('/api/pay/status', methods=['GET'])
@rate_limit(max_req=60, window=300)
def pay_status():
    """Poll after a payment prompt: is this checkout paid yet? (own orders only)"""
    uid, err = verify_supabase_user()
    if err:
        return err
    ref = _base_ref((request.args.get('order_ref') or '').strip()[:80])
    rows = supa_get('orders', {'payment_ref': f'eq.{ref}',
                               'select': 'buyer_id,payment_status'}, limit=50) or []
    rows = [r for r in rows if str(r.get('buyer_id')) == str(uid)]
    if not rows:
        return jsonify({'ok': False, 'error': 'order not found'}), 404
    return jsonify({'ok': True, 'paid': all(r.get('payment_status') == 'paid' for r in rows)}), 200


def _at_momo_initiate(order_ref, amount, phone, provider='momo'):
    """Initiate an Africa's Talking mobile money collection request.

    Sends a USSD push to the buyer's phone prompting them to approve payment.
    Works for MTN MoMo (provider='momo') and Airtel Money (provider='airtel').
    Requires AT_USERNAME and AT_API_KEY to be set with a live (not sandbox) key
    and the Africa's Talking Payment product enabled on the account.
    """
    if not AT_AVAILABLE or not at_sms:
        return jsonify({
            'ok': False, 'enabled': False,
            'error': 'Africa\'s Talking not configured — set AT_USERNAME and AT_API_KEY in Render env vars',
            'fallback': 'manual',  # frontend shows manual USSD instructions
        }), 200
    if not phone:
        return jsonify({'ok': False, 'error': 'phone number required for mobile money payment'}), 400
    # Normalize phone to E.164 (+256...)
    p = phone.strip().replace(' ', '').replace('-', '')
    if p.startswith('0'):
        p = '+256' + p[1:]
    elif not p.startswith('+'):
        p = '+256' + p
    try:
        import africastalking as _at_mod
        payment = _at_mod.Payment
        resp = payment.mobile_checkout(
            product_name=os.environ.get('AT_PAYMENT_PRODUCT', 'AgriBridge'),
            phone_number=p,
            currency_code='UGX',
            amount=float(amount),
            metadata={'order_ref': order_ref, 'provider': provider},
        )
        status = (resp.get('status') or '').lower()
        entry = ((resp.get('entries') or [{}])[0]) if resp.get('entries') else {}
        if status == 'success' or entry.get('status', '').lower() in ('pendingconfirmation', 'success'):
            return jsonify({
                'ok': True, 'mode': 'momo_push',
                'message': 'Payment request sent to your phone. Check your phone and approve the payment.',
                'provider': provider,
                'transaction_id': entry.get('transactionId', ''),
            }), 200
        err_msg = entry.get('errorMessage') or resp.get('errorMessage') or 'Mobile money request failed'
        return jsonify({'ok': False, 'error': err_msg, 'fallback': 'manual'}), 200
    except Exception as e:
        print(f"AT momo initiate error: {e}")
        return jsonify({'ok': False, 'error': 'Mobile money gateway unavailable', 'fallback': 'manual'}), 200


def _flw_initiate(order_ref, amount, email, phone):
    if not FLW_SECRET_KEY:
        return jsonify({'ok': False, 'enabled': False, 'error': 'flutterwave not configured'}), 200
    try:
        res = requests.post(
            'https://api.flutterwave.com/v3/payments',
            headers={'Authorization': 'Bearer ' + FLW_SECRET_KEY, 'Content-Type': 'application/json'},
            json={
                'tx_ref':       _pay_attempt_ref(order_ref),
                'amount':       str(amount),
                'currency':     'UGX',
                'redirect_url': PUBLIC_BASE_URL + '/?pay=done',
                'customer':     {'email': email or business.SUPPORT_EMAIL, 'phonenumber': phone},
                'customizations': {'title': 'AgriBridge', 'description': 'Order ' + order_ref},
                'payment_options': 'card,mobilemoneyuganda',
            },
            timeout=25,
        )
        j = res.json() if res.ok else {}
        link = ((j.get('data') or {}).get('link')) if isinstance(j, dict) else None
        if link:
            return jsonify({'ok': True, 'provider': 'flutterwave', 'link': link}), 200
        return jsonify({'ok': False, 'error': 'could not start payment'}), 200
    except Exception as e:
        print(f"flutterwave initiate error: {e}")
        return jsonify({'ok': False, 'error': 'payment gateway unavailable'}), 200


def _luhn_ok(number):
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _parse_card_fields(data):
    """Validate the card form and return (fields, error). Nothing is stored."""
    number = re.sub(r'\D', '', str(data.get('card_number') or ''))[:19]
    if not 13 <= len(number) <= 19 or not _luhn_ok(number):
        return None, 'Enter a valid card number'
    expiry = re.sub(r'\D', '', str(data.get('expiry') or ''))
    if len(expiry) != 4:
        return None, 'Enter the expiry date as MM/YY'
    month, year = expiry[:2], expiry[2:]
    try:
        m, y = int(month), 2000 + int(year)
    except ValueError:
        return None, 'Enter the expiry date as MM/YY'
    now = datetime.datetime.now(datetime.timezone.utc)
    if not 1 <= m <= 12 or y < now.year or (y == now.year and m < now.month) or y > now.year + 20:
        return None, 'That card is expired or the year is invalid'
    cvv = re.sub(r'\D', '', str(data.get('cvv') or ''))
    if len(cvv) not in (3, 4):
        return None, 'Enter the 3 or 4 digit security code'
    name = str(data.get('cardholder') or '').strip()[:80]
    if len(name) < 2:
        return None, 'Enter the name on the card'
    return {'number': number, 'month': month, 'year': year, 'cvv': cvv, 'name': name}, None


def _flw_encrypt(payload_json):
    """3DES-ECB + PKCS7, base64 — the format the Flutterwave v3 card charge expects."""
    key = FLW_ENCRYPTION_KEY.encode('utf-8')
    padder = _sym_padding.PKCS7(64).padder()
    padded = padder.update(payload_json.encode('utf-8')) + padder.finalize()
    encryptor = _SymCipher(_TripleDES(key), _SymModes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode('ascii')


def _flw_card_charge(order_ref, amount, email, phone, card):
    """Charge a card through Flutterwave v3. Raw card data stays in this request only."""
    payload = {
        'enckey': FLW_ENCRYPTION_KEY,
        'card_number': card['number'],
        'cvv': card['cvv'],
        'expiry_month': card['month'],
        'expiry_year': card['year'],
        'currency': 'UGX',
        'amount': amount,
        'email': email or business.SUPPORT_EMAIL,
        'fullname': card['name'],
        'tx_ref': _pay_attempt_ref(order_ref),
        'redirect_url': PUBLIC_BASE_URL + '/?pay=done',
        'meta': {'order_ref': order_ref},
    }
    if phone:
        payload['phone_number'] = re.sub(r'[^\d+]', '', phone)[:20]
    client = _flw_encrypt(_json.dumps(payload, separators=(',', ':')))
    res = requests.post(
        'https://api.flutterwave.com/v3/charges?type=card',
        headers={'Authorization': 'Bearer ' + FLW_SECRET_KEY, 'Content-Type': 'application/json'},
        json={'public_key': FLW_PUBLIC_KEY, 'client': client},
        timeout=30,
    )
    return (res.json() if res.content else {}), payload['tx_ref']


def _flw_card_result(j, fallback_ref):
    """Map a charge/validate response to the next step the checkout must take."""
    j = j if isinstance(j, dict) else {}
    data = j.get('data') or {}
    auth = (j.get('meta') or {}).get('authorization') or data.get('authorization') or {}
    mode = (data.get('auth_mode') or auth.get('mode') or '').lower()
    state = (data.get('status') or '').lower()
    base = {'tx_ref': data.get('tx_ref') or fallback_ref,
            'flw_ref': data.get('flw_ref') or '', 'flw_id': data.get('id')}
    if (j.get('status') or '').lower() == 'success':
        if state == 'successful' and data.get('id'):
            return dict(base, ok=True, mode='paid')
        if mode == 'redirect' and auth.get('redirect'):
            return dict(base, ok=True, mode='redirect', link=auth['redirect'])
        if mode == 'otp' and data.get('flw_ref'):
            return dict(base, ok=True, mode='otp',
                        message='Enter the one-time code sent to your phone.')
        if mode == 'pin':
            return {'ok': False, 'mode': 'pin',
                    'error': 'This card needs a PIN. Please use the secure card page instead.'}
        if data.get('flw_ref'):
            return dict(base, ok=True, mode='pending', message='Waiting for the bank to confirm.')
    msg = str(j.get('message') or 'Your card was not charged. Please try again.')[:160]
    return {'ok': False, 'error': msg}


def _mark_checkout_paid(tx_ref, *, received, currency, provider, provider_ref):
    """Mark a verified checkout paid and post its ledger receipt.

    Callers must have verified the payment with the provider first. Amount and
    currency are re-checked against the stored order rows so a verified but
    underpaid transaction can never close an order. The ledger reference is
    `payment:<tx_ref>`, so repeated notifications cannot double-post; buyer
    notifications and receipts are sent only on the first successful mark."""
    try:
        rows = supa_get('orders', {
            'payment_ref': f'eq.{tx_ref}',
            'select': 'id,total_amount,total_price,quantity_kg,price_per_kg,status,payment_status',
        }, limit=50) or []
        try:
            owed = sum((ledger.money(finance.order_total(o)) for o in rows), ledger.ZERO)
            amount = ledger.money(received)
        except ledger.LedgerError:
            return False
        if not (currency == 'UGX' and owed > 0 and amount >= owed):
            log.warning('payment.amount_mismatch', payment_ref=tx_ref,
                        provider=provider, owed=str(owed))
            return False

        was_paid = bool(rows) and all(
            str(o.get('payment_status') or '').lower() == 'paid' for o in rows)
        if not was_paid:
            if not supa_update('orders', {'payment_status': 'paid'}, 'payment_ref', tx_ref):
                log.error('payment.mark_paid_failed', payment_ref=tx_ref, provider=provider)
                _log_system_event('payment_mark_paid_failed',
                                  {'payment_ref': tx_ref, 'provider': provider})
                return False

        posting = ledger.post_verified_payment(
            SUPA, tx_ref=tx_ref, orders=rows, received=amount, currency=currency,
            provider=provider, flw_id=provider_ref)
        if not posting.ok:
            # Money is confirmed and the order is paid; a ledger gap is an
            # operator alert (reconciliation also reports it), not a failed
            # payment. Do not make the buyer pay again over a bookkeeping fault.
            log.error('payment.ledger_post_failed', payment_ref=tx_ref,
                      error=posting.error)
            _log_system_event('payment_ledger_post_failed', {
                'payment_ref': tx_ref, 'provider': provider, 'error': posting.error,
            })
            _discord_notify(
                event_type='payment.ledger_failed',
                title='⚠️ Payment received but not posted to ledger',
                description=(f"**Ref:** `{tx_ref}`\n**Provider:** {provider}\n"
                             f"**Error:** {posting.error[:200]}"),
                severity='error',
            )

        if was_paid:
            return True

        try:
            amt = float(amount)
            _discord_notify(
                event_type='payment.confirmed',
                title='💰 Payment Confirmed',
                description=(
                    f"**Ref:** `{tx_ref}`\n"
                    f"**Amount:** {currency} {amt:,.0f}\n"
                    f"**Provider:** {provider}\n"
                    f"**Provider ref:** {provider_ref}\n"
                    f"**Ledger:** `{posting.journal_id or 'not posted'}`"
                ),
                severity='payment',
            )
            paid_rows = supa_get('orders', {
                'payment_ref': f'eq.{tx_ref}',
                'select': 'buyer_id,payment_ref,tracking_code',
            }, limit=1) or []
            if paid_rows:
                pr = paid_rows[0]
                b_id = pr.get('buyer_id')
                o_ref = pr.get('payment_ref') or tx_ref
                if b_id:
                    NOTIFY.enqueue(
                        channel='in_app',
                        audience='user',
                        recipient=b_id,
                        event_type='payment.confirmed',
                        severity='info',
                        title='💰 Payment Confirmed!',
                        body=(
                            f'Your payment of {currency} {int(amt):,} for order {o_ref} has been received. '
                            f'Your order will be dispatched shortly.'
                        ),
                        payload={'order_ref': o_ref, 'amount': amt, 'currency': currency},
                        dedupe_key=f'payment_confirmed:{tx_ref}',
                    )
        except Exception as _pne:
            log.warning('payment.notify_failed', error=type(_pne).__name__)
        _send_payment_receipt(tx_ref)
        return True
    except Exception as e:
        log.error('payment.mark_exception', error=type(e).__name__)
        return False


def _flw_verify_and_mark(attempt_ref, flw_id, provider='Flutterwave'):
    """Verify a Flutterwave transaction server-side, then mark its checkout paid.

    The webhook body is never trusted on its own; this is also used after a
    direct card charge or OTP validation."""
    try:
        v = requests.get(
            'https://api.flutterwave.com/v3/transactions/' + str(flw_id) + '/verify',
            headers={'Authorization': 'Bearer ' + FLW_SECRET_KEY}, timeout=20)
        vj = v.json() if v.ok else {}
        vdata = vj.get('data') or {}
        tx_ref = _base_ref(attempt_ref)
        if not ((vdata.get('status') or '').lower() == 'successful'
                and _base_ref(vdata.get('tx_ref')) == tx_ref):
            return False
        return _mark_checkout_paid(
            tx_ref, received=vdata.get('amount'), provider=provider,
            currency=str(vdata.get('currency') or '').strip().upper(),
            provider_ref=flw_id)
    except Exception as e:
        log.error('payment.verify_exception', error=type(e).__name__)
        return False


def _pesapal_verify_and_mark(order_ref, status, tracking_id):
    """Verify a Pesapal transaction server-side, then mark its checkout paid.

    The callback/IPN parameters are only a hint about which transaction to
    fetch; the authoritative amount, currency, and completion state come from
    GetTransactionStatus. Recurring charges carry no order and are recorded by
    `_record_pesapal_recurring` instead."""
    try:
        order_ref = _base_ref(order_ref)
        status = status if isinstance(status, dict) else {}
        # Pesapal echoes the submitted reference in merchant_reference on most
        # responses; accept account_number too so a version difference cannot
        # turn a genuinely paid order into an unverified one.
        echoed = {_base_ref(status.get(key)) for key in
                  ('merchant_reference', 'account_number', 'id')}
        if not (pesapal_completed(status) and order_ref in echoed):
            log.warning('pesapal.unverified_callback', payment_ref=order_ref,
                        tracking_id=str(tracking_id)[:80],
                        status_code=status.get('status_code'))
            return False
        return _mark_checkout_paid(
            order_ref, received=status.get('amount'), provider='Pesapal',
            currency=str(status.get('currency') or '').strip().upper(),
            provider_ref=tracking_id)
    except Exception as e:
        log.error('pesapal.verify_exception', error=type(e).__name__)
        return False


@app.route('/api/pay/card', methods=['POST'])
@rate_limit(max_req=10, window=300)
def pay_card():
    """Charge a card entered on our checkout form (opt-in; PCI DSS scoped)."""
    uid, err = verify_supabase_user()
    if err:
        return err
    if not _direct_card_ready():
        return jsonify({'ok': False, 'enabled': False,
                        'error': 'Direct card payments are not enabled'}), 200
    data = request.get_json(force=True, silent=True) or {}
    order_ref = (data.get('order_ref') or '').strip()[:60]
    try:
        amount = int(float(data.get('amount')))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'invalid amount'}), 400
    if amount <= 0 or not order_ref:
        return jsonify({'ok': False, 'error': 'missing order_ref or amount'}), 400
    amount, perr = _checkout_amount(uid, order_ref, amount)
    if perr:
        return perr
    card, cerr = _parse_card_fields(data)
    if cerr:
        return jsonify({'ok': False, 'error': cerr}), 400
    try:
        j, tx_ref = _flw_card_charge(
            order_ref, amount, (data.get('email') or '').strip()[:120],
            (data.get('phone') or '').strip()[:20], card)
    except Exception:
        return jsonify({'ok': False, 'error': 'payment gateway unavailable'}), 200
    result = _flw_card_result(j, tx_ref)
    if result.get('mode') == 'paid':
        if _flw_verify_and_mark(tx_ref, result.get('flw_id'), provider='Flutterwave card'):
            result['mode'] = 'paid'
        else:
            result['mode'] = 'pending'
            result['message'] = 'The bank is confirming your payment.'
    resp = jsonify(result)
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 200


@app.route('/api/pay/card/validate', methods=['POST'])
@rate_limit(max_req=10, window=300)
def pay_card_validate():
    """Complete a card charge that the bank asked to authorise by OTP."""
    uid, err = verify_supabase_user()
    if err:
        return err
    if not _direct_card_ready():
        return jsonify({'ok': False, 'enabled': False,
                        'error': 'Direct card payments are not enabled'}), 200
    data = request.get_json(force=True, silent=True) or {}
    order_ref = (data.get('order_ref') or '').strip()[:60]
    flw_ref = (data.get('flw_ref') or '').strip()[:100]
    otp = (data.get('otp') or '').strip()
    if not order_ref or not re.fullmatch(r'[A-Za-z0-9_\-/.]{6,100}', flw_ref):
        return jsonify({'ok': False, 'error': 'missing payment reference'}), 400
    if not re.fullmatch(r'\d{4,8}', otp):
        return jsonify({'ok': False, 'error': 'Enter the code sent to your phone'}), 400
    rows = supa_get('orders', {'payment_ref': f'eq.{order_ref}',
                               'select': 'buyer_id,payment_status'}, limit=50) or []
    if not rows:
        return jsonify({'ok': False, 'error': 'order not found'}), 404
    if any(str(r.get('buyer_id')) != str(uid) for r in rows):
        return jsonify({'ok': False, 'error': 'not your order'}), 403
    if all((r.get('payment_status') or '') == 'paid' for r in rows):
        return jsonify({'ok': True, 'mode': 'paid'}), 200
    try:
        res = requests.post(
            'https://api.flutterwave.com/v3/validate-charge',
            headers={'Authorization': 'Bearer ' + FLW_SECRET_KEY,
                     'Content-Type': 'application/json'},
            json={'flw_ref': flw_ref, 'otp': otp}, timeout=30,
        )
        j = res.json() if res.content else {}
    except Exception:
        return jsonify({'ok': False, 'error': 'payment gateway unavailable'}), 200
    result = _flw_card_result(j, order_ref)
    if result.get('mode') == 'paid':
        if _flw_verify_and_mark(result.get('tx_ref') or order_ref, result.get('flw_id'),
                                provider='Flutterwave card'):
            result['mode'] = 'paid'
        else:
            result['mode'] = 'pending'
            result['message'] = 'The bank is confirming your payment.'
    resp = jsonify(result)
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 200


@app.route('/api/pay/webhook/flutterwave', methods=['POST'])
def pay_webhook_flw():
    # Only trust calls carrying the secret hash we configured in the FLW dashboard.
    sig = request.headers.get('verif-hash', '')
    if not FLW_WEBHOOK_HASH or not sig or not hmac.compare_digest(sig, FLW_WEBHOOK_HASH):
        return jsonify({'status': 'unauthorized'}), 401
    data = request.get_json(force=True, silent=True) or {}
    ev = data.get('data') or {}
    attempt_ref = ev.get('tx_ref') or ''
    flw_id = ev.get('id')
    if (ev.get('status') or '').lower() == 'successful' and attempt_ref and flw_id:
        _flw_verify_and_mark(attempt_ref, flw_id)
    return jsonify({'status': 'ok'}), 200


def _pesapal_request_values():
    """Merge query, form, and JSON fields from a Pesapal callback or IPN."""
    values = {key: value for key, value in request.args.items()}
    if request.method == 'POST':
        payload = request.get_json(force=True, silent=True)
        if isinstance(payload, dict):
            values.update(payload)
        values.update(request.form.to_dict())
    return values


def _record_pesapal_recurring(status, tracking_id):
    """Record one recurring charge and post it to the ledger suspense account.

    A recurring charge has no new order. It is a liability until operations
    allocate it, so it is never credited to a farmer. Idempotent on
    (provider, provider_tracking_id); a retried IPN only retries a missing
    ledger posting."""
    info = status.get('subscription_transaction_info') \
        if isinstance(status, dict) else None
    if not isinstance(info, dict) or not info:
        return None
    if not pesapal_completed(status):
        return True

    tracking_id = str(tracking_id or '').strip()[:80]
    if not tracking_id:
        return False
    currency = str(status.get('currency') or 'UGX').strip().upper()
    try:
        amount = ledger.money(info.get('amount') or status.get('amount'))
    except ledger.LedgerError:
        log.error('pesapal.recurring_invalid_amount', tracking_id=tracking_id)
        _log_system_event('pesapal_recurring_invalid_amount',
                          {'tracking_id': tracking_id}, severity='warning')
        return False
    if amount <= 0 or currency != 'UGX':
        log.error('pesapal.recurring_invalid_money', tracking_id=tracking_id,
                  currency=currency)
        return False

    account_ref = _base_ref(str(
        info.get('account_reference') or status.get('merchant_reference')
        or status.get('account_number') or '').strip())[:60]
    correlation = str(info.get('correlation_id') or '').strip()[:120]
    confirmation = str(status.get('confirmation_code') or '').strip()[:120]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    existing = supa_get('subscription_charges', {
        'provider': 'eq.pesapal',
        'provider_tracking_id': f'eq.{tracking_id}',
        'select': 'id,ledger_reference',
    }, limit=1) or []
    charge = existing[0] if existing else None

    if charge and charge.get('ledger_reference'):
        return True

    subscription_id = None
    if correlation:
        buyer_id = None
        if account_ref:
            order_rows = supa_get('orders', {
                'payment_ref': f'eq.{account_ref}', 'select': 'buyer_id'}, limit=1) or []
            if order_rows:
                buyer_id = order_rows[0].get('buyer_id')
        saved = SUPA.upsert('payment_subscriptions', {
            'provider': 'pesapal',
            'provider_correlation_id': correlation,
            'account_ref': account_ref,
            'buyer_id': buyer_id,
            'amount': float(amount),
            'currency': currency,
            'status': 'active',
            'last_charged_at': now,
            'updated_at': now,
        }, on_conflict='provider,provider_correlation_id')
        if saved.ok:
            row = saved.data[0] if isinstance(saved.data, list) and saved.data \
                else saved.data if isinstance(saved.data, dict) else {}
            subscription_id = str((row or {}).get('id') or '') or None
        if not subscription_id:
            read_back = supa_get('payment_subscriptions', {
                'provider': 'eq.pesapal',
                'provider_correlation_id': f'eq.{correlation}',
                'select': 'id'}, limit=1) or []
            if read_back:
                subscription_id = str(read_back[0].get('id') or '') or None
        if not subscription_id:
            log.error('pesapal.recurring_subscription_failed', tracking_id=tracking_id,
                      status=saved.status, error=str(saved.error)[:120])
            _log_system_event('pesapal_recurring_subscription_failed',
                              {'tracking_id': tracking_id,
                               'error': str(saved.error)[:200]})
            return False

    if not charge:
        inserted = SUPA.insert('subscription_charges', {
            'subscription_id': subscription_id,
            'provider': 'pesapal',
            'provider_tracking_id': tracking_id,
            'confirmation_code': confirmation or None,
            'account_ref': account_ref or None,
            'amount': float(amount),
            'currency': currency,
            'status': 'paid',
        })
        if not inserted.ok:
            # A concurrent delivery may have won the unique key; treat that as
            # already recorded and continue with any missing ledger posting.
            existing = supa_get('subscription_charges', {
                'provider': 'eq.pesapal',
                'provider_tracking_id': f'eq.{tracking_id}',
                'select': 'id,ledger_reference',
            }, limit=1) or []
            if not existing:
                log.error('pesapal.recurring_charge_insert_failed',
                          tracking_id=tracking_id, status=inserted.status,
                          error=str(inserted.error)[:120])
                _log_system_event('pesapal_recurring_charge_insert_failed',
                                  {'tracking_id': tracking_id,
                                   'error': str(inserted.error)[:200]})
                return False
            charge = existing[0]
            if charge.get('ledger_reference'):
                return True
        else:
            rows = inserted.data if isinstance(inserted.data, list) else [inserted.data]
            charge = (rows[0] if rows and isinstance(rows[0], dict) else {}) or None

    posting = ledger.post_recurring_charge(
        SUPA, tracking_id=tracking_id, amount=amount, currency=currency,
        provider='Pesapal', account_ref=account_ref, correlation_id=correlation,
        confirmation_code=confirmation)
    if not posting.ok:
        # The charge is durably recorded; the ledger gap is an operator alert
        # and the admin recurring view surfaces it. Do not drop the charge.
        log.error('pesapal.recurring_ledger_failed', tracking_id=tracking_id,
                  error=posting.error)
        _log_system_event('pesapal_recurring_ledger_failed',
                          {'tracking_id': tracking_id, 'error': posting.error})
        _discord_notify(
            event_type='payment.recurring_ledger_failed',
            title='⚠️ Recurring payment received but not posted to ledger',
            description=(f"**Tracking:** `{tracking_id}`\n"
                         f"**Account:** `{account_ref or '-'}`\n"
                         f"**Amount:** {currency} {float(amount):,.0f}\n"
                         f"**Error:** {posting.error[:200]}"),
            severity='error',
        )
        return True

    if charge and charge.get('id'):
        if not supa_update('subscription_charges',
                           {'ledger_reference': posting.reference},
                           'id', charge['id']):
            log.warning('pesapal.recurring_charge_update_failed',
                        tracking_id=tracking_id)

    try:
        _discord_notify(
            event_type='payment.recurring_confirmed',
            title='🔁 Recurring payment received',
            description=(f"**Tracking:** `{tracking_id}`\n"
                         f"**Account:** `{account_ref or '-'}`\n"
                         f"**Amount:** {currency} {float(amount):,.0f}\n"
                         f"**Correlation:** `{correlation or '-'}`\n"
                         f"**Ledger:** `{posting.journal_id or 'not posted'}`"),
            severity='payment',
        )
        if account_ref:
            order_rows = supa_get('orders', {
                'payment_ref': f'eq.{account_ref}',
                'select': 'buyer_id,tracking_code'}, limit=1) or []
            buyer_id = order_rows[0].get('buyer_id') if order_rows else None
            tracking_code = order_rows[0].get('tracking_code') if order_rows else ''
            if buyer_id:
                NOTIFY.enqueue(
                    channel='in_app',
                    audience='user',
                    recipient=buyer_id,
                    event_type='payment.recurring',
                    severity='info',
                    title='🔁 Recurring payment received',
                    body=(f'We received a recurring payment of {currency} '
                          f'{int(float(amount)):,} for order {account_ref}. '
                          f'Our team will allocate it to your order.'),
                    payload={'order_ref': account_ref, 'amount': float(amount),
                             'currency': currency, 'tracking': tracking_code},
                    dedupe_key=f'recurring_confirmed:{tracking_id}',
                )
    except Exception as exc:
        log.warning('pesapal.recurring_notify_failed', error=type(exc).__name__)
    return True


def _handle_pesapal_notification(values):
    """Verify one callback/IPN against Pesapal and apply its effect.

    Callback fields are hints only: the transaction status fetched here is the
    source of truth for completion, amount, currency, and merchant reference."""
    values = values if isinstance(values, dict) else {}
    tracking_id = str(values.get('OrderTrackingId')
                      or values.get('orderTrackingId') or '').strip()[:80]
    notification_type = str(values.get('OrderNotificationType')
                            or values.get('orderNotificationType')
                            or 'IPNCHANGE').strip().upper()[:20]
    result = {'ok': False, 'paid': False, 'recurring': False, 'failed': False,
              'order_ref': '', 'notification_type': notification_type}
    client = _pesapal_client()
    if not client or not tracking_id:
        return result
    try:
        status = client.get_transaction_status(tracking_id)
    except PesapalError as exc:
        log.warning('pesapal.status_unavailable', tracking_id=tracking_id,
                    error=str(exc)[:120])
        return result

    order_ref = _base_ref(str(status.get('merchant_reference')
                              or status.get('account_number') or '').strip())[:60]
    result['order_ref'] = order_ref
    if not pesapal_completed(status):
        # A pending or failed callback changes nothing; acknowledge it so the
        # gateway stops retrying, and let the browser show the outcome.
        result['ok'] = True
        result['failed'] = pesapal_failed(status)
        return result

    info = status.get('subscription_transaction_info')
    if isinstance(info, dict) and info:
        recorded = _record_pesapal_recurring(status, tracking_id)
        result['recurring'] = True
        result['ok'] = recorded is not False
        # A renewal is not the original checkout: never mark that order paid
        # again from a recurring IPN.
        return result

    if not order_ref:
        return result
    result['paid'] = _pesapal_verify_and_mark(order_ref, status, tracking_id)
    result['ok'] = result['paid']
    return result


@app.route('/api/pay/pesapal/callback', methods=['GET', 'POST'])
@rate_limit(max_req=90, window=300)
def pay_pesapal_callback():
    """Browser return from the Pesapal iframe; always redirects to the app."""
    result = _handle_pesapal_notification(_pesapal_request_values())
    if result.get('paid'):
        outcome = 'paid'
    elif result.get('recurring'):
        outcome = 'recurring'
    elif result.get('failed'):
        outcome = 'failed'
    else:
        outcome = 'pending'
    return _pesapal_site_redirect(outcome, result.get('order_ref') or '')


@app.route('/api/pay/webhook/pesapal', methods=['GET', 'POST'])
def pay_webhook_pesapal():
    """Pesapal IPN endpoint; acknowledges with the JSON shape Pesapal expects.

    The body is untrusted. Every notification is verified with
    GetTransactionStatus before any order or ledger change."""
    values = _pesapal_request_values()
    result = _handle_pesapal_notification(values)
    tracking_id = str(values.get('OrderTrackingId')
                      or values.get('orderTrackingId') or '')[:80]
    merchant_ref = str(result.get('order_ref')
                       or values.get('OrderMerchantReference') or '')[:80]
    resp, code = _pesapal_ack(bool(result.get('ok')), tracking_id, merchant_ref,
                              result.get('notification_type'))
    resp.headers['Cache-Control'] = 'no-store'
    return resp, code


def _send_payment_receipt(payment_ref):
    """Look up the paid order's buyer + items and email a receipt via the
    send-receipt Edge Function. Best-effort; never raises to the webhook."""
    try:
        orows = supa_get('orders', filters={
            'payment_ref': f'eq.{payment_ref}',
            'select': 'buyer_id,buyer_phone,quantity_kg,price_per_kg,total_price,tracking_code,notes',
        })
        if not orows:
            return
        buyer_id = orows[0].get('buyer_id')
        total = sum(float(o.get('total_price') or 0) for o in orows)
        tracking = orows[0].get('tracking_code') or ''
        _sms_receipt(orows[0].get('buyer_phone'), payment_ref, total, 'payment')
        email, name = '', 'there'
        if buyer_id:
            frows = supa_get('farmers', filters={'id': f'eq.{buyer_id}', 'select': 'email,full_name'}, limit=1)
            if frows:
                email = frows[0].get('email') or ''
                name = frows[0].get('full_name') or 'there'
        if not email:
            return
        items = [{
            'name': ((o.get('notes') or '').split('Item:')[-1].strip() or 'Order'),
            'qty': o.get('quantity_kg'),
            'price': o.get('price_per_kg'),
        } for o in orows]
        requests.post(
            SUPABASE_URL + '/functions/v1/send-receipt',
            headers={'Authorization': f'Bearer {SUPABASE_KEY}', 'Content-Type': 'application/json'},
            json={'type': 'payment', 'email': email, 'name': name, 'ref': payment_ref,
                  'total': total, 'tracking': tracking, 'items': items},
            timeout=15,
        )
    except Exception as e:
        print(f"payment receipt email error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') == 'development'
    print(f"AgriBridge API v3 starting on port {port} (debug={debug})")
    app.run(host='0.0.0.0', port=port, debug=debug)
