import os
import json
import requests

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CHAT_URL = "https://api.openai.com/v1/chat/completions"

# The house voice for @wokevision_: sharp political/cultural commentary,
# openly sarcastic, aimed at a socially-conscious audience that enjoys
# edgy takes. Overridable via the CAPTION_STYLE_PROMPT env var if the voice
# needs tuning without a code change.
BRAND_VOICE = os.environ.get("CAPTION_STYLE_PROMPT", """You write for @wokevision_, an Instagram-first page doing sharp, \
openly sarcastic political and cultural commentary. The house voice: \
edgy, savage, unapologetic, dripping with sarcasm aimed at liberal \
hypocrisy and performative politics -- think a politically engaged \
friend who's done being polite about it, not a brand account. Confident \
and cutting, never hedged, never "on the other hand." Short, punchy \
sentences. Dry wit over exclamation points. Specific jabs beat generic \
ones -- if the source material gives you a concrete detail or contradiction \
to skewer, use it instead of a generic dunk. Never attack people over \
protected traits (race, religion, sexuality, disability, etc.) -- the \
target is ideas, hypocrisy and politicians/public figures' actions, not \
who someone is.""")

ON_SCREEN_SYSTEM = BRAND_VOICE + """

Your job right now: write ONE short line of text to overlay directly on \
top of a video, in the white space above it. Under 12 words. This is the \
hook -- it should land as a single punch, not a summary. You may include \
up to 2 emoji if they land well; skip them if they don't add anything. No \
hashtags, no quotation marks around the line itself.

Respond ONLY with JSON: {"on_screen": "..."}"""

POSTING_SYSTEM = BRAND_VOICE + """

Your job right now: write the caption that goes out with this video when \
it's posted simultaneously to Instagram, YouTube, X, TikTok, Threads and \
Facebook. 2-4 sentences, savage and sharp, building on the hook rather \
than repeating it. No hashtags inside the caption body itself -- those \
come separately. Then give 5-8 relevant hashtags (a mix of broad reach \
tags and topic-specific ones).

Respond ONLY with JSON: {"caption": "...", "hashtags": ["#...", "#..."]}"""


def _build_context(transcript: str, meta: dict, extra_note: str = "") -> str:
    title = (meta or {}).get("title") or ""
    description = (meta or {}).get("description") or ""
    context = (transcript or "").strip() or f"{title}\n{description}".strip()
    if not context:
        context = "No transcript, title or description available -- write something generic but on-brand."
    if extra_note:
        context += f"\n\n{extra_note}"
    return context[:4000]


def _call_openai(system_prompt: str, user_content: str) -> dict:
    resp = requests.post(
        CHAT_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            # A little temperature so "regenerate" calls actually come back
            # different rather than near-identical rewordings.
            "temperature": 0.95,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def generate_on_screen_caption(transcript: str, meta: dict, avoid: str = None) -> str:
    """The short line overlaid on the video itself. Falls back to the
    video's own title when no OPENAI_API_KEY is set or the call fails --
    the pipeline still produces something usable either way."""
    title = (meta or {}).get("title") or "Watch this"
    if not OPENAI_API_KEY:
        return title

    note = f'A previous version was: "{avoid}" -- write a genuinely different one, not a light rewording.' if avoid else ""
    try:
        data = _call_openai(ON_SCREEN_SYSTEM, _build_context(transcript, meta, note))
        return (data.get("on_screen") or "").strip() or title
    except Exception as e:
        print(f"ON-SCREEN CAPTION GEN FAILED: {e}", flush=True)
        return title


def generate_posting_caption(transcript: str, meta: dict, on_screen_caption: str = "", avoid: str = None) -> str:
    """The longer caption for the actual social posts (Instagram, YouTube,
    X, TikTok, Threads, Facebook), hashtags included. Falls back to the
    video's own title/description when no OPENAI_API_KEY is set or the call
    fails."""
    title = (meta or {}).get("title") or ""
    description = (meta or {}).get("description") or ""
    fallback = title or (description[:80] if description else "") or "Watch this"
    if not OPENAI_API_KEY:
        return fallback

    note = ""
    if on_screen_caption:
        note += f'The on-screen hook text on the video is: "{on_screen_caption}". Build on it, don\'t just restate it.'
    if avoid:
        note += f' A previous caption was: "{avoid}" -- write a genuinely different take, not a light rewording.'

    try:
        data = _call_openai(POSTING_SYSTEM, _build_context(transcript, meta, note))
        body = (data.get("caption") or "").strip()
        hashtags = " ".join(h for h in data.get("hashtags", []) if h)
        return f"{body}\n\n{hashtags}".strip() or fallback
    except Exception as e:
        print(f"POSTING CAPTION GEN FAILED: {e}", flush=True)
        return fallback
