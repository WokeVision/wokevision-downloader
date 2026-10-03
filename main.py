import os
import uuid
import time
import shutil
import threading
import traceback

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downloader import download_video, DownloadError
from transcribe import transcribe_audio
from caption import generate_on_screen_caption, generate_posting_caption
from render import render_video

app = FastAPI()

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
# running and complete it when done.
STAGE_WEIGHTS = {
    "downloading": (0.00, 0.35),
    "transcribing": (0.35, 0.55),
    "captioning": (0.55, 0.65),
    "rendering": (0.65, 1.00),
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

# How long a finished job's source/output files stick around on disk, so the
# "change caption" buttons can re-render or re-write a caption without the
# user having to re-download or re-upload anything. Matches the existing
# output-file cleanup window.
KEEP_ALIVE_SECONDS = 1200


def _set_job(job_id, **fields):
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


def _set_stage(job_id, stage, within_stage=0.0):
    lo, hi = STAGE_WEIGHTS.get(stage, (0.0, 1.0))
    overall = lo + (hi - lo) * max(0.0, min(within_stage, 1.0))
    _set_job(job_id, stage=stage, stage_label=STAGE_LABELS.get(stage, stage), progress=overall)


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
        on_screen_caption = generate_on_screen_caption(transcript, meta)
        posting_caption = generate_posting_caption(transcript, meta, on_screen_caption)

        _set_stage(job_id, "rendering", 0.0)
        render_video(
            source_path=final_source_path,
            caption_text=on_screen_caption,
            output_path=output_path,
            progress_cb=lambda frac: _set_stage(job_id, "rendering", frac),
        )

        if not os.path.exists(output_path):
            raise RuntimeError("Render finished but no output file was produced.")

        _cleanup_later(output_path, delay=KEEP_ALIVE_SECONDS)
        _cleanup_later(final_source_path, delay=KEEP_ALIVE_SECONDS)
        _set_job(
            job_id,
            stage="done", stage_label="Done", progress=1.0, status="done",
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

        if not os.path.exists(output_path):
            raise RuntimeError("Render finished but no output file was produced.")

        _cleanup_later(output_path, delay=KEEP_ALIVE_SECONDS)
        _set_job(
            job_id,
            stage="done", stage_label="Done", progress=1.0, status="done",
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


# Serves the submission page's own static assets, if any are added later
# (CSS/JS files). The page itself is served by the "/" route above.
app.mount("/static", StaticFiles(directory="static"), name="static")
