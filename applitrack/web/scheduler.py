"""Background work: the polling fallback and the daily digest.

Push notifications handle the real-time case, but they are optional and they
do fail -- a watch lapses, Pub/Sub drops a message, someone never set it up.
The poller is the safety net: it re-syncs everyone on an interval, which is
cheap because each pass is incremental.

A second loop sends the daily digest to anyone who wants one, so people hear
from the service even on days when nothing changed status.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("applitrack.scheduler")


class Scheduler:
    def __init__(
        self,
        user_store,
        db_path: str = "data/applitrack.db",
        poll_minutes: int = 15,
        digest_hour: int = 8,
        send_daily_digest: bool = False,
    ):
        self.user_store = user_store
        self.db_path = db_path
        self.poll_seconds = max(60, poll_minutes * 60)
        self.digest_hour = digest_hour
        self.send_daily_digest = send_daily_digest
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_digest_date = None

    def start(self):
        if self._threads:
            return
        for target, name in ((self._poll_loop, "poller"), (self._digest_loop, "digest")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("scheduler started (polling every %ds)", self.poll_seconds)

    def stop(self):
        self._stop.set()

    # -- loops -------------------------------------------------------------

    def _poll_loop(self):
        # Let the web server finish starting before the first pass.
        if self._stop.wait(30):
            return
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("poll pass failed")
            self._stop.wait(self.poll_seconds)

    def poll_once(self) -> int:
        from .service import sync_user

        users = self.user_store.all_active()
        synced = 0
        for user in users:
            if self._stop.is_set():
                break
            result = sync_user(user, self.user_store, self.db_path)
            if result.skipped:
                continue
            synced += 1
            if not result.ok:
                log.warning("sync failed for %s: %s", user.email, result.error)
            elif result.changes:
                log.info("%s: %d change(s)", user.email, len(result.changes))
        return synced

    def _digest_loop(self):
        while not self._stop.is_set():
            try:
                if self.send_daily_digest:
                    self._maybe_send_digests()
            except Exception:
                log.exception("digest pass failed")
            self._stop.wait(600)          # check every 10 minutes

    def _maybe_send_digests(self):
        now = datetime.now(timezone.utc)
        if now.hour != self.digest_hour:
            return
        if self._last_digest_date == now.date():
            return
        self._last_digest_date = now.date()
        self.send_digests()

    def send_digests(self) -> int:
        """One digest per user who has anything worth reporting."""
        from ..digest import build_digest, render_html
        from ..pipeline import summarise
        from ..store import Store
        from .mailer import send

        sent = 0
        since = datetime.now(timezone.utc) - timedelta(days=1)

        for user in self.user_store.all_active():
            if not user.notify_email:
                continue
            store = Store(self.db_path, user_id=user.user_id)
            try:
                apps = store.all_applications()
                if not apps:
                    continue
                summary = summarise(apps)
                changes = [
                    {"company": r["company"] or "?", "role": r["role"] or "",
                     "old": r["old_status"], "new": r["new_status"]}
                    for r in store.recent_changes(since)
                ]
                # Nothing changed and nothing is waiting on them: stay quiet.
                if not changes and not summary["needs_action"]:
                    continue
                digest = build_digest(apps, summary, changes)
                if send(
                    to=user.email,
                    subject=f"Your job applications - {datetime.now():%d %b}",
                    html=render_html(digest),
                ):
                    sent += 1
            finally:
                store.close()
        return sent
