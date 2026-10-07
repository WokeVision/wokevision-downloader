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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    id INT PRIMARY KEY DEFAULT 1,
                    data JSONB NOT NULL DEFAULT '{}'::jsonb
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sponsor TEXT DEFAULT '',
                    brief TEXT DEFAULT '',
                    hashtags TEXT DEFAULT '',
                    wm_token TEXT DEFAULT '',
                    wm_pos TEXT DEFAULT 'right',
                    share_token TEXT UNIQUE,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS campaign_id TEXT")
            cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS approval TEXT")
            cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS approval_note TEXT")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_usage (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ DEFAULT now(),
                    kind TEXT, model TEXT,
                    in_tokens BIGINT DEFAULT 0, out_tokens BIGINT DEFAULT 0, audio_seconds DOUBLE PRECISION DEFAULT 0,
                    cost_usd DOUBLE PRECISION DEFAULT 0
                )
            """)
            # Saved campaign watermarks (the image lives in storage as wm_<token>.png).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watermark_library (
                    token TEXT PRIMARY KEY,
                    name TEXT,
                    pos TEXT DEFAULT 'right',
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            # Editor jobs: just enough to tell the page what happened to a job
            # after a server restart (the live progress itself stays in memory).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT,
                    stage_label TEXT,
                    error TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """)
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
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("CREATE TABLE IF NOT EXISTS caption_versions (id BIGSERIAL PRIMARY KEY, history_id TEXT, ts TIMESTAMPTZ DEFAULT now(), posts JSONB)")
                cur.execute("SELECT platform_posts FROM history WHERE id = %s", (entry_id,))
                row = cur.fetchone()
                old = (row or {}).get("platform_posts")
                if old and old != (posts or {}):
                    cur.execute("INSERT INTO caption_versions (history_id, posts) VALUES (%s, %s)", (str(entry_id), json.dumps(old)))
                    cur.execute("DELETE FROM caption_versions WHERE history_id = %s AND id NOT IN (SELECT id FROM caption_versions WHERE history_id = %s ORDER BY id DESC LIMIT 20)", (str(entry_id), str(entry_id)))
            except Exception as e:
                print(f"VERSION SNAPSHOT FAILED: {e}", flush=True)
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


# --- App settings (brand voice, vocabulary, caption defaults) -------------------

def settings_get() -> dict:
    if not configured():
        return {}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT data FROM app_settings WHERE id = 1")
            row = cur.fetchone()
    return (row or {}).get("data") or {}


def settings_save(data: dict):
    if not configured():
        raise RuntimeError("Database isn't configured.")
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO app_settings (id, data) VALUES (1, %s)
                           ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data""", (json.dumps(data),))


# --- Watermark library -----------------------------------------------------------

def wm_lib_save(token: str, name: str, pos: str = "right"):
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO watermark_library (token, name, pos) VALUES (%s, %s, %s)
                           ON CONFLICT (token) DO UPDATE SET name = EXCLUDED.name, pos = EXCLUDED.pos""", (token, name, pos))


def wm_lib_list():
    if not configured():
        return []
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT token, name, pos FROM watermark_library ORDER BY created_at DESC LIMIT 100")
            return [dict(r) for r in cur.fetchall()]


def wm_lib_delete(token: str):
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watermark_library WHERE token = %s", (token,))


# --- Editor jobs (restart safety) ----------------------------------------------

def job_upsert(job_id: str, status: str, stage_label: str = None, error: str = None):
    if not configured():
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO jobs (id, status, stage_label, error, updated_at) VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, stage_label = EXCLUDED.stage_label,
                    error = EXCLUDED.error, updated_at = now()
            """, (job_id, status, stage_label, error))


def job_get(job_id: str):
    if not configured():
        return None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def jobs_mark_interrupted() -> int:
    """Called at startup: anything still 'running' died with the old process."""
    if not configured():
        return 0
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE jobs SET status = 'error', stage_label = 'Error', updated_at = now(),
                    error = 'The server restarted while this was processing. Please start it again.'
                WHERE status = 'running'
            """)
            n = cur.rowcount
            cur.execute("""
                UPDATE clip_sessions SET status = 'error',
                    error = 'The server restarted while this was running. Start it again.'
                WHERE status = 'running'
            """)
            return n


def attention_items():
    """Things that need a human: failed scheduled posts (last 7 days) and
    connections that are broken or about to expire."""
    out = []
    if not configured():
        return out
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.id, s.platform, s.run_at, s.result, h.on_screen_caption, s.history_id
                FROM scheduled_posts s LEFT JOIN history h ON h.id = s.history_id
                WHERE s.status = 'error' AND s.run_at > now() - interval '7 days'
                ORDER BY s.run_at DESC LIMIT 20
            """)
            for r in cur.fetchall():
                err = ((r.get("result") or {}).get("error") or "Failed")[:160]
                out.append({"kind": "post_failed", "platform": r["platform"], "history_id": str(r["history_id"]),
                            "text": f"Scheduled {r['platform']} post failed: {err}", "href": "/schedule"})
            cur.execute("""
                SELECT platform, expires_at, last_check_ok, last_error FROM connections
                WHERE last_check_ok = false OR (refresh_token IS NULL AND expires_at IS NOT NULL AND expires_at < now() + interval '5 days')
            """)
            for r in cur.fetchall():
                if r["last_check_ok"] is False:
                    out.append({"kind": "connection", "platform": r["platform"],
                                "text": f"{r['platform']} connection is failing: {(r.get('last_error') or '')[:120]}", "href": "/accounts#connections"})
                else:
                    out.append({"kind": "expiry", "platform": r["platform"],
                                "text": f"{r['platform']} login expires soon -- reconnect it", "href": "/accounts#connections"})
    return out


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


# --- Campaigns -------------------------------------------------------------------

_CAMP_COLS = ("name", "sponsor", "brief", "hashtags", "wm_token", "wm_pos")


def camp_list():
    if not configured():
        return []
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT c.*, (SELECT count(*) FROM history h WHERE h.campaign_id = c.id) AS posts
                           FROM campaigns c ORDER BY c.created_at DESC LIMIT 200""")
            return [dict(r) for r in cur.fetchall()]


def camp_get(cid: str = None, share_token: str = None):
    if not configured():
        return None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if share_token:
                cur.execute("SELECT * FROM campaigns WHERE share_token = %s", (share_token,))
            else:
                cur.execute("SELECT * FROM campaigns WHERE id = %s", (cid,))
            r = cur.fetchone()
    return dict(r) if r else None


def camp_save(cid: str, share_token: str, **f):
    f = {k: v for k, v in f.items() if k in _CAMP_COLS}
    with _conn() as conn:
        with conn.cursor() as cur:
            cols = ["id", "share_token"] + list(f)
            cur.execute(f"""INSERT INTO campaigns ({",".join(cols)}) VALUES ({",".join(["%s"] * len(cols))})
                            ON CONFLICT (id) DO UPDATE SET {",".join(f"{k} = EXCLUDED.{k}" for k in f)}""",
                        [cid, share_token] + list(f.values()))


def camp_delete(cid: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE history SET campaign_id = NULL WHERE campaign_id = %s", (cid,))
            cur.execute("DELETE FROM campaigns WHERE id = %s", (cid,))


def history_set_campaign(entry_id: str, cid):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE history SET campaign_id = %s, approval = NULL, approval_note = NULL WHERE id = %s", (cid or None, entry_id))


def camp_items(cid: str):
    """History entries in a campaign plus their scheduled/posted rows."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT id, created_at, title, on_screen_caption, posting_caption, video_filename, approval, approval_note
                           FROM history WHERE campaign_id = %s ORDER BY created_at ASC""", (cid,))
            items = [_titled(r) for r in cur.fetchall()]
            for it in items:
                cur.execute("SELECT id, platform, run_at, status, result FROM scheduled_posts WHERE history_id = %s ORDER BY run_at", (it["id"],))
                it["posts"] = [dict(r) for r in cur.fetchall()]
    return items


def history_set_approval(entry_id: str, cid: str, status: str, note: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE history SET approval = %s, approval_note = %s WHERE id = %s AND campaign_id = %s",
                        (status, note[:1000], entry_id, cid))
            return cur.rowcount


# --- AI spend tracking -----------------------------------------------------------------
# USD per 1M tokens (input, output); Whisper is per minute of audio. Estimates only.
_PRICES = {"gpt-4o": (2.50, 10.00), "gpt-4o-mini": (0.15, 0.60)}
_WHISPER_PER_MIN = 0.006


def record_ai_usage(kind: str, model: str, in_tokens: int = 0, out_tokens: int = 0, audio_seconds: float = 0.0):
    """Best-effort: never raises, never slows a request noticeably."""
    try:
        if not configured():
            return
        pin, pout = _PRICES.get(model, _PRICES["gpt-4o"])
        cost = in_tokens / 1e6 * pin + out_tokens / 1e6 * pout + audio_seconds / 60 * _WHISPER_PER_MIN
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ai_usage (kind, model, in_tokens, out_tokens, audio_seconds, cost_usd) VALUES (%s,%s,%s,%s,%s,%s)",
                            (kind, model, int(in_tokens or 0), int(out_tokens or 0), float(audio_seconds or 0), cost))
    except Exception as e:
        print(f"AI USAGE LOG FAILED: {e}", flush=True)


def ai_usage_summary():
    if not configured():
        return {}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT coalesce(sum(cost_usd),0) AS c, count(*) AS n FROM ai_usage WHERE ts >= date_trunc('month', now())""")
            month = dict(cur.fetchone())
            cur.execute("""SELECT coalesce(sum(cost_usd),0) AS c FROM ai_usage WHERE ts >= now() - interval '7 days'""")
            week = cur.fetchone()["c"]
            cur.execute("""SELECT kind, round(sum(cost_usd)::numeric, 3) AS cost, count(*) AS calls FROM ai_usage
                           WHERE ts >= now() - interval '30 days' GROUP BY kind ORDER BY sum(cost_usd) DESC""")
            kinds = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT count(*) AS n FROM history WHERE created_at >= date_trunc('month', now())")
            videos = cur.fetchone()["n"]
    return {"month_usd": round(float(month["c"]), 2), "week_usd": round(float(week), 2), "videos_month": videos,
            "per_video_usd": round(float(month["c"]) / videos, 3) if videos else None,
            "kinds": [{"kind": k["kind"], "cost": float(k["cost"]), "calls": k["calls"]} for k in kinds]}


def post_stats_by_url(urls):
    """{url: {views, likes, comments}} from the analytics cache."""
    if not urls:
        return {}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT url, views, likes, comments FROM post_stats WHERE url = ANY(%s)", (list(urls),))
            return {r["url"]: {"views": r["views"], "likes": r["likes"], "comments": r["comments"]} for r in cur.fetchall()}


def audit(event: str, detail: str = ""):
    """Best-effort security/event log (logins, failed logins, publishes)."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), event TEXT, detail TEXT)")
                cur.execute("INSERT INTO audit_log (event, detail) VALUES (%s, %s)", (event[:60], (detail or "")[:300]))
    except Exception as e:
        print(f"AUDIT FAILED: {e}", flush=True)


def audit_list(limit: int = 50):
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), event TEXT, detail TEXT)")
                cur.execute("SELECT ts, event, detail FROM audit_log ORDER BY id DESC LIMIT %s", (limit,))
                return [{"ts": r["ts"].isoformat(), "event": r["event"], "detail": r["detail"]} for r in cur.fetchall()]
    except Exception:
        return []


_IDEAS_DDL = "CREATE TABLE IF NOT EXISTS ideas (id BIGSERIAL PRIMARY KEY, url TEXT, note TEXT, created_at TIMESTAMPTZ DEFAULT now())"


def ideas_list():
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_IDEAS_DDL)
            cur.execute("SELECT id, url, note FROM ideas ORDER BY id DESC LIMIT 200")
            return [dict(r) for r in cur.fetchall()]


def ideas_add(url, note):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_IDEAS_DDL)
            cur.execute("INSERT INTO ideas (url, note) VALUES (%s, %s)", (url, note))


def ideas_delete(iid):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ideas WHERE id = %s", (iid,))


def caption_versions(entry_id: str):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS caption_versions (id BIGSERIAL PRIMARY KEY, history_id TEXT, ts TIMESTAMPTZ DEFAULT now(), posts JSONB)")
            cur.execute("SELECT id, ts, posts FROM caption_versions WHERE history_id = %s ORDER BY id DESC LIMIT 20", (str(entry_id),))
            return [{"id": r["id"], "ts": r["ts"].isoformat(), "posts": r["posts"]} for r in cur.fetchall()]


# --- Link-in-bio ------------------------------------------------------------------------
_BIO_DDL = (
    "CREATE TABLE IF NOT EXISTS bio_links (id BIGSERIAL PRIMARY KEY, label TEXT, url TEXT, sort INT DEFAULT 0, "
    "active BOOLEAN DEFAULT true, campaign_id TEXT, created_at TIMESTAMPTZ DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS bio_clicks (id BIGSERIAL PRIMARY KEY, link_id BIGINT, ts TIMESTAMPTZ DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS bio_clicks_link ON bio_clicks (link_id, ts)",
    "CREATE TABLE IF NOT EXISTS bio_page (id INT PRIMARY KEY, data JSONB)",
)


def _bio_cur(conn, **kw):
    cur = conn.cursor(**kw)
    for d in _BIO_DDL:
        cur.execute(d)
    return cur


def bio_page_get():
    with _conn() as conn:
        cur = _bio_cur(conn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT data FROM bio_page WHERE id = 1")
        row = cur.fetchone()
        return (row or {}).get("data") or {}


def bio_page_save(data: dict):
    with _conn() as conn:
        cur = _bio_cur(conn)
        cur.execute("INSERT INTO bio_page (id, data) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data", (json.dumps(data),))


def bio_links_list(only_active=False, with_stats=False):
    with _conn() as conn:
        cur = _bio_cur(conn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, label, url, sort, active, campaign_id FROM bio_links " + ("WHERE active " if only_active else "") + "ORDER BY sort, id")
        rows = [dict(r) for r in cur.fetchall()]
        if with_stats:
            cur.execute("SELECT link_id, count(*) AS total, count(*) FILTER (WHERE ts > now() - interval '7 days') AS week FROM bio_clicks GROUP BY link_id")
            st = {r["link_id"]: r for r in cur.fetchall()}
            for r in rows:
                r["clicks"] = int((st.get(r["id"]) or {}).get("total") or 0)
                r["clicks_7d"] = int((st.get(r["id"]) or {}).get("week") or 0)
        return rows


def bio_link_save(lid, label, url, active, campaign_id):
    with _conn() as conn:
        cur = _bio_cur(conn)
        if lid:
            cur.execute("UPDATE bio_links SET label=%s, url=%s, active=%s, campaign_id=%s WHERE id=%s", (label, url, active, campaign_id or None, lid))
        else:
            cur.execute("INSERT INTO bio_links (label, url, active, campaign_id, sort) VALUES (%s,%s,%s,%s,(SELECT coalesce(max(sort),0)+1 FROM bio_links))", (label, url, active, campaign_id or None))


def bio_link_delete(lid):
    with _conn() as conn:
        cur = _bio_cur(conn)
        cur.execute("DELETE FROM bio_links WHERE id=%s", (lid,))
        cur.execute("DELETE FROM bio_clicks WHERE link_id=%s", (lid,))


def bio_links_reorder(ids):
    with _conn() as conn:
        cur = _bio_cur(conn)
        for i, lid in enumerate(ids):
            cur.execute("UPDATE bio_links SET sort=%s WHERE id=%s", (i, int(lid)))


def bio_click(lid):
    """Records a click and returns the destination url (None if unknown/inactive)."""
    with _conn() as conn:
        cur = _bio_cur(conn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT url FROM bio_links WHERE id=%s AND active", (lid,))
        row = cur.fetchone()
        return (row or {}).get("url")


def bio_click_record(lid):
    with _conn() as conn:
        cur = _bio_cur(conn)
        cur.execute("INSERT INTO bio_clicks (link_id) VALUES (%s)", (lid,))


def bio_campaign_clicks(cid):
    with _conn() as conn:
        cur = _bio_cur(conn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT l.label, count(c.id) AS clicks FROM bio_links l LEFT JOIN bio_clicks c ON c.link_id = l.id
                       WHERE l.campaign_id = %s GROUP BY l.id, l.label ORDER BY l.id""", (cid,))
        return [dict(r) for r in cur.fetchall()]


# --- Customer requests (paid post orders + consultation bookings) -------------------------
_REQ_DDL = ("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, kind TEXT, status TEXT DEFAULT 'pending', "
            "created_at TIMESTAMPTZ DEFAULT now(), name TEXT, email TEXT, data JSONB DEFAULT '{}'::jsonb, "
            "run_at TIMESTAMPTZ, history_id TEXT, owner_note TEXT DEFAULT '')")


def _req_cur(conn, **kw):
    cur = conn.cursor(**kw)
    cur.execute(_REQ_DDL)
    return cur


def req_create(rid, kind, name, email, data, run_at=None):
    with _conn() as conn:
        cur = _req_cur(conn)
        cur.execute("INSERT INTO requests (id, kind, name, email, data, run_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (rid, kind, name, email, json.dumps(data or {}), run_at))


def _req_row(r):
    r = dict(r)
    r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
    r["run_at"] = r["run_at"].isoformat() if r.get("run_at") else None
    return r


def req_list(limit=200):
    with _conn() as conn:
        cur = _req_cur(conn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM requests WHERE status <> 'archived' ORDER BY (status = 'pending') DESC, created_at DESC LIMIT %s", (limit,))
        return [_req_row(r) for r in cur.fetchall()]


def req_list_removed(limit=200):
    with _conn() as conn:
        cur = _req_cur(conn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM requests WHERE status = 'archived' ORDER BY created_at DESC LIMIT %s", (limit,))
        return [_req_row(r) for r in cur.fetchall()]


def req_purge(rid):
    """Permanently deletes an archived request row."""
    with _conn() as conn:
        cur = _req_cur(conn)
        cur.execute("DELETE FROM requests WHERE id = %s AND status = 'archived'", (rid,))
        return cur.rowcount > 0


def req_get(rid):
    with _conn() as conn:
        cur = _req_cur(conn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM requests WHERE id = %s", (rid,))
        r = cur.fetchone()
        return _req_row(r) if r else None


def req_update(rid, status=None, data=None, run_at=None, history_id=None, owner_note=None):
    with _conn() as conn:
        cur = _req_cur(conn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT data FROM requests WHERE id = %s", (rid,))
        row = cur.fetchone()
        if not row:
            return False
        merged = {**(row["data"] or {}), **(data or {})}
        cur.execute("""UPDATE requests SET status = coalesce(%s, status), data = %s, run_at = coalesce(%s, run_at),
                       history_id = coalesce(%s, history_id), owner_note = coalesce(%s, owner_note) WHERE id = %s""",
                    (status, json.dumps(merged), run_at, history_id, owner_note, rid))
        return True


def req_pending_count():
    try:
        with _conn() as conn:
            cur = _req_cur(conn)
            cur.execute("SELECT count(*) FROM requests WHERE status = 'pending'")
            return int(cur.fetchone()[0])
    except Exception:
        return 0
