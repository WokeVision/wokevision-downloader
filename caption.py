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


# ---------------------------------------------------------------------------
# Editorial stance guide -- the "who is WokeVision and what does it believe"
# brief that every caption prompt carries. Written from the owner's own
# answers. Override wholesale with the STANCE_GUIDE env var, or just edit
# here. The model is told to treat it as the authority over its own default
# instincts (which lean sympathetic/neutral and so get WokeVision wrong).
# ---------------------------------------------------------------------------
STANCE_GUIDE = os.environ.get("STANCE_GUIDE", """EDITORIAL STANCE (authoritative -- follow this over your own instincts):
- WokeVision is right-leaning, anti-woke and anti-establishment, with a \
common-sense, traditional-values bent: personal responsibility, family, \
free speech, skepticism of institutions, media, politicians and corporations \
pushing progressive agendas. Mocking the left, 'wokeness' and performative \
politics is the default target; other establishments get mocked too.
- Never take the progressive/'be kind'/'empowerment' side of a culture-war \
topic. Never praise, defend or celebrate things WokeVision would mock. When \
unsure which way a topic leans, take the skeptical, common-sense, \
anti-establishment view -- never the sympathetic-to-the-trend one.
- OnlyFans / sex work / 'degeneracy': mostly mock the people defending it \
and the 'empowerment' framing -- the hypocrisy, the spin, the celebrity \
cheerleading -- more than the individuals. Occasionally treat it as plainly \
a symptom of cultural decline. Never celebrate it, never present it as \
admirable, never be a cheerleader for it.
- DARK HUMOUR / MEMES / ANIMATIONS: many clips are satire or dark comedy \
about real events or people. Recognise this. Do NOT read a joke as a \
sincere news report or sincere opinion, and do not moralise at it. Play \
along in the same humour, or add a dry, deadpan, knowing one-liner. Nod at \
the real event the joke is about (name it if the clip does) but never be \
earnest, preachy or sympathetic-corporate about it. Don't over-explain.
- Real victims/tragedies: the joke targets the situation, the system, or \
the absurdity -- never celebrate harm to an innocent victim.
- CAPTION MODES -- the owner's real captions vary with the clip, so first \
pick the mode that fits, then write in it:
  (1) QUICK REACTION -- funny/meme/viral clips: one short punchy line (a \
reaction or a joke) + one emoji, e.g. "Johnny took no prisoners 🤣". \
  (2) FACT DROP -- a claim or clip that "people don't realise": a short \
hook + one line saying what it shows, e.g. "Crazy how many people don't \
realise this 💀".
  (3) NEWS / STORY -- a real event or controversy: 2-3 plain sentences of \
context (who, what, the ironic twist), then an engagement question, e.g. "What \
do you think about this debacle? 🤔👇".
  (4) RANT / COMMENTARY -- opinion or culture-war clips (woke activists, \
street arguments, education, politicians): a longer, passionate, sarcastic \
paragraph in the owner's voice -- big stakes language ("the matrix is \
glitching", "clown world", "rules for thee but not for me", "the audacity"), \
a few emoji (🤡😂🤯💀), ending on a punch line. Never a dry essay.
  Satire/dark-humour clips use mode 1 or a deadpan version of 3.
- Match the length to the mode. Don't write a rant for a quick joke, and \
don't write one line for a clip that needs context.
- Never attack people for protected traits. Target ideas, hypocrisy, \
institutions and public figures' actions.""")


# ---------------------------------------------------------------------------
# Real-voice examples: the page's own best-performing captions, pulled live
# from Instagram (cached), cleaned of store promos/hashtags, and shown to the
# model as the target voice and LENGTH. The owner's top posts are short,
# casual reactions -- the model's default is a paragraph, so examples matter.
# ---------------------------------------------------------------------------
import time as _time
_EX_CACHE = {"at": 0.0, "text": ""}


def _clean_example(cap: str) -> str:
    # Everything before the first "-" separator line / hashtag block / promo.
    cap = (cap or "").replace("\r", "")
    cap = re.split(r"\n\s*-\s*(?:\n|$)|\n\s*#|DON.T FORGET|link in bio", cap, flags=re.I)[0]
    return cap.strip()


CURATED_EXAMPLES = """REAL @wokevision_ CAPTIONS THE OWNER LIKES (match the voice, length and hashtag style for the matching mode):

[QUICK REACTION]
Johnny took no prisoners \U0001f923
#funnymemes #wokememes #running #race #genders

[FACT DROP]
Crazy how many people don't realise this \U0001f480
On a podcast, Andrew Tate explains how our phones are being surveyed without our knowledge or consent
#redpill #surveillance #phones #truth #exposed

[NEWS / STORY]
Billie Eilish has gone viral recently for her Anti ICE speech, suggesting that no one can be illegal on 'stolen land'. Much of mainstream media hasn't taken kindly to the singers comments, with a native tribe recently claiming that her 3 million dollar mansion itself is situated on their stolen land. What do you think about this debacle? \U0001f914\U0001f447
#billieeilish #stolenland #grammys #speech #politics

[RANT / COMMENTARY]
The absolute AUDACITY on display here is unreal. \U0001f92f This street preacher is out here minding his own business and spreading his message when he gets completely blindsided by a woke activist who thinks physical harassment is an acceptable response to speech. Notice how they immediately try to switch up the narrative and play the victim card the second they get called out? The matrix is glitching in real-time. \U0001f602 They want total validation for their identity but refuse to grant basic human respect to anyone else. Rules for thee but not for me! \U0001f921
Credit: @clark_flipper
#Woke #WokeCulture #ClownWorld #MatrixGlitch #DoubleStandards

[RANT / COMMENTARY]
We are officially witnessing the final boss of the matrix. The absolute collapse of basic cognitive functioning is on full display here. Decades of institutional captured education have produced adults who can't even identify foundational geography or human biology, yet they speak with absolute, unearned moral authority. The simulation is officially broken.
Credit: @prageru
#Brainwashed #Matrix #VirtueSignaling #ObjectiveTruth #antiwoke"""


def style_examples() -> str:
    return CURATED_EXAMPLES


HASHTAG_STRATEGY = """HASHTAG STRATEGY (reach + relevance): mix three kinds, using only tags \
that fit THIS clip -- 
 * #WokeVision is NOT required -- skip it unless it genuinely fits; spend every slot on tags that drive reach and engagement
 * topic tags specific to the clip (names, events, themes), e.g. #billieeilish #stolenland #surveillance
 * popular reach tags from this pool, picked by theme: general humour -- \
#funny #funnymeme #lol #lolmeme #memesdaily #memepage #comedy #viral #viralvideo; \
politics/news -- #politics #USA #drama #beef #crazy #news; culture war -- \
#woke #antiwoke #wokememes #wokeculture #clownworld #satire #darkhumour. \
Only use #lgbtmemes / #transmemes when the clip is actually about those topics. \
Never stuff unrelated tags and never use #fyp/#foryou."""

READ_THE_CLIP = """BEFORE WRITING, work out in the "angle_read" field (1-2 short sentences): \
(a) what is this clip actually about / which real event or person, (b) is it \
satire/dark humour/meme or a sincere clip, (c) which side WokeVision takes. \
Then write everything to match that read."""

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

# Platform minimums the posting caption is built against, so ONE caption
# works unedited on every platform it goes out to (Instagram, YouTube, X,
# TikTok, Threads, Facebook) -- always the most restrictive limit of the
# bunch, never an average or a per-platform variant:
#   - POSTING_CAPTION_MAX_CHARS: X/Twitter's standard (non-Premium) post
#     limit, 280 characters -- by far the shortest of the lot (Threads 500,
#     Instagram/TikTok ~2,200, YouTube ~5,000).
#   - POSTING_HASHTAG_COUNT: 5. Threads' "one topic tag" limit only applies
#     to its special clickable topic-tag feature -- plain #hashtag text
#     written directly in a post's body (which is what this app posts) is
#     not limited to one there, and Instagram/X/TikTok/YouTube all allow
#     5+ comfortably, so 5 is the count that matches how these posts have
#     actually been used (e.g. cross-posted from Instagram to Threads).
POSTING_CAPTION_MAX_CHARS = int(os.environ.get("POSTING_CAPTION_MAX_CHARS", "280"))
POSTING_HASHTAG_COUNT = int(os.environ.get("POSTING_HASHTAG_COUNT", "5"))

# The explicit copywriter brief for the posting caption specifically --
# separate from BRAND_VOICE (used for the on-screen hook) because this is
# what actually ships as the post description, so it carries the fuller
# direction: match the @wokevision_ Instagram page's own tone, end on an
# engagement line, and fit every platform's limits unedited.
POSTING_VOICE = os.environ.get("CAPTION_STYLE_PROMPT_POSTING", """You are the social media copywriter for @wokevision_. Your job is to write \
highly engaging descriptions with proper formatting, in the tone of voice, \
and political stance of content, captions and descriptions of the \
@wokevision_ Instagram page: sharp, openly sarcastic political and cultural \
commentary -- edgy, savage, unapologetic, dripping with sarcasm aimed at \
liberal hypocrisy and performative politics, like a politically engaged \
friend who's done being polite about it, not a brand account. Confident and \
cutting, never hedged, never "on the other hand." Specific jabs beat \
generic ones. Never attack people over protected traits (race, religion, \
sexuality, disability, etc.) -- the target is ideas, hypocrisy and public \
figures' actions, not who someone is.""")

POSTING_RULES = f"""Write the caption/description that goes out with this video when it's \
posted simultaneously to Instagram, YouTube, X, TikTok, Threads and \
Facebook. Properly formatted, genuinely engaging, on-brand -- not generic.

End the caption with exactly ONE short line that is either a question or a \
statement built to drive engagement -- for example (don't just reuse these \
verbatim every time): "Do you agree with them?", "What do you think about \
this?", "Is this a bit too far?", "Should we all be thinking like this?", \
"What would you do in their situation?", "This is too cold.", "Protect \
this person at all costs.", "I wouldn't want to get into it with them." \
That line is immediately followed by exactly ONE emoji that amplifies it \
-- the very last character, no punctuation between the line and the emoji.

Include exactly {POSTING_HASHTAG_COUNT} hashtags, each a separate \
#WokeVision-style tag relevant to the content -- this count works unedited \
on every platform this goes out to.

The ENTIRE caption -- body, the question/statement line, and the hashtags \
all included -- must fit within {POSTING_CAPTION_MAX_CHARS} characters \
total. Hashtags take about 55 of those, so the text before them MUST be \
under {POSTING_CAPTION_MAX_CHARS - 65} characters INCLUDING the closing \
question and emoji -- count carefully and finish the sentence; never run \
long and get cut off. That's X's standard post limit, the shortest of any platform this \
goes out to, so nothing needs trimming per platform."""

COMBINED_SYSTEM = BRAND_VOICE + "\n\n" + STANCE_GUIDE + "\n\n" + HASHTAG_STRATEGY + "\n\n" + READ_THE_CLIP + f"""

You have two things to write for the same video, in one response.

1) ON-SCREEN HOOK: {ON_SCREEN_RULES}

2) POSTING CAPTION: {POSTING_VOICE}

{POSTING_RULES}

Respond ONLY with JSON: {{"angle_read": "...", "on_screen": "...", "caption": "...", "hashtags": ["#...", "#..."]}}"""

ON_SCREEN_SYSTEM = BRAND_VOICE + "\n\n" + STANCE_GUIDE + "\n\n" + READ_THE_CLIP + f"\n\nYour job right now: {ON_SCREEN_RULES}\n\nRespond ONLY with JSON: {{\"angle_read\": \"...\", \"on_screen\": \"...\"}}"

POSTING_SYSTEM = POSTING_VOICE + "\n\n" + STANCE_GUIDE + "\n\n" + HASHTAG_STRATEGY + "\n\n" + READ_THE_CLIP + f"\n\nYour job right now: {POSTING_RULES}\n\nRespond ONLY with JSON: {{\"angle_read\": \"...\", \"caption\": \"...\", \"hashtags\": [\"#...\"]}}"

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


def _trim_to_chars(text: str, limit: int) -> str:
    """Hard-trims to `limit` characters, backing up to the last whitespace
    so it doesn't cut off mid-word. Last-resort safety net -- the prompt
    already asks for this, this just guarantees it."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip()


def _assemble_posting_caption(body: str, hashtags: list) -> str:
    """Joins the caption body with its hashtag(s) and guarantees the result
    fits POSTING_CAPTION_MAX_CHARS and carries exactly POSTING_HASHTAG_COUNT
    hashtags -- regardless of what the model actually returned -- so the
    same caption is always safe to post unedited on every platform."""
    body = (body or "").strip()
    tags = [h.strip() for h in (hashtags or []) if h and h.strip()]
    if tags:
        mixed = enforce_tag_mix(" ".join(tags), body, POSTING_HASHTAG_COUNT)
        tags = _HASHTAG_RE.findall(mixed)
    tags = tags[:POSTING_HASHTAG_COUNT]
    # Pad with generic fallback tags (never a duplicate) until there are
    # exactly POSTING_HASHTAG_COUNT, regardless of how many the model
    # returned -- a single fallback tag used to only ever fill one slot.
    fallback_pool = ["#Politics", "#Viral", "#News", "#Trending", "#Funny"]
    fallback_iter = iter(t for t in fallback_pool if t not in tags)
    while len(tags) < POSTING_HASHTAG_COUNT:
        try:
            tags.append(next(fallback_iter))
        except StopIteration:
            tags.append("#Viral")
    tags = tags[:POSTING_HASHTAG_COUNT]
    tag_str = " ".join(tags)

    separator = "\n\n"
    budget = POSTING_CAPTION_MAX_CHARS - len(separator) - len(tag_str)
    if budget < 0:
        # The hashtag alone blows the budget (shouldn't happen at normal
        # lengths) -- there's nothing sensible left to show but the tag.
        return tag_str[:POSTING_CAPTION_MAX_CHARS]
    body = _trim_to_chars(body, budget)
    return f"{body}{separator}{tag_str}".strip()


# Popular reach tags, grouped by theme, used both in the prompt and to
# enforce the mix in code (the model likes to invent #RoastBattle-style tags).
POOL_HUMOUR = ["#funny", "#funnymeme", "#lol", "#lolmeme", "#memesdaily", "#memepage", "#comedy", "#viral", "#viralvideo"]
POOL_POLITICS = ["#politics", "#USA", "#drama", "#beef", "#crazy", "#viral"]
POOL_CULTURE = ["#woke", "#antiwoke", "#wokememes", "#satire", "#darkhumour", "#clownworld", "#memepage"]
_POOL_ALL = {t.lower() for t in POOL_HUMOUR + POOL_POLITICS + POOL_CULTURE + ["#lgbtmemes", "#transmemes", "#wokeculture", "#news"]}
_POLITICS_WORDS = re.compile(r"politic|trump|biden|harris|election|vote|congress|senate|government|police|cop\b|ice\b|immigra|border|president|democrat|republican|court|law\b|military|war\b|tax", re.I)
_CULTURE_WORDS = re.compile(r"woke|trans\b|lgbt|dei\b|feminis|activist|liberal|gender|onlyfans|degenera|pronoun|leftist|protest|brainwash", re.I)


def enforce_tag_mix(text: str, theme_text: str = "", total: int = 5) -> str:
    """Rewrites the trailing hashtag set of `text` so it has #WokeVision plus at
    least two popular pool tags (themed to the clip), keeping the model's own
    clip-specific tags, at most `total` tags. Hashtags are moved to the end."""
    if not text:
        return text
    found, seen = [], set()
    for t in _HASHTAG_RE.findall(text):
        if t.lower() not in seen:
            seen.add(t.lower()); found.append(t)
    body = re.sub(r"[ \t]{2,}", " ", _HASHTAG_RE.sub("", text)).strip()
    brand = "#WokeVision"
    tags = [t for t in found if t.lower() != brand.lower()]
    probe = f"{theme_text} {body}"
    theme = []
    if _CULTURE_WORDS.search(probe):
        theme += POOL_CULTURE
    if _POLITICS_WORDS.search(probe):
        theme += POOL_POLITICS
    theme += POOL_HUMOUR
    pool_have = [t for t in tags if t.lower() in _POOL_ALL]
    specific = [t for t in tags if t.lower() not in _POOL_ALL]
    need = max(0, 2 - len(pool_have))
    adds = []
    for t in theme:
        if len(adds) >= need:
            break
        if t.lower() not in seen and t.lower() not in {a.lower() for a in adds}:
            adds.append(t)
    keep_brand = [t for t in found if t.lower() == brand.lower()]
    keep_specific = max(0, total - len(keep_brand) - len(pool_have) - len(adds))
    final = keep_brand + specific[:keep_specific] + pool_have + adds
    final = final[:total]
    return f"{body}\n\n{' '.join(final)}" if body else " ".join(final)


def _clean_handle(h: str) -> str:
    h = (h or "").strip().lstrip("@")
    if not h or " " in h or h.isdigit() or len(h) > 40 or "/" in h:
        return ""
    return h


def credit_handle(meta: dict) -> str:
    """The original creator's handle for the 'Credit: @x' line, or '' when
    none can be found -- or when the page we got the video from looks like a
    repost/aggregator rather than the creator (meta["credit_ok"] is False,
    see decide_credit)."""
    meta = meta or {}
    if meta.get("credit_ok") is False:
        return ""
    for key in ("uploader_id", "channel", "uploader"):
        h = _clean_handle(meta.get(key))
        if h:
            return h
    url = meta.get("source_url") or ""
    m = re.search(r"(?:tiktok\.com|youtube\.com|threads\.(?:net|com))/@([A-Za-z0-9._-]+)", url) \
        or re.search(r"(?:instagram\.com|x\.com|twitter\.com)/([A-Za-z0-9._]+)/(?:reel|p|status|tv)", url)
    if m and m.group(1).lower() not in ("reel", "p", "i", "share"):
        return _clean_handle(m.group(1))
    return ""


def decide_credit(meta: dict, transcript: str) -> bool:
    """False only when the source account is obviously a repost/aggregator
    page (clips, memes, news roundups, 'viral' pages etc.) rather than the
    person/outlet that made the video. Any doubt or failure -> True (credit)."""
    handle_src = (meta or {}).get("uploader") or (meta or {}).get("uploader_id") or ""
    if not OPENAI_API_KEY or not handle_src:
        return True
    try:
        data = _call_openai(
            "You decide whether to credit the account a video was downloaded from. Answer JSON "
            '{"original": true|false}. true = the account looks like the ORIGINAL creator/owner of the '
            "video (a person filming themselves, a journalist/outlet/channel posting its own footage, a "
            "streamer, a company, a podcast's own channel). false = it is OBVIOUSLY a repost/aggregator "
            "page (generic viral/meme/clips/news-compilation/fan pages reposting other people's footage, "
            "watermarks of other accounts mentioned in the title or description). When unsure answer true.",
            f"Source account: {handle_src}\nTitle: {(meta or {}).get('title','')}\n"
            f"Description: {((meta or {}).get('description') or '')[:500]}\nTranscript: {(transcript or '')[:600]}",
        )
        return bool(data.get("original", True))
    except Exception as e:
        print(f"CREDIT DECISION FAILED: {e}", flush=True)
        return True


def _ensure_credit(text: str, handle: str) -> str:
    """Guarantees exactly one 'Credit: @handle' line, placed just before the
    trailing hashtag line(s) (or at the end when there are none)."""
    if not handle or not text:
        return text
    if re.search(r"credit\s*:\s*@", text, re.I):
        return text
    lines = text.rstrip().split("\n")
    i = len(lines)
    while i > 0 and (not lines[i - 1].strip() or all(w.startswith("#") for w in lines[i - 1].split())):
        i -= 1
    return "\n".join(lines[:i] + [f"Credit: @{handle}"] + lines[i:]) if i < len(lines) else "\n".join(lines + ["", f"Credit: @{handle}"])


def _build_context(transcript: str, meta: dict, extra_note: str = "") -> str:
    title = (meta or {}).get("title") or ""
    description = (meta or {}).get("description") or ""
    transcript = (transcript or "").strip()
    uploader = (meta or {}).get("uploader") or ""
    handle = credit_handle(meta)
    angle = ((meta or {}).get("angle") or "").strip()
    parts = []
    if angle:
        parts.append(f"OPERATOR NOTE FROM THE PAGE OWNER (authoritative -- this is what the clip is about and how to treat it; it overrides your own reading):\n{angle}")
    if uploader:
        parts.append(f"Source account/channel: {uploader}")
    if handle:
        parts.append(f"Credit handle: @{handle}")
    if title:
        parts.append(f"Original post title: {title}")
    if description:
        parts.append(f"Original post description: {description[:700]}")
    spoken = transcript[:3000] or "[none]"
    parts.append(f"Spoken transcript (may be empty or music-only for memes/animations):\n{spoken}")
    context = "\n\n".join(parts)
    if extra_note:
        context += f"\n\n{extra_note}"
    return context[:5000]


def _call_openai(system_prompt: str, user_content: str) -> dict:
    try:
        ex = style_examples()
        if ex:
            system_prompt = system_prompt + "\n\n" + ex
    except Exception:
        pass
    resp = requests.post(
        CHAT_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": os.environ.get("CAPTION_MODEL", "gpt-4o"),
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
        posting = _assemble_posting_caption(data.get("caption"), data.get("hashtags")) or fallback_posting
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
        return _assemble_posting_caption(data.get("caption"), data.get("hashtags")) or fallback
    except Exception as e:
        print(f"POSTING CAPTION GEN FAILED: {e}", flush=True)
        return fallback


# ---------------------------------------------------------------------------
# Per-platform versions of the posting caption
# ---------------------------------------------------------------------------
# The master posting caption above is written to survive every platform
# unedited (280 chars, 5 hashtags). That is a compromise, so each platform
# also gets its own native version, built from these rules (researched Oct
# 2026 -- limits and best practice per platform):
#   Instagram  2,200 chars; app hard-caps 5 hashtags; only the first ~125
#              chars show before "more" so the hook goes first.
#   Threads    500 chars; ONE topic tag (API `topic_tag`, 1-50 chars, no
#              "." or "&"), set separately from the text, not as #hashtags.
#   YouTube    title <=100 (only ~50 show in feed -- keywords first, no
#              hashtags); description <=5,000 (first ~100 chars show), 3-5
#              hashtags at the end (3 surface above the title); tags field
#              <=500 chars total; category.
#   X          280 chars (standard accounts); 0-2 hashtags at most -- X's own
#              guidance says <=2, and engagement-wise zero or one is best.
#   TikTok     2,200 chars via the API; only the first 5 hashtags count;
#              keywords/hook in the first line; skip generic #fyp.
#   Facebook   hashtags barely help discovery -- 1-3; conversational hook.
PLATFORM_IDS = ["instagram", "threads", "youtube", "tiktok", "x", "facebook"]
LIM = {
    "ig_caption": 2200, "ig_tags": 5,
    "threads_text": 500, "threads_tag": 50,
    "yt_title": 100, "yt_desc": 5000, "yt_tags_chars": 500, "yt_hashtags": 5,
    "x_text": 280, "x_tags": 1,
    "tiktok_caption": 2200, "tiktok_tags": 5,
    "fb_desc": 2200, "fb_tags": 3,
}
YT_DEFAULT_CATEGORY = "25"  # News & Politics
_HASHTAG_RE = re.compile(r"#\w+")


def _split_master(master: str):
    """(body, [hashtags]) from a caption that ends in hashtags."""
    master = (master or "").strip()
    tags = _HASHTAG_RE.findall(master)
    body = _HASHTAG_RE.sub("", master)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    return body, tags


def _limit_hashtags(text: str, n: int) -> str:
    """Keeps the first n hashtags in text and strips the rest (word and all)."""
    count = 0

    def repl(m):
        nonlocal count
        count += 1
        return m.group(0) if count <= n else ""
    out = _HASHTAG_RE.sub(repl, text or "")
    return re.sub(r"[ \t]{2,}", " ", out).rstrip()


def _strip_hashtags(text: str) -> str:
    out = _HASHTAG_RE.sub("", text or "")
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _clean_topic_tag(tag: str) -> str:
    tag = (tag or "").strip().lstrip("#").replace(".", "").replace("&", "")
    tag = re.sub(r"\s+", " ", tag).strip()
    return tag[:LIM["threads_tag"]]


def _clean_yt_tags(tags) -> list:
    if isinstance(tags, str):
        tags = re.split(r"[,\n]", tags)
    out, seen, total = [], set(), 0
    for t in tags or []:
        t = str(t).strip().lstrip("#").strip()
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (1 if out else 0) + (2 if " " in t else 0)
        if total + cost > LIM["yt_tags_chars"]:
            break
        seen.add(t.lower())
        out.append(t)
        total += cost
    return out


def normalize_platform_posts(raw: dict, master: str = "") -> dict:
    """Makes any platform_posts dict (model output OR a user's saved edits)
    structurally valid: every platform present, every field the right type,
    and the hard limits that would make a platform reject the post enforced.
    Missing platforms fall back to a version derived from the master caption."""
    raw = raw or {}
    base = default_platform_posts(master)
    out = {}

    g = lambda p, k: (raw.get(p) or {}).get(k) if isinstance(raw.get(p), dict) else None

    ig = g("instagram", "caption")
    ig = base["instagram"]["caption"] if ig is None else str(ig)
    out["instagram"] = {"caption": _trim_to_chars(_limit_hashtags(ig, LIM["ig_tags"]).strip(), LIM["ig_caption"])}

    th = g("threads", "text")
    th = base["threads"]["text"] if th is None else str(th)
    tt = g("threads", "topic_tag")
    tt = base["threads"]["topic_tag"] if tt is None else str(tt)
    out["threads"] = {"text": _trim_to_chars(_limit_hashtags(th, 5).strip(), LIM["threads_text"]), "topic_tag": _clean_topic_tag(tt)}

    yt_title = g("youtube", "title")
    yt_title = base["youtube"]["title"] if yt_title is None else str(yt_title)
    yt_desc = g("youtube", "description")
    yt_desc = base["youtube"]["description"] if yt_desc is None else str(yt_desc)
    yt_tags = g("youtube", "tags")
    yt_tags = base["youtube"]["tags"] if yt_tags is None else yt_tags
    cat = str(g("youtube", "category") or YT_DEFAULT_CATEGORY)
    out["youtube"] = {
        "title": _trim_to_chars(_strip_hashtags(yt_title).strip(), LIM["yt_title"]),
        "description": _trim_to_chars(_limit_hashtags(yt_desc, LIM["yt_hashtags"]).strip(), LIM["yt_desc"]),
        "tags": _clean_yt_tags(yt_tags),
        "category": cat if cat.isdigit() else YT_DEFAULT_CATEGORY,
    }

    xt = g("x", "text")
    xt = base["x"]["text"] if xt is None else str(xt)
    out["x"] = {"text": _trim_to_chars(_limit_hashtags(xt, LIM["x_tags"]).strip(), LIM["x_text"])}

    tk = g("tiktok", "caption")
    tk = base["tiktok"]["caption"] if tk is None else str(tk)
    tkd = raw.get("tiktok") if isinstance(raw.get("tiktok"), dict) else {}
    flag = lambda k: bool(tkd.get(k)) if k in tkd else True
    out["tiktok"] = {
        "caption": _trim_to_chars(_limit_hashtags(tk, LIM["tiktok_tags"]).strip(), LIM["tiktok_caption"]),
        "allow_comments": flag("allow_comments"), "allow_duet": flag("allow_duet"), "allow_stitch": flag("allow_stitch"),
    }

    fb = g("facebook", "description")
    fb = base["facebook"]["description"] if fb is None else str(fb)
    out["facebook"] = {"description": _trim_to_chars(_limit_hashtags(fb, LIM["fb_tags"]).strip(), LIM["fb_desc"])}
    return out


def default_platform_posts(master: str) -> dict:
    """Deterministic (no AI) per-platform versions built from the master
    caption -- used when there's no OPENAI_API_KEY, when generation fails,
    and to fill any platform a saved record is missing."""
    body, tags = _split_master(master)
    first_line = (body.splitlines() or [""])[0].strip() or "WokeVision"
    tag_words = [t.lstrip("#") for t in tags]
    tags5 = " ".join(tags[:5])
    return {
        "instagram": {"caption": (body + ("\n\n" + tags5 if tags5 else "")).strip()},
        "threads": {"text": (body + ("\n\n" + tags5 if tags5 else "")).strip(), "topic_tag": tag_words[0] if tag_words else "WokeVision"},
        "youtube": {
            "title": first_line[:100],
            "description": (body + "\n\n" + " ".join((tags[:4] + ["#Shorts"])[:5])).strip(),
            "tags": tag_words, "category": YT_DEFAULT_CATEGORY,
        },
        "x": {"text": body},
        "tiktok": {"caption": (body + ("\n\n" + tags5 if tags5 else "")).strip(),
                   "allow_comments": True, "allow_duet": True, "allow_stitch": True},
        "facebook": {"description": (body + ("\n\n" + " ".join(tags[:2]) if tags else "")).strip()},
    }


PLATFORM_SYSTEM = BRAND_VOICE + "\n\n" + STANCE_GUIDE + "\n\n" + HASHTAG_STRATEGY + """

You are adapting ONE core caption into six platform-native versions of the \
same post, to maximise reach and engagement on each platform. Keep the \
same take, jabs and voice everywhere -- but each version must read as if it \
was written natively for that platform, not copy-pasted. Do not invent \
facts beyond what the transcript and caption support. The final engagement \
line should be a question or a punchy statement; vary it per platform.

PLATFORM RULES (hard limits in brackets):

instagram -- "caption" [max 2,200 chars; EXACTLY 3-5 hashtags in total]. Only \
the first ~125 characters show before "more", so open with the sharpest hook \
line. Length follows the clip's CAPTION MODE (quick reaction = 1 short line; \
rant = a full punchy paragraph, up to ~700 chars). Line breaks for rhythm. \
Weave 1-2 searchable topic keywords into the sentence itself. End with an \
engagement line, then the credit line if one is supplied, then the \
hashtags on their own line at the very end -- exactly 5: 2-3 clip-specific \
tags and 2-3 popular pool tags (#WokeVision is optional, not required).

threads -- "text" [max 500 chars] and "topic_tag" [1-50 chars, no "." or "&", \
no leading #]. Conversational, like talking to followers, ends with a \
genuine question to start replies (Threads rewards replies), then EXACTLY 5 \
hashtags on their own line at the very end (same mix as Instagram: clip-specific \
plus popular pool tags; keep the whole text under 500 chars). "topic_tag" is the single most specific topic people browse \
(e.g. "Politics", "Free Speech", "Policing") -- one tag only.

youtube -- "title" [max 100 chars, no hashtags]: front-load the keyword and \
hook in the first 40-50 characters (only ~50 show in the feed), curiosity or \
conflict, no clickbait lies, no ALL CAPS shouting. "description" [max 5,000]: \
the first line (~100 chars) is a hook that does NOT just repeat the title; \
then 1-2 short lines of context and an engagement question, then the credit \
line if supplied; end with 3-5 \
hashtags on their own line (the first three show above the title; include \
#Shorts as one of them). "tags": 8-12 search keywords/phrases (no #, total \
under 400 characters) people would actually search for this topic.

x -- "text" [max 280 chars INCLUDING any hashtag]. One sharp, tight take. \
Hashtags rarely help on X: use 0, and at most 1 only if it is a genuinely \
trending topic tag. Lead with the punch, finish with a question or hot line \
that invites replies and quote-posts. No emoji spam.

tiktok -- "caption" [max 2,200 chars; EXACTLY 3-5 hashtags -- only the \
first five count]. First line is a hook with the topic's keywords (TikTok \
search reads captions). Length follows the CAPTION MODE but stays tighter than \
Instagram (max ~3 lines). End with a prompt that drives comments ("Agree or \
nah?"), then the credit line if supplied. Hashtags at the end: exactly 5, \
the same mix as Instagram (no #WokeVision required), no #fyp/#foryou.

facebook -- "description" [max 2,200 chars]. Hashtags barely help on \
Facebook: use 1-3 at most at the end. Slightly warmer and more explanatory \
than the others, 2-4 short lines (or one line for a quick reaction), a hook \
first line, a closing question that drives comments and shares, then the \
credit line if supplied.

If the context gives a "Credit handle", the instagram, tiktok, facebook \
and youtube versions each include one line "Credit: @handle" before the \
hashtags (you may leave it out of threads/x).

Never attack people over protected traits; target ideas, hypocrisy and \
public figures' actions.

Respond ONLY with JSON in exactly this shape:
{"instagram": {"caption": "..."},
 "threads": {"text": "...", "topic_tag": "..."},
 "youtube": {"title": "...", "description": "...", "tags": ["...", "..."]},
 "x": {"text": "..."},
 "tiktok": {"caption": "..."},
 "facebook": {"description": "..."}}"""


def generate_platform_posts(transcript: str, meta: dict, on_screen_caption: str = "",
                            master_caption: str = "", only: str = None, current: dict = None) -> dict:
    """Builds the per-platform versions in one OpenAI call (so it costs one
    round trip, not six). `only` regenerates a single platform while leaving
    the rest of `current` exactly as the user has them. Always returns a
    fully valid dict -- falls back to deterministic derivations from the
    master caption when there's no API key or the call fails."""
    fallback = normalize_platform_posts(default_platform_posts(master_caption), master_caption)
    if not OPENAI_API_KEY:
        generated = fallback
    else:
        note = f'The core posting caption is:\n"""{master_caption}"""'
        if on_screen_caption:
            note += f'\nThe on-screen hook on the video is: "{on_screen_caption}".'
        if only:
            note += f'\nWrite a fresh, genuinely different take for the "{only}" version.'
        try:
            data = _call_openai(PLATFORM_SYSTEM, _build_context(transcript, meta, note))
            generated = normalize_platform_posts(data, master_caption)
        except Exception as e:
            print(f"PLATFORM POSTS GEN FAILED: {e}", flush=True)
            generated = fallback
    for pid, key in (("instagram", "caption"), ("tiktok", "caption"), ("threads", "text")):
        if isinstance(generated.get(pid), dict) and generated[pid].get(key):
            generated[pid][key] = enforce_tag_mix(generated[pid][key], f"{(meta or {}).get('title','')} {master_caption}")
    handle = credit_handle(meta)
    if handle:
        for pid, key in (("instagram", "caption"), ("tiktok", "caption"), ("facebook", "description"), ("youtube", "description")):
            if isinstance(generated.get(pid), dict) and generated[pid].get(key):
                generated[pid][key] = _ensure_credit(generated[pid][key], handle)
    if only and current:
        merged = dict(normalize_platform_posts(current, master_caption))
        if only in generated:
            merged[only] = generated[only]
        return merged
    return generated


# --- Paid-partnership / ad disclosure ----------------------------------------
_DISCLOSE_FIELDS = {"instagram": "caption", "facebook": "description", "threads": "text",
                    "x": "text", "tiktok": "caption", "youtube": "description"}


def apply_disclosure(platform: str, post: dict) -> dict:
    """Marks a post as a paid partnership: sets the platform's native flag
    where one exists (TikTok branded content, YouTube paid promotion -- read
    by those platform modules from post["paid_promo"]) and puts a clear
    on-text disclosure at the very start of the caption everywhere (the FTC
    wants it before the 'more' fold). Idempotent."""
    post = dict(post or {})
    post["paid_promo"] = True
    key = _DISCLOSE_FIELDS.get(platform)
    if key:
        text = (post.get(key) or "").lstrip()
        low = text[:80].lower()
        if "paid partnership" not in low and not low.startswith("#ad") and not low.startswith("ad:"):
            prefix = "#ad " if platform in ("x", "threads") else "Paid partnership\n\n"
            post[key] = prefix + text
    return post
