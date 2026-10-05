"""YouTube (Shorts) connect + publish, via standard Google OAuth 2.0 and
the YouTube Data API v3's resumable upload endpoint. Plain `requests`
calls throughout, no google-api-python-client dependency, to match the
rest of this app's platform modules.

Flow:
  1. Browser -> AUTHORIZE_URL (user approves) -> redirected back with a
     `code`. access_type=offline + prompt=consent so Google actually
     hands back a refresh_token (it's silently omitted otherwise, e.g. on
     a second consent for the same user/client).
  2. code -> access + refresh token (oauth2.googleapis.com/token)
  3. access token refreshed from the stored refresh_token whenever it's
     close to expiring (Google access tokens are short-lived, ~1h, but
     refresh tokens don't expire under normal use).
  4. Publishing: stream the rendered video straight from this app's own
     /files/<name>.mp4 URL into a YouTube resumable upload session,
     without buffering the whole file in memory -- important on a
     memory-constrained host.
"""
import os
import requests

import db

CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

# youtube.upload alone is enough to publish; youtube.readonly lets us look
# up the connected channel's name for the status display.
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"

PLATFORM = "youtube"


class YouTubeError(Exception):
    pass


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and PUBLIC_BASE_URL)


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/connections/youtube/callback"


def get_auth_url(state: str) -> str:
    if not configured():
        raise YouTubeError("YouTube isn't configured yet (missing YOUTUBE_CLIENT_ID/SECRET or PUBLIC_BASE_URL).")
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"{AUTHORIZE_URL}?{query}"


def _exchange_code_for_tokens(code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
            "code": code,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise YouTubeError(f"Token exchange failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _refresh_access_token(refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise YouTubeError(f"Token refresh failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _get_channel(access_token: str) -> dict:
    resp = requests.get(
        CHANNELS_URL,
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise YouTubeError(f"Channel lookup failed: {resp.status_code} {resp.text[:500]}")
    items = resp.json().get("items") or []
    if not items:
        raise YouTubeError("No YouTube channel found on this Google account.")
    return items[0]


def handle_callback(code: str):
    """Completes the OAuth flow from an authorization code: exchanges it
    for access + refresh tokens, looks up the connected channel, and
    stores it. Raises YouTubeError with a human-readable message on any
    failure -- the caller surfaces that directly to the user."""
    tokens = _exchange_code_for_tokens(code)
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    if not access_token:
        raise YouTubeError("No access token returned from Google.")
    if not refresh_token:
        raise YouTubeError(
            "Google didn't return a refresh token -- this can happen if the account already "
            "granted access before. Revoke WokeVision's access at myaccount.google.com/permissions "
            "and try connecting again."
        )

    channel = _get_channel(access_token)

    import datetime
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    db.save_connection(
        PLATFORM, access_token, refresh_token=refresh_token, expires_at=expires_at,
        extra={"channel_id": channel.get("id"), "channel_title": channel.get("snippet", {}).get("title")},
    )
    return channel


def refresh_if_needed():
    """Google access tokens are short-lived (~1h); refresh a bit before
    expiry using the stored refresh_token. Called opportunistically before
    publishing and status checks so a token never silently goes stale."""
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
    if new_token:
        new_expires_at = now + datetime.timedelta(seconds=data.get("expires_in", 3600))
        db.save_connection(
            PLATFORM, new_token, refresh_token=conn["refresh_token"], expires_at=new_expires_at,
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
        channel = _get_channel(conn["access_token"])
        db.set_check_result(PLATFORM, True)
        title = channel.get("snippet", {}).get("title") or conn.get("extra", {}).get("channel_title")
        return {"connected": True, "ok": True, "label": title or "Connected", "error": None}
    except Exception as e:
        db.set_check_result(PLATFORM, False, str(e))
        return {"connected": True, "ok": False, "label": "Connection error", "error": str(e)}


def publish_video(video_url: str, caption: str, post: dict = None) -> dict:
    """Streams the rendered video from video_url into a YouTube resumable
    upload session and publishes it as public. video_url must be a public
    URL (this app's own /files/<name>.mp4 route). Returns {"video_id":
    ...}. Raises YouTubeError on any failure, with the underlying platform
    message included so the UI can show something actionable."""
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        raise YouTubeError("YouTube isn't connected.")
    refresh_if_needed()
    conn = db.get_connection(PLATFORM)
    access_token = conn["access_token"]

    post = post or {}
    caption = caption or ""
    if post.get("title") or post.get("description"):
        # Written/edited in the editor: separate title + description, tags, category.
        title = (post.get("title") or "").strip() or "WokeVision"
        description = (post.get("description") or "").strip()
        tags = [t for t in (post.get("tags") or []) if t]
        category_id = str(post.get("category") or "25")
    else:
        first_line = caption.splitlines()[0].strip() if caption.strip() else "WokeVision"
        title = (first_line or "WokeVision")[:95]
        if "short" not in title.lower():
            title = (title + " #Shorts")[:100]
        description = caption if "#shorts" in caption.lower() else (caption + "\n\n#Shorts").strip()
        tags, category_id = [], "22"
    title = title[:100]

    # Pull the source video with a streaming GET so we never hold the
    # whole file in memory, and so we know its exact size up front (the
    # resumable upload session needs Content-Length).
    source_resp = requests.get(video_url, stream=True, timeout=60)
    if source_resp.status_code != 200:
        raise YouTubeError(f"Could not fetch the rendered video to upload: {source_resp.status_code}")
    content_length = source_resp.headers.get("Content-Length")
    if not content_length:
        source_resp.close()
        raise YouTubeError("Rendered video has no known size -- can't start a resumable upload.")

    init_resp = requests.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": content_length,
        },
        json={
            "snippet": {"title": title, "description": description, "categoryId": category_id,
                        **({"tags": tags} if tags else {})},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        },
        timeout=30,
    )
    if init_resp.status_code != 200:
        source_resp.close()
        raise YouTubeError(f"Could not start the upload session: {init_resp.status_code} {init_resp.text[:500]}")
    upload_session_url = init_resp.headers.get("Location")
    if not upload_session_url:
        source_resp.close()
        raise YouTubeError("YouTube didn't return an upload session URL.")

    # `requests` can't determine the length of a raw urllib3 socket stream
    # on its own (it has no __len__/len/fileno it can use), so passing
    # source_resp.raw straight through as `data=` makes requests fall back
    # to adding "Transfer-Encoding: chunked" -- IN ADDITION to the
    # Content-Length we set explicitly below, since prepare_body() never
    # removes a caller-supplied header. Having both on the same request is
    # invalid HTTP, and Google's frontend rejects it outright with a
    # generic "Error 400 (Bad Request)!!1" HTML page -- not a YouTube API
    # error at all, which is why it happens on every upload regardless of
    # video size. Giving the raw stream an explicit `.len` lets requests'
    # super_len() succeed, so it sends a normal Content-Length request
    # instead of switching to chunked.
    source_resp.raw.len = int(content_length)

    upload_resp = requests.put(
        upload_session_url,
        data=source_resp.raw,
        headers={"Content-Type": "video/mp4", "Content-Length": content_length},
        timeout=600,
    )
    source_resp.close()
    if upload_resp.status_code not in (200, 201):
        raise YouTubeError(f"Upload failed: {upload_resp.status_code} {upload_resp.text[:500]}")
    video_id = upload_resp.json().get("id")
    return {"video_id": video_id}
