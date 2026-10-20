"""Core data types shared across the pipeline."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

# How far back a first sync reads. Three months covers the applications
# that are still live for most people; a year took ten minutes to read
# and mostly returned things already decided. Adjustable per user.
DEFAULT_LOOKBACK_DAYS = 90


class Status(str, Enum):
    """Where an application currently stands.

    The integer `rank` orders statuses for display and for breaking ties when
    two emails land on the same day.
    """

    APPLIED = "Applied"
    ACKNOWLEDGED = "Acknowledged"
    ACTION_REQUIRED = "Action Required"
    ASSESSMENT = "Assessment"
    INTERVIEW = "Interview"
    OFFER = "Offer"
    REJECTED = "Rejected"
    GHOSTED = "Ghosted"
    WITHDRAWN = "Withdrawn"

    @property
    def rank(self) -> int:
        return _STATUS_RANK[self]

    @property
    def is_open(self) -> bool:
        return self not in (Status.REJECTED, Status.WITHDRAWN, Status.GHOSTED)

    @property
    def is_terminal(self) -> bool:
        return self in (Status.REJECTED, Status.WITHDRAWN, Status.OFFER)


_STATUS_RANK = {
    Status.APPLIED: 1,
    Status.ACKNOWLEDGED: 2,
    Status.ACTION_REQUIRED: 3,
    Status.ASSESSMENT: 4,
    Status.INTERVIEW: 5,
    Status.REJECTED: 6,
    Status.WITHDRAWN: 6,
    Status.GHOSTED: 6,
    Status.OFFER: 7,
}


@dataclass
class Email:
    """A single Gmail message, normalised to what the classifier needs."""

    msg_id: str
    thread_id: str
    date: datetime
    sender: str          # bare address, lowercased
    sender_name: str     # display name from the From header
    subject: str
    body: str            # plain text, HTML already stripped
    labels: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        return self.sender.rpartition("@")[2].lower()

    @property
    def root_domain(self) -> str:
        """`careers.microsoft.com` -> `microsoft.com` (handles common 2-part TLDs)."""
        parts = self.domain.split(".")
        if len(parts) <= 2:
            return self.domain
        if parts[-2] in ("co", "com", "net", "org", "gov", "ac") and len(parts[-1]) == 2:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

    @property
    def local_part(self) -> str:
        return self.sender.partition("@")[0].lower()

    @property
    def local_part_normalised(self) -> str:
        """`talent.acquisition` -> `talentacquisition`, `no-reply` -> `noreply`.

        Recruiting mailboxes are spelled a dozen ways; the separators carry
        no meaning, so strip them before matching against the known names.
        """
        return re.sub(r"[._\-+]", "", self.local_part)

    @property
    def text(self) -> str:
        """Subject + body, for pattern matching."""
        return f"{self.subject}\n{self.body}"

    @property
    def gmail_url(self) -> str:
        return f"https://mail.google.com/mail/u/0/#all/{self.thread_id}"


@dataclass
class Signal:
    """One classified email: the evidence behind an application's status."""

    email: Email
    status: Status
    confidence: float          # 0.0 - 1.0
    company: str
    role: str
    job_id: str = ""
    source: str = ""           # LinkedIn / Workday / Internshala / Direct ...
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Identity of the application this email belongs to.

        Two postings at the same company are the same application only when
        they share a job id; without one we fall back to the normalised role.
        """
        company = _slug(self.company)
        discriminator = _slug(self.job_id) if self.job_id else _slug(self.role)
        raw = f"{company}|{discriminator}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class Application:
    """An application, assembled from every email that mentions it."""

    key: str
    company: str
    role: str
    job_id: str
    source: str
    status: Status
    applied_on: Optional[datetime]
    last_update: datetime
    last_subject: str
    thread_url: str
    email_count: int = 1
    confidence: float = 1.0
    history: list[tuple[datetime, Status, str]] = field(default_factory=list)
    notes: str = ""

    @property
    def days_since_update(self) -> int:
        return (datetime.now(timezone.utc) - self.last_update).days

    @property
    def days_in_pipeline(self) -> int:
        start = self.applied_on or self.last_update
        return (datetime.now(timezone.utc) - start).days

    def to_row(self) -> dict:
        """Flat dict for the spreadsheet / JSON export."""
        return {
            "Company": self.company,
            "Role": self.role,
            "Status": self.status.value,
            "Applied On": self.applied_on.strftime("%Y-%m-%d") if self.applied_on else "",
            "Last Update": self.last_update.strftime("%Y-%m-%d"),
            "Days Since Update": self.days_since_update,
            "Source": self.source,
            "Job ID": self.job_id,
            "Emails": self.email_count,
            "Confidence": f"{self.confidence:.0%}",
            "Last Email Subject": self.last_subject,
            "Thread": self.thread_url,
            "Notes": self.notes,
        }


COLUMNS = [
    "Company", "Role", "Status", "Applied On", "Last Update", "Days Since Update",
    "Source", "Job ID", "Emails", "Confidence", "Last Email Subject", "Thread", "Notes",
]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())
