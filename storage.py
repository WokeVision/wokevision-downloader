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


# --- Direct browser -> bucket multipart uploads -------------------------------
# Big videos go from the browser straight to R2 in resumable 16MB parts, so
# the app server never carries the upload and a refresh/drop doesn't lose it.

PART_SIZE = 16 * 1024 * 1024
_cors_done = False


def _s3():
    """Client configured for SigV4 presigning against R2."""
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=ENDPOINT, aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY, region_name="auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def ensure_cors(origins) -> bool:
    """Lets the site's pages PUT parts to the bucket. Best-effort; if the
    API token isn't allowed to set CORS this returns False and the app falls
    back to uploading through the server."""
    global _cors_done
    if _cors_done:
        return True
    try:
        _s3().put_bucket_cors(Bucket=BUCKET, CORSConfiguration={"CORSRules": [{
            "AllowedOrigins": list(origins), "AllowedMethods": ["PUT", "GET", "HEAD"],
            "AllowedHeaders": ["*"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3600}]})
        _cors_done = True
    except Exception as e:
        print(f"R2 CORS SETUP FAILED: {e}", flush=True)
        return False
    return True


def multipart_start(key: str, size: int, resume_upload_id: str = None):
    """Returns {upload_id, part_size, urls:[...], done:[part numbers]}."""
    import math
    c = _s3()
    part_size = max(PART_SIZE, math.ceil(size / 9000))
    done = []
    if resume_upload_id:
        try:
            done = [p["PartNumber"] for p in list_parts(key, resume_upload_id)]
            upload_id = resume_upload_id
        except Exception:
            resume_upload_id = None
    if not resume_upload_id:
        upload_id = c.create_multipart_upload(Bucket=BUCKET, Key=key, ContentType="video/mp4")["UploadId"]
    n = max(1, math.ceil(size / part_size))
    urls = [c.generate_presigned_url("upload_part", Params={"Bucket": BUCKET, "Key": key, "UploadId": upload_id, "PartNumber": i},
                                     ExpiresIn=86400) for i in range(1, n + 1)]
    return {"upload_id": upload_id, "part_size": part_size, "urls": urls, "done": done}


def list_parts(key: str, upload_id: str):
    c = _s3()
    parts, marker = [], 0
    while True:
        r = c.list_parts(Bucket=BUCKET, Key=key, UploadId=upload_id, PartNumberMarker=marker, MaxParts=1000)
        parts += r.get("Parts", [])
        if not r.get("IsTruncated"):
            return parts
        marker = r.get("NextPartNumberMarker")


def multipart_complete(key: str, upload_id: str, expected_size: int = None) -> int:
    """Stitches whatever parts arrived; returns the object size."""
    c = _s3()
    parts = sorted(list_parts(key, upload_id), key=lambda p: p["PartNumber"])
    if not parts:
        raise RuntimeError("No parts were uploaded.")
    got = sum(p["Size"] for p in parts)
    if expected_size and got != expected_size:
        raise RuntimeError(f"Upload incomplete ({got} of {expected_size} bytes).")
    c.complete_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload_id,
                                MultipartUpload={"Parts": [{"ETag": p["ETag"], "PartNumber": p["PartNumber"]} for p in parts]})
    return got


def download_with_progress(key: str, local_path: str, progress_cb=None):
    size = None
    try:
        size = _get_client().head_object(Bucket=BUCKET, Key=key)["ContentLength"]
    except Exception:
        pass
    done = [0]
    def cb(n):
        done[0] += n
        if progress_cb and size:
            progress_cb(min(done[0] / size, 1.0))
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    _get_client().download_file(BUCKET, key, local_path, Callback=cb)


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
    if not configured():
        return False
    try:
        _get_client().put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)
        return True
    except Exception as e:
        print(f"STORAGE PUT FAILED ({key}): {e}", flush=True)
        return False


def get_bytes(key: str):
    if not configured():
        return None
    try:
        return _get_client().get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except Exception as e:
        print(f"STORAGE GET FAILED ({key}): {e}", flush=True)
        return None


def list_objects(prefix: str):
    """[{key, size, modified}] for keys under prefix, newest first."""
    out = []
    if not configured():
        return out
    try:
        for page in _get_client().get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
            for o in page.get("Contents", []):
                out.append({"key": o["Key"], "size": o["Size"], "modified": o["LastModified"].isoformat()})
    except Exception as e:
        print(f"STORAGE LIST FAILED ({prefix}): {e}", flush=True)
    return sorted(out, key=lambda x: x["modified"], reverse=True)


def delete_key(key: str):
    try:
        _get_client().delete_object(Bucket=BUCKET, Key=key)
    except Exception as e:
        print(f"STORAGE DELETE FAILED ({key}): {e}", flush=True)
