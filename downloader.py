import os
import re
import shutil
import subprocess
import requests
import yt_dlp

DOWNLOAD_DIR = "/tmp/downloads"
COOKIES_FILE = "/etc/secrets/cookies.txt"


class DownloadError(Exception):
    pass


def _writable_cookies():
    """Render Secret Files are mounted read-only, but yt-dlp wants to rewrite
    the cookie file after use (to persist refreshed session tokens) -- so we
    copy it to a writable path first and hand yt-dlp that copy instead."""
    if os.path.exists(COOKIES_FILE):
        dest = os.path.join(DOWNLOAD_DIR, "cookies.txt")
        shutil.copyfile(COOKIES_FILE, dest)
        return dest
    return None


def _attempt_ytdlp(url, raw_path, opts_extra, progress_cb=None):
    ydl_opts = {
        "outtmpl": raw_path,
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "noprogress": True,
    }
    if progress_cb:
        def _hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes")
                if total and downloaded:
                    progress_cb(min(downloaded / total, 1.0))
            elif d.get("status") == "finished":
                progress_cb(1.0)
        ydl_opts["progress_hooks"] = [_hook]
    ydl_opts.update(opts_extra)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return info or {}


def _attempt_pytubefix(url, raw_path):
    """A completely separate extraction library, used only as a last resort
    for YouTube. If yt-dlp itself is broken against some change YouTube just
    shipped, a second independent implementation has a real chance of still
    working, since the two projects don't share code and often break at
    different times."""
    from pytubefix import YouTube

    yt = YouTube(url)
    stream = (
        yt.streams.filter(progressive=True, file_extension="mp4")
        .order_by("resolution")
        .desc()
        .first()
    )
    if not stream:
        raise DownloadError("pytubefix: no suitable progressive mp4 stream found")

    tmp_dir, tmp_name = os.path.split(raw_path)
    stream.download(output_path=tmp_dir, filename=tmp_name)
    return {"title": yt.title or "", "description": yt.description or ""}


def _finish(raw_path: str, final_path: str):
    """Remux to ensure the moov atom is at the front (faststart) so the file
    streams/serves cleanly, regardless of which method produced it."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", raw_path, "-c", "copy", "-movflags", "+faststart", final_path],
            check=True,
            capture_output=True,
        )
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)
    if not os.path.exists(final_path):
        raise DownloadError("ffmpeg finished but no output file was produced")


def _download_vreddit(url: str, final_path: str, progress_cb=None) -> dict:
    """Reddit-hosted video (v.redd.it): yt-dlp wants a Reddit login for these, but the video and audio streams are public.
    Reads the stream list, takes the best video up to 720p plus the audio track and joins them."""
    m = re.search(r"v\.redd\.it/([a-z0-9]+)", url)
    if not m:
        raise DownloadError("Not a v.redd.it link")
    base = f"https://v.redd.it/{m.group(1)}/"
    hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    mpd = requests.get(base + "DASHPlaylist.mpd", headers=hdr, timeout=20)
    if mpd.status_code != 200:
        raise DownloadError(f"Reddit video list HTTP {mpd.status_code}")
    names = re.findall(r"<BaseURL>([^<]+)</BaseURL>", mpd.text)
    vids = sorted(((int(re.search(r"(?:DASH|CMAF)_(\d+)", n).group(1)), n) for n in names if re.match(r"(?:DASH|CMAF)_\d+", n)), key=lambda t: t[0])
    best = [n for h, n in vids if h <= 720] or [n for h, n in vids]
    if not best:
        raise DownloadError("No video stream found for this Reddit post")
    audio = [n for n in names if "audio" in n.lower()]
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    vpath, apath = final_path + ".v.mp4", final_path + ".a.mp4"
    try:
        for name, path in ((best[-1], vpath),) + (((audio[0], apath),) if audio else ()):
            r = requests.get(base + name, headers=hdr, timeout=60)
            if r.status_code != 200:
                if path == apath:
                    audio = []
                    continue
                raise DownloadError(f"Reddit video HTTP {r.status_code}")
            with open(path, "wb") as f:
                f.write(r.content)
        cmd = ["ffmpeg", "-y", "-i", vpath] + (["-i", apath] if audio and os.path.exists(apath) else []) + ["-c", "copy", "-movflags", "+faststart", final_path]
        subprocess.run(cmd, check=True, capture_output=True)
    finally:
        for p in (vpath, apath):
            if os.path.exists(p):
                os.remove(p)
    if progress_cb:
        progress_cb(1.0)
    return {"title": "", "description": "", "uploader": "", "uploader_id": "", "channel": "", "source_url": url, "method": "reddit-direct"}


def download_video(url: str, final_path: str, progress_cb=None) -> dict:
    """Downloads `url` to `final_path` as a faststart mp4. Returns a metadata
    dict with 'title', 'description' and 'method' (whichever attempt
    succeeded). Tries several independent strategies in order -- YouTube in
    particular changes its anti-bot defenses often enough that no single
    approach can be trusted to "always work".

    progress_cb, if given, is called with a float in [0, 1] as bytes come in
    -- real download progress, not a simulated estimate. It may be called
    several times across different fallback attempts if earlier ones fail
    partway through."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    if "v.redd.it/" in url:
        try:
            return _download_vreddit(url, final_path, progress_cb)
        except Exception as e:
            print(f"REDDIT DIRECT FAILED: {e}", flush=True)
            raise DownloadError(f"Reddit video: {e}")
    raw_path = final_path + ".raw.mp4"
    cookies = _writable_cookies()
    is_youtube = "youtube.com" in url or "youtu.be" in url

    attempts = []

    if is_youtube:
        # 1. yt-dlp relying on the bundled PO-token provider (see start.sh) --
        #    this satisfies YouTube's "proof of origin" check without needing
        #    any logged-in cookies at all, so it's the most durable option.
        attempts.append(("yt-dlp + PO-token", {
            "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
        }))
        # 2. A different client combination, with cookies if we have them --
        #    covers the case where the PO-token provider itself is briefly
        #    down or YouTube is treating one client type differently.
        extra = {"extractor_args": {"youtube": {"player_client": ["android", "ios"]}}}
        if cookies:
            extra["cookiefile"] = cookies
        attempts.append(("yt-dlp alt client" + (" + cookies" if cookies else ""), extra))
    else:
        # TikTok / Instagram / X / Threads etc. -- yt-dlp handles these
        # reliably without any special handling.
        attempts.append(("yt-dlp", {"cookiefile": cookies} if cookies else {}))

    last_error = None
    for name, extra in attempts:
        try:
            if os.path.exists(raw_path):
                os.remove(raw_path)
            info = _attempt_ytdlp(url, raw_path, extra, progress_cb=progress_cb)
            if os.path.exists(raw_path):
                _finish(raw_path, final_path)
                return {
                    "title": info.get("title", "") or "",
                    "description": info.get("description", "") or "",
                    "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
                    "uploader_id": info.get("uploader_id") or "",
                    "channel": info.get("channel") or "",
                    "source_url": url,
                    "method": name,
                }
        except Exception as e:
            print(f"DOWNLOAD ATTEMPT FAILED ({name}): {e}", flush=True)
            last_error = e

    # 3. Last resort for YouTube only: a second, independent library.
    if is_youtube:
        try:
            if os.path.exists(raw_path):
                os.remove(raw_path)
            meta = _attempt_pytubefix(url, raw_path)
            if os.path.exists(raw_path):
                _finish(raw_path, final_path)
                meta["method"] = "pytubefix"
                return meta
        except Exception as e:
            print(f"DOWNLOAD ATTEMPT FAILED (pytubefix): {e}", flush=True)
            last_error = e

    raise DownloadError(f"All download methods failed. Last error: {last_error}")
