"""Facebook Page connect + publish, via standard Facebook Login (NOT the
page-free "Business Login for Instagram" pattern used by instagram.py).
Posting to a Facebook Page requires a Page Access Token, which in turn
requires logging in as a user who administers that Page and asking for
Page permissions -- there's no page-free equivalent for Facebook itself.

Note: this is a genuinely separate OAuth app/credential from Instagram and
Threads. Even though all three can live under the same Meta developer app,
each "use case" (API setup with Instagram login / Threads API / Facebook
Login for Business) has its own App ID + App Secret shown on its own
settings page -- FACEBOOK_APP_ID/SECRET below must be the Facebook Login
for Business values, not the Instagram- or Threads-specific ones, and not
necessarily the same as the Meta app's top-level "App ID" shown on the
Dashboard (check the Facebook Login for Business product's own settings
page first; if it doesn't show separate values, the top-level App ID/
Secret is the right one to use here).

Flow:
  1. Browser -> AUTHORIZE_URL (user approves pages_show_list,
     pages_manage_posts, pages_read_engagement) -> redirected back with a
     `code`
  2. code -> short-lived User Access Token (graph.facebook.com)
  3. short-lived -> long-lived User Access Token, ~60 days
     (graph.facebook.com)
  4. long-lived User Access Token -> list the Pages this user administers
     (/me/accounts) -> take the WokeVision Page's own Page Access Token
     from that list. Page Access Tokens inherit the long-lived user
     token's ~60 day lifetime and are refreshed by simply re-deriving them
     from a refreshed user token.
  5. Publishing: upload the rendered video to the Page as a Reel -- create
     an upload session, upload the video bytes, then publish it. Mirrors
     the two-step "start -> poll/finish -> publish" shape used by
     Instagram/Threads, but via the /{page_id}/video_reels endpoint.
"""
import os
import json
import time
import requests

import db

APP_ID = os.environ.get("FACEBOOK_APP_ID")
APP_SECRET = os.environ.get("FACEBOOK_APP_SECRET")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Which Facebook Page to post to, when the user's token grants access to
# more than one. Optional -- if unset, the first Page returned by
# /me/accounts is used (fine for a single-Page account like WokeVision's).
PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")

GRAPH_BASE = "https://graph.facebook.com/v23.0"
AUTHORIZE_URL = "https://www.facebook.com/v23.0/dialog/oauth"
TOKEN_URL = f"{GRAPH_BASE}/oauth/access_token"

SCOPES = "pages_show_list,pages_manage_posts,pages_read_engagement,business_management"

PLATFORM = "facebook"


class FacebookError(Exception):
    pass


def configured() -> bool:
    return bool(APP_ID and APP_SECRET and PUBLIC_BASE_URL)


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/connections/facebook/callback"


def get_auth_url(state: str) -> str:
    if not configured():
        raise FacebookError("Facebook isn't configured yet (missing FACEBOOK_APP_ID/SECRET or PUBLIC_BASE_URL).")
    params = {
        "client_id": APP_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"{AUTHORIZE_URL}?{query}"


def _exchange_code_for_short_lived_token(code: str) -> dict:
    resp = requests.get(
        TOKEN_URL,
        params={
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "redirect_uri": redirect_uri(),
            "code": code,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise FacebookError(f"Short-lived token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _exchange_for_long_lived_token(short_lived_token: str) -> dict:
    resp = requests.get(
        TOKEN_URL,
        params={
            "grant_type": "fb_exchange_token",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "fb_exchange_token": short_lived_token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise FacebookError(f"Long-lived token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _get_page_token(user_access_token: str) -> dict:
    """Looks up the Pages this user administers and returns the chosen
    Page's {id, name, access_token}. Raises FacebookError if the user
    doesn't administer any Page (or not the configured FACEBOOK_PAGE_ID
    one)."""
    resp = requests.get(
        f"{GRAPH_BASE}/me/accounts",
        params={"fields": "id,name,access_token", "access_token": user_access_token},
        timeout=20,
    )
    if resp.status_code != 200:
        raise FacebookError(f"Could not list Facebook Pages: {resp.status_code} {resp.text[:500]}")
    body = resp.json()
    pages = body.get("data") or []
    if not pages:
        # Temporary diagnostic: surface the raw /me/accounts response so we
        # can see *why* Facebook thinks there are no Pages (e.g. a scopes
        # mismatch, a business-asset access quirk, pagination/paging info)
        # rather than guessing blind. Safe to trim back down once this is
        # understood -- there's nothing secret in this response, it's just
        # an empty/near-empty Pages list.
        raise FacebookError(
            "This Facebook account doesn't administer any Page -- connect with the account that manages the "
            f"WokeVision Page. (Raw /me/accounts response: {json.dumps(body)[:800]})"
        )
    if PAGE_ID:
        for p in pages:
            if p.get("id") == PAGE_ID:
                return p
        raise FacebookError(f"The configured FACEBOOK_PAGE_ID ({PAGE_ID}) isn't among this account's Pages.")
    return pages[0]


def handle_callback(code: str):
    """Completes the OAuth flow from an authorization code: exchanges it for
    a long-lived user token, looks up the WokeVision Page's own Page Access
    Token, and stores that (not the user token -- the Page token is what
    publish_video actually uses). Raises FacebookError with a human-readable
    message on any failure -- the caller surfaces that directly to the
    user."""
    short = _exchange_code_for_short_lived_token(code)
    short_token = short.get("access_token")
    if not short_token:
        raise FacebookError("No access token returned from Facebook.")

    long_lived = _exchange_for_long_lived_token(short_token)
    user_access_token = long_lived.get("access_token")
    expires_in = long_lived.get("expires_in", 60 * 24 * 3600)
    if not user_access_token:
        raise FacebookError("Could not get a long-lived token from Facebook.")

    page = _get_page_token(user_access_token)
    page_access_token = page.get("access_token")
    if not page_access_token:
        raise FacebookError("Facebook didn't return a Page access token.")

    import datetime
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    db.save_connection(
        PLATFORM, page_access_token, refresh_token=None, expires_at=expires_at,
        extra={
            "page_id": page.get("id"),
            "page_name": page.get("name"),
            # The long-lived USER token is kept too (separately from the
            # Page token used for publishing) purely so refresh_if_needed
            # can re-derive a fresh Page token later without asking the
            # user to reconnect every ~60 days.
            "user_access_token": user_access_token,
        },
    )
    return page


def refresh_if_needed():
    """Page Access Tokens inherit the long-lived user token's ~60 day
    lifetime. Rather than a separate refresh endpoint, we re-derive a fresh
    Page token from the stored long-lived user token once it's getting
    close to expiry -- same cadence as Instagram/Threads."""
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        return
    import datetime
    expires_at = conn.get("expires_at")
    now = datetime.datetime.now(datetime.timezone.utc)
    time_left = (expires_at - now).total_seconds() if expires_at else 0
    if time_left > 10 * 86400:
        return

    user_access_token = (conn.get("extra") or {}).get("user_access_token")
    if not user_access_token:
        db.set_check_result(PLATFORM, False, "No stored user token to refresh from -- reconnect Facebook.")
        return
    try:
        long_lived = _exchange_for_long_lived_token(user_access_token)
        new_user_token = long_lived.get("access_token")
        if not new_user_token:
            return
        page = _get_page_token(new_user_token)
        new_expires_at = now + datetime.timedelta(seconds=long_lived.get("expires_in", 60 * 24 * 3600))
        db.save_connection(
            PLATFORM, page.get("access_token"), expires_at=new_expires_at,
            extra={"page_id": page.get("id"), "page_name": page.get("name"), "user_access_token": new_user_token},
        )
    except FacebookError as e:
        db.set_check_result(PLATFORM, False, f"Token refresh failed: {e}")


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
        page_id = conn["extra"].get("page_id")
        resp = requests.get(
            f"{GRAPH_BASE}/{page_id}",
            params={"fields": "id,name", "access_token": conn["access_token"]},
            timeout=20,
        )
        if resp.status_code != 200:
            raise FacebookError(resp.text[:500])
        db.set_check_result(PLATFORM, True)
        page_name = resp.json().get("name") or conn.get("extra", {}).get("page_name")
        return {"connected": True, "ok": True, "label": page_name or "Connected", "error": None}
    except Exception as e:
        db.set_check_result(PLATFORM, False, str(e))
        return {"connected": True, "ok": False, "label": "Connection error", "error": str(e)}


def publish_video(video_url: str, caption: str) -> dict:
    """Uploads+publishes a video as a Facebook Reel on the connected Page.
    video_url must be a public URL (this app's own /files/<name>.mp4
    route). Returns {"media_id": ...}. Raises FacebookError on any
    failure, with the underlying platform message included so the UI can
    show something actionable."""
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        raise FacebookError("Facebook isn't connected.")
    refresh_if_needed()
    conn = db.get_connection(PLATFORM)
    access_token = conn["access_token"]
    page_id = conn["extra"].get("page_id")
    if not page_id:
        raise FacebookError("No Facebook Page id on file -- try reconnecting.")

    # Step 1: start an upload session for a hosted-URL video.
    start_resp = requests.post(
        f"{GRAPH_BASE}/{page_id}/video_reels",
        data={
            "upload_phase": "start",
            "access_token": access_token,
        },
        timeout=30,
    )
    if start_resp.status_code != 200:
        raise FacebookError(f"Could not start the upload: {start_resp.text[:500]}")
    start_data = start_resp.json()
    video_id = start_data.get("video_id")
    upload_url = start_data.get("upload_url")
    if not video_id or not upload_url:
        raise FacebookError("Facebook didn't return an upload session.")

    # Step 2: hand Facebook the public video_url to fetch directly, rather
    # than streaming the file bytes through this server.
    upload_resp = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {access_token}",
            "file_url": video_url,
        },
        timeout=60,
    )
    if upload_resp.status_code != 200:
        raise FacebookError(f"Video upload failed: {upload_resp.text[:500]}")

    # Facebook processes the uploaded video asynchronously; poll until it's
    # ready to publish (or errors out). Mirrors Instagram/Threads' container
    # pattern -- up to 5 minutes, checked every 10s.
    deadline = time.time() + 5 * 60
    status = "processing"
    while time.time() < deadline:
        status_resp = requests.get(
            f"{GRAPH_BASE}/{video_id}",
            params={"fields": "status", "access_token": access_token},
            timeout=20,
        )
        status_data = status_resp.json().get("status") or {}
        phase = status_data.get("video_status") or status_data.get("uploading_phase", {}).get("status")
        if phase == "ready":
            break
        if phase == "error":
            raise FacebookError(f"Facebook failed to process the video: {status_data}")
        time.sleep(10)
    else:
        raise FacebookError("Timed out waiting for Facebook to process the video.")

    # Step 3: publish the processed video as a Reel.
    publish_resp = requests.post(
        f"{GRAPH_BASE}/{page_id}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption or "",
            "access_token": access_token,
        },
        timeout=30,
    )
    if publish_resp.status_code != 200:
        raise FacebookError(f"Could not publish: {publish_resp.text[:500]}")
    return {"media_id": video_id}
