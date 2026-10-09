"""Lets the caption writer actually *see* the video.

A handful of frames (plus the transcript and original post text) go to a
vision model once per video. It writes a plain-English "read" of what is
happening: who is on screen, their expressions and body language, any text
on screen, whether it's satire or sincere, and who comes off badly. That read
is stored on the job (meta["video_read"]) and fed to every caption prompt, so
regenerating captions never repeats the vision call.

Best-effort: any failure returns "" and captions fall back to text-only.
"""
import base64
import os
import subprocess
import tempfile

import requests

import db
import speech

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CHAT_URL = "https://api.openai.com/v1/chat/completions"
FRAME_COUNT = int(os.environ.get("VISION_FRAMES", "6"))
VISION_MODEL = os.environ.get("VISION_MODEL", "gpt-4o")

_SYSTEM = """You are the eyes for a social-media caption writer. You are shown frames \
taken in order from one short video, plus its transcript (which may be empty) \
and the original post's title/description. Work out exactly what is going on so \
the writer doesn't have to guess.

Describe, in plain English and concretely:
- what happens from start to finish (the story, in order);
- who is on screen (names ONLY if shown on screen or said in the transcript -- never identify anyone from their face), \
and their facial expressions, body language and reactions;
- any text, captions, logos or chyrons visible;
- the tone: sincere clip, satire, dark humour, meme/animation, or a real confrontation;
- the key moment or punchline, and who or what comes off looking bad, foolish or hypocritical;
- the real-world event, topic or controversy it relates to, if you can tell.
Be literal about what you can see and say when you are unsure. Do not editorialise or write a caption.

Respond ONLY with JSON: {"story": "...", "people": "...", "on_screen_text": "...", "tone": "...", \
"key_moment": "...", "who_looks_bad": "...", "topic": "..."}"""


def extract_frames(video_path: str, n: int = None) -> list:
    """Evenly spaced JPEG frames as base64 strings (max ~768px wide)."""
    n = n or FRAME_COUNT
    try:
        dur = float(speech._probe_duration(video_path) or 0)
    except Exception:
        dur = 0
    if dur <= 0:
        dur = 10.0
    times = [dur * (i + 0.5) / n for i in range(n)]
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(times):
            p = os.path.join(td, f"f{i}.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video_path,
                     "-frames:v", "1", "-vf", "scale='min(768,iw)':-2", "-q:v", "4", p],
                    check=True, timeout=40)
                with open(p, "rb") as fh:
                    out.append(base64.b64encode(fh.read()).decode())
            except Exception:
                continue
    return out


def watch(video_path: str, transcript: str, meta: dict) -> str:
    """Returns a compact text 'read' of the video, or '' if unavailable."""
    if not OPENAI_API_KEY or not video_path or not os.path.exists(video_path):
        return ""
    try:
        frames = extract_frames(video_path)
        if not frames:
            return ""
        meta = meta or {}
        text = (f"Original title: {meta.get('title') or ''}\n"
                f"Original description: {(meta.get('description') or '')[:500]}\n"
                f"Source account: {meta.get('uploader') or ''}\n"
                f"Transcript: {(transcript or '').strip()[:2500] or '[none]'}\n"
                f"There are {len(frames)} frames, in order.")
        content = [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + f, "detail": "high"}} for f in frames]
        r = requests.post(
            CHAT_URL, headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": VISION_MODEL, "response_format": {"type": "json_object"}, "temperature": 0.2,
                  "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}]},
            timeout=90)
        r.raise_for_status()
        body = r.json()
        u = body.get("usage") or {}
        try:
            db.record_ai_usage("vision", VISION_MODEL, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        except Exception:
            pass
        import json
        d = json.loads(body["choices"][0]["message"]["content"])
        labels = [("story", "What happens"), ("people", "People and expressions"), ("on_screen_text", "Text on screen"),
                  ("tone", "Tone"), ("key_moment", "Key moment"), ("who_looks_bad", "Who comes off badly"), ("topic", "Topic")]
        return "\n".join(f"{lab}: {str(d.get(k)).strip()}" for k, lab in labels if d.get(k) and str(d.get(k)).strip())
    except Exception as e:
        print(f"VISION READ FAILED: {e}", flush=True)
        return ""
