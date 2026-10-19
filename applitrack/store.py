"""SQLite persistence, scoped per user.

Every row carries a `user_id` so one database can hold many people's
trackers. The CLI uses the reserved id `local`; the web service uses the
Google account id.

Classified emails are stored with enough detail (company, role, job id) to
rebuild a `Signal` without the original body. That is what makes incremental
sync possible: a push notification brings in three new emails, we reload the
four hundred we already judged, and rebuild the applications from all of it.

All of this is derived data -- it can be rebuilt from Gmail at any time -- so
a schema change simply recreates the tables rather than migrating them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Application, Email, Signal, Status

SCHEMA_VERSION = "3"

LOCAL_USER = "local"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    user_id     TEXT NOT NULL,
    msg_id      TEXT NOT NULL,
    thread_id   TEXT,
    app_key     TEXT,
    date        TEXT,
    sender      TEXT,
    sender_name TEXT,
    subject     TEXT,
    company     TEXT,
    role        TEXT,
    job_id      TEXT,
    source      TEXT,
    status      TEXT,
    confidence  REAL,
    reasons     TEXT,
    seen_at     TEXT,
    PRIMARY KEY (user_id, msg_id)
);

CREATE TABLE IF NOT EXISTS applications (
    user_id      TEXT NOT NULL,
    key          TEXT NOT NULL,
    company      TEXT,
    role         TEXT,
    job_id       TEXT,
    source       TEXT,
    status       TEXT,
    applied_on   TEXT,
    last_update  TEXT,
    last_subject TEXT,
    thread_url   TEXT,
    email_count  INTEGER,
    confidence   REAL,
    notes        TEXT,
    updated_at   TEXT,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS status_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    app_key     TEXT,
    company     TEXT,
    role        TEXT,
    old_status  TEXT,
    new_status  TEXT,
    changed_at  TEXT,
    notified_at TEXT
);

-- Every message id we have already downloaded, whether or not it turned
-- out to be application mail. This is what makes a full sync resumable:
-- if a run dies halfway through a rate limit, the next one picks up the
-- remainder instead of paying for the whole mailbox again.
CREATE TABLE IF NOT EXISTS fetched (
    user_id    TEXT NOT NULL,
    msg_id     TEXT NOT NULL,
    fetched_at TEXT,
    PRIMARY KEY (user_id, msg_id)
);

CREATE TABLE IF NOT EXISTS meta (
    user_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (user_id, key)
);

CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
CREATE INDEX IF NOT EXISTS idx_apps_user ON applications(user_id);
CREATE INDEX IF NOT EXISTS idx_changes_user ON status_changes(user_id, changed_at);
"""


class Store:
    def __init__(self, path: str = "data/applitrack.db", user_id: str = LOCAL_USER):
        self.path = Path(path)
        self.user_id = user_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        found = self._schema_version()
        if found is not None and found != SCHEMA_VERSION:
            # Derived data only -- rebuilding is cheaper and safer than migrating.
            for table in ("messages", "applications", "status_changes", "fetched", "meta"):
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (user_id, key, value) VALUES ('_schema','version',?)",
            (SCHEMA_VERSION,),
        )
        self.conn.commit()

    def _schema_version(self) -> str | None:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE user_id='_schema' AND key='version'"
            ).fetchone()
            return row["value"] if row else "1"   # table exists but unversioned
        except sqlite3.OperationalError:
            return None                            # fresh database

    def for_user(self, user_id: str) -> "Store":
        """A view of the same connection scoped to another user."""
        clone = object.__new__(Store)
        clone.path = self.path
        clone.conn = self.conn
        clone.user_id = user_id
        return clone

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- messages ----------------------------------------------------------

    def seen_message_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT msg_id FROM messages WHERE user_id = ?", (self.user_id,)
        ).fetchall()
        return {r["msg_id"] for r in rows}

    def fetched_ids(self) -> set[str]:
        """Message ids already downloaded for this user."""
        rows = self.conn.execute(
            "SELECT msg_id FROM fetched WHERE user_id = ?", (self.user_id,)
        ).fetchall()
        return {r["msg_id"] for r in rows}

    def mark_fetched(self, msg_ids: list[str]) -> None:
        if not msg_ids:
            return
        now = _now()
        self.conn.executemany(
            "INSERT OR REPLACE INTO fetched (user_id, msg_id, fetched_at) VALUES (?,?,?)",
            [(self.user_id, mid, now) for mid in msg_ids],
        )

    def forget_fetched(self) -> None:
        """Force the next full sync to re-read everything (e.g. rules changed)."""
        self.conn.execute("DELETE FROM fetched WHERE user_id = ?", (self.user_id,))

    def record_signal(self, signal: Signal) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO messages
               (user_id, msg_id, thread_id, app_key, date, sender, sender_name,
                subject, company, role, job_id, source, status, confidence,
                reasons, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.user_id,
                signal.email.msg_id,
                signal.email.thread_id,
                signal.key,
                signal.email.date.isoformat(),
                signal.email.sender,
                signal.email.sender_name,
                signal.email.subject,
                signal.company,
                signal.role,
                signal.job_id,
                signal.source,
                signal.status.value,
                signal.confidence,
                json.dumps(signal.reasons),
                _now(),
            ),
        )

    def load_signals(self) -> list[Signal]:
        """Rebuild previously classified emails, so a partial sync can still
        reconstruct the full picture. Bodies are not kept -- they are not
        needed once the email has been judged."""
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE user_id = ? ORDER BY date", (self.user_id,)
        ).fetchall()

        signals = []
        for r in rows:
            try:
                status = Status(r["status"])
            except ValueError:
                continue
            email = Email(
                msg_id=r["msg_id"],
                thread_id=r["thread_id"] or r["msg_id"],
                date=datetime.fromisoformat(r["date"]),
                sender=r["sender"] or "",
                sender_name=r["sender_name"] or "",
                subject=r["subject"] or "",
                body="",
            )
            signals.append(Signal(
                email=email,
                status=status,
                confidence=r["confidence"] or 0.5,
                company=r["company"] or "Unknown",
                role=r["role"] or "Unspecified",
                job_id=r["job_id"] or "",
                source=r["source"] or "",
                reasons=json.loads(r["reasons"] or "[]"),
            ))
        return signals

    # -- applications ------------------------------------------------------

    def get_application(self, key: str) -> Application | None:
        row = self.conn.execute(
            "SELECT * FROM applications WHERE user_id = ? AND key = ?",
            (self.user_id, key),
        ).fetchone()
        return _row_to_app(row) if row else None

    def all_applications(self) -> list[Application]:
        rows = self.conn.execute(
            "SELECT * FROM applications WHERE user_id = ? ORDER BY last_update DESC",
            (self.user_id,),
        ).fetchall()
        return [_row_to_app(r) for r in rows]

    def replace_applications(self, apps: list[Application]) -> list[dict]:
        """Write the current application set, returning the status changes.

        Applications that disappear from the rebuild are removed, so a
        correction in the rules does not leave a stale row behind forever.
        """
        before = {a.key: a for a in self.all_applications()}
        changes: list[dict] = []

        for app in apps:
            previous = before.get(app.key)
            if previous and previous.status != app.status:
                changes.append({
                    "key": app.key, "company": app.company, "role": app.role,
                    "old": previous.status.value, "new": app.status.value,
                })
                self.conn.execute(
                    """INSERT INTO status_changes
                       (user_id, app_key, company, role, old_status, new_status, changed_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (self.user_id, app.key, app.company, app.role,
                     previous.status.value, app.status.value, _now()),
                )
            self._write_application(app)

        current = {a.key for a in apps}
        for stale in set(before) - current:
            self.conn.execute(
                "DELETE FROM applications WHERE user_id = ? AND key = ?",
                (self.user_id, stale),
            )
        return changes

    def _write_application(self, app: Application) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO applications
               (user_id, key, company, role, job_id, source, status, applied_on,
                last_update, last_subject, thread_url, email_count,
                confidence, notes, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.user_id, app.key, app.company, app.role, app.job_id, app.source,
                app.status.value,
                app.applied_on.isoformat() if app.applied_on else None,
                app.last_update.isoformat(), app.last_subject, app.thread_url,
                app.email_count, app.confidence, app.notes, _now(),
            ),
        )

    # Kept for the single-user CLI path.
    def upsert_application(self, app: Application) -> str | None:
        existing = self.get_application(app.key)
        previous = existing.status.value if existing else None
        self._write_application(app)
        if previous and previous != app.status.value:
            self.conn.execute(
                """INSERT INTO status_changes
                   (user_id, app_key, company, role, old_status, new_status, changed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (self.user_id, app.key, app.company, app.role,
                 previous, app.status.value, _now()),
            )
            return previous
        return None

    def recent_changes(self, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM status_changes
               WHERE user_id = ? AND changed_at >= ?
               ORDER BY changed_at DESC""",
            (self.user_id, since.isoformat()),
        ).fetchall()

    def unnotified_changes(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM status_changes
               WHERE user_id = ? AND notified_at IS NULL
               ORDER BY changed_at DESC""",
            (self.user_id,),
        ).fetchall()

    def mark_notified(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE status_changes SET notified_at = ? WHERE id IN ({placeholders})",
            [_now(), *ids],
        )

    def purge_user(self) -> None:
        """Delete everything belonging to this user."""
        for table in ("messages", "applications", "status_changes", "fetched", "meta"):
            self.conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (self.user_id,))
        self.conn.commit()

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE user_id = ? AND key = ?", (self.user_id, key)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (user_id, key, value) VALUES (?,?,?)",
            (self.user_id, key, value),
        )

    def commit(self):
        self.conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_app(row: sqlite3.Row) -> Application:
    return Application(
        key=row["key"],
        company=row["company"],
        role=row["role"],
        job_id=row["job_id"] or "",
        source=row["source"] or "",
        status=Status(row["status"]),
        applied_on=datetime.fromisoformat(row["applied_on"]) if row["applied_on"] else None,
        last_update=datetime.fromisoformat(row["last_update"]),
        last_subject=row["last_subject"] or "",
        thread_url=row["thread_url"] or "",
        email_count=row["email_count"] or 1,
        confidence=row["confidence"] or 1.0,
        notes=row["notes"] or "",
    )
