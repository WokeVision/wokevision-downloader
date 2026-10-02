import os
import json
import requests

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CHAT_URL = "https://api.openai.com/v1/chat/completions"

DEFAULT_STYLE_PROMPT = """You write short, punchy social captions for \
@wokevision_, an Instagram page covering social, political and cultural \
topics for a young, socially conscious audience. Tone: direct, \
thought-provoking, occasionally confrontational -- never dry or corporate.

You always produce two things:
1. "on_screen": a short caption (under 12 words, no hashtags, no quote \
marks) to overlay directly on the video.
2. "caption": a longer Instagram caption (2-4 sentences, no hashtags \
inside it) that expands on the point.
3. "hashtags": a list of 5-8 relevant hashtags (each starting with #).
"""

STYLE_PROMPT = os.environ.get("CAPTION_STYLE_PROMPT", DEFAULT_STYLE_PROMPT)


def _fallback(meta: dict):
    title = (meta or {}).get("title") or ""
    description = (meta or {}).get("description") or ""
    basic = title or (description[:80] if description else "") or "Watch this"
    return basic, basic


def generate_caption(transcript: str, meta: dict):
    """Returns (on_screen_caption, full_instagram_caption).

    Falls back to the video's own title/description when no OPENAI_API_KEY
    is set, or if the API call fails for any reason -- so the pipeline
    always produces something usable, it's just smarter with a key set."""
    if not OPENAI_API_KEY:
        return _fallback(meta)

    title = (meta or {}).get("title") or ""
    description = (meta or {}).get("description") or ""
    context = (transcript or "").strip() or f"{title}\n{description}".strip()
    if not context:
        context = "No transcript, title or description available -- write something generic but on-brand."

    messages = [
        {"role": "system", "content": STYLE_PROMPT},
        {"role": "user", "content": (
            "Here is the video's content/transcript:\n\n"
            f"{context[:4000]}\n\n"
            'Respond ONLY with JSON in this exact shape: '
            '{"on_screen": "...", "caption": "...", "hashtags": ["#...", "#..."]}'
        )},
    ]

    try:
        resp = requests.post(
            CHAT_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": messages,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)

        on_screen = (data.get("on_screen") or "").strip() or title or "Watch this"
        hashtags = " ".join(h for h in data.get("hashtags", []) if h)
        body = (data.get("caption") or "").strip()
        full_caption = f"{body}\n\n{hashtags}".strip()
        return on_screen, full_caption
    except Exception as e:
        print(f"CAPTION GEN FAILED: {e}", flush=True)
        return _fallback(meta)
