import os
import uuid
import time
import threading

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
    output_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp4")

    ydl_opts = {
        "outtmpl": output_path,
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([req.url])
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not download video: {e}")

    if not os.path.exists(output_path):
        raise HTTPException(status_code=422, detail="Download finished but no file was produced.")

    # Delete the file after 10 minutes — plenty of time for Creatomate to fetch it
    def cleanup():
        time.sleep(600)
        if os.path.exists(output_path):
            os.remove(output_path)

    threading.Thread(target=cleanup, daemon=True).start()

    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return {"video_url": f"{base_url}/files/{file_id}.mp4"}


@app.get("/files/{filename}")
def get_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    return FileResponse(path, media_type="video/mp4", filename=filename)
