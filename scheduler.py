"""Scheduled publishing. Reads the saved edit (captions per platform) from
the history table at the moment a post is due -- so whatever the owner last
edited is what goes out -- and publishes through the same platform modules the
editor's Upload button uses.

Two things drive run_due(): a short in-process loop (works while the app is
awake) and POST /api/cron/tick (hit by an outside timer every minute, which is
what wakes the free Render dyno from sleep)."""
import os
import time
import threading
import datetime

import db
import notify
import storage
from caption import normalize_platform_posts, apply_disclosure


def _publish_one(modules: dict, item: dict) -> dict:
    platform = item["platform"]
    e = db.get_history_entry(item["history_id"])
    if not e:
        return {"ok": False, "error": "The edit this was scheduled from no longer exists."}
    module = modules.get(platform)
    if not module:
        return {"ok": False, "error": "This platform isn't connected."}
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    filename = e.get("video_filename") or ""
    if not filename:
        return {"ok": False, "error": "No video file recorded for this edit."}
    # Make sure the file is on local disk (pulled back from R2 if a redeploy
    # wiped it) before the platform comes to fetch it from our /files route.
    local = os.path.join(os.environ.get("DOWNLOAD_DIR", "/tmp/downloads"), filename)
    try:
        storage.fetch_to(local, filename)
    except Exception as ex:
        print(f"SCHEDULE: storage fetch failed for {filename}: {ex}", flush=True)
    video_url = f"{base}/files/{filename}"
    caption = e.get("posting_caption") or ""
    posts = normalize_platform_posts(e.get("platform_posts") or {}, caption)
    post = posts.get(platform)
    if (e.get("meta") or {}).get("paid_promo"):
        post = apply_disclosure(platform, post)
    try:
        outcome = module.publish_video(video_url, caption, post=post)
        return {"ok": True, **(outcome or {})}
    except Exception as ex:
        print(f"SCHEDULED PUBLISH FAILED ({platform}): {ex}", flush=True)
        return {"ok": False, "error": str(ex)}


def run_due(modules: dict) -> int:
    """Publishes everything that is due. Returns how many it picked up."""
    if not db.configured():
        return 0
    # No global lock: db.sched_claim_due() claims rows atomically, so two
    # overlapping ticks never take the same post, and a slow upload on one
    # tick doesn't hold up posts that fall due meanwhile.
    try:
        items = db.sched_claim_due()
        if not items:
            return 0

        def _do(item):
            res = _publish_one(modules, item)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            try:
                db.update_history_publish_results(item["history_id"], {item["platform"]: {"status": "done", **res, "at": now, "scheduled": True}})
            except Exception as ex:
                print(f"SCHEDULE: history result save failed: {ex}", flush=True)
            try:
                if res.get("ok"):
                    db.sched_finish(item["id"], "done", res)
                elif item.get("attempts", 1) < 2:
                    db.sched_finish(item["id"], "error", res, retry_in_minutes=3)   # one automatic retry
                else:
                    db.sched_finish(item["id"], "error", res)
                    notify.notify(f"Scheduled {item['platform']} post failed", str(res.get("error") or "")[:200],
                                  os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/schedule")
            except Exception as ex:
                print(f"SCHEDULE: finish failed: {ex}", flush=True)

        threads = [threading.Thread(target=_do, args=(i,), daemon=True) for i in items]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return len(items)
    except Exception as ex:
        print(f"SCHEDULE RUN ERROR: {ex}", flush=True)
        return 0


def start_loop(modules: dict, every: int = 30):
    def _loop():
        while True:
            try:
                run_due(modules)
            except Exception as ex:
                print(f"SCHEDULE LOOP ERROR: {ex}", flush=True)
            time.sleep(every)
    threading.Thread(target=_loop, daemon=True).start()
