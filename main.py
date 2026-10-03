import os
import uuid
import time
import shutil
import threading
import traceback

from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downloader import download_video, DownloadError
from transcribe import transcribe_audio
from caption import generate_captions, generate_on_screen_caption, generate_posting_caption
from render import render_video
import db
from platforms import instagram, threads, youtube, x, tiktok

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
}
PLATFORM_LABELS = {
    "instagram": "Instagram",
    "threads": "Threads",
    "youtube": "YouTube Shorts",
    "tiktok": "TikTok",
    "x": "X",
}
PLATFORM_ORDER = ["instagram", "threads", "youtube", "tiktok", "x"]

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


# How long a finished job's source/output files stick around on disk, so the
# "change caption" buttons can re-render or re-write a caption without the
# user having to re-download or re-upload anything. Matches the existing
# output-file cleanup window.
KEEP_ALIVE_SECONDS = 1200


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


def _result_for(job_id: str, meta: dict, on_screen_caption: str, posting_caption: str) -> dict:
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {
        "video_url": f"{base_url}/files/{job_id}_final.mp4",
        "on_screen_caption": on_screen_caption,
        "caption": posting_caption,
        "download_method": meta.get("method", ""),
    }


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
    try:
        _set_stage(job_id, "transcribing")
        transcript = transcribe_audio(final_source_path)
        _set_job(job_id, transcript=transcript, meta=meta, source_path=final_source_path)

        _set_stage(job_id, "captioning")
        on_screen_caption, posting_caption = generate_captions(transcript, meta)

        _set_stage(job_id, "rendering", 0.0)
        render_video(
            source_path=final_source_path,
            caption_text=on_screen_caption,
            output_path=output_path,
            progress_cb=lambda frac: _set_stage(job_id, "rendering", frac),
        )
        with JOBS_LOCK:
            _record_stage_duration("rendering", time.time() - JOBS[job_id].get("_stage_started_at", time.time()))

        if not os.path.exists(output_path):
            raise RuntimeError("Render finished but no output file was produced.")

        _cleanup_later(output_path, delay=KEEP_ALIVE_SECONDS)
        _cleanup_later(final_source_path, delay=KEEP_ALIVE_SECONDS)
        _set_job(
            job_id,
            stage="done", stage_label="Done", progress=1.0, status="done", eta_seconds=0,
            on_screen_caption=on_screen_caption,
            posting_caption=posting_caption,
            result=_result_for(job_id, meta, on_screen_caption, posting_caption),
        )
    except Exception as e:
        tb = traceback.format_exc()
        print("PIPELINE FAILED:", tb, flush=True)
        if os.path.exists(final_source_path):
            os.remove(final_source_path)
        _set_job(job_id, stage="error", stage_label="Error", status="error", error=str(e))


def _run_regenerate_video(job_id: str):
    """Re-generate the on-screen caption and re-render the video against the
    kept source file, without re-downloading/re-uploading. Used by the
    "change video caption" button."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    # The endpoint already validated status=="done" and flipped it to
    # "running" before starting this thread, so we don't re-check it here.
    if not job:
        return
    final_source_path = job.get("source_path")
    transcript = job.get("transcript", "")
    meta = job.get("meta", {})
    prev_on_screen = job.get("on_screen_caption", "")
    output_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_final.mp4")

    if not final_source_path or not os.path.exists(final_source_path):
        _set_job(
            job_id, stage="error", stage_label="Error", status="error",
            error="The original video file has expired, so the caption can't be "
                  "regenerated anymore -- please re-process the video.",
        )
        return

    try:
        _set_stage(job_id, "captioning")
        on_screen_caption = generate_on_screen_caption(transcript, meta, avoid=prev_on_screen)
        posting_caption = generate_posting_caption(transcript, meta, on_screen_caption)

        _set_stage(job_id, "rendering", 0.0)
        render_video(
            source_path=final_source_path,
            caption_text=on_screen_caption,
            output_path=output_path,
            progress_cb=lambda frac: _set_stage(job_id, "rendering", frac),
        )
        with JOBS_LOCK:
            _record_stage_duration("rendering", time.time() - JOBS[job_id].get("_stage_started_at", time.time()))

        if not os.path.exists(output_path):
            raise RuntimeError("Render finished but no output file was produced.")

        _cleanup_later(output_path, delay=KEEP_ALIVE_SECONDS)
        _set_job(
            job_id,
            stage="done", stage_label="Done", progress=1.0, status="done", eta_seconds=0,
            on_screen_caption=on_screen_caption,
            posting_caption=posting_caption,
            result=_result_for(job_id, meta, on_screen_caption, posting_caption),
        )
    except Exception as e:
        tb = traceback.format_exc()
        print("REGENERATE VIDEO FAILED:", tb, flush=True)
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
        _set_job(
            job_id,
            posting_caption=posting_caption,
            result=_result_for(job_id, meta, on_screen_caption, posting_caption),
        )
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
def index():
    return FileResponse("static/index.html")


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
    return {"posting_caption": job.get("posting_caption", ""), "result": job.get("result")}


@app.get("/files/{filename}")
def get_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
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
        return RedirectResponse(f"/?connect_error={_url_quote(msg)}")
    code = params.get("code")
    state = params.get("state")
    if not code or not _check_oauth_state(state or ""):
        return RedirectResponse(f"/?connect_error={_url_quote('Invalid or expired connection attempt, please try again.')}")
    try:
        module.handle_callback(code)
    except Exception as e:
        return RedirectResponse(f"/?connect_error={_url_quote(str(e))}")
    return RedirectResponse(f"/?connected={platform}")


@app.post("/connections/{platform}/disconnect")
def connect_disconnect(platform: str):
    if platform not in PLATFORM_MODULES:
        raise HTTPException(status_code=404, detail="Unknown or not-yet-supported platform.")
    db.delete_connection(platform)
    return {"ok": True}


class PublishRequest(BaseModel):
    job_id: str
    platforms: list[str]


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

    results = {}
    for platform in req.platforms:
        module = PLATFORM_MODULES.get(platform)
        if not module:
            results[platform] = {"ok": False, "error": "This platform isn't connected yet."}
            continue
        try:
            outcome = module.publish_video(video_url, caption)
            results[platform] = {"ok": True, **outcome}
        except Exception as e:
            results[platform] = {"ok": False, "error": str(e)}
    return {"results": results}


# Serves the submission page's own static assets, if any are added later
# (CSS/JS files). The page itself is served by the "/" route above.
app.mount("/static", StaticFiles(directory="static"), name="static")
