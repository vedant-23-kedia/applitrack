"""Tests for the multi-user service.

Covers the parts that are easy to get quietly wrong: token encryption, user
isolation (one person must never see another's applications), the Pub/Sub
webhook, and incremental rebuilds from stored signals.

OAuth itself is not exercised -- it needs Google -- so credentials are faked
at the store level.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before anything imports the crypto helpers.
os.environ.setdefault("APPLITRACK_SECRET_KEY",
                      "dGVzdC1rZXktZm9yLXVuaXQtdGVzdHMtMzJieXRlcyE=")
os.environ["APPLITRACK_HTTPS"] = "0"
os.environ["APPLITRACK_SCHEDULER"] = "0"

from applitrack.classify import classify  # noqa: E402
from applitrack.models import Email, Status  # noqa: E402
from applitrack.pipeline import build_applications  # noqa: E402
from applitrack.store import Store  # noqa: E402

NOW = datetime.now(timezone.utc)
_tmp = tempfile.mkdtemp(prefix="applitrack-test-")


def _key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


os.environ["APPLITRACK_SECRET_KEY"] = _key()


def mk(sender, subject, body, name="", days_ago=10):
    return Email(
        msg_id=f"m{abs(hash((sender, subject, days_ago))) % 10**9}",
        thread_id=f"t{abs(hash(subject)) % 10**6}",
        date=NOW - timedelta(days=days_ago),
        sender=sender, sender_name=name or sender.split("@")[0],
        subject=subject, body=body, labels=["INBOX"],
    )


ALICE_MAIL = mk(
    "careers@acme.com", "Thank you for applying",
    "Thank you for applying for the position of Data Analyst at ACME. "
    "Job ID: ACME-100. We will review your application.", days_ago=5)

BOB_MAIL = mk(
    "recruiting@globex.com", "Your application update",
    "Thank you for applying for the position of Backend Engineer at Globex. "
    "Job ID: GBX-7. Unfortunately we will not be moving forward.", days_ago=3)


# --------------------------------------------------------------------------

def test_token_encryption_roundtrip():
    from applitrack.web.users import decrypt, encrypt

    secret = json.dumps({"refresh_token": "1//very-secret", "token": "ya29.abc"})
    blob = encrypt(secret)
    assert secret not in blob, "plaintext leaked into the stored blob"
    assert decrypt(blob) == secret


def test_tokens_are_not_stored_in_plaintext():
    from applitrack.web.users import UserStore

    store = UserStore(os.path.join(_tmp, "users_enc.db"))
    creds = json.dumps({"refresh_token": "1//UNIQUE-SENTINEL-VALUE"})
    store.upsert("u1", "a@example.com", "A", creds)

    raw = Path(os.path.join(_tmp, "users_enc.db")).read_bytes()
    assert b"UNIQUE-SENTINEL-VALUE" not in raw, "refresh token readable on disk"
    assert store.load_credentials("u1")["refresh_token"] == "1//UNIQUE-SENTINEL-VALUE"
    store.close()


def test_users_cannot_see_each_others_applications():
    db = os.path.join(_tmp, "iso.db")
    alice = Store(db, user_id="alice")
    bob = alice.for_user("bob")

    a_signal = classify(ALICE_MAIL)
    b_signal = classify(BOB_MAIL)
    assert a_signal and b_signal

    alice.record_signal(a_signal)
    alice.replace_applications(build_applications([a_signal]))
    bob.record_signal(b_signal)
    bob.replace_applications(build_applications([b_signal]))
    alice.commit()

    a_apps = alice.all_applications()
    b_apps = bob.all_applications()

    assert len(a_apps) == 1 and a_apps[0].company == "ACME"
    assert len(b_apps) == 1 and b_apps[0].company == "Globex"
    assert {a.company for a in a_apps}.isdisjoint({a.company for a in b_apps})
    alice.close()


def test_signals_survive_a_reload():
    """Incremental sync depends on rebuilding old signals from the database."""
    db = os.path.join(_tmp, "reload.db")
    store = Store(db, user_id="carol")
    original = classify(ALICE_MAIL)
    store.record_signal(original)
    store.commit()
    store.close()

    reopened = Store(db, user_id="carol")
    restored = reopened.load_signals()
    assert len(restored) == 1
    assert restored[0].company == original.company
    assert restored[0].job_id == original.job_id
    assert restored[0].status == original.status
    # The grouping key must survive, or follow-ups would land on a new row.
    assert restored[0].key == original.key
    reopened.close()


def test_incremental_rebuild_merges_old_and_new():
    """A later rejection must update the existing row, not create a second."""
    db = os.path.join(_tmp, "incr.db")
    store = Store(db, user_id="dave")

    first = classify(mk("careers@acme.com", "Thank you for applying",
                        "Thank you for applying for the position of Data Analyst "
                        "at Acme. Job ID: ACME-100.", days_ago=20))
    store.record_signal(first)
    store.replace_applications(build_applications([first]))
    store.commit()
    assert store.all_applications()[0].status == Status.ACKNOWLEDGED

    later = classify(mk("careers@acme.com", "Acme update",
                        "Your application for the Data Analyst position. Job ID: "
                        "ACME-100. We regret to inform you we are not moving forward.",
                        days_ago=1))
    store.record_signal(later)
    changes = store.replace_applications(build_applications(store.load_signals()))
    store.commit()

    apps = store.all_applications()
    assert len(apps) == 1, [f"{a.company}/{a.job_id}" for a in apps]
    assert apps[0].status == Status.REJECTED
    assert apps[0].email_count == 2
    assert len(changes) == 1 and changes[0]["new"] == "Rejected"
    store.close()


def test_disconnect_erases_everything():
    db = os.path.join(_tmp, "purge.db")
    store = Store(db, user_id="erin")
    signal = classify(ALICE_MAIL)
    store.record_signal(signal)
    store.replace_applications(build_applications([signal]))
    store.commit()
    assert store.all_applications()

    store.purge_user()
    assert store.all_applications() == []
    assert store.load_signals() == []
    store.close()


def test_pubsub_payload_is_decoded():
    import base64

    from applitrack.web import push

    inner = json.dumps({"emailAddress": "someone@gmail.com", "historyId": 987654})
    envelope = {"message": {"data": base64.urlsafe_b64encode(inner.encode()).decode()}}

    parsed = push.parse_notification(envelope)
    assert parsed["emailAddress"] == "someone@gmail.com"
    assert str(parsed["historyId"]) == "987654"

    assert push.parse_notification({}) is None
    assert push.parse_notification({"message": {"data": "!!!not-base64!!!"}}) is None


def test_webhook_rejects_a_wrong_token():
    from applitrack.web import push

    push.PUSH_TOKEN = "expected-secret"
    try:
        assert push.token_ok("expected-secret") is True
        assert push.token_ok("guess") is False
        assert push.token_ok(None) is False
    finally:
        push.PUSH_TOKEN = ""


# --------------------------------------------------------------------------
# Web routes
# --------------------------------------------------------------------------

def _app():
    """A Flask test app wired to throwaway databases."""
    import applitrack.web.app as webapp

    webapp.DB_PATH = os.path.join(_tmp, "web_apps.db")
    webapp.USERS_DB = os.path.join(_tmp, "web_users.db")
    app = webapp.create_app()
    app.config["TESTING"] = True
    app.config["SESSION_COOKIE_SECURE"] = False
    return app


def test_landing_page_renders():
    app = _app()
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Connect your Gmail" in body
    assert "read-only" in body.lower() or "Read-only" in body


def test_dashboard_requires_sign_in():
    app = _app()
    response = app.test_client().get("/dashboard")
    assert response.status_code == 302
    assert "/" in response.headers["Location"]


def test_dashboard_shows_only_my_applications():
    import applitrack.web.app as webapp

    app = _app()
    users = app.user_store

    users.upsert("alice-id", "alice@example.com", "Alice Smith",
                 json.dumps({"refresh_token": "x"}))
    users.upsert("bob-id", "bob@example.com", "Bob Jones",
                 json.dumps({"refresh_token": "y"}))
    users.update("alice-id", spreadsheet_id="SHEET-A", last_sync_at="2026-10-19T10:00:00")

    store = Store(webapp.DB_PATH, user_id="alice-id")
    a = classify(ALICE_MAIL)
    store.record_signal(a)
    store.replace_applications(build_applications([a]))
    bob_store = store.for_user("bob-id")
    b = classify(BOB_MAIL)
    bob_store.record_signal(b)
    bob_store.replace_applications(build_applications([b]))
    store.commit()
    store.close()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "alice-id"

    body = client.get("/dashboard").get_data(as_text=True)
    assert "ACME" in body, "Alice cannot see her own application"
    assert "Globex" not in body, "Alice can see Bob's application"
    assert "Alice" in body
    assert "SHEET-A" in body


def test_healthz():
    app = _app()
    data = app.test_client().get("/healthz").get_json()
    assert data["ok"] is True
    assert "users" in data


def test_push_endpoint_ignores_unknown_mailbox():
    import base64

    app = _app()
    inner = json.dumps({"emailAddress": "nobody@nowhere.com", "historyId": 1})
    response = app.test_client().post(
        "/push",
        json={"message": {"data": base64.urlsafe_b64encode(inner.encode()).decode()}},
    )
    assert response.status_code == 204


def test_push_endpoint_rejects_bad_token():
    from applitrack.web import push

    app = _app()
    push.PUSH_TOKEN = "s3cret"
    try:
        response = app.test_client().post("/push?token=wrong", json={})
        assert response.status_code == 403
    finally:
        push.PUSH_TOKEN = ""



# --------------------------------------------------------------------------
# OAuth / PKCE
# --------------------------------------------------------------------------

DUMMY_CLIENT = {
    "web": {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "not-a-real-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:5000/oauth2callback"],
    }
}

REDIRECT = "http://localhost:5000/oauth2callback"


def _with_dummy_client():
    """Point the oauth module at a throwaway client file."""
    from applitrack.web import oauth

    path = os.path.join(_tmp, "dummy_client.json")
    Path(path).write_text(json.dumps(DUMMY_CLIENT), encoding="utf-8")
    oauth.CLIENT_SECRETS = path
    return oauth


def test_authorization_url_hands_back_a_code_verifier():
    """Regression: the verifier must escape the function, or the token
    exchange later fails with 'Missing code verifier'."""
    oauth = _with_dummy_client()

    result = oauth.authorization_url(REDIRECT)
    assert len(result) == 3, f"expected (url, state, code_verifier), got {len(result)} values"

    url, state, verifier = result
    assert url.startswith("https://accounts.google.com/o/oauth2/auth")
    assert state
    assert verifier, "no code_verifier returned - PKCE would break the exchange"
    assert "code_challenge=" in url, "PKCE challenge missing from the sign-in URL"


def test_code_verifier_survives_into_the_exchange_flow():
    """The second Flow must carry the first one's verifier."""
    oauth = _with_dummy_client()

    _url, state, verifier = oauth.authorization_url(REDIRECT)

    # exchange_code builds a fresh Flow; it must be seeded with the verifier.
    flow = oauth._flow(REDIRECT, state=state, code_verifier=verifier)
    assert flow.code_verifier == verifier, (
        "verifier did not reach the exchange Flow - this is the exact bug that "
        "produced '(invalid_grant) Missing code verifier'"
    )


def test_login_route_stores_the_verifier_in_the_session():
    oauth = _with_dummy_client()
    app = _app()
    client = app.test_client()

    response = client.get("/login")
    assert response.status_code == 302
    assert "accounts.google.com" in response.headers["Location"]

    with client.session_transaction() as sess:
        assert sess.get("oauth_state"), "state not stored"
        assert sess.get("oauth_code_verifier"), "code_verifier not stored in session"


# --------------------------------------------------------------------------
# Gmail rate limiting
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status):
        self.status = status


class _FakeHttpError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message)
        self.resp = _FakeResp(status)


def test_rate_limit_errors_are_retryable_but_real_403s_are_not():
    from applitrack.gmail_client import is_retryable

    quota = _FakeHttpError(403, "Quota exceeded for quota metric 'Total Query Cost'")
    assert is_retryable(quota), "a quota 403 must be retried"

    assert is_retryable(_FakeHttpError(429, "too many requests"))
    assert is_retryable(_FakeHttpError(503, "backendError"))

    forbidden = _FakeHttpError(403, "Request had insufficient authentication scopes")
    assert not is_retryable(forbidden), "a permissions 403 must NOT be retried forever"
    assert not is_retryable(_FakeHttpError(404, "not found"))
    assert not is_retryable(ValueError("unrelated"))


def test_retry_backs_off_then_succeeds():
    import applitrack.gmail_client as gc

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeHttpError(403, "Quota exceeded for quota metric")
        return "ok"

    slept = []
    original_sleep = gc.time.sleep
    gc.time.sleep = lambda s: slept.append(s)
    try:
        assert gc.retry(flaky, "test") == "ok"
    finally:
        gc.time.sleep = original_sleep

    assert calls["n"] == 3, f"expected 3 attempts, got {calls['n']}"
    assert len(slept) == 2, "should have backed off between attempts"
    assert slept[1] > slept[0], "delay should grow"


def test_retry_gives_up_on_a_permanent_error_immediately():
    import applitrack.gmail_client as gc

    calls = {"n": 0}

    def forbidden():
        calls["n"] += 1
        raise _FakeHttpError(403, "insufficient authentication scopes")

    try:
        gc.retry(forbidden, "test")
        raise AssertionError("should have raised")
    except _FakeHttpError:
        pass
    assert calls["n"] == 1, "a permanent 403 must not be retried"


# --------------------------------------------------------------------------
# Cross-process sync locking
# --------------------------------------------------------------------------

def _lock_class(tmpdir):
    import applitrack.web.service as svc
    svc.LOCK_DIR = Path(tmpdir)
    return svc.ProcessLock


def test_only_one_process_can_hold_a_users_sync_lock():
    """Regression: a threading.Lock does not span processes, so two syncs of
    one mailbox ran together and doubled the Gmail request rate."""
    d = os.path.join(_tmp, "locks1")
    ProcessLock = _lock_class(d)

    first = ProcessLock("user-123")
    second = ProcessLock("user-123")

    assert first.acquire() is True
    assert second.acquire() is False, "two holders of the same user lock"

    first.release()
    assert second.acquire() is True, "lock not released"
    second.release()


def test_different_users_do_not_block_each_other():
    d = os.path.join(_tmp, "locks2")
    ProcessLock = _lock_class(d)

    a = ProcessLock("alice")
    b = ProcessLock("bob")
    assert a.acquire() is True
    assert b.acquire() is True, "one user's sync blocked another's"
    a.release()
    b.release()


def test_a_stale_lock_is_reclaimed():
    """A crashed sync must not block that user forever."""
    import applitrack.web.service as svc

    d = os.path.join(_tmp, "locks3")
    ProcessLock = _lock_class(d)

    dead = ProcessLock("ghost")
    assert dead.acquire() is True

    # Pretend the holder died well beyond the staleness window.
    old = time.time() - (svc.LOCK_STALE_SECONDS + 60)
    os.utime(dead.path, (old, old))

    fresh = ProcessLock("ghost")
    assert fresh.acquire() is True, "stale lock was never reclaimed"
    fresh.release()


def test_lock_is_released_even_when_the_sync_raises():
    d = os.path.join(_tmp, "locks4")
    ProcessLock = _lock_class(d)

    try:
        with ProcessLock("boom") as lock:
            assert lock.acquire() is True
            raise RuntimeError("sync blew up")
    except RuntimeError:
        pass

    after = ProcessLock("boom")
    assert after.acquire() is True, "lock leaked after an exception"
    after.release()


# --------------------------------------------------------------------------
# Sheets request shape
# --------------------------------------------------------------------------

class _FakeSheets:
    """Records batchUpdate payloads instead of calling Google."""

    def __init__(self):
        self.requests = []

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId=None, **kw):
        self._result = {"sheets": [{"properties": {"title": "Applications", "sheetId": 0}},
                                   {"properties": {"title": "Summary", "sheetId": 1}}]}
        return self

    def values(self):
        return self

    def clear(self, **kw):
        self._result = {}
        return self

    def update(self, **kw):
        self._result = {}
        return self

    def batchUpdate(self, spreadsheetId=None, body=None, **kw):
        self.requests.extend((body or {}).get("requests", []))
        self._result = {}
        return self

    def execute(self):
        return getattr(self, "_result", {})


def _walk(node, path=""):
    """Yield (path, key) for every key in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield path, k
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, path)


def test_sheet_formatting_puts_text_colour_inside_textformat():
    """Regression: foregroundColorStyle at CellFormat level makes Google
    reject the whole batchUpdate with 'Unknown name'."""
    from applitrack.sheets import SheetsWriter

    fake = _FakeSheets()
    writer = SheetsWriter(service=fake, spreadsheet_id="sheet-1")

    signal = classify(ALICE_MAIL)
    writer.write(build_applications([signal]), summary=None)

    assert fake.requests, "no formatting requests were sent"

    bad = [p for p, k in _walk(fake.requests)
           if k == "foregroundColorStyle" and not p.endswith("textFormat")]
    assert not bad, (
        f"foregroundColorStyle found outside textFormat at: {bad}. "
        "Google rejects the entire request when it sits on userEnteredFormat."
    )

    good = [p for p, k in _walk(fake.requests)
            if k == "foregroundColorStyle" and p.endswith("textFormat")]
    assert good, "header text colour was dropped entirely"


# --------------------------------------------------------------------------
# Chunked first sync (for hosts with a hard request timeout)
# --------------------------------------------------------------------------

def test_backlog_drives_the_dashboard_continue_loop():
    """A capped sync must tell the page there is more to come."""
    import applitrack.web.app as webapp

    app = _app()
    users = app.user_store
    users.upsert("chunky", "chunky@example.com", "Chunk Tester",
                 json.dumps({"refresh_token": "x"}))
    users.update("chunky", last_sync_at="2026-10-20T00:00:00")

    store = Store(webapp.DB_PATH, user_id="chunky")
    signal = classify(ALICE_MAIL)
    store.record_signal(signal)
    store.replace_applications(build_applications([signal]))
    store.set_meta("backlog", "431")
    store.commit()
    store.close()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "chunky"

    body = client.get("/dashboard").get_data(as_text=True)
    assert "431" in body, "backlog count not shown"
    assert "/continue" in body, "page has no way to fetch the next chunk"
    assert "emails left to read" in body


def test_no_backlog_means_no_continue_loop():
    import applitrack.web.app as webapp

    app = _app()
    users = app.user_store
    users.upsert("clean", "clean@example.com", "Clean",
                 json.dumps({"refresh_token": "x"}))
    users.update("clean", last_sync_at="2026-10-20T00:00:00")

    store = Store(webapp.DB_PATH, user_id="clean")
    signal = classify(ALICE_MAIL)
    store.record_signal(signal)
    store.replace_applications(build_applications([signal]))
    store.set_meta("backlog", "0")
    store.commit()
    store.close()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "clean"

    body = client.get("/dashboard").get_data(as_text=True)
    assert "emails left to read" not in body
    assert "ACME" in body, "finished tracker should render normally"


def test_sync_result_knows_when_it_is_incomplete():
    from applitrack.web.service import SyncResult

    partial = SyncResult(ok=True, fetched=250, remaining=550)
    assert partial.complete is False

    finished = SyncResult(ok=True, fetched=250, remaining=0)
    assert finished.complete is True

    failed = SyncResult(ok=False, error="boom", remaining=0)
    assert failed.complete is False


# --------------------------------------------------------------------------
# Outbound proxy (PythonAnywhere free tier)
# --------------------------------------------------------------------------

def _clear_proxy_env():
    for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "no_proxy", "NO_PROXY", "APPLITRACK_PROXY",
              "PYTHONANYWHERE_DOMAIN", "PYTHONANYWHERE_SITE"):
        os.environ.pop(v, None)


def test_pythonanywhere_is_detected_and_proxied():
    """Regression: without this the token exchange dies with
    '[Errno 101] Network is unreachable' after a successful Google sign-in."""
    from applitrack.env import PYTHONANYWHERE_PROXY, configure_proxy

    _clear_proxy_env()
    os.environ["PYTHONANYWHERE_DOMAIN"] = "pythonanywhere.com"
    try:
        applied = configure_proxy()
        assert applied == PYTHONANYWHERE_PROXY
        assert os.environ["https_proxy"] == PYTHONANYWHERE_PROXY
        assert os.environ["HTTPS_PROXY"] == PYTHONANYWHERE_PROXY
        # Loopback must stay direct or health checks round-trip the proxy.
        assert "127.0.0.1" in os.environ["no_proxy"]
    finally:
        _clear_proxy_env()


def test_no_proxy_is_set_on_an_ordinary_machine():
    from applitrack.env import configure_proxy

    _clear_proxy_env()
    try:
        assert configure_proxy() is None
        assert "https_proxy" not in os.environ
    finally:
        _clear_proxy_env()


def test_explicit_proxy_wins_and_host_proxy_is_never_overridden():
    from applitrack.env import configure_proxy

    _clear_proxy_env()
    os.environ["PYTHONANYWHERE_DOMAIN"] = "pythonanywhere.com"
    os.environ["APPLITRACK_PROXY"] = "http://my.proxy:8080"
    try:
        assert configure_proxy() == "http://my.proxy:8080"
    finally:
        _clear_proxy_env()

    # A proxy the host already set must survive untouched.
    _clear_proxy_env()
    os.environ["PYTHONANYWHERE_DOMAIN"] = "pythonanywhere.com"
    os.environ["https_proxy"] = "http://host.set:3128"
    try:
        configure_proxy()
        assert os.environ["https_proxy"] == "http://host.set:3128"
    finally:
        _clear_proxy_env()

# --------------------------------------------------------------------------

def run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}\n        {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
