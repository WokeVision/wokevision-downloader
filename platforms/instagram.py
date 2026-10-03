"""Instagram connect + publish, via "Facebook Login for Business" with a
Login Configuration (config_id) -- this is the flow available on the
existing WokeVision Meta app, and it publishes through a Facebook Page
linked to the Instagram Business account.

The Page is a separate public business asset, not a personal profile --
linking wokevision_ to a dedicated "WokeVision" Page (rather than any
personal Facebook account) is what keeps this fully separate from the
account owner's personal Facebook, while still using this login product.

Flow:
  1. Browser -> facebook.com/dialog/oauth (with config_id) -> user approves
     -> redirected back with a `code`
  2. code -> short-lived USER access token (graph.facebook.com)
  3. short-lived -> long-lived USER token, ~60 days
  4. long-lived user token -> list of Pages the user manages, each with its
     own (effectively non-expiring, as long as the user token is valid)
     PAGE access token
  5. Page -> linked Instagram Business Account id
  6. Publishing: create a media container from the rendered video's public
     URL using the Page access token, poll until Instagram finishes
     processing it, then publish it -- same container flow as the
     Instagram-Graph-API-direct version, just via graph.facebook.com and a
     Page token instead of graph.instagram.com and an Instagram token.
"""
import os
import time
import datetime
import requests

import db

APP_ID = os.environ.get("INSTAGRAM_APP_ID")
APP_SECRET = os.environ.get("INSTAGRAM_APP_SECRET")
LOGIN_CONFIG_ID = os.environ.get("INSTAGRAM_LOGIN_CONFIG_ID")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

GRAPH_VERSION = "v23.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
AUTHORIZE_URL = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
TOKEN_URL = f"{GRAPH_BASE}/oauth/access_token"

PLATFORM = "instagram"


class InstagramError(Exception):
    pass


def configured() -> bool:
    return bool(APP_ID and APP_SECRET and LOGIN_CONFIG_ID and PUBLIC_BASE_URL)


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/connections/instagram/callback"


def get_auth_url(state: str) -> str:
    if not configured():
        raise InstagramError(
            "Instagram isn't configured yet (missing INSTAGRAM_APP_ID/SECRET, "
            "INSTAGRAM_LOGIN_CONFIG_ID, or PUBLIC_BASE_URL)."
        )
    params = {
        "client_id": APP_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "config_id": LOGIN_CONFIG_ID,
        "state": state,
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"{AUTHORIZE_URL}?{query}"


def _exchange_code_for_user_token(code: str) -> dict:
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
        raise InstagramError(f"Token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _exchange_for_long_lived_user_token(short_lived_token: str) -> dict:
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
        raise InstagramError(f"Long-lived token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _get_page_and_ig_account(user_token: str) -> dict:
    """Finds the Page this user manages and the Instagram Business account
    linked to it. Assumes a single relevant Page (the dedicated WokeVision
    Page) -- if several are returned, picks the first one that actually has
    an Instagram Business account linked."""
    pages_resp = requests.get(
        f"{GRAPH_BASE}/me/accounts",
        params={"access_token": user_token, "fields": "id,name,access_token"},
        timeout=30,
    )
    if pages_resp.status_code != 200:
        raise InstagramError(f"Could not list Facebook Pages: {pages_resp.text[:500]}")
    pages = pages_resp.json().get("data", [])
    if not pages:
        raise InstagramError(
            "No Facebook Page found for this account. Create a Page and link "
            "your Instagram Business account to it, then try connecting again."
        )

    for page in pages:
        page_token = page.get("access_token")
        ig_resp = requests.get(
            f"{GRAPH_BASE}/{page['id']}",
            params={"fields": "instagram_business_account", "access_token": page_token},
            timeout=20,
        )
        if ig_resp.status_code != 200:
            continue
        ig_account = ig_resp.json().get("instagram_business_account")
        if ig_account and ig_account.get("id"):
            username = None
            user_resp = requests.get(
                f"{GRAPH_BASE}/{ig_account['id']}",
                params={"fields": "username", "access_token": page_token},
                timeout=20,
            )
            if user_resp.status_code == 200:
                username = user_resp.json().get("username")
            return {
                "page_id": page["id"],
                "page_name": page.get("name"),
                "page_access_token": page_token,
                "ig_user_id": ig_account["id"],
                "username": username,
            }

    raise InstagramError(
        "Found a Facebook Page, but it doesn't have an Instagram Business "
        "account linked yet. Link wokevision_ to it (Instagram app -> "
        "Settings -> Account -> Linked accounts -> Facebook), then try again."
    )


def handle_callback(code: str):
    """Completes the OAuth flow: exchanges the code for a long-lived user
    token, finds the linked Page + Instagram Business account, and stores
    everything needed to publish. Raises InstagramError with a
    human-readable message on any failure -- the caller surfaces that
    directly to the user."""
    short = _exchange_code_for_user_token(code)
    short_token = short.get("access_token")
    if not short_token:
        raise InstagramError("No access token returned from Facebook.")

    long_lived = _exchange_for_long_lived_user_token(short_token)
    user_token = long_lived.get("access_token")
    expires_in = long_lived.get("expires_in", 60 * 24 * 3600)
    if not user_token:
        raise InstagramError("Could not get a long-lived token from Facebook.")

    info = _get_page_and_ig_account(user_token)

    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    db.save_connection(
        PLATFORM,
        access_token=info["page_access_token"],  # used directly for publishing
        refresh_token=user_token,  # kept to re-derive a Page token later
        expires_at=expires_at,
        extra={
            "page_id": info["page_id"],
            "page_name": info["page_name"],
            "ig_user_id": info["ig_user_id"],
            "username": info["username"],
        },
    )
    return info


def refresh_if_needed():
    """Facebook long-lived user tokens (~60 days) can be re-extended with
    the same fb_exchange_token grant while they're still valid, which also
    lets us re-derive a fresh Page token. Called opportunistically before
    publishing so a token never silently goes stale between posts. If the
    underlying user token has actually expired, this can't fix that -- the
    user has to reconnect, which check_status() will surface."""
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("refresh_token"):
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = conn.get("expires_at")
    time_left = (expires_at - now).total_seconds() if expires_at else 0

    if time_left > 10 * 86400:
        return
    try:
        long_lived = _exchange_for_long_lived_user_token(conn["refresh_token"])
        new_user_token = long_lived.get("access_token")
        if not new_user_token:
            return
        info = _get_page_and_ig_account(new_user_token)
        new_expires_at = now + datetime.timedelta(seconds=long_lived.get("expires_in", 60 * 24 * 3600))
        db.save_connection(
            PLATFORM,
            access_token=info["page_access_token"],
            refresh_token=new_user_token,
            expires_at=new_expires_at,
            extra={
                "page_id": info["page_id"],
                "page_name": info["page_name"],
                "ig_user_id": info["ig_user_id"],
                "username": info["username"],
            },
        )
    except Exception as e:
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
        ig_user_id = conn["extra"].get("ig_user_id")
        resp = requests.get(
            f"{GRAPH_BASE}/{ig_user_id}",
            params={"fields": "username", "access_token": conn["access_token"]},
            timeout=20,
        )
        if resp.status_code != 200:
            raise InstagramError(resp.text[:300])
        username = resp.json().get("username") or conn.get("extra", {}).get("username")
        db.set_check_result(PLATFORM, True)
        return {"connected": True, "ok": True, "label": f"@{username}" if username else "Connected", "error": None}
    except Exception as e:
        db.set_check_result(PLATFORM, False, str(e))
        return {"connected": True, "ok": False, "label": "Connection error", "error": str(e)}


def publish_video(video_url: str, caption: str) -> dict:
    """Uploads+publishes a video as a Reel. video_url must be a public URL
    (this app's own /files/<name>.mp4 route). Returns {"media_id": ...}.
    Raises InstagramError on any failure, with the underlying platform
    message included so the UI can show something actionable."""
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
