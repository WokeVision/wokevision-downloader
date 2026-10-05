"""Account profile helpers for the Accounts tab.

Reads each platform's LIVE bio where its API exposes it, and pushes a new bio
only where an API genuinely allows that (today: Facebook Page "about").
Instagram, Threads, TikTok, X and YouTube don't let third-party apps change
the bio, so the Accounts tab keeps the draft and links straight to that
platform's own edit-profile screen.
"""
import requests

import db

EDIT_LINKS = {
    "instagram": "https://www.instagram.com/accounts/edit/",
    "threads": "https://www.threads.com/",
    "youtube": "https://studio.youtube.com/channel/UC/editing/details",
    "tiktok": "https://www.tiktok.com/setting",
    "x": "https://x.com/settings/profile",
    "facebook": "https://business.facebook.com/latest/settings/",
}
BIO_LIMITS = {"instagram": 150, "threads": 150, "youtube": 1000, "tiktok": 80, "x": 160, "facebook": 255}
LABELS = {"instagram": "Instagram", "threads": "Threads", "youtube": "YouTube", "tiktok": "TikTok", "x": "X", "facebook": "Facebook"}
ORDER = ["instagram", "threads", "youtube", "tiktok", "x", "facebook"]


def _get(url, **kw):
    kw.setdefault("timeout", 15)
    return requests.get(url, **kw)


def live(platform: str) -> dict:
    """{'connected': bool, 'name':..., 'handle':..., 'bio':..., 'link':..., 'url':..., 'error':...}"""
    out = {"connected": False, "name": None, "handle": None, "bio": None, "link": None, "url": None, "error": None}
    try:
        c = db.get_connection(platform)
        if not c or not c.get("access_token"):
            return out
        out["connected"] = True
        tok = c["access_token"]
        if platform == "instagram":
            from platforms import instagram as m
            r = _get(f"{m.GRAPH_BASE}/me", params={"fields": "username,name,biography,website", "access_token": tok})
            if r.status_code == 200:
                j = r.json()
                out.update(name=j.get("name"), handle="@" + (j.get("username") or ""), bio=j.get("biography"), link=j.get("website"),
                           url=f"https://www.instagram.com/{j.get('username')}/")
            else:
                out["error"] = r.text[:200]
        elif platform == "threads":
            from platforms import threads as m
            r = _get(f"{m.GRAPH_BASE}/me", params={"fields": "username,name,threads_biography", "access_token": tok})
            if r.status_code == 200:
                j = r.json()
                out.update(name=j.get("name"), handle="@" + (j.get("username") or ""), bio=j.get("threads_biography"),
                           url=f"https://www.threads.com/@{j.get('username')}")
            else:
                out["error"] = r.text[:200]
        elif platform == "youtube":
            r = _get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet,brandingSettings", "mine": "true"},
                     headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 200 and r.json().get("items"):
                ch = r.json()["items"][0]
                out.update(name=ch["snippet"].get("title"), handle=ch["snippet"].get("customUrl"), bio=ch["snippet"].get("description"),
                           url=f"https://www.youtube.com/channel/{ch['id']}")
            else:
                out["error"] = r.text[:200]
        elif platform == "x":
            r = _get("https://api.x.com/2/users/me", params={"user.fields": "description,url,name,username"},
                     headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 200:
                j = r.json().get("data", {})
                out.update(name=j.get("name"), handle="@" + (j.get("username") or ""), bio=j.get("description"),
                           url=f"https://x.com/{j.get('username')}")
            else:
                out["error"] = r.text[:200]
        elif platform == "facebook":
            pid = (c.get("extra") or {}).get("page_id")
            from platforms import facebook as m
            r = _get(f"{m.GRAPH_BASE}/{pid}", params={"fields": "name,about,website,link,username", "access_token": tok})
            if r.status_code == 200:
                j = r.json()
                out.update(name=j.get("name"), handle=j.get("username"), bio=j.get("about"), link=j.get("website"), url=j.get("link"))
            else:
                out["error"] = r.text[:200]
        elif platform == "tiktok":
            out["name"] = (c.get("extra") or {}).get("display_name")
            out["error"] = None
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def push_facebook(about: str = None, website: str = None) -> dict:
    c = db.get_connection("facebook")
    if not c or not c.get("access_token"):
        raise RuntimeError("Facebook isn't connected.")
    from platforms import facebook as m
    pid = (c.get("extra") or {}).get("page_id")
    data = {"access_token": c["access_token"]}
    if about is not None:
        data["about"] = about
    if website:
        data["website"] = website
    r = requests.post(f"{m.GRAPH_BASE}/{pid}", data=data, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(r.text[:300])
    return {"ok": True}
