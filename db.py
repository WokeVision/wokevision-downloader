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
    conn = psycopg2.connect(DATABASE_URL)
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
