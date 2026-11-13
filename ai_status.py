"""Remembers whether the last OpenAI calls worked, so the editor can show a
clear banner (e.g. "out of credit") instead of quietly producing generic text."""
import time

_state = {"ok": True, "kind": "", "message": "", "at": 0.0}


def classify(status_code: int = 0, body: str = "") -> tuple:
    b = (body or "").lower()
    if "insufficient_quota" in b or "exceeded your current quota" in b or "billing" in b:
        return "quota", "OpenAI has no credit left. Top up at platform.openai.com/settings/organization/billing and turn on auto-recharge, then captions and transcripts will work again."
    if status_code == 401 or "invalid_api_key" in b:
        return "key", "The OpenAI API key was rejected. Check OPENAI_API_KEY in Render."
    if status_code == 429:
        return "rate", "OpenAI is rate-limiting requests right now. Try again in a minute."
    return "error", f"OpenAI request failed (HTTP {status_code})." if status_code else "OpenAI request failed."


def record_ok():
    _state.update(ok=True, kind="", message="", at=time.time())


def record_fail(status_code: int = 0, body: str = ""):
    kind, msg = classify(status_code, body)
    _state.update(ok=False, kind=kind, message=msg, at=time.time())


def get() -> dict:
    return dict(_state)
