"""Sync one person, end to end.

    Gmail -> classify -> group -> their own Google Sheet -> tell them

Two modes. A **full** sync reads the last year and is used the first time
someone connects, or whenever the incremental cursor goes stale. An
**incremental** sync reads only the handful of messages a push notification
points at, then reloads the already-classified ones from the database so the
applications can be rebuilt from the complete set.

Everything is created with the person's own credentials, so the spreadsheet
lands in their Drive, owned by them. If they delete their account here, the
sheet stays with them.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..classify import classify
from ..gmail_client import GmailClient
from ..models import Application, Signal
from ..pipeline import build_applications, summarise
from ..store import Store
from . import oauth, push

log = logging.getLogger("applitrack.service")

# One sync per user at a time -- and that has to hold across *processes*, not
# just threads. The web server (with its scheduler) and a `cli sync` run in
# separate processes; a threading.Lock does nothing between them, and two
# concurrent syncs of one mailbox double the Gmail request rate, which is
# enough on its own to trip the per-second quota. So: a lock file as well.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

LOCK_DIR = Path(os.environ.get("APPLITRACK_LOCK_DIR", "data/locks"))
LOCK_STALE_SECONDS = 1800          # a sync that outlives this is assumed dead


def _lock_for(user_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(user_id, threading.Lock())


class ProcessLock:
    """A lock file, so two processes cannot sync one mailbox at once.

    Created atomically with O_EXCL. A lock older than `LOCK_STALE_SECONDS` is
    treated as abandoned -- otherwise a crashed sync would block that user
    forever.
    """

    def __init__(self, user_id: str):
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOCK_DIR / f"sync-{user_id}.lock"
        self.held = False

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._is_stale():
                log.warning("clearing stale lock %s", self.path)
                try:
                    self.path.unlink()
                except OSError:
                    return False
                return self.acquire()
            return False
        except OSError:
            return False

        try:
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
        finally:
            os.close(fd)
        self.held = True
        return True

    def _is_stale(self) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > LOCK_STALE_SECONDS
        except OSError:
            return False

    def release(self) -> None:
        if not self.held:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.held = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


@dataclass
class SyncResult:
    ok: bool = True
    mode: str = "full"
    fetched: int = 0
    kept: int = 0
    applications: int = 0
    changes: list[dict] = field(default_factory=list)
    sheet_url: str = ""
    error: str = ""
    skipped: bool = False

    @property
    def summary_line(self) -> str:
        if self.skipped:
            return "already syncing"
        if not self.ok:
            return f"failed: {self.error}"
        return (
            f"{self.mode}: {self.fetched} new email(s), "
            f"{self.applications} applications, {len(self.changes)} change(s)"
        )


def sync_user(
    user,
    user_store,
    db_path: str = "data/applitrack.db",
    force_full: bool = False,
    notify: bool = True,
) -> SyncResult:
    """Refresh one person's tracker. Safe to call concurrently."""
    lock = _lock_for(user.user_id)
    if not lock.acquire(blocking=False):
        return SyncResult(skipped=True, ok=True)

    process_lock = ProcessLock(user.user_id)
    try:
        if not process_lock.acquire():
            log.info("%s is already being synced by another process", user.email)
            return SyncResult(skipped=True, ok=True)
        try:
            return _sync_locked(user, user_store, db_path, force_full, notify)
        except Exception as exc:
            user_store.record_error(user.user_id, str(exc))
            return SyncResult(ok=False, error=str(exc))
        finally:
            process_lock.release()
    finally:
        lock.release()


def _sync_locked(user, user_store, db_path, force_full, notify) -> SyncResult:
    from googleapiclient.discovery import build

    creds = oauth.load_credentials(user_store, user.user_id)
    if creds is None:
        user_store.update(user.user_id, state="revoked", sync_enabled=False)
        return SyncResult(ok=False, error="access revoked - the user must reconnect")

    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    client = GmailClient(service=gmail)

    store = Store(db_path, user_id=user.user_id)
    try:
        result = SyncResult()
        incremental = bool(user.last_history_id) and not force_full

        if incremental:
            ids, latest = push.history_since(gmail, user.last_history_id)
            if ids is None:
                incremental = False          # cursor too old
            else:
                result.mode = "incremental"
                new_ids = [i for i in ids if i not in store.seen_message_ids()]
                emails = client.fetch(new_ids) if new_ids else []
                store.mark_fetched([e.msg_id for e in emails])
                result.fetched = len(emails)
                history_cursor = latest

        if not incremental:
            result.mode = "full"
            # Resumable: skip anything a previous run already downloaded, so a
            # sync cut short by a rate limit is picked up where it stopped
            # rather than paying for the whole mailbox again.
            already = store.fetched_ids()
            ids = client.search_ids(
                client.full_query(user.lookback_days), max_results=1500
            )
            todo = [i for i in ids if i not in already]
            result.mode = "full" if not already else "resume"
            log.info(
                "%s: %d message(s) match, %d already fetched, %d to download",
                user.email, len(ids), len(ids) - len(todo), len(todo),
            )
            emails = client.fetch(todo) if todo else []
            store.mark_fetched([e.msg_id for e in emails])
            result.fetched = len(emails)
            history_cursor = push.current_history_id(gmail)

        # Judge the new mail, then put it back together with everything
        # already judged -- grouping needs the whole history, not a slice.
        fresh: list[Signal] = []
        for email in emails:
            signal = classify(email)
            if signal:
                fresh.append(signal)
                store.record_signal(signal)
        result.kept = len(fresh)

        # Always rebuild from everything on record: a full sync may now be
        # a resumed one, holding only part of the mailbox.
        all_signals = store.load_signals()
        if incremental and not fresh:
            # Nothing new was application mail, but statuses still age into
            # Ghosted, so the rebuild is still worth doing.
            pass

        apps = build_applications(all_signals)
        result.applications = len(apps)
        result.changes = store.replace_applications(apps)
        store.commit()

        # Keep the sheet and the push subscription current.
        sheet_url = write_user_sheet(creds, user, user_store, apps)
        result.sheet_url = sheet_url

        push.ensure_watch(gmail, user, user_store)
        user_store.touch_sync(user.user_id, history_cursor)

        if notify and result.changes and user.notify_email:
            notify_user(user, store, apps, result.changes)

        return result
    finally:
        store.close()


def write_user_sheet(creds, user, user_store, apps: list[Application]) -> str:
    """Create or update the person's spreadsheet, in their own Drive."""
    from googleapiclient.discovery import build

    from ..sheets import SheetsWriter

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    writer = SheetsWriter(service=service, spreadsheet_id=user.spreadsheet_id or None)

    if not user.spreadsheet_id:
        writer.create(f"Job Application Tracker - {user.first_name}")
        user_store.update(user.user_id, spreadsheet_id=writer.spreadsheet_id)
        user.spreadsheet_id = writer.spreadsheet_id

    try:
        return writer.write(apps, summarise(apps))
    except Exception as exc:
        # Most often: the person deleted the sheet. Make a fresh one.
        if "404" in str(exc) or "not found" in str(exc).lower():
            writer.spreadsheet_id = None
            writer.create(f"Job Application Tracker - {user.first_name}")
            user_store.update(user.user_id, spreadsheet_id=writer.spreadsheet_id)
            user.spreadsheet_id = writer.spreadsheet_id
            return writer.write(apps, summarise(apps))
        raise


def notify_user(user, store, apps: list[Application], changes: list[dict]) -> bool:
    """Email the candidate about what just changed."""
    from ..digest import build_digest, render_html
    from .mailer import send

    pending = store.unnotified_changes()
    digest = build_digest(apps, summarise(apps), changes)
    html = render_html(digest)

    headline = _headline(changes)
    sent = send(
        to=user.email,
        subject=headline,
        html=_wrap_update(html, user, changes),
    )
    if sent:
        store.mark_notified([r["id"] for r in pending])
        store.commit()
    return sent


def _headline(changes: list[dict]) -> str:
    if len(changes) == 1:
        c = changes[0]
        return f"{c['company']}: {c['new']}"
    leading = ", ".join(c["company"] for c in changes[:3])
    return f"{len(changes)} application updates - {leading}"


def _wrap_update(html: str, user, changes: list[dict]) -> str:
    """Put the headline and the sheet link above the digest."""
    rows = "".join(
        f"<tr><td style='padding:6px 10px'>{c['company']}</td>"
        f"<td style='padding:6px 10px'>{c['role']}</td>"
        f"<td style='padding:6px 10px;color:#6b7280'>{c['old']}</td>"
        f"<td style='padding:6px 10px'><b>{c['new']}</b></td></tr>"
        for c in changes
    )
    banner = (
        f"<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        f"max-width:760px;margin:0 auto;padding:24px 24px 0\">"
        f"<p>Hi {user.first_name},</p>"
        f"<p>Your job tracker just changed:</p>"
        f"<table style='border-collapse:collapse;font-size:14px'>{rows}</table>"
        + (f"<p style='margin-top:18px'><a href='{user.sheet_url}' "
           f"style='background:#1a73e8;color:#fff;padding:10px 18px;"
           f"border-radius:6px;text-decoration:none;display:inline-block'>"
           f"Open your sheet</a></p>" if user.sheet_url else "")
        + "</div>"
    )
    return html.replace("<body>", "<body>" + banner, 1)


def send_welcome(user, apps: list[Application]) -> bool:
    """First email after someone connects: here is your sheet."""
    from .mailer import send

    open_count = sum(1 for a in apps if a.status.is_open)
    action = [a for a in apps if a.status.value in ("Assessment", "Interview", "Action Required")]

    action_block = ""
    if action:
        items = "".join(
            f"<li><b>{a.company}</b> &mdash; {a.role} ({a.status.value})</li>"
            for a in action
        )
        action_block = (
            "<p><b>These need you right now:</b></p>"
            f"<ul style='line-height:1.7'>{items}</ul>"
        )

    html = f"""<!doctype html><html><body style="font-family:-apple-system,Segoe UI,
Roboto,sans-serif;color:#1c2024;max-width:620px;margin:0 auto;padding:28px;line-height:1.6">
<h2 style="margin:0 0 6px">Your job tracker is ready</h2>
<p>Hi {user.first_name},</p>
<p>I read your inbox and found <b>{len(apps)} job applications</b>,
{open_count} of them still open. They are all in a spreadsheet in your own
Google Drive &mdash; you own it, and it updates itself whenever a company emails
you.</p>
{action_block}
<p style="margin:26px 0">
  <a href="{user.sheet_url}" style="background:#1a73e8;color:#fff;padding:12px 22px;
     border-radius:6px;text-decoration:none;display:inline-block;font-weight:600">
     Open your tracker</a>
</p>
<p style="font-size:13px;color:#6b7280;border-top:1px solid #e5e7eb;padding-top:16px">
This service has read-only access to your Gmail &mdash; it cannot send, delete or
change anything in your mailbox. You can disconnect it at any time, which
deletes everything stored about you here. The spreadsheet stays yours.</p>
</body></html>"""

    return send(to=user.email, subject="Your job application tracker is ready", html=html)


def sync_all(user_store, db_path: str = "data/applitrack.db") -> dict:
    """Every active user, one after another. Used by the scheduler."""
    results = {}
    for user in user_store.all_active():
        results[user.email] = sync_user(user, user_store, db_path)
    return results
