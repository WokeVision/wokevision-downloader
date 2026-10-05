"""Threads connect + publish, via the Threads API's own OAuth (same Meta
app as Instagram, but Threads has its own separate "Threads API" use case
with its own App ID/Secret and its own graph host -- graph.threads.net,
not graph.instagram.com or graph.facebook.com).

Note: like Instagram, the Threads use case's "Threads app ID" / "Threads
app secret" (shown on its own settings page under Use Cases -> Customize
-> Threads API) are separate from both the main Meta app's ID/secret AND
from the Instagram-specific ones -- THREADS_APP_ID/SECRET below must be
those Threads-specific values.

Flow:
  1. Browser -> AUTHORIZE_URL (user approves) -> redirected back with a
     `code`
  2. code -> short-lived access token (graph.threads.net)
  3. short-lived -> long-lived token, ~60 days (graph.threads.net)
  4. long-lived token refreshed periodically (also ~60 days, must be done
     before it expires, and the token must be at least 24h old to refresh)
  5. Publishing: create a media container from the rendered video's public
     URL, poll until Threads finishes processing it, then publish it.
"""
import os
import time
import requests

import db

APP_ID = os.environ.get("THREADS_APP_ID")
APP_SECRET = os.environ.get("THREADS_APP_SECRET")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

AUTHORIZE_URL = "https://threads.net/oauth/authorize"
SHORT_LIVED_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
LONG_LIVED_EXCHANGE_URL = "https://graph.threads.net/access_token"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
GRAPH_BASE = "https://graph.threads.net/v1.0"

SCOPES = "threads_basic,threads_content_publish,threads_manage_insights"

PLATFORM = "threads"


class ThreadsError(Exception):
    pass


def configured() -> bool:
    return bool(APP_ID and APP_SECRET and PUBLIC_BASE_URL)


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/connections/threads/callback"


def get_auth_url(state: str) -> str:
    if not configured():
        raise ThreadsError("Threads isn't configured yet (missing THREADS_APP_ID/SECRET or PUBLIC_BASE_URL).")
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
    resp = requests.post(
        SHORT_LIVED_TOKEN_URL,
        data={
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
            "code": code,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise ThreadsError(f"Short-lived token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _exchange_for_long_lived_token(short_lived_token: str) -> dict:
    resp = requests.get(
        LONG_LIVED_EXCHANGE_URL,
        params={
            "grant_type": "th_exchange_token",
            "client_secret": APP_SECRET,
            "access_token": short_lived_token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise ThreadsError(f"Long-lived token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _get_profile(access_token: str) -> dict:
    resp = requests.get(
        f"{GRAPH_BASE}/me",
        params={"fields": "id,username", "access_token": access_token},
        timeout=20,
    )
    if resp.status_code != 200:
        raise ThreadsError(f"Profile lookup failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def handle_callback(code: str):
    """Completes the OAuth flow from an authorization code: exchanges it for
    a long-lived token, looks up the connected account's Threads user
    id/username, and stores it. Raises ThreadsError with a human-readable
    message on any failure -- the caller surfaces that directly to the
    user."""
    short = _exchange_code_for_short_lived_token(code)
    short_token = short.get("access_token")
    if not short_token:
        raise ThreadsError("No access token returned from Threads.")

    long_lived = _exchange_for_long_lived_token(short_token)
    access_token = long_lived.get("access_token")
    expires_in = long_lived.get("expires_in", 60 * 24 * 3600)
    if not access_token:
        raise ThreadsError("Could not get a long-lived token from Threads.")

    profile = _get_profile(access_token)

    import datetime
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    db.save_connection(
        PLATFORM, access_token, refresh_token=None, expires_at=expires_at,
        extra={"threads_user_id": profile.get("id"), "username": profile.get("username")},
    )
    return profile


def refresh_if_needed():
    """Long-lived Threads tokens last ~60 days and can be refreshed for
    another 60 once they're at least 24h old. Called opportunistically
    before publishing so a token never silently goes stale between posts."""
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        return
    import datetime
    expires_at = conn.get("expires_at")
    connected_at = conn.get("connected_at")
    now = datetime.datetime.now(datetime.timezone.utc)
    token_age = (now - connected_at).total_seconds() if connected_at else 999999
    time_left = (expires_at - now).total_seconds() if expires_at else 0

    # Refresh once the token is at least a day old AND has less than ~10
    # days left -- plenty of margin before it would actually expire.
    if token_age < 86400 or time_left > 10 * 86400:
        return
    resp = requests.get(
        REFRESH_URL,
        params={"grant_type": "th_refresh_token", "access_token": conn["access_token"]},
        timeout=30,
    )
    if resp.status_code != 200:
        db.set_check_result(PLATFORM, False, f"Token refresh failed: {resp.text[:300]}")
        return
    data = resp.json()
    new_token = data.get("access_token")
    if new_token:
        new_expires_at = now + datetime.timedelta(seconds=data.get("expires_in", 60 * 24 * 3600))
        db.save_connection(
            PLATFORM, new_token, expires_at=new_expires_at,
            extra={"threads_user_id": conn["extra"].get("threads_user_id"), "username": conn["extra"].get("username")},
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
        profile = _get_profile(conn["access_token"])
        db.set_check_result(PLATFORM, True)
        username = profile.get("username") or conn.get("extra", {}).get("username")
        return {"connected": True, "ok": True, "label": f"@{username}" if username else "Connected", "error": None}
    except Exception as e:
        db.set_check_result(PLATFORM, False, str(e))
        return {"connected": True, "ok": False, "label": "Connection error", "error": str(e)}


def publish_video(video_url: str, caption: str, post: dict = None) -> dict:
    """Uploads+publishes a video as a Threads post. video_url must be a
    public URL (this app's own /files/<name>.mp4 route). Returns
    {"media_id": ...}. Raises ThreadsError on any failure, with the
    underlying platform message included so the UI can show something
    actionable."""
    caption = (post or {}).get("text", caption)
    topic_tag = ((post or {}).get("topic_tag") or "").strip()
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        raise ThreadsError("Threads isn't connected.")
    refresh_if_needed()
    conn = db.get_connection(PLATFORM)
    access_token = conn["access_token"]
    threads_user_id = conn["extra"].get("threads_user_id")
    if not threads_user_id:
        raise ThreadsError("No Threads account id on file -- try reconnecting.")

    create_data = {
        "video_url": video_url,
        "media_type": "VIDEO",
        "text": caption or "",
        "access_token": access_token,
    }
    if topic_tag:
        # Threads allows ONE topic tag per post, set here rather than as a
        # #hashtag in the text (1-50 chars, no "." or "&").
        create_data["topic_tag"] = topic_tag
    create_resp = requests.post(
        f"{GRAPH_BASE}/{threads_user_id}/threads",
        data=create_data,
        timeout=60,
    )
    if create_resp.status_code != 200:
        raise ThreadsError(f"Could not start the upload: {create_resp.text[:500]}")
    container_id = create_resp.json().get("id")
    if not container_id:
        raise ThreadsError("Threads didn't return a container id.")

    # Threads processes the video asynchronously; poll until it's ready to
    # publish (or errors out). Mirrors Instagram's container pattern -- up
    # to 5 minutes, checked every 10s, which is plenty for our short clips.
    deadline = time.time() + 5 * 60
    status = "IN_PROGRESS"
    while time.time() < deadline:
        status_resp = requests.get(
            f"{GRAPH_BASE}/{container_id}",
            params={"fields": "status", "access_token": access_token},
            timeout=20,
        )
        status = status_resp.json().get("status", "IN_PROGRESS")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise ThreadsError(f"Threads failed to process the video (status: {status}).")
        time.sleep(10)
    else:
        raise ThreadsError("Timed out waiting for Threads to process the video.")

    publish_resp = requests.post(
        f"{GRAPH_BASE}/{threads_user_id}/threads_publish",
        data={"creation_id": container_id, "access_token": access_token},
        timeout=30,
    )
    if publish_resp.status_code != 200:
        raise ThreadsError(f"Could not publish: {publish_resp.text[:500]}")
    return {"media_id": publish_resp.json().get("id")}
