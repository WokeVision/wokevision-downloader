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


def email_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))


def send_email(to: str, subject: str, body: str, reply_to: str = None) -> bool:
    """Sends a plain-text email through SMTP (SMTP_HOST/PORT/USER/PASS, SMTP_FROM optional).
    Returns False (never raises) when SMTP isn't set up or sending fails."""
    if not email_configured() or not to:
        return False
    import smtplib
    from email.message import EmailMessage
    try:
        msg = EmailMessage()
        msg["From"] = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=20) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"EMAIL FAILED: {e}", flush=True)
        return False


def send_invite(to: str, name: str, title: str, start, minutes: int, link: str, organizer: str, note: str = "") -> bool:
    """Emails a calendar invite (iCalendar METHOD:REQUEST) with a video link, so Gmail/Outlook/
    Apple Mail show it as an invitation with Yes/No buttons. `start` is a UTC datetime."""
    if not email_configured() or not to:
        return False
    import smtplib, uuid, datetime
    from email.message import EmailMessage
    sender = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]
    end = start + datetime.timedelta(minutes=minutes)
    fmt = lambda d: d.strftime("%Y%m%dT%H%M%SZ")
    esc = lambda t: str(t).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    desc = (note + "\n\n" if note else "") + f"Join the video call: {link}"
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//WokeVision//Invite//EN", "CALSCALE:GREGORIAN", "METHOD:REQUEST",
        "BEGIN:VEVENT", f"UID:{uuid.uuid4()}@wokevision", f"DTSTAMP:{fmt(datetime.datetime.now(datetime.timezone.utc))}",
        f"DTSTART:{fmt(start)}", f"DTEND:{fmt(end)}", f"SUMMARY:{esc(title)}", f"DESCRIPTION:{esc(desc)}",
        f"LOCATION:{esc(link)}", f"ORGANIZER;CN=WokeVision:mailto:{sender}",
        f"ATTENDEE;CN={esc(name)};ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{to}",
        "STATUS:CONFIRMED", "SEQUENCE:0", "END:VEVENT", "END:VCALENDAR", ""])
    try:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = f"Invitation: {title}"
        if organizer:
            msg["Reply-To"] = organizer
        when = start.strftime("%A %d %B %Y, %H:%M UTC")
        body = (f"Hi {name},\n\nYou're booked in: {when} ({minutes} minutes).\n\nJoin the video call: {link}\n\n"
                + (note + "\n\n" if note else "") + "A calendar invite is attached — accept it and it goes straight into your calendar.\n\nSee you then.")
        msg.set_content(body)
        msg.add_alternative(ics, subtype="calendar", params={"method": "REQUEST"})
        msg.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar", filename="invite.ics", params={"method": "REQUEST"})
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"INVITE EMAIL FAILED: {e}", flush=True)
        return False
