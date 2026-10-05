import os
import uuid
import time
import shutil
import threading
import traceback

from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downloader import download_video, DownloadError
from transcribe import transcribe_audio
from caption import (generate_captions, generate_on_screen_caption, generate_posting_caption,
                     generate_platform_posts, normalize_platform_posts, PLATFORM_IDS)
from render import render_staged, apply_caption
import db
import auth
import storage
import insights
from platforms import instagram, threads, youtube, x, tiktok, facebook

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
PUBLIC_PATH_PREFIXES = ("/static/", "/auth/", "/files/")
PUBLIC_PATHS = {
    "/",                    # public homepage
    "/api/home/popular",    # public: top Instagram posts for the homepage carousel
    "/login",
    "/health",
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


def _set_stage(job_id, stage, within_stage=0.0):
    now = time.time()
    with JOBS_LOCK:
        job = JOBS[job_id]
        prev_stage = job.get("stage")
        if prev_stage != stage:
            prev_started = job.get("_stage_started_at")
            if prev_stage in STAGE_ORDER and prev_started:
                _record_stage_duration(prev_stage, now - prev_started)
            job["_stage_started_at"] = now

        lo, hi = STAGE_WEIGHTS.get(stage, (0.0, 1.0))
        overall = lo + (hi - lo) * max(0.0, min(within_stage, 1.0))
        job["stage"] = stage
        job["stage_label"] = STAGE_LABELS.get(stage, stage)
        job["progress"] = overall
        job["eta_seconds"] = _estimate_eta_seconds(job)


class ProcessRequest(BaseModel):
    url: str


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


def _run_pipeline(job_id: str, final_source_path: str, meta: dict):
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
        _set_stage(job_id, "transcribing")
        transcript = transcribe_audio(final_source_path)
        _set_job(job_id, transcript=transcript, meta=meta, source_path=final_source_path)

        _set_stage(job_id, "captioning")
        on_screen_caption, posting_caption = generate_captions(transcript, meta)
        platform_posts = generate_platform_posts(transcript, meta, on_screen_caption, posting_caption)

        # Rendering happens in two cached stages: render_staged() does the
        # crop/scale/logo/watermark compositing (everything that has nothing
        # to do with the caption text) and is kept on disk afterwards, so a
        # later caption-only edit (regenerate-video / set-on-screen-caption)
        # can call apply_caption() straight against it instead of redoing
        # this work and re-decoding the original source every time.
        _set_stage(job_id, "rendering", 0.0)
        render_staged(
            source_path=final_source_path,
            output_path=staged_path,
            progress_cb=lambda frac: _set_stage(job_id, "rendering", frac * 0.5),
        )
        _set_job(job_id, staged_path=staged_path)
        apply_caption(
            staged_path=staged_path,
            caption_text=on_screen_caption,
            output_path=output_path,
            progress_cb=lambda frac: _set_stage(job_id, "rendering", 0.5 + frac * 0.5),
        )
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
        )
        _cleanup_later(staged_path, delay=KEEP_ALIVE_SECONDS)
        _set_job(job_id, staged_path=staged_path)
        progress_base = 0.5

    apply_caption(
        staged_path=staged_path, caption_text=on_screen_caption, output_path=output_path,
        progress_cb=lambda frac: _set_stage(job_id, "rendering", progress_base + frac * (1 - progress_base)),
    )
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
def auth_register_options(req: PasskeyRegisterStart):
    try:
        return auth.start_registration(req.setup_code)
    except auth.AuthError as e:
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


@app.post("/auth/login/options")
def auth_login_options():
    try:
        return auth.start_login()
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/auth/login/verify")
def auth_login_verify(req: PasskeyLoginFinish):
    try:
        session_token = auth.finish_login(req.token, req.credential)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
        JOBS[job_id] = {"stage": "queued", "stage_label": "Queued", "progress": 0.0, "status": "running"}
    threading.Thread(
        target=_run_download_then_pipeline, args=(job_id, req.url, final_source_path), daemon=True
    ).start()
    return {"job_id": job_id}


@app.post("/process-file")
async def process_file(file: UploadFile = File(...)):
    """Direct upload path: skips the download step entirely. Use this when a
    link can't be fetched automatically (most often YouTube, when the host's
    IP is being rate-limited) -- download the video yourself and upload the
    file here instead."""
    job_id = str(uuid.uuid4())
    final_source_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
    with JOBS_LOCK:
        JOBS[job_id] = {"stage": "uploading", "stage_label": "Receiving upload", "progress": 0.0, "status": "running"}

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


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
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


@app.get("/files/{filename}")
def get_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, os.path.basename(filename))
    if not os.path.exists(path):
        # Local disk is wiped on every redeploy; pull from durable storage.
        storage.fetch_to(path, os.path.basename(filename))
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    return FileResponse(path, media_type="video/mp4", headers={"Content-Disposition": "inline"})


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
    }


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


# Serves the submission page's own static assets, if any are added later
# (CSS/JS files). The page itself is served by the "/" route above.
app.mount("/static", StaticFiles(directory="static"), name="static")
