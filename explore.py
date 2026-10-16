"""Explore: finds clips on its own and queues them for the owner to approve.

Built for a centre-right, culture-war / politics niche. Every source runs on
its own (one failing never stops the rest) and the last scan reports which
ones answered. Nothing here needs a platform account:
  news      -- politics RSS from a spread of outlets (Telegraph, Spectator, GB News,
               Fox, NY Post, BBC, Sky ...) plus Google News searches. Headlines are
               clustered into stories; a story many outlets run is "buzzing", and
               each buzzing story is searched on YouTube for video of it.
  channels  -- newest uploads from a list of YouTube channels you trust
  keywords  -- your niche search terms searched on YouTube
  reddit    -- hot video posts from political / culture subreddits
  (Rumble's public list is behind Cloudflare and refuses servers; add Rumble channels under 'other accounts' instead.)
  handles   -- any TikTok / X / Instagram / Rumble / YouTube accounts you add
               (best effort: those platforms often block servers)
Everything is editable from the Explore page (stored in the database)."""
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
from concurrent.futures import ThreadPoolExecutor

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


# ---------------------------------------------------------------- config (editable from the Explore page)

DEFAULT_CFG = {
    "keywords": ["woke", "DEI", "free speech", "cancel culture", "illegal migrants", "small boats", "asylum hotels", "trans athletes",
                 "gender ideology", "Starmer", "Reform UK Farage", "net zero", "grooming gangs", "two-tier policing", "mass migration",
                 "Trump", "border crisis", "campus protests", "BBC bias", "mainstream media", "masculinity", "migrant crime"],
    "channels": ["@GBNEWS", "@FoxNews", "@DailyWire", "@PragerU", "@TuckerCarlson", "@TheBlaze", "@spikedonline", "@UnHerd",
                 "@piersmorganuncensored", "@triggerpod", "@SkyNews", "@RebelNewsOnline"],
    "subreddits": ["Conservative", "ukpolitics", "PublicFreakout", "Libertarian", "walkaway", "TikTokCringe", "AskUK"],
    "handles": [],            # full URLs: https://www.tiktok.com/@name, https://x.com/name, https://rumble.com/c/name ...
    "outlets": [],            # extra RSS feed URLs
    "push_min_score": 35,
}

NEWS_FEEDS = [
    "https://www.telegraph.co.uk/politics/rss.xml", "https://www.spectator.co.uk/feed", "https://www.spiked-online.com/feed/",
    "https://unherd.com/feed/", "https://order-order.com/feed/", "https://www.gbnews.com/feeds/politics.rss",
    "https://www.dailymail.co.uk/news/index.rss", "https://www.thesun.co.uk/news/politics/feed/", "https://nypost.com/politics/feed/",
    "https://moxie.foxnews.com/google-publisher/politics.xml", "https://www.washingtonexaminer.com/feed", "https://www.dailywire.com/feeds/rss.xml",
    "https://feeds.bbci.co.uk/news/politics/rss.xml", "https://feeds.skynews.com/feeds/rss/politics.xml",
]

# Words that make a title count as "in the niche". Matching is on whole words / phrases, lowercase.
NICHE_WORDS = [
    "woke", "dei", "diversity", "equity", "inclusion", "transgender", "trans ", "gender", "biological sex", "women's sport", "free speech", "censorship",
    "cancel", "cancelled", "migrant", "migrants", "immigration", "illegal", "asylum", "small boats", "deport", "border", "channel crossing", "illegals",
    "islam", "islamist", "grooming gang", "two-tier", "police", "crime", "knife", "starmer", "labour", "reform uk", "farage", "tory", "tories", "badenoch",
    "conservative", "republican", "democrat", "trump", "biden", "harris", "vance", "left-wing", "leftist", "liberal", "progressive", "marxist", "socialis",
    "net zero", "climate", "bbc", "mainstream media", "msm", "fact check", "propaganda", "protest", "antisemit", "palestin", "hamas", "israel",
    "feminis", "masculin", "men ", "family", "birth rate", "tax", "welfare", "benefits", "nhs", "university", "campus", "teacher", "school",
    "mayor", "sadiq", "khan", "supreme court", "judge", "arrest", "jail", "prison", "free speech", "online safety", "lucy connolly", "tommy robinson",
]
STOP = set("the a an and or of to in on for with at by from is are was were be been it its this that these those as but not no yes after over about into out up "
           "says said say will would could should may might has have had having more most than then just also new news latest live video watch how why what who "
           "when where their they them his her him she he you your our we us can cant wont dont".split())


def get_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        raw = _kv_get("explore_cfg")
        if raw:
            saved = json.loads(raw)
            for k in cfg:
                if k in saved and saved[k] not in (None, ""):
                    cfg[k] = saved[k]
    except Exception:
        pass
    # env stays as an optional override for the lists
    for env, key in (("EXPLORE_KEYWORDS", "keywords"), ("EXPLORE_SUBREDDITS", "subreddits")):
        if os.environ.get(env):
            cfg[key] = [x.strip() for x in os.environ[env].split(",") if x.strip()]
    return cfg


def save_cfg(new):
    cfg = {}
    for k in ("keywords", "channels", "subreddits", "handles", "outlets"):
        v = new.get(k)
        if isinstance(v, str):
            v = [x.strip() for x in re.split(r"[\n,]", v) if x.strip()]
        cfg[k] = [str(x).strip()[:200] for x in (v or [])][:60]
    try:
        cfg["push_min_score"] = max(0, min(100, int(new.get("push_min_score", 35))))
    except Exception:
        cfg["push_min_score"] = 35
    _kv_set("explore_cfg", json.dumps(cfg))
    return get_cfg()


def _kv_get(key):
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("SELECT value FROM kv WHERE key=%s", (key,))
        r = cur.fetchone()
    return r[0] if r else None


def _kv_set(key, value):
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("INSERT INTO kv (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))


def last_scan():
    if LAST_SCAN.get("at") or LAST_SCAN.get("running"):
        return LAST_SCAN
    try:
        raw = _kv_get("explore_last_scan")
        if raw:
            return {**json.loads(raw), "running": False}
    except Exception:
        pass
    return LAST_SCAN


# ---------------------------------------------------------------- helpers

def _mk_id(source, ext_id):
    return hashlib.sha1(f"{source}:{ext_id}".encode()).hexdigest()[:20]


def _log_score(n, scale):
    """0..1 on a log curve so a million views doesn't drown a hundred thousand."""
    return min(1.0, math.log10(max(1, n or 0)) / scale)


def _get(url, **kw):
    return requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9", **kw.pop("headers", {})}, timeout=15, **kw)


_NICHE_RX = None


def relevance(text):
    """How many distinct niche words/phrases a title hits (0..n). Short words must match whole; longer ones match as prefixes."""
    global _NICHE_RX
    if _NICHE_RX is None:
        _NICHE_RX = [re.compile(r"\b" + re.escape(w.strip()) + (r"\b" if len(w.strip()) <= 5 else "")) for w in dict.fromkeys(NICHE_WORDS)]
    t = re.sub(r"[^a-z0-9' \-]+", " ", (text or "").lower())
    return sum(1 for rx in _NICHE_RX if rx.search(t))


def _tokens(text):
    return {w for w in re.findall(r"[a-z][a-z'\-]{3,}", (text or "").lower()) if w not in STOP}


def _yt_search(query, n=6):
    """Newest-first search; if this yt-dlp build doesn't know ytsearchdate, falls back to normal relevance search."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "socket_timeout": 15}
    last = None
    for key in ("ytsearchdate", "ytsearch"):
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(f"{key}{n}:{query}", download=False)
            return (info or {}).get("entries") or []
        except Exception as e:
            last = e
            if "Unsupported url" not in str(e) and "scheme" not in str(e):
                break
    raise last


def _flat(url, n=8):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "socket_timeout": 15, "playlistend": n}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    return (info or {}).get("entries") or ([info] if info else [])


def _yt_item(e, topic, source, base=0.0, extra_stats=None, need_niche=True):
    """Turns a yt-dlp flat entry into a candidate, or None if it's too long / off-niche."""
    vid = e.get("id")
    if not vid or e.get("_type") == "playlist":
        return None
    dur = e.get("duration")
    if dur and dur > 600:
        return None
    title = e.get("title") or ""
    rel = relevance(title + " " + topic)
    if need_niche and rel == 0 and base < 10:
        return None
    views = e.get("view_count") or 0
    link = e.get("webpage_url") or e.get("url") or ""
    if not link.startswith("http"):
        link = f"https://www.youtube.com/watch?v={vid}"
    is_yt = "youtube.com" in link or "youtu.be" in link
    thumbs = e.get("thumbnails") or []
    thumb = (thumbs[-1].get("url") if thumbs else None) or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if is_yt else None)
    stats = {"views": views, "channel": e.get("channel") or e.get("uploader")}
    stats.update(extra_stats or {})
    return {
        "source": source, "ext_id": str(vid), "url": link, "title": title, "thumb": thumb, "embed": "youtube" if is_yt else None,
        "duration": dur, "topic": topic, "stats": stats,
        "_raw": 100 * (0.40 * _log_score(views, 6.3) + 0.30 * min(rel, 3) / 3) + base,
    }


def _parallel(fn, items, workers=5):
    out, errs = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fn, i) for i in items]
        for f in futs:
            try:
                out.extend(f.result() or [])
            except Exception as e:
                errs.append(str(e)[:100])
    return out, errs


# ---------------------------------------------------------------- sources: each takes cfg, returns candidate dicts

def _parse_feed(content):
    root = ET.fromstring(content)
    out = []
    for it in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
        title = (it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        if title:
            out.append(title)
    return out


def news_stories(cfg):
    """Returns ([{title, outlets, tokens}], errors) -- headlines clustered into stories, biggest first."""
    feeds = NEWS_FEEDS + list(cfg.get("outlets") or [])
    for kw in cfg["keywords"][:8]:
        feeds.append("https://news.google.com/rss/search?q=" + requests.utils.quote(kw + " when:1d") + "&hl=en-GB&gl=GB&ceid=GB:en")

    def one(u):
        r = _get(u)
        if r.status_code != 200:
            raise RuntimeError(f"{u.split('/')[2]} HTTP {r.status_code}")
        return [(u.split("/")[2], t) for t in _parse_feed(r.content)[:25]]

    heads, errs = _parallel(one, feeds, 8)
    clusters = []
    for host, title in heads:
        tk = _tokens(title)
        if len(tk) < 3:
            continue
        for c in clusters:
            if len(tk & c["tokens"]) >= 3 or len(tk & c["tokens"]) / max(1, len(tk | c["tokens"])) >= 0.4:
                c["outlets"].add(host); c["titles"].append(title); c["tokens"] |= tk
                break
        else:
            clusters.append({"title": title, "titles": [title], "outlets": {host}, "tokens": set(tk)})
    for c in clusters:
        c["rel"] = relevance(" ".join(c["titles"]))
        c["n"] = len(c["outlets"])
    return clusters, errs, len(heads)


def src_news(cfg):
    clusters, errs, nheads = news_stories(cfg)
    if not nheads:
        raise RuntimeError("no news feeds answered: " + "; ".join(errs[:3]))
    clusters = [c for c in clusters if c["rel"] > 0 or c["n"] >= 3]
    clusters.sort(key=lambda c: (c["n"] * 10 + c["rel"] * 3), reverse=True)

    def one(c):
        # Search with the most distinctive shared words, not the full headline.
        freq = {}
        for t in c["titles"]:
            for w in _tokens(t):
                freq[w] = freq.get(w, 0) + 1
        words = [w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:5]]
        q = " ".join(words)
        res = []
        for e in _yt_search(q, 4):
            it = _yt_item(e, c["title"][:90], "news", base=min(35, c["n"] * 7), extra_stats={"outlets": c["n"], "story": c["title"][:120]}, need_niche=False)
            if it:
                res.append(it)
        return res

    out, e2 = _parallel(one, clusters[:12], 4)
    if not out and e2:
        raise RuntimeError("YouTube search failed: " + e2[0])
    return out


def src_channels(cfg):
    chans = cfg["channels"][:30]
    if not chans:
        raise RuntimeError("no channels configured")

    def one(ch):
        url = ch if ch.startswith("http") else "https://www.youtube.com/" + ch.lstrip("/")
        res = []
        for suffix in ("/shorts", "/videos"):
            try:
                for e in _flat(url.rstrip("/") + suffix, 6):
                    it = _yt_item(e, ch, "channels", base=12, need_niche=False)
                    if it:
                        res.append(it)
            except Exception:
                pass
        if not res:
            raise RuntimeError(f"{ch}: nothing readable")
        return res

    out, errs = _parallel(one, chans, 5)
    if not out:
        raise RuntimeError("; ".join(errs[:3]) or "nothing")
    return out


def src_keywords(cfg):
    kws = cfg["keywords"][:14]

    def one(k):
        return [i for i in (_yt_item(e, k, "keywords", base=6) for e in _yt_search(k, 6)) if i]

    out, errs = _parallel(one, kws, 4)
    if not out and errs:
        raise RuntimeError(errs[0])
    return out


def src_reddit(cfg):
    subs = cfg["subreddits"][:12]
    out, last, errors = [], "", 0
    now = time.time()
    for sub in subs:
        try:
            r = None
            for host in ("www.reddit.com", "old.reddit.com", "api.reddit.com"):
                u = f"https://{host}/r/{sub}/hot.json?limit=40&raw_json=1" if host != "api.reddit.com" else f"https://api.reddit.com/r/{sub}/hot?limit=40&raw_json=1"
                try:
                    r = _get(u)
                except Exception as e:
                    last = str(e)[:80]; continue
                if r.status_code == 200:
                    break
                last = f"HTTP {r.status_code}"
            if r is None or r.status_code != 200:
                errors += 1; continue
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
                if age_h > 48 or ups < 150:
                    continue
                velocity = ups / (age_h ** 0.8)
                thumb = d.get("thumbnail") if str(d.get("thumbnail", "")).startswith("http") else None
                try:
                    thumb = (d.get("preview", {}).get("images", [{}])[0].get("source", {}).get("url")) or thumb
                except Exception:
                    pass
                title = d.get("title") or ""
                rel = relevance(title + " " + sub)
                out.append({
                    "source": "reddit", "ext_id": d["id"], "url": "https://www.reddit.com" + d["permalink"], "title": title, "thumb": thumb,
                    "preview_url": media.get("fallback_url"), "embed": "video", "duration": dur, "topic": f"r/{sub}",
                    "stats": {"upvotes": ups, "comments": d.get("num_comments"), "age_hours": round(age_h, 1), "per_hour": int(velocity)},
                    "_raw": 100 * (0.30 * _log_score(velocity, 3.8) + 0.12 * _log_score(ups, 5) + 0.30 * min(rel, 3) / 3) + 6,
                })
        except Exception as e:
            errors += 1; last = str(e)[:100]
    if subs and errors == len(subs):
        raise RuntimeError(f"Reddit refused every request ({last})")
    return out


def src_rumble(cfg):
    r = _get("https://rumble.com/videos?sort=views&date=today")
    if r.status_code != 200:
        raise RuntimeError(f"Rumble HTTP {r.status_code}")
    out, seen = [], set()
    for m in re.finditer(r'href="(/v[a-z0-9]+-[^"]+\.html)"[^>]*>(.*?)</a>', r.text, re.S):
        href, inner = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        title = re.sub(r"\s+", " ", inner).strip()
        if href in seen or len(title) < 12:
            continue
        seen.add(href)
        rel = relevance(title)
        if rel == 0:
            continue
        out.append({"source": "rumble", "ext_id": href, "url": "https://rumble.com" + href, "title": title[:200], "thumb": None, "embed": None,
                    "duration": None, "topic": "Rumble top today", "stats": {}, "_raw": 100 * 0.35 * min(rel, 3) / 3 + 20})
        if len(out) >= 15:
            break
    if not out:
        raise RuntimeError("page loaded but no on-niche videos found")
    return out


def src_handles(cfg):
    hs = cfg["handles"][:25]
    if not hs:
        raise RuntimeError("no accounts added (optional)")

    def one(h):
        url = h if h.startswith("http") else "https://www.tiktok.com/" + h.lstrip("/")
        res = []
        for e in _flat(url, 8):
            e = dict(e)
            e.setdefault("webpage_url", e.get("url"))
            it = _yt_item(e, h.replace("https://", "")[:60], "handles", base=14, need_niche=False)
            if it:
                it["embed"] = it["embed"]
                res.append(it)
        if not res:
            raise RuntimeError(f"{h}: nothing readable")
        return res

    out, errs = _parallel(one, hs, 4)
    if not out:
        raise RuntimeError("; ".join(errs[:2]))
    return out


SOURCES = {"news": src_news, "channels": src_channels, "keywords": src_keywords, "reddit": src_reddit, "handles": src_handles}


# ---------------------------------------------------------------- scan

def scan():
    """Runs every source in parallel, scores, dedupes against everything seen before, stores the new ones."""
    if not _SCAN_LOCK.acquire(blocking=False):
        return LAST_SCAN
    LAST_SCAN["running"] = True
    try:
        cfg = get_cfg()
        results, status = {}, {}

        def _run(name, fn):
            t0 = time.time()
            try:
                results[name] = fn(cfg)
                status[name] = {"ok": True, "found": len(results[name]), "seconds": round(time.time() - t0, 1)}
            except Exception as e:
                results[name] = []
                status[name] = {"ok": False, "error": str(e)[:200]}

        threads = [threading.Thread(target=_run, args=(n, f), daemon=True) for n, f in SOURCES.items()]
        for t in threads:
            t.start()
        deadline = time.time() + int(os.environ.get("EXPLORE_MAX_SECONDS", "240"))
        for t in threads:
            t.join(max(1, deadline - time.time()))
        for n in SOURCES:
            if n not in status:
                status[n] = {"ok": False, "error": "timed out"}

        known_ids, known_urls = _known_ids_and_urls()
        seen_titles = set()
        added = 0
        cands = [c for name in list(results) for c in list(results[name])]
        cands.sort(key=lambda c: c.get("_raw", 0), reverse=True)
        floor = 15
        kept_below = 0
        for c in cands:
            iid = _mk_id(c["source"], c["ext_id"])
            norm = re.sub(r"\W+", " ", (c["title"] or "").lower()).strip()[:60]
            dup_title = bool(norm) and norm in seen_titles
            if norm:
                seen_titles.add(norm)
            if iid in known_ids or c["url"] in known_urls or dup_title:
                continue
            c["id"] = iid
            c["score"] = round(max(0.0, min(100.0, c.get("_raw", 0))), 1)
            if c["score"] < floor:
                continue
            _insert(c)
            added += 1
            if added >= 60:
                break
        try:
            prune()
        except Exception:
            pass
        LAST_SCAN.update({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "sources": status, "added": added,
                          "candidates": len(cands)})
        try:
            _kv_set("explore_last_scan", json.dumps({k: LAST_SCAN[k] for k in ("at", "sources", "added", "candidates")}))
        except Exception:
            pass
        return LAST_SCAN
    finally:
        LAST_SCAN["running"] = False
        _SCAN_LOCK.release()
