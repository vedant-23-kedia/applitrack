"""Real-time updates via Gmail push notifications.

Gmail can tell us the moment a mailbox changes instead of us asking every
few minutes. The chain is:

    new mail -> Gmail -> Pub/Sub topic -> HTTPS POST to /push -> we sync

What arrives is only a mailbox address and a `historyId` -- a cursor, not the
message. We then ask Gmail for everything that happened between the cursor we
stored last time and this one, which is usually two or three message ids.

Two details that bite people:

* A watch expires after seven days. It must be renewed, so `ensure_watch`
  runs on every sync and renews anything inside a day of lapsing.
* A `historyId` older than about a week is rejected by Gmail. When that
  happens the only correct response is a full re-read, which `history_since`
  signals by returning None rather than an empty list.

Without Pub/Sub configured the service still works -- the scheduler polls
instead. Push makes it immediate; polling makes it eventual.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone

from ..gmail_client import retry

# projects/<project>/topics/<topic>
PUBSUB_TOPIC = os.environ.get("APPLITRACK_PUBSUB_TOPIC", "").strip()

# Shared secret appended to the push endpoint as ?token=... so random POSTs
# to the webhook are ignored.
PUSH_TOKEN = os.environ.get("APPLITRACK_PUSH_TOKEN", "").strip()

WATCH_RENEW_WITHIN_HOURS = 24


def push_configured() -> bool:
    return bool(PUBSUB_TOPIC)


# --------------------------------------------------------------------------
# Watch lifecycle
# --------------------------------------------------------------------------


def ensure_watch(gmail, user, user_store) -> str | None:
    """Start or renew this mailbox's push subscription.

    Returns the current historyId, which doubles as the starting cursor for
    a mailbox we have just begun watching.
    """
    if not push_configured():
        return None

    if user.watch_expires_at:
        try:
            expiry = datetime.fromisoformat(user.watch_expires_at)
            if expiry - datetime.now(timezone.utc) > timedelta(hours=WATCH_RENEW_WITHIN_HOURS):
                return user.last_history_id or None
        except ValueError:
            pass

    try:
        result = retry(
            lambda: gmail.users().watch(
                userId="me",
                body={
                    "topicName": PUBSUB_TOPIC,
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "INCLUDE",
                },
            ).execute(),
            "users.watch",
        )
    except Exception as exc:
        user_store.record_error(user.user_id, f"watch failed: {exc}")
        return None

    # expiration is epoch milliseconds
    expires = datetime.fromtimestamp(int(result["expiration"]) / 1000, tz=timezone.utc)
    history_id = str(result.get("historyId", ""))
    user_store.update(
        user.user_id,
        watch_expires_at=expires.isoformat(),
        **({"last_history_id": history_id} if not user.last_history_id else {}),
    )
    return history_id


def stop_watch(gmail) -> None:
    try:
        gmail.users().stop(userId="me").execute()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Incremental reads
# --------------------------------------------------------------------------


def history_since(gmail, start_history_id: str) -> tuple[list[str] | None, str | None]:
    """Message ids added since `start_history_id`.

    Returns `(ids, latest_history_id)`. `ids` is None when the cursor is too
    old to be useful -- the caller must then do a full sync.
    """
    message_ids: list[str] = []
    latest = start_history_id
    page_token = None

    while True:
        try:
            response = retry(
                lambda: gmail.users().history().list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                ).execute(),
                "history.list",
            )
        except Exception as exc:
            if "404" in str(exc) or "Invalid" in str(exc) or "failedPrecondition" in str(exc):
                return None, None          # cursor expired -> full resync
            raise

        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                labels = msg.get("labelIds", [])
                if "DRAFT" in labels or "SENT" in labels or "CHAT" in labels:
                    continue
                message_ids.append(msg["id"])

        latest = str(response.get("historyId", latest))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # One message can appear in several history records.
    return list(dict.fromkeys(message_ids)), latest


def current_history_id(gmail) -> str:
    profile = retry(
        lambda: gmail.users().getProfile(userId="me").execute(),
        "users.getProfile",
    )
    return str(profile.get("historyId", ""))


# --------------------------------------------------------------------------
# Webhook payload
# --------------------------------------------------------------------------


def parse_notification(body: dict) -> dict | None:
    """Unwrap the Pub/Sub envelope into `{emailAddress, historyId}`.

    Pub/Sub wraps our payload twice: a `message` object whose `data` field is
    base64 of the JSON Gmail actually sent.
    """
    message = (body or {}).get("message") or {}
    data = message.get("data")
    if not data:
        return None
    try:
        padded = data + "=" * (-len(data) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        payload = json.loads(decoded)
    except Exception:
        return None
    if "emailAddress" not in payload:
        return None
    return payload


def token_ok(provided: str | None) -> bool:
    """Reject webhook calls that do not carry the shared secret."""
    if not PUSH_TOKEN:
        return True          # not configured; endpoint is open by choice
    return provided == PUSH_TOKEN
