"""Durable video storage on any S3-compatible bucket (built for Cloudflare R2).

Render's free tier wipes local disk on every deploy/restart, so rendered
videos (and the kept source / staged files that make caption edits fast)
used to vanish -- History then showed "video expired" even for edits made
minutes earlier. The local /tmp/downloads folder stays the fast working
copy; this module mirrors each file to the bucket and pulls it back on
demand when the local copy is gone.

Entirely optional: with the env vars below unset, configured() is False and
the app behaves exactly as before (local disk only).

  R2_ENDPOINT_URL        e.g. https://<accountid>.r2.cloudflarestorage.com
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_BUCKET
"""
import os
import threading

ENDPOINT = os.environ.get("R2_ENDPOINT_URL")
ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID")
SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
BUCKET = os.environ.get("R2_BUCKET")

_client = None
_client_lock = threading.Lock()


def configured() -> bool:
    return bool(ENDPOINT and ACCESS_KEY and SECRET_KEY and BUCKET)


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            import boto3
            _client = boto3.client(
                "s3",
                endpoint_url=ENDPOINT,
                aws_access_key_id=ACCESS_KEY,
                aws_secret_access_key=SECRET_KEY,
                region_name="auto",
            )
        return _client


def upload_file(local_path: str, key: str) -> bool:
    """Best-effort mirror to the bucket. Never raises -- storage trouble
    must not break the editor, it just means that file won't survive a
    redeploy."""
    if not configured() or not os.path.exists(local_path):
        return False
    try:
        _get_client().upload_file(local_path, BUCKET, key)
        return True
    except Exception as e:
        print(f"STORAGE UPLOAD FAILED ({key}): {e}", flush=True)
        return False


def upload_many_async(pairs):
    """pairs: [(local_path, key), ...] uploaded in a background thread."""
    if not configured():
        return
    def _run():
        for path, key in pairs:
            upload_file(path, key)
    threading.Thread(target=_run, daemon=True).start()


def fetch_to(local_path: str, key: str) -> bool:
    """Downloads key to local_path if it isn't already there. True when the
    file exists locally afterwards."""
    if os.path.exists(local_path):
        return True
    if not configured():
        return False
    tmp = f"{local_path}.part"
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        _get_client().download_file(BUCKET, key, tmp)
        os.replace(tmp, local_path)
        return True
    except Exception as e:
        # Missing key is the normal "really expired/never stored" case.
        if "404" not in str(e) and "Not Found" not in str(e):
            print(f"STORAGE FETCH FAILED ({key}): {e}", flush=True)
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


def list_keys() -> set:
    """All keys in the bucket (one paginated listing)."""
    if not configured():
        return set()
    keys = set()
    try:
        paginator = _get_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET):
            for obj in page.get("Contents", []):
                keys.add(obj["Key"])
    except Exception as e:
        print(f"STORAGE LIST FAILED: {e}", flush=True)
    return keys
