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

MAX_DUR = 180   # seconds: the longest a clip can be and still post to every platform (YouTube Shorts / Reels / TikTok)
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


def list_items(status="new", limit=200):
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


def purge_unpostable():
    """Drops waiting suggestions that are too long (or have no known length) to post on every platform."""
    with db._conn() as conn:
        cur = conn.cursor()
        _init(cur)
        cur.execute("DELETE FROM explore_items WHERE status='new' AND (duration IS NULL OR duration > %s)", (MAX_DUR,))


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
    "subreddits": ["Conservative", "ukpolitics", "PublicFreakout", "Libertarian", "walkaway", "ConservativeMemes"],
    "telegram": [],           # public Telegram channel names (t.me/s/<name>) -- optional
    "hashtags": [],           # TikTok hashtags (TikTok blocks cloud servers; best effort if you add some)
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
    for k in ("keywords", "channels", "subreddits", "hashtags", "telegram", "handles", "outlets"):
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
    """YouTube search limited to the last week (so old viral videos don't resurface). Tries the filtered results page,
    then newest-first search, then plain search."""
    import yt_dlp
    from urllib.parse import quote
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "socket_timeout": 15, "playlistend": n}
    last = None
    for target in (f"https://www.youtube.com/results?search_query={quote(query)}&sp=EgQIAxAB", f"ytsearchdate{n}:{query}"):
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(target, download=False)
            ents = [e for e in ((info or {}).get("entries") or []) if e]
            if ents:
                return ents[:n]
        except Exception as e:
            last = e
    if last:
        raise last
    return []


def _flat(url, n=8):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "socket_timeout": 15, "playlistend": n}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    return (info or {}).get("entries") or ([info] if info else [])


def _yt_item(e, topic, source, base=0.0, extra_stats=None, need_niche=True, assume_short=False):
    """Turns a yt-dlp flat entry into a candidate, or None if it's too long / off-niche."""
    vid = e.get("id")
    if not vid or e.get("_type") == "playlist":
        return None
    dur = e.get("duration")
    if dur is None and assume_short:
        dur = 60          # Shorts-tab listings don't carry a length, but Shorts are at most 3 minutes
    if not dur or dur > MAX_DUR:
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


_NEWS = {"v": None}
RADAR = {"topics": []}


def src_news(cfg):
    clusters, errs, nheads = _NEWS["v"] or news_stories(cfg)
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



# ---------------------------------------------------------------- trend radar: what is hot right now in the niche

def _niche_text(t):
    return relevance(t) > 0


def radar(cfg):
    """Polls Google Trends, Wikipedia's most-read list, Google search suggestions and the news clusters, keeps only
    what is on-niche, and returns [{topic, strength, via}] strongest first. Used to widen the searches and to boost
    clips that match what is being talked about today."""
    found, status = [], {}
    clusters, _e, nheads = _NEWS["v"] or ([], [], 0)
    news_blob = " ".join(" ".join(c["titles"]) for c in clusters).lower()

    # a) Google Trends (GB + US daily RSS)
    try:
        n = 0
        for geo in ("GB", "US"):
            r = _get(f"https://trends.google.com/trending/rss?geo={geo}")
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                t = (item.findtext("title") or "").strip()
                traffic, newst = "", []
                for ch in item.iter():
                    tag = ch.tag.split("}")[-1]
                    if tag == "approx_traffic":
                        traffic = (ch.text or "").strip()
                    elif tag == "news_item_title":
                        newst.append(ch.text or "")
                if t and (_niche_text(t + " " + " ".join(newst)) or t.lower() in news_blob):
                    digits = re.sub(r"\D", "", traffic) or "0"
                    found.append({"topic": t, "strength": 30 + min(25, 5 * math.log10(max(10, int(digits)))), "via": "Google Trends"})
                    n += 1
        status["trends"] = n
    except Exception as e:
        status["trends"] = f"failed: {str(e)[:60]}"

    # b) Wikipedia most-read (names in today's news that people look up)
    try:
        n = 0
        for back in (1, 2, 3):
            d = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=back)
            r = _get(f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{d:%Y/%m/%d}")
            if r.status_code == 200:
                for a in r.json()["items"][0]["articles"][:120]:
                    title = a["article"].replace("_", " ")
                    if title.startswith(("Main Page", "Special:", "Wikipedia:", "Portal:", "File:", "Category:")):
                        continue
                    if len(title) > 5 and (_niche_text(title) or title.lower() in news_blob):
                        found.append({"topic": title, "strength": 25 + min(20, 4 * math.log10(max(10, a["views"]))), "via": "Wikipedia"})
                        n += 1
                break
        status["wikipedia"] = n
    except Exception as e:
        status["wikipedia"] = f"failed: {str(e)[:60]}"

    # c) Google search suggestions = what people are typing about the seed topics right now
    try:
        n = 0

        def sug(k):
            r = _get("https://suggestqueries.google.com/complete/search", params={"client": "firefox", "q": k, "hl": "en-GB", "gl": "gb"})
            return [(k, x) for x in r.json()[1][:4] if _niche_text(x) and len(x.split()) >= 2 and x.lower() != k.lower()]

        res, _ = _parallel(sug, cfg["keywords"][:10], 5)
        for k, x in res:
            found.append({"topic": x, "strength": 18, "via": "Google searches"})
            n += 1
        status["suggest"] = n
    except Exception as e:
        status["suggest"] = f"failed: {str(e)[:60]}"

    # d) stories many outlets are running
    for c in sorted(clusters, key=lambda c: -c["n"])[:8]:
        if c["n"] >= 2 and (c["rel"] > 0 or c["n"] >= 3):
            freq = {}
            for t in c["titles"]:
                for w in _tokens(t):
                    freq[w] = freq.get(w, 0) + 1
            q = " ".join(w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:4])
            found.append({"topic": c["title"][:90], "q": q, "strength": 22 + 8 * min(c["n"], 5), "via": f"{c['n']} outlets"})

    # merge near-duplicates (same name/words), add strengths, keep the best 15
    merged = []
    for f in sorted(found, key=lambda f: -f["strength"]):
        tk = _tokens(f["topic"])
        for m in merged:
            if tk and (len(tk & m["_tk"]) >= 2 or tk <= m["_tk"] or m["_tk"] <= tk):
                m["strength"] += 8; m["via"] += " + " + f["via"] if f["via"] not in m["via"] else ""
                break
        else:
            merged.append({**f, "_tk": tk})
    merged.sort(key=lambda m: -m["strength"])
    out = [{"topic": m["topic"], "q": m.get("q") or m["topic"], "strength": min(100, round(m["strength"])), "via": m["via"]} for m in merged[:15]]
    return out, status


def radar_hit(title):
    """The hot topic this title is about (needs 2 shared words, or 1 for a one-word topic), else None."""
    tk = _tokens(title)
    best = None
    for r in RADAR["topics"]:
        rt = _tokens(r["topic"])
        if not rt:
            continue
        need = 1 if len(rt) == 1 else 2
        if len(tk & rt) >= need and (best is None or r["strength"] > best["strength"]):
            best = r
    return best


def _search_terms(cfg):
    """Your fixed topics plus today's hottest radar topics."""
    terms = list(cfg["keywords"][:14])
    for r in RADAR["topics"][:8]:
        q = (r.get("q") or r["topic"])[:70]
        if q not in terms:
            terms.append(q)
    return terms


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
                    it = _yt_item(e, ch, "channels", base=12 + (6 if suffix == "/shorts" else 0), need_niche=False, assume_short=(suffix == "/shorts"))
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
    kws = _search_terms(cfg)[:22]

    def one(k):
        res = []
        for q in (k + " #shorts", k):
            res += [i for i in (_yt_item(e, k, "keywords", base=6) for e in _yt_search(q, 6)) if i]
        return res

    out, errs = _parallel(one, kws, 4)
    if not out and errs:
        raise RuntimeError(errs[0])
    return out


def src_reddit(cfg):
    """Reddit's JSON is blocked for servers, but its RSS feeds answer. RSS has no vote counts or lengths, so rank in the
    day's top list stands in for popularity, and each v.redd.it video's length is read from its stream manifest."""
    subs = cfg["subreddits"][:10]
    if not subs:
        raise RuntimeError("off (optional) - no subreddits set")
    A = "{http://www.w3.org/2005/Atom}"
    M = "{http://search.yahoo.com/mrss/}"
    out, last, errors = [], "", 0
    for si, sub in enumerate(subs):
        try:
            r = None
            for attempt in range(2):
                r = _get(f"https://www.reddit.com/r/{sub}/top/.rss?t=day&limit=50")
                if r.status_code != 429:
                    break
                time.sleep(4)
            if r.status_code != 200:
                errors += 1; last = f"HTTP {r.status_code}"; continue
            root = ET.fromstring(r.content)
            rank = 0
            for ent in root.iter(A + "entry"):
                rank += 1
                body = ent.findtext(A + "content") or ""
                m = re.search(r"v\.redd\.it/([a-z0-9]+)", body)
                if not m:
                    continue
                vid = m.group(1)
                link = (ent.find(A + "link").get("href") if ent.find(A + "link") is not None else "")
                title = (ent.findtext(A + "title") or "").strip()
                th = ent.find(M + "thumbnail")
                dur = None
                try:
                    mp = _get(f"https://v.redd.it/{vid}/DASHPlaylist.mpd")
                    dm = re.search(r'mediaPresentationDuration="PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?"', mp.text)
                    if dm:
                        dur = int(dm.group(1) or 0) * 3600 + int(dm.group(2) or 0) * 60 + float(dm.group(3) or 0)
                except Exception:
                    pass
                if not dur or dur > MAX_DUR:
                    continue
                rel = relevance(title + " " + sub)
                out.append({
                    "source": "reddit", "ext_id": vid, "url": link, "title": title, "thumb": th.get("url") if th is not None else None,
                    "embed": None, "duration": round(dur), "topic": f"r/{sub}",
                    "stats": {"top_rank": rank, "subreddit": sub},
                    "_raw": 100 * (0.30 * max(0, 1 - rank / 50) + 0.30 * min(rel, 3) / 3) + 8,
                })
        except Exception as e:
            errors += 1; last = str(e)[:100]
        time.sleep(1.2)
    if errors == len(subs):
        raise RuntimeError(f"Reddit refused ({last})")
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


def src_dailymotion(cfg):
    kws = _search_terms(cfg)[:14]

    def one(k):
        r = _get("https://api.dailymotion.com/videos", params={
            "search": k, "sort": "trending", "limit": 10, "shorter_than": 3, "flags": "no_live,exportable",
            "created_after": int(time.time()) - 7 * 86400, "fields": "id,title,thumbnail_480_url,duration,views_total,url,owner.screenname"})
        if r.status_code != 200:
            raise RuntimeError(f"Dailymotion HTTP {r.status_code}")
        res = []
        for v in r.json().get("list", []):
            dur = v.get("duration")
            if not dur or dur > MAX_DUR:
                continue
            title = v.get("title") or ""
            rel = relevance(title + " " + k)
            if rel == 0:
                continue
            views = v.get("views_total") or 0
            res.append({"source": "dailymotion", "ext_id": v["id"], "url": v.get("url") or f"https://www.dailymotion.com/video/{v['id']}",
                        "title": title, "thumb": v.get("thumbnail_480_url"), "embed": None, "duration": dur, "topic": k,
                        "stats": {"views": views, "channel": v.get("owner.screenname")},
                        "_raw": 100 * (0.40 * _log_score(views, 5.5) + 0.30 * min(rel, 3) / 3) + 4})
        return res

    out, errs = _parallel(one, kws, 4)
    if not out and errs:
        raise RuntimeError(errs[0])
    return out


def src_hashtags(cfg):
    tags = [t.strip().lstrip("#") for t in cfg.get("hashtags", []) if t.strip()][:10]
    if not tags:
        raise RuntimeError("off (optional) - TikTok blocks cloud servers")

    def one(t):
        res = []
        for e in _flat(f"https://www.tiktok.com/tag/{t}", 12):
            e = dict(e)
            e.setdefault("webpage_url", e.get("url"))
            it = _yt_item(e, "#" + t, "hashtags", base=8, need_niche=False)
            if it:
                res.append(it)
        if not res:
            raise RuntimeError(f"#{t}: nothing readable")
        return res

    out, errs = _parallel(one, tags, 3)
    if not out:
        raise RuntimeError("TikTok blocks this server (" + (errs[0] if errs else "no data") + ")")
    return out


def src_odysee(cfg):
    """Odysee has an open search API and is popular with centre-right / free-speech creators."""
    kws = _search_terms(cfg)[:12]

    def one(k):
        res = []
        for extra in ({"release_time": ">" + str(int(time.time()) - 4 * 86400)}, {"release_time": ">" + str(int(time.time()) - 14 * 86400)}):
            r = requests.post("https://api.na-backend.odysee.com/api/v1/proxy?m=claim_search", headers={"User-Agent": UA}, timeout=15, json={
                "jsonrpc": "2.0", "method": "claim_search", "id": 1,
                "params": {"text": k, "claim_type": ["stream"], "stream_types": ["video"], "order_by": ["trending_group", "trending_mixed"],
                           "page_size": 20, "no_totals": True, "has_source": True, **extra}})
            if r.status_code != 200:
                raise RuntimeError(f"Odysee HTTP {r.status_code}")
            items = (r.json().get("result") or {}).get("items") or []
            if items:
                break
        for it in items:
            v = it.get("value") or {}
            dur = (v.get("video") or {}).get("duration")
            if not dur or dur > MAX_DUR:
                continue
            title = v.get("title") or ""
            rel = relevance(title + " " + k)
            if rel == 0:
                continue
            canon = (it.get("canonical_url") or "").replace("lbry://", "").replace("#", ":")
            if not canon:
                continue
            support = float((it.get("meta") or {}).get("effective_amount") or 0)
            res.append({"source": "odysee", "ext_id": it["claim_id"], "url": "https://odysee.com/" + canon, "title": title,
                        "thumb": (v.get("thumbnail") or {}).get("url"), "embed": None, "duration": dur, "topic": k,
                        "stats": {"channel": (it.get("signing_channel") or {}).get("name")},
                        "_raw": 100 * (0.30 * min(rel, 3) / 3 + 0.25 * _log_score(support * 10, 4)) + 6})
        return res

    out, errs = _parallel(one, kws, 4)
    if not out and errs:
        raise RuntimeError(errs[0])
    return out


def src_telegram(cfg):
    chans = [c.strip().lstrip("@").replace("https://t.me/", "").replace("s/", "") for c in cfg.get("telegram", []) if c.strip()][:15]
    if not chans:
        raise RuntimeError("no Telegram channels added (optional)")

    def one(ch):
        r = _get(f"https://t.me/s/{ch}")
        if r.status_code != 200:
            raise RuntimeError(f"{ch}: HTTP {r.status_code}")
        res = []
        for blk in r.text.split('class="tgme_widget_message_wrap')[1:]:
            post = re.search(r'data-post="([^"]+)"', blk)
            vid = re.search(r'<video[^>]+src="([^"]+)"', blk)
            if not post or not vid:
                continue
            dm = re.search(r'message_video_duration[^>]*>(\d+):(\d+)', blk)
            dur = int(dm.group(1)) * 60 + int(dm.group(2)) if dm else None
            if not dur or dur > MAX_DUR:
                continue
            txt = re.sub(r"<[^>]+>", " ", (re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', blk, re.S) or [None, ""])[1])
            title = re.sub(r"\s+", " ", txt).strip()[:200] or f"Video from {ch}"
            th = re.search(r"background-image:url\('([^']+)'\)", blk)
            vw = re.search(r'message_views">([\d.,KM]+)<', blk)
            v = 0
            if vw:
                x = vw.group(1).replace(",", "")
                v = int(float(x[:-1]) * (1000 if x[-1] == "K" else 1000000)) if x[-1] in "KM" else int(float(x))
            res.append({"source": "telegram", "ext_id": post.group(1), "url": "https://t.me/" + post.group(1), "title": title,
                        "thumb": th.group(1) if th else None, "preview_url": vid.group(1), "embed": "video", "duration": dur, "topic": "t.me/" + ch,
                        "stats": {"views": v}, "_raw": 100 * (0.35 * _log_score(v, 5.5) + 0.25 * min(relevance(title), 3) / 3) + 10})
        return res

    out, errs = _parallel(one, chans, 4)
    if not out:
        raise RuntimeError("; ".join(errs[:2]) or "no short videos found")
    return out


SOURCES = {"news": src_news, "channels": src_channels, "keywords": src_keywords, "reddit": src_reddit, "dailymotion": src_dailymotion, "odysee": src_odysee, "telegram": src_telegram, "hashtags": src_hashtags, "handles": src_handles}


# ---------------------------------------------------------------- scan

def scan():
    """Runs every source in parallel, scores, dedupes against everything seen before, stores the new ones."""
    if not _SCAN_LOCK.acquire(blocking=False):
        return LAST_SCAN
    LAST_SCAN["running"] = True
    try:
        cfg = get_cfg()
        results, status = {}, {}
        try:
            _NEWS["v"] = news_stories(cfg)
        except Exception:
            _NEWS["v"] = None
        try:
            RADAR["topics"], rstat = radar(cfg)
            status["radar"] = {"ok": any(isinstance(v, int) and v for v in rstat.values()) or bool(RADAR["topics"]), "found": len(RADAR["topics"]),
                               "detail": ", ".join(f"{k} {v}" for k, v in rstat.items())}
            if not status["radar"]["ok"]:
                status["radar"]["error"] = status["radar"]["detail"]
        except Exception as e:
            RADAR["topics"] = []
            status["radar"] = {"ok": False, "error": str(e)[:160]}

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
        for name in list(results):
            mx = max([c.get("_raw", 0) for c in results[name]] or [0])
            if mx > 0:
                for c in results[name]:
                    # blend: half the raw score, half "how it ranks within its own platform" so no platform is buried
                    c["_raw"] = 0.5 * c.get("_raw", 0) + 0.5 * (c.get("_raw", 0) / mx * 85)
        by_src = {}
        for name in list(results):
            by_src[name] = sorted(list(results[name]), key=lambda c: c.get("_raw", 0), reverse=True)
        # Round-robin across sources so one big source (YouTube) can't crowd the rest out of the 60 slots.
        cands, i = [], 0
        while any(i < len(v) for v in by_src.values()):
            for v in by_src.values():
                if i < len(v):
                    cands.append(v[i])
            i += 1
        floor = 15
        for c in cands:
            iid = _mk_id(c["source"], c["ext_id"])
            norm = re.sub(r"\W+", " ", (c["title"] or "").lower()).strip()[:60]
            dup_title = bool(norm) and norm in seen_titles
            if norm:
                seen_titles.add(norm)
            if iid in known_ids or c["url"] in known_urls or dup_title:
                continue
            c["id"] = iid
            hot = radar_hit(c["title"])
            if hot:
                c["_raw"] = c.get("_raw", 0) + 15
                c.setdefault("stats", {})["hot"] = hot["topic"][:60]
            c["score"] = round(max(0.0, min(100.0, c.get("_raw", 0))), 1)
            if c["score"] < floor:
                continue
            _insert(c)
            added += 1
            if added >= 60:
                break
        try:
            purge_unpostable()
            prune()
        except Exception:
            pass
        LAST_SCAN.update({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "sources": status, "added": added,
                          "candidates": len(cands)})
        try:
            LAST_SCAN["radar"] = RADAR["topics"]
            _kv_set("explore_last_scan", json.dumps({k: LAST_SCAN[k] for k in ("at", "sources", "added", "candidates", "radar")}))
        except Exception:
            pass
        return LAST_SCAN
    finally:
        LAST_SCAN["running"] = False
        _SCAN_LOCK.release()
