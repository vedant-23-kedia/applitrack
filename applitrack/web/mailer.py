"""Outgoing mail to candidates.

Deliberately separate from the Gmail integration. The app's Gmail scope is
read-only -- it cannot send as the user, and should not. These notifications
come from the service's own mailbox instead.

Configure with environment variables:

    APPLITRACK_SMTP_HOST       default smtp.gmail.com
    APPLITRACK_SMTP_PORT       default 587
    APPLITRACK_SMTP_USER
    APPLITRACK_SMTP_PASSWORD   a Gmail App Password, not the account password
    APPLITRACK_FROM_NAME       default "AppliTrack"

With nothing configured, `send` logs and returns False rather than raising,
so a sync is never lost just because notification is switched off.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("applitrack.mailer")


def config() -> dict:
    return {
        "host": os.environ.get("APPLITRACK_SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("APPLITRACK_SMTP_PORT", "587")),
        "user": os.environ.get("APPLITRACK_SMTP_USER", ""),
        "password": os.environ.get("APPLITRACK_SMTP_PASSWORD", ""),
        "from_name": os.environ.get("APPLITRACK_FROM_NAME", "AppliTrack"),
    }


def configured() -> bool:
    cfg = config()
    return bool(cfg["user"] and cfg["password"])


def send(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one HTML email. Returns whether it went out."""
    cfg = config()
    if not configured():
        log.warning("email not configured; would have sent %r to %s", subject, to)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["user"]))
    msg["To"] = to
    msg.set_content(text or _strip(html))
    msg.add_alternative(html, subtype="html")

    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as server:
                server.login(cfg["user"], cfg["password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
                server.starttls()
                server.login(cfg["user"], cfg["password"])
                server.send_message(msg)
        log.info("sent %r to %s", subject, to)
        return True
    except Exception as exc:
        log.error("failed to send to %s: %s", to, exc)
        return False


def _strip(html: str) -> str:
    """A plain-text fallback, for clients that will not render HTML."""
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</h\d>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    import html as html_mod

    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
