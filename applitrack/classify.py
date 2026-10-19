"""Decide whether an email is about a job application, and if so, its status.

Two stages:

1. `score_email` asks "is this a real application email at all?" It returns a
   score and the reasons behind it, so a misfiled email can be debugged with
   `applitrack explain`.
2. `classify` runs the status patterns over anything that cleared the bar.

An optional LLM pass (`--llm`) only ever sees emails the rules found ambiguous,
which keeps the token cost proportional to the genuinely hard cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .extract import extract_company, extract_job_id, extract_role
from .models import Email, Signal, Status
from . import rules


@dataclass
class Score:
    value: int
    reasons: list[str]

    @property
    def is_application(self) -> bool:
        return self.value >= rules.MIN_SCORE

    @property
    def is_ambiguous(self) -> bool:
        """Close to the threshold -- the band where an LLM pass pays off."""
        return rules.MIN_SCORE - 3 <= self.value <= rules.MIN_SCORE + 2


def score_email(email: Email) -> Score:
    """Weigh an email's application signals against its marketing signals."""
    reasons: list[str] = []
    total = 0
    s = rules.SCORES

    domain = email.domain
    root = email.root_domain
    sender = email.sender
    subject_lower = email.subject.lower().strip()

    # --- hard vetoes -------------------------------------------------------
    if sender in rules.NEVER_SENDERS or domain in rules.NEVER_SENDERS or root in rules.NEVER_SENDERS:
        total += s["never_sender"]
        reasons.append(f"never-sender:{sender}")

    if domain in rules.NON_JOB_DOMAINS or root in rules.NON_JOB_DOMAINS:
        total += s["non_job_domain"]
        reasons.append(f"non-job-domain:{domain}")

    for rx in rules.RX_NON_JOB:
        if rx.search(email.text):
            total += s["non_job_context"]
            reasons.append(f"non-job-context:{rx.pattern[:34]}")
            break

    # --- sender reputation -------------------------------------------------
    ats = rules.ATS_DOMAINS.get(root) or rules.ATS_DOMAINS.get(domain)
    if ats:
        total += s["ats_domain"]
        reasons.append(f"ats:{ats}")

    board = rules.AGGREGATOR_DOMAINS.get(root) or rules.AGGREGATOR_DOMAINS.get(domain)
    if board and not ats:
        total += s["aggregator_domain"]
        reasons.append(f"job-board:{board}")

    if not ats and not board:
        if (
            email.local_part in rules.RECRUITING_LOCALPARTS
            or email.local_part_normalised in rules.RECRUITING_LOCALPARTS
        ):
            total += s["recruiting_localpart"]
            reasons.append(f"recruiting-mailbox:{email.local_part}")
        if any(hint in domain for hint in rules.RECRUITING_SUBDOMAIN_HINTS):
            total += s["recruiting_subdomain"]
            reasons.append("recruiting-subdomain")

    # --- content -----------------------------------------------------------
    strong_hits = [rx.pattern for rx in rules.RX_STRONG if rx.search(email.text)]
    if strong_hits:
        total += s["strong_phrase"]
        reasons.append(f"application-phrase:{strong_hits[0][:34]}")

    weak_hits = sum(1 for rx in rules.RX_WEAK if rx.search(email.text))
    if weak_hits:
        total += min(weak_hits, 3) * s["weak_phrase"]
        reasons.append(f"weak-phrases:{weak_hits}")

    if extract_job_id(email):
        total += s["job_id"]
        reasons.append("job-id-present")

    # An email that announces a decision -- a rejection, an interview invite,
    # an assessment link -- is application mail even when it never uses the
    # words "thank you for applying". A hiring recruiter writing by hand
    # usually does not. Qualified by HIRING_CONTEXT so a failed bus booking
    # does not read as a rejection.
    if rules.HIRING_CONTEXT.search(email.text):
        for name, patterns in rules.RX_STATUS:
            if name not in rules.DECISIVE_STATUSES:
                continue
            hit = next((rx for rx in patterns if rx.search(email.text)), None)
            if hit:
                total += s["outcome_phrase"]
                reasons.append(f"outcome:{name}")
                break

    # --- marketing ---------------------------------------------------------
    marketing_hits = [rx.pattern for rx in rules.RX_MARKETING if rx.search(email.text)]
    if marketing_hits:
        # Two or three promotional phrases is a blast, not a coincidence.
        weight = min(len(marketing_hits), 3)
        total += weight * s["marketing_phrase"]
        reasons.append(f"marketing x{len(marketing_hits)}:{marketing_hits[0][:30]}")

    if subject_lower.startswith(rules.MARKETING_SUBJECT_PREFIXES):
        total += s["marketing_subject_prefix"]
        reasons.append("marketing-subject-prefix")

    if "CATEGORY_PROMOTIONS" in email.labels:
        total += s["promotions_label"]
        reasons.append("gmail-promotions")

    # --- job-board redemption ---------------------------------------------
    # Internshala and friends do send real per-application mail. Give it back
    # the points the aggregator and marketing penalties took away.
    if board:
        txn = [rx.pattern for rx in rules.RX_TRANSACTIONAL if rx.search(email.text)]
        if txn and not subject_lower.startswith(rules.MARKETING_SUBJECT_PREFIXES):
            total += s["transactional_board"]
            reasons.append(f"board-transactional:{txn[0][:30]}")

    return Score(total, reasons)


def detect_status(email: Email) -> tuple[Status, float, str]:
    """Map an email onto a pipeline status.

    Returns `(status, confidence, matched_pattern)`. Patterns are tried in the
    order given in `rules.STATUS_PATTERNS`, so terminal outcomes beat the
    process language they are often wrapped in.
    """
    for name, patterns in rules.RX_STATUS:
        for rx in patterns:
            m = rx.search(email.text)
            if m:
                status = Status(name)
                confidence = 0.9 if status in (Status.REJECTED, Status.OFFER) else 0.8
                return status, confidence, rx.pattern
    # It cleared the application bar but said nothing about progress.
    return Status.APPLIED, 0.5, "default"


def classify(email: Email, min_score: int | None = None) -> Signal | None:
    """Turn an email into a `Signal`, or `None` if it is not application mail."""
    threshold = rules.MIN_SCORE if min_score is None else min_score
    score = score_email(email)
    if score.value < threshold:
        return None

    status, confidence, pattern = detect_status(email)
    company, source = extract_company(email)
    role = extract_role(email)
    job_id = extract_job_id(email)

    # A confident classification needs both a clear signal and a known company.
    if company == "Unknown":
        confidence *= 0.6
    if score.value >= rules.MIN_SCORE + 4:
        confidence = min(1.0, confidence + 0.1)

    return Signal(
        email=email,
        status=status,
        confidence=round(confidence, 2),
        company=company,
        role=role,
        job_id=job_id,
        source=source,
        reasons=score.reasons + [f"status:{pattern[:40]}", f"score:{score.value}"],
    )


# --------------------------------------------------------------------------
# Optional LLM pass for the ambiguous band
# --------------------------------------------------------------------------

LLM_SYSTEM = """You classify emails for a job-application tracker.

Reply with ONLY a JSON object, no prose:
{"is_application": bool, "company": str, "role": str, "status": str, "confidence": float}

status must be one of: Applied, Acknowledged, Action Required, Assessment,
Interview, Offer, Rejected.

is_application is false for job-board marketing, newsletters, job
recommendations, event or contest invitations, and any "application" that is
not for a job (loans, passports, tax, bookings). It is true only when the
email concerns a specific application this person actually submitted."""


def classify_with_llm(emails: list[Email], model: str = "claude-sonnet-5-5") -> dict[str, dict]:
    """Ask Claude about emails the rules could not settle.

    Returns `{msg_id: parsed_json}`. Requires `anthropic` and ANTHROPIC_API_KEY;
    raises RuntimeError if either is missing, so the caller can fall back.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("pip install anthropic to use --llm") from exc

    import json

    client = anthropic.Anthropic()
    out: dict[str, dict] = {}

    for email in emails:
        body = email.body[:3000]
        prompt = (
            f"From: {email.sender_name} <{email.sender}>\n"
            f"Date: {email.date:%Y-%m-%d}\n"
            f"Subject: {email.subject}\n\n{body}"
        )
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=300,
                system=LLM_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                out[email.msg_id] = json.loads(match.group(0))
        except Exception as exc:  # pragma: no cover - network path
            out[email.msg_id] = {"error": str(exc)}

    return out
