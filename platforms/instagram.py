"""Instagram connect + publish, via "Business Login for Instagram" (the
"API setup with Instagram login" option under the Instagram API use case)
-- this logs in directly with the Instagram Business account, no linked
Facebook Page required, which matches how @wokevision_ is set up and keeps
this fully separate from any personal Facebook account.

Note: this use case's "Instagram app ID" / "Instagram app secret" (shown
on its own "API setup with Instagram login" settings page) are separate
from the main Meta app's ID/secret -- INSTAGRAM_APP_ID/SECRET below must
be those Instagram-specific values, not the top-level app ones.

Flow:
  1. Browser -> AUTH_URL (user approves) -> redirected back with a `code`
  2. code -> short-lived access token (api.instagram.com)
  3. short-lived -> long-lived token, ~60 days (graph.instagram.com)
  4. long-lived token refreshed periodically (also ~60 days, must be done
     before it expires, and the token must be at least 24h old to refresh)
  5. Publishing: create a media container from the rendered video's public
     URL, poll until Instagram finishes processing it, then publish it.
"""
import os
import time
import requests

import db

APP_ID = os.environ.get("INSTAGRAM_APP_ID")
APP_SECRET = os.environ.get("INSTAGRAM_APP_SECRET")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
SHORT_LIVED_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
LONG_LIVED_EXCHANGE_URL = "https://graph.instagram.com/access_token"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
GRAPH_BASE = "https://graph.instagram.com/v23.0"

SCOPES = ("instagram_business_basic,instagram_business_content_publish,"
          "instagram_business_manage_insights,instagram_business_manage_messages")

PLATFORM = "instagram"


class InstagramError(Exception):
    pass


def configured() -> bool:
    return bool(APP_ID and APP_SECRET and PUBLIC_BASE_URL)


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/connections/instagram/callback"


def get_auth_url(state: str) -> str:
    if not configured():
        raise InstagramError("Instagram isn't configured yet (missing INSTAGRAM_APP_ID/SECRET or PUBLIC_BASE_URL).")
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
        raise InstagramError(f"Short-lived token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _exchange_for_long_lived_token(short_lived_token: str) -> dict:
    resp = requests.get(
        LONG_LIVED_EXCHANGE_URL,
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": APP_SECRET,
            "access_token": short_lived_token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise InstagramError(f"Long-lived token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _get_profile(access_token: str) -> dict:
    resp = requests.get(
        f"{GRAPH_BASE}/me",
        params={"fields": "id,username", "access_token": access_token},
        timeout=20,
    )
    if resp.status_code != 200:
        raise InstagramError(f"Profile lookup failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def handle_callback(code: str):
    """Completes the OAuth flow from an authorization code: exchanges it for
    a long-lived token, looks up the connected account's IG user id/username,
    and stores it. Raises InstagramError with a human-readable message on
    any failure -- the caller surfaces that directly to the user."""
    short = _exchange_code_for_short_lived_token(code)
    short_token = short.get("access_token")
    if not short_token:
        raise InstagramError("No access token returned from Instagram.")

    long_lived = _exchange_for_long_lived_token(short_token)
    access_token = long_lived.get("access_token")
    expires_in = long_lived.get("expires_in", 60 * 24 * 3600)
    if not access_token:
        raise InstagramError("Could not get a long-lived token from Instagram.")

    profile = _get_profile(access_token)

    import datetime
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    db.save_connection(
        PLATFORM, access_token, refresh_token=None, expires_at=expires_at,
        extra={"ig_user_id": profile.get("id"), "username": profile.get("username")},
    )
    return profile


def refresh_if_needed():
    """Long-lived Instagram tokens last ~60 days and can be refreshed for
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
        params={"grant_type": "ig_refresh_token", "access_token": conn["access_token"]},
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
            extra={"ig_user_id": conn["extra"].get("ig_user_id"), "username": conn["extra"].get("username")},
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
    """Uploads+publishes a video as a Reel. video_url must be a public URL
    (this app's own /files/<name>.mp4 route). Returns {"media_id": ...}.
    Raises InstagramError on any failure, with the underlying platform
    message included so the UI can show something actionable."""
    # `post` carries the Instagram-specific version written/edited in the editor.
    caption = (post or {}).get("caption", caption)
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        raise InstagramError("Instagram isn't connected.")
    refresh_if_needed()
    conn = db.get_connection(PLATFORM)
    access_token = conn["access_token"]
    ig_user_id = conn["extra"].get("ig_user_id")
    if not ig_user_id:
        raise InstagramError("No Instagram account id on file -- try reconnecting.")

    create_resp = requests.post(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        data={
            "video_url": video_url,
            "media_type": "REELS",
            "caption": caption or "",
            "access_token": access_token,
        },
        timeout=60,
    )
    if create_resp.status_code != 200:
        raise InstagramError(f"Could not start the upload: {create_resp.text[:500]}")
    container_id = create_resp.json().get("id")
    if not container_id:
        raise InstagramError("Instagram didn't return a container id.")

    # Instagram processes the video asynchronously; poll until it's ready
    # to publish (or errors out). Their own docs say to check about once a
    # minute for up to 5 minutes -- we check a bit more often since our
    # videos are short.
    deadline = time.time() + 5 * 60
    status = "IN_PROGRESS"
    while time.time() < deadline:
        status_resp = requests.get(
            f"{GRAPH_BASE}/{container_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=20,
        )
        status = status_resp.json().get("status_code", "IN_PROGRESS")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(f"Instagram failed to process the video (status: {status}).")
        time.sleep(10)
    else:
        raise InstagramError("Timed out waiting for Instagram to process the video.")

    publish_resp = requests.post(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": access_token},
        timeout=30,
    )
    if publish_resp.status_code != 200:
        raise InstagramError(f"Could not publish: {publish_resp.text[:500]}")
    return {"media_id": publish_resp.json().get("id")}


# --- Direct messages + account insights (need the manage_messages /
# manage_insights scopes; until Instagram is reconnected with them these
# raise InstagramError with Instagram's own message, which callers treat as
# "permission not granted yet" rather than a failure) ----------------------

def _auth():
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        raise InstagramError("Instagram isn't connected.")
    refresh_if_needed()
    conn = db.get_connection(PLATFORM)
    uid = conn.get("extra", {}).get("ig_user_id")
    if not uid:
        raise InstagramError("No Instagram account id on file -- try reconnecting.")
    return conn["access_token"], uid


def list_conversations(limit: int = 25) -> list:
    token, uid = _auth()
    url, params, out = f"{GRAPH_BASE}/me/conversations", {
        "platform": "instagram", "limit": limit, "access_token": token,
        "fields": "id,updated_time,participants,messages.limit(1){message,created_time,from}"}, []
    # Instagram can return an empty page that still has a `next` cursor, so
    # follow a few pages rather than stopping at the first empty one.
    for _ in range(6):
        r = requests.get(url, params=params, timeout=25)
        if r.status_code != 200:
            raise InstagramError(r.text[:400])
        j = r.json()
        for c in j.get("data", []):
            others = [p for p in c.get("participants", {}).get("data", []) if p.get("id") != uid]
            other = others[0] if others else {}
            last = (c.get("messages", {}).get("data") or [{}])[0]
            out.append({
                "id": c["id"], "platform": PLATFORM,
                "with_id": other.get("id"), "with_name": other.get("username") or other.get("id") or "Unknown",
                "last_text": last.get("message") or "", "updated": c.get("updated_time"),
            })
        nxt = (j.get("paging") or {}).get("next")
        if not nxt or len(out) >= limit:
            break
        url, params = nxt, None
    return out


def get_thread(conversation_id: str, limit: int = 40) -> list:
    token, uid = _auth()
    r = requests.get(f"{GRAPH_BASE}/{conversation_id}", params={
        "fields": f"messages.limit({limit}){{id,message,created_time,from}}", "access_token": token}, timeout=25)
    if r.status_code != 200:
        raise InstagramError(r.text[:400])
    msgs = r.json().get("messages", {}).get("data", [])
    return [{"id": m["id"], "text": m.get("message") or "(attachment)", "ts": m.get("created_time"),
             "mine": (m.get("from") or {}).get("id") == uid} for m in reversed(msgs)]


def send_message(recipient_id: str, text: str) -> dict:
    token, uid = _auth()
    r = requests.post(f"{GRAPH_BASE}/{uid}/messages", params={"access_token": token},
                      json={"recipient": {"id": recipient_id}, "message": {"text": text}}, timeout=25)
    if r.status_code != 200:
        raise InstagramError(r.text[:400])
    return r.json()


def account_insights(days: int = 30) -> dict:
    """Account-level views / reach over the last `days` days."""
    import time as _t
    token, uid = _auth()
    now = int(_t.time())
    out = {}
    for metric in ("views", "reach"):
        r = requests.get(f"{GRAPH_BASE}/{uid}/insights", params={
            "metric": metric, "period": "day", "metric_type": "total_value",
            "since": now - days * 86400, "until": now, "access_token": token}, timeout=25)
        if r.status_code != 200:
            raise InstagramError(r.text[:400])
        data = r.json().get("data") or []
        out[metric] = (data[0].get("total_value") or {}).get("value") if data else None
    return out


def views_between(since: int, until: int):
    token, uid = _auth()
    r = requests.get(f"{GRAPH_BASE}/{uid}/insights", params={
        "metric": "views", "period": "day", "metric_type": "total_value",
        "since": int(since), "until": int(until), "access_token": token}, timeout=25)
    if r.status_code != 200:
        raise InstagramError(r.text[:400])
    data = r.json().get("data") or []
    return (data[0].get("total_value") or {}).get("value") if data else None
