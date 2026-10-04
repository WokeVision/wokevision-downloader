"""TikTok connect + publish, via the Content Posting API (TikTok for
Developers). Standard OAuth 2.0 (PKCE not required for a server-side web
app, only for mobile/desktop apps), then a chunked FILE_UPLOAD publish --
not PULL_FROM_URL, which would additionally require verifying ownership of
this app's domain in the TikTok Developer Portal (a DNS TXT record) before
it could be used at all. Streaming our own rendered-video bytes up in
chunks avoids that extra prerequisite and matches the same pattern already
used for YouTube/X.

Important caveat, not a bug: TikTok restricts every post from an app that
hasn't been through their manual content-posting audit to SELF_ONLY
(private) visibility, regardless of what privacy_level is requested --
see https://developers.tiktok.com/doc/content-posting-api-get-started.
Until @wokevision_'s app is audited, publish_video() will successfully
post, but only privately/visible to the account itself. There's nothing to
fix in code for that -- it clears up once TikTok approves the app.

Flow:
  1. Browser -> AUTHORIZE_URL (user approves) -> redirected back with a
     `code`.
  2. code -> access + refresh token (open.tiktokapis.com/v2/oauth/token/)
  3. access token refreshed from the stored refresh_token whenever it's
     close to expiring (TikTok access tokens last 24h; refresh tokens last
     365 days and rotate on each use).
  4. Publishing: stream the rendered video from this app's own
     /files/<name>.mp4 URL, chunk it (5-64MB, final chunk up to 128MB) for
     the video/init FILE_UPLOAD flow, PUT each chunk with a Content-Range
     header, then poll publish status until it completes or fails.
"""
import os
import time
import requests

import db

CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

# video.publish posts directly to the profile (what we need); user.info.basic
# is just so the status card can show the connected account's name.
SCOPES = "video.publish,user.info.basic"

PLATFORM = "tiktok"

# TikTok's own bounds: 5-64MB per chunk, final chunk may run up to 128MB,
# max 1000 chunks, max 4GB total. Our rendered clips are short-form/small,
# so a flat 8MB chunk size comfortably clears the 5MB floor while keeping
# chunk count low.
CHUNK_SIZE = 8 * 1024 * 1024
MIN_CHUNK_SIZE = 5 * 1024 * 1024


def _plan_chunks(total_bytes: int) -> list:
    """Returns the byte size of each chunk to upload. TikTok requires every
    chunk -- including the last -- to be at least 5MB, UNLESS the whole
    video is a single chunk (video_size == chunk_size, total_chunk_count ==
    1), which is also the only valid shape when the video itself is under
    5MB. A flat `total_bytes // CHUNK_SIZE` split can leave a trailing
    remainder under 5MB (e.g. a 20MB video split into 8+8+4), which TikTok
    rejects as an invalid chunk size -- so instead of starting a new tiny
    final chunk, fold any under-sized remainder into the previous chunk."""
    if total_bytes <= CHUNK_SIZE:
        return [total_bytes]
    sizes = []
    remaining = total_bytes
    while remaining > CHUNK_SIZE:
        if remaining - CHUNK_SIZE < MIN_CHUNK_SIZE:
            sizes.append(remaining)
            remaining = 0
            break
        sizes.append(CHUNK_SIZE)
        remaining -= CHUNK_SIZE
    if remaining > 0:
        sizes.append(remaining)
    return sizes


class TikTokError(Exception):
    pass


def configured() -> bool:
    return bool(CLIENT_KEY and CLIENT_SECRET and PUBLIC_BASE_URL)


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/connections/tiktok/callback"


def get_auth_url(state: str) -> str:
    if not configured():
        raise TikTokError("TikTok isn't configured yet (missing TIKTOK_CLIENT_KEY/SECRET or PUBLIC_BASE_URL).")
    params = {
        "client_key": CLIENT_KEY,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri(),
        "state": state,
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"{AUTHORIZE_URL}?{query}"


def _exchange_code_for_tokens(code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
        },
        timeout=30,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code != 200 or data.get("error"):
        raise TikTokError(f"Token exchange failed: {resp.status_code} {resp.text[:500]}")
    return data


def _refresh_access_token(refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code != 200 or data.get("error"):
        raise TikTokError(f"Token refresh failed: {resp.status_code} {resp.text[:500]}")
    return data


def _get_user_info(access_token: str) -> dict:
    resp = requests.get(
        USER_INFO_URL,
        params={"fields": "open_id,display_name"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise TikTokError(f"User lookup failed: {resp.status_code} {resp.text[:500]}")
    data = (resp.json().get("data") or {}).get("user") or {}
    if not data:
        raise TikTokError("No user data returned from TikTok.")
    return data


def handle_callback(code: str):
    """Completes the OAuth flow from an authorization code: exchanges it for
    access + refresh tokens, looks up the connected account's display name,
    and stores it. Raises TikTokError with a human-readable message on any
    failure -- the caller surfaces that directly to the user."""
    tokens = _exchange_code_for_tokens(code)
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 86400)
    if not access_token:
        raise TikTokError("No access token returned from TikTok.")
    if not refresh_token:
        raise TikTokError("No refresh token returned from TikTok -- try connecting again.")

    user = _get_user_info(access_token)

    import datetime
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    db.save_connection(
        PLATFORM, access_token, refresh_token=refresh_token, expires_at=expires_at,
        extra={"open_id": user.get("open_id"), "display_name": user.get("display_name")},
    )
    return user


def refresh_if_needed():
    """TikTok access tokens last 24h; refresh a bit before expiry using the
    stored refresh_token. The refresh token itself rotates on each use (and
    lasts 365 days), so the new one must be saved every time. Called
    opportunistically before publishing and status checks so a token never
    silently goes stale."""
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
        new_expires_at = now + datetime.timedelta(seconds=data.get("expires_in", 86400))
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
        user = _get_user_info(conn["access_token"])
        db.set_check_result(PLATFORM, True)
        name = user.get("display_name") or conn.get("extra", {}).get("display_name")
        return {"connected": True, "ok": True, "label": name or "Connected", "error": None}
    except Exception as e:
        db.set_check_result(PLATFORM, False, str(e))
        return {"connected": True, "ok": False, "label": "Connection error", "error": str(e)}


def _upload_video(access_token: str, video_url: str, caption: str) -> str:
    """Runs the full chunked FILE_UPLOAD publish cycle (init -> chunked PUTs
    -> poll status) and returns the resulting publish_id. Streams the
    source video rather than buffering it whole, chunking it as it reads so
    memory use stays flat regardless of video length."""
    source_resp = requests.get(video_url, stream=True, timeout=60)
    if source_resp.status_code != 200:
        raise TikTokError(f"Could not fetch the rendered video to upload: {source_resp.status_code}")
    content_length = source_resp.headers.get("Content-Length")
    if not content_length:
        source_resp.close()
        raise TikTokError("Rendered video has no known size -- can't start a chunked upload.")
    total_bytes = int(content_length)
    chunk_sizes = _plan_chunks(total_bytes)
    total_chunks = len(chunk_sizes)
    # TikTok's `chunk_size` field is nominal -- the size of every chunk
    # except (possibly) the last, which is what each PUT's Content-Range
    # actually reflects. For a single-chunk upload this must equal
    # video_size exactly.
    nominal_chunk_size = chunk_sizes[0]

    title = (caption or "").strip()[:2200]

    init_resp = requests.post(
        INIT_URL,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "post_info": {
                "title": title,
                "privacy_level": os.environ.get("TIKTOK_PRIVACY_LEVEL", "PUBLIC_TO_EVERYONE"),
                "disable_duet": False,
                "disable_stitch": False,
                "disable_comment": False,
                "video_cover_timestamp_ms": 1000,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": total_bytes,
                "chunk_size": nominal_chunk_size,
                "total_chunk_count": total_chunks,
            },
        },
        timeout=30,
    )
    init_data = init_resp.json() if init_resp.content else {}
    if init_resp.status_code != 200 or (init_data.get("error") or {}).get("code") not in (None, "ok"):
        source_resp.close()
        raise TikTokError(f"Could not start the upload: {init_resp.status_code} {init_resp.text[:500]}")
    publish_id = (init_data.get("data") or {}).get("publish_id")
    upload_url = (init_data.get("data") or {}).get("upload_url")
    if not publish_id or not upload_url:
        source_resp.close()
        raise TikTokError("TikTok didn't return a publish id / upload url.")

    try:
        sent = 0
        for chunk_index, size in enumerate(chunk_sizes):
            chunk = source_resp.raw.read(size)
            if not chunk:
                break
            start = sent
            end = sent + len(chunk) - 1
            put_resp = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total_bytes}",
                },
                data=chunk,
                timeout=120,
            )
            # TikTok's own docs: a successful PUT returns 206 ("chunk
            # processed, more chunks pending") for every intermediate chunk
            # and 201 ("all parts uploaded") for the final one -- 206 is a
            # success code here, not an error, so it must be accepted too.
            if put_resp.status_code not in (200, 201, 206):
                raise TikTokError(f"Upload chunk {chunk_index} failed: {put_resp.status_code} {put_resp.text[:500]}")
            sent += len(chunk)
    finally:
        source_resp.close()

    if sent != total_bytes:
        raise TikTokError(f"Uploaded {sent} bytes but expected {total_bytes} -- upload was incomplete.")

    return publish_id


def _poll_publish_status(access_token: str, publish_id: str):
    deadline = time.time() + 5 * 60
    while time.time() < deadline:
        status_resp = requests.post(
            STATUS_URL,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"publish_id": publish_id},
            timeout=20,
        )
        data = (status_resp.json().get("data") or {}) if status_resp.content else {}
        status = data.get("status")
        if status == "PUBLISH_COMPLETE":
            return
        if status == "FAILED":
            raise TikTokError(f"TikTok failed to process the video (reason: {data.get('fail_reason')}).")
        time.sleep(5)
    raise TikTokError("Timed out waiting for TikTok to finish processing the video.")


def publish_video(video_url: str, caption: str) -> dict:
    """Uploads+publishes a video as a TikTok post. video_url must be a
    public URL (this app's own /files/<name>.mp4 route). Returns
    {"publish_id": ...}. Raises TikTokError on any failure, with the
    underlying platform message included so the UI can show something
    actionable. Note: until this app passes TikTok's content-posting audit,
    every post lands as SELF_ONLY (private) no matter what privacy_level is
    requested -- that's a TikTok-side restriction, not an error here."""
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        raise TikTokError("TikTok isn't connected.")
    refresh_if_needed()
    conn = db.get_connection(PLATFORM)
    access_token = conn["access_token"]

    publish_id = _upload_video(access_token, video_url, caption)
    _poll_publish_status(access_token, publish_id)
    return {"publish_id": publish_id}
