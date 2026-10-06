import os
import re
import uuid
import unicodedata
import subprocess
import requests
from PIL import Image, ImageDraw, ImageFont

from emoji_names import CODEPOINT_TO_NAME

CANVAS_W = 720
CANVAS_H = 1280

FONT_DIR = "/app/fonts"
ASSET_DIR = "/app/assets"
TMP_DIR = "/tmp/downloads"
EMOJI_PACK_DIR = "/app/emoji_pack"
EMOJI_CACHE_DIR = "/tmp/emoji_cache"
TWEMOJI_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/{cp}.png"

def _resolve_caption_font():
    """Use the user-supplied font if it was uploaded; otherwise fall back to
    a bundled open-license font so the service still works without it."""
    own_font = os.path.join(FONT_DIR, "SFProText.ttf")
    if os.path.exists(own_font):
        return own_font
    fallback = os.path.join(FONT_DIR, "Fallback.ttf")
    if os.path.exists(fallback):
        return fallback
    return own_font  # will raise a clear error at render time if neither exists


CAPTION_FONT_PATH = _resolve_caption_font()
CAPTION_COLOR = (0, 0, 0, 255)

VIDEO_W = round(0.82 * CANVAS_W)
VIDEO_H = round(VIDEO_W * 4 / 3)
VIDEO_X = round((CANVAS_W - VIDEO_W) / 2)
VIDEO_Y = 260

CAPTION_GAP = 24
CAPTION_SIDE_PADDING = 20
CAPTION_MAX_LINES = 2
CAPTION_MAX_FONT = 50
CAPTION_MIN_FONT = 24

LOGO_WIDTH_RATIO = 0.16
LOGO_MARGIN = 16
LOGO_OPACITY = 0.75

WATERMARK_WIDTH_RATIO = 0.85
WATERMARK_OPACITY = 0.5

HASHTAG_PATTERN = re.compile(r"#\w+")
QUOTE_STRIP_CHARS = "\"'\u2018\u2019\u201C\u201D"

EMOJI_PATTERN = re.compile(
    "("
    "(?:[\U0001F1E6-\U0001F1FF]{2})"
    "|(?:[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF\U00002B00-\U00002BFF]"
    "[\U0000FE0F\U0000200D\U0001F3FB-\U0001F3FF]*)+"
    ")"
)


def strip_hashtags(text: str) -> str:
    text = HASHTAG_PATTERN.sub("", text)
    text = text.strip().strip(QUOTE_STRIP_CHARS).strip()
    return re.sub(r"\s+", " ", text)


def codepoints_for(emoji: str) -> str:
    cps = [f"{ord(ch):x}" for ch in emoji if ch != "\uFE0F"]
    return "-".join(cps)


def unicode_name_slug(emoji: str):
    """For a simple single-codepoint emoji, return its official Unicode name
    as a lowercase_underscore slug (e.g. 'crystal_ball'). Returns None for
    multi-codepoint sequences (skin tones, flags, ZWJ combos) or unnamed chars."""
    base = emoji.replace("\uFE0F", "")
    if len(base) != 1:
        return None
    try:
        name = unicodedata.name(base)
    except ValueError:
        return None
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def get_emoji_image(emoji: str):
    # Strip the invisible "emoji-style" variation selector before name
    # lookups -- GPT-generated text almost always includes it (e.g. the
    # warning sign becomes U+26A0 + U+FE0F), but our name tables are keyed
    # by the plain base character.
    base_emoji = emoji.replace("\uFE0F", "")

    # 1. Check the user's own local emoji pack first, using the standard
    #    "gemoji" shortcode name (e.g. "sweat_smile") -- this is the naming
    #    convention their files actually use.
    gemoji_name = CODEPOINT_TO_NAME.get(base_emoji)
    if gemoji_name:
        local_path = os.path.join(EMOJI_PACK_DIR, f"{gemoji_name}.png")
        if os.path.exists(local_path):
            print(f"EMOJI: {emoji!r} -> local pack ({gemoji_name}.png)", flush=True)
            return local_path

    # 2. Some packs instead use the official Unicode character name -- try
    #    that naming style too before giving up on the local pack.
    slug = unicode_name_slug(emoji)
    if slug:
        local_path = os.path.join(EMOJI_PACK_DIR, f"{slug}.png")
        if os.path.exists(local_path):
            print(f"EMOJI: {emoji!r} -> local pack ({slug}.png)", flush=True)
            return local_path

    # 3. Fall back to Twemoji (fetched once, then cached) for anything not
    #    covered by the local pack.
    cp = codepoints_for(emoji)
    if not cp:
        print(f"EMOJI: {emoji!r} -> no codepoints resolved, skipping", flush=True)
        return None
    os.makedirs(EMOJI_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(EMOJI_CACHE_DIR, f"{cp}.png")
    if os.path.exists(cache_path):
        print(f"EMOJI: {emoji!r} -> Twemoji cache ({cp}.png)", flush=True)
        return cache_path
    url = TWEMOJI_CDN.format(cp=cp)
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200 and resp.content:
            with open(cache_path, "wb") as f:
                f.write(resp.content)
            print(f"EMOJI: {emoji!r} -> fetched from Twemoji ({cp}.png)", flush=True)
            return cache_path
        else:
            print(f"EMOJI: {emoji!r} -> Twemoji returned status {resp.status_code} for {cp}.png", flush=True)
    except Exception as e:
        print(f"EMOJI: {emoji!r} -> Twemoji fetch failed: {e}", flush=True)
    print(f"EMOJI: {emoji!r} -> NOT FOUND anywhere, will render as blank gap", flush=True)
    return None


def get_scaled_height(image_path, target_w):
    with Image.open(image_path) as img:
        w, h = img.size
    return round(target_w * h / w)


def token_width(token, font, font_size):
    if EMOJI_PATTERN.fullmatch(token):
        return font_size
    return font.getlength(token)


def measure_and_wrap_tokens(tokens, font_path, max_width_px, max_font, min_font, max_lines):
    def wrap_with(font, font_size):
        space_w = font.getlength(" ")
        lines, current, current_w = [], [], 0
        for tok in tokens:
            w = token_width(tok, font, font_size)
            add_w = w + (space_w if current else 0)
            if current and current_w + add_w > max_width_px:
                lines.append(current)
                current, current_w = [tok], w
            else:
                current.append(tok)
                current_w += add_w
        if current:
            lines.append(current)
        return lines

    for font_size in range(max_font, min_font - 1, -2):
        font = ImageFont.truetype(font_path, font_size)
        lines = wrap_with(font, font_size)
        if len(lines) <= max_lines:
            ascent, descent = font.getmetrics()
            line_height = int((ascent + descent) * 1.25)
            return font_size, lines, line_height, font

    font = ImageFont.truetype(font_path, min_font)
    lines = wrap_with(font, min_font)[:max_lines]
    ascent, descent = font.getmetrics()
    line_height = int((ascent + descent) * 1.25)
    return min_font, lines, line_height, font


def build_caption_image(caption_text):
    caption_text = strip_hashtags(caption_text)
    spaced = EMOJI_PATTERN.sub(lambda m: f" {m.group(0)} ", caption_text)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    tokens = [t for t in spaced.split(" ") if t]

    max_width_px = VIDEO_W - 2 * CAPTION_SIDE_PADDING
    font_size, lines_tokens, line_height, font = measure_and_wrap_tokens(
        tokens, CAPTION_FONT_PATH, max_width_px,
        CAPTION_MAX_FONT, CAPTION_MIN_FONT, CAPTION_MAX_LINES,
    )
    space_w = font.getlength(" ")
    ascent, _descent = font.getmetrics()
    canvas_h = line_height * len(lines_tokens)

    canvas = Image.new("RGBA", (VIDEO_W, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    for line_idx, line_tokens in enumerate(lines_tokens):
        widths = [token_width(t, font, font_size) for t in line_tokens]
        gap_total = space_w * (len(line_tokens) - 1) if len(line_tokens) > 1 else 0
        total_w = sum(widths) + gap_total
        cur_x = (VIDEO_W - total_w) / 2
        baseline_y = line_idx * line_height + ascent

        for tok, w in zip(line_tokens, widths):
            if EMOJI_PATTERN.fullmatch(tok):
                img_path = get_emoji_image(tok)
                if img_path:
                    try:
                        with Image.open(img_path) as em:
                            em = em.convert("RGBA").resize((font_size, font_size), Image.LANCZOS)
                            emoji_y = int(baseline_y - font_size * 0.85)
                            canvas.paste(em, (round(cur_x), emoji_y), em)
                    except Exception:
                        pass
            else:
                draw.text((cur_x, baseline_y), tok, font=font, fill=CAPTION_COLOR, anchor="ls")
            cur_x += w + space_w

    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, f"caption_{uuid.uuid4()}.png")
    canvas.save(path)
    return path, VIDEO_W, canvas_h


def _probe_duration(path: str):
    """Source video duration in seconds, used to turn ffmpeg's raw
    out_time_ms progress updates into a fraction complete. Returns None if
    ffprobe can't determine it (progress then just can't be computed, same
    as not passing progress_cb at all)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _run_ffmpeg(cmd, progress_cb, duration):
    """Shared ffmpeg runner for both render_staged() and apply_caption() --
    streams -progress pipe:1's clean "key=value" lines (rather than the
    human-readable stats line meant for a terminal) into progress_cb, and
    raises with the tail of stderr on a non-zero exit."""
    print("FFMPEG COMMAND:", " ".join(cmd), flush=True)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    log_lines = []
    for line in proc.stdout:
        line = line.strip()
        log_lines.append(line)
        if progress_cb and duration and "=" in line:
            key, _, value = line.partition("=")
            if key == "out_time_ms":
                try:
                    progress_cb(min(int(value) / 1_000_000 / duration, 1.0))
                except ValueError:
                    pass
            elif key == "progress" and value == "end":
                progress_cb(1.0)
    proc.wait()
    stderr_text = "\n".join(log_lines)
    print("FFMPEG STDERR:", stderr_text, flush=True)
    if proc.returncode != 0:
        raise RuntimeError(stderr_text[-4000:])


def _build_stage_graph(source_path: str):
    """Builds the ffmpeg inputs/filters that composite the source video onto
    the canvas with the logo + watermark -- everything EXCEPT the on-screen
    caption. Shared by render_staged() (which caches this as its own file)
    and the historical single-pass path, so the crop/scale/logo/watermark
    work only has to happen once per source video, not once per caption
    edit. Returns (inputs, filters, last_label)."""
    logo_path = os.path.join(ASSET_DIR, "logo.png")
    watermark_path = os.path.join(ASSET_DIR, "watermark.png")
    have_logo = os.path.exists(logo_path)
    have_watermark = os.path.exists(watermark_path)

    inputs = ["-i", source_path]
    filters = [
        f"color=white:s={CANVAS_W}x{CANVAS_H}[bg]",
        f"[0:v]crop=min(iw\\,ih*3/4):min(ih\\,iw*4/3):(iw-out_w)/2:(ih-out_h)/2,scale={VIDEO_W}:{VIDEO_H}[vid]",
        f"[bg][vid]overlay={VIDEO_X}:{VIDEO_Y}[stage]",
    ]
    last_label = "stage"
    next_input_index = 1

    if have_logo:
        logo_w = round(VIDEO_W * LOGO_WIDTH_RATIO)
        logo_h = get_scaled_height(logo_path, logo_w)
        logo_x = VIDEO_X + VIDEO_W - logo_w - LOGO_MARGIN
        logo_y = VIDEO_Y + LOGO_MARGIN
        inputs += ["-i", logo_path]
        filters.append(
            f"[{next_input_index}:v]scale={logo_w}:{logo_h},format=rgba,"
            f"colorchannelmixer=aa={LOGO_OPACITY}[logo]"
        )
        filters.append(f"[{last_label}][logo]overlay={logo_x}:{logo_y}[stage_logo]")
        last_label = "stage_logo"
        next_input_index += 1

    if have_watermark:
        wm_w = round(VIDEO_W * WATERMARK_WIDTH_RATIO)
        wm_h = get_scaled_height(watermark_path, wm_w)
        wm_x = VIDEO_X + round((VIDEO_W - wm_w) / 2)
        wm_y = VIDEO_Y + round((VIDEO_H - wm_h) / 2)
        inputs += ["-i", watermark_path]
        filters.append(
            f"[{next_input_index}:v]scale={wm_w}:{wm_h},format=rgba,"
            f"colorchannelmixer=aa={WATERMARK_OPACITY}[wm]"
        )
        filters.append(f"[{last_label}][wm]overlay={wm_x}:{wm_y}[stage_wm]")
        last_label = "stage_wm"
        next_input_index += 1

    return inputs, filters, last_label


def render_staged(source_path: str, output_path: str, progress_cb=None):
    """Composites the source video onto the canvas (crop/scale + logo +
    watermark) WITHOUT the on-screen caption, and encodes it to output_path.
    This is cached on disk by the caller (main.py keeps it alongside the
    source/final files, same lifetime) so a later caption-only change can
    call apply_caption() against this instead of redoing the crop/scale/
    logo/watermark work and re-decoding the original source every time."""
    inputs, filters, last_label = _build_stage_graph(source_path)
    filter_complex = ";".join(filters)
    duration = _probe_duration(source_path) if progress_cb else None

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{last_label}]",
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-shortest",
        "-nostats", "-progress", "pipe:1",
        output_path,
    ]
    _run_ffmpeg(cmd, progress_cb, duration)


WM_POSITIONS = ("left", "middle", "right")
WM_WIDTH_RATIO = 0.42   # campaign watermark width vs the video width
WM_OVERLAP = 0.40       # fraction of the watermark that hangs inside the video
SPEECH_BASE_LIFT = 70   # px above the video bottom edge for speech captions


def _watermark_geometry(wm_path, pos):
    wm_w = round(VIDEO_W * WM_WIDTH_RATIO)
    wm_h = get_scaled_height(wm_path, wm_w)
    if wm_h > 230:  # very tall logos: cap height, keep aspect
        wm_w = round(wm_w * 230 / wm_h)
        wm_h = 230
    if pos == "left":
        x = VIDEO_X
    elif pos == "right":
        x = VIDEO_X + VIDEO_W - wm_w
    else:
        x = VIDEO_X + round((VIDEO_W - wm_w) / 2)
    y = VIDEO_Y + VIDEO_H - round(WM_OVERLAP * wm_h)
    return wm_w, wm_h, x, y


def apply_caption(staged_path: str, caption_text: str, output_path: str, progress_cb=None,
                  cues=None, watermark=None, cue_style="classic"):
    """The fast path: overlays the on-screen caption, the optional campaign
    watermark ({"path","pos"}) and the optional burned-in speech captions
    (cues: [{start,end,text}]) onto an already-staged video and re-encodes.
    None of these need the original source re-decoded or re-cropped."""
    caption_img_path, cap_w, cap_h = build_caption_image(caption_text)
    caption_x = VIDEO_X
    caption_y = VIDEO_Y - CAPTION_GAP - cap_h

    inputs = ["-i", staged_path, "-i", caption_img_path]
    chain = [f"[1:v]format=rgba[capimg]", f"[0:v][capimg]overlay={caption_x}:{caption_y}[v1]"]
    last = "v1"
    lift = SPEECH_BASE_LIFT
    ass_path = None

    if watermark and watermark.get("path") and os.path.exists(watermark["path"]):
        pos = watermark.get("pos") if watermark.get("pos") in WM_POSITIONS else "right"
        wm_w, wm_h, wm_x, wm_y = _watermark_geometry(watermark["path"], pos)
        inputs += ["-i", watermark["path"]]
        chain.append(f"[2:v]scale={wm_w}:{wm_h},format=rgba[cwm]")
        chain.append(f"[{last}][cwm]overlay={wm_x}:{wm_y}[v2]")
        last = "v2"
        lift = max(lift, round(WM_OVERLAP * wm_h) + 22)

    if cues:
        from speech import build_ass
        ass_path = f"{output_path}.cues.ass"
        margin_v = (CANVAS_H - (VIDEO_Y + VIDEO_H)) + lift
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(build_ass(cues, CANVAS_W, CANVAS_H, margin_v, style=cue_style))
        esc_ass = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        esc_fonts = FONT_DIR.replace(":", "\\:")
        chain.append(f"[{last}]subtitles='{esc_ass}':fontsdir='{esc_fonts}'[v3]")
        last = "v3"

    filter_complex = ";".join(chain)
    duration = _probe_duration(staged_path) if progress_cb else None

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{last}]",
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-shortest",
        "-nostats", "-progress", "pipe:1",
        output_path,
    ]
    try:
        _run_ffmpeg(cmd, progress_cb, duration)
    finally:
        for pth in (caption_img_path, ass_path):
            if pth and os.path.exists(pth):
                os.remove(pth)


def render_video(source_path: str, caption_text: str, output_path: str, progress_cb=None):
    """Back-compat convenience wrapper: stages (crop/scale/logo/watermark)
    and applies the caption in one call, without keeping the intermediate
    staged file around. Prefer calling render_staged() + apply_caption()
    directly (as main.py does) when the staged file should be cached for a
    later fast caption-only re-render."""
    staged_path = f"{output_path}.staged.mp4"
    half = (lambda frac: progress_cb(frac * 0.5)) if progress_cb else None
    other_half = (lambda frac: progress_cb(0.5 + frac * 0.5)) if progress_cb else None
    try:
        render_staged(source_path, staged_path, progress_cb=half)
        apply_caption(staged_path, caption_text, output_path, progress_cb=other_half)
    finally:
        if os.path.exists(staged_path):
            os.remove(staged_path)
