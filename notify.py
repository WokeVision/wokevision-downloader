"""Optional phone/desktop notifications. Configure either (or both):
  NTFY_TOPIC            -> free push via the ntfy app (https://ntfy.sh); pick a long random topic name
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID -> Telegram bot message
Does nothing (silently) when neither is set. Always best-effort, never raises."""
import os
import threading
import requests


def configured() -> bool:
    return bool(os.environ.get("NTFY_TOPIC") or (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")))


def _send(title: str, message: str, url: str = None):
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            headers = {"Title": title.encode("utf-8")}
            if url:
                headers["Click"] = url
            r = requests.post(f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{topic}",
                              data=message.encode("utf-8"), headers=headers, timeout=10)
            print(f"NTFY sent: HTTP {r.status_code}", flush=True)
        except Exception as e:
            print(f"NTFY FAILED: {e}", flush=True)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            text = f"{title}\n{message}" + (f"\n{url}" if url else "")
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=10)
        except Exception as e:
            print(f"TELEGRAM FAILED: {e}", flush=True)


def send_sync(title: str, message: str) -> dict:
    """Used by the Settings test button: reports what ntfy/Telegram actually answered."""
    res = []
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            r = requests.post(f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{topic}",
                              data=message.encode("utf-8"), headers={"Title": title}, timeout=10)
            res.append(f"ntfy topic '{topic}': HTTP {r.status_code}")
            ok = r.status_code < 300
        except Exception as e:
            res.append(f"ntfy failed: {e}"); ok = False
    else:
        ok = True
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": f"{title}\n{message}"}, timeout=10)
            res.append(f"telegram: HTTP {r.status_code}"); ok = ok and r.status_code < 300
        except Exception as e:
            res.append(f"telegram failed: {e}"); ok = False
    return {"ok": ok, "detail": "; ".join(res)}


def notify(title: str, message: str, url: str = None):
    if not configured():
        return
    threading.Thread(target=_send, args=(title, message, url), daemon=True).start()
