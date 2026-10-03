import os
import re
import json
import random
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

# Shared instructions for the on-screen hook line, reused by both the
# combined (fast, single-call) generator and the standalone regenerate-only
# one. Emoji placement is spelled out explicitly because this is the part
# that was quietly getting dropped/misplaced before.
ON_SCREEN_RULES = """Write ONE short line of text to overlay directly on top of a video, in \
the white space above it. Under 12 words. This is the hook -- it should \
land as a single punch, not a summary. No hashtags, no quotation marks \
around the line itself.

End the line with exactly ONE emoji that fits the tone -- it must be the \
very last character. Do not put a full stop, period, or any other \
punctuation between the last word and the emoji (e.g. "Libs are shaking \
🤡" not "Libs are shaking. 🤡"). Do not use more than one emoji."""

POSTING_RULES = """Write the caption that goes out with this video when it's posted \
simultaneously to Instagram, YouTube, X, TikTok, Threads and Facebook. \
2-4 sentences, savage and sharp, building on the on-screen hook rather \
than repeating it. No hashtags inside the caption body itself -- those \
come separately. Then give 5-8 relevant hashtags (a mix of broad reach \
tags and topic-specific ones)."""

COMBINED_SYSTEM = BRAND_VOICE + f"""

You have two things to write for the same video, in one response.

1) ON-SCREEN HOOK: {ON_SCREEN_RULES}

2) POSTING CAPTION: {POSTING_RULES}

Respond ONLY with JSON: {{"on_screen": "...", "caption": "...", "hashtags": ["#...", "#..."]}}"""

ON_SCREEN_SYSTEM = BRAND_VOICE + f"\n\nYour job right now: {ON_SCREEN_RULES}\n\nRespond ONLY with JSON: {{\"on_screen\": \"...\"}}"

POSTING_SYSTEM = BRAND_VOICE + f"\n\nYour job right now: {POSTING_RULES}\n\nRespond ONLY with JSON: {{\"caption\": \"...\", \"hashtags\": [\"#...\", \"#...\"]}}"

# Fallback emoji, confirmed present in the local emoji_pack/ so the on-screen
# caption always renders a real image instead of silently dropping a
# character the pack doesn't have. Used only when the model's own line
# somehow comes back with no emoji at all.
FALLBACK_EMOJI = ["\U0001F525", "\U0001F921", "\U0001F480", "\U0001F6A8", "\U0001F62D",
                   "\U0001F644", "\U0001F62F", "\U0001F60F", "\U0001F914", "\U0001F4AF"]

# Mirrors render.py's own emoji matcher closely enough to detect "is there
# an emoji in this text at all" without importing render.py (keeps caption.py
# usable standalone / in tests without the Pillow/ffmpeg stack).
_EMOJI_RE = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2})"
    "|(?:[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF\U00002B00-\U00002BFF]"
    "[\U0000FE0F\U0000200D\U0001F3FB-\U0001F3FF]*)"
)


def _enforce_single_trailing_emoji(text: str) -> str:
    """Guarantees the on-screen line ends in exactly one emoji with no
    punctuation in between, regardless of what the model actually returned:
    - strips a period/other punctuation sitting right before an emoji
    - if an emoji is present but buried mid-sentence rather than at the end,
      moves one to the end
    - if no emoji is present at all, appends a fallback one
    - collapses multiple trailing emoji down to one
    """
    text = (text or "").strip()
    if not text:
        return text

    found = _EMOJI_RE.findall(text)
    # Strip every emoji out of the body so we can cleanly re-append one.
    stripped = _EMOJI_RE.sub("", text)
    # Clean up leftover punctuation/whitespace left dangling where an emoji
    # used to sit (e.g. "Libs are shaking. " -> "Libs are shaking").
    stripped = re.sub(r"\s+", " ", stripped).strip()
    stripped = stripped.rstrip(" .!?,;:").strip()

    emoji = found[0] if found else random.choice(FALLBACK_EMOJI)
    return f"{stripped} {emoji}"


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


def generate_captions(transcript: str, meta: dict) -> tuple:
    """Generates BOTH captions (on-screen hook + posting caption/hashtags)
    in a single OpenAI call instead of two serial ones -- used for the main
    pipeline where speed matters and there's nothing to "avoid" yet. Falls
    back to the video's own title/description when no OPENAI_API_KEY is set
    or the call fails. Returns (on_screen_caption, posting_caption)."""
    title = (meta or {}).get("title") or "Watch this"
    description = (meta or {}).get("description") or ""
    fallback_posting = title or (description[:80] if description else "") or "Watch this"
    if not OPENAI_API_KEY:
        return title, fallback_posting

    try:
        data = _call_openai(COMBINED_SYSTEM, _build_context(transcript, meta))
        on_screen = _enforce_single_trailing_emoji((data.get("on_screen") or "").strip() or title)
        body = (data.get("caption") or "").strip()
        hashtags = " ".join(h for h in data.get("hashtags", []) if h)
        posting = f"{body}\n\n{hashtags}".strip() or fallback_posting
        return on_screen, posting
    except Exception as e:
        print(f"CAPTION GEN FAILED: {e}", flush=True)
        return title, fallback_posting


def generate_on_screen_caption(transcript: str, meta: dict, avoid: str = None) -> str:
    """The short line overlaid on the video itself, generated alone -- used
    by the "change video caption" regenerate button. Falls back to the
    video's own title when no OPENAI_API_KEY is set or the call fails."""
    title = (meta or {}).get("title") or "Watch this"
    if not OPENAI_API_KEY:
        return title

    note = f'A previous version was: "{avoid}" -- write a genuinely different one, not a light rewording.' if avoid else ""
    try:
        data = _call_openai(ON_SCREEN_SYSTEM, _build_context(transcript, meta, note))
        return _enforce_single_trailing_emoji((data.get("on_screen") or "").strip() or title)
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
