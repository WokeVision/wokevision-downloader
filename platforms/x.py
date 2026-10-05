"""X (formerly Twitter) connect + publish, via OAuth 2.0 Authorization Code
flow with PKCE and the v2 API throughout -- including the newer JSON-based
chunked media upload endpoints (api.x.com/2/media/upload/*), which accept a
plain OAuth 2.0 Bearer token like the rest of v2, so there's no need for the
older OAuth 1.0a-signed upload.twitter.com/1.1 path here.

Flow:
  1. Browser -> AUTHORIZE_URL (user approves) -> redirected back with a
     `code`. PKCE (code_verifier/code_challenge) is required by X's OAuth 2.0
     apps even for confidential (client-secret-bearing) clients; the
     verifier is generated per auth attempt and held in a module-level
     variable until the callback completes -- fine for this single-user app,
     same assumption the rest of main.py's OAuth state tracking makes.
  2. code (+ verifier) -> access + refresh token (api.x.com/2/oauth2/token)
  3. access token refreshed from the stored refresh_token whenever it's
     close to expiring (X access tokens are short-lived, ~2h; refresh
     tokens are long-lived with offline.access and rotate on each use).
  4. Publishing: stream the rendered video from this app's own
     /files/<name>.mp4 URL, chunk it into <=5MB pieces for the v2 chunked
     media upload (initialize -> append -> finalize -> poll status), then
     create the post referencing the resulting media_id.
"""
import base64
import hashlib
import os
import secrets
import time
import requests

import db

CLIENT_ID = os.environ.get("X_CLIENT_ID")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
USER_URL = "https://api.x.com/2/users/me"
MEDIA_UPLOAD_BASE = "https://api.x.com/2/media/upload"
TWEETS_URL = "https://api.x.com/2/tweets"

# tweet.write/read + users.read to post and look up the account; media.write
# to upload video; offline.access to get a refresh token.
SCOPES = "tweet.read tweet.write users.read media.write offline.access"

PLATFORM = "x"

# Chunk size for APPEND -- X asks for <=5MB per segment.
CHUNK_SIZE = 4 * 1024 * 1024

# Holds the PKCE code_verifier for the in-flight auth attempt. A module
# global (not a dict keyed by state) is enough here: this is a single-user
# app and only one connect attempt is ever in flight at a time, matching how
# main.py's own OAUTH_STATES tracking already assumes single-flight use.
_PENDING_VERIFIER = None


class XError(Exception):
    pass


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and PUBLIC_BASE_URL)


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/connections/x/callback"


def _make_pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def get_auth_url(state: str) -> str:
    global _PENDING_VERIFIER
    if not configured():
        raise XError("X isn't configured yet (missing X_CLIENT_ID/SECRET or PUBLIC_BASE_URL).")
    verifier, challenge = _make_pkce_pair()
    _PENDING_VERIFIER = verifier
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"{AUTHORIZE_URL}?{query}"


def _basic_auth_header() -> dict:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}


def _exchange_code_for_tokens(code: str, verifier: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise XError(f"Token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _refresh_access_token(refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise XError(f"Token refresh failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _get_user(access_token: str) -> dict:
    resp = requests.get(
        USER_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise XError(f"User lookup failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json().get("data") or {}
    if not data:
        raise XError("No user data returned from X.")
    return data


def handle_callback(code: str):
    """Completes the OAuth flow from an authorization code: exchanges it
    (with the matching PKCE verifier) for access + refresh tokens, looks up
    the connected account's username, and stores it. Raises XError with a
    human-readable message on any failure -- the caller surfaces that
    directly to the user."""
    global _PENDING_VERIFIER
    verifier = _PENDING_VERIFIER
    _PENDING_VERIFIER = None
    if not verifier:
        raise XError("Lost track of this connection attempt -- please try connecting again.")

    tokens = _exchange_code_for_tokens(code, verifier)
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 7200)
    if not access_token:
        raise XError("No access token returned from X.")
    if not refresh_token:
        raise XError(
            "X didn't return a refresh token -- the app's OAuth 2.0 settings need "
            "offline.access enabled, or this account already authorized the app without it."
        )

    user = _get_user(access_token)

    import datetime
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    db.save_connection(
        PLATFORM, access_token, refresh_token=refresh_token, expires_at=expires_at,
        extra={"user_id": user.get("id"), "username": user.get("username")},
    )
    return user


def refresh_if_needed():
    """X access tokens are short-lived (~2h); refresh a bit before expiry
    using the stored refresh_token. X also rotates the refresh token on each
    use, so the new one must be saved every time. Called opportunistically
    before publishing and status checks so a token never silently goes
    stale."""
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("refresh_token"):
        return
    import datetime
    expires_at = conn.get("expires_at")
    now = datetime.datetime.now(datetime.timezone.utc)
    time_left = (expires_at - now).total_seconds() if expires_at else 0

    if time_left > 300:
        return
    data = _refresh_access_token(conn["refresh_token"])
    new_token = data.get("access_token")
    new_refresh = data.get("refresh_token", conn["refresh_token"])
    if new_token:
        new_expires_at = now + datetime.timedelta(seconds=data.get("expires_in", 7200))
        db.save_connection(
            PLATFORM, new_token, refresh_token=new_refresh, expires_at=new_expires_at,
            extra=conn.get("extra", {}),
        )


def check_status() -> dict:
    """Live probe used by the /connections status endpoint: not just "is a
    token stored" but "does it still actually work right now". Returns
    {"connected": bool, "ok": bool, "label": str, "error": str|None}."""
    if not configured():
        return {"connected": False, "ok": False, "label": "Not set up", "error": None}
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        return {"connected": False, "ok": False, "label": "Not connected", "error": None}
    try:
        refresh_if_needed()
        conn = db.get_connection(PLATFORM)
        user = _get_user(conn["access_token"])
        db.set_check_result(PLATFORM, True)
        username = user.get("username") or conn.get("extra", {}).get("username")
        return {"connected": True, "ok": True, "label": f"@{username}" if username else "Connected", "error": None}
    except Exception as e:
        db.set_check_result(PLATFORM, False, str(e))
        return {"connected": True, "ok": False, "label": "Connection error", "error": str(e)}


def _upload_video(access_token: str, video_url: str) -> str:
    """Runs the full chunked-upload cycle (initialize -> append -> finalize
    -> poll status) and returns the resulting media_id. Streams the source
    video rather than buffering it whole, and chunks it into <=5MB pieces as
    it reads, so memory use stays flat regardless of video length."""
    source_resp = requests.get(video_url, stream=True, timeout=60)
    if source_resp.status_code != 200:
        raise XError(f"Could not fetch the rendered video to upload: {source_resp.status_code}")
    content_length = source_resp.headers.get("Content-Length")
    if not content_length:
        source_resp.close()
        raise XError("Rendered video has no known size -- can't start a chunked upload.")

    auth_header = {"Authorization": f"Bearer {access_token}"}

    init_resp = requests.post(
        f"{MEDIA_UPLOAD_BASE}/initialize",
        headers={**auth_header, "Content-Type": "application/json"},
        json={
            "media_type": "video/mp4",
            "total_bytes": int(content_length),
            "media_category": "tweet_video",
        },
        timeout=30,
    )
    if init_resp.status_code not in (200, 201):
        source_resp.close()
        raise XError(f"Could not start the upload: {init_resp.status_code} {init_resp.text[:500]}")
    media_id = (init_resp.json().get("data") or init_resp.json()).get("id")
    if not media_id:
        source_resp.close()
        raise XError("X didn't return a media id to upload into.")

    try:
        segment_index = 0
        for chunk in source_resp.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            append_resp = requests.post(
                f"{MEDIA_UPLOAD_BASE}/{media_id}/append",
                headers=auth_header,
                data={"segment_index": str(segment_index)},
                files={"media": ("chunk.mp4", chunk, "application/octet-stream")},
                timeout=60,
            )
            if append_resp.status_code not in (200, 201, 204):
                raise XError(f"Upload chunk {segment_index} failed: {append_resp.status_code} {append_resp.text[:500]}")
            segment_index += 1
    finally:
        source_resp.close()

    finalize_resp = requests.post(
        f"{MEDIA_UPLOAD_BASE}/{media_id}/finalize",
        headers=auth_header,
        timeout=30,
    )
    if finalize_resp.status_code not in (200, 201):
        raise XError(f"Could not finalize the upload: {finalize_resp.status_code} {finalize_resp.text[:500]}")

    processing_info = (finalize_resp.json().get("data") or finalize_resp.json()).get("processing_info")
    deadline = time.time() + 5 * 60
    while processing_info and time.time() < deadline:
        state = processing_info.get("state")
        if state == "succeeded":
            break
        if state == "failed":
            raise XError(f"X failed to process the video: {processing_info.get('error')}")
        time.sleep(min(processing_info.get("check_after_secs", 5), 15))
        status_resp = requests.get(
            MEDIA_UPLOAD_BASE,
            params={"command": "STATUS", "media_id": media_id},
            headers=auth_header,
            timeout=20,
        )
        processing_info = (status_resp.json().get("data") or status_resp.json()).get("processing_info")
    else:
        if processing_info and processing_info.get("state") != "succeeded":
            raise XError("Timed out waiting for X to process the video.")

    return media_id


def publish_video(video_url: str, caption: str, post: dict = None) -> dict:
    """Uploads the rendered video and posts it. video_url must be a public
    URL (this app's own /files/<name>.mp4 route). Returns {"tweet_id": ...}.
    Raises XError on any failure, with the underlying platform message
    included so the UI can show something actionable."""
    caption = (post or {}).get("text", caption)
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        raise XError("X isn't connected.")
    refresh_if_needed()
    conn = db.get_connection(PLATFORM)
    access_token = conn["access_token"]

    media_id = _upload_video(access_token, video_url)

    # X posts top out at 280 characters; trim long captions rather than
    # letting the post get rejected outright.
    text = (caption or "").strip()
    if len(text) > 280:
        text = text[:277].rstrip() + "..."

    post_resp = requests.post(
        TWEETS_URL,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"text": text, "media": {"media_ids": [media_id]}},
        timeout=30,
    )
    if post_resp.status_code not in (200, 201):
        raise XError(f"Could not post: {post_resp.status_code} {post_resp.text[:500]}")
    tweet_id = (post_resp.json().get("data") or {}).get("id")
    return {"tweet_id": tweet_id}
