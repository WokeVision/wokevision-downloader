import os
import subprocess
from PIL import Image, ImageFont

CANVAS_W = 720
CANVAS_H = 1280

FONT_DIR = "/app/fonts"
ASSET_DIR = "/app/assets"

CAPTION_FONT_PATH = os.path.join(FONT_DIR, "SFProText.ttf")
CAPTION_COLOR = "#000000"

# Video box: 3:4 aspect ratio, centered horizontally, positioned to leave
# room above for a 1-2 line caption and below for breathing room.
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


def get_scaled_height(image_path, target_w):
    with Image.open(image_path) as img:
        w, h = img.size
    return round(target_w * h / w)


def measure_and_wrap(text, font_path, max_width_px, max_font, min_font, max_lines):
    words = text.split()

    def wrap_at(font):
        lines, current = [], ""
        for word in words:
            trial = (current + " " + word).strip()
            if font.getlength(trial) <= max_width_px or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    for font_size in range(max_font, min_font - 1, -2):
        font = ImageFont.truetype(font_path, font_size)
        lines = wrap_at(font)
        if len(lines) <= max_lines:
            ascent, descent = font.getmetrics()
            line_height = int((ascent + descent) * 1.25)
            return font_size, lines, line_height

    font = ImageFont.truetype(font_path, min_font)
    lines = wrap_at(font)[:max_lines]
    ascent, descent = font.getmetrics()
    line_height = int((ascent + descent) * 1.25)
    return min_font, lines, line_height


def build_caption_drawtext(caption_text):
    max_width_px = VIDEO_W - 2 * CAPTION_SIDE_PADDING
    font_size, lines, line_height = measure_and_wrap(
        caption_text, CAPTION_FONT_PATH, max_width_px,
        CAPTION_MAX_FONT, CAPTION_MIN_FONT, CAPTION_MAX_LINES,
    )
    text_block_h = line_height * len(lines)
    caption_y = VIDEO_Y - CAPTION_GAP - text_block_h

    joined = "\n".join(lines)
    escaped = (
        joined.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("\n", "\\n")
    )

    return (
        f"drawtext=fontfile='{CAPTION_FONT_PATH}':text='{escaped}':fontcolor={CAPTION_COLOR}:"
        f"fontsize={font_size}:x=(w-text_w)/2:y={caption_y}:line_spacing=6"
    )


def render_video(source_path: str, caption_text: str, output_path: str):
    logo_path = os.path.join(ASSET_DIR, "logo.png")
    watermark_path = os.path.join(ASSET_DIR, "watermark.png")
    have_logo = os.path.exists(logo_path)
    have_watermark = os.path.exists(watermark_path)

    caption_filter = build_caption_drawtext(caption_text)

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

    filters.append(f"[{last_label}]{caption_filter}[final]")
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
