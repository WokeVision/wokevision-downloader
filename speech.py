"""Speech -> timed words -> burned-in caption cues (ASS), plus the clip finder.

Kept separate from transcribe.py (plain-text transcript used for caption
writing) so the existing pipeline is untouched when no key is configured."""
import json
import os
import re
import subprocess
import uuid
import requests

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
CHAT_URL = "https://api.openai.com/v1/chat/completions"
TMP_DIR = "/tmp/downloads"
CHUNK_SECONDS = 600  # 10 min of 64kbps mono mp3 ~= 4.8MB, far under Whisper's 25MB cap

# WokeVision red + white outline (ASS colours are &HAABBGGRR)
CAPTION_RED_ASS = "&H003C2BFF"
CAPTION_OUTLINE_ASS = "&H00FFFFFF"


# ----------------------------------------------------------------- whisper
def _extract_chunk(video_path, start, length, out_path):
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(length), "-i", video_path,
           "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


def _probe_duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", path],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


def _whisper_verbose(audio_path):
    with open(audio_path, "rb") as f:
        resp = requests.post(
            WHISPER_URL,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
            data=[("model", "whisper-1"), ("response_format", "verbose_json"),
                  ("timestamp_granularities[]", "word"),
                  ("timestamp_granularities[]", "segment")],
            timeout=300,
        )
    resp.raise_for_status()
    return resp.json()


def transcribe_words(video_path, progress_cb=None):
    """Returns {"text", "words":[{w,start,end}], "segments":[{start,end,text}],
    "duration"}; chunks long videos and offsets timestamps. Empty result if
    there is no key or Whisper fails (callers treat that as "no captions")."""
    empty = {"text": "", "words": [], "segments": [], "duration": _probe_duration(video_path) or 0}
    if not OPENAI_API_KEY:
        return empty
    os.makedirs(TMP_DIR, exist_ok=True)
    duration = empty["duration"] or 0
    n_chunks = max(1, int(-(-duration // CHUNK_SECONDS))) if duration else 1
    words, segments, texts = [], [], []
    for i in range(n_chunks):
        offset = i * CHUNK_SECONDS
        audio = os.path.join(TMP_DIR, f"sp_{uuid.uuid4()}.mp3")
        try:
            if not _extract_chunk(video_path, offset, CHUNK_SECONDS, audio):
                continue
            data = _whisper_verbose(audio)
            texts.append((data.get("text") or "").strip())
            for w in data.get("words") or []:
                words.append({"w": str(w.get("word", "")).strip(),
                              "start": float(w["start"]) + offset,
                              "end": float(w["end"]) + offset})
            for s in data.get("segments") or []:
                segments.append({"start": float(s["start"]) + offset,
                                 "end": float(s["end"]) + offset,
                                 "text": str(s.get("text", "")).strip()})
        except Exception as e:
            print(f"TRANSCRIBE_WORDS chunk {i} failed: {e}", flush=True)
        finally:
            if os.path.exists(audio):
                os.remove(audio)
        if progress_cb:
            progress_cb((i + 1) / n_chunks)
    words = [w for w in words if w["w"]]
    return {"text": " ".join(t for t in texts if t), "words": words,
            "segments": segments, "duration": duration}


# -------------------------------------------------------------- cue builder
MAX_LINE_CHARS = 20
MAX_CUE_CHARS = 38
MAX_CUE_SECONDS = 2.8
PAUSE_BREAK = 0.55


def _wrap_two_lines(text):
    """Balance a cue's text over at most two lines."""
    if len(text) <= MAX_LINE_CHARS:
        return text
    words = text.split()
    best, best_diff = text, 10 ** 9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(len(a) - len(b))
        if diff < best_diff:
            best, best_diff = f"{a}\n{b}", diff
    return best


def build_cues(words):
    """Groups timed words into short readable cues: [{start,end,text}] where
    text may contain a single \\n. Breaks on sentence punctuation, pauses,
    length and duration."""
    cues, cur = [], []

    def flush():
        nonlocal cur
        if not cur:
            return
        text = " ".join(w["w"] for w in cur)
        cues.append({"start": round(cur[0]["start"], 2),
                     "end": round(cur[-1]["end"], 2),
                     "text": _wrap_two_lines(text)})
        cur = []

    for i, w in enumerate(words):
        if cur:
            gap = w["start"] - cur[-1]["end"]
            joined = " ".join(x["w"] for x in cur) + " " + w["w"]
            if (gap > PAUSE_BREAK or len(joined) > MAX_CUE_CHARS
                    or (w["end"] - cur[0]["start"]) > MAX_CUE_SECONDS):
                flush()
        cur.append(w)
        if re.search(r"[.!?]$", w["w"]) and len(" ".join(x["w"] for x in cur)) >= 12:
            flush()
    flush()

    # hold each cue until just before the next (max +0.35s), min 0.6s on screen
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else c["end"] + 1.0
        end = max(c["end"], min(c["end"] + 0.35, nxt - 0.02))
        end = max(end, min(c["start"] + 0.6, nxt - 0.02))
        c["end"] = round(end, 2)
    return cues


def clip_cues(cues, start, end):
    """Cues re-based to a clip [start,end) (for the clipper)."""
    out = []
    for c in cues:
        if c["end"] <= start or c["start"] >= end:
            continue
        out.append({"start": round(max(c["start"], start) - start, 2),
                    "end": round(min(c["end"], end) - start, 2),
                    "text": c["text"]})
    return out


def clip_words(words, start, end):
    return [{"w": w["w"], "start": w["start"] - start, "end": w["end"] - start}
            for w in words if w["end"] > start and w["start"] < end]


# ---------------------------------------------------------------------- ASS
def _ass_time(t):
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text):
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return text.replace("\r", "").replace("\n", "\\N")


def build_ass(cues, play_w, play_h, margin_v, font_size=52, font_name="Poppins"):
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {play_w}\nPlayResY: {play_h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},{CAPTION_RED_ASS},{CAPTION_RED_ASS},"
        f"{CAPTION_OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,5,0,2,80,80,{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for c in cues:
        txt = (c.get("text") or "").strip()
        if not txt or c["end"] <= c["start"]:
            continue
        lines.append(f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},Default,,0,0,0,,{_ass_escape(txt.upper())}")
    return head + "\n".join(lines) + "\n"


# ----------------------------------------------------------------- clipper
def _chat_json(system, user, model="gpt-4o"):
    resp = requests.post(
        CHAT_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={"model": model, "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=180,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def _fmt(t):
    t = int(t)
    return f"{t // 3600}:{(t % 3600) // 60:02d}:{t % 60:02d}"


def parse_timestamp(s):
    """'1:23', '01:02:03', '90', '90s' -> seconds, else None."""
    s = s.strip().lower().rstrip("s")
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def parse_focus(text):
    """Pulls 'a-b' / 'a to b' time ranges out of free text. Returns
    (ranges, remaining_text)."""
    ranges = []
    pat = re.compile(r"(\d{1,2}(?::\d{2}){1,2}|\d+s?)\s*(?:-|–|—|to|until)\s*(\d{1,2}(?::\d{2}){1,2}|\d+s?)")
    for m in pat.finditer(text or ""):
        a, b = parse_timestamp(m.group(1)), parse_timestamp(m.group(2))
        if a is not None and b is not None and b > a:
            ranges.append((a, b))
    return ranges, pat.sub("", text or "").strip()


def pick_clips(transcript, duration, focus_ranges=None, notes="", max_clips=6,
               min_len=15, max_len=75):
    """Asks the LLM for the best self-contained, shareable moments. Returns
    [{start,end,title,reason,score}] snapped to segment boundaries."""
    segs = transcript["segments"] or []
    if not segs:
        return []
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs]
    body = "\n".join(lines)
    if len(body) > 90000:
        body = body[:90000]
    focus = ""
    if focus_ranges:
        focus = ("ONLY choose clips that lie inside these ranges (seconds): "
                 + ", ".join(f"{a:.0f}-{b:.0f}" for a, b in focus_ranges) + ". ")
    system = (
        "You are the clip editor for a political/news meme page. From a timestamped transcript, pick the "
        f"{max_clips} best moments most likely to go viral as short vertical clips: a strong hook in the first "
        "seconds, a complete thought that makes sense without context, punchy, shocking, funny or emotionally "
        f"charged. Each clip must be between {min_len} and {max_len} seconds, must start at the beginning of a "
        "sentence and end at the end of one, and must not overlap another clip. "
        f"{focus}Return JSON: {{\"clips\":[{{\"start\":sec,\"end\":sec,\"title\":\"<=8 words\","
        "\"reason\":\"one short sentence\",\"score\":1-100}]}. Best first."
    )
    user = f"Video length: {duration:.0f}s.\n{('Editor notes: ' + notes) if notes else ''}\nTranscript:\n{body}"
    data = _chat_json(system, user)
    out = []
    for c in data.get("clips") or []:
        try:
            s, e = float(c["start"]), float(c["end"])
        except Exception:
            continue
        # snap to nearest segment boundaries
        s = min(segs, key=lambda x: abs(x["start"] - s))["start"]
        e = min(segs, key=lambda x: abs(x["end"] - e))["end"]
        if focus_ranges and not any(s >= a - 2 and e <= b + 2 for a, b in focus_ranges):
            continue
        if e - s < min_len * 0.6 or e <= s:
            continue
        if e - s > max_len * 1.3:
            e = s + max_len
        if any(not (e <= o["start"] or s >= o["end"]) for o in out):
            continue
        out.append({"start": round(s, 2), "end": round(e, 2),
                    "title": str(c.get("title", ""))[:80],
                    "reason": str(c.get("reason", ""))[:200],
                    "score": int(c.get("score") or 0)})
    return out[:max_clips]


def cut_clip(video_path, start, end, out_path):
    """Frame-accurate cut (re-encode, fast preset) so the editor pipeline
    gets a normal standalone source file."""
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", video_path, "-t", f"{end - start:.2f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
           "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-1500:])
