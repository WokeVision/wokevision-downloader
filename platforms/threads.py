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
# Add threads_read_replies + threads_manage_replies to the Threads use case first,
# then set COMMENT_SCOPES=1 on the server and reconnect Threads.
if os.environ.get("COMMENT_SCOPES") == "1":
    SCOPES += ",threads_read_replies,threads_manage_replies"
SCOPES = os.environ.get("THREADS_SCOPES") or SCOPES

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

    def _attempt(tag: str, text: str) -> dict:
        create_data = {
            "video_url": video_url,
            "media_type": "VIDEO",
            "text": text or "",
            "access_token": access_token,
        }
        if tag:
            # Threads allows ONE topic tag per post, set here rather than as a
            # #hashtag in the text (1-50 chars, no "." or "&").
            create_data["topic_tag"] = tag
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
                params={"fields": "status,error_message", "access_token": access_token},
                timeout=20,
            )
            sdata = status_resp.json()
            status = sdata.get("status", "IN_PROGRESS")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                detail = (sdata.get("error_message") or "").strip()
                raise ThreadsError(f"Threads failed to process the video (status: {status})" + (f": {detail}" if detail else "") + ".")
            time.sleep(10)
        else:
            raise ThreadsError("Timed out waiting for Threads to process the video.")

        # Meta's own guidance is to give a finished container a moment before
        # publishing, and threads_publish is known to return a transient
        # "OAuthException code 2 / unexpected error" (is_transient: true) when
        # called straight away. So: pause briefly, then retry transient failures
        # with growing gaps. Before each retry, check the account's latest posts
        # in case the earlier attempt actually went through (never double-post).
        time.sleep(8)
        last_err = ""
        for attempt, wait in enumerate((0, 10, 25, 45)):
            if wait:
                time.sleep(wait)
                already = _already_posted(threads_user_id, access_token, text)
                if already:
                    return {"media_id": already}
            publish_resp = requests.post(
                f"{GRAPH_BASE}/{threads_user_id}/threads_publish",
                data={"creation_id": container_id, "access_token": access_token},
                timeout=30,
            )
            if publish_resp.status_code == 200:
                return {"media_id": publish_resp.json().get("id")}
            last_err = publish_resp.text[:500]
            try:
                err = (publish_resp.json() or {}).get("error") or {}
            except Exception:
                err = {}
            if not (err.get("is_transient") or err.get("code") in (1, 2, 4, 17, 341)):
                break
        raise ThreadsError(f"Could not publish: {last_err}")

    # Same clip failed identically on repeated tries, so the cause can be the
    # post itself rather than timing. Try the full post first; if Meta keeps
    # refusing at the publish step, retry once without the topic tag, then once
    # more with the hashtags moved out of the text. The first variant that goes
    # through wins and is reported back so it's visible what changed.
    import re as _re
    plain = _re.sub(r"\s*#\w+", "", caption or "").strip()
    variants = [(topic_tag, caption, "")]
    if topic_tag:
        variants.append(("", caption, " (posted without the topic tag)"))
    if plain and plain != (caption or "").strip():
        variants.append(("", plain, " (posted without hashtags)"))
    first_err = None
    for tag, text, note in variants:
        try:
            out = _attempt(tag, text)
            if note:
                out["note"] = "Threads" + note
                print(f"THREADS: succeeded{note}", flush=True)
            return out
        except ThreadsError as e:
            msg = str(e)
            first_err = first_err or msg
            print(f"THREADS variant failed{note or ' (full post)'}: {msg[:300]}", flush=True)
            if not msg.startswith("Could not publish"):
                raise
    raise ThreadsError(first_err or "Threads publish failed.")


def _already_posted(threads_user_id: str, access_token: str, caption: str):
    """Returns the media id of a very recent post whose text matches, else None."""
    try:
        r = requests.get(
            f"{GRAPH_BASE}/{threads_user_id}/threads",
            params={"fields": "id,text,timestamp", "limit": 5, "access_token": access_token},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        want = (caption or "").strip()[:60]
        now = time.time()
        for item in r.json().get("data", []):
            if want and (item.get("text") or "").strip().startswith(want):
                ts = item.get("timestamp") or ""
                try:
                    import datetime as _dt
                    t = _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc).timestamp()
                    if now - t < 600:
                        return item.get("id")
                except Exception:
                    return item.get("id")
    except Exception:
        return None
    return None


# --- Replies (comments) ----------------------------------------------------------
# Needs threads_read_replies + threads_manage_replies (reconnect Threads once).
def _auth():
    conn = db.get_connection(PLATFORM)
    if not conn or not conn.get("access_token"):
        raise ThreadsError("Threads isn't connected.")
    refresh_if_needed()
    conn = db.get_connection(PLATFORM)
    uid = (conn.get("extra") or {}).get("threads_user_id")
    if not uid:
        raise ThreadsError("No Threads account id on file -- try reconnecting.")
    return conn["access_token"], uid


def own_username() -> str:
    conn = db.get_connection(PLATFORM) or {}
    return (conn.get("extra") or {}).get("username") or ""


def recent_comments(media_limit: int = 8, per_media: int = 30, diag: dict = None) -> list:
    token, uid = _auth()
    r = requests.get(f"{GRAPH_BASE}/{uid}/threads", params={"fields": "id,text,permalink,timestamp", "limit": media_limit, "access_token": token}, timeout=30)
    if r.status_code != 200:
        raise ThreadsError(f"Couldn't list your Threads posts: {r.text[:300]}")
    posts = r.json().get("data", [])
    errors, out = [], []
    for m in posts:
        cr = requests.get(f"{GRAPH_BASE}/{m['id']}/conversation", params={
            "fields": "id,text,username,timestamp,hide_status,like_count,replied_to,permalink", "limit": per_media * 2, "access_token": token}, timeout=30)
        if cr.status_code != 200:
            low = cr.text.lower()
            if "permission" in low or "(#10)" in cr.text or "(#200)" in cr.text or "threads_read_replies" in low:
                raise ThreadsError("Threads needs the replies permissions: reconnect Threads on the Accounts page, then try again. (" + cr.text[:160] + ")")
            errors.append(f"HTTP {cr.status_code}: {cr.text[:200]}")
            continue
        flat = cr.json().get("data", [])
        by_parent = {}
        for x in flat:
            by_parent.setdefault((x.get("replied_to") or {}).get("id") or m["id"], []).append(x)
        for c in by_parent.get(m["id"], []):
            out.append({"id": c["id"], "text": c.get("text", ""), "username": c.get("username", ""), "ts": c.get("timestamp"),
                        "hidden": (c.get("hide_status") or "NOT_HUSHED") == "HIDDEN", "likes": c.get("like_count", 0),
                        "replies": [{"id": x.get("id"), "text": x.get("text", ""), "username": x.get("username", ""), "ts": x.get("timestamp")}
                                    for x in by_parent.get(c["id"], [])],
                        "media_caption": (m.get("text") or "")[:140], "permalink": c.get("permalink") or m.get("permalink")})
    out.sort(key=lambda c: c.get("ts") or "", reverse=True)
    if diag is not None:
        diag.update({"posts_scanned": len(posts), "errors": errors[:3]})
    return out


def reply_comment(comment_id: str, text: str) -> dict:
    token, uid = _auth()
    c = requests.post(f"{GRAPH_BASE}/{uid}/threads", data={"media_type": "TEXT", "text": text, "reply_to_id": comment_id, "access_token": token}, timeout=30)
    if c.status_code != 200:
        raise ThreadsError(f"Couldn't create the reply: {c.text[:300]}")
    time.sleep(3)
    p = requests.post(f"{GRAPH_BASE}/{uid}/threads_publish", data={"creation_id": c.json().get("id"), "access_token": token}, timeout=30)
    if p.status_code != 200:
        raise ThreadsError(f"Couldn't post the reply: {p.text[:300]}")
    return p.json()


def hide_comment(comment_id: str, hide: bool = True) -> None:
    token, _ = _auth()
    r = requests.post(f"{GRAPH_BASE}/{comment_id}/manage_reply", data={"hide": "true" if hide else "false", "access_token": token}, timeout=30)
    if r.status_code != 200:
        raise ThreadsError(f"Couldn't {'hide' if hide else 'unhide'} the reply: {r.text[:300]}")
