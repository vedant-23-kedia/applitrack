"""Fetch and normalise Gmail messages.

Gmail's search does the coarse filtering so we download hundreds of messages
rather than tens of thousands; the scoring in `classify` does the rest. The
query is deliberately broad -- recall matters more than precision here,
because a missed email is a missed application.
"""

from __future__ import annotations

import base64
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime

from .models import DEFAULT_LOOKBACK_DAYS, Email

log = logging.getLogger("applitrack.gmail")

# Gmail allows 15,000 quota units per user per minute. A messages.get costs 5,
# so a 1500-message first sync is ~7,500 units -- comfortably inside the limit
# on its own, but not if a previous attempt already spent budget in the same
# minute. So: pace the batches, and back off properly when told to.
# Gmail's real ceiling is ~250 quota units per user per second. A
# messages.get costs 5, so a 50-message batch returning in about a second
# sits exactly on the limit -- and anything else touching the mailbox at the
# same time puts it over. 25 messages per ~2s is roughly 60 units/second,
# a quarter of the budget, which leaves room for a second sync to coexist.
BATCH_SIZE = 25
MIN_BATCH_SIZE = 5
PAUSE_BETWEEN_BATCHES = 1.0     # seconds
MAX_PAUSE = 16.0
COOLDOWN_SECONDS = 65           # a full quota window, for when we do get throttled
MAX_ROUNDS = 4                  # re-attempts of the whole throttled remainder
MAX_ATTEMPTS = 6

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RATE_LIMIT_MARKERS = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quotaexceeded",
    "quota exceeded",
    "backenderror",
    "internalerror",
)


def is_retryable(exc: Exception) -> bool:
    """Is this worth trying again, or is it a real failure?

    A 403 is usually permanent (no access), but Gmail also uses it for rate
    limiting, so the reason string decides.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in _RETRYABLE_STATUS:
        return True
    if status == 403:
        return any(m in str(exc).lower() for m in _RATE_LIMIT_MARKERS)
    return False


def retry(call, what: str = "gmail call", attempts: int = MAX_ATTEMPTS):
    """Run `call`, backing off exponentially on rate limits.

    Delays double from 2s, with jitter so concurrent users do not retry in
    lockstep. Six attempts spans roughly two minutes, which is what a
    per-minute quota needs to recover.
    """
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            if attempt == attempts - 1 or not is_retryable(exc):
                raise
            delay = min(2.0 * (2 ** attempt), 64.0) + random.uniform(0, 1.0)
            log.warning(
                "%s rate-limited (attempt %d/%d), waiting %.1fs",
                what, attempt + 1, attempts, delay,
            )
            time.sleep(delay)

# Broad net: anything that smells like recruitment mail. The classifier
# throws out the marketing afterwards.
BASE_QUERY = (
    "("
    'subject:(application OR applying OR applied OR candidature OR interview OR '
    'assessment OR shortlist OR "job" OR role OR position OR offer OR recruitment '
    'OR hiring OR candidate) '
    'OR "thank you for applying" OR "your application" OR "we received your application" '
    'OR "application received" OR "application status" OR "application update" '
    'OR "interview" OR "assessment" OR "shortlisted" OR "regret to inform" '
    'OR "offer letter" OR "job id" OR "requisition"'
    ") -in:chats -in:draft"
)


class GmailClient:
    """Read-only Gmail access, normalised into `Email` objects."""

    def __init__(self, service=None, user_id: str = "me"):
        if service is None:
            from .auth import build_service

            service = build_service("gmail", "v1")
        self.service = service
        self.user_id = user_id

    # -- fetching ----------------------------------------------------------

    def search_ids(self, query: str, max_results: int = 1000) -> list[str]:
        """Message ids matching a Gmail query, newest first."""
        ids: list[str] = []
        page_token = None
        while len(ids) < max_results:
            resp = retry(
                lambda: self.service.users()
                .messages()
                .list(
                    userId=self.user_id,
                    q=query,
                    maxResults=min(500, max_results - len(ids)),
                    pageToken=page_token,
                )
                .execute(),
                "messages.list",
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids[:max_results]

    def fetch(self, msg_ids: list[str], progress=None) -> list[Email]:
        """Download messages in batches and normalise them.

        Messages rate-limited inside a batch are collected and retried one by
        one afterwards, so a partial throttle costs a few messages' delay
        rather than the whole sync.
        """
        emails: list[Email] = []
        pending = list(msg_ids)
        total = len(msg_ids)
        done = 0

        for round_no in range(MAX_ROUNDS):
            if not pending:
                break
            if round_no:
                # Everything left was throttled. Let the quota window refill
                # before trying again -- hammering it just extends the ban.
                log.warning(
                    "%d message(s) throttled; cooling down %ds before round %d",
                    len(pending), COOLDOWN_SECONDS, round_no + 1,
                )
                time.sleep(COOLDOWN_SECONDS)

            throttled: list[str] = []
            # Adaptive pacing: slow down when refused, speed back up when the
            # mailbox lets us through. Abandoning the round on the first
            # refusal (what this used to do) was far worse -- it turned a
            # mild throttle into ~20 messages per minute.
            size = BATCH_SIZE
            pause = PAUSE_BETWEEN_BATCHES
            index = 0

            while index < len(pending):
                chunk = pending[index : index + size]
                index += len(chunk)
                batch_failed: list[str] = []

                def collect(request_id, response, exception, _failed=batch_failed):
                    if exception is not None:
                        if is_retryable(exception):
                            _failed.append(request_id)
                        else:
                            log.warning("message %s failed: %s", request_id, exception)
                        return
                    parsed = self._parse(response)
                    if parsed:
                        emails.append(parsed)

                batch = self.service.new_batch_http_request(callback=collect)
                for mid in chunk:
                    batch.add(
                        self.service.users()
                        .messages()
                        .get(userId=self.user_id, id=mid, format="full"),
                        request_id=mid,
                    )

                # A batch request succeeds as a whole even when the individual
                # sub-requests inside it are refused, so the per-message
                # failures above are the ones that actually matter.
                retry(batch.execute, "messages.get batch")

                throttled.extend(batch_failed)
                done += len(chunk) - len(batch_failed)
                if progress:
                    progress(min(done, total), total)

                if batch_failed:
                    # Back off, but keep going -- the rest of this round can
                    # still succeed at a slower rate.
                    size = max(MIN_BATCH_SIZE, size // 2)
                    pause = min(MAX_PAUSE, pause * 2)
                    log.warning(
                        "throttled on %d/%d; easing to %d per batch, %.1fs apart",
                        len(batch_failed), len(chunk), size, pause,
                    )
                elif size < BATCH_SIZE:
                    # Recovering: creep back up rather than snapping back.
                    size = min(BATCH_SIZE, size + MIN_BATCH_SIZE)
                    pause = max(PAUSE_BETWEEN_BATCHES, pause / 1.5)

                if index < len(pending):
                    time.sleep(pause)

            pending = list(dict.fromkeys(throttled))

        if pending:
            log.error(
                "could not fetch %d message(s) after %d rounds - quota stayed exhausted",
                len(pending), MAX_ROUNDS,
            )

        emails.sort(key=lambda e: e.date)
        return emails

    def full_query(self, since_days: int = DEFAULT_LOOKBACK_DAYS, extra_query: str = "") -> str:
        """The Gmail search used for a full sync."""
        after = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y/%m/%d")
        query = f"{BASE_QUERY} after:{after}"
        if extra_query:
            query = f"{query} {extra_query}"
        return query

    def search_and_fetch(
        self,
        since_days: int = DEFAULT_LOOKBACK_DAYS,
        extra_query: str = "",
        max_results: int = 1000,
        progress=None,
    ) -> list[Email]:
        query = self.full_query(since_days, extra_query)
        ids = self.search_ids(query, max_results=max_results)
        if not ids:
            return []
        return self.fetch(ids, progress=progress)

    # -- normalising -------------------------------------------------------

    def _parse(self, msg: dict) -> Email | None:
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

        raw_from = headers.get("from", "")
        sender_name, sender_addr = parseaddr(raw_from)
        if not sender_addr:
            return None

        date = _parse_date(headers.get("date"), msg.get("internalDate"))
        body = _extract_body(payload)

        return Email(
            msg_id=msg["id"],
            thread_id=msg.get("threadId", msg["id"]),
            date=date,
            sender=sender_addr.lower(),
            sender_name=sender_name or sender_addr.split("@")[0],
            subject=headers.get("subject", "(no subject)"),
            body=body,
            labels=msg.get("labelIds", []),
        )


def _parse_date(date_header: str | None, internal_date: str | None) -> datetime:
    if internal_date:
        try:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
        except (ValueError, TypeError):
            pass
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _extract_body(payload: dict, limit: int = 20000) -> str:
    """Walk the MIME tree for text, preferring text/plain over HTML."""
    plain, html = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            except Exception:
                decoded = ""
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)

    text = "\n".join(plain).strip()
    if not text and html:
        text = html_to_text("\n".join(html))
    return normalise_whitespace(text)[:limit]


def html_to_text(html: str) -> str:
    """Strip HTML to readable text, without pulling in a parser if avoidable."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "head", "title"]):
            tag.decompose()
        return soup.get_text(" ")
    except Exception:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return _unescape(text)


def _unescape(text: str) -> str:
    import html as html_mod

    return html_mod.unescape(text)


def normalise_whitespace(text: str) -> str:
    """Collapse the zero-width padding bulk senders use to defeat clipping."""
    text = text.replace("‌", " ").replace("‍", " ").replace("﻿", " ")
    text = text.replace("­", "").replace("͏", " ")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
