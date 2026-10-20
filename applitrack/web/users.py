"""The user registry.

One row per person who has connected their Gmail. The refresh token is the
sensitive part -- it is encrypted at rest with a key the operator supplies in
APPLITRACK_SECRET_KEY, so a stolen database file alone does not give anyone
access to anybody's mailbox.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..models import DEFAULT_LOOKBACK_DAYS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,   -- Google 'sub' claim, stable forever
    email            TEXT NOT NULL,
    name             TEXT,
    token_blob       TEXT NOT NULL,      -- encrypted OAuth credentials
    spreadsheet_id   TEXT,
    created_at       TEXT,
    last_sync_at     TEXT,
    last_history_id  TEXT,               -- Gmail cursor for incremental sync
    watch_expires_at TEXT,               -- Gmail push subscription expiry
    notify_email     INTEGER DEFAULT 1,
    sync_enabled     INTEGER DEFAULT 1,
    lookback_days    INTEGER DEFAULT 90,
    last_error       TEXT,
    state            TEXT DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""


@dataclass
class User:
    user_id: str
    email: str
    name: str = ""
    spreadsheet_id: str = ""
    created_at: str = ""
    last_sync_at: str = ""
    last_history_id: str = ""
    watch_expires_at: str = ""
    notify_email: bool = True
    sync_enabled: bool = True
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    last_error: str = ""
    state: str = "active"

    @property
    def sheet_url(self) -> str:
        if not self.spreadsheet_id:
            return ""
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"

    @property
    def first_name(self) -> str:
        return (self.name or self.email).split()[0].split("@")[0].title()


class UserStore:
    def __init__(self, path: str = "data/users.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- reads -------------------------------------------------------------

    def get(self, user_id: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return _to_user(row) if row else None

    def get_by_email(self, email: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)", (email,)
        ).fetchone()
        return _to_user(row) if row else None

    def all_active(self) -> list[User]:
        rows = self.conn.execute(
            "SELECT * FROM users WHERE state = 'active' AND sync_enabled = 1"
        ).fetchall()
        return [_to_user(r) for r in rows]

    def count(self) -> int:
        return self.conn.execute(
            "SELECT count(*) AS n FROM users WHERE state = 'active'"
        ).fetchone()["n"]

    # -- credentials -------------------------------------------------------

    def save_credentials(self, user_id: str, creds_json: str) -> None:
        self.conn.execute(
            "UPDATE users SET token_blob = ? WHERE user_id = ?",
            (encrypt(creds_json), user_id),
        )
        self.conn.commit()

    def load_credentials(self, user_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT token_blob FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row or not row["token_blob"]:
            return None
        try:
            return json.loads(decrypt(row["token_blob"]))
        except Exception:
            return None

    # -- writes ------------------------------------------------------------

    def upsert(self, user_id: str, email: str, name: str, creds_json: str) -> User:
        existing = self.get(user_id)
        if existing:
            self.conn.execute(
                """UPDATE users SET email = ?, name = ?, token_blob = ?,
                   state = 'active', last_error = '' WHERE user_id = ?""",
                (email, name, encrypt(creds_json), user_id),
            )
        else:
            self.conn.execute(
                """INSERT INTO users (user_id, email, name, token_blob, created_at)
                   VALUES (?,?,?,?,?)""",
                (user_id, email, name, encrypt(creds_json), _now()),
            )
        self.conn.commit()
        return self.get(user_id)

    def update(self, user_id: str, **fields) -> None:
        allowed = {
            "spreadsheet_id", "last_sync_at", "last_history_id", "watch_expires_at",
            "notify_email", "sync_enabled", "lookback_days", "last_error", "state",
            "email", "name",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(int(value) if isinstance(value, bool) else value)
        if not sets:
            return
        values.append(user_id)
        self.conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", values)
        self.conn.commit()

    def touch_sync(self, user_id: str, history_id: str | None = None) -> None:
        fields = {"last_sync_at": _now(), "last_error": ""}
        if history_id:
            fields["last_history_id"] = str(history_id)
        self.update(user_id, **fields)

    def record_error(self, user_id: str, message: str) -> None:
        self.update(user_id, last_error=message[:500])

    def disconnect(self, user_id: str) -> None:
        """Forget the person entirely: token destroyed, row removed."""
        self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        self.conn.commit()


def _to_user(row: sqlite3.Row) -> User:
    return User(
        user_id=row["user_id"],
        email=row["email"],
        name=row["name"] or "",
        spreadsheet_id=row["spreadsheet_id"] or "",
        created_at=row["created_at"] or "",
        last_sync_at=row["last_sync_at"] or "",
        last_history_id=row["last_history_id"] or "",
        watch_expires_at=row["watch_expires_at"] or "",
        notify_email=bool(row["notify_email"]),
        sync_enabled=bool(row["sync_enabled"]),
        lookback_days=row["lookback_days"] or DEFAULT_LOOKBACK_DAYS,
        last_error=row["last_error"] or "",
        state=row["state"] or "active",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Token encryption
# --------------------------------------------------------------------------

_FERNET = None


def _fernet():
    """Build the cipher from APPLITRACK_SECRET_KEY.

    Refusing to start without a key is deliberate: silently falling back to
    plaintext would store every user's mailbox token in the clear.
    """
    global _FERNET
    if _FERNET is not None:
        return _FERNET

    from cryptography.fernet import Fernet

    key = os.environ.get("APPLITRACK_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "APPLITRACK_SECRET_KEY is not set.\n"
            "Generate one and keep it safe -- losing it means every user has to "
            "reconnect:\n\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n'
        )
    _FERNET = Fernet(key.encode())
    return _FERNET


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
