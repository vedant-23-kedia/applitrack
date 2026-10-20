"""The web service.

Routes:

    GET  /                  landing page, "Connect Gmail" button
    GET  /login             start Google OAuth
    GET  /oauth2callback    finish it, then sync in the background
    GET  /dashboard         the person's own applications
    POST /refresh           sync now
    POST /settings          notification preferences
    POST /disconnect        revoke the token and delete their data
    POST /push              Gmail -> Pub/Sub webhook (no session)
    GET  /healthz           liveness

Sessions hold nothing but the Google account id; everything else is looked
up per request, so a stolen cookie cannot carry a token.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, abort, flash, redirect, render_template, request, session, url_for
)

from ..env import configure_proxy, load_dotenv
from ..pipeline import summarise
from ..store import Store
from . import oauth, push
from .mailer import configured as mailer_configured
from .scheduler import Scheduler
from .service import send_welcome, sync_user
from .users import UserStore

# Before anything reads os.environ. A real shell variable still wins.
load_dotenv()
# Some hosts (PythonAnywhere free) have no direct outbound route.
_PROXY = configure_proxy()

log = logging.getLogger("applitrack.web")

DB_PATH = os.environ.get("APPLITRACK_DB", "data/applitrack.db")
USERS_DB = os.environ.get("APPLITRACK_USERS_DB", "data/users.db")


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = _session_secret()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("APPLITRACK_HTTPS", "1") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    )

    user_store = UserStore(USERS_DB)
    app.user_store = user_store

    scheduler = Scheduler(
        user_store,
        db_path=DB_PATH,
        poll_minutes=int(os.environ.get("APPLITRACK_POLL_MINUTES", "15")),
        send_daily_digest=os.environ.get("APPLITRACK_DAILY_DIGEST", "0") == "1",
    )
    if os.environ.get("APPLITRACK_SCHEDULER", "1") == "1":
        scheduler.start()
    app.scheduler = scheduler

    _register_routes(app, user_store)
    return app


def _session_secret() -> str:
    secret = os.environ.get("APPLITRACK_FLASK_SECRET", "").strip()
    if secret:
        return secret
    # Derive from the encryption key so sessions survive a restart.
    key = os.environ.get("APPLITRACK_SECRET_KEY", "").strip()
    if key:
        import hashlib

        return hashlib.sha256(("flask:" + key).encode()).hexdigest()
    raise RuntimeError(
        "Set APPLITRACK_SECRET_KEY (and optionally APPLITRACK_FLASK_SECRET).\n"
        'Generate one with:\n  python -m applitrack.web.cli keygen\n'
    )


# --------------------------------------------------------------------------


def _register_routes(app: Flask, user_store: UserStore):

    def current_user():
        user_id = session.get("user_id")
        return user_store.get(user_id) if user_id else None

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("index"))
            return view(user, *args, **kwargs)
        return wrapper

    def redirect_uri() -> str:
        configured = os.environ.get("APPLITRACK_REDIRECT_URI", "").strip()
        return configured or url_for("oauth2callback", _external=True)

    # -- public ------------------------------------------------------------

    @app.route("/")
    def index():
        if current_user():
            return redirect(url_for("dashboard"))
        return render_template(
            "landing.html",
            user_count=user_store.count(),
            realtime=push.push_configured(),
        )

    @app.route("/login")
    def login():
        try:
            url, state, code_verifier = oauth.authorization_url(redirect_uri())
        except FileNotFoundError as exc:
            return render_template("error.html", message=str(exc)), 500
        session["oauth_state"] = state
        # PKCE: the exchange in /oauth2callback has to present this same value.
        session["oauth_code_verifier"] = code_verifier
        return redirect(url)

    @app.route("/oauth2callback")
    def oauth2callback():
        if request.args.get("error"):
            flash("Sign-in was cancelled.", "warn")
            return redirect(url_for("index"))

        state = session.pop("oauth_state", None)
        code_verifier = session.pop("oauth_code_verifier", None)
        if not state or state != request.args.get("state"):
            flash("Sign-in expired. Please try again.", "warn")
            return redirect(url_for("index"))

        try:
            creds = oauth.exchange_code(
                redirect_uri(), request.url.replace("http://", "https://", 1)
                if os.environ.get("APPLITRACK_HTTPS", "1") == "1" else request.url,
                state,
                code_verifier=code_verifier,
            )
            who = oauth.identity(creds)
        except Exception as exc:
            log.exception("oauth exchange failed")
            return render_template("error.html", message=f"Google sign-in failed: {exc}"), 400

        is_new = user_store.get(who["user_id"]) is None
        user = user_store.upsert(
            who["user_id"], who["email"], who["name"],
            oauth.credentials_to_json(creds),
        )
        session["user_id"] = user.user_id
        session.permanent = True

        # The first read takes a while; do it off the request.
        threading.Thread(
            target=_first_sync, args=(user.user_id, user_store, is_new),
            daemon=True, name=f"firstsync-{user.email}",
        ).start()

        return redirect(url_for("dashboard", welcome=1 if is_new else None))

    # -- the person's own view --------------------------------------------

    @app.route("/dashboard")
    @login_required
    def dashboard(user):
        store = Store(DB_PATH, user_id=user.user_id)
        try:
            apps = store.all_applications()
            summary = summarise(apps)
            recent = store.recent_changes(
                datetime.now(timezone.utc).replace(hour=0, minute=0) )
            backlog = int(store.get_meta("backlog", "0") or 0)
        finally:
            store.close()

        # A failed sync must not leave the page spinning forever.
        syncing = not apps and not user.last_sync_at and not user.last_error
        return render_template(
            "dashboard.html",
            user=user,
            apps=apps,
            summary=summary,
            changes=recent,
            syncing=syncing,
            welcome=request.args.get("welcome"),
            realtime=push.push_configured() and bool(user.watch_expires_at),
            email_on=mailer_configured(),
            backlog=backlog,
        )

    @app.route("/refresh", methods=["POST"])
    @login_required
    def refresh(user):
        result = sync_user(
            user, user_store, DB_PATH,
            force_full=request.form.get("full") == "1",
        )
        if result.skipped:
            flash("A sync is already running.", "info")
        elif result.ok:
            flash(
                f"Up to date - {result.applications} applications"
                + (f", {len(result.changes)} changed" if result.changes else ""),
                "ok",
            )
        else:
            flash(f"Sync failed: {result.error}", "warn")
        return redirect(url_for("dashboard"))

    @app.route("/continue", methods=["POST"])
    @login_required
    def continue_sync(user):
        """Fetch the next chunk of a first read.

        Hosts with a hard request timeout cannot do a year of mail in one
        go, so the page calls this repeatedly until the backlog is clear.
        """
        result = sync_user(user, user_store, DB_PATH, notify=False)
        return {
            "ok": result.ok,
            "skipped": result.skipped,
            "fetched": result.fetched,
            "remaining": result.remaining,
            "applications": result.applications,
            "complete": result.complete,
            "error": result.error,
        }

    @app.route("/settings", methods=["POST"])
    @login_required
    def settings(user):
        user_store.update(
            user.user_id,
            notify_email=request.form.get("notify_email") == "on",
            sync_enabled=request.form.get("sync_enabled") == "on",
            lookback_days=max(30, min(1095, int(request.form.get("lookback_days", 365)))),
        )
        flash("Settings saved.", "ok")
        return redirect(url_for("dashboard"))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/disconnect", methods=["POST"])
    @login_required
    def disconnect(user):
        """Revoke access and erase everything we hold about this person."""
        creds = oauth.load_credentials(user_store, user.user_id)
        if creds:
            try:
                from .. import net as _net

                push.stop_watch(_net.build_service("gmail", "v1", creds))
            except Exception:
                pass
            oauth.revoke(creds)

        store = Store(DB_PATH, user_id=user.user_id)
        try:
            store.purge_user()
        finally:
            store.close()

        user_store.disconnect(user.user_id)
        session.clear()
        flash("Disconnected. Everything stored about you has been deleted; "
              "your spreadsheet stays in your Drive.", "ok")
        return redirect(url_for("index"))

    # -- machine endpoints -------------------------------------------------

    @app.route("/push", methods=["POST"])
    def push_endpoint():
        """Gmail change notification, relayed by Pub/Sub.

        Always answers 204. A non-2xx makes Pub/Sub redeliver, and a bad
        payload will never succeed on a retry -- it would just loop.
        """
        if not push.token_ok(request.args.get("token")):
            abort(403)

        payload = push.parse_notification(request.get_json(silent=True) or {})
        if not payload:
            return "", 204

        user = user_store.get_by_email(payload["emailAddress"])
        if not user or not user.sync_enabled:
            return "", 204

        threading.Thread(
            target=_push_sync, args=(user.user_id, user_store),
            daemon=True, name=f"push-{user.email}",
        ).start()
        return "", 204

    @app.route("/healthz")
    def healthz():
        # ?probe=1 additionally checks that this host can actually reach
        # Google. Sign-in fails confusingly without it: the browser half
        # succeeds, then the server cannot swap the code for a token.
        probe = None
        if request.args.get("probe"):
            probe = _probe_outbound()

        return {
            "ok": True,
            "proxy": _PROXY or False,
            "outbound": probe,
            "users": user_store.count(),
            "realtime": push.push_configured(),
            "email": mailer_configured(),
        }

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", message="Page not found."), 404


# --------------------------------------------------------------------------


def _probe_outbound() -> dict:
    """Can this host reach the Google endpoints the app depends on?

    Any HTTP status means reachable -- 400 or 405 from an endpoint that
    expects a POST still proves the connection got through. Only a transport
    error means blocked.
    """
    from .. import net

    sess = net.session()
    targets = {
        "token": "https://oauth2.googleapis.com/token",
        "gmail": "https://gmail.googleapis.com/$discovery/rest?version=v1",
        "sheets": "https://sheets.googleapis.com/$discovery/rest?version=v4",
    }
    out = {}
    for name, url in targets.items():
        try:
            out[name] = sess.get(url, timeout=12).status_code
        except Exception as exc:
            out[name] = f"FAIL: {type(exc).__name__}"
    out["reachable"] = all(isinstance(v, int) for v in out.values() if v != out.get("reachable"))
    return out


def _first_sync(user_id: str, user_store: UserStore, is_new: bool):
    """Full read after someone connects, then the welcome email."""
    user = user_store.get(user_id)
    if not user:
        return
    result = sync_user(user, user_store, DB_PATH, force_full=True, notify=False)
    if not result.ok:
        log.warning("first sync failed for %s: %s", user.email, result.error)
        return
    if is_new:
        user = user_store.get(user_id)          # picks up the new sheet id
        store = Store(DB_PATH, user_id=user_id)
        try:
            send_welcome(user, store.all_applications())
        finally:
            store.close()


def _push_sync(user_id: str, user_store: UserStore):
    user = user_store.get(user_id)
    if user:
        sync_user(user, user_store, DB_PATH)


app = None


def main():
    logging.basicConfig(
        level=os.environ.get("APPLITRACK_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = create_app()
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("APPLITRACK_HOST", "127.0.0.1")
    application.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
