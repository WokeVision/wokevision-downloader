import os
import re
import subprocess
import requests
from PIL import Image, ImageFont

CANVAS_W = 720
CANVAS_H = 1280

FONT_DIR = "/app/fonts"
ASSET_DIR = "/app/assets"
EMOJI_CACHE_DIR = "/tmp/emoji_cache"
TWEMOJI_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/{cp}.png"

CAPTION_FONT_PATH = os.path.join(FONT_DIR, "SFProText.ttf")
CAPTION_COLOR = "#000000"

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

# Matches a single emoji "run" - one or more emoji codepoints combined with
# variation selectors / ZWJ / skin tone modifiers (e.g. a flag, or a person +
# skin tone), so a multi-part emoji is treated as one atomic token.
EMOJI_PATTERN = re.compile(
    "("
    "(?:[\U0001F1E6-\U0001F1FF]{2})"
    "|(?:[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF\U00002B00-\U00002BFF]"
    "[\U0000FE0F\U0000200D\U0001F3FB-\U0001F3FF]*)+"
    ")"
)


def strip_hashtags(text: str) -> str:
    text = HASHTAG_PATTERN.sub("", text)
    text = text.strip().strip('"').strip("'").strip()
    return re.sub(r"\s+", " ", text)


def codepoints_for(emoji: str) -> str:
    cps = [f"{ord(ch):x}" for ch in emoji if ch != "\uFE0F"]
    return "-".join(cps)


def get_emoji_image(emoji: str):
    cp = codepoints_for(emoji)
    if not cp:
        return None
    os.makedirs(EMOJI_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(EMOJI_CACHE_DIR, f"{cp}.png")
    if os.path.exists(cache_path):
        return cache_path
    url = TWEMOJI_CDN.format(cp=cp)
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200 and resp.content:
            with open(cache_path, "wb") as f:
                f.write(resp.content)
            return cache_path
    except Exception:
        pass
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


def build_caption_filters(caption_text, start_label, next_input_index):
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
    text_block_h = line_height * len(lines_tokens)
    caption_top_y = VIDEO_Y - CAPTION_GAP - text_block_h

    filters = []
    inputs = []
    last_label = start_label
    input_index = next_input_index  # only advances when a new -i is actually added
    label_id = 0  # unique label numbering, independent of input_index

    for line_idx, line_tokens in enumerate(lines_tokens):
        widths = [token_width(t, font, font_size) for t in line_tokens]
        gap_total = space_w * (len(line_tokens) - 1) if len(line_tokens) > 1 else 0
        total_w = sum(widths) + gap_total
        cur_x = (CANVAS_W - total_w) / 2
        line_y = caption_top_y + line_idx * line_height

        for tok, w in zip(line_tokens, widths):
            label_id += 1
            if EMOJI_PATTERN.fullmatch(tok):
                img_path = get_emoji_image(tok)
                if img_path:
                    emoji_size = font_size
                    emoji_y = line_y + round(font_size * 0.12)
                    inputs += ["-i", img_path]
                    scaled_label = f"emoscaled{label_id}"
                    out_label = f"stage{label_id}"
                    filters.append(f"[{input_index}:v]scale={emoji_size}:{emoji_size}[{scaled_label}];")
                    filters.append(
                        f"[{last_label}][{scaled_label}]overlay={round(cur_x)}:{round(emoji_y)}[{out_label}];"
                    )
                    last_label = out_label
                    input_index += 1
            else:
                escaped = (
                    tok.replace("\\", "\\\\")
                    .replace(":", "\\:")
                    .replace("'", "\u2019")
                )
                out_label = f"stage{label_id}"
                filters.append(
                    f"[{last_label}]drawtext=fontfile='{CAPTION_FONT_PATH}':text='{escaped}':"
                    f"fontcolor={CAPTION_COLOR}:fontsize={font_size}:x={round(cur_x)}:y={round(line_y)}[{out_label}];"
                )
                last_label = out_label
            cur_x += w + space_w

    filters.append(f"[{last_label}]null[final]")
    return filters, inputs, input_index


def render_video(source_path: str, caption_text: str, output_path: str):
    logo_path = os.path.join(ASSET_DIR, "logo.png")
    watermark_path = os.path.join(ASSET_DIR, "watermark.png")
    have_logo = os.path.exists(logo_path)
    have_watermark = os.path.exists(watermark_path)

    inputs = ["-i", source_path]
    filters = [
        f"color=white:s={CANVAS_W}x{CANVAS_H}[bg];",
        f"[0:v]crop=iw:iw*4/3:0:(ih-iw*4/3)/2,scale={VIDEO_W}:{VIDEO_H}[vid];",
        f"[bg][vid]overlay={VIDEO_X}:{VIDEO_Y}[stage];",
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
            f"colorchannelmixer=aa={LOGO_OPACITY}[logo];"
        )
        filters.append(f"[{last_label}][logo]overlay={logo_x}:{logo_y}[stage_logo];")
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
            f"colorchannelmixer=aa={WATERMARK_OPACITY}[wm];"
        )
        filters.append(f"[{last_label}][wm]overlay={wm_x}:{wm_y}[stage_wm];")
        last_label = "stage_wm"
        next_input_index += 1

    caption_filters, caption_inputs, _ = build_caption_filters(caption_text, last_label, next_input_index)
    filters += caption_filters
    inputs += caption_inputs

    filter_complex = "".join(filters)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[final]",
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-shortest",
        output_path,
    ]

    print("FFMPEG COMMAND:", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, capture_output=True)
    stderr_text = result.stderr.decode(errors="ignore")
    print("FFMPEG STDERR:", stderr_text, flush=True)
    if result.returncode != 0:
        raise RuntimeError(stderr_text[-4000:])
