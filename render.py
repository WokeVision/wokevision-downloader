import os
import textwrap
import subprocess

CANVAS_W = 720
CANVAS_H = 1280

FONT_DIR = "/app/fonts"
ASSET_DIR = "/app/assets"

CAPTION_FONT = os.path.join(FONT_DIR, "Inter-Bold.ttf")
CAPTION_COLOR = "#000000"


def build_caption_drawtext(caption_text: str) -> str:
    font_size = round(0.07 * CANVAS_W)
    max_width_px = round(0.75 * CANVAS_W)
    avg_char_width = font_size * 0.55
    max_chars_per_line = max(1, int(max_width_px / avg_char_width))

    wrapped = textwrap.fill(caption_text, width=max_chars_per_line)
    escaped = (
        wrapped.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("\n", "\\n")
    )

    pos_y = round(0.1466 * CANVAS_H)

    return (
        f"drawtext=fontfile='{CAPTION_FONT}':text='{escaped}':fontcolor={CAPTION_COLOR}:"
        f"fontsize={font_size}:x=(w-text_w)/2:y={pos_y}:line_spacing=6"
    )


def render_video(source_path: str, caption_text: str, output_path: str):
    video_w = round(0.803 * CANVAS_W)
    video_h = round(0.8091 * CANVAS_H)
    video_x = round(0.5005 * CANVAS_W - video_w / 2)
    video_y = round(0.5277 * CANVAS_H - video_h / 2)

    logo_path = os.path.join(ASSET_DIR, "logo.png")
    watermark_path = os.path.join(ASSET_DIR, "watermark.png")
    have_logo = os.path.exists(logo_path)
    have_watermark = os.path.exists(watermark_path)

    caption_filter = build_caption_drawtext(caption_text)

    inputs = ["-i", source_path]
    filters = [
        f"color=white:s={CANVAS_W}x{CANVAS_H}[bg];",
        f"[0:v]scale={video_w}:{video_h}[vid];",
        f"[bg][vid]overlay={video_x}:{video_y}[stage];",
    ]
    last_label = "stage"
    next_input_index = 1

    if have_logo:
        logo_w = round(0.14 * CANVAS_W)
        inputs += ["-i", logo_path]
        filters.append(f"[{next_input_index}:v]scale={logo_w}:-1[logo];")
        filters.append(f"[{last_label}][logo]overlay=20:20[stage_logo];")
        last_label = "stage_logo"
        next_input_index += 1

    if have_watermark:
        wm_w = round(0.45 * CANVAS_W)
        wm_x = round((CANVAS_W - wm_w) / 2)
        wm_y = round(0.93 * CANVAS_H)
        inputs += ["-i", watermark_path]
        filters.append(f"[{next_input_index}:v]scale={wm_w}:-1[wm];")
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

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="ignore")[-1500:])
