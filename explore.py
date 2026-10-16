"""Explore: finds clips on its own and queues them for the owner to approve.

Sources (all work without extra accounts; each fails on its own without
affecting the others, and the last scan reports which ones answered):
  reddit   -- hot video posts from a list of subreddits (public JSON)
  trends   -- Google Trends "trending now" topics, each searched on YouTube
  youtube  -- YouTube's most-popular chart (only if YOUTUBE_API_KEY is set)
  keywords -- the owner's own search terms searched on YouTube (EXPLORE_KEYWORDS)

TikTok and Instagram have no public trending feed that a server can read, so
they are deliberately not scraped here.

Env (all optional): EXPLORE_SUBREDDITS, EXPLORE_KEYWORDS, EXPLORE_GEO (GB),
EXPLORE_MAX_SECONDS (300), YOUTUBE_API_KEY."""
import os
import re
import json
import math
import time
import hmac
import uuid
import hashlib
import threading
import datetime
import xml.etree.ElementTree as ET

import requests

import db

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DEFAULT_SUBS = "PublicFreakout,nextfuckinglevel,funny,Unexpected,interestingasfuck,TikTokCringe,oddlysatisfying,Damnthatsinteresting"

LAST_SCAN = {"at": None, "sources": {}, "added": 0, "running": False}
_SCAN_LOCK = threading.Lock()


# ---------------------------------------------------------------- storage

def _init(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS explore_items (
        id TEXT PRIMARY KEY, source TEXT, ext_id TEXT, url TEXT NOT NULL, title TEXT, thumb TEXT,
        preview_url TEXT, embed TEXT, duration REAL, stats JSONB, score REAL DEFAULT 0, topic TEXT,
        status TEXT DEFAULT 'new', notified BOOLEAN DEFAULT FALSE, job_id TEXT,
        created_at TIMESTAMPTZ DEFAULT now(), decided_at TIMESTAMPTZ)""")


def _row(r):
    r = dict(r)
    for k in ("created_at", "decided_at"):
        if r.get(k):
            r[k] = r[k].isoformat()
    return r


def list_items(status="new", limit=60):
    import psycopg2.extras
    with db._conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _init(cur)
        if status == "all":
            cur.execute("SELECT * FROM explore_items ORDER BY created_at DESC LIMIT %s", (limit,))
        else:
            cur.execute("SELECT * FROM explore_items WHERE status = %s ORDER BY score DESC, created_at DESC LIMIT %s", (status, limit))
        return [_row(r) for r in cur.fetchall()]


def get_item(iid):
    import psycopg2.extras
    with db._conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _init(cur)
        cur.execute("SELECT * FROM explore_items WHERE id = %s", (iid,))
        r = cur.fetchone()
        return _row(r) if r else None


def set_status(iid, status, job_id=None):
    with db._conn() as conn:
        cur = conn.cursor()
        _init(cur)
        cur.execute("UPDATE explore_items SET status=%s, job_id=COALESCE(%s, job_id), decided_at=now() WHERE id=%s", (status, job_id, iid))


def mark_notified(ids):
    if not ids:
        return
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE explore_items SET notified=TRUE WHERE id = ANY(%s)", (list(ids),))


def unnotified(limit=3, min_score=0):
    import psycopg2.extras
    with db._conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _init(cur)
        cur.execute("SELECT * FROM explore_items WHERE status='new' AND notified=FALSE AND score >= %s "
                    "ORDER BY score DESC LIMIT %s", (min_score, limit))
        return [_row(r) for r in cur.fetchall()]


def _known_ids_and_urls():
    with db._conn() as conn:
        cur = conn.cursor()
        _init(cur)
        cur.execute("SELECT id, url FROM explore_items")
        rows = cur.fetchall()
    return {r[0] for r in rows}, {r[1] for r in rows}


def _insert(it):
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO explore_items (id, source, ext_id, url, title, thumb, preview_url, embed, duration, stats, score, topic)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                    (it["id"], it["source"], it["ext_id"], it["url"], it["title"], it.get("thumb"), it.get("preview_url"),
                     it.get("embed"), it.get("duration"), json.dumps(it.get("stats") or {}), it["score"], it.get("topic")))


def prune(days=30):
    """Old undecided/skipped suggestions don't pile up forever."""
    with db._conn() as conn:
        cur = conn.cursor()
        _init(cur)
        cur.execute("DELETE FROM explore_items WHERE created_at < now() - (%s || ' days')::interval AND status <> 'approved'", (str(days),))


# ---------------------------------------------------------------- tokens (for phone buttons, which carry no login cookie)

def _secret():
    return (os.environ.get("SESSION_SECRET") or os.environ.get("CRON_SECRET") or "").encode()


def token(iid, action):
    return hmac.new(_secret(), f"explore:{iid}:{action}".encode(), hashlib.sha256).hexdigest()[:32]


def token_ok(iid, action, given):
    s = _secret()
    return bool(s) and hmac.compare_digest(token(iid, action), given or "")


# ---------------------------------------------------------------- helpers

def _mk_id(source, ext_id):
    return hashlib.sha1(f"{source}:{ext_id}".encode()).hexdigest()[:20]


def _log_score(n, scale):
    """0..~1 using a log curve so a million views doesn't drown a hundred thousand."""
    return min(1.0, math.log10(max(1, n)) / scale)


def _boost(title):
    words = [w.strip().lower() for w in (os.environ.get("EXPLORE_BOOST_WORDS") or "").split(",") if w.strip()]
    t = (title or "").lower()
    return 8 if any(w in t for w in words) else 0


def _get(url, **kw):
    return requests.get(url, headers={"User-Agent": UA, **kw.pop("headers", {})}, timeout=15, **kw)


# ---------------------------------------------------------------- sources: each returns a list of candidate dicts

def src_reddit():
    subs = [s.strip() for s in (os.environ.get("EXPLORE_SUBREDDITS") or DEFAULT_SUBS).split(",") if s.strip()]
    out = []
    errors = 0
    last = ""
    for sub in subs[:12]:
        try:
            r = _get(f"https://www.reddit.com/r/{sub}/hot.json?limit=30&raw_json=1")
            if r.status_code != 200:
                errors += 1
                last = f"HTTP {r.status_code}"
                continue
            now = time.time()
            for c in r.json().get("data", {}).get("children", []):
                d = c.get("data") or {}
                if d.get("stickied") or d.get("over_18"):
                    continue
                media = (d.get("secure_media") or d.get("media") or {}).get("reddit_video") or {}
                if not (d.get("is_video") and media):
                    continue
                dur = media.get("duration")
                if dur and dur > 300:
                    continue
                age_h = max(0.5, (now - (d.get("created_utc") or now)) / 3600)
                ups = d.get("score") or 0
                if age_h > 48 or ups < 500:
                    continue
                velocity = ups / (age_h ** 0.8)
                thumb = d.get("thumbnail") if str(d.get("thumbnail", "")).startswith("http") else None
                try:
                    thumb = (d.get("preview", {}).get("images", [{}])[0].get("source", {}).get("url")) or thumb
                except Exception:
                    pass
                out.append({
                    "source": "reddit", "ext_id": d["id"], "url": "https://www.reddit.com" + d["permalink"],
                    "title": d.get("title") or "", "thumb": thumb, "preview_url": media.get("fallback_url"),
                    "embed": "video", "duration": dur, "topic": f"r/{sub}",
                    "stats": {"upvotes": ups, "comments": d.get("num_comments"), "age_hours": round(age_h, 1), "per_hour": int(velocity)},
                    "_raw": 100 * (0.6 * _log_score(velocity, 4) + 0.4 * _log_score(ups, 5.5)),
                })
        except Exception as e:
            errors += 1
            last = str(e)[:120]
    if errors == len(subs[:12]) and subs:
        raise RuntimeError(f"Reddit refused every request ({last})")
    return out


def _trend_topics(geo):
    r = _get(f"https://trends.google.com/trending/rss?geo={geo}")
    if r.status_code != 200:
        raise RuntimeError(f"Google Trends HTTP {r.status_code}")
    root = ET.fromstring(r.content)
    topics = []
    for item in root.iter("item"):
        t = (item.findtext("title") or "").strip()
        traffic = ""
        for ch in item:
            if ch.tag.endswith("approx_traffic"):
                traffic = (ch.text or "").strip()
        if t:
            topics.append((t, traffic))
    return topics


def _yt_search(query, n=6):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "socket_timeout": 15}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearchdate{n}:{query}", download=False)
    return (info or {}).get("entries") or []


def _yt_item(e, topic, source, boost=0.0):
    vid = e.get("id")
    if not vid:
        return None
    dur = e.get("duration")
    if dur and dur > 300:
        return None
    views = e.get("view_count") or 0
    thumbs = e.get("thumbnails") or []
    thumb = (thumbs[-1].get("url") if thumbs else None) or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return {
        "source": source, "ext_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
        "title": e.get("title") or "", "thumb": thumb, "embed": "youtube", "duration": dur, "topic": topic,
        "stats": {"views": views, "channel": e.get("channel") or e.get("uploader")},
        "_raw": 100 * (0.7 * _log_score(views, 6.5)) + boost,
    }


def src_trends():
    geo = (os.environ.get("EXPLORE_GEO") or "GB").upper()
    topics = _trend_topics(geo)[:10]
    out = []
    for i, (t, traffic) in enumerate(topics):
        try:
            for e in _yt_search(f"{t} shorts", 5):
                it = _yt_item(e, t, "trends", boost=max(0, 30 - i * 3))
                if it:
                    it["stats"]["trend_traffic"] = traffic
                    out.append(it)
        except Exception as e:
            print(f"EXPLORE trends search failed for {t}: {e}", flush=True)
    return out


def src_keywords():
    kws = [k.strip() for k in (os.environ.get("EXPLORE_KEYWORDS") or "").split(",") if k.strip()]
    if not kws:
        raise RuntimeError("No EXPLORE_KEYWORDS set (optional)")
    out = []
    for k in kws[:8]:
        try:
            for e in _yt_search(k, 6):
                it = _yt_item(e, k, "keywords", boost=10)
                if it:
                    out.append(it)
        except Exception as ex:
            print(f"EXPLORE keyword search failed for {k}: {ex}", flush=True)
    return out


def src_youtube():
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("No YOUTUBE_API_KEY set (optional)")
    geo = (os.environ.get("EXPLORE_GEO") or "GB").upper()
    r = _get("https://www.googleapis.com/youtube/v3/videos", params={
        "part": "snippet,statistics,contentDetails", "chart": "mostPopular", "regionCode": geo, "maxResults": 40, "key": key})
    if r.status_code != 200:
        raise RuntimeError(f"YouTube API HTTP {r.status_code}")
    out = []
    for v in r.json().get("items", []):
        m = re.match(r"PT(?:(\d+)M)?(?:(\d+)S)?$", v["contentDetails"]["duration"])
        dur = (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) if m else None
        if dur is None or dur > 300:
            continue
        sn, st = v["snippet"], v.get("statistics", {})
        views = int(st.get("viewCount") or 0)
        out.append({
            "source": "youtube", "ext_id": v["id"], "url": f"https://www.youtube.com/watch?v={v['id']}", "title": sn["title"],
            "thumb": (sn.get("thumbnails", {}).get("high") or {}).get("url"), "embed": "youtube", "duration": dur,
            "topic": "YouTube trending", "stats": {"views": views, "likes": int(st.get("likeCount") or 0), "channel": sn.get("channelTitle")},
            "_raw": 100 * 0.7 * _log_score(views, 6.5) + 15,
        })
    return out


SOURCES = {"reddit": src_reddit, "trends": src_trends, "youtube": src_youtube, "keywords": src_keywords}


# ---------------------------------------------------------------- scan

def scan():
    """Runs every source in parallel, scores, dedupes against everything seen before, stores the new ones."""
    if not _SCAN_LOCK.acquire(blocking=False):
        return LAST_SCAN
    LAST_SCAN["running"] = True
    try:
        results, status = {}, {}

        def _run(name, fn):
            t0 = time.time()
            try:
                results[name] = fn()
                status[name] = {"ok": True, "found": len(results[name]), "seconds": round(time.time() - t0, 1)}
            except Exception as e:
                results[name] = []
                status[name] = {"ok": False, "error": str(e)[:160]}

        threads = [threading.Thread(target=_run, args=(n, f), daemon=True) for n, f in SOURCES.items()]
        for t in threads:
            t.start()
        deadline = time.time() + int(os.environ.get("EXPLORE_MAX_SECONDS", "300"))
        for t in threads:
            t.join(max(1, deadline - time.time()))

        known_ids, known_urls = _known_ids_and_urls()
        seen_titles = set()
        added = 0
        cands = [c for name in results for c in results[name]]
        cands.sort(key=lambda c: c.get("_raw", 0), reverse=True)
        for c in cands:
            iid = _mk_id(c["source"], c["ext_id"])
            norm = re.sub(r"\W+", " ", (c["title"] or "").lower()).strip()[:60]
            dup_title = bool(norm) and norm in seen_titles
            if norm:
                seen_titles.add(norm)
            if iid in known_ids or c["url"] in known_urls or dup_title:
                continue
            c["id"] = iid
            c["score"] = round(min(100.0, c.get("_raw", 0) + _boost(c["title"])), 1)
            if c["score"] < 20:
                continue
            _insert(c)
            added += 1
        try:
            prune()
        except Exception:
            pass
        LAST_SCAN.update({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "sources": status, "added": added})
        return LAST_SCAN
    finally:
        LAST_SCAN["running"] = False
        _SCAN_LOCK.release()
