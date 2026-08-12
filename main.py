import os
import uuid
import time
import threading
import subprocess

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI()

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class DownloadRequest(BaseModel):
    url: str


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/download")
def download(req: DownloadRequest):
    file_id = str(uuid.uuid4())
    raw_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_raw.mp4")
    final_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp4")

    ydl_opts = {
        "outtmpl": raw_path,
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([req.url])
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not download video: {e}")

    if not os.path.exists(raw_path):
        raise HTTPException(status_code=422, detail="Download finished but no file was produced.")

    # Re-mux with faststart so metadata sits at the front of the file — lets
    # services like Creatomate validate/stream it without needing a full download first
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", raw_path, "-c", "copy", "-movflags", "+faststart", final_path],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Could not process video for streaming: {e.stderr.decode(errors='ignore')[:300]}",
        )
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)

    if not os.path.exists(final_path):
        raise HTTPException(status_code=422, detail="Video processing finished but no output file was produced.")

    # Delete the file after 20 minutes — plenty of time for Creatomate to fetch it
    def cleanup():
        time.sleep(1200)
        if os.path.exists(final_path):
            os.remove(final_path)

    threading.Thread(target=cleanup, daemon=True).start()

    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {"video_url": f"{base_url}/files/{file_id}.mp4"}


@app.get("/files/{filename}")
def get_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    return FileResponse(path, media_type="video/mp4", headers={"Content-Disposition": "inline"})
