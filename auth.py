"""Passkey (WebAuthn) authentication for the whole app -- replaces a
username/password with Face ID / Touch ID / Windows Hello, backed by a
long-lived signed session cookie so the prompt only reappears occasionally
rather than on every visit.

Single-user app, so this is deliberately simple:
  - Any number of passkeys can be registered (one per device -- phone,
    laptop, etc.), all treated as equally "the owner".
  - Registering a *new* passkey requires SETUP_CODE, a secret only the
    owner knows, so a random visitor can't enroll their own face before the
    real owner does. Signing in with an *already-registered* passkey never
    needs the code -- that's the whole point of a passkey.
  - Challenges for in-flight registration/login ceremonies are kept in a
    short-lived in-memory dict (this app runs as a single process/worker,
    same assumption the existing JOBS/PUBLISH_JOBS dicts already make).
  - Sessions are a signed, stdlib-only cookie (HMAC-SHA256 over an
    expiry timestamp) -- no extra dependency, no server-side session store
    to clean up.
"""
import os
import time
import json
import base64
import hmac
import hashlib
import secrets

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.helpers import base64url_to_bytes

import db

SETUP_CODE = os.environ.get("SETUP_CODE")
SESSION_SECRET = os.environ.get("SESSION_SECRET")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# The domain WebAuthn credentials are bound to. Must exactly match the host
# the page is actually served from -- a passkey registered under one RP ID
# will NOT work under another, so moving to a custom domain later means
# registering passkeys again once (set WEBAUTHN_RP_ID explicitly if the
# auto-derived value from PUBLIC_BASE_URL is ever wrong).
def _default_rp_id() -> str:
    host = PUBLIC_BASE_URL.replace("https://", "").replace("http://", "")
    return host.split("/")[0].split(":")[0]

RP_ID = os.environ.get("WEBAUTHN_RP_ID") or _default_rp_id()
RP_NAME = "WokeVision Editor"
ORIGIN = PUBLIC_BASE_URL

SESSION_COOKIE = "wv_session"
SESSION_TTL_SECONDS = 180 * 24 * 60 * 60  # 180 days

# In-memory pending-ceremony challenges: token -> (challenge_bytes, expires_at).
_PENDING = {}
_PENDING_TTL = 300


def configured() -> bool:
    return bool(SETUP_CODE and SESSION_SECRET and RP_ID and ORIGIN and db.configured())


class AuthError(Exception):
    pass


def _store_challenge(challenge: bytes) -> str:
    token = secrets.token_urlsafe(24)
    _PENDING[token] = (challenge, time.time() + _PENDING_TTL)
    # Opportunistic cleanup so this dict doesn't grow unbounded over a long
    # uptime -- cheap, and only ever runs on the (rare) auth-ceremony path.
    now = time.time()
    for k in [k for k, (_, exp) in _PENDING.items() if exp < now]:
        _PENDING.pop(k, None)
    return token


def _pop_challenge(token: str) -> bytes:
    entry = _PENDING.pop(token, None)
    if not entry:
        raise AuthError("That setup/login attempt expired -- please try again.")
    challenge, expires_at = entry
    if time.time() > expires_at:
        raise AuthError("That setup/login attempt expired -- please try again.")
    return challenge


# --- Registration (adding a new passkey) ------------------------------------

def start_registration(setup_code: str) -> dict:
    if not configured():
        raise AuthError("Passkey login isn't configured yet (missing SETUP_CODE/SESSION_SECRET).")
    if not setup_code or not hmac.compare_digest(setup_code, SETUP_CODE):
        raise AuthError("Incorrect setup code.")

    existing = db.list_passkeys()
    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=secrets.token_bytes(16),
        user_name="wokevision-owner",
        user_display_name="WokeVision",
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=p["credential_id"]) for p in existing
        ],
    )
    token = _store_challenge(options.challenge)
    return {"token": token, "options": json.loads(options_to_json(options))}


def finish_registration(token: str, setup_code: str, credential: dict, label: str = None):
    if not setup_code or not hmac.compare_digest(setup_code, SETUP_CODE):
        raise AuthError("Incorrect setup code.")
    challenge = _pop_challenge(token)
    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            require_user_verification=True,
        )
    except Exception as e:
        raise AuthError(f"Couldn't verify that passkey: {e}")

    db.add_passkey(
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        label=label,
    )


# --- Login (using an existing passkey) --------------------------------------

def start_login() -> dict:
    if not configured():
        raise AuthError("Passkey login isn't configured yet (missing SETUP_CODE/SESSION_SECRET).")
    existing = db.list_passkeys()
    if not existing:
        raise AuthError("No passkey has been set up yet -- use the setup code first.")
    options = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=[PublicKeyCredentialDescriptor(id=p["credential_id"]) for p in existing],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    token = _store_challenge(options.challenge)
    return {"token": token, "options": json.loads(options_to_json(options))}


def finish_login(token: str, credential: dict) -> str:
    """Verifies the signed challenge and returns a fresh session cookie
    value on success."""
    challenge = _pop_challenge(token)
    raw_id = credential.get("rawId") or credential.get("id")
    if not raw_id:
        raise AuthError("Malformed passkey response.")
    credential_id = base64url_to_bytes(raw_id)
    stored = db.get_passkey(credential_id)
    if not stored:
        raise AuthError("That passkey isn't registered here.")

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            credential_public_key=stored["public_key"],
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=True,
        )
    except Exception as e:
        raise AuthError(f"Couldn't verify that passkey: {e}")

    db.update_passkey_sign_count(credential_id, verification.new_sign_count)
    return make_session_token()


# --- Session cookie (stdlib HMAC, no extra dependency) ----------------------

def _sign(payload: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")


def make_session_token() -> str:
    payload = json.dumps({"exp": time.time() + SESSION_TTL_SECONDS})
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{payload_b64}.{_sign(payload)}"


def verify_session_token(token: str) -> bool:
    if not configured() or not token or "." not in token:
        return False
    payload_b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "==").decode()
    except Exception:
        return False
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        data = json.loads(payload)
        return float(data.get("exp", 0)) > time.time()
    except Exception:
        return False


# --- Signed, expiring /files links (opt-in via SIGNED_FILES=1) ---------------

def signed_files_enabled() -> bool:
    return os.environ.get("SIGNED_FILES", "").strip().lower() in ("1", "true", "yes", "on") and configured()


def _file_sig(name: str, exp: int) -> str:
    return _sign(f"file:{name}:{exp}")


def sign_file_url(url: str, ttl: int = 6 * 3600) -> str:
    """Append ?exp&sig to a /files/<name> URL when signing is switched on;
    otherwise return it unchanged. Used for URLs handed to outside fetchers
    (platform publishing, scheduled posts, client share pages)."""
    if not url or not signed_files_enabled() or "/files/" not in url or "sig=" in url:
        return url
    name = url.split("/files/", 1)[1].split("?", 1)[0]
    exp = int(time.time()) + ttl
    return f"{url.split('?', 1)[0]}?exp={exp}&sig={_file_sig(name, exp)}"


def valid_file_sig(name: str, exp, sig) -> bool:
    try:
        exp = int(exp)
    except Exception:
        return False
    if not sig or exp < time.time():
        return False
    return hmac.compare_digest(str(sig), _file_sig(name, exp))
