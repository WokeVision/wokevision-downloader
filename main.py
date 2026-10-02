import os
import uuid
import time
import threading
import traceback

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downloader import download_video, DownloadError
from transcribe import transcribe_audio
from caption import generate_caption
from render import render_video

app = FastAPI()

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class ProcessRequest(BaseModel):
    url: str


def _cleanup_later(path: str, delay: int = 1200):
    def _run():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
    threading.Thread(target=_run, daemon=True).start()


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/process")
def process(req: ProcessRequest):
    file_id = str(uuid.uuid4())
    final_source_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_source.mp4")
    output_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_final.mp4")

    # 1. Download (with fallback chain -- see downloader.py)
    try:
        meta = download_video(req.url, final_source_path)
    except DownloadError as e:
        raise HTTPException(status_code=422, detail=f"Could not download video: {e}")

    # 2. Transcribe (best-effort -- empty string if no OPENAI_API_KEY)
    transcript = transcribe_audio(final_source_path)

    # 3. Generate the on-screen caption + full Instagram caption
    on_screen_caption, full_caption = generate_caption(transcript, meta)

    # 4. Render (crop, logo/watermark overlay, caption image overlay)
    try:
        render_video(
            source_path=final_source_path,
            caption_text=on_screen_caption,
            output_path=output_path,
        )
    except Exception:
        tb = traceback.format_exc()
        print("RENDER TRACEBACK:", tb, flush=True)
        if os.path.exists(final_source_path):
            os.remove(final_source_path)
        raise HTTPException(status_code=422, detail=f"Render failed:\n{tb[-2000:]}")

    if os.path.exists(final_source_path):
        os.remove(final_source_path)

    if not os.path.exists(output_path):
        raise HTTPException(status_code=422, detail="Render finished but no output file was produced.")

    _cleanup_later(output_path)

    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {
        "video_url": f"{base_url}/files/{file_id}_final.mp4",
        "on_screen_caption": on_screen_caption,
        "caption": full_caption,
        "download_method": meta.get("method", ""),
    }


@app.get("/files/{filename}")
def get_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    return FileResponse(path, media_type="video/mp4", headers={"Content-Disposition": "inline"})


# Serves the submission page's own static assets, if any are added later
# (CSS/JS files). The page itself is served by the "/" route above.
app.mount("/static", StaticFiles(directory="static"), name="static")
