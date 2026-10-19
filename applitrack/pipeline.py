"""Assemble classified emails into applications.

The merge rules are where accuracy is won or lost:

* Emails group by `(company, job_id)`, falling back to `(company, role)` when
  no id is present. Two Microsoft internships with different job numbers stay
  two applications; five emails about one Workday requisition become one.
* Companies whose names differ only by a suffix ("DHL" vs "DHL Group") are
  merged, and the longer, more specific name wins for display.
* Status comes from the newest email, not the highest-ranked one. A rejection
  after an interview is a rejection -- but a same-day tie breaks toward the
  more advanced status.
* An application with no reply for `ghost_after_days` is marked Ghosted so it
  stops sitting in the "in progress" count forever.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .classify import classify
from .models import Application, Email, Signal, Status

GHOST_AFTER_DAYS = 45


def build_applications(
    signals: list[Signal],
    ghost_after_days: int = GHOST_AFTER_DAYS,
) -> list[Application]:
    """Collapse signals into one `Application` per real application."""
    if not signals:
        return []

    groups: dict[str, list[Signal]] = {}
    for signal in signals:
        groups.setdefault(signal.key, []).append(signal)

    groups = _merge_company_aliases(groups)

    apps = [_fold(group, ghost_after_days) for group in groups.values()]
    apps.sort(key=lambda a: (a.status.is_open is False, -a.last_update.timestamp()))
    return apps


def process_emails(emails: list[Email], ghost_after_days: int = GHOST_AFTER_DAYS):
    """Classify then assemble. Returns `(applications, signals, skipped)`."""
    signals: list[Signal] = []
    skipped: list[Email] = []
    for email in emails:
        signal = classify(email)
        if signal:
            signals.append(signal)
        else:
            skipped.append(email)
    return build_applications(signals, ghost_after_days), signals, skipped


# --------------------------------------------------------------------------


def _fold(group: list[Signal], ghost_after_days: int) -> Application:
    group = sorted(group, key=lambda s: (s.email.date, s.status.rank))

    newest = group[-1]
    status = newest.status

    # The application date is the earliest email that reads like a submission.
    applied_on = None
    for signal in group:
        if signal.status in (Status.APPLIED, Status.ACKNOWLEDGED):
            applied_on = signal.email.date
            break
    if applied_on is None:
        applied_on = group[0].email.date

    company = _best_company(group)
    role = _best_role(group)
    job_id = next((s.job_id for s in reversed(group) if s.job_id), "")
    source = next((s.source for s in group if s.source and s.source != "Direct"), group[0].source)

    last_update = newest.email.date
    age = (datetime.now(timezone.utc) - last_update).days
    if status.is_open and age >= ghost_after_days:
        status = Status.GHOSTED

    history = [(s.email.date, s.status, s.email.subject) for s in group]

    return Application(
        key=newest.key,
        company=company,
        role=role,
        job_id=job_id,
        source=source,
        status=status,
        applied_on=applied_on,
        last_update=last_update,
        last_subject=newest.email.subject,
        thread_url=newest.email.gmail_url,
        email_count=len(group),
        confidence=round(max(s.confidence for s in group), 2),
        history=history,
        notes=_notes(status, age, ghost_after_days),
    )


def _notes(status: Status, age_days: int, ghost_after_days: int) -> str:
    if status == Status.GHOSTED:
        return f"No reply in {age_days} days"
    if status in (Status.ASSESSMENT, Status.ACTION_REQUIRED):
        return "Needs action from you"
    if status == Status.INTERVIEW:
        return "Interview stage"
    if status.is_open and age_days >= ghost_after_days // 2:
        return f"Quiet for {age_days} days - consider following up"
    return ""


def _best_company(group: list[Signal]) -> str:
    """Prefer the most specific name that most emails agree on."""
    counts: dict[str, int] = {}
    for signal in group:
        name = signal.company
        if name and name != "Unknown":
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return "Unknown"
    # Most frequent first, then longest (more specific) as the tie-break.
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def _best_role(group: list[Signal]) -> str:
    counts: dict[str, int] = {}
    for signal in group:
        role = signal.role
        if role and role != "Unspecified":
            counts[role] = counts.get(role, 0) + 1
    if not counts:
        return "Unspecified"
    return max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


_COMPANY_NOISE = re.compile(
    r"\b(?:pvt|private|ltd|limited|llp|inc|corp|corporation|co|gmbh|ag|plc|"
    r"llc|group|global|india|technologies|technology|solutions|services|"
    r"systems|labs|holdings|the)\b",
    re.I,
)


def _company_root(name: str) -> str:
    """`DHL Group` and `DHL` collapse to the same root."""
    stripped = _COMPANY_NOISE.sub(" ", name or "")
    return re.sub(r"[^a-z0-9]+", "", stripped.lower())


def _merge_company_aliases(groups: dict[str, list[Signal]]) -> dict[str, list[Signal]]:
    """Fold groups whose company names are the same once suffixes are stripped.

    Only merges when the discriminator (job id, else role) also matches, so
    two genuinely different roles at one company stay apart.
    """
    by_identity: dict[tuple[str, str], str] = {}
    merged: dict[str, list[Signal]] = {}

    for key, group in groups.items():
        company_root = _company_root(_best_company(group))
        job_id = next((s.job_id for s in group if s.job_id), "")
        discriminator = job_id.lower() if job_id else _role_root(_best_role(group))
        identity = (company_root, discriminator)

        target = by_identity.get(identity)
        if target is None:
            by_identity[identity] = key
            merged[key] = list(group)
        else:
            merged[target].extend(group)

    return merged


def _role_root(role: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (role or "").lower())


def summarise(apps: list[Application]) -> dict:
    """Counts the digest and the sheet header both need."""
    by_status: dict[str, int] = {}
    for app in apps:
        by_status[app.status.value] = by_status.get(app.status.value, 0) + 1

    active = [a for a in apps if a.status.is_open]
    recent_cut = datetime.now(timezone.utc) - timedelta(days=7)

    return {
        "total": len(apps),
        "active": len(active),
        "by_status": by_status,
        "needs_action": [
            a for a in apps
            if a.status in (Status.ASSESSMENT, Status.INTERVIEW, Status.ACTION_REQUIRED)
        ],
        "recent": [a for a in apps if a.last_update >= recent_cut],
        "stale": [
            a for a in active
            if a.days_since_update >= 21 and a.status != Status.GHOSTED
        ],
        "response_rate": _response_rate(apps),
    }


def _response_rate(apps: list[Application]) -> float:
    """Share of applications that got past an auto-acknowledgement."""
    if not apps:
        return 0.0
    progressed = sum(
        1 for a in apps
        if a.status in (Status.ASSESSMENT, Status.INTERVIEW, Status.OFFER, Status.REJECTED)
    )
    return progressed / len(apps)
