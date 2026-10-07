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


def detect_subject(source_path: str):
    """Finds where the people are in a video so the 3:4 crop can follow them
    instead of cutting dead centre. Returns {"cx", "cy"} (0-1, fraction of the
    source frame) or None to keep the centre crop. Never raises."""
    try:
        import cv2
        import glob
        import tempfile
        import numpy as np
        dur = _probe_duration(source_path) or 0
        if dur <= 0:
            return None
        # Only worth doing when the 3:4 crop actually removes something.
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0:s=x", source_path], capture_output=True, text=True, timeout=30)
        w, h = [int(x) for x in r.stdout.strip().split("x")[:2]]
        if abs((w / h) - 0.75) < 0.03:
            return None
        cascade_dir = getattr(getattr(cv2, "data", None), "haarcascades", "") or ""
        cascades = []
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml"):
            c = cv2.CascadeClassifier(os.path.join(cascade_dir, name))
            if not c.empty():
                cascades.append(c)
        if not cascades:
            print("SMART CROP: no face model available, keeping centre crop", flush=True)
            return None
        n = 24
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", source_path, "-vf",
                            f"fps={n / max(dur, 1):.4f},scale=480:-2", "-frames:v", str(n), os.path.join(td, "f%02d.jpg")],
                           capture_output=True, timeout=240)
            hits, frames = [], 0
            for fp in sorted(glob.glob(os.path.join(td, "f*.jpg"))):
                img = cv2.imread(fp)
                if img is None:
                    continue
                frames += 1
                g = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
                ih, iw = g.shape
                best = None
                for c in cascades:
                    for (x, y, fw, fh) in c.detectMultiScale(g, scaleFactor=1.1, minNeighbors=5, minSize=(int(iw * 0.06), int(iw * 0.06))):
                        if best is None or fw * fh > best[2] * best[3]:
                            best = (x, y, fw, fh)
                    if best:
                        break
                if best:
                    x, y, fw, fh = best
                    hits.append(((x + fw / 2) / iw, (y + fh / 2) / ih, fw * fh))
        if frames == 0 or len(hits) < max(3, 0.25 * frames):
            return None
        def wmedian(vals):
            vals = sorted(vals)
            tot = sum(v[1] for v in vals); acc = 0
            for v, wt in vals:
                acc += wt
                if acc >= tot / 2:
                    return v
        cx = wmedian([(a, w_) for a, _, w_ in hits]); cy = wmedian([(b, w_) for _, b, w_ in hits])
        return {"cx": round(float(cx), 3), "cy": round(float(cy), 3), "faces": len(hits), "frames": frames}
    except Exception as e:
        print(f"SMART CROP FAILED (keeping centre crop): {e}", flush=True)
        return None


def _crop_filter(crop):
    """ffmpeg crop to 3:4, centred unless `crop` carries a subject position."""
    if not crop or crop.get("cx") is None:
        return "crop=min(iw\\,ih*3/4):min(ih\\,iw*4/3):(iw-out_w)/2:(ih-out_h)/2"
    cx, cy = float(crop["cx"]), float(crop["cy"])
    # Horizontally centre on the subject; vertically keep the face about 38% down the frame.
    return ("crop=min(iw\\,ih*3/4):min(ih\\,iw*4/3):"
            f"max(0\\,min(iw-out_w\\,{cx:.3f}*iw-out_w/2)):"
            f"max(0\\,min(ih-out_h\\,{cy:.3f}*ih-0.38*out_h))")


def _video_filters(crop):
    """Filters that produce the [vid] label: either a 3:4 crop (optionally following a position),
    or a 'fit' where the whole picture is shown over a blurred, zoomed copy of itself."""
    if crop and crop.get("mode") == "fit":
        return [
            "[0:v]split[fa][fb]",
            f"[fa]crop=min(iw\\,ih*3/4):min(ih\\,iw*4/3),scale={VIDEO_W}:{VIDEO_H},boxblur=28:6,eq=brightness=-0.08[fbg]",
            f"[fb]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease[ffg]",
            "[fbg][ffg]overlay=(W-w)/2:(H-h)/2[vid]",
        ]
    return [f"[0:v]{_crop_filter(crop)},scale={VIDEO_W}:{VIDEO_H}[vid]"]


def _build_stage_graph(source_path: str, crop=None):
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
        *_video_filters(crop),
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


def render_staged(source_path: str, output_path: str, progress_cb=None, crop=None):
    """Composites the source video onto the canvas (crop/scale + logo +
    watermark) WITHOUT the on-screen caption, and encodes it to output_path.
    This is cached on disk by the caller (main.py keeps it alongside the
    source/final files, same lifetime) so a later caption-only change can
    call apply_caption() against this instead of redoing the crop/scale/
    logo/watermark work and re-decoding the original source every time."""
    inputs, filters, last_label = _build_stage_graph(source_path, crop)
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


# --- Final polish: trim / silence removal / loudness ------------------------------------
# Applied to the finished (captioned) video, so burned-in captions and the
# watermark stay in sync with the picture automatically.

def _has_audio(path: str) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=30)
    return bool(r.stdout.strip())


def _keep_segments(path: str, ts, te, noise_db: int = -32, min_silence: float = 0.55, pad: float = 0.12):
    """[(start, end)] of the parts worth keeping, in time relative to the
    trimmed start. Returns None when nothing useful could be detected."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if ts:
        cmd += ["-ss", f"{ts:.2f}"]
    if te:
        cmd += ["-to", f"{te:.2f}"]
    cmd += ["-i", path, "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    starts = [float(m) for m in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    dm = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    total = _probe_duration(path) or 0
    if te or ts:
        total = max(0.0, (te or total) - (ts or 0))
    if not starts or not total:
        return None
    segs, cur = [], 0.0
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total
        if s - cur > 0.25:
            segs.append((max(0.0, cur - pad), min(total, s + pad)))
        cur = e
    if total - cur > 0.25:
        segs.append((max(0.0, cur - pad), total))
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] < 0.1:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged[:180] or None


def post_process(path: str, opts: dict) -> None:
    """opts: {trim_start, trim_end (seconds, on the pre-trim timeline), silence, loudness}.
    Rewrites `path` in place. No-op when nothing is requested."""
    opts = opts or {}
    ts = float(opts.get("trim_start") or 0) or None
    te = float(opts.get("trim_end") or 0) or None
    if ts and te and te <= ts + 0.5:
        raise RuntimeError("The trim end has to be after the start.")
    silence, loud = bool(opts.get("silence")), bool(opts.get("loudness"))
    music = opts.get("music_path") if opts.get("music_path") and os.path.exists(opts.get("music_path")) else None
    if not (ts or te or silence or loud):
        if music:
            _mix_music(path, music, float(opts.get("music_vol") or 0.25))
        return
    audio = _has_audio(path)
    silence = silence and audio
    loud = loud and audio
    tmp = path + ".pp.mp4"
    cmd = ["ffmpeg", "-y"]
    if ts:
        cmd += ["-ss", f"{ts:.2f}"]
    if te:
        cmd += ["-to", f"{te:.2f}"]
    cmd += ["-i", path]
    segs = _keep_segments(path, ts, te) if silence else None
    reencode = bool(ts or te or segs)
    if segs:
        expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in segs)
        fc = f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB[v];[0:a]aselect='{expr}',asetpts=N/SR/TB" + (",loudnorm=I=-14:TP=-1.5:LRA=11" if loud else "") + "[a]"
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "0:a?"]
        if loud:
            cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "21"] if reencode else ["-c:v", "copy"])
    cmd += ["-c:a", "aac", "-movflags", "+faststart", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError("Couldn't apply the trim/polish: " + (r.stderr or "")[-300:])
    os.replace(tmp, path)
    if music:
        _mix_music(path, music, float(opts.get("music_vol") or 0.25))


def _mix_music(path: str, music: str, vol: float) -> None:
    """Loops `music` under the video's own audio at `vol` (0.05-1) of its level,
    ending with the video. Rewrites `path` in place."""
    vol = max(0.05, min(1.0, vol))
    tmp = path + ".mx.mp4"
    if _has_audio(path):
        fc = f"[1:a]volume={vol:.2f}[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    else:
        fc = f"[1:a]volume={vol:.2f}[a]"
    cmd = ["ffmpeg", "-y", "-i", path, "-stream_loop", "-1", "-i", music, "-filter_complex", fc,
           "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
           "-movflags", "+faststart", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError("Couldn't add the music: " + (r.stderr or "")[-300:])
    os.replace(tmp, path)
