import os
import re
import datetime as _dt
import secrets
import render
import uuid
import time
import shutil
import subprocess
import threading
import traceback

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, PlainTextResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downloader import download_video, DownloadError
from transcribe import transcribe_audio
import speech
import notify
from caption import (risk_check, translate_lines, generate_hook_options, generate_captions, generate_on_screen_caption, generate_posting_caption,
                     generate_platform_posts, normalize_platform_posts, PLATFORM_IDS, decide_credit, apply_disclosure,
                     credit_handle as caption_credit_handle, set_credit_in_posts)
from render import render_staged, apply_caption
import db
import auth
import storage
import insights
import scheduler
import accounts
from platforms import instagram, threads, youtube, x, tiktok, facebook

try:
    if os.environ.get("SENTRY_DSN"):
        import sentry_sdk
        sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=0.0, send_default_pii=False)
except Exception as _e:
    print(f"SENTRY INIT FAILED: {_e}", flush=True)

app = FastAPI()


@app.on_event("startup")
def _startup():
    # Platform connections are an optional subsystem layered on top of the
    # core video editor -- a bad/missing DATABASE_URL should never take the
    # whole app down, just leave connections showing as unavailable.
    try:
        db.init_db()
    except Exception as e:
        print(f"DB INIT FAILED (platform connections will be unavailable): {e}", flush=True)
    try:
        n = db.jobs_mark_interrupted()
        if n:
            print(f"Marked {n} interrupted job(s)", flush=True)
    except Exception as e:
        print(f"JOB RECOVERY FAILED: {e}", flush=True)
    try:
        scheduler.start_loop(PLATFORM_MODULES)
    except Exception as e:
        print(f"SCHEDULER START FAILED: {e}", flush=True)
    try:
        insights.init_snapshots()
    except Exception as e:
        print(f"INSIGHTS INIT FAILED (follower history unavailable): {e}", flush=True)


# --- Access control: everything behind a passkey, except what genuinely has
# to stay open (the login page itself, the auth ceremony endpoints, server-
# to-server video fetches from the platform APIs, TikTok's domain
# verification file, and the health check). Without PUBLIC_PATHS, a
# half-configured deploy (missing SETUP_CODE/SESSION_SECRET) would lock
# everyone out including the owner, so auth.configured() gates the whole
# thing -- if it's not set up yet, the app behaves exactly as before.
PUBLIC_PATH_PREFIXES = ("/static/", "/auth/", "/files/", "/c/", "/api/public/", "/go/")
PUBLIC_PATHS = {
    "/links/order", "/links/consult",
    "/links",               # public link-in-bio page
    "/sw.js",               # PWA service worker (must be at the root to cover the whole site)
    "/manifest.webmanifest",
    "/",                    # public homepage
    "/api/home/popular",    # public: top Instagram posts for the homepage carousel
    "/login",
    "/health",
    "/api/cron/tick",       # public but secret-gated (CRON_SECRET) -- the outside timer that wakes the app
    "/tiktokf2TEyaKWItLVEN7IU6Sr0Fyd4eBclual.txt",
    "/tiktokXoc4Y47fr98En3040kRaFWp20XNF2taG.txt",
}


@app.middleware("http")
async def _require_passkey_session(request: Request, call_next):
    if not auth.configured():
        return await call_next(request)
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
        return await call_next(request)

    token = request.cookies.get(auth.SESSION_COOKIE)
    if auth.verify_session_token(token):
        return await call_next(request)

    if request.method == "GET" and "text/html" in (request.headers.get("accept") or ""):
        return RedirectResponse(url="/login?next=" + _url_quote(path))
    return JSONResponse(status_code=401, content={"detail": "Not authenticated"})


# Platforms that are wired up for real vs. still placeholders in the UI.
# Extending to a new platform means adding its module here and to
# PLATFORM_LABELS -- the /connections, /connections/{id}/*, and /publish
# routes are all written generically against this registry.
PLATFORM_MODULES = {
    "instagram": instagram,
    "threads": threads,
    "youtube": youtube,
    "x": x,
    "tiktok": tiktok,
    "facebook": facebook,
}
PLATFORM_LABELS = {
    "instagram": "Instagram",
    "threads": "Threads",
    "youtube": "YouTube Shorts",
    "tiktok": "TikTok",
    "x": "X",
    "facebook": "Facebook",
}
PLATFORM_ORDER = ["instagram", "threads", "youtube", "tiktok", "x", "facebook"]

# Short-lived store of in-flight OAuth "state" values (CSRF protection for
# the connect flow). Single-user app, modest size -- an in-memory dict with
# a timestamp is enough; entries older than 10 minutes are ignored.
OAUTH_STATES = {}
OAUTH_STATE_TTL = 600


def _new_oauth_state(platform: str) -> str:
    state = f"{platform}:{uuid.uuid4()}"
    OAUTH_STATES[state] = time.time()
    return state


def _check_oauth_state(state: str) -> bool:
    ts = OAUTH_STATES.pop(state, None)
    return bool(ts and time.time() - ts < OAUTH_STATE_TTL)


def _url_quote(text: str) -> str:
    import urllib.parse
    return urllib.parse.quote((text or "")[:300])

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory job tracker. One process, modest traffic -- a dict behind the
# GIL is enough; nothing here needs to survive a restart.
JOBS = {}
JOBS_LOCK = threading.Lock()

# Same idea for /publish: each platform's publish_video() call can take a
# while (TikTok's status poll alone can run up to 5 minutes, and a resumable
# upload on a slow connection longer still), so publishing runs in a
# background thread -- one per platform, in parallel -- rather than as a
# single blocking request the browser just has to sit on with no feedback
# and no way to tell a slow call apart from a hung one.
PUBLISH_JOBS = {}

# Rough share of total time each stage takes, used to blend per-stage
# progress (0..1 within a stage) into one overall bar. Download and render
# report real progress within their slice; transcribe/caption don't have a
# meaningful sub-progress signal, so they just occupy their slice while
# running and complete it when done. (Captioning is now one combined OpenAI
# call instead of two serial ones, so it gets a smaller slice than before.)
STAGE_WEIGHTS = {
    "downloading": (0.00, 0.35),
    "transcribing": (0.35, 0.57),
    "captioning": (0.57, 0.63),
    "rendering": (0.63, 1.00),
}
STAGE_LABELS = {
    "queued": "Queued",
    "downloading": "Downloading video",
    "uploading": "Receiving upload",
    "transcribing": "Transcribing audio",
    "captioning": "Writing caption",
    "rendering": "Rendering video",
    "done": "Done",
    "error": "Error",
}

STAGE_ORDER = ["downloading", "transcribing", "captioning", "rendering"]

# Seed guesses for how long each stage takes (seconds), used only until real
# measurements come in. Deliberately on the generous side -- a first-run ETA
# that's a bit pessimistic and then comes in early feels much better than
# one that's optimistic and then blows past zero.
STAGE_AVG_SECONDS = {
    "downloading": 25.0,
    "transcribing": 18.0,
    "captioning": 8.0,
    "rendering": 25.0,
}
STAGE_AVG_LOCK = threading.Lock()


def _record_stage_duration(stage: str, duration: float):
    """Self-tuning ETA: blends each real stage duration into a running
    average (exponential moving average, recent runs weighted more) so the
    ETA keeps adapting to this server's actual speed instead of a fixed
    guess. Clamped to sane bounds so one freak slow/fast run can't skew
    future estimates too hard."""
    if stage not in STAGE_AVG_SECONDS or duration <= 0:
        return
    duration = max(1.0, min(duration, 600.0))
    with STAGE_AVG_LOCK:
        prev = STAGE_AVG_SECONDS[stage]
        STAGE_AVG_SECONDS[stage] = prev * 0.7 + duration * 0.3


def _estimate_eta_seconds(job: dict) -> float:
    """Seconds remaining, estimated from a mix of real-time progress signal
    (when a stage reports one -- download/render) and this server's learned
    average duration per stage (for transcribe/caption, which don't).
    Whichever source implies MORE time left wins, so the estimate only ever
    gets revised up when reality is running behind -- never quietly
    lowballs by trusting an optimistic average over what's actually
    happening."""
    stage = job.get("stage")
    if stage not in STAGE_ORDER:
        return 0.0
    idx = STAGE_ORDER.index(stage)
    with STAGE_AVG_LOCK:
        avgs = dict(STAGE_AVG_SECONDS)

    now = time.time()
    started = job.get("_stage_started_at", now)
    elapsed_in_stage = max(0.0, now - started)
    lo, hi = STAGE_WEIGHTS.get(stage, (0.0, 1.0))
    span = max(hi - lo, 0.001)
    frac_done = max(0.0, min((job.get("progress", lo) - lo) / span, 1.0))

    current_avg = avgs.get(stage, 15.0)
    if frac_done > 0.02:
        # Real sub-progress signal available (download/render): project from
        # it, but never go below the learned average for this stage.
        projected_total = elapsed_in_stage / frac_done
        expected_total = max(current_avg, projected_total)
    else:
        # No sub-progress signal (transcribe/caption): lean on the learned
        # average, but if we've already run past it, grow the estimate
        # rather than let the countdown hit zero while still working.
        expected_total = max(current_avg, elapsed_in_stage * 1.25)

    time_left_in_stage = max(expected_total - elapsed_in_stage, 2.0)
    time_left_future_stages = sum(avgs.get(s, 15.0) for s in STAGE_ORDER[idx + 1:])
    return round(time_left_in_stage + time_left_future_stages)


# How long a finished job's source/output files stick around on local disk,
# so the "change caption" buttons -- and now History -- can re-render, or
# play back an old video preview, without the user having to re-download or
# re-upload anything. This is local ephemeral disk, not persistent storage:
# a restart/redeploy wipes it regardless of this number. History's captions
# and publish results live in the database and survive forever either way;
# only the video preview itself depends on this window.
KEEP_ALIVE_SECONDS = 24 * 60 * 60


def _set_job(job_id, **fields):
    with JOBS_LOCK:
        JOBS[job_id].update(fields)
        st = JOBS[job_id].get("status")
        label = JOBS[job_id].get("stage_label")
        err = JOBS[job_id].get("error")
    if "status" in fields:
        try:
            db.job_upsert(job_id, st, label, err)
        except Exception as e:
            print(f"JOB DB SAVE FAILED: {e}", flush=True)


def _set_stage(job_id, stage, within_stage=0.0):
    now = time.time()
    with JOBS_LOCK:
        job = JOBS[job_id]
        prev_stage = job.get("stage")
        if prev_stage != stage:
            prev_started = job.get("_stage_started_at")
            if prev_stage in STAGE_ORDER and prev_started:
                _record_stage_duration(prev_stage, now - prev_started)
                job.setdefault("timings", {})[prev_stage] = round(job.get("timings", {}).get(prev_stage, 0) + now - prev_started, 1)
                print(f"TIMING job={job_id[:8]} stage={prev_stage} secs={now - prev_started:.1f}", flush=True)
            job["_stage_started_at"] = now

        lo, hi = STAGE_WEIGHTS.get(stage, (0.0, 1.0))
        overall = lo + (hi - lo) * max(0.0, min(within_stage, 1.0))
        job["stage"] = stage
        job["stage_label"] = STAGE_LABELS.get(stage, stage)
        job["progress"] = overall
        job["eta_seconds"] = _estimate_eta_seconds(job)


class ProcessRequest(BaseModel):
    url: str
    angle: str = ""
    wm_token: str = ""
    wm_pos: str = "right"
    campaign_id: str = ""


def _cleanup_later(path: str, delay: int = 1200):
    def _run():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
    threading.Thread(target=_run, daemon=True).start()


def _result_for(job_id: str, meta: dict, on_screen_caption: str, posting_caption: str, platform_posts: dict = None) -> dict:
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {
        "platform_posts": platform_posts or {},
        "video_url": f"{base_url}/files/{job_id}_final.mp4",
        "on_screen_caption": on_screen_caption,
        "caption": posting_caption,
        "download_method": meta.get("method", ""),
        "angle": meta.get("angle", ""),
        "cues": meta.get("cues") or [],
        "captions_on": bool(meta.get("captions_on", False)),
        "paid_promo": bool(meta.get("paid_promo", False)),
        "captions_style": meta.get("captions_style", "classic"),
        "credit": caption_credit_handle(meta),
        "campaign_id": (meta or {}).get("campaign_id", ""),
        "post": (meta or {}).get("post") or {},
        "cover_ms": (meta or {}).get("cover_ms"),
        "watermark": ({"pos": (meta.get("wm") or {}).get("pos", "right"),
                       "url": f"{base_url}/files/{(meta.get('wm') or {}).get('file')}"}
                      if (meta.get("wm") or {}).get("file") else None),
    }


def _save_history(job_id: str, meta: dict, on_screen_caption: str, posting_caption: str, transcript: str = ""):
    """Records this render as a history entry the editor can click back to
    later. Best-effort -- a history-save failure should never take down the
    actual video pipeline, so this only ever logs. The source file isn't
    guaranteed to still exist by the time this is read back (see
    KEEP_ALIVE_SECONDS) -- /history/{id} reports that at read time rather
    than assuming it here."""
    try:
        title = (meta or {}).get("title") or on_screen_caption or "Untitled"
        db.save_history_entry(
            entry_id=job_id,
            title=title[:200],
            video_filename=f"{job_id}_final.mp4",
            source_filename=f"{job_id}_source.mp4",
            on_screen_caption=on_screen_caption,
            posting_caption=posting_caption,
            transcript=transcript or "",
            meta=meta or {},
        )
    except Exception as e:
        print(f"HISTORY SAVE FAILED ({job_id}): {e}", flush=True)


def _save_platform_posts(job_id: str, posts: dict):
    try:
        db.update_history_platform_posts(job_id, posts)
    except Exception as e:
        print(f"HISTORY PLATFORM-POSTS SAVE FAILED ({job_id}): {e}", flush=True)


_SETTINGS_CACHE = {"t": 0.0, "v": {}}


def app_settings() -> dict:
    now = time.time()
    if now - _SETTINGS_CACHE["t"] > 30:
        try:
            _SETTINGS_CACHE["v"] = db.settings_get() or {}
        except Exception as e:
            print(f"SETTINGS LOAD FAILED: {e}", flush=True)
        _SETTINGS_CACHE["t"] = now
        _apply_brand_notes(_SETTINGS_CACHE["v"])
    return _SETTINGS_CACHE["v"]


def _apply_brand_notes(s: dict):
    import caption as _cap
    parts = []
    if (s.get("voice_notes") or "").strip():
        parts.append(s["voice_notes"].strip())
    if (s.get("banned") or "").strip():
        parts.append("NEVER use these words or phrases: " + s["banned"].strip().replace("\n", ", "))
    _cap.BRAND_NOTES = "\n".join(parts)[:3000]


class SettingsModel(BaseModel):
    vocab: str = ""
    banned: str = ""
    voice_notes: str = ""
    mask_profanity: bool = False
    mask_words: str = ""
    caption_style: str = "classic"
    slots: str = "08:00,12:00,18:00,20:00"
    smart_crop: bool = True


@app.get("/settings")
def settings_page():
    return FileResponse("static/settings.html")


@app.get("/api/settings")
def settings_get():
    _SETTINGS_CACHE["t"] = 0.0
    return {**SettingsModel().model_dump(), **app_settings()}


@app.put("/api/settings")
def settings_put(req: SettingsModel):
    data = req.model_dump()
    _t = re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", data.get("slots") or "")
    data["slots"] = ",".join(f"{int(h):02d}:{m}" for h, m in _t) or "08:00,12:00,18:00,20:00"
    data["caption_style"] = data["caption_style"] if data["caption_style"] in ("classic", "highlight") else "classic"
    try:
        db.settings_save(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    _SETTINGS_CACHE["v"], _SETTINGS_CACHE["t"] = data, time.time()
    _apply_brand_notes(data)
    return {"ok": True}


_HEX = re.compile(r"^[0-9a-f\-]{8,40}$")


def _wm_args(meta: dict):
    """Campaign watermark for apply_caption(): {"path","pos"} or None. Pulls
    the PNG back from durable storage if the local copy has aged out."""
    wm = (meta or {}).get("wm") or {}
    fn = wm.get("file")
    if not fn:
        return None
    path = os.path.join(DOWNLOAD_DIR, fn)
    if not os.path.exists(path):
        storage.fetch_to(path, fn)
    if not os.path.exists(path):
        return None
    return {"path": path, "pos": wm.get("pos") or "right"}


def _cues_args(meta: dict):
    meta = meta or {}
    return (meta.get("cues") or None) if meta.get("captions_on") else None


def _post_process(path: str, post: dict):
    """render.post_process with the saved music track (if any) resolved to a local file."""
    opts = dict(post or {})
    tok = (opts.get("music") or "").lower()
    if tok and _HEX.match(tok):
        mp = os.path.join(DOWNLOAD_DIR, f"music_{tok}.mp3")
        if not os.path.exists(mp):
            storage.fetch_to(mp, os.path.basename(mp))
        if os.path.exists(mp):
            opts["music_path"] = mp
    render.post_process(path, opts)


def _claim_watermark(job_id: str, token: str, pos: str, meta: dict) -> dict:
    """Attaches an uploaded-ahead watermark (token from /api/watermark) to
    this job's meta under a job-specific filename."""
    token = (token or "").strip().lower()
    if not token or not _HEX.match(token):
        return meta
    src = os.path.join(DOWNLOAD_DIR, f"wm_{token}.png")
    if not os.path.exists(src):
        storage.fetch_to(src, os.path.basename(src))
    if not os.path.exists(src):
        return meta
    fn = f"{job_id}_wm.png"
    shutil.copyfile(src, os.path.join(DOWNLOAD_DIR, fn))
    storage.upload_many_async([(os.path.join(DOWNLOAD_DIR, fn), fn)])
    return {**meta, "wm": {"file": fn, "pos": pos if pos in render.WM_POSITIONS else "right"}}


def _run_pipeline(job_id: str, final_source_path: str, meta: dict, pre_speech: dict = None):
    """Shared steps once a source video is on disk, regardless of whether it
    got there via download or direct upload: transcribe -> caption -> render
    -> store the result on the job. Runs in a background thread; all
    progress/results are communicated back through JOBS[job_id].

    The source file and transcript/meta are kept on the job (rather than
    deleted immediately) so the "change caption" buttons can regenerate the
    on-screen caption + re-render, or regenerate just the posting caption,
    without re-downloading or re-uploading anything."""
    output_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_final.mp4")
    staged_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_staged.mp4")
    try:
        with JOBS_LOCK:
            _angle = (JOBS.get(job_id) or {}).get("angle", "")
        meta = {**(meta or {}), "angle": _angle}
        with JOBS_LOCK:
            _j = JOBS.get(job_id) or {}
            _wm_token, _wm_pos = _j.get("wm_token", ""), _j.get("wm_pos", "right")
        _cid = _j.get("campaign_id", "")
        _camp = None
        try:
            _camp = db.camp_get(_cid) if _cid else None
        except Exception:
            _camp = None
        if _camp:
            if not _wm_token and _camp.get("wm_token"):
                _wm_token, _wm_pos = _camp["wm_token"], _camp.get("wm_pos") or "right"
            meta = {**meta, "campaign_id": _cid}
            if (_camp.get("brief") or "").strip():
                meta["campaign_brief"] = _camp["brief"].strip()[:1500]
        meta = _claim_watermark(job_id, _wm_token, _wm_pos, meta)
        _set_stage(job_id, "transcribing")
        _st = app_settings()
        sp = pre_speech or speech.transcribe_words(final_source_path, vocab=_st.get("vocab", ""))
        transcript = sp.get("text", "")
        cues = speech.build_cues(sp.get("words") or [])
        if _st.get("mask_profanity"):
            cues = speech.mask_cues(cues, (_st.get("mask_words") or "").replace("\n", ",").split(","))
        meta = {**meta, "cues": cues, "captions_on": bool(cues), "captions_style": _st.get("caption_style", "classic")}
        _set_job(job_id, transcript=transcript, meta=meta, source_path=final_source_path)

        _set_stage(job_id, "captioning")
        if "credit_ok" not in meta:
            meta = {**meta, "credit_ok": decide_credit(meta, transcript)}
            _set_job(job_id, meta=meta)
        on_screen_caption, posting_caption = generate_captions(transcript, meta)
        platform_posts = generate_platform_posts(transcript, meta, on_screen_caption, posting_caption)
        if _camp:
            _tags = _camp_hashtags(_camp)
            if _tags:
                posting_caption = _with_hashtags(posting_caption, _tags)
                platform_posts = _tag_posts(platform_posts, posting_caption, _tags)

        # Rendering happens in two cached stages: render_staged() does the
        # crop/scale/logo/watermark compositing (everything that has nothing
        # to do with the caption text) and is kept on disk afterwards, so a
        # later caption-only edit (regenerate-video / set-on-screen-caption)
        # can call apply_caption() straight against it instead of redoing
        # this work and re-decoding the original source every time.
        _set_stage(job_id, "rendering", 0.0)
        _crop = None
        if app_settings().get("smart_crop", True):
            _crop = render.detect_subject(final_source_path)
            if _crop:
                meta = {**meta, "crop": _crop}
                print(f"SMART CROP {job_id}: {_crop}", flush=True)
        render_staged(
            source_path=final_source_path,
            output_path=staged_path,
            progress_cb=lambda frac: _set_stage(job_id, "rendering", frac * 0.5),
            crop=_crop,
        )
        _set_job(job_id, staged_path=staged_path)
        apply_caption(
            staged_path=staged_path,
            caption_text=on_screen_caption,
            output_path=output_path,
            progress_cb=lambda frac: _set_stage(job_id, "rendering", 0.5 + frac * 0.5),
            cues=_cues_args(meta), watermark=_wm_args(meta), cue_style=(meta or {}).get("captions_style", "classic"),
        )
        _post_process(output_path, (meta or {}).get("post"))
        with JOBS_LOCK:
            _record_stage_duration("rendering", time.time() - JOBS[job_id].get("_stage_started_at", time.time()))

        if not os.path.exists(output_path):
            raise RuntimeError("Render finished but no output file was produced.")

        _cleanup_later(output_path, delay=KEEP_ALIVE_SECONDS)
        _cleanup_later(staged_path, delay=KEEP_ALIVE_SECONDS)
        _cleanup_later(final_source_path, delay=KEEP_ALIVE_SECONDS)
        _set_job(
            job_id,
            stage="done", stage_label="Done", progress=1.0, status="done", eta_seconds=0,
            on_screen_caption=on_screen_caption,
            posting_caption=posting_caption,
            platform_posts=platform_posts,
            result=_result_for(job_id, meta, on_screen_caption, posting_caption, platform_posts),
        )
        _save_history(job_id, meta, on_screen_caption, posting_caption, transcript)
        _save_platform_posts(job_id, platform_posts)
        if _camp:
            try:
                db.history_set_campaign(job_id, _cid)
            except Exception as e:
                print(f"CAMPAIGN LINK FAILED: {e}", flush=True)
        if not (JOBS.get(job_id) or {}).get("_quiet"):
            _b = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
            notify.notify("Edit ready", on_screen_caption[:120], f"{_b}/editor#entry={job_id}" if _b else None)
        # Mirror to durable storage (no-op if not configured) so the video
        # and the files behind fast caption edits survive redeploys.
        storage.upload_many_async([
            (output_path, os.path.basename(output_path)),
            (staged_path, os.path.basename(staged_path)),
            (final_source_path, os.path.basename(final_source_path)),
        ])
    except Exception as e:
        tb = traceback.format_exc()
        print("PIPELINE FAILED:", tb, flush=True)
        if os.path.exists(final_source_path):
            os.remove(final_source_path)
        _set_job(job_id, stage="error", stage_label="Error", status="error", error=str(e))
        if not (JOBS.get(job_id) or {}).get("_quiet"):
            notify.notify("Edit failed", str(e)[:200])


def _render_with_caption(job_id: str, on_screen_caption: str, posting_caption: str, platform_posts: dict = None):
    """Shared fast re-render: applies on_screen_caption to the job's cached
    staged video (see render_staged/apply_caption in render.py), rebuilding
    that staged composite from the kept raw source first if its own
    keep-alive window has already lapsed, then writes the new output and
    persists both captions onto the job + history. Used by both the
    AI-regenerate path ("change video caption") and the manual on-screen-
    caption editor -- they differ only in how on_screen_caption/
    posting_caption were produced, not in how the render happens.
    Raises RuntimeError (with a user-facing message) on failure; callers are
    expected to catch it and set the job's error state."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise RuntimeError("Unknown job id.")
    # Expected names are fixed per job id, so a reopened History entry (or a
    # post-redeploy job) can pull them back from durable storage on demand.
    staged_path = job.get("staged_path") or os.path.join(DOWNLOAD_DIR, f"{job_id}_staged.mp4")
    final_source_path = job.get("source_path") or os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
    meta = job.get("meta", {})
    transcript = job.get("transcript", "")
    output_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_final.mp4")

    have_staged = storage.fetch_to(staged_path, os.path.basename(staged_path))
    have_source = have_staged or storage.fetch_to(final_source_path, os.path.basename(final_source_path))
    if not have_staged and not have_source:
        raise RuntimeError(
            "The original video file has expired, so the caption can't be "
            "changed anymore -- please re-process the video."
        )

    _set_stage(job_id, "rendering", 0.0)
    progress_base = 0.0
    if not have_staged:
        # The staged composite itself aged out (shouldn't normally happen,
        # since it shares the source file's keep-alive window) but the raw
        # source is still here -- rebuild the staged file once, then cache
        # it again so later caption edits are fast again too.
        staged_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_staged.mp4")
        render_staged(
            source_path=final_source_path, output_path=staged_path,
            progress_cb=lambda frac: _set_stage(job_id, "rendering", frac * 0.5),
            crop=(meta or {}).get("crop"),
        )
        _cleanup_later(staged_path, delay=KEEP_ALIVE_SECONDS)
        _set_job(job_id, staged_path=staged_path)
        progress_base = 0.5

    apply_caption(
        staged_path=staged_path, caption_text=on_screen_caption, output_path=output_path,
        progress_cb=lambda frac: _set_stage(job_id, "rendering", progress_base + frac * (1 - progress_base)),
        cues=_cues_args(meta), watermark=_wm_args(meta), cue_style=(meta or {}).get("captions_style", "classic"),
    )
    _post_process(output_path, (meta or {}).get("post"))
    with JOBS_LOCK:
        _record_stage_duration("rendering", time.time() - JOBS[job_id].get("_stage_started_at", time.time()))

    if not os.path.exists(output_path):
        raise RuntimeError("Render finished but no output file was produced.")

    _cleanup_later(output_path, delay=KEEP_ALIVE_SECONDS)
    if platform_posts is None:
        platform_posts = job.get("platform_posts") or {}
    _set_job(
        job_id,
        stage="done", stage_label="Done", progress=1.0, status="done", eta_seconds=0,
        on_screen_caption=on_screen_caption,
        posting_caption=posting_caption,
        platform_posts=platform_posts,
        result=_result_for(job_id, meta, on_screen_caption, posting_caption, platform_posts),
    )
    _save_history(job_id, meta, on_screen_caption, posting_caption, transcript)
    _save_platform_posts(job_id, platform_posts)
    # The re-rendered final replaces the stored one; staged is re-uploaded
    # too in case it had to be rebuilt.
    storage.upload_many_async([
        (output_path, os.path.basename(output_path)),
        (staged_path, os.path.basename(staged_path)),
    ])


def _run_regenerate_video(job_id: str):
    """Re-generate the on-screen caption (and, to match it, the posting
    caption) and fast-re-render against the kept staged/source file, without
    re-downloading/re-uploading or redoing the crop/scale/logo/watermark
    work. Used by the "change video caption" button."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    # The endpoint already validated status=="done" and flipped it to
    # "running" before starting this thread, so we don't re-check it here.
    if not job:
        return
    transcript = job.get("transcript", "")
    meta = job.get("meta", {})
    prev_on_screen = job.get("on_screen_caption", "")

    try:
        _set_stage(job_id, "captioning")
        on_screen_caption = generate_on_screen_caption(transcript, meta, avoid=prev_on_screen)
        posting_caption = generate_posting_caption(transcript, meta, on_screen_caption)
        platform_posts = generate_platform_posts(transcript, meta, on_screen_caption, posting_caption)
        _render_with_caption(job_id, on_screen_caption, posting_caption, platform_posts)
    except Exception as e:
        tb = traceback.format_exc()
        print("REGENERATE VIDEO FAILED:", tb, flush=True)
        _set_job(job_id, stage="error", stage_label="Error", status="error", error=str(e))


def _run_set_on_screen_caption(job_id: str, on_screen_caption: str):
    """Manual on-screen-caption edit: the text comes straight from the user,
    so -- unlike "change video caption" -- this never calls OpenAI, it only
    fast-re-renders. The posting caption is left exactly as it was."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return
    posting_caption = job.get("posting_caption", "")
    try:
        _render_with_caption(job_id, on_screen_caption, posting_caption)
    except Exception as e:
        tb = traceback.format_exc()
        print("SET ON-SCREEN CAPTION FAILED:", tb, flush=True)
        _set_job(job_id, stage="error", stage_label="Error", status="error", error=str(e))


def _run_regenerate_caption(job_id: str):
    """Re-generate only the posting caption (text-only, no re-render). Used
    by the "change posting caption" button."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return
    transcript = job.get("transcript", "")
    meta = job.get("meta", {})
    on_screen_caption = job.get("on_screen_caption", "")
    prev_posting = job.get("posting_caption", "")

    try:
        posting_caption = generate_posting_caption(
            transcript, meta, on_screen_caption, avoid=prev_posting
        )
        platform_posts = generate_platform_posts(transcript, meta, on_screen_caption, posting_caption)
        _set_job(
            job_id,
            posting_caption=posting_caption,
            platform_posts=platform_posts,
            result=_result_for(job_id, meta, on_screen_caption, posting_caption, platform_posts),
        )
        try:
            db.update_history_caption(job_id, posting_caption=posting_caption)
        except Exception as e:
            print(f"HISTORY UPDATE FAILED ({job_id}): {e}", flush=True)
        _save_platform_posts(job_id, platform_posts)
    except Exception as e:
        tb = traceback.format_exc()
        print("REGENERATE CAPTION FAILED:", tb, flush=True)
        _set_job(job_id, stage="error", stage_label="Error", status="error", error=str(e))


def _run_download_then_pipeline(job_id: str, url: str, final_source_path: str):
    try:
        _set_stage(job_id, "downloading", 0.0)
        meta = download_video(
            url, final_source_path,
            progress_cb=lambda frac: _set_stage(job_id, "downloading", frac),
        )
    except DownloadError as e:
        _set_job(
            job_id, stage="error", stage_label="Error", status="error",
            error=(
                f"Could not download video: {e}\n\n"
                "If this is a YouTube link, this usually means YouTube is "
                "currently blocking this server's IP address rather than "
                "anything wrong with the link -- download the video "
                "yourself (e.g. with a browser extension) and upload the "
                "file directly instead."
            ),
        )
        return
    _run_pipeline(job_id, final_source_path, meta)


@app.get("/")
def home():
    return FileResponse("static/home.html")


@app.get("/editor")
def editor():
    return FileResponse("static/index.html")


@app.get("/dashboards")
def dashboards_page():
    return FileResponse("static/dashboards.html")


@app.get("/auth/status")
def auth_status(request: Request):
    """Lets the public pages' nav bar show Sign in vs Sign out."""
    if not auth.configured():
        return {"authed": True}
    return {"authed": bool(auth.verify_session_token(request.cookies.get(auth.SESSION_COOKIE)))}


@app.get("/api/home/popular")
def home_popular():
    """Most-engaged recent Instagram posts, for the public homepage. Only
    the public bits (thumbnail, caption snippet, link, like/comment counts)
    leave the server -- never tokens or anything account-private."""
    data = insights.get("instagram")
    posts = [p for p in data.get("posts", []) if p.get("thumb")]
    posts.sort(key=lambda p: (p.get("likes") or 0) + 2 * (p.get("comments") or 0), reverse=True)
    return {"account": data.get("account"), "posts": [
        {"thumb": p["thumb"], "url": p["url"], "title": p["title"],
         "likes": p.get("likes"), "comments": p.get("comments")} for p in posts[:10]]}


# What each platform's messaging API actually allows. The inbox itself comes
# online platform by platform once the permission below is granted -- until
# then the Messages tab shows exactly what's missing instead of an empty box.
MESSAGE_SUPPORT = {
    "instagram": ("needs_access", "Needs the instagram_business_manage_messages permission (Meta app review), then reconnect Instagram."),
    "facebook": ("needs_access", "Needs the pages_messaging permission (Meta app review), then reconnect Facebook."),
    "x": ("needs_access", "X direct messages need dm.read / dm.write scopes on a paid X API plan."),
    "threads": ("unsupported", "Threads doesn't offer a direct-message API."),
    "tiktok": ("unsupported", "TikTok doesn't offer a direct-message API to apps like this."),
    "youtube": ("unsupported", "YouTube has no direct messages; comment replies could be added instead."),
}


# Platforms whose DMs are actually wired up. Each needs list_conversations /
# get_thread / send_message on its module.
MESSAGING_MODULES = {"instagram": instagram}


@app.get("/api/messages")
def api_messages():
    convos, status = [], []
    for p in PLATFORM_ORDER:
        mod = MESSAGING_MODULES.get(p)
        if not mod:
            st, note = MESSAGE_SUPPORT[p]
        else:
            try:
                convos += mod.list_conversations()
                st, note = "live", "Connected -- messages from this platform appear in the inbox."
            except Exception as e:
                msg = str(e)
                if "not connected" in msg.lower():
                    st, note = "needs_access", "Connect Instagram in the Video Editor first."
                else:
                    st, note = "needs_access", "Reconnect Instagram in the Video Editor to grant message access, and make sure 'Allow access to messages' is on in the Instagram app. (" + msg[:120] + ")"
        status.append({"platform": p, "label": PLATFORM_LABELS[p], "status": st, "note": note})
    convos.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return {"conversations": convos, "platforms": status}


@app.get("/api/messages/{platform}/{conversation_id}")
def api_message_thread(platform: str, conversation_id: str):
    mod = MESSAGING_MODULES.get(platform)
    if not mod:
        raise HTTPException(status_code=404, detail="Messaging isn't available for this platform.")
    try:
        return {"messages": mod.get_thread(conversation_id)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:300])


class ReplyRequest(BaseModel):
    recipient_id: str
    text: str


@app.post("/api/messages/{platform}/reply")
def api_message_reply(platform: str, req: ReplyRequest):
    mod = MESSAGING_MODULES.get(platform)
    text = (req.text or "").strip()
    if not mod:
        raise HTTPException(status_code=404, detail="Messaging isn't available for this platform.")
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty.")
    try:
        mod.send_message(req.recipient_id, text[:1000])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:300])
    return {"ok": True}


@app.get("/ideas")
def ideas_page():
    return FileResponse("static/ideas.html")


class IdeaModel(BaseModel):
    url: str
    note: str = ""


@app.get("/api/ideas")
def ideas_list():
    return {"items": db.ideas_list()}


@app.post("/api/ideas")
def ideas_add(req: IdeaModel):
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="Add a link.")
    db.ideas_add(req.url.strip()[:1000], req.note.strip()[:500])
    return {"ok": True}


@app.delete("/api/ideas/{iid}")
def ideas_delete(iid: int):
    db.ideas_delete(iid)
    return {"ok": True}


# --- Link-in-bio ---
def _clean_link_url(u: str) -> str:
    u = (u or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", u, re.I):
        raise HTTPException(status_code=400, detail="That doesn't look like a valid link.")
    return u[:1000]


@app.get("/links")
def bio_public_page():
    return FileResponse("static/links.html")


_BIO_DEFAULTS = {"title": "WokeVision", "handle": "@wokevision_", "text": "", "contact_email": "contactwokevision@gmail.com",
                 "reel_price": 100, "still_price": 80, "currency": "$",
                 "deal_note": "First-time customer deals and bulk-upload deals are available on request.",
                 "socials": [{"label": "Instagram", "url": "https://instagram.com/wokevision_"}],
                 "pay_link_reel": "", "pay_link_still": "", "meeting_link": "", "consult_blurb": "", "show_services": True, "show_tool": True}


def _bio_page() -> dict:
    return {**_BIO_DEFAULTS, **{k: v for k, v in (db.bio_page_get() or {}).items() if v not in (None, "")}}


@app.get("/api/public/bio")
def bio_public_data():
    page = _bio_page()
    return {**{k: page[k] for k in ("title", "handle", "text", "contact_email", "reel_price", "still_price", "currency", "deal_note",
                                    "socials", "consult_blurb", "show_services", "show_tool")},
            "links": [{"id": l["id"], "label": l["label"]} for l in db.bio_links_list(only_active=True)]}


@app.get("/go/{lid}")
def bio_go(lid: int, request: Request):
    url = db.bio_click(lid)
    if not url:
        return RedirectResponse("/links", status_code=302)
    ua = (request.headers.get("user-agent") or "").lower()
    if not re.search(r"bot|crawl|spider|preview|facebookexternalhit|slurp", ua):
        try:
            db.bio_click_record(lid)
        except Exception as e:
            print(f"BIO CLICK FAILED: {e}", flush=True)
    return RedirectResponse(url, status_code=302)


@app.get("/bio")
def bio_admin_page():
    return FileResponse("static/bio.html")


class BioPage(BaseModel):
    title: str = ""
    handle: str = ""
    text: str = ""
    contact_email: str = ""
    reel_price: int = 100
    still_price: int = 80
    currency: str = "$"
    deal_note: str = ""
    socials: list = []
    pay_link_reel: str = ""
    pay_link_still: str = ""
    meeting_link: str = ""
    consult_blurb: str = ""
    show_services: bool = True
    show_tool: bool = True


class BioLink(BaseModel):
    id: int = 0
    label: str
    url: str
    active: bool = True
    campaign_id: str = ""


@app.get("/api/bio")
def bio_admin_data():
    return {"page": _bio_page(), "links": db.bio_links_list(with_stats=True),
            "campaigns": [{"id": c["id"], "name": c["name"]} for c in db.camp_list()]}


@app.put("/api/bio/page")
def bio_admin_page_save(req: BioPage):
    socials = []
    for it in (req.socials or [])[:12]:
        if isinstance(it, dict) and str(it.get("url") or "").strip():
            socials.append({"label": str(it.get("label") or "").strip()[:30] or "Link", "url": _clean_link_url(str(it["url"]))})
    for k in ("pay_link_reel", "pay_link_still", "meeting_link"):
        v = getattr(req, k).strip()
        if v:
            _clean_link_url(v)
    em = req.contact_email.strip()
    if em and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em):
        raise HTTPException(status_code=400, detail="That contact email doesn't look right.")
    db.bio_page_save({"title": req.title[:60], "handle": req.handle[:40], "text": req.text[:240], "contact_email": em[:120],
                      "reel_price": max(0, min(req.reel_price, 100000)), "still_price": max(0, min(req.still_price, 100000)),
                      "currency": (req.currency or "$")[:3], "deal_note": req.deal_note[:300], "socials": socials,
                      "pay_link_reel": req.pay_link_reel.strip()[:500], "pay_link_still": req.pay_link_still.strip()[:500],
                      "meeting_link": req.meeting_link.strip()[:500], "consult_blurb": req.consult_blurb[:600], "show_services": req.show_services, "show_tool": req.show_tool})
    return {"ok": True}


@app.post("/api/bio/link")
def bio_admin_link_save(req: BioLink):
    if not req.label.strip():
        raise HTTPException(status_code=400, detail="Give the button a label.")
    db.bio_link_save(req.id or None, req.label.strip()[:80], _clean_link_url(req.url), req.active, req.campaign_id)
    return {"ok": True}


@app.delete("/api/bio/link/{lid}")
def bio_admin_link_delete(lid: int):
    db.bio_link_delete(lid)
    return {"ok": True}


class BioOrder(BaseModel):
    ids: list


@app.put("/api/bio/order")
def bio_admin_order(req: BioOrder):
    db.bio_links_reorder([int(i) for i in req.ids])
    return {"ok": True}



# --- Customer requests: paid post orders + consultation bookings --------------------------
_REQ_HITS = {}   # (kind, ip) -> [timestamps]
_ORDER_DIR = DOWNLOAD_DIR


def _rate_ok(kind: str, ip: str, limit: int = 5, window: int = 3600) -> bool:
    now = time.time()
    hits = [t for t in _REQ_HITS.get((kind, ip), []) if now - t < window]
    if len(hits) >= limit:
        _REQ_HITS[(kind, ip)] = hits
        return False
    hits.append(now)
    _REQ_HITS[(kind, ip)] = hits
    return True


@app.get("/links/order")
def order_page():
    return FileResponse("static/order.html")


@app.get("/links/consult")
def consult_page():
    return FileResponse("static/consult.html")


@app.get("/requests")
def requests_page():
    return FileResponse("static/requests.html")


_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
_MAX_VIDEO = 250 * 1024 * 1024
_MAX_IMAGE = 25 * 1024 * 1024
_ORDER_PLATFORMS = {"instagram", "tiktok", "youtube", "x", "threads", "facebook"}


def _stream_to(upload: UploadFile, path: str, limit: int) -> int:
    size = 0
    with open(path, "wb") as f:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                f.close()
                os.remove(path)
                raise HTTPException(status_code=413, detail=f"That file is too large (max {limit // (1024 * 1024)}MB).")
            f.write(chunk)
    return size


def _prep_still(src: str, dest: str):
    """JPEG, in Instagram's allowed 4:5-1.91:1 range (padded over a blurred copy if outside it)."""
    from PIL import Image, ImageFilter, ImageOps
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    im.thumbnail((1440, 1800))
    w, h = im.size
    ratio = w / h
    lo, hi = 0.8, 1.91
    if ratio < lo or ratio > hi:
        tw, th = (int(h * lo), h) if ratio < lo else (w, int(w / hi))
        bg = im.resize((tw, th)).filter(ImageFilter.GaussianBlur(24))
        bg.paste(im, ((tw - w) // 2, (th - h) // 2))
        im = bg
    im.save(dest, "JPEG", quality=90)


def _base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


@app.post("/api/public/order")
async def public_order(request: Request, kind: str = Form(...), platform: str = Form("instagram"), caption: str = Form(""),
                       run_at: str = Form(""), name: str = Form(""), email: str = Form(""), notes: str = Form(""),
                       website: str = Form(""), files: list[UploadFile] = File(...)):
    if website:                                   # honeypot: real people never fill this
        return {"ok": True, "id": "x"}
    ip = _client_ip(request)
    if not _rate_ok("order", ip):
        raise HTTPException(status_code=429, detail="Too many requests from your connection. Please try again later.")
    page = _bio_page()
    kind = "reel" if kind == "reel" else "still"
    platform = platform if platform in _ORDER_PLATFORMS else "instagram"
    if kind == "still":
        platform = "instagram"
    name, email = name.strip()[:80], email.strip()[:120]
    if not name or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Please add your name and a valid email so we can confirm your booking.")
    caption = caption.strip()[:2200]
    if not caption:
        raise HTTPException(status_code=400, detail="Please write the caption you'd like us to post.")
    when = _parse_when(run_at)
    if when < _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1):
        raise HTTPException(status_code=400, detail="Please choose a date and time at least an hour from now.")
    files = [f for f in files if f and f.filename]
    rid = uuid.uuid4().hex[:14]
    saved = []
    try:
        if kind == "reel":
            if len(files) != 1 or os.path.splitext(files[0].filename.lower())[1] not in _VIDEO_EXT:
                raise HTTPException(status_code=400, detail="Please upload one video file (MP4 or MOV).")
            fn = f"order_{rid}_src{os.path.splitext(files[0].filename.lower())[1]}"
            _stream_to(files[0], os.path.join(DOWNLOAD_DIR, fn), _MAX_VIDEO)
            dur = render._probe_duration(os.path.join(DOWNLOAD_DIR, fn)) or 0
            if dur < 3 or dur > 90:
                raise HTTPException(status_code=400, detail="Reels need to be between 3 and 90 seconds long.")
            saved = [fn]
        else:
            if not 1 <= len(files) <= 10:
                raise HTTPException(status_code=400, detail="Please upload between 1 and 10 images.")
            for i, f in enumerate(files):
                if os.path.splitext(f.filename.lower())[1] not in _IMAGE_EXT:
                    raise HTTPException(status_code=400, detail="Images must be JPG, PNG or WebP.")
                raw = os.path.join(DOWNLOAD_DIR, f"order_{rid}_{i}.raw")
                _stream_to(f, raw, _MAX_IMAGE)
                try:
                    _prep_still(raw, os.path.join(DOWNLOAD_DIR, f"order_{rid}_{i}.jpg"))
                except Exception:
                    raise HTTPException(status_code=400, detail=f"We couldn't read “{f.filename}” as an image.")
                finally:
                    if os.path.exists(raw):
                        os.remove(raw)
                saved.append(f"order_{rid}_{i}.jpg")
    except HTTPException:
        for fn in saved + [f"order_{rid}_src{e}" for e in _VIDEO_EXT]:
            fp = os.path.join(DOWNLOAD_DIR, fn)
            if os.path.exists(fp):
                os.remove(fp)
        raise
    finally:
        for f in files:
            await f.close()
    storage.upload_many_async([(os.path.join(DOWNLOAD_DIR, fn), fn) for fn in saved])
    price = page["reel_price"] if kind == "reel" else page["still_price"]
    db.req_create(rid, "order", name, email, {"type": kind, "platform": platform, "caption": caption, "files": saved,
                                              "notes": notes.strip()[:500], "price": price, "currency": page["currency"]}, when)
    notify.notify("New post request", f"{name}: {kind} for {platform} ({page['currency']}{price}) — scheduled {when.strftime('%d %b %H:%M')} UTC",
                  f"{_base_url()}/requests" if _base_url() else None)
    notify.send_email(email, "We got your request", f"Hi {name},\n\nThanks — we've received your {kind} request for {platform} and will confirm it by email shortly.\n\n{page['title']}", page["contact_email"])
    pay = page.get("pay_link_reel" if kind == "reel" else "pay_link_still") or ""
    return {"ok": True, "id": rid, "price": price, "currency": page["currency"], "pay_link": pay}


class ConsultReq(BaseModel):
    name: str
    email: str
    when: str = ""
    when_iso: str = ""
    topic: str = ""
    website: str = ""


@app.post("/api/public/consult")
def public_consult(req: ConsultReq, request: Request):
    if req.website:
        return {"ok": True}
    if not _rate_ok("consult", _client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests from your connection. Please try again later.")
    name, email = req.name.strip()[:80], req.email.strip()[:120]
    if not name or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Please add your name and a valid email.")
    when = req.when.strip()[:200]
    rid = uuid.uuid4().hex[:14]
    db.req_create(rid, "consult", name, email, {"when": when, "when_iso": req.when_iso[:40], "topic": req.topic.strip()[:1500]})
    notify.notify("Consultation request", f"{name} would like a call: {when or 'time not given'}", f"{_base_url()}/requests" if _base_url() else None)
    page = _bio_page()
    notify.send_email(email, "We got your consultation request", f"Hi {name},\n\nThanks — we'll reply shortly to confirm a time.\n\n{page['title']}", page["contact_email"])
    return {"ok": True}


@app.get("/api/requests/count")
def requests_count():
    return {"pending": db.req_pending_count()}


@app.get("/api/requests")
def requests_list():
    base = _base_url()
    items = db.req_list()
    for it in items:
        d = it.get("data") or {}
        it["file_urls"] = [f"{base}/files/{fn}" for fn in d.get("files", [])]
    return {"items": items, "email_configured": notify.email_configured()}


class ReqEdit(BaseModel):
    caption: str = None
    run_at: str = None
    platform: str = None
    note: str = None


def _order_error(rid: str, msg: str):
    print(f"ORDER {rid} FAILED: {msg}", flush=True)
    db.req_update(rid, status="error", data={"error": msg[:300]})


def _approve_order(rid: str):
    r = db.req_get(rid)
    d = r["data"]
    try:
        files = d.get("files") or []
        kind, platform, caption = d["type"], d["platform"], d["caption"]
        entry_id = str(uuid.uuid4())
        meta = {"order_id": rid, "customer": r["name"]}
        video_fn = ""
        if kind == "reel":
            src = os.path.join(DOWNLOAD_DIR, files[0])
            storage.fetch_to(src, files[0])
            video_fn = f"{entry_id}_final.mp4"
            out = os.path.join(DOWNLOAD_DIR, video_fn)
            p = subprocess.run(["ffmpeg", "-y", "-i", src, "-vf", "scale='min(1080,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                                "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out],
                               capture_output=True, text=True, timeout=1200)
            if p.returncode != 0 or not os.path.exists(out):
                raise RuntimeError("Couldn't prepare the video: " + (p.stderr or "")[-200:])
            storage.upload_many_async([(out, video_fn)])
        else:
            meta["images"] = files
        db.save_history_entry(entry_id, f"Order: {r['name']}", video_fn, files[0] if files else "", "", caption, "", meta)
        db.update_history_platform_posts(entry_id, normalize_platform_posts({}, caption))
        when = _parse_when(r["run_at"])
        floor = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=3)
        bumped = when < floor
        if bumped:
            when = floor
        db.sched_upsert(entry_id, platform, when)
        db.req_update(rid, status="approved", history_id=entry_id, run_at=when, data={"time_bumped": bumped})
    except Exception as e:
        _order_error(rid, str(e))


@app.put("/api/requests/{rid}")
def request_edit(rid: str, req: ReqEdit):
    r = db.req_get(rid)
    if not r or r["kind"] != "order" or r["status"] != "pending":
        raise HTTPException(status_code=404, detail="That request can't be edited.")
    data = {}
    if req.caption is not None:
        data["caption"] = req.caption.strip()[:2200]
    if req.platform in _ORDER_PLATFORMS and (r["data"].get("type") == "reel" or req.platform == "instagram"):
        data["platform"] = req.platform
    when = _parse_when(req.run_at) if req.run_at else None
    db.req_update(rid, data=data, run_at=when, owner_note=req.note)
    return {"ok": True}


@app.post("/api/requests/{rid}/approve")
def request_approve(rid: str):
    r = db.req_get(rid)
    if not r or r["kind"] != "order" or r["status"] not in ("pending", "error"):
        raise HTTPException(status_code=404, detail="That request can't be approved.")
    db.req_update(rid, status="processing")
    threading.Thread(target=_approve_order, args=(rid,), daemon=True).start()
    return {"ok": True}


class ReqDecision(BaseModel):
    note: str = ""


@app.post("/api/requests/{rid}/decline")
def request_decline(rid: str, req: ReqDecision):
    r = db.req_get(rid)
    if not r or r["status"] not in ("pending", "error"):
        raise HTTPException(status_code=404, detail="That request can't be declined.")
    db.req_update(rid, status="declined", owner_note=req.note[:500])
    return {"ok": True}


class ReqEmail(BaseModel):
    subject: str
    body: str


@app.post("/api/requests/{rid}/email")
def request_email(rid: str, req: ReqEmail):
    """Sends the customer an email through SMTP when it's configured; otherwise the
    inbox falls back to opening the owner's own mail app (mailto) with the text filled in."""
    r = db.req_get(rid)
    if not r:
        raise HTTPException(status_code=404, detail="Unknown request.")
    sent = notify.send_email(r["email"], req.subject[:150], req.body[:4000], _bio_page()["contact_email"])
    if sent:
        db.req_update(rid, data={"emailed": True})
    return {"sent": sent}


_MEETING_LINK_KEY = "meeting_link"


def _make_meeting(start: _dt.datetime, minutes: int, topic: str):
    """Returns (join_url, how). A fresh Zoom meeting when the Zoom API is set up
    (ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID / ZOOM_CLIENT_SECRET, optional ZOOM_USER = your Zoom login email), otherwise the
    standing meeting link saved on the Bio link page."""
    acct, cid, sec = (os.environ.get(k) for k in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"))
    if acct and cid and sec:
        try:
            import requests as _rq
            tok = _rq.post("https://zoom.us/oauth/token", params={"grant_type": "account_credentials", "account_id": acct},
                           auth=(cid, sec), timeout=20)
            tok.raise_for_status()
            m = _rq.post(f"https://api.zoom.us/v2/users/{os.environ.get('ZOOM_USER') or _bio_page()['contact_email'] or 'me'}/meetings", headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
                         json={"topic": topic[:150], "type": 2, "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "duration": minutes, "timezone": "UTC", "settings": {"join_before_host": True}}, timeout=20)
            m.raise_for_status()
            return m.json()["join_url"], "zoom"
        except Exception as e:
            print(f"ZOOM MEETING FAILED: {e}", flush=True)
    link = (_bio_page().get("meeting_link") or "").strip()
    return (link or None), "link"


class InviteReq(BaseModel):
    start_iso: str
    minutes: int = 30
    note: str = ""


@app.post("/api/requests/{rid}/invite")
def request_invite(rid: str, req: InviteReq):
    """Emails the customer a calendar invite (.ics) with a video-call link."""
    r = db.req_get(rid)
    if not r or r["kind"] != "consult":
        raise HTTPException(status_code=404, detail="Unknown consultation request.")
    start = _parse_when(req.start_iso)
    if start < _dt.datetime.now(_dt.timezone.utc):
        raise HTTPException(status_code=400, detail="Pick a time in the future.")
    minutes = max(15, min(req.minutes, 120))
    page = _bio_page()
    link, how = _make_meeting(start, minutes, f"{page['title']} consultation with {r['name']}")
    if not link:
        raise HTTPException(status_code=400, detail="No video link yet — add a meeting link on the Bio link page (or set up Zoom), then try again.")
    if not notify.email_configured():
        raise HTTPException(status_code=400, detail="Sending invites needs the email settings on Render (SMTP_HOST, SMTP_USER, SMTP_PASS).")
    ok = notify.send_invite(r["email"], r["name"], f"{page['title']} consultation", start, minutes, link, page["contact_email"], req.note)
    if not ok:
        raise HTTPException(status_code=502, detail="The invite couldn't be sent — check the email settings.")
    db.req_update(rid, status="confirmed", run_at=start, data={"meeting_link": link, "minutes": minutes, "invited": True})
    return {"ok": True, "link": link, "how": how}


@app.post("/api/requests/{rid}/done")
def request_done(rid: str):
    """Marks a consultation as handled."""
    r = db.req_get(rid)
    if not r:
        raise HTTPException(status_code=404, detail="Unknown request.")
    if r["status"] != "done":
        db.req_update(rid, status="done", data={"prev_status": r["status"]})
    return {"ok": True}


@app.post("/api/requests/{rid}/reopen")
def request_reopen(rid: str):
    """Undoes 'Mark handled' — puts it back to what it was (needs review / invite sent)."""
    r = db.req_get(rid)
    if not r:
        raise HTTPException(status_code=404, detail="Unknown request.")
    if r["status"] == "done":
        prev = (r.get("data") or {}).get("prev_status")
        if prev not in ("pending", "confirmed", "approved"):
            prev = "confirmed" if (r.get("data") or {}).get("invited") else "pending"
        db.req_update(rid, status=prev)
    return {"ok": True}


@app.delete("/api/requests/{rid}")
def request_delete(rid: str):
    r = db.req_get(rid)
    if r and r["status"] in ("declined", "done", "error"):
        db.req_update(rid, status="archived")
    return {"ok": True}


@app.get("/api/audit")
def api_audit():
    return {"items": db.audit_list()}


@app.get("/api/ai-usage")
def api_ai_usage():
    return db.ai_usage_summary()


@app.get("/api/analytics/summary")
def api_analytics_summary(tz: int = 0, days: int = 30):
    s = insights.summary(tz, max(7, min(days, 180)))
    if s.get("posts") and s.get("posts") > s.get("tagged", 0):
        threading.Thread(target=insights.tag_pending, daemon=True).start()
    return s


@app.get("/api/analytics/export.csv")
def api_analytics_export():
    return Response(insights.export_csv(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=wokevision-posts.csv"})


@app.get("/api/insights/{platform}/views")
def api_insights_views(platform: str, start: int, end: int):
    if platform not in PLATFORM_MODULES:
        raise HTTPException(status_code=404, detail="Unknown platform.")
    try:
        return insights.range_views(platform, start, end)
    except Exception as e:
        return {"views": None, "error": str(e)[:200]}


@app.get("/api/insights/{platform}")
def api_insights(platform: str, refresh: bool = False):
    if platform not in PLATFORM_MODULES:
        raise HTTPException(status_code=404, detail="Unknown platform.")
    return insights.get(platform, force=refresh)


@app.get("/api/insights-history")
def api_insights_history():
    return {"history": insights.history()}


@app.get("/api/insights")
def api_insights_all(refresh: bool = False):
    results = {}
    def _one(p):
        results[p] = insights.get(p, force=refresh)
    ts = [threading.Thread(target=_one, args=(p,)) for p in PLATFORM_ORDER]
    for t in ts: t.start()
    for t in ts: t.join()
    return {"platforms": [results[p] for p in PLATFORM_ORDER], "history": insights.history()}


@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


class PasskeyRegisterStart(BaseModel):
    setup_code: str


class PasskeyRegisterFinish(BaseModel):
    token: str
    setup_code: str
    credential: dict


class PasskeyLoginFinish(BaseModel):
    token: str
    credential: dict


@app.post("/auth/register/options")
def auth_register_options(req: PasskeyRegisterStart, request: Request):
    ip = _client_ip(request)
    if _login_blocked(ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")
    try:
        return auth.start_registration(req.setup_code)
    except auth.AuthError as e:
        _LOGIN_FAILS.setdefault(ip, []).append(time.time())
        db.audit("login_failed", f"{ip} (setup code)")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/auth/register/verify")
def auth_register_verify(req: PasskeyRegisterFinish):
    try:
        auth.finish_registration(req.token, req.setup_code, req.credential)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.SESSION_COOKIE, auth.make_session_token(),
        max_age=auth.SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="lax",
    )
    return resp


_LOGIN_FAILS = {}   # ip -> [timestamps]


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()


def _login_blocked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 900]
    _LOGIN_FAILS[ip] = recent
    return len(recent) >= 8


@app.post("/auth/login/options")
def auth_login_options(request: Request):
    if _login_blocked(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")
    try:
        return auth.start_login()
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/auth/login/verify")
def auth_login_verify(req: PasskeyLoginFinish, request: Request):
    ip = _client_ip(request)
    if _login_blocked(ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")
    try:
        session_token = auth.finish_login(req.token, req.credential)
    except auth.AuthError as e:
        _LOGIN_FAILS.setdefault(ip, []).append(time.time())
        db.audit("login_failed", ip)
        raise HTTPException(status_code=400, detail=str(e))
    db.audit("login", ip)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.SESSION_COOKIE, session_token,
        max_age=auth.SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="lax",
    )
    return resp


@app.post("/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/health")
def health():
    return {"status": "ok"}


# TikTok's URL-prefix domain verification expects a specific plaintext file
# at the site root, containing a token it gives us (see Developer Portal ->
# App -> URL properties -> Verify). Static files are otherwise only served
# under /static, so this one gets its own tiny route rather than a broader
# root-level static mount.
@app.get("/tiktokf2TEyaKWItLVEN7IU6Sr0Fyd4eBclual.txt")
def tiktok_site_verification():
    return PlainTextResponse("tiktok-developers-site-verification=f2TEyaKWItLVEN7IU6Sr0Fyd4eBclual")


# Second verification file -- issued when verifying the https://wokevision.com/
# URL prefix against the Production TikTok app (the token above was issued
# for the old onrender.com URL / a different app). Kept alongside the first
# rather than replacing it, since TikTok doesn't let you remove a verified
# property from its side and we don't need to either.
@app.get("/tiktokXoc4Y47fr98En3040kRaFWp20XNF2taG.txt")
def tiktok_site_verification_2():
    return PlainTextResponse("tiktok-developers-site-verification=Xoc4Y47fr98En3040kRaFWp20XNF2taG")


@app.post("/process")
def process(req: ProcessRequest):
    job_id = str(uuid.uuid4())
    final_source_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
    with JOBS_LOCK:
        JOBS[job_id] = {"stage": "queued", "stage_label": "Queued", "progress": 0.0, "status": "running", "angle": (req.angle or "").strip()[:1500], "wm_token": req.wm_token, "wm_pos": req.wm_pos, "campaign_id": req.campaign_id}
    threading.Thread(
        target=_run_download_then_pipeline, args=(job_id, req.url, final_source_path), daemon=True
    ).start()
    return {"job_id": job_id}


class BatchRequest(BaseModel):
    urls: list
    angle: str = ""
    wm_token: str = ""
    wm_pos: str = "right"
    campaign_id: str = ""


def _run_batch(items):
    for job_id, url in items:
        path = os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
        try:
            _run_download_then_pipeline(job_id, url, path)
        except Exception as e:
            print(f"BATCH ITEM FAILED {url}: {e}", flush=True)
            _set_job(job_id, stage="error", stage_label="Error", status="error", error=str(e))


@app.post("/api/batch")
def process_batch(req: BatchRequest):
    """Several links at once: every job exists immediately (so they all show
    in History as queued) and they're worked through one at a time so the
    server isn't hammered."""
    urls = [u.strip() for u in req.urls if isinstance(u, str) and u.strip().startswith(("http://", "https://"))][:15]
    if not urls:
        raise HTTPException(status_code=400, detail="No valid links found.")
    items = []
    with JOBS_LOCK:
        for u in urls:
            jid = str(uuid.uuid4())
            JOBS[jid] = {"stage": "queued", "stage_label": "Queued", "progress": 0.0, "status": "running",
                         "angle": (req.angle or "").strip()[:1500], "wm_token": req.wm_token, "wm_pos": req.wm_pos, "campaign_id": req.campaign_id}
            items.append((jid, u))
    threading.Thread(target=_run_batch, args=(items,), daemon=True).start()
    return {"job_ids": [j for j, _ in items]}


@app.post("/process-file")
async def process_file(file: UploadFile = File(...), angle: str = Form(""), wm_token: str = Form(""), wm_pos: str = Form("right"), campaign_id: str = Form("")):
    """Direct upload path: skips the download step entirely. Use this when a
    link can't be fetched automatically (most often YouTube, when the host's
    IP is being rate-limited) -- download the video yourself and upload the
    file here instead."""
    job_id = str(uuid.uuid4())
    final_source_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
    with JOBS_LOCK:
        JOBS[job_id] = {"stage": "uploading", "stage_label": "Receiving upload", "progress": 0.0, "status": "running", "angle": (angle or "").strip()[:1500], "wm_token": wm_token, "wm_pos": wm_pos, "campaign_id": campaign_id}

    try:
        with open(final_source_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        await file.close()

    if not os.path.exists(final_source_path) or os.path.getsize(final_source_path) == 0:
        _set_job(job_id, stage="error", stage_label="Error", status="error", error="Upload failed: no file data received.")
        return {"job_id": job_id}

    meta = {"title": os.path.splitext(file.filename or "")[0], "description": "", "method": "direct upload"}
    threading.Thread(target=_run_pipeline, args=(job_id, final_source_path, meta), daemon=True).start()
    return {"job_id": job_id}


@app.get("/sw.js")
def sw_js():
    return FileResponse("static/sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest_file():
    return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json")


@app.post("/share-target")
async def share_target(video: UploadFile = File(None), title: str = Form(""), text: str = Form(""), url: str = Form("")):
    """Android 'Share to WokeVision' from any app: a shared video file or a
    shared link starts a new edit and lands in the editor."""
    if video is not None and video.filename:
        res = await process_file(file=video, angle="", wm_token="", wm_pos="right", campaign_id="")
        return RedirectResponse(f"/editor#job={res['job_id']}", status_code=303)
    m = re.search(r"https?://\S+", f"{url} {text}")
    if not m:
        return RedirectResponse("/editor", status_code=303)
    link = m.group(0).rstrip(").,")
    job_id = str(uuid.uuid4())
    path = os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
    with JOBS_LOCK:
        JOBS[job_id] = {"stage": "queued", "stage_label": "Queued", "progress": 0.0, "status": "running", "angle": "", "wm_token": "", "wm_pos": "right", "campaign_id": ""}
    threading.Thread(target=_run_download_then_pipeline, args=(job_id, link, path), daemon=True).start()
    return RedirectResponse(f"/editor#job={job_id}", status_code=303)


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        row = None
        try:
            row = db.job_get(job_id)
        except Exception:
            pass
        if row and row.get("status") == "error":
            return {"status": "error", "stage": "error", "stage_label": "Error", "error": row.get("error") or "This job was interrupted."}
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job


@app.post("/jobs/{job_id}/regenerate-video")
def regenerate_video(job_id: str):
    """Reprocesses the video with a new on-screen (top-of-frame) caption,
    reusing the kept source file -- no re-download/re-upload needed."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        if job.get("status") != "done":
            raise HTTPException(status_code=409, detail="Job isn't finished yet.")
        job["status"] = "running"
        job["stage"] = "captioning"
        job["stage_label"] = "Writing new caption"
        job["progress"] = STAGE_WEIGHTS["captioning"][0]
        job["_stage_started_at"] = time.time()
        job["eta_seconds"] = _estimate_eta_seconds(job)
    threading.Thread(target=_run_regenerate_video, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


@app.post("/jobs/{job_id}/regenerate-caption")
def regenerate_caption(job_id: str):
    """Refreshes just the posting caption (the text used when sharing the
    finished video to Instagram/YouTube/X/TikTok/Threads/Facebook) -- no
    re-render, so this is near-instant."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        if job.get("status") != "done":
            raise HTTPException(status_code=409, detail="Job isn't finished yet.")
    _run_regenerate_caption(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job.get("stage") == "error":
        raise HTTPException(status_code=500, detail=job.get("error", "Something went wrong."))
    return {"posting_caption": job.get("posting_caption", ""), "platform_posts": job.get("platform_posts") or {},
            "result": job.get("result")}


class PostingCaptionRequest(BaseModel):
    caption: str


class PlatformPostsRequest(BaseModel):
    posts: dict


class PlatformPostsGenerateRequest(BaseModel):
    platform: str | None = None


def _done_job(job_id: str) -> dict:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        if job.get("status") != "done":
            raise HTTPException(status_code=409, detail="Job isn't finished yet.")
        return job


@app.put("/jobs/{job_id}/posting-caption")
def save_posting_caption(job_id: str, req: PostingCaptionRequest):
    """Saves the user's edit of the master posting caption (on the job and in
    History) so it survives a reload, a redeploy and a device switch."""
    job = _done_job(job_id)
    caption = req.caption or ""
    meta = job.get("meta", {})
    _set_job(
        job_id, posting_caption=caption,
        result=_result_for(job_id, meta, job.get("on_screen_caption", ""), caption, job.get("platform_posts")),
    )
    try:
        db.update_history_caption(job_id, posting_caption=caption)
    except Exception as e:
        print(f"HISTORY UPDATE FAILED ({job_id}): {e}", flush=True)
    return {"ok": True}


@app.put("/jobs/{job_id}/platform-posts")
def save_platform_posts(job_id: str, req: PlatformPostsRequest):
    """Saves the user's edits to the per-platform versions. Stored as the
    user typed them (only structurally validated and hard-capped), so
    publishing sends exactly what they saw in the editor."""
    job = _done_job(job_id)
    merged = {**(job.get("platform_posts") or {})}
    for pid, val in (req.posts or {}).items():
        if pid in PLATFORM_IDS and isinstance(val, dict):
            merged[pid] = val
    posts = normalize_platform_posts(merged, job.get("posting_caption", ""))
    meta = job.get("meta", {})
    _set_job(
        job_id, platform_posts=posts,
        result=_result_for(job_id, meta, job.get("on_screen_caption", ""), job.get("posting_caption", ""), posts),
    )
    _save_platform_posts(job_id, posts)
    return {"platform_posts": posts}


@app.get("/jobs/{job_id}/versions")
def job_versions(job_id: str):
    return {"items": db.caption_versions(job_id)}


@app.post("/jobs/{job_id}/platform-posts/generate")
def generate_platform_posts_route(job_id: str, req: PlatformPostsGenerateRequest):
    """(Re)writes the per-platform versions from the master caption --
    all platforms, or just `platform` (leaving the others as the user has
    them). Also used to fill in History entries that predate this feature."""
    job = _done_job(job_id)
    only = req.platform if req.platform in PLATFORM_IDS else None
    posts = generate_platform_posts(
        job.get("transcript", ""), job.get("meta", {}), job.get("on_screen_caption", ""),
        job.get("posting_caption", ""), only=only, current=job.get("platform_posts"),
    )
    meta = job.get("meta", {})
    _set_job(
        job_id, platform_posts=posts,
        result=_result_for(job_id, meta, job.get("on_screen_caption", ""), job.get("posting_caption", ""), posts),
    )
    _save_platform_posts(job_id, posts)
    return {"platform_posts": posts}


class AngleRequest(BaseModel):
    angle: str = ""


@app.put("/jobs/{job_id}/angle")
def set_angle(job_id: str, req: AngleRequest):
    """Saves the per-video 'what this clip is about / how to treat it' note.
    It lives in the job's meta, so every later caption (re)generation sees
    it, and it is persisted with the history entry."""
    job = _done_job(job_id)
    angle = (req.angle or "").strip()[:1500]
    meta = {**(job.get("meta") or {}), "angle": angle}
    _set_job(job_id, meta=meta, angle=angle)
    try:
        db.update_history_meta(job_id, meta)
    except Exception as e:
        print("ANGLE SAVE FAILED:", e, flush=True)
    return {"angle": angle}


class OnScreenCaptionRequest(BaseModel):
    on_screen_caption: str


@app.post("/jobs/{job_id}/set-on-screen-caption")
def set_on_screen_caption(job_id: str, req: OnScreenCaptionRequest):
    """User-typed on-screen (burned-in) caption edit -- the text is already
    given, so this skips AI generation entirely and goes straight to the
    fast render path (see _render_with_caption): it reuses the cached staged
    video and only re-applies the caption overlay, instead of reprocessing
    the whole video from the original source."""
    text = (req.on_screen_caption or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="On-screen caption can't be empty.")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        if job.get("status") != "done":
            raise HTTPException(status_code=409, detail="Job isn't finished yet.")
        job["status"] = "running"
        job["stage"] = "rendering"
        job["stage_label"] = "Updating caption"
        job["progress"] = STAGE_WEIGHTS["rendering"][0]
        job["_stage_started_at"] = time.time()
        job["eta_seconds"] = _estimate_eta_seconds(job)
    threading.Thread(target=_run_set_on_screen_caption, args=(job_id, text), daemon=True).start()
    return {"job_id": job_id}


@app.post("/jobs/{job_id}/hooks")
def job_hooks(job_id: str):
    job = get_job(job_id)
    hooks = generate_hook_options(job.get("transcript", ""), job.get("meta", {}), job.get("on_screen_caption", ""))
    if not hooks:
        raise HTTPException(status_code=502, detail="Couldn't generate hook options right now.")
    return {"hooks": hooks}


@app.post("/jobs/{job_id}/risk-check")
def job_risk_check(job_id: str):
    job = get_job(job_id)
    texts = {"on-screen": job.get("on_screen_caption", ""), "caption": job.get("posting_caption", "")}
    for k, v in (job.get("platform_posts") or {}).items():
        if isinstance(v, dict):
            for fk, fv in v.items():
                if isinstance(fv, str) and fv.strip() and fk in ("caption", "text", "title", "description"):
                    texts[f"{k} {fk}"] = fv
    try:
        return risk_check(job.get("transcript", ""), texts)
    except Exception as e:
        print(f"RISK CHECK FAILED: {e}", flush=True)
        raise HTTPException(status_code=502, detail="Couldn't run the risk check right now.")


class TranslateRequest(BaseModel):
    lines: list
    language: str


@app.post("/api/translate")
def api_translate(req: TranslateRequest):
    lines = [str(x)[:300] for x in req.lines][:200]
    if not lines:
        return {"lines": []}
    try:
        return {"lines": translate_lines(lines, req.language[:40])}
    except Exception as e:
        print(f"TRANSLATE FAILED: {e}", flush=True)
        raise HTTPException(status_code=502, detail="Translation failed, try again.")


@app.get("/files/{filename}")
def get_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, os.path.basename(filename))
    if not os.path.exists(path):
        # Local disk is wiped on every redeploy; pull from durable storage.
        storage.fetch_to(path, os.path.basename(filename))
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    _ext = os.path.splitext(path)[1].lower()
    _mt = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".mov": "video/quicktime"}.get(_ext, "video/mp4")
    return FileResponse(path, media_type=_mt, headers={"Content-Disposition": "inline"})


# --- Platform connections (Instagram, and eventually Threads/YouTube/TikTok/X) ---

@app.get("/connections")
def list_connections():
    """Status for every platform the UI shows -- wired-up ones get a real
    live probe (db.configured() gates all of this gracefully: if Neon/the
    encryption key aren't set up yet, every platform just reports as
    unavailable rather than erroring)."""
    out = []
    for platform in PLATFORM_ORDER:
        module = PLATFORM_MODULES.get(platform)
        if not module:
            out.append({
                "platform": platform, "label": PLATFORM_LABELS[platform],
                "available": False, "connected": False, "ok": False,
                "status_label": "Coming soon",
            })
            continue
        if not db.configured():
            out.append({
                "platform": platform, "label": PLATFORM_LABELS[platform],
                "available": True, "connected": False, "ok": False,
                "status_label": "Storage not set up",
            })
            continue
        try:
            status = module.check_status()
        except Exception as e:
            status = {"connected": False, "ok": False, "label": "Connection error"}
            print(f"CONNECTION STATUS CHECK FAILED ({platform}): {e}", flush=True)
        out.append({
            "platform": platform, "label": PLATFORM_LABELS[platform],
            "available": True, "connected": status["connected"], "ok": status["ok"],
            "status_label": status["label"],
        })
    return {"connections": out}


@app.get("/connections/{platform}/start")
def connect_start(platform: str):
    module = PLATFORM_MODULES.get(platform)
    if not module:
        raise HTTPException(status_code=404, detail="Unknown or not-yet-supported platform.")
    if not db.configured():
        raise HTTPException(status_code=503, detail="Connection storage isn't set up yet (Neon database not configured).")
    try:
        state = _new_oauth_state(platform)
        url = module.get_auth_url(state)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    return RedirectResponse(url)


@app.get("/connections/{platform}/callback")
def connect_callback(platform: str, request: Request):
    module = PLATFORM_MODULES.get(platform)
    if not module:
        raise HTTPException(status_code=404, detail="Unknown or not-yet-supported platform.")
    params = request.query_params
    if params.get("error"):
        msg = params.get("error_description", params.get("error"))
        return RedirectResponse(f"/editor?connect_error={_url_quote(msg)}")
    code = params.get("code")
    state = params.get("state")
    if not code or not _check_oauth_state(state or ""):
        return RedirectResponse(f"/editor?connect_error={_url_quote('Invalid or expired connection attempt, please try again.')}")
    try:
        module.handle_callback(code)
    except Exception as e:
        return RedirectResponse(f"/editor?connect_error={_url_quote(str(e))}")
    return RedirectResponse(f"/editor?connected={platform}")


@app.post("/connections/{platform}/disconnect")
def connect_disconnect(platform: str):
    if platform not in PLATFORM_MODULES:
        raise HTTPException(status_code=404, detail="Unknown or not-yet-supported platform.")
    db.delete_connection(platform)
    return {"ok": True}


class PublishRequest(BaseModel):
    job_id: str
    platforms: list[str]


def _run_publish(publish_job_id: str, history_id: str, platforms: list, video_url: str, caption: str,
                 platform_posts: dict = None):
    """Runs one publish_video() call per platform, each in its own thread,
    so a slow platform (or one that's genuinely stuck) never blocks the
    others -- and the job's per-platform results are visible to a poller
    as soon as each one finishes, rather than all-at-once at the end."""
    import datetime
    def _do(platform):
        module = PLATFORM_MODULES.get(platform)
        if not module:
            result = {"status": "done", "ok": False, "error": "This platform isn't connected yet."}
        else:
            try:
                outcome = module.publish_video(video_url, caption, post=(platform_posts or {}).get(platform))
                result = {"status": "done", "ok": True, **outcome}
            except Exception as e:
                print(f"PUBLISH FAILED ({platform}): {e}", flush=True)
                result = {"status": "done", "ok": False, "error": str(e)}
        with JOBS_LOCK:
            PUBLISH_JOBS[publish_job_id]["results"][platform] = result
        try:
            db.update_history_publish_results(history_id, {
                platform: {**result, "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            })
        except Exception as e:
            print(f"HISTORY PUBLISH-RESULT SAVE FAILED ({history_id}/{platform}): {e}", flush=True)

    workers = [threading.Thread(target=_do, args=(p,), daemon=True) for p in platforms]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    with JOBS_LOCK:
        PUBLISH_JOBS[publish_job_id]["status"] = "done"


@app.post("/publish")
def publish(req: PublishRequest):
    with JOBS_LOCK:
        job = JOBS.get(req.job_id)
    if not job or job.get("status") != "done" or not job.get("result"):
        raise HTTPException(status_code=409, detail="That video isn't ready to publish yet.")

    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    video_url = job["result"]["video_url"]
    if base_url and video_url.startswith("/"):
        video_url = base_url + video_url
    caption = job.get("posting_caption") or job["result"].get("caption") or ""
    platform_posts = normalize_platform_posts(job.get("platform_posts") or {}, caption)
    if (job.get("meta") or {}).get("paid_promo"):
        platform_posts = {p: apply_disclosure(p, v) for p, v in platform_posts.items()}
    _cv = (job.get("meta") or {}).get("cover_ms")
    if _cv is not None:
        platform_posts = {p: ({**v, "cover_ms": _cv} if isinstance(v, dict) else v) for p, v in platform_posts.items()}

    publish_job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        PUBLISH_JOBS[publish_job_id] = {
            "status": "running",
            "results": {platform: {"status": "pending"} for platform in req.platforms},
        }
    threading.Thread(
        target=_run_publish, args=(publish_job_id, req.job_id, req.platforms, video_url, caption, platform_posts), daemon=True
    ).start()
    return {"publish_job_id": publish_job_id}


@app.get("/publish/{publish_job_id}")
def get_publish_job(publish_job_id: str):
    with JOBS_LOCK:
        job = PUBLISH_JOBS.get(publish_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown publish job id")
    return job


# --- History: every past edit, clickable back into the editor --------------

@app.get("/history")
def list_history():
    entries = db.list_history()
    stored_keys = storage.list_keys()
    out = []
    for e in entries:
        out.append({
            "id": str(e["id"]),
            "created_at": e["created_at"].isoformat() if e.get("created_at") else None,
            "title": e.get("title"),
            "on_screen_caption": e.get("on_screen_caption"),
            "video_available": bool(e.get("video_filename")) and (
                os.path.exists(os.path.join(DOWNLOAD_DIR, e["video_filename"])) or e["video_filename"] in stored_keys
            ),
            "publish_results": e.get("publish_results") or {},
        })
    return {"history": out}


@app.get("/history/{entry_id}")
def get_history(entry_id: str):
    e = db.get_history_entry(entry_id)
    if not e:
        raise HTTPException(status_code=404, detail="Unknown history entry")
    video_path = os.path.join(DOWNLOAD_DIR, e["video_filename"]) if e.get("video_filename") else None
    source_path = os.path.join(DOWNLOAD_DIR, e["source_filename"]) if e.get("source_filename") else None
    stored_keys = storage.list_keys()
    video_available = bool(video_path and (os.path.exists(video_path) or e["video_filename"] in stored_keys))
    source_available = bool(source_path and (os.path.exists(source_path) or e["source_filename"] in stored_keys))
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {
        "id": str(e["id"]),
        "created_at": e["created_at"].isoformat() if e.get("created_at") else None,
        "title": e.get("title"),
        "on_screen_caption": e.get("on_screen_caption"),
        "posting_caption": e.get("posting_caption"),
        "platform_posts": normalize_platform_posts(e.get("platform_posts") or {}, e.get("posting_caption") or ""),
        "video_available": video_available,
        "source_available": source_available,
        "video_url": f"{base_url}/files/{e['video_filename']}" if video_available else None,
        "publish_results": e.get("publish_results") or {},
        "meta": {"angle": (e.get("meta") or {}).get("angle", "")},
        **{k: v for k, v in _result_for(str(e["id"]), e.get("meta") or {}, "", "").items()
           if k in ("cues", "captions_on", "watermark", "paid_promo", "captions_style", "credit", "campaign_id", "post", "cover_ms", "campaign_brief", "crop")},
    }


@app.post("/api/watermark")
async def upload_watermark(file: UploadFile = File(...)):
    """Stores a campaign watermark ahead of processing; the returned token is
    passed to /process or /process-file."""
    raw = await file.read()
    await file.close()
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Watermark must be an image under 8MB.")
    token = uuid.uuid4().hex
    path = os.path.join(DOWNLOAD_DIR, f"wm_{token}.png")
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGBA")
        im.thumbnail((1200, 1200))
        im.save(path, "PNG")
    except Exception:
        raise HTTPException(status_code=400, detail="That file isn't a readable image (use PNG, JPG or WebP).")
    storage.upload_many_async([(path, os.path.basename(path))])
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {"token": token, "url": f"{base_url}/files/wm_{token}.png"}


class WmSave(BaseModel):
    token: str
    name: str
    pos: str = "right"


@app.get("/api/watermarks")
def wm_list():
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {"items": [{**r, "url": f"{base_url}/files/wm_{r['token']}.png"} for r in db.wm_lib_list()]}


@app.post("/api/watermarks")
def wm_save(req: WmSave):
    if not _HEX.match(req.token.lower()):
        raise HTTPException(status_code=400, detail="Bad token.")
    name = (req.name or "").strip()[:60]
    if not name:
        raise HTTPException(status_code=400, detail="Give it a name.")
    db.wm_lib_save(req.token.lower(), name, req.pos if req.pos in render.WM_POSITIONS else "right")
    return {"ok": True}


@app.delete("/api/watermarks/{token}")
def wm_delete(token: str):
    if not _HEX.match(token.lower()):
        raise HTTPException(status_code=400, detail="Bad token.")
    db.wm_lib_delete(token.lower())
    return {"ok": True}


def _begin_rerender(job_id: str, label: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        if job.get("status") != "done":
            raise HTTPException(status_code=409, detail="Job isn't finished yet.")
        job["status"] = "running"
        job["stage"] = "rendering"
        job["stage_label"] = label
        job["progress"] = STAGE_WEIGHTS["rendering"][0]
        job["_stage_started_at"] = time.time()
        job["eta_seconds"] = _estimate_eta_seconds(job)
        return job


class CreditRequest(BaseModel):
    handle: str = ""


@app.put("/jobs/{job_id}/credit")
def set_credit(job_id: str, req: CreditRequest):
    handle = req.handle.strip().lstrip("@")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        job["meta"] = {**(job.get("meta") or {}), "credit_override": handle}
        meta = job["meta"]
        caption = job.get("posting_caption") or ""
        posts = normalize_platform_posts(job.get("platform_posts") or {}, caption)
        posts = set_credit_in_posts(posts, caption_credit_handle(meta))
        job["platform_posts"] = posts
    try:
        db.update_history_meta(job_id, meta)
        _save_platform_posts(job_id, posts)
    except Exception as e:
        print(f"CREDIT SAVE FAILED: {e}", flush=True)
    return {"platform_posts": posts, "credit": caption_credit_handle(meta)}


class PaidPromoRequest(BaseModel):
    on: bool


@app.put("/jobs/{job_id}/paid-promo")
def set_paid_promo(job_id: str, req: PaidPromoRequest):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        job["meta"] = {**(job.get("meta") or {}), "paid_promo": bool(req.on)}
        meta = job["meta"]
    try:
        db.update_history_meta(job_id, meta)
    except Exception as e:
        print(f"PAID PROMO SAVE FAILED: {e}", flush=True)
    return {"ok": True}


class FinishRequest(BaseModel):
    trim_start: float = 0
    trim_end: float = 0
    silence: bool = False
    loudness: bool = False
    music: str = ""
    music_vol: float = 0.25


@app.post("/api/music")
async def upload_music(file: UploadFile = File(...)):
    """Background track for the finish step; normalised to mp3 and kept in storage."""
    raw = await file.read()
    await file.close()
    if not raw or len(raw) > 30 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Music must be an audio file under 30MB.")
    token = uuid.uuid4().hex
    src = os.path.join(DOWNLOAD_DIR, f"music_{token}.src")
    out = os.path.join(DOWNLOAD_DIR, f"music_{token}.mp3")
    with open(src, "wb") as f:
        f.write(raw)
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-t", "600", "-c:a", "libmp3lame", "-b:a", "128k", out],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not os.path.exists(out):
            raise HTTPException(status_code=400, detail="That doesn't look like an audio file (try MP3, M4A or WAV).")
    finally:
        if os.path.exists(src):
            os.remove(src)
    storage.upload_many_async([(out, os.path.basename(out))])
    return {"token": token, "name": os.path.splitext(file.filename or "track")[0][:60]}


@app.put("/jobs/{job_id}/finish")
def set_finish(job_id: str, req: FinishRequest):
    """Trim / cut the silences / even out the loudness of the finished video.
    Saved on the job so later caption edits keep it, then re-rendered."""
    if req.trim_end and req.trim_start and req.trim_end <= req.trim_start + 0.5:
        raise HTTPException(status_code=400, detail="The trim end has to be after the start.")
    post = {"trim_start": max(0.0, req.trim_start), "trim_end": max(0.0, req.trim_end), "silence": req.silence, "loudness": req.loudness,
            "music": req.music if _HEX.match((req.music or "").lower()) else "", "music_vol": max(0.05, min(1.0, req.music_vol)),
            "music_name": ""}
    job = _begin_rerender(job_id, "Applying trim & polish")
    with JOBS_LOCK:
        job["meta"] = {**(job.get("meta") or {}), "post": post}
        meta = job["meta"]
    try:
        db.update_history_meta(job_id, meta)
    except Exception as e:
        print(f"FINISH SAVE FAILED: {e}", flush=True)
    threading.Thread(target=_run_set_on_screen_caption, args=(job_id, job.get("on_screen_caption", "")), daemon=True).start()
    return {"ok": True}


class CoverRequest(BaseModel):
    ms: int = 1000


@app.put("/jobs/{job_id}/cover")
def set_cover(job_id: str, req: CoverRequest):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        job["meta"] = {**(job.get("meta") or {}), "cover_ms": max(0, min(req.ms, 600000))}
        meta = job["meta"]
    try:
        db.update_history_meta(job_id, meta)
    except Exception as e:
        print(f"COVER SAVE FAILED: {e}", flush=True)
    return {"ok": True}


class WordModel(BaseModel):
    w: str
    start: float
    end: float


class CueModel(BaseModel):
    start: float
    end: float
    text: str
    words: list[WordModel] | None = None


class ClosedCaptionsRequest(BaseModel):
    cues: list[CueModel]
    enabled: bool = True
    style: str = "classic"


@app.put("/jobs/{job_id}/closed-captions")
def set_closed_captions(job_id: str, req: ClosedCaptionsRequest):
    cues = []
    for c in req.cues:
        if not c.text.strip() or c.end <= c.start:
            continue
        item = {"start": round(c.start, 2), "end": round(c.end, 2), "text": c.text.strip()[:200]}
        if c.words and len(c.words) == len(c.text.split()):
            item["words"] = [{"w": w.w, "start": w.start, "end": w.end} for w in c.words]
        cues.append(item)
    cues.sort(key=lambda c: c["start"])
    job = _begin_rerender(job_id, "Updating captions")
    with JOBS_LOCK:
        job["meta"] = {**(job.get("meta") or {}), "cues": cues, "captions_on": bool(req.enabled and cues),
                                                   "captions_style": req.style if req.style in ("classic", "highlight") else "classic"}
    threading.Thread(target=_run_set_on_screen_caption,
                     args=(job_id, job.get("on_screen_caption", "")), daemon=True).start()
    return {"ok": True}


@app.post("/jobs/{job_id}/watermark")
async def set_job_watermark(job_id: str, file: UploadFile = File(None), pos: str = Form("right"), remove: str = Form(""), token: str = Form("")):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    meta = dict(job.get("meta") or {})
    if remove:
        meta.pop("wm", None)
    else:
        pos = pos if pos in render.WM_POSITIONS else "right"
        wm = dict(meta.get("wm") or {})
        if file is not None and file.filename:
            raw = await file.read()
            await file.close()
            try:
                from PIL import Image
                import io
                im = Image.open(io.BytesIO(raw)).convert("RGBA")
                im.thumbnail((1200, 1200))
                fn = f"{job_id}_wm.png"
                im.save(os.path.join(DOWNLOAD_DIR, fn), "PNG")
                storage.upload_many_async([(os.path.join(DOWNLOAD_DIR, fn), fn)])
                wm["file"] = fn
            except Exception:
                raise HTTPException(status_code=400, detail="That file isn't a readable image.")
        if token and _HEX.match(token.lower()):
            src = os.path.join(DOWNLOAD_DIR, f"wm_{token.lower()}.png")
            if not os.path.exists(src):
                storage.fetch_to(src, os.path.basename(src))
            if os.path.exists(src):
                fn = f"{job_id}_wm.png"
                shutil.copyfile(src, os.path.join(DOWNLOAD_DIR, fn))
                storage.upload_many_async([(os.path.join(DOWNLOAD_DIR, fn), fn)])
                wm["file"] = fn
        if not wm.get("file"):
            raise HTTPException(status_code=400, detail="Choose a watermark image first.")
        wm["pos"] = pos
        meta["wm"] = wm
    job = _begin_rerender(job_id, "Updating watermark")
    with JOBS_LOCK:
        job["meta"] = meta
    threading.Thread(target=_run_set_on_screen_caption,
                     args=(job_id, job.get("on_screen_caption", "")), daemon=True).start()
    return {"ok": True}


@app.post("/history/{entry_id}/reopen")
def reopen_history(entry_id: str):
    """Re-seeds JOBS[entry_id] from the stored history row (if it isn't
    already an active in-memory job -- e.g. after a server restart), so the
    existing /jobs, /publish, and regenerate-* routes all work against a
    history entry exactly as they would against a freshly-processed one."""
    with JOBS_LOCK:
        already_loaded = entry_id in JOBS
    if already_loaded:
        return {"ok": True}

    e = db.get_history_entry(entry_id)
    if not e:
        raise HTTPException(status_code=404, detail="Unknown history entry")

    source_path = os.path.join(DOWNLOAD_DIR, e["source_filename"]) if e.get("source_filename") else None
    if not source_path or not (os.path.exists(source_path) or (storage.configured() and e.get("source_filename") in storage.list_keys())):
        source_path = None

    # The staged (pre-caption) composite isn't tracked in the database --
    # it's a disk-only cache -- but it's always named {id}_staged.mp4 when
    # it exists, so it can just be looked up directly.
    staged_path = os.path.join(DOWNLOAD_DIR, f"{entry_id}_staged.mp4")
    if not os.path.exists(staged_path) and not (storage.configured() and os.path.basename(staged_path) in storage.list_keys()):
        staged_path = None

    meta = e.get("meta") or {}
    on_screen_caption = e.get("on_screen_caption") or ""
    posting_caption = e.get("posting_caption") or ""
    platform_posts = normalize_platform_posts(e.get("platform_posts") or {}, posting_caption)
    with JOBS_LOCK:
        JOBS[entry_id] = {
            "stage": "done", "stage_label": "Done", "progress": 1.0, "status": "done", "eta_seconds": 0,
            "transcript": e.get("transcript") or "",
            "meta": meta,
            "source_path": source_path,
            "staged_path": staged_path,
            "on_screen_caption": on_screen_caption,
            "posting_caption": posting_caption,
            "platform_posts": platform_posts,
            "result": _result_for(entry_id, meta, on_screen_caption, posting_caption, platform_posts),
        }
    return {"ok": True}


# --- Clipping -----------------------------------------------------------------
# Everything runs server-side in a background thread, so once the video is on
# the server (a pasted link, or a finished upload) the browser can be closed.
# Sessions + suggested clips + the word-level transcript are saved to Postgres
# (history survives restarts); each suggested clip is also pre-cut to a small
# file kept in durable storage, so a clip stays editable after the big source
# file has been cleaned off disk.

CLIPS = {}
CLIPS_LOCK = threading.Lock()
CLIP_MAX_BYTES = 2500 * 1024 * 1024
CLIP_SOURCE_KEEP = 12 * 3600


def _clip_set(cid, **kw):
    with CLIPS_LOCK:
        CLIPS.setdefault(cid, {}).update(kw)
    try:
        db.clip_upsert(cid, **kw)
    except Exception as e:
        print(f"CLIP DB SAVE FAILED ({cid}): {e}", flush=True)


def _clip_load(cid: str):
    """In-memory session, else rebuilt from the database (after a restart)."""
    with CLIPS_LOCK:
        c = CLIPS.get(cid)
    if c:
        return c
    row = db.clip_get(cid)
    if not row:
        return None
    c = {"title": row.get("title"), "status": row.get("status"), "stage_label": row.get("stage_label"),
         "error": row.get("error"), "duration": row.get("duration"),
         "clips": row.get("clips") or [], "speech": row.get("speech"),
         "source": os.path.join(DOWNLOAD_DIR, f"clip_{cid}_source.mp4")}
    if c["status"] == "running":
        c["status"] = "error"
        c["error"] = "The server restarted while this was running. Start it again."
    with CLIPS_LOCK:
        CLIPS[cid] = c
    return c


def _clip_file(cid: str, i: int) -> str:
    return f"clip_{cid}_{i}.mp4"


def _clip_analyze(cid: str, focus_text: str):
    try:
        c = CLIPS[cid]
        _clip_set(cid, status="running", stage_label="Transcribing the whole video", clips=[], error=None)
        sp = c.get("speech")
        if not sp:
            if not os.path.exists(c["source"]):
                raise RuntimeError("The original video is no longer on the server, so it can't be re-analysed.")
            sp = speech.transcribe_words(c["source"])
            if not sp.get("segments"):
                raise RuntimeError("No speech could be transcribed (is OPENAI_API_KEY set, and does the video have talking?).")
            _clip_set(cid, speech=sp, duration=sp.get("duration") or 0)
        _clip_set(cid, stage_label="Finding the best moments")
        ranges, notes = speech.parse_focus(focus_text or "")
        clips = speech.pick_clips(sp, sp.get("duration") or 0, ranges or None, notes)
        if not clips:
            raise RuntimeError("The clipper couldn't find a clean moment" + (" in those timestamps." if ranges else "."))
        for i, k in enumerate(clips):
            _clip_set(cid, stage_label=f"Cutting clip {i + 1} of {len(clips)}")
            k["id"] = i
            k["text"] = " ".join(w["w"] for w in sp["words"] if k["start"] <= w["start"] < k["end"])[:400]
            fn = _clip_file(cid, i)
            path = os.path.join(DOWNLOAD_DIR, fn)
            try:
                speech.cut_clip(c["source"], k["start"], k["end"], path)
            except Exception as ce:
                print(f"CLIP CUT FAILED ({cid} #{i}): {ce}", flush=True)
                continue
            storage.upload_many_async([(path, fn)])
            k["file"] = fn
        clips = [k for k in clips if k.get("file")]
        _clip_set(cid, clips=clips)
        # Run every clip through the normal WokeVision edit (template, hook
        # caption, burned-in captions, platform captions) so the results are
        # already finished; each lands in the editor's history too.
        for i, k in enumerate(clips):
            _clip_set(cid, stage_label=f"Editing clip {i + 1} of {len(clips)} in the WokeVision template")
            jid = str(uuid.uuid4())
            try:
                with JOBS_LOCK:
                    JOBS[jid] = {"stage": "queued", "stage_label": "Queued", "progress": 0.0, "status": "running", "angle": "", "_quiet": True}
                src = os.path.join(DOWNLOAD_DIR, f"{jid}_source.mp4")
                shutil.copyfile(os.path.join(DOWNLOAD_DIR, k["file"]), src)
                words = speech.clip_words(sp["words"], k["start"], k["end"])
                pre = {"text": " ".join(w["w"] for w in words), "words": words}
                _run_pipeline(jid, src, {"title": k.get("title") or "Clip", "description": "", "method": f"clipped {int(k['start'])}s-{int(k['end'])}s"}, pre_speech=pre)
                with JOBS_LOCK:
                    ok = (JOBS.get(jid) or {}).get("status") == "done"
                if ok:
                    k["job_id"] = jid
            except Exception as ee:
                print(f"CLIP EDIT FAILED ({cid} #{i}): {ee}", flush=True)
            _clip_set(cid, clips=clips)
        if not clips:
            raise RuntimeError("The clips were found but none could be cut. Try again, or use a smaller file.")
        _clip_set(cid, status="done", stage_label="Done", clips=clips)
        _b = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        notify.notify("Clips ready", f"{len(clips)} clips from {CLIPS[cid].get('title') or 'your video'}", f"{_b}/clipping" if _b else None)
    except Exception as e:
        print("CLIP ANALYZE FAILED:", traceback.format_exc(), flush=True)
        _clip_set(cid, status="error", error=str(e))
        notify.notify("Clipping failed", str(e)[:200])


def _clip_ingest_and_analyze(cid: str, url: str, focus_text: str):
    c = CLIPS[cid]
    if url:
        try:
            _clip_set(cid, status="running", stage_label="Downloading video")
            meta = download_video(url, c["source"])
            if meta.get("title"):
                _clip_set(cid, title=meta["title"][:200])
        except DownloadError as e:
            _clip_set(cid, status="error", error=f"Could not download video: {e}. If it's YouTube, download it yourself and upload the file instead.")
            return
    _cleanup_later(c["source"], delay=CLIP_SOURCE_KEEP)
    # Keep a durable copy so custom trims still work after the local file is cleaned up / redeploys.
    storage.upload_many_async([(c["source"], os.path.basename(c["source"]))])
    _clip_analyze(cid, focus_text)


def _clip_pull_and_analyze(cid: str, key: str, focus_text: str):
    c = CLIPS[cid]
    try:
        last = [-1]
        def _prog(f):
            p = int(f * 100) // 5 * 5
            if p != last[0]:
                last[0] = p
                _clip_set(cid, stage_label=f"Fetching your upload ({p}%)")
        _pull_upload(key, c["source"], _prog)
    except Exception as e:
        _clip_set(cid, status="error", error=f"Couldn't fetch the uploaded file: {e}")
        return
    _cleanup_later(c["source"], delay=CLIP_SOURCE_KEEP)
    # Keep a durable copy so custom trims still work after the local file is cleaned up / redeploys.
    storage.upload_many_async([(c["source"], os.path.basename(c["source"]))])
    _clip_analyze(cid, focus_text)


@app.get("/api/attention")
def attention():
    try:
        return {"items": db.attention_items()}
    except Exception as e:
        print(f"ATTENTION FAILED: {e}", flush=True)
        return {"items": []}


# --- Direct (browser -> R2) resumable uploads ---------------------------------

class UploadStart(BaseModel):
    filename: str
    size: int
    resume_key: str = ""
    resume_upload_id: str = ""


@app.post("/api/uploads/start")
def upload_start(req: UploadStart, request: Request):
    if not storage.configured():
        return {"direct": False}
    origins = {"https://wokevision.com", "https://www.wokevision.com"}
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        origins.add(base)
    origins.add(f"{request.url.scheme}://{request.headers.get('host', '')}")
    if not storage.ensure_cors(origins):
        return {"direct": False}
    if req.size <= 0 or req.size > 6 * 1024 ** 3:
        raise HTTPException(status_code=400, detail="That file is empty or over 6GB.")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(req.filename))[:80] or "video.mp4"
    key = req.resume_key if (req.resume_key.startswith("uploads/") and ".." not in req.resume_key) else f"uploads/{uuid.uuid4().hex}/{safe}"
    try:
        info = storage.multipart_start(key, req.size, req.resume_upload_id or None)
    except Exception as e:
        print(f"UPLOAD START FAILED: {e}", flush=True)
        return {"direct": False}
    return {"direct": True, "key": key, **info}


class UploadComplete(BaseModel):
    key: str
    upload_id: str
    size: int = 0


@app.post("/api/uploads/complete")
def upload_complete(req: UploadComplete):
    if not req.key.startswith("uploads/") or ".." in req.key:
        raise HTTPException(status_code=400, detail="Bad key.")
    try:
        storage.multipart_complete(req.key, req.upload_id, req.size or None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


def _pull_upload(key: str, dest: str, progress_cb=None):
    storage.download_with_progress(key, dest, progress_cb)
    storage.delete_key(key)


class ProcessUpload(BaseModel):
    key: str
    filename: str = ""
    angle: str = ""
    wm_token: str = ""
    wm_pos: str = "right"
    campaign_id: str = ""


def _run_pulled_pipeline(job_id: str, key: str, final_source_path: str, filename: str):
    try:
        _set_stage(job_id, "downloading", 0.0)
        _pull_upload(key, final_source_path, lambda f: _set_stage(job_id, "downloading", f))
    except Exception as e:
        _set_job(job_id, stage="error", stage_label="Error", status="error", error=f"Couldn't fetch the uploaded file: {e}")
        return
    meta = {"title": os.path.splitext(filename or "")[0], "description": "", "method": "direct upload"}
    _run_pipeline(job_id, final_source_path, meta)


@app.post("/process-upload")
def process_upload(req: ProcessUpload):
    if not req.key.startswith("uploads/") or ".." in req.key:
        raise HTTPException(status_code=400, detail="Bad key.")
    job_id = str(uuid.uuid4())
    final_source_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
    with JOBS_LOCK:
        JOBS[job_id] = {"stage": "downloading", "stage_label": "Fetching your upload", "progress": 0.0, "status": "running",
                        "angle": (req.angle or "").strip()[:1500], "wm_token": req.wm_token, "wm_pos": req.wm_pos, "campaign_id": req.campaign_id}
    threading.Thread(target=_run_pulled_pipeline, args=(job_id, req.key, final_source_path, req.filename), daemon=True).start()
    return {"job_id": job_id}


@app.get("/clipping")
def clipping_page():
    return FileResponse("static/clipping.html")


@app.post("/api/clip")
async def clip_start(file: UploadFile = File(None), url: str = Form(""), focus: str = Form(""),
                     upload_key: str = Form(""), filename: str = Form("")):
    cid = uuid.uuid4().hex
    source = os.path.join(DOWNLOAD_DIR, f"clip_{cid}_source.mp4")
    title = ""
    if upload_key:
        if not upload_key.startswith("uploads/") or ".." in upload_key:
            raise HTTPException(status_code=400, detail="Bad key.")
        title = os.path.splitext(filename or "")[0][:200] or "Uploaded video"
        _clip_set(cid, status="running", stage_label="Fetching your upload", source=source, clips=[], title=title)
        threading.Thread(target=_clip_pull_and_analyze, args=(cid, upload_key, focus), daemon=True).start()
        return {"clip_id": cid}
    if file is not None and file.filename:
        try:
            with open(source, "wb") as f:
                shutil.copyfileobj(file.file, f)
        finally:
            await file.close()
        size = os.path.getsize(source)
        if size == 0:
            raise HTTPException(status_code=400, detail="Upload failed: no file data received.")
        if size > CLIP_MAX_BYTES:
            os.remove(source)
            raise HTTPException(status_code=413, detail="That file is over 2.5GB. Compress it first (720p is plenty) and try again.")
        title = os.path.splitext(file.filename)[0][:200]
        url = ""
    elif not url.strip():
        raise HTTPException(status_code=400, detail="Add a link or upload a video.")
    else:
        title = url.strip()[:200]
    _clip_set(cid, status="running", stage_label="Starting", source=source, clips=[], title=title)
    threading.Thread(target=_clip_ingest_and_analyze, args=(cid, url.strip(), focus), daemon=True).start()
    return {"clip_id": cid}


def _clip_public(cid, c):
    base = {k: v for k, v in c.items() if k not in ("speech", "source")}
    base["id"] = cid
    base["clips"] = [{**k, "video_url": (f"/files/{k['job_id']}_final.mp4" if k.get("job_id") else f"/files/{k['file']}")}
                     for k in (c.get("clips") or []) if k.get("file")]
    return base


@app.post("/api/notify-test")
def notify_test():
    import notify as _n
    if not _n.configured():
        return {"ok": False, "detail": "No NTFY_TOPIC (or Telegram vars) set on the server. Add it in Render > Environment, then redeploy."}
    return _n.send_sync("WokeVision test", "If you can read this, alerts work.")


@app.get("/api/clip/{cid}/words")
def clip_words_window(cid: str, start: float = 0, end: float = 60):
    """Transcript words in a time window -- powers the click-to-trim UI."""
    c = _clip_load(cid)
    if not c or not c.get("speech"):
        raise HTTPException(status_code=404, detail="Unknown clipping session.")
    end = min(end, start + 300)
    ws = [w for w in c["speech"].get("words", []) if w["end"] >= start and w["start"] <= end]
    return {"words": ws}


@app.get("/api/clips")
def clip_history():
    out = []
    for r in db.clip_list():
        out.append({"id": r["id"], "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    "title": r.get("title") or "Untitled video", "status": r.get("status"),
                    "duration": r.get("duration"), "clip_count": len(r.get("clips") or [])})
    return {"history": out}


@app.get("/api/clip/{cid}")
def clip_status(cid: str):
    c = _clip_load(cid)
    if not c:
        raise HTTPException(status_code=404, detail="Unknown clipping session.")
    return _clip_public(cid, c)


class ClipFocus(BaseModel):
    focus: str = ""


@app.post("/api/clip/{cid}/reanalyze")
def clip_reanalyze(cid: str, req: ClipFocus):
    c = _clip_load(cid)
    if not c:
        raise HTTPException(status_code=404, detail="Unknown clipping session.")
    if c.get("status") == "running":
        raise HTTPException(status_code=409, detail="Still working.")
    if not c.get("speech"):
        raise HTTPException(status_code=409, detail="This session has no transcript to re-run from.")
    if not os.path.exists(c["source"]) and not storage.fetch_to(c["source"], os.path.basename(c["source"])):
        raise HTTPException(status_code=410, detail="The original video has been cleared from the server (it's kept for 12 hours). Upload it again to re-run the clipper; your existing clips are still editable.")
    _clip_set(cid, status="running", stage_label="Finding the best moments", error=None)
    threading.Thread(target=_clip_analyze, args=(cid, req.focus), daemon=True).start()
    return {"ok": True}


class ClipPick(BaseModel):
    index: int | None = None
    start: float = 0
    end: float = 0
    title: str = ""


def _run_clip_to_editor(job_id: str, cid: str, pick: dict):
    try:
        c = _clip_load(cid)
        out = os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
        words_all = c["speech"]["words"]
        if pick.get("file"):
            _set_job(job_id, stage="downloading", stage_label="Loading the clip", progress=0.1)
            src = os.path.join(DOWNLOAD_DIR, pick["file"])
            if not os.path.exists(src):
                storage.fetch_to(src, pick["file"])
            if not os.path.exists(src):
                raise RuntimeError("That clip file has expired.")
            shutil.copyfile(src, out)
        else:
            if not os.path.exists(c["source"]):
                storage.fetch_to(c["source"], os.path.basename(c["source"]))
            if not os.path.exists(c["source"]):
                raise RuntimeError("The original video has been cleared from the server, so a custom range can't be cut. Pick one of the suggested clips, or upload the video again.")
            _set_job(job_id, stage="downloading", stage_label="Cutting the clip", progress=0.1)
            speech.cut_clip(c["source"], pick["start"], pick["end"], out)
        start, end = pick["start"], pick["end"]
        words = speech.clip_words(words_all, start, end)
        pre = {"text": " ".join(w["w"] for w in words), "words": words}
        meta = {"title": pick.get("title") or "Clip", "description": "", "method": f"clipped {int(start)}s-{int(end)}s"}
        _run_pipeline(job_id, out, meta, pre_speech=pre)
    except Exception as e:
        print("CLIP TO EDITOR FAILED:", traceback.format_exc(), flush=True)
        _set_job(job_id, stage="error", stage_label="Error", status="error", error=str(e))


@app.post("/api/clip/{cid}/edit")
def clip_to_editor(cid: str, req: ClipPick):
    c = _clip_load(cid)
    if not c or not c.get("speech"):
        raise HTTPException(status_code=404, detail="Unknown clipping session.")
    pick = {"title": req.title}
    clips = c.get("clips") or []
    if req.index is not None and 0 <= req.index < len(clips):
        k = clips[req.index]
        pick.update(start=k["start"], end=k["end"], file=k.get("file"), title=req.title or k.get("title", ""))
    else:
        dur = c.get("duration") or 0
        start, end = max(0.0, req.start), min(req.end, dur or req.end)
        if end - start < 3:
            raise HTTPException(status_code=400, detail="Clip is too short.")
        pick.update(start=start, end=end)
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"stage": "downloading", "stage_label": "Preparing the clip", "progress": 0.05, "status": "running", "angle": ""}
    threading.Thread(target=_run_clip_to_editor, args=(job_id, cid, pick), daemon=True).start()
    return {"job_id": job_id}


# Serves the submission page's own static assets, if any are added later
# (CSS/JS files). The page itself is served by the "/" route above.
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Scheduling ---------------------------------------------------------------

import datetime as _dt
import hmac as _hmac


def _parse_when(value: str) -> _dt.datetime:
    try:
        d = _dt.datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="That date/time isn't valid.")
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


class ScheduleItem(BaseModel):
    platform: str
    run_at: str


class ScheduleRequest(BaseModel):
    history_id: str
    items: list[ScheduleItem]


class ScheduleTimeRequest(BaseModel):
    run_at: str


def _post_url(platform: str, res: dict):
    if platform == "youtube" and res.get("video_id"):
        return f"https://www.youtube.com/shorts/{res['video_id']}"
    if platform == "x" and res.get("tweet_id"):
        return f"https://x.com/i/status/{res['tweet_id']}"
    return None


def _sched_public(r: dict) -> dict:
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    fn = r.get("video_filename")
    return {
        "id": r["id"], "history_id": r["history_id"], "platform": r["platform"],
        "label": PLATFORM_LABELS.get(r["platform"], r["platform"]),
        "run_at": r["run_at"], "status": r["status"], "attempts": r.get("attempts", 0),
        "error": ((r.get("result") or {}).get("error") if r["status"] in ("error", "missed") else None),
        "post_url": _post_url(r["platform"], r.get("result") or {}) if r["status"] == "done" else None,
        "title": r.get("title") or "", "on_screen_caption": r.get("on_screen_caption") or "",
        "video_url": f"{base_url}/files/{fn}" if fn else None,
    }


@app.get("/api/schedule")
def schedule_list(history_id: str = None, past: bool = True):
    if not db.configured():
        return {"items": []}
    return {"items": [_sched_public(r) for r in db.sched_list(history_id=history_id, include_past=past)]}


@app.post("/api/schedule")
def schedule_create(req: ScheduleRequest):
    """Schedules each listed platform of a saved edit at its own time (send the
    same time for all to schedule them together). Re-scheduling a platform that
    is already scheduled just moves it."""
    if not db.configured():
        raise HTTPException(status_code=503, detail="Scheduling needs the database to be set up.")
    if not req.items:
        raise HTTPException(status_code=400, detail="Pick at least one platform.")
    if not db.get_history_entry(req.history_id):
        raise HTTPException(status_code=404, detail="That edit isn't saved yet.")
    now = _dt.datetime.now(_dt.timezone.utc)
    out = []
    for it in req.items:
        if it.platform not in PLATFORM_MODULES:
            raise HTTPException(status_code=400, detail=f"Unknown platform: {it.platform}")
        when = _parse_when(it.run_at)
        if when < now - _dt.timedelta(seconds=60):
            raise HTTPException(status_code=400, detail="Pick a time in the future.")
        out.append(db.sched_upsert(req.history_id, it.platform, when))
    rows = {r["id"] for r in out}
    return {"items": [_sched_public(r) for r in db.sched_list(history_id=req.history_id) if r["id"] in rows]}


@app.put("/api/schedule/{sid}")
def schedule_move(sid: str, req: ScheduleTimeRequest):
    when = _parse_when(req.run_at)
    if when < _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=60):
        raise HTTPException(status_code=400, detail="Pick a time in the future.")
    r = db.sched_set_time(sid, when)
    if not r:
        raise HTTPException(status_code=409, detail="That post can't be changed right now (it may be posting).")
    return {"ok": True}


@app.delete("/api/schedule/{sid}")
def schedule_cancel(sid: str):
    if not db.sched_delete(sid):
        raise HTTPException(status_code=409, detail="Couldn't remove it (it may be posting right now).")
    return {"ok": True}


@app.post("/api/schedule/{sid}/post-now")
def schedule_post_now(sid: str):
    r = db.sched_set_time(sid, _dt.datetime.now(_dt.timezone.utc))
    if not r:
        raise HTTPException(status_code=409, detail="That post can't be changed right now (it may be posting).")
    threading.Thread(target=scheduler.run_due, args=(PLATFORM_MODULES,), daemon=True).start()
    return {"ok": True}


_LAST_TICK = {"at": None}


def _maybe_weekly_digest():
    """Mondays from 08:00 UTC, once a week: a short performance summary as a phone alert."""
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        if now.weekday() != 0 or now.hour < 8 or not notify.configured() or not db.configured():
            return
        key = now.strftime("%G-W%V")
        if insights.kv_get("digest_week") == key:
            return
        insights.kv_set("digest_week", key)
        text = insights.weekly_digest()
        if text:
            notify.notify("WokeVision weekly summary", text)
    except Exception as e:
        print(f"DIGEST FAILED: {e}", flush=True)


@app.get("/api/schedule-health")
def schedule_health():
    """Lets the Schedule page warn when nothing is poking the app awake."""
    at = _LAST_TICK["at"]
    return {
        "timer_configured": bool(os.environ.get("CRON_SECRET")),
        "last_tick_seconds_ago": None if at is None else int(time.time() - at),
    }


@app.api_route("/api/cron/tick", methods=["GET", "POST", "HEAD"])
def cron_tick(request: Request, key: str = ""):
    """Hit every minute by an outside timer. Public path, but only with the
    CRON_SECRET; its job is to wake the (free-tier, sleeping) app and publish
    whatever has come due. Returns immediately -- publishing runs in the
    background so the timer service never times out."""
    secret = os.environ.get("CRON_SECRET", "")
    given = key or request.headers.get("x-cron-key", "")
    if not secret:
        raise HTTPException(status_code=503, detail="CRON_SECRET isn't set on the server.")
    if not _hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="Bad key.")
    _LAST_TICK["at"] = time.time()
    _maybe_weekly_digest()
    threading.Thread(target=scheduler.run_due, args=(PLATFORM_MODULES,), daemon=True).start()
    return {"ok": True}


# --- Campaigns -------------------------------------------------------------------

class CampaignModel(BaseModel):
    name: str
    sponsor: str = ""
    brief: str = ""
    hashtags: str = ""
    wm_token: str = ""
    wm_pos: str = "right"


def _camp_public(c, with_share=True):
    out = {k: c.get(k) for k in ("id", "name", "sponsor", "brief", "hashtags", "wm_token", "wm_pos", "posts")}
    if with_share:
        out["share_token"] = c.get("share_token")
    return out


@app.get("/campaigns")
def campaigns_page():
    return FileResponse("static/campaigns.html")


@app.get("/campaigns/{cid}/report")
def campaign_report_page(cid: str):
    return FileResponse("static/campaign_report.html")


@app.get("/c/{token}")
def campaign_share_page(token: str):
    return FileResponse("static/campaign_share.html")


@app.get("/api/campaigns")
def campaigns_list():
    return {"items": [_camp_public(c) for c in db.camp_list()]}


def _camp_fields(req: CampaignModel) -> dict:
    return dict(name=req.name.strip()[:100], sponsor=req.sponsor[:100], brief=req.brief[:4000], hashtags=req.hashtags[:500],
                wm_token=req.wm_token if _HEX.match(req.wm_token or "") else "",
                wm_pos=req.wm_pos if req.wm_pos in render.WM_POSITIONS else "right")


@app.post("/api/campaigns")
def campaigns_create(req: CampaignModel):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Give the campaign a name.")
    cid = uuid.uuid4().hex[:12]
    db.camp_save(cid, secrets.token_urlsafe(16), **_camp_fields(req))
    return {"id": cid}


@app.put("/api/campaigns/{cid}")
def campaigns_update(cid: str, req: CampaignModel):
    if not db.camp_get(cid):
        raise HTTPException(status_code=404, detail="Unknown campaign.")
    db.camp_save(cid, "", **_camp_fields(req))
    return {"ok": True}


@app.delete("/api/campaigns/{cid}")
def campaigns_delete(cid: str):
    db.camp_delete(cid)
    return {"ok": True}


def _camp_hashtags(c) -> list:
    return [t if t.startswith("#") else "#" + t for t in re.split(r"[\s,]+", c.get("hashtags") or "") if t.strip("#")]


def _with_hashtags(text: str, tags: list) -> str:
    missing = [t for t in tags if t.lower() not in (text or "").lower()]
    return (text or "").rstrip() + ("\n\n" + " ".join(missing) if missing else "")


def _tag_posts(posts: dict, caption: str, tags: list) -> dict:
    posts = normalize_platform_posts(posts, caption)
    for v in posts.values():
        if isinstance(v, dict):
            for fld in ("caption", "text", "description"):
                if isinstance(v.get(fld), str) and v[fld].strip():
                    v[fld] = _with_hashtags(v[fld], tags)
                    break
    return normalize_platform_posts(posts, caption)


class JobCampaign(BaseModel):
    campaign_id: str = ""


@app.put("/jobs/{job_id}/campaign")
def job_set_campaign(job_id: str, req: JobCampaign):
    """Files this edit under a campaign and makes sure the campaign's required
    hashtags are on every platform caption."""
    c = db.camp_get(req.campaign_id) if req.campaign_id else None
    if req.campaign_id and not c:
        raise HTTPException(status_code=404, detail="Unknown campaign.")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id")
        job["meta"] = {**(job.get("meta") or {}), "campaign_id": req.campaign_id,
                       "campaign_brief": ((c or {}).get("brief") or "").strip()[:1500]}
        meta = job["meta"]
        posts = job.get("platform_posts") or {}
        if c:
            tags = _camp_hashtags(c)
            if tags:
                posts = normalize_platform_posts(posts, job.get("posting_caption") or "")
                for v in posts.values():
                    if isinstance(v, dict):
                        for fld in ("caption", "text", "description"):
                            if isinstance(v.get(fld), str) and v[fld].strip():
                                v[fld] = _with_hashtags(v[fld], tags)
                                break
                posts = normalize_platform_posts(posts, job.get("posting_caption") or "")
                job["platform_posts"] = posts
    try:
        db.update_history_meta(job_id, meta)
        db.history_set_campaign(job_id, req.campaign_id)
        if c and posts:
            _save_platform_posts(job_id, posts)
    except Exception as e:
        print(f"CAMPAIGN SAVE FAILED: {e}", flush=True)
    return {"platform_posts": posts, "campaign": _camp_public(c) if c else None}


def _camp_report(c):
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    out = []
    items = db.camp_items(c["id"])
    stats = {}
    try:
        urls = [_post_url(p["platform"], p.get("result") or {}) for it in items for p in it["posts"] if p["status"] == "done"]
        stats = db.post_stats_by_url([u for u in urls if u])
    except Exception:
        stats = {}
    for it in items:
        fn = it.get("video_filename")
        out.append({
            "id": it["id"], "title": it.get("title") or "", "caption": it.get("posting_caption") or "",
            "video_url": f"{base}/files/{fn}" if fn else None, "approval": it.get("approval"), "approval_note": it.get("approval_note"),
            "posts": [{"platform": PLATFORM_LABELS.get(p["platform"], p["platform"]), "run_at": p["run_at"], "status": p["status"],
                       "url": _post_url(p["platform"], p.get("result") or {}) if p["status"] == "done" else None,
                       **(stats.get(_post_url(p["platform"], p.get("result") or {}) if p["status"] == "done" else None) or {})} for p in it["posts"]],
        })
    return out


@app.get("/api/campaigns/{cid}/report")
def campaign_report(cid: str):
    c = db.camp_get(cid)
    if not c:
        raise HTTPException(status_code=404, detail="Unknown campaign.")
    try:
        clicks = db.bio_campaign_clicks(cid)
    except Exception:
        clicks = []
    return {"campaign": _camp_public(c), "items": _camp_report(c), "bio_clicks": clicks}


@app.get("/api/public/campaign/{token}")
def campaign_public(token: str):
    """What a sponsor sees: the videos and captions, nothing internal."""
    c = db.camp_get(share_token=token)
    if not c:
        raise HTTPException(status_code=404, detail="This link isn't valid.")
    keep = ("id", "title", "caption", "video_url", "approval", "approval_note")
    return {"name": c["name"], "sponsor": c.get("sponsor") or "", "items": [{k: i[k] for k in keep} for i in _camp_report(c)]}


class ReviewModel(BaseModel):
    history_id: str
    status: str
    note: str = ""


@app.post("/api/public/campaign/{token}/review")
def campaign_review(token: str, req: ReviewModel):
    c = db.camp_get(share_token=token)
    if not c:
        raise HTTPException(status_code=404, detail="This link isn't valid.")
    if req.status not in ("approved", "changes"):
        raise HTTPException(status_code=400, detail="Bad status.")
    if not db.history_set_approval(req.history_id, c["id"], req.status, req.note or ""):
        raise HTTPException(status_code=404, detail="Unknown video.")
    notify.notify(f"{c['name']}: {'approved' if req.status == 'approved' else 'changes requested'}", (req.note or "")[:150])
    return {"ok": True}


@app.get("/schedule")
def schedule_page():
    return FileResponse("static/schedule.html")


# --- Accounts tab: live bios + saved drafts ---------------------------------

class ProfileDraft(BaseModel):
    name: str = ""
    bio: str = ""
    link: str = ""
    notes: str = ""


@app.get("/accounts")
def accounts_page():
    return FileResponse("static/accounts.html")


@app.get("/api/accounts")
def api_accounts():
    from concurrent.futures import ThreadPoolExecutor
    drafts = db.profiles_get()
    with ThreadPoolExecutor(max_workers=6) as ex:
        lives = list(ex.map(accounts.live, accounts.ORDER))
    out = []
    for pid, lv in zip(accounts.ORDER, lives):
        out.append({"platform": pid, "label": accounts.LABELS[pid], "live": lv, "draft": drafts.get(pid) or {},
                    "limit": accounts.BIO_LIMITS[pid], "edit_url": accounts.EDIT_LINKS[pid],
                    "can_push": pid == "facebook"})
    return {"accounts": out}


@app.put("/api/accounts/{platform}")
def api_account_save(platform: str, body: ProfileDraft):
    if platform not in accounts.ORDER:
        raise HTTPException(status_code=404, detail="Unknown platform.")
    db.profile_save(platform, {"name": body.name[:200], "bio": body.bio[:2000], "link": body.link[:500], "notes": body.notes[:2000]})
    return {"ok": True}


@app.post("/api/accounts/facebook/push")
def api_account_push_facebook(body: ProfileDraft):
    try:
        return accounts.push_facebook(about=body.bio[:255], website=body.link or None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/content-groups")
def api_content_groups():
    """Which platform posts came from the same upload, from the editor's own
    publish records (so the Analytics Hub can show one video across platforms)."""
    groups = []
    for e in db.list_history(300):
        res = e.get("publish_results") or {}
        ids = {}
        for pid, r in res.items():
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            v = r.get("media_id") or r.get("tweet_id") or r.get("video_id")
            if v:
                ids[pid] = str(v)
        if ids:
            groups.append({"id": str(e.get("id")), "title": e.get("on_screen_caption") or e.get("title") or "",
                           "created_at": e["created_at"].isoformat() if e.get("created_at") else None, "ids": ids})
    return {"groups": groups}
