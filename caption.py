import os
import re
import json
import random
import requests
import db

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
- WokeVision is centre-right: anti-woke and anti-establishment, with a \
common-sense, traditional-values bent (witty and grounded, never extremist or hateful): personal responsibility, family, \
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
- Captions are deliberately SIMPLE (see the caption format rules). Never write essays, rants or jokes unless asked.
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


CURATED_EXAMPLES = """EXAMPLES OF THE CORE CAPTION STYLE (one short line, then ONE emoji):
Do you agree with her? \U0001f447
Do you agree with him? \U0001f914
Is this valid? \U0001f928
Have they got a point? \U0001f440
What do you think about this? \U0001f914
What would you do in this situation? \U0001f605
How would you handle this? \U0001f440
How would you react? \U0001f928
This is crazy! \U0001f92f
Mad props to this guy \U0001f44f
He did well to stand up for himself like that \U0001f4af

EXAMPLES OF THE ON-SCREEN TOP LINE (simple, shocked at what is literally happening):
Wait, did she really just say that? \U0001f633
He actually walked up and did it \U0001f62e
The way he just stood there and took it \U0001f633
She came in swinging with zero warning \U0001f62f"""


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
satire/dark humour/meme or a sincere clip, (c) who or what the clip makes look foolish or hypocritical, and which side WokeVision takes. \
If a "WHAT THE VIDEO SHOWS" section is given, it comes from actually watching the footage -- trust it \
for who is on screen, their reactions and the story, and make your caption fit what really happens. \
Then write everything to match that read."""

# Shared instructions for the on-screen hook line, reused by both the
# combined (fast, single-call) generator and the standalone regenerate-only
# one. Emoji placement is spelled out explicitly because this is the part
# that was quietly getting dropped/misplaced before.
ON_SCREEN_RULES = """Write ONE short line of text to overlay on top of a video. It must be \
SIMPLE and plain: sound genuinely shocked, surprised or stunned at what is \
literally happening in the video itself (e.g. "Wait, did she really just say that?", \
"He actually walked up and did it", "The way he just took it"). Describe the \
real moment on screen -- who did what -- using plain everyday words. No jokes, \
no sarcasm, no politics, no clever wordplay, no opinions, no slogans. Do not \
mention things that are not in the video. Under 10 words, no hashtags, no \
quotation marks around the line.

End the line with exactly ONE emoji that fits (😳 😮 😯 🤯 😬 are typical) -- it \
must be the very last character. Do not put a full stop, period, or any other \
punctuation between the last word and the emoji. Do not use more than one emoji."""

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

POSTING_RULES = f"""Write the CORE caption that goes out with this video on Instagram, \
YouTube, X, TikTok, Threads and Facebook. It is deliberately SIMPLE: ONE single \
short line, nothing else (no paragraphs, no context, no jokes, no rant).

Pick ONE of two forms:
A) A question that makes people comment, using typical engagement questions that fit \
what is on screen, for example: "Do you agree with her?", "Do you agree with him?", \
"Is this valid?", "Have they got a point?", "What do you think about this?", \
"What would you do in this situation?", "How would you handle this?", \
"How would you react?" Make sure the pronoun / subject matches who is actually in the \
video (her / him / them / this guy / this woman). Vary your choice.
B) Only if no question fits the clip: ONE short statement about what is happening on \
screen, for example "This is crazy!", "Mad props to this guy", "He did well to stand \
up for himself like that". Plain and punchy, not a joke.

The line is followed by exactly ONE emoji, the very last character, with no \
punctuation between the line and the emoji besides the ? or ! that belongs to it. For \
questions use a comment-prompting emoji such as 👇 🤔 🤨 😅 👀 ; for statements use one \
that matches the feeling. Never more than one emoji.

Give exactly {POSTING_HASHTAG_COUNT} hashtags in the separate "hashtags" field (not inside \
"caption"). The caption line itself must be under 100 characters."""

COMBINED_SYSTEM = STANCE_GUIDE + "\n\n" + HASHTAG_STRATEGY + "\n\n" + READ_THE_CLIP + f"""

You have two things to write for the same video, in one response.

1) ON-SCREEN HOOK: {ON_SCREEN_RULES}

2) POSTING CAPTION:

{POSTING_RULES}

Respond ONLY with JSON: {{"angle_read": "...", "on_screen": "...", "caption": "...", "hashtags": ["#...", "#..."]}}"""

ON_SCREEN_SYSTEM = STANCE_GUIDE + "\n\n" + READ_THE_CLIP + f"\n\nYour job right now: {ON_SCREEN_RULES}\n\nRespond ONLY with JSON: {{\"angle_read\": \"...\", \"on_screen\": \"...\"}}"

POSTING_SYSTEM = "You write the simple core caption for the @wokevision_ page.\n\n" + STANCE_GUIDE + "\n\n" + HASHTAG_STRATEGY + "\n\n" + READ_THE_CLIP + f"\n\nYour job right now: {POSTING_RULES}\n\nRespond ONLY with JSON: {{\"angle_read\": \"...\", \"caption\": \"...\", \"hashtags\": [\"#...\"]}}"

# Fallback emoji, confirmed present in the local emoji_pack/ so the on-screen
# caption always renders a real image instead of silently dropping a
# character the pack doesn't have. Used only when the model's own line
# somehow comes back with no emoji at all.
FALLBACK_EMOJI = ["\U0001F633", "\U0001F62E", "\U0001F62F", "\U0001F92F", "\U0001F62C"]

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
    stripped = stripped.rstrip(" .,;:").strip()

    emoji = found[0] if found else random.choice(FALLBACK_EMOJI)
    return f"{stripped} {emoji}"



QUESTION_EMOJI = ["\U0001F447", "\U0001F914", "\U0001F928", "\U0001F605", "\U0001F440"]
STATEMENT_EMOJI = ["\U0001F633", "\U0001F92F", "\U0001F62E", "\U0001F4AF", "\U0001F440"]
FALLBACK_QUESTIONS = ["What do you think about this?", "Is this valid?", "Do you agree with this?",
                      "How would you react?", "What would you do in this situation?"]
FALLBACK_TOP = ["Wait, is this actually happening?", "Did that really just happen?", "I can't believe what I just watched"]


def _core_line(text: str, default: str = "") -> str:
    """Forces the core caption into its fixed shape: ONE line, ending in
    exactly ONE emoji (a question gets a comment-prompting one). Anything
    else the model wrote -- extra lines, hashtags, extra emoji, a full stop
    before the emoji -- is stripped."""
    text = _HASHTAG_RE.sub("", text or "")
    lines = [l.strip() for l in text.replace("\r", "").split("\n") if l.strip()]
    line = lines[0] if lines else (default or "")
    found = _EMOJI_RE.findall(line)
    body = re.sub(r"\s+", " ", _EMOJI_RE.sub("", line)).strip()
    body = body.strip('"\u201c\u201d ').rstrip(" .,;:-").strip()
    if not body:
        body = random.choice(FALLBACK_QUESTIONS)
    if re.match(r"(?i)^(do|does|did|is|are|was|were|have|has|had|would|could|should|can|will|what|how|who|why|where|which|am)\\b", body) and not body.endswith(("?", "!")):
        body += "?"
    is_q = body.endswith("?")
    emoji = found[0] if found else random.choice(QUESTION_EMOJI if is_q else STATEMENT_EMOJI)
    return f"{body} {emoji}"


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
    body = _core_line(body)
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


# Extra high-traffic tags used only to top up a set that is still short.
POOL_REACH = ["#viral", "#trending", "#memes", "#funny", "#news", "#politics"]

# Most hashtags each platform will actually honour (see the research notes
# further down). Threads takes ONE topic tag (set separately, not in the text).
TAG_MAX = {"instagram": 5, "tiktok": 5, "youtube": 5, "x": 2, "facebook": 3, "threads": 5}


def _theme_pool(probe: str) -> list:
    theme = []
    if _CULTURE_WORDS.search(probe):
        theme += POOL_CULTURE
    if _POLITICS_WORDS.search(probe):
        theme += POOL_POLITICS
    theme += POOL_HUMOUR
    return theme


def enforce_tag_mix(text: str, theme_text: str = "", total: int = 5, keep=None, exclude=None) -> str:
    """Rewrites the trailing hashtag set of `text` so it holds exactly `total`
    tags (when enough exist): any `keep` tags first (never dropped), the
    model's clip-specific tags, and popular pool tags themed to the clip,
    topped up from the reach pool. `exclude` tags are not added. Hashtags are
    moved to the end."""
    if not text:
        return text
    keep_l = []
    for t in (keep or []):
        t = "#" + str(t).strip().lstrip("#")
        if len(t) > 1 and t.lower() not in [k.lower() for k in keep_l]:
            keep_l.append(t)
    excl = {("#" + str(t).strip().lstrip("#")).lower() for t in (exclude or [])}
    found, seen = [], set()
    for t in _HASHTAG_RE.findall(text):
        if t.lower() not in seen:
            seen.add(t.lower()); found.append(t)
    body = re.sub(r"[ \t]{2,}", " ", _HASHTAG_RE.sub("", text)).strip()
    brand = "#WokeVision"
    held_l = {k.lower() for k in keep_l}
    tags = [t for t in found if t.lower() != brand.lower() and t.lower() not in held_l]
    probe = f"{theme_text} {body}"
    theme = _theme_pool(probe)
    pool_have = [t for t in tags if t.lower() in _POOL_ALL]
    specific = [t for t in tags if t.lower() not in _POOL_ALL]
    need = max(0, min(2, total // 2) - pool_have.__len__())
    taken = held_l | seen | excl
    adds = []
    for t in theme:
        if len(adds) >= need:
            break
        if t.lower() not in taken and t.lower() not in {a.lower() for a in adds}:
            adds.append(t)
    keep_brand = [t for t in found if t.lower() == brand.lower() and brand.lower() not in held_l]
    keep_specific = max(0, total - len(keep_l) - len(keep_brand) - len(pool_have) - len(adds))
    final = keep_l + keep_brand + specific[:keep_specific] + pool_have + adds
    final = final[:max(total, len(keep_l))]
    have = {t.lower() for t in final}
    for pool_try in (True, False):
        for t in theme + POOL_REACH:
            if len(final) >= total:
                break
            if t.lower() in have or (pool_try and t.lower() in excl):
                continue
            final.append(t); have.add(t.lower())
    return f"{body}\n\n{' '.join(final)}" if body else " ".join(final)


def tags_in(text: str) -> list:
    out, seen = [], set()
    for t in _HASHTAG_RE.findall(text or ""):
        if t.lower() not in seen:
            seen.add(t.lower()); out.append(t)
    return out


def fit_chars(text: str, limit: int) -> str:
    """Trims the body (never the trailing hashtag line) so text fits `limit`."""
    if len(text) <= limit:
        return text
    tags = tags_in(text)
    body = re.sub(r"[ \t]{2,}", " ", _HASHTAG_RE.sub("", text)).strip()
    tail = " ".join(tags)
    budget = limit - len(tail) - (2 if tail else 0)
    if budget < 20:
        return text[:limit]
    return f"{_trim_to_chars(body, budget)}\n\n{tail}" if tail else _trim_to_chars(body, limit)


def reroll_hashtags(transcript: str, meta: dict, text: str, theme_text: str, total: int, keep=None) -> str:
    """Returns `text` with every hashtag not in `keep` swapped for fresh ones
    (same count as before, never the ones just dropped). The words are left
    exactly as they are."""
    cur = tags_in(text)
    keep_l = [k for k in (keep or []) if k.lower() in {c.lower() for c in cur}]
    if keep_l and len({k.lower() for k in keep_l}) >= total:
        return text
    avoid = [c for c in cur if c.lower() not in {k.lower() for k in keep_l}]
    need = max(1, total - len(keep_l))
    body = re.sub(r"[ \t]{2,}", " ", _HASHTAG_RE.sub("", text)).strip()
    fresh = []
    if OPENAI_API_KEY:
        try:
            note = (f"Return JSON {{\"hashtags\": [...]}} with EXACTLY {need} NEW hashtags (each starting with #) for this post. "
                    f"Do NOT use any of: {' '.join(avoid + keep_l)}. Already fixed on the post: {' '.join(keep_l) or 'none'}. "
                    "Pick tags people really browse and that fit this clip: a mix of clip-specific tags and popular reach tags. "
                    "Never #fyp / #foryou. No spaces inside a tag.")
            data = _call_openai(HASHTAG_STRATEGY, _build_context(transcript, meta, note + f"\nPost text:\n{body}"), temperature=1.0)
            for t in (data.get("hashtags") or []):
                t = "#" + re.sub(r"\W", "", str(t))
                if len(t) > 1 and t.lower() not in {x.lower() for x in avoid + keep_l + fresh} and t.lower() not in ("#fyp", "#foryou"):
                    fresh.append(t)
        except Exception as e:
            print(f"HASHTAG REROLL AI FAILED: {e}", flush=True)
    theme = _theme_pool(f"{theme_text} {body}") + POOL_REACH
    random.shuffle(theme)
    blocked = {x.lower() for x in avoid + keep_l + fresh}
    for t in theme:
        if len(fresh) >= need:
            break
        if t.lower() not in blocked:
            fresh.append(t); blocked.add(t.lower())
    fresh = fresh[:need]
    joined = (body + "\n\n" if body else "") + " ".join(keep_l + fresh)
    return enforce_tag_mix(joined, theme_text, total, keep=keep_l, exclude=avoid)


# Topics that exist on Threads and that this post type can sensibly use.
# Threads has no endpoint that lists topics, so the choice is limited to this
# fixed set of established, high-traffic ones (an invented topic would be an
# empty feed, or be refused).
THREADS_TOPICS = ["Politics", "US Politics", "Current Events", "News", "Memes", "Funny", "Comedy", "Humor", "Satire",
                  "Culture", "Pop Culture", "Internet Culture", "Social Media", "Free Speech", "Media", "Society",
                  "Opinion", "Debate", "Elections", "Government", "Law", "Immigration", "Economy", "Gender",
                  "Celebrities", "Entertainment", "Music", "Movies", "TV", "Sports", "Tech", "AI", "Education",
                  "Religion", "Philosophy", "History", "Crime", "Military", "Health", "Viral"]
_TOPIC_LOWER = {t.lower(): t for t in THREADS_TOPICS}
_TOPIC_SYN = {"politic": "Politics", "trump": "Politics", "biden": "Politics", "election": "Elections", "vote": "Elections",
              "congress": "Government", "senate": "Government", "government": "Government", "police": "Law", "policing": "Law",
              "court": "Law", "border": "Immigration", "ice": "Immigration", "woke": "Culture", "culture": "Culture",
              "lgbt": "Culture", "trans": "Gender", "gender": "Gender", "feminis": "Gender", "meme": "Memes", "joke": "Funny",
              "humour": "Humor", "humor": "Humor", "comed": "Comedy", "satir": "Satire", "news": "News", "speech": "Free Speech",
              "censor": "Free Speech", "media": "Media", "war": "Military", "army": "Military", "military": "Military",
              "economy": "Economy", "tax": "Economy", "tech": "Tech", "ai": "AI", "music": "Music", "movie": "Movies",
              "film": "Movies", "tv": "TV", "sport": "Sports", "celeb": "Celebrities", "relig": "Religion", "church": "Religion",
              "school": "Education", "universit": "Education", "crime": "Crime", "health": "Health", "viral": "Viral"}


def _theme_topics(probe: str) -> list:
    out = []
    if _POLITICS_WORDS.search(probe):
        out += ["Politics", "US Politics", "Current Events", "News"]
    if _CULTURE_WORDS.search(probe):
        out += ["Culture", "Free Speech", "Society", "Opinion"]
    return out + ["Memes", "Funny", "Humor", "Comedy", "Satire", "Viral"]


def pick_topic_tag(tag: str, theme_text: str = "", exclude=None) -> str:
    """Always returns a Threads topic that is on the approved list: the given
    tag if it is one (any case), else the closest match, else the best fit for
    the clip. Never contains '.', '&', '#' and stays 1-50 chars."""
    excl = {str(x).strip().lower() for x in (exclude or [])}
    t = re.sub(r"[^A-Za-z0-9 '\-]", " ", str(tag or "").replace("&", " ")).strip().lower()
    t = re.sub(r"\s+", " ", t)
    if t in _TOPIC_LOWER and t not in excl:
        return _TOPIC_LOWER[t]
    if t:
        for key, topic in _TOPIC_SYN.items():
            if re.search(r"\b" + re.escape(key), t) and topic.lower() not in excl:
                return topic
    cands = [x for x in _theme_topics(theme_text or "") if x.lower() not in excl]
    return cands[0] if cands else "Memes"


def reroll_topic(current: str, theme_text: str = "") -> str:
    pool = [x for x in _theme_topics(theme_text) if x.lower() != str(current or "").lower()]
    return random.choice(pool[:6]) if pool else pick_topic_tag("", theme_text)


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
    if "credit_override" in meta:           # set by hand in the editor ("" = no credit)
        return _clean_handle(meta.get("credit_override"))
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
    brief = ((meta or {}).get("campaign_brief") or "").strip()
    if brief:
        parts.append(f"SPONSORED CAMPAIGN BRIEF (follow its key messages, required wording and do/don't rules; keep the house voice):\n{brief}")
    if uploader:
        parts.append(f"Source account/channel: {uploader}")
    if handle:
        parts.append(f"Credit handle: @{handle}")
    if title:
        parts.append(f"Original post title: {title}")
    if description:
        parts.append(f"Original post description: {description[:700]}")
    seen = ((meta or {}).get("video_read") or "").strip()
    if seen:
        parts.append("WHAT THE VIDEO SHOWS (from watching the actual footage -- reliable on visuals, expressions and on-screen text):\n" + seen[:2500])
    spoken = transcript[:3000] or "[none]"
    parts.append(f"Spoken transcript (may be empty or music-only for memes/animations):\n{spoken}")
    context = "\n\n".join(parts)
    if extra_note:
        context += f"\n\n{extra_note}"
    return context[:9000]


BRAND_NOTES = ""   # set from the Settings page (main.py refreshes it)


def _call_openai(system_prompt: str, user_content: str, temperature: float = 0.95, kind: str = "caption") -> dict:
    if BRAND_NOTES:
        system_prompt = system_prompt + "\n\nADDITIONAL OWNER INSTRUCTIONS (follow these; they override the defaults above):\n" + BRAND_NOTES
    try:
        ex = style_examples()
        if ex:
            system_prompt = system_prompt + "\n\n" + ex
    except Exception:
        pass
    import time as _t
    last = None
    for attempt in range(3):
        try:
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
                    "temperature": temperature,
                },
                timeout=60,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            body = resp.json()
            u = body.get("usage") or {}
            db.record_ai_usage(kind, os.environ.get("CAPTION_MODEL", "gpt-4o"), u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
            return json.loads(body["choices"][0]["message"]["content"])
        except Exception as e:
            last = e
            print(f"OPENAI CALL FAILED (attempt {attempt + 1}/3, {kind}): {e}", flush=True)
            if attempt < 2:
                _t.sleep(1.5 * (attempt + 1))
    raise last


def generate_captions(transcript: str, meta: dict) -> tuple:
    """Generates BOTH captions (on-screen line + core posting caption/hashtags)
    in a single OpenAI call. If the AI is unavailable it falls back to a plain
    generic question -- never the source post's own title. Returns
    (on_screen_caption, posting_caption)."""
    fallback_top = _enforce_single_trailing_emoji(random.choice(FALLBACK_TOP))
    fallback_posting = _assemble_posting_caption("", [])
    if not OPENAI_API_KEY:
        return fallback_top, fallback_posting
    try:
        data = _call_openai(COMBINED_SYSTEM, _build_context(transcript, meta))
        on_screen = _enforce_single_trailing_emoji((data.get("on_screen") or "").strip() or random.choice(FALLBACK_TOP))
        posting = _assemble_posting_caption(data.get("caption"), data.get("hashtags")) or fallback_posting
        return on_screen, posting
    except Exception as e:
        print(f"CAPTION GEN FAILED: {e}", flush=True)
        return fallback_top, fallback_posting


def generate_on_screen_caption(transcript: str, meta: dict, avoid: str = None, strict: bool = False) -> str:
    """The short line overlaid on the video itself, generated alone -- used
    by the "change video caption" regenerate button. With strict=True a
    failure raises (so the caller can keep the old caption and say so) instead
    of quietly substituting something else."""
    if not OPENAI_API_KEY:
        if strict:
            raise RuntimeError("No OPENAI_API_KEY is set, so captions can't be regenerated.")
        return _enforce_single_trailing_emoji(random.choice(FALLBACK_TOP))

    note = f'A previous version was: "{avoid}" -- write a genuinely different one, not a light rewording.' if avoid else ""
    try:
        data = _call_openai(ON_SCREEN_SYSTEM, _build_context(transcript, meta, note))
        line = (data.get("on_screen") or "").strip()
        if not line:
            raise ValueError("empty on-screen caption")
        return _enforce_single_trailing_emoji(line)
    except Exception as e:
        print(f"ON-SCREEN CAPTION GEN FAILED: {e}", flush=True)
        if strict:
            raise
        return _enforce_single_trailing_emoji(random.choice(FALLBACK_TOP))


HOOKS_SYSTEM = STANCE_GUIDE + "\n\n" + READ_THE_CLIP + f"""

Your job right now: write THREE different on-screen lines for this clip. All three follow these rules: {ON_SCREEN_RULES}
Make them differ in wording and in which moment of the clip they react to:
1. "reaction" - shocked at the main thing that happens.
2. "moment" - shocked at one specific detail or moment.
3. "disbelief" - disbelief that it is actually happening.

Respond ONLY with JSON: {{"hooks": [{{"style": "reaction", "text": "..."}}, {{"style": "moment", "text": "..."}}, {{"style": "disbelief", "text": "..."}}]}}"""


def generate_hook_options(transcript: str, meta: dict, current: str = "") -> list:
    """Three alternative on-screen hooks (question / claim / curiosity) so the
    editor can pick instead of re-rolling one at a time. [] on any failure."""
    if not OPENAI_API_KEY:
        return []
    note = f'The current hook is: "{current}" -- all three must be different from it.' if current else ""
    try:
        data = _call_openai(HOOKS_SYSTEM, _build_context(transcript, meta, note))
        out = []
        for h in (data.get("hooks") or [])[:3]:
            t = _enforce_single_trailing_emoji((h.get("text") or "").strip())
            if t:
                out.append({"style": str(h.get("style") or ""), "text": t})
        return out
    except Exception as e:
        print(f"HOOK OPTIONS FAILED: {e}", flush=True)
        return []


RISK_SYSTEM = """You are a cautious social-media legal/policy reviewer for a political meme page. \
Review the post copy and video transcript BEFORE publishing. Flag only real concerns, each with a severity ("high" or "medium" or "low"), \
a category (one of: defamation, unverified claim, hate speech, graphic content, harassment, misinformation, platform policy, copyright, other), \
a short quote of the problematic text, why it is a risk, and a concrete safer rewrite. Defamation concerns apply mainly to false factual \
accusations about private individuals; opinion and satire about public figures is normally fine. If nothing is concerning, return an empty list. \
Respond ONLY with JSON: {"summary": "one sentence", "flags": [{"severity": "...", "category": "...", "quote": "...", "why": "...", "fix": "..."}]}"""


def risk_check(transcript: str, texts: dict) -> dict:
    """Pre-publish scan of the copy + transcript. Raises on failure."""
    body = "VIDEO TRANSCRIPT:\n" + (transcript or "(none)")[:6000] + "\n\nPOST COPY:\n" + \
        "\n".join(f"[{k}] {v}" for k, v in (texts or {}).items() if v)
    data = _call_openai(RISK_SYSTEM, body, temperature=0.2, kind="risk")
    flags = []
    for f in (data.get("flags") or [])[:10]:
        flags.append({k: str(f.get(k) or "")[:400] for k in ("severity", "category", "quote", "why", "fix")})
    return {"summary": str(data.get("summary") or "")[:300], "flags": flags}


def translate_lines(lines: list, language: str) -> list:
    """Translate subtitle lines, keeping count and order. Raises on failure."""
    system = ("You translate subtitle lines into " + language + ". Keep each line short, natural and in the same order; "
              "keep names and hashtags. Respond ONLY with JSON: {\"lines\": [\"...\"]} with exactly the same number of lines as the input.")
    data = _call_openai(system, json.dumps({"lines": lines}), temperature=0.2, kind="translate")
    out = [str(x) for x in (data.get("lines") or [])]
    if len(out) != len(lines):
        raise ValueError("translation line count mismatch")
    return out


def generate_posting_caption(transcript: str, meta: dict, on_screen_caption: str = "", avoid: str = None, strict: bool = False) -> str:
    """The core caption for the actual social posts (one short line + one
    emoji, then hashtags). With strict=True a failure raises so the caller can
    keep the existing caption and tell the user; otherwise it falls back to a
    generic question (never the source post's title)."""
    if not OPENAI_API_KEY:
        if strict:
            raise RuntimeError("No OPENAI_API_KEY is set, so captions can't be regenerated.")
        return _assemble_posting_caption("", [])

    note = ""
    if on_screen_caption:
        note += f'The on-screen line on the video is: "{on_screen_caption}". The core caption is separate: a question or one-line statement, do not repeat it.'
    if avoid:
        first = (avoid.strip().splitlines() or [""])[0]
        note += f' The previous core caption was: "{first}" -- choose a DIFFERENT question or statement.'

    try:
        data = _call_openai(POSTING_SYSTEM, _build_context(transcript, meta, note))
        cap = (data.get("caption") or "").strip()
        if not cap:
            raise ValueError("empty caption")
        return _assemble_posting_caption(cap, data.get("hashtags"))
    except Exception as e:
        print(f"POSTING CAPTION GEN FAILED: {e}", flush=True)
        if strict:
            raise
        return _assemble_posting_caption("", [])


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
    "x_text": 280, "x_tags": 2,
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


_EMOJI_ONLY_LINE = re.compile(r"(?m)^[ \t]*((?:" + _EMOJI_RE.pattern + r"[ \t]*)+)$")


def fix_emoji_runts(text: str) -> str:
    """An emoji sitting alone on its own line looks like a typo: pull it up onto the end of the line above."""
    if not text:
        return text
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"([^\s])[ \t]*\n[ \t]*((?:" + _EMOJI_RE.pattern + r")(?:[ \t]*(?:" + _EMOJI_RE.pattern + r"))*)[ \t]*(?=\n|$)", r"\1 \2", text)
    return text



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


def _clean_topic_tag(tag: str, theme: str = "") -> str:
    return pick_topic_tag(tag, theme)[:LIM["threads_tag"]]


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
    out["instagram"] = {"caption": _trim_to_chars(fix_emoji_runts(_limit_hashtags(ig, LIM["ig_tags"]).strip()), LIM["ig_caption"])}
    out["instagram"]["story"] = bool((raw.get("instagram") or {}).get("story")) if isinstance(raw.get("instagram"), dict) else False
    _ib = (raw.get("instagram") or {}).get("story_black") if isinstance(raw.get("instagram"), dict) else None
    out["instagram"]["story_black"] = True if _ib is None else bool(_ib)
    _fc = g("instagram", "first_comment")
    if _fc and str(_fc).strip():
        out["instagram"]["first_comment"] = str(_fc).strip()[:2200]

    th = g("threads", "text")
    th = base["threads"]["text"] if th is None else str(th)
    tt = g("threads", "topic_tag")
    tt = base["threads"]["topic_tag"] if tt is None else str(tt)
    out["threads"] = {"text": _trim_to_chars(fix_emoji_runts(_limit_hashtags(th, 5).strip()), LIM["threads_text"]), "topic_tag": _clean_topic_tag(tt, master)}

    yt_title = g("youtube", "title")
    yt_title = base["youtube"]["title"] if yt_title is None else str(yt_title)
    yt_desc = g("youtube", "description")
    yt_desc = base["youtube"]["description"] if yt_desc is None else str(yt_desc)
    yt_tags = g("youtube", "tags")
    yt_tags = base["youtube"]["tags"] if yt_tags is None else yt_tags
    cat = str(g("youtube", "category") or YT_DEFAULT_CATEGORY)
    out["youtube"] = {
        "title": _trim_to_chars(_strip_hashtags(yt_title).strip(), LIM["yt_title"]),
        "description": _trim_to_chars(fix_emoji_runts(_limit_hashtags(yt_desc, LIM["yt_hashtags"]).strip()), LIM["yt_desc"]),
        "tags": _clean_yt_tags(yt_tags),
        "category": cat if cat.isdigit() else YT_DEFAULT_CATEGORY,
    }

    xt = g("x", "text")
    xt = base["x"]["text"] if xt is None else str(xt)
    out["x"] = {"text": _trim_to_chars(fix_emoji_runts(_limit_hashtags(xt, LIM["x_tags"]).strip()), LIM["x_text"])}

    tk = g("tiktok", "caption")
    tk = base["tiktok"]["caption"] if tk is None else str(tk)
    tkd = raw.get("tiktok") if isinstance(raw.get("tiktok"), dict) else {}
    # TikTok's posting rules: interactions start OFF, privacy has no default, and the
    # commercial-content disclosure starts OFF -- the user opts in to each.
    flag = lambda k: bool(tkd.get(k)) if k in tkd else False
    _priv = str(tkd.get("privacy_level") or "")
    out["tiktok"] = {
        "caption": _trim_to_chars(fix_emoji_runts(_limit_hashtags(tk, LIM["tiktok_tags"]).strip()), LIM["tiktok_caption"]),
        "allow_comments": flag("allow_comments"), "allow_duet": flag("allow_duet"), "allow_stitch": flag("allow_stitch"),
        "privacy_level": _priv if _priv in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY") else "",
        "disclose": flag("disclose"), "your_brand": flag("your_brand"), "branded_content": flag("branded_content"),
    }

    fb = g("facebook", "description")
    fb = base["facebook"]["description"] if fb is None else str(fb)
    out["facebook"] = {"description": _trim_to_chars(fix_emoji_runts(_limit_hashtags(fb, LIM["fb_tags"]).strip()), LIM["fb_desc"])}
    out["facebook"]["story"] = bool((raw.get("facebook") or {}).get("story")) if isinstance(raw.get("facebook"), dict) else False
    _fbb = (raw.get("facebook") or {}).get("story_black") if isinstance(raw.get("facebook"), dict) else None
    out["facebook"]["story_black"] = True if _fbb is None else bool(_fbb)
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
        "threads": {"text": (body + ("\n\n" + tags5 if tags5 else "")).strip(), "topic_tag": pick_topic_tag(tag_words[0] if tag_words else "", master)},
        "youtube": {
            "title": first_line[:100],
            "description": (body + "\n\n" + " ".join((tags[:4] + ["#Shorts"])[:5])).strip(),
            "tags": tag_words, "category": YT_DEFAULT_CATEGORY,
        },
        "x": {"text": fit_chars((body + ("\n\n" + " ".join(tags[:2]) if tags else "")).strip(), LIM["x_text"])},
        "tiktok": {"caption": (body + ("\n\n" + tags5 if tags5 else "")).strip(),
                   "allow_comments": False, "allow_duet": False, "allow_stitch": False, "privacy_level": "",
                   "disclose": False, "your_brand": False, "branded_content": False},
        "facebook": {"description": (body + ("\n\n" + " ".join(tags[:2]) if tags else "")).strip()},
    }


PLATFORM_SYSTEM = BRAND_VOICE + "\n\n" + STANCE_GUIDE + "\n\n" + HASHTAG_STRATEGY + """

You are adapting ONE core caption into six platform versions of the same \
post. The core caption is deliberately SIMPLE -- ONE short line (a question such \
as "Do you agree with her?" or a one-line statement) followed by ONE emoji. \
Keep every version equally simple: the visible text of each post is that one \
line + one emoji (you may reword the question slightly per platform, but it \
stays one short question or statement with exactly one trailing emoji). NO \
paragraphs, NO extra context sentences, NO jokes, NO essays. Then add only what \
the platform needs: the credit line (where listed below) and the hashtags. \
YouTube is the only exception: its title is a short keyword-first headline and \
its description is the same one line, the credit line and hashtags.

PLATFORM RULES (hard limits in brackets):

instagram -- "caption" [max 2,200 chars]. The one core line + one emoji, then \
the credit line if one is supplied, then the hashtags on their own line at the \
very end -- exactly 5: 2-3 clip-specific tags and 2-3 popular pool tags \
(#WokeVision is optional, not required).

threads -- "text" [max 500 chars] and "topic_tag" [1-50 chars, no "." or "&", \
no leading #]. The one core line + one emoji (a question works best on Threads), \
then EXACTLY 5 hashtags on their own line. "topic_tag" is separate from those and \
must be ONE of these exact approved topics, whichever fits the clip best: {THREADS_TOPIC_LIST}.

youtube -- "title" [max 100 chars, no hashtags]: a short plain headline of what \
happens, keyword first (only ~50 chars show), no clickbait lies, no ALL CAPS. \
"description" [max 5,000]: the one core line + one emoji, then the credit line if \
supplied, then 3-5 hashtags on their own line (include #Shorts as one of them). \
"tags": 8-12 search keywords/phrases (no #, total under 400 characters).

x -- "text" [max 280 chars INCLUDING the hashtags]. The one core line + one emoji, \
then exactly 2 hashtags (one specific to the clip, one popular reach tag).

tiktok -- "caption" [max 2,200 chars]. The one core line + one emoji, then the credit \
line if supplied, then exactly 5 hashtags (same mix as Instagram), no #fyp/#foryou.

facebook -- "description" [max 2,200 chars]. The one core line + one emoji, then the \
credit line if supplied, then exactly 3 hashtags (clip-specific plus a popular reach tag).

If the context gives a "Credit handle", the instagram, tiktok, facebook \
and youtube versions each include one line "Credit: @handle" before the \
hashtags (you may leave it out of threads/x).

Never attack people over protected traits.

Respond ONLY with JSON in exactly this shape:
{"instagram": {"caption": "..."},
 "threads": {"text": "...", "topic_tag": "..."},
 "youtube": {"title": "...", "description": "...", "tags": ["...", "..."]},
 "x": {"text": "..."},
 "tiktok": {"caption": "..."},
 "facebook": {"description": "..."}}"""
PLATFORM_SYSTEM = PLATFORM_SYSTEM.replace("{THREADS_TOPIC_LIST}", ", ".join(THREADS_TOPICS))


def generate_platform_posts(transcript: str, meta: dict, on_screen_caption: str = "",
                            master_caption: str = "", only: str = None, current: dict = None, keep=None) -> dict:
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
    theme_all = f"{(meta or {}).get('title','')} {master_caption}"
    keep_all = [str(k) for k in (keep or [])]
    for pid, key in (("instagram", "caption"), ("tiktok", "caption"), ("facebook", "description"), ("x", "text"), ("threads", "text")):
        if isinstance(generated.get(pid), dict) and generated[pid].get(key):
            txt = enforce_tag_mix(generated[pid][key], theme_all, TAG_MAX[pid], keep=keep_all if pid == only else None)
            generated[pid][key] = fit_chars(txt, LIM["x_text"]) if pid == "x" else txt
    yt = generated.get("youtube")
    if isinstance(yt, dict) and yt.get("description"):
        hold = ["#Shorts"] + ([k for k in keep_all if k.lower() != "#shorts"] if only == "youtube" else [])
        yt["description"] = enforce_tag_mix(yt["description"], theme_all, TAG_MAX["youtube"], keep=hold)
    if isinstance(generated.get("threads"), dict):
        generated["threads"]["topic_tag"] = pick_topic_tag(generated["threads"].get("topic_tag"), theme_all)
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


def set_credit_in_posts(posts: dict, handle: str) -> dict:
    """Rewrites the 'Credit: @x' line in every credited platform's text:
    removes any existing one and adds the new handle (or none if blank)."""
    out = {}
    for pid, post in (posts or {}).items():
        post = dict(post) if isinstance(post, dict) else post
        key = _DISCLOSE_FIELDS.get(pid)
        if isinstance(post, dict) and key and pid in ("instagram", "tiktok", "facebook", "youtube") and post.get(key):
            text = re.sub(r"(?im)^\s*credit\s*:\s*@\S+\s*$\n?", "", post[key]).rstrip()
            post[key] = _ensure_credit(text, handle) if handle else text
        out[pid] = post
    return out
