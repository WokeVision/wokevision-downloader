import os
import uuid
import subprocess
import requests

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
TMP_DIR = "/tmp/downloads"


def _extract_audio(video_path: str):
    """Pulls just the audio track out as a small mono 16kHz MP3 before
    uploading to Whisper. Whisper only needs speech-quality audio, so
    sending the whole source video (often 10-50x larger) wastes most of the
    upload time on video bytes Whisper immediately throws away. Returns the
    audio file path, or None if extraction fails (caller falls back to the
    original video file)."""
    os.makedirs(TMP_DIR, exist_ok=True)
    audio_path = os.path.join(TMP_DIR, f"audio_{uuid.uuid4()}.mp3")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
                audio_path,
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            return audio_path
        print(f"AUDIO EXTRACT FAILED (ffmpeg rc={result.returncode}): {result.stderr[-1000:]}", flush=True)
    except Exception as e:
        print(f"AUDIO EXTRACT FAILED: {e}", flush=True)
    if os.path.exists(audio_path):
        os.remove(audio_path)
    return None


def transcribe_audio(video_path: str) -> str:
    """Returns a plain-text transcript of the video's audio track.

    If no OPENAI_API_KEY is configured, returns an empty string -- the rest
    of the pipeline still works without it, the caption generator just falls
    back to the video's own title/description instead of what's actually
    said in it. This keeps the whole thing usable at $0 if you don't want to
    add a paid API key; it's only smarter (not required) with one."""
    if not OPENAI_API_KEY:
        return ""

    audio_path = _extract_audio(video_path)
    upload_path = audio_path or video_path
    content_type = "audio/mpeg" if audio_path else "video/mp4"

    try:
        with open(upload_path, "rb") as f:
            resp = requests.post(
                WHISPER_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": (os.path.basename(upload_path), f, content_type)},
                data={"model": "whisper-1"},
                timeout=120,
            )
        resp.raise_for_status()
        return resp.json().get("text", "").strip()
    except Exception as e:
        print(f"TRANSCRIBE FAILED: {e}", flush=True)
        return ""
    finally:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
