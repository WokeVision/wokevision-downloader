"""Read-only stats for the Dashboards page, normalised across platforms.

Every fetcher returns the same shape so the UI can compare or combine them:

  {
    "platform": "instagram", "label": "Instagram",
    "state": "ok" | "not_connected" | "limited" | "error",
    "note": str | None,           # shown under the platform name
    "account": str | None,        # @handle / channel / page name
    "totals": {"followers": int|None, "posts": int|None, "views": int|None,
               "likes": int|None, "comments": int|None, "shares": int|None},
    "posts": [{"id","title","url","thumb","ts","views","likes","comments","shares"}],
  }

A metric a platform doesn't hand us with the permissions currently granted
is None (the UI shows "n/a" rather than a misleading 0). "limited" means the
connection works but some numbers need an extra permission -- `note` says
which. Nothing here ever writes to a platform.
"""
import time
import datetime
import threading

import requests

import db

LABELS = {
    "instagram": "Instagram", "threads": "Threads", "youtube": "YouTube",
    "tiktok": "TikTok", "x": "X", "facebook": "Facebook",
}
ORDER = ["instagram", "threads", "youtube", "tiktok", "x", "facebook"]
_TTL = 600
_CACHE = {}
_LOCK = threading.Lock()


def _empty(platform, state, note=None, account=None):
    return {
        "platform": platform, "label": LABELS[platform], "state": state,
        "note": note, "account": account,
        "totals": {k: None for k in ("followers", "posts", "views", "likes", "comments", "shares")},
        "posts": [],
    }


def _get(url, **kw):
    kw.setdefault("timeout", 20)
    return requests.get(url, **kw)


def _graph_pages(url, params, max_pages=14):
    """Follows Graph-style `paging.next` links so the dashboard can see a
    whole post history (for the date-range picker), not just the latest page.
    Returns (items, last_response)."""
    items, r, pages = [], _get(url, params=params), 0
    while True:
        if r.status_code != 200:
            return items, r
        j = r.json()
        items += j.get("data", [])
        pages += 1
        nxt = (j.get("paging") or {}).get("next")
        if not nxt or pages >= max_pages:
            return items, r
        r = _get(nxt)


def _sum(posts, key):
    vals = [p[key] for p in posts if p.get(key) is not None]
    return sum(vals) if vals else None


def _conn(platform, module):
    """Returns the decrypted connection after making sure the token is fresh,
    or None if the platform isn't connected."""
    c = db.get_connection(platform)
    if not c or not c.get("access_token"):
        return None
    try:
        module.refresh_if_needed()
        c = db.get_connection(platform) or c
    except Exception:
        pass
    return c


# --- Instagram -------------------------------------------------------------

def _instagram():
    from platforms import instagram as m
    c = _conn("instagram", m)
    if not c:
        return _empty("instagram", "not_connected", "Connect Instagram in the Video Editor to see stats.")
    tok = c["access_token"]
    r = _get(f"{m.GRAPH_BASE}/me", params={
        "fields": "username,followers_count,follows_count,media_count", "access_token": tok})
    if r.status_code != 200:
        return _empty("instagram", "error", r.text[:300])
    me = r.json()
    items, r = _graph_pages(f"{m.GRAPH_BASE}/me/media", {
        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count",
        "limit": 50, "access_token": tok})
    posts = []
    for p in items:
        posts.append({
            "id": p["id"], "title": (p.get("caption") or "")[:140], "caption": p.get("caption") or "", "url": p.get("permalink"),
            "thumb": p.get("thumbnail_url") or p.get("media_url"), "ts": p.get("timestamp"),
            "views": None, "likes": p.get("like_count"), "comments": p.get("comments_count"),
            "shares": None, "type": p.get("media_type"),
        })
    # Per-post views (and shares) for the newest posts -- one small call each,
    # run in parallel; any post that refuses just keeps n/a.
    from concurrent.futures import ThreadPoolExecutor

    def _media_stats(p):
        try:
            rr = _get(f"{m.GRAPH_BASE}/{p['id']}/insights", params={"metric": "views,shares", "access_token": tok}, timeout=15)
            if rr.status_code != 200:
                rr = _get(f"{m.GRAPH_BASE}/{p['id']}/insights", params={"metric": "views", "access_token": tok}, timeout=15)
            if rr.status_code == 200:
                for it in rr.json().get("data", []):
                    v = (it.get("values") or [{}])[0].get("value")
                    if it.get("name") == "views":
                        p["views"] = v
                    elif it.get("name") == "shares":
                        p["shares"] = v
        except Exception:
            pass
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_media_stats, posts[:40]))
    except Exception:
        pass
    views = None
    try:
        views = m.account_insights(30).get("views")
        out_state, out_note = "ok", None
    except Exception:
        out_state, out_note = "limited", "Views need the Instagram insights permission -- reconnect Instagram in the Video Editor to grant it."
    out = _empty("instagram", out_state, out_note, "@" + (me.get("username") or ""))
    out["totals"].update(followers=me.get("followers_count"), posts=me.get("media_count"), views=views,
                         likes=_sum(posts, "likes"), comments=_sum(posts, "comments"))
    out["posts"] = posts
    return out


# --- Threads ---------------------------------------------------------------

def _threads():
    from platforms import threads as m
    c = _conn("threads", m)
    if not c:
        return _empty("threads", "not_connected", "Connect Threads in the Video Editor to see stats.")
    tok = c["access_token"]
    items, r = _graph_pages(f"{m.GRAPH_BASE}/me/threads", {
        "fields": "id,text,permalink,timestamp,media_type,media_url,thumbnail_url", "limit": 50, "access_token": tok})
    if r.status_code != 200 and not items:
        return _empty("threads", "error", r.text[:300])
    posts = [{
        "id": p["id"], "title": (p.get("text") or "")[:140], "url": p.get("permalink"),
        "thumb": p.get("thumbnail_url") or p.get("media_url"), "ts": p.get("timestamp"),
        "views": None, "likes": None, "comments": None, "shares": None,
    } for p in items]
    out = _empty("threads", "ok", None, "@" + (c.get("extra", {}).get("username") or ""))
    # Per-post numbers (needs threads_manage_insights); newest 40 only to keep it quick.
    got_any = False
    for p in posts[:40]:
        ir = _get(f"{m.GRAPH_BASE}/{p['id']}/insights", params={"metric": "views,likes,replies,reposts,quotes", "access_token": tok})
        if ir.status_code != 200:
            continue
        vals = {d["name"]: (d.get("values") or [{}])[0].get("value") for d in ir.json().get("data", [])}
        p.update(views=vals.get("views"), likes=vals.get("likes"), comments=vals.get("replies"),
                 shares=(vals.get("reposts") or 0) + (vals.get("quotes") or 0))
        got_any = True
    followers = None
    ur = _get(f"{m.GRAPH_BASE}/me/threads_insights", params={"metric": "followers_count", "access_token": tok})
    if ur.status_code == 200:
        for d in ur.json().get("data", []):
            if d.get("name") == "followers_count":
                followers = (d.get("total_value") or {}).get("value")
                got_any = True
    if not got_any:
        out["state"] = "limited"
        out["note"] = "Views, likes and followers need the Threads insights permission (reconnect Threads to grant it)."
    elif len(posts) > 40:
        out["note"] = "Per-post numbers cover your newest 40 posts."
    out["totals"].update(posts=len(posts), followers=followers, views=_sum(posts, "views"),
                         likes=_sum(posts, "likes"), comments=_sum(posts, "comments"), shares=_sum(posts, "shares"))
    out["posts"] = posts
    return out


# --- YouTube ---------------------------------------------------------------

def _youtube():
    from platforms import youtube as m
    c = _conn("youtube", m)
    if not c:
        return _empty("youtube", "not_connected", "Connect YouTube in the Video Editor to see stats.")
    h = {"Authorization": f"Bearer {c['access_token']}"}
    r = _get("https://www.googleapis.com/youtube/v3/channels",
             params={"part": "snippet,statistics,contentDetails", "mine": "true"}, headers=h)
    if r.status_code != 200:
        return _empty("youtube", "error", r.text[:300])
    items = r.json().get("items") or []
    if not items:
        return _empty("youtube", "error", "No YouTube channel found.")
    ch = items[0]
    st = ch.get("statistics", {})
    uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    posts = []
    if uploads:
        ids, token = [], None
        for _ in range(14):
            params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
            if token:
                params["pageToken"] = token
            pl = _get("https://www.googleapis.com/youtube/v3/playlistItems", params=params, headers=h)
            if pl.status_code != 200:
                break
            j = pl.json()
            ids += [i["contentDetails"]["videoId"] for i in j.get("items", [])]
            token = j.get("nextPageToken")
            if not token:
                break
        for i in range(0, len(ids), 50):
            vr = _get("https://www.googleapis.com/youtube/v3/videos",
                      params={"part": "snippet,statistics", "id": ",".join(ids[i:i + 50])}, headers=h)
            for v in (vr.json().get("items", []) if vr.status_code == 200 else []):
                s_ = v.get("statistics", {})
                th = v["snippet"].get("thumbnails", {})
                posts.append({
                    "id": v["id"], "title": v["snippet"].get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={v['id']}",
                    "thumb": (th.get("medium") or th.get("default") or {}).get("url"),
                    "ts": v["snippet"].get("publishedAt"),
                    "views": int(s_.get("viewCount", 0)), "likes": int(s_.get("likeCount", 0)),
                    "comments": int(s_.get("commentCount", 0)), "shares": None,
                })
    out = _empty("youtube", "ok", None, ch["snippet"].get("title"))
    out["profile_url"] = f"https://www.youtube.com/channel/{ch.get('id')}"
    out["totals"].update(
        followers=None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0)),
        posts=int(st.get("videoCount", 0)), views=int(st.get("viewCount", 0)),
        likes=_sum(posts, "likes"), comments=_sum(posts, "comments"))
    out["posts"] = posts
    return out


# --- TikTok ----------------------------------------------------------------

def _tiktok():
    from platforms import tiktok as m
    c = _conn("tiktok", m)
    if not c:
        return _empty("tiktok", "not_connected", "Connect TikTok in the Video Editor to see stats.")
    out = _empty("tiktok", "limited",
                 "TikTok only shares follower, view and like counts after its app review is approved and the stats permission is granted.",
                 c.get("extra", {}).get("display_name"))
    return out


# --- X ---------------------------------------------------------------------

def _x():
    from platforms import x as m
    c = _conn("x", m)
    if not c:
        return _empty("x", "not_connected", "Connect X in the Video Editor to see stats.")
    h = {"Authorization": f"Bearer {c['access_token']}"}
    uid = c.get("extra", {}).get("user_id")
    r = _get("https://api.x.com/2/users/me", params={"user.fields": "public_metrics,username"}, headers=h)
    if r.status_code != 200:
        return _empty("x", "error", f"X returned {r.status_code}: {r.text[:200]}")
    me = r.json().get("data", {})
    pm = me.get("public_metrics", {})
    posts = []
    note, token = None, None
    for _ in range(5):
        params = {"max_results": 100, "tweet.fields": "public_metrics,created_at"}
        if token:
            params["pagination_token"] = token
        tr = _get(f"https://api.x.com/2/users/{uid or me.get('id')}/tweets", params=params, headers=h)
        if tr.status_code != 200:
            if not posts:
                note = "Post-level stats weren't available from X with the current plan."
            break
        j = tr.json()
        for t in j.get("data", []):
            p = t.get("public_metrics", {})
            posts.append({
                "id": t["id"], "title": t.get("text", "")[:140],
                "url": f"https://x.com/{me.get('username')}/status/{t['id']}", "thumb": None,
                "ts": t.get("created_at"), "views": p.get("impression_count"),
                "likes": p.get("like_count"), "comments": p.get("reply_count"),
                "shares": (p.get("retweet_count") or 0) + (p.get("quote_count") or 0),
            })
        token = (j.get("meta") or {}).get("next_token")
        if not token:
            break
    out = _empty("x", "ok" if not note else "limited", note, "@" + (me.get("username") or ""))
    out["totals"].update(followers=pm.get("followers_count"), posts=pm.get("tweet_count"),
                         views=_sum(posts, "views"), likes=_sum(posts, "likes"),
                         comments=_sum(posts, "comments"), shares=_sum(posts, "shares"))
    out["posts"] = posts
    return out


# --- Facebook --------------------------------------------------------------

def _facebook():
    from platforms import facebook as m
    c = _conn("facebook", m)
    if not c:
        return _empty("facebook", "not_connected", "Connect Facebook in the Video Editor to see stats.")
    tok, pid = c["access_token"], c.get("extra", {}).get("page_id")
    r = _get(f"{m.GRAPH_BASE}/{pid}", params={"fields": "name,fan_count,followers_count", "access_token": tok})
    if r.status_code != 200:
        return _empty("facebook", "error", r.text[:300])
    pg = r.json()
    posts = []
    items, pr = _graph_pages(f"{m.GRAPH_BASE}/{pid}/posts", {
        "fields": "id,message,permalink_url,created_time,full_picture,shares,"
                  "reactions.summary(true).limit(0),comments.summary(true).limit(0)",
        "limit": 50, "access_token": tok})
    for p in items:
        posts.append({
            "id": p["id"], "title": (p.get("message") or "")[:140], "url": p.get("permalink_url"),
            "thumb": p.get("full_picture"), "ts": p.get("created_time"), "views": None,
            "likes": p.get("reactions", {}).get("summary", {}).get("total_count"),
            "comments": p.get("comments", {}).get("summary", {}).get("total_count"),
            "shares": (p.get("shares") or {}).get("count", 0),
        })
    # Reels / videos don't appear in /posts, so read those edges too.
    seen = {p["id"] for p in posts}
    for edge in ("video_reels", "videos"):
        vitems, _vr = _graph_pages(f"{m.GRAPH_BASE}/{pid}/{edge}", {
            "fields": "id,description,permalink_url,created_time,picture,views,"
                      "likes.summary(true).limit(0),comments.summary(true).limit(0)",
            "limit": 50, "access_token": tok})
        if not vitems and _vr.status_code != 200:
            # Retry without the optional `views` field, which some tokens can't read.
            vitems, _vr = _graph_pages(f"{m.GRAPH_BASE}/{pid}/{edge}", {
                "fields": "id,description,permalink_url,created_time,picture,"
                          "likes.summary(true).limit(0),comments.summary(true).limit(0)",
                "limit": 50, "access_token": tok})
        for v in vitems:
            if v["id"] in seen:
                continue
            seen.add(v["id"])
            url = v.get("permalink_url") or ""
            if url.startswith("/"):
                url = "https://www.facebook.com" + url
            posts.append({
                "id": v["id"], "title": (v.get("description") or "")[:140], "url": url,
                "thumb": v.get("picture"), "ts": v.get("created_time"), "views": v.get("views"),
                "likes": v.get("likes", {}).get("summary", {}).get("total_count"),
                "comments": v.get("comments", {}).get("summary", {}).get("total_count"),
                "shares": None,
            })
    out = _empty("facebook", "ok", None, pg.get("name"))
    out["profile_url"] = f"https://www.facebook.com/{pid}"
    # Page views over the last 28 days (needs read_insights).
    views, now = None, int(time.time())
    vr = _get(f"{m.GRAPH_BASE}/{pid}/insights", params={
        "metric": "page_media_view", "period": "day", "since": now - 28 * 86400, "until": now, "access_token": tok})
    if vr.status_code == 200:
        vals = [v.get("value") or 0 for d in vr.json().get("data", []) for v in d.get("values", [])]
        views = sum(vals) if vals else None
    if views is None:
        out["state"] = "limited"
        out["note"] = "Views need the Page insights permission (reconnect Facebook to grant it)."
    else:
        out["note"] = "Views cover the last 28 days."
    out["totals"].update(followers=pg.get("followers_count", pg.get("fan_count")), posts=len(posts), views=views,
                         likes=_sum(posts, "likes"), comments=_sum(posts, "comments"), shares=_sum(posts, "shares"))
    out["posts"] = posts
    return out


_FETCHERS = {"instagram": _instagram, "threads": _threads, "youtube": _youtube,
             "tiktok": _tiktok, "x": _x, "facebook": _facebook}


def get(platform: str, force: bool = False) -> dict:
    if platform not in _FETCHERS:
        raise KeyError(platform)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(platform)
        if hit and not force and now - hit[0] < _TTL:
            return hit[1]
    try:
        data = _FETCHERS[platform]()
    except Exception as e:
        print(f"INSIGHTS FAILED ({platform}): {e}", flush=True)
        data = _empty(platform, "error", f"Couldn't load stats: {e}"[:300])
    with _LOCK:
        _CACHE[platform] = (now, data)
    try:
        _snapshot(data)
    except Exception as e:
        print(f"INSIGHTS SNAPSHOT FAILED ({platform}): {e}", flush=True)
    return data


# --- follower history ------------------------------------------------------
# Platforms only give today's follower count, so to draw a growth line we
# store one reading per platform per day whenever the dashboard is opened.

def init_snapshots():
    if not db.configured():
        return
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS insight_snapshots (
                    platform TEXT NOT NULL, day DATE NOT NULL,
                    followers BIGINT, posts BIGINT, views BIGINT, likes BIGINT, comments BIGINT,
                    PRIMARY KEY (platform, day)
                )""")


def _snapshot(data):
    if not db.configured() or data["state"] not in ("ok", "limited"):
        return
    t = data["totals"]
    if t.get("followers") is None and t.get("views") is None:
        return
    day = datetime.datetime.now(datetime.timezone.utc).date()
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO insight_snapshots (platform, day, followers, posts, views, likes, comments)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (platform, day) DO UPDATE SET followers=EXCLUDED.followers,
                  posts=EXCLUDED.posts, views=EXCLUDED.views, likes=EXCLUDED.likes, comments=EXCLUDED.comments
            """, (data["platform"], day, t.get("followers"), t.get("posts"), t.get("views"),
                  t.get("likes"), t.get("comments")))


def history(days: int = 90) -> dict:
    if not db.configured():
        return {}
    import psycopg2.extras
    out = {}
    with db._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT platform, day, followers, views FROM insight_snapshots
                           WHERE day >= CURRENT_DATE - %s ORDER BY day""", (days,))
            for r in cur.fetchall():
                out.setdefault(r["platform"], []).append(
                    {"day": r["day"].isoformat(), "followers": r["followers"], "views": r["views"]})
    return out


def range_views(platform: str, start: int, end: int) -> dict:
    """Account-level views for an arbitrary window, for platforms that report
    them (Instagram). Others return views=None and the page falls back to
    summing per-post views. Windows are fetched in <=29-day chunks and capped
    at the most recent ~13 months."""
    if platform != "instagram":
        return {"views": None}
    from platforms import instagram as m
    cap = 13 * 29 * 86400
    clamped = end - start > cap
    start = max(start, end - cap)
    total, cur = 0, start
    while cur < end:
        nxt = min(cur + 29 * 86400, end)
        v = m.views_between(cur, nxt)
        total += v or 0
        cur = nxt
    return {"views": total, "clamped": clamped}
