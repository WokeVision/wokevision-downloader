import os
import shutil
import subprocess
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


def _attempt_ytdlp(url, raw_path, opts_extra):
    ydl_opts = {
        "outtmpl": raw_path,
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "noprogress": True,
    }
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


def download_video(url: str, final_path: str) -> dict:
    """Downloads `url` to `final_path` as a faststart mp4. Returns a metadata
    dict with 'title', 'description' and 'method' (whichever attempt
    succeeded). Tries several independent strategies in order -- YouTube in
    particular changes its anti-bot defenses often enough that no single
    approach can be trusted to "always work"."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
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
            info = _attempt_ytdlp(url, raw_path, extra)
            if os.path.exists(raw_path):
                _finish(raw_path, final_path)
                return {
                    "title": info.get("title", "") or "",
                    "description": info.get("description", "") or "",
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
