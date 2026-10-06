"""Persistent storage for platform connections (OAuth tokens), backed by a
tiny Postgres database (Neon's free tier -- chosen specifically because it
wakes itself automatically on the next query after sleeping, unlike
Supabase/Mongo's free tiers which need a manual dashboard click to resume,
and unlike Render's own free Postgres which gets deleted outright after 30
days). None of that is visible from here -- this module just talks to
whatever DATABASE_URL points at.

Tokens are encrypted at rest with Fernet (symmetric encryption) using a key
from TOKEN_ENCRYPTION_KEY, so a database leak alone doesn't hand over
working access to your accounts.
"""
import os
import json
import contextlib
import psycopg2
import psycopg2.extras
from cryptography.fernet import Fernet, InvalidToken

DATABASE_URL = os.environ.get("DATABASE_URL")
TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY")

_fernet = Fernet(TOKEN_ENCRYPTION_KEY.encode()) if TOKEN_ENCRYPTION_KEY else None


def configured() -> bool:
    """Whether persistent connection storage is actually usable right now --
    both env vars need to be set. Callers should treat "not configured" as a
    normal, expected state (not an error) until the user finishes setup."""
    return bool(DATABASE_URL and TOKEN_ENCRYPTION_KEY)


@contextlib.contextmanager
def _conn():
    # A short, explicit timeout here matters a lot: with none, a bad/slow
    # connection string can hang the whole process indefinitely at startup
    # (since init_db() runs in FastAPI's startup event), which looks from
    # the outside like the app never deploys at all rather than like a
    # clear, fast failure.
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Creates the connections table if it doesn't exist yet. Safe to call
    on every startup -- CREATE TABLE IF NOT EXISTS is a no-op once it's
    there."""
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS connections (
                    platform TEXT PRIMARY KEY,
                    access_token TEXT,
                    refresh_token TEXT,
                    expires_at TIMESTAMPTZ,
                    extra JSONB,
                    connected_at TIMESTAMPTZ DEFAULT now(),
                    last_checked_at TIMESTAMPTZ,
                    last_check_ok BOOLEAN,
                    last_error TEXT
                )
            """)
            # One row per registered passkey (WebAuthn credential) -- a
            # person can have more than one (phone + laptop), so this isn't
            # keyed to a single "the" credential the way `connections` is
            # keyed to a single platform.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS passkeys (
                    credential_id BYTEA PRIMARY KEY,
                    public_key BYTEA NOT NULL,
                    sign_count BIGINT NOT NULL DEFAULT 0,
                    label TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    last_used_at TIMESTAMPTZ
                )
            """)
            # Scheduled posts: one row per (edit, platform) so every platform
            # can have its own time. The caption/platform text is NOT copied
            # here -- it is read from the history row when the post fires, so
            # edits made after scheduling are what actually goes out.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS account_profiles (
                    platform TEXT PRIMARY KEY,
                    data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_posts (
                    id UUID PRIMARY KEY,
                    history_id UUID NOT NULL,
                    platform TEXT NOT NULL,
                    run_at TIMESTAMPTZ NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    attempts INT NOT NULL DEFAULT 0,
                    started_at TIMESTAMPTZ,
                    result JSONB,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS scheduled_posts_due ON scheduled_posts (status, run_at)")
            # One row per processed video -- the editor's history. Kept
            # indefinitely (unlike the rendered video file itself, which is
            # cleaned up from local disk after a while -- see KEEP_ALIVE_SECONDS
            # in main.py); a history row whose file has aged out just shows
            # captions/platforms with no video preview.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id UUID PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    title TEXT,
                    video_filename TEXT,
                    source_filename TEXT,
                    on_screen_caption TEXT,
                    posting_caption TEXT,
                    transcript TEXT,
                    meta JSONB,
                    publish_results JSONB
                )
            """)
            # Older deploys may already have a history table from before
            # source_filename/transcript existed -- add them if missing
            # rather than requiring a manual migration.
            cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS source_filename TEXT")
            cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS transcript TEXT")
            # Per-platform versions of the posting caption (and the user's
            # edits to them), so nothing typed in the editor is lost.
            cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS platform_posts JSONB")
            # Clipping sessions: one row per long video the clipper has
            # analysed, with the suggested clips and the word-level
            # transcript, so the Clipping history survives restarts.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clip_sessions (
                    id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    title TEXT,
                    status TEXT,
                    stage_label TEXT,
                    error TEXT,
                    duration REAL,
                    clips JSONB,
                    speech JSONB
                )
            """)


def _encrypt(value: str):
    if value is None or not _fernet:
        return value
    return _fernet.encrypt(value.encode()).decode()


def _decrypt(value: str):
    if value is None or not _fernet:
        return value
    try:
        return _fernet.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        # Key rotated or data predates encryption -- treat as unreadable
        # rather than crash; caller sees this as "not connected".
        return None


def save_connection(platform: str, access_token: str, refresh_token: str = None,
                     expires_at=None, extra: dict = None):
    if not configured():
        raise RuntimeError("Connection storage isn't configured yet (DATABASE_URL / TOKEN_ENCRYPTION_KEY missing).")
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO connections (platform, access_token, refresh_token, expires_at, extra, connected_at, last_checked_at, last_check_ok, last_error)
                VALUES (%s, %s, %s, %s, %s, now(), now(), true, NULL)
                ON CONFLICT (platform) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = COALESCE(EXCLUDED.refresh_token, connections.refresh_token),
                    expires_at = EXCLUDED.expires_at,
                    extra = EXCLUDED.extra,
                    connected_at = now(),
                    last_checked_at = now(),
                    last_check_ok = true,
                    last_error = NULL
            """, (
                platform, _encrypt(access_token), _encrypt(refresh_token),
                expires_at, json.dumps(extra or {}),
            ))


def get_connection(platform: str):
    """Returns {access_token, refresh_token, expires_at, extra, connected_at,
    last_check_ok, last_error} or None if never connected / storage isn't
    configured. Tokens are decrypted here."""
    if not configured():
        return None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM connections WHERE platform = %s", (platform,))
            row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    row["access_token"] = _decrypt(row.get("access_token"))
    row["refresh_token"] = _decrypt(row.get("refresh_token"))
    return row


def set_check_result(platform: str, ok: bool, error: str = None):
    """Records the result of a live "is this connection still working" probe,
    without touching the stored tokens themselves."""
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE connections SET last_checked_at = now(), last_check_ok = %s, last_error = %s
                WHERE platform = %s
            """, (ok, error, platform))


def delete_connection(platform: str):
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM connections WHERE platform = %s", (platform,))


def list_connections():
    """All stored connections, decrypted. Used by the /connections status
    endpoint."""
    if not configured():
        return {}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM connections")
            rows = cur.fetchall()
    out = {}
    for row in rows:
        row = dict(row)
        row["access_token"] = _decrypt(row.get("access_token"))
        row["refresh_token"] = _decrypt(row.get("refresh_token"))
        out[row["platform"]] = row
    return out


# --- Passkeys (WebAuthn credentials) ---------------------------------------

def add_passkey(credential_id: bytes, public_key: bytes, sign_count: int, label: str = None):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO passkeys (credential_id, public_key, sign_count, label)
                VALUES (%s, %s, %s, %s)
            """, (psycopg2.Binary(credential_id), psycopg2.Binary(public_key), sign_count, label))


def list_passkeys():
    """All registered passkeys, raw bytes for credential_id/public_key."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM passkeys")
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["credential_id"] = bytes(r["credential_id"])
        r["public_key"] = bytes(r["public_key"])
    return rows


def get_passkey(credential_id: bytes):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM passkeys WHERE credential_id = %s", (psycopg2.Binary(credential_id),))
            row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    row["credential_id"] = bytes(row["credential_id"])
    row["public_key"] = bytes(row["public_key"])
    return row


def update_passkey_sign_count(credential_id: bytes, sign_count: int):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE passkeys SET sign_count = %s, last_used_at = now()
                WHERE credential_id = %s
            """, (sign_count, psycopg2.Binary(credential_id)))


def any_passkeys_registered() -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM passkeys LIMIT 1")
            return cur.fetchone() is not None


# --- Edit history ------------------------------------------------------------

def save_history_entry(entry_id: str, title: str, video_filename: str, source_filename: str,
                        on_screen_caption: str, posting_caption: str, transcript: str, meta: dict):
    """Creates (or fully overwrites) a history row for a processed video --
    called once per /process, /process-file, or "change video caption"
    completion, since each of those produces a fresh render under the same
    job id. created_at is intentionally NOT bumped on an update, so a
    "change caption" on an old entry doesn't jump it back to the top of the
    history list -- it's still the same edit, just revised."""
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO history (id, title, video_filename, source_filename, on_screen_caption, posting_caption, transcript, meta, publish_results, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, now())
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    video_filename = EXCLUDED.video_filename,
                    source_filename = EXCLUDED.source_filename,
                    on_screen_caption = EXCLUDED.on_screen_caption,
                    posting_caption = EXCLUDED.posting_caption,
                    transcript = EXCLUDED.transcript,
                    meta = EXCLUDED.meta
            """, (entry_id, title, video_filename, source_filename, on_screen_caption,
                  posting_caption, transcript, json.dumps(meta or {})))


def update_history_caption(entry_id: str, on_screen_caption: str = None, posting_caption: str = None):
    """Used by the "change caption" (text-only) regenerate, which doesn't
    touch the video file."""
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            if on_screen_caption is not None:
                cur.execute("UPDATE history SET on_screen_caption = %s WHERE id = %s", (on_screen_caption, entry_id))
            if posting_caption is not None:
                cur.execute("UPDATE history SET posting_caption = %s WHERE id = %s", (posting_caption, entry_id))


def update_history_meta(entry_id: str, meta: dict):
    """Persists the (possibly angle-updated) meta for a history entry."""
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE history SET meta = %s WHERE id = %s", (json.dumps(meta or {}), entry_id))


def update_history_platform_posts(entry_id: str, posts: dict):
    """Saves the per-platform post versions (generated or user-edited)."""
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE history SET platform_posts = %s WHERE id = %s", (json.dumps(posts or {}), entry_id))


def update_history_publish_results(entry_id: str, results: dict):
    """Merges newly-finished per-platform publish results into whatever's
    already recorded for this entry, so re-publishing to one platform later
    doesn't erase an earlier platform's recorded result."""
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT publish_results FROM history WHERE id = %s", (entry_id,))
            row = cur.fetchone()
            existing = (row and row.get("publish_results")) or {}
            existing.update(results or {})
            cur.execute("UPDATE history SET publish_results = %s WHERE id = %s", (json.dumps(existing), entry_id))


def list_history(limit: int = 100):
    if not configured():
        return []
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM history ORDER BY created_at DESC LIMIT %s", (limit,))
            return [_titled(r) for r in cur.fetchall()]


def _titled(r):
    r = dict(r)
    # The list/recognition title is the last saved on-screen caption.
    r["title"] = (r.get("on_screen_caption") or "").strip() or r.get("title")
    return r


def get_history_entry(entry_id: str):
    if not configured():
        return None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM history WHERE id = %s", (entry_id,))
            row = cur.fetchone()
    return _titled(row) if row else None


# --- Clipping sessions ---------------------------------------------------------

_CLIP_COLS = ("title", "status", "stage_label", "error", "duration", "clips", "speech")


def clip_upsert(cid: str, **fields):
    if not configured():
        return
    fields = {k: v for k, v in fields.items() if k in _CLIP_COLS}
    for k in ("clips", "speech"):
        if k in fields:
            fields[k] = json.dumps(fields[k])
    cols = ["id"] + list(fields)
    vals = [cid] + list(fields.values())
    sets = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields) or "id = EXCLUDED.id"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO clip_sessions ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (id) DO UPDATE SET {sets}", vals)


def clip_get(cid: str):
    if not configured():
        return None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM clip_sessions WHERE id = %s", (cid,))
            row = cur.fetchone()
    return dict(row) if row else None


def clip_list(limit: int = 60):
    if not configured():
        return []
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, created_at, title, status, duration, clips FROM clip_sessions ORDER BY created_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]


# --- Scheduled posts ----------------------------------------------------------

def _row(r):
    r = dict(r)
    for k in ("id", "history_id"):
        if r.get(k) is not None:
            r[k] = str(r[k])
    for k in ("run_at", "started_at", "created_at"):
        if r.get(k) is not None:
            r[k] = r[k].isoformat()
    return r


def sched_upsert(history_id: str, platform: str, run_at):
    """Schedules (or re-schedules) one platform of one edit. An existing
    not-yet-posted row for the same edit+platform is updated rather than
    duplicated."""
    import uuid
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT id FROM scheduled_posts WHERE history_id = %s AND platform = %s
                           AND status IN ('scheduled', 'missed', 'error') ORDER BY created_at DESC LIMIT 1""",
                        (history_id, platform))
            ex = cur.fetchone()
            if ex:
                cur.execute("""UPDATE scheduled_posts SET run_at = %s, status = 'scheduled', attempts = 0,
                               started_at = NULL, result = NULL WHERE id = %s RETURNING *""", (run_at, ex["id"]))
            else:
                cur.execute("""INSERT INTO scheduled_posts (id, history_id, platform, run_at) VALUES (%s, %s, %s, %s) RETURNING *""",
                            (str(uuid.uuid4()), history_id, platform, run_at))
            return _row(cur.fetchone())


def sched_list(history_id: str = None, include_past: bool = True, limit: int = 300):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            q = """SELECT s.*, h.title, h.on_screen_caption, h.posting_caption, h.video_filename
                   FROM scheduled_posts s LEFT JOIN history h ON h.id = s.history_id"""
            cond, args = [], []
            if history_id:
                cond.append("s.history_id = %s"); args.append(history_id)
            if not include_past:
                cond.append("s.status IN ('scheduled', 'publishing', 'missed', 'error')")
            if cond:
                q += " WHERE " + " AND ".join(cond)
            q += " ORDER BY s.run_at ASC LIMIT %s"
            args.append(limit)
            cur.execute(q, args)
            return [_row(r) for r in cur.fetchall()]


def sched_get(sid: str):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM scheduled_posts WHERE id = %s", (sid,))
            r = cur.fetchone()
    return _row(r) if r else None


def sched_set_time(sid: str, run_at):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""UPDATE scheduled_posts SET run_at = %s, status = 'scheduled', attempts = 0, started_at = NULL, result = NULL
                           WHERE id = %s AND status <> 'publishing' RETURNING *""", (run_at, sid))
            r = cur.fetchone()
    return _row(r) if r else None


def sched_delete(sid: str) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scheduled_posts WHERE id = %s AND status <> 'publishing'", (sid,))
            return cur.rowcount > 0


def sched_claim_due(missed_after_minutes: int = 180, limit: int = 12):
    """Atomically claims due posts for publishing (safe against two ticks
    running at once). Posts more than `missed_after_minutes` overdue (e.g. the
    server was down) are parked as 'missed' for the owner to decide, rather
    than going out hours late unannounced. Stuck 'publishing' rows (process
    died mid-post) are flagged as errors, never silently re-posted."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""UPDATE scheduled_posts SET status = 'error',
                           result = '{"error": "Interrupted while posting -- check the platform before retrying, it may have gone out."}'::jsonb
                           WHERE status = 'publishing' AND started_at < now() - interval '20 minutes'""")
            cur.execute("""UPDATE scheduled_posts SET status = 'missed',
                           result = '{"error": "The app was not running at the scheduled time, so this was not posted. Post it now or pick a new time."}'::jsonb
                           WHERE status = 'scheduled' AND run_at < now() - make_interval(mins => %s)""", (missed_after_minutes,))
            cur.execute("""UPDATE scheduled_posts SET status = 'publishing', attempts = attempts + 1, started_at = now()
                           WHERE id IN (SELECT id FROM scheduled_posts WHERE status = 'scheduled' AND run_at <= now()
                                        ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT %s) RETURNING *""", (limit,))
            return [_row(r) for r in cur.fetchall()]


def sched_finish(sid: str, status: str, result: dict, retry_in_minutes: int = None):
    with _conn() as conn:
        with conn.cursor() as cur:
            if retry_in_minutes:
                cur.execute("""UPDATE scheduled_posts SET status = 'scheduled', run_at = now() + make_interval(mins => %s),
                               result = %s WHERE id = %s""", (retry_in_minutes, json.dumps(result or {}), sid))
            else:
                cur.execute("UPDATE scheduled_posts SET status = %s, result = %s WHERE id = %s",
                            (status, json.dumps(result or {}), sid))


# --- Account profile drafts (bio, display name, link -- per platform) -------

def profiles_get() -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT platform, data, updated_at FROM account_profiles")
            return {r["platform"]: {**(r["data"] or {}), "updated_at": r["updated_at"].isoformat()} for r in cur.fetchall()}


def profile_save(platform: str, data: dict):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO account_profiles (platform, data, updated_at) VALUES (%s, %s, now())
                           ON CONFLICT (platform) DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
                        (platform, psycopg2.extras.Json(data)))
