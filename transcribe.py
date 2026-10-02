import os
import requests

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"


def transcribe_audio(video_path: str) -> str:
    """Returns a plain-text transcript of the video's audio track.

    If no OPENAI_API_KEY is configured, returns an empty string -- the rest
    of the pipeline still works without it, the caption generator just falls
    back to the video's own title/description instead of what's actually
    said in it. This keeps the whole thing usable at $0 if you don't want to
    add a paid API key; it's only smarter (not required) with one."""
    if not OPENAI_API_KEY:
        return ""

    try:
        with open(video_path, "rb") as f:
            resp = requests.post(
                WHISPER_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": (os.path.basename(video_path), f, "video/mp4")},
                data={"model": "whisper-1"},
                timeout=120,
            )
        resp.raise_for_status()
        return resp.json().get("text", "").strip()
    except Exception as e:
        print(f"TRANSCRIBE FAILED: {e}", flush=True)
        return ""
