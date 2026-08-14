import os
import uuid
import time
import threading
import subprocess
import json
import requests

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
import yt_dlp

from render import render_video

app = FastAPI()

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class DownloadRequest(BaseModel):
    url: str


class Segment(BaseModel):
    start: float
    end: float
    text: str


class RenderRequest(BaseModel):
    video_url: str
    caption_text: str
    segments: List[Segment]


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


@app.post("/render")
def render(req: RenderRequest):
    file_id = str(uuid.uuid4())
    source_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_source.mp4")
    output_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_final.mp4")

    try:
        segments_list = json.loads(req.segments)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"segments field wasn't valid JSON: {e}")

    try:
        resp = requests.get(req.video_url, stream=True, timeout=60)
        resp.raise_for_status()
        with open(source_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not fetch video_url: {e}")

    try:
        render_video(
            source_path=source_path,
            caption_text=req.caption_text,
            segments=segments_list,
            output_path=output_path,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Render failed: {e}")
    finally:
        if os.path.exists(source_path):
            os.remove(source_path)

    if not os.path.exists(output_path):
        raise HTTPException(status_code=422, detail="Render finished but no output file was produced.")

    def cleanup():
        time.sleep(1200)
        if os.path.exists(output_path):
            os.remove(output_path)

    threading.Thread(target=cleanup, daemon=True).start()

    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {"render_url": f"{base_url}/files/{file_id}_final.mp4"}
