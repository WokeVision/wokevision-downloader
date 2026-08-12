import os
import textwrap
import subprocess

CANVAS_W = 720
CANVAS_H = 1280

FONT_DIR = "/app/fonts"
ASSET_DIR = "/app/assets"

CAPTION_FONT = os.path.join(FONT_DIR, "Inter-Bold.ttf")
SUBTITLE_FONT_NAME = "Montserrat"  # must match the font's internal name for libass

HIGHLIGHT_COLOR = "#e74c3c"
STROKE_COLOR = "#000000"
FILL_COLOR = "#ffffff"
CAPTION_COLOR = "#000000"

MAX_SUBTITLE_CHARS = 14


def rgb_to_ass(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
    return f"&H{b}{g}{r}&".upper()


def ass_time(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def words_with_timing(segments):
    """Estimate per-word start/end by splitting each segment's duration
    proportionally across its words, based on word length."""
    all_words = []
    for seg in segments:
        text = seg.get("text", "").strip()
        words = text.split()
        if not words:
            continue
        total_chars = sum(len(w) for w in words) or 1
        duration = max(seg["end"] - seg["start"], 0.01)
        cursor = seg["start"]
        for w in words:
            w_dur = duration * (len(w) / total_chars)
            all_words.append({"word": w, "start": cursor, "end": cursor + w_dur})
            cursor += w_dur
    return all_words


def chunk_words(all_words, max_len=MAX_SUBTITLE_CHARS):
    chunks = []
    current = []
    current_len = 0
    for w in all_words:
        wl = len(w["word"])
        added = wl + (1 if current else 0)
        if current and current_len + added > max_len:
            chunks.append(current)
            current = [w]
            current_len = wl
        else:
            current.append(w)
            current_len += added
    if current:
        chunks.append(current)
    return chunks


def build_ass_subtitles(segments, output_path):
    all_words = words_with_timing(segments)
    chunks = chunk_words(all_words)

    font_size = round(0.06 * CANVAS_W)
    pos_x = round(0.50 * CANVAS_W)
    pos_y = round(0.74 * CANVAS_H)

    fill_ass = rgb_to_ass(FILL_COLOR)
    highlight_ass = rgb_to_ass(HIGHLIGHT_COLOR)
    outline_ass = rgb_to_ass(STROKE_COLOR)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {CANVAS_W}
PlayResY: {CANVAS_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{SUBTITLE_FONT_NAME},{font_size},{fill_ass},{fill_ass},{outline_ass},&H000000&,-1,0,0,0,100,100,0,0,1,3,0,5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    for chunk in chunks:
        for i, w in enumerate(chunk):
            parts = []
            for j, cw in enumerate(chunk):
                if j == i:
                    parts.append("{\\c" + highlight_ass + "}" + cw["word"] + "{\\c" + fill_ass + "}")
                else:
                    parts.append(cw["word"])
            line_text = " ".join(parts)
            start = ass_time(w["start"])
            end = ass_time(w["end"])
            lines.append(
                f"Dialogue: 0,{start},{end},Default,,0,0,0,,{{\\an5\\pos({pos_x},{pos_y})}}{line_text}\n"
            )

    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


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


def render_video(source_path: str, caption_text: str, segments: list, output_path: str):
    work_dir = os.path.dirname(output_path)
    ass_path = os.path.join(work_dir, "subs.ass")
    build_ass_subtitles(segments, ass_path)

    video_w = round(0.803 * CANVAS_W)
    video_h = round(0.8091 * CANVAS_H)
    video_x = round(0.5005 * CANVAS_W - video_w / 2)
    video_y = round(0.5277 * CANVAS_H - video_h / 2)

    logo_w = round(0.14 * CANVAS_W)
    logo_x = 20
    logo_y = 20

    watermark_w = round(0.45 * CANVAS_W)
    watermark_x = round((CANVAS_W - watermark_w) / 2)
    watermark_y = round(0.93 * CANVAS_H)

    caption_filter = build_caption_drawtext(caption_text)
    ass_path_escaped = ass_path.replace(":", "\\:")

    filter_complex = (
        f"color=white:s={CANVAS_W}x{CANVAS_H}[bg];"
        f"[1:v]scale={video_w}:{video_h}[vid];"
        f"[bg][vid]overlay={video_x}:{video_y}[stage];"
        f"[2:v]scale={logo_w}:-1[logo];"
        f"[stage][logo]overlay={logo_x}:{logo_y}[stage2];"
        f"[3:v]scale={watermark_w}:-1[wm];"
        f"[stage2][wm]overlay={watermark_x}:{watermark_y}[stage3];"
        f"[stage3]{caption_filter}[captioned];"
        f"[captioned]subtitles='{ass_path_escaped}'[final]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", source_path,
        "-i", source_path,
        "-i", os.path.join(ASSET_DIR, "logo.png"),
        "-i", os.path.join(ASSET_DIR, "watermark.png"),
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
