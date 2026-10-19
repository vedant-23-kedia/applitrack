"""Operator commands for the hosted service.

    python -m applitrack.web.cli keygen      make an encryption key
    python -m applitrack.web.cli check       is everything configured?
    python -m applitrack.web.cli users       who is connected
    python -m applitrack.web.cli sync        sync everyone now
    python -m applitrack.web.cli digest      send digests now
    python -m applitrack.web.cli serve       run the web server
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    from ..env import configure_proxy, load_dotenv

    load_dotenv()          # a real shell variable still wins
    configure_proxy()

    parser = argparse.ArgumentParser(prog="applitrack-service")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("keygen", help="generate APPLITRACK_SECRET_KEY")
    sub.add_parser("check", help="verify configuration")
    sub.add_parser("users", help="list connected users")
    sub.add_parser("digest", help="send digests to everyone now")

    p_sync = sub.add_parser("sync", help="sync all users now")
    p_sync.add_argument("--email", help="only this user")
    p_sync.add_argument("--full", action="store_true", help="ignore the incremental cursor")

    p_serve = sub.add_parser("serve", help="run the web server")
    p_serve.add_argument("--host", default=os.environ.get("APPLITRACK_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")))

    args = parser.parse_args(argv)

    if args.command == "keygen":
        return cmd_keygen()
    if args.command == "check":
        return cmd_check()
    if args.command == "users":
        return cmd_users()
    if args.command == "sync":
        return cmd_sync(args)
    if args.command == "digest":
        return cmd_digest()
    if args.command == "serve":
        return cmd_serve(args)
    return 1


def cmd_keygen() -> int:
    from .users import generate_key

    key = generate_key()
    print(key)
    print()
    print("Set this and keep it safe. If you lose it, every user must reconnect:")
    print(f'  PowerShell:  $env:APPLITRACK_SECRET_KEY="{key}"')
    print(f'  bash:        export APPLITRACK_SECRET_KEY="{key}"')
    return 0


def cmd_check() -> int:
    from . import push
    from .mailer import configured as mail_configured, config as mail_config
    from .oauth import CLIENT_SECRETS, client_config, client_source

    rows = []

    key = os.environ.get("APPLITRACK_SECRET_KEY", "")
    rows.append(("Encryption key", bool(key),
                 "set" if key else "missing - run: keygen"))

    try:
        data = client_config()
    except ValueError as exc:
        data = None
        rows.append(("Google OAuth client", False, str(exc)))
    else:
        rows.append(("Google OAuth client", data is not None,
                     client_source() if data else f"{CLIENT_SECRETS} not found"))

    if data:
        kind = "web" if "web" in data else ("installed" if "installed" in data else "?")
        rows.append(("  client type", kind == "web",
                     f"{kind}" + ("" if kind == "web"
                                  else " - the service needs a 'Web application' client")))
        uris = (data.get(kind) or {}).get("redirect_uris", [])
        want = os.environ.get("APPLITRACK_REDIRECT_URI", "").strip()
        if want:
            rows.append(("  redirect registered", want in uris,
                         "yes" if want in uris
                         else f"{want} not in the client's redirect_uris "
                              "(Google's own setting is what counts - this is only the local copy)"))

    redirect = os.environ.get("APPLITRACK_REDIRECT_URI", "")
    rows.append(("Redirect URI", True,
                 redirect or "derived from the request host"))

    rows.append(("Email notifications", mail_configured(),
                 mail_config()["user"] or "not configured - users get no emails"))

    rows.append(("Real-time (Pub/Sub)", push.push_configured(),
                 push.PUBSUB_TOPIC or "not configured - falls back to polling"))

    if push.push_configured():
        rows.append(("  webhook token", bool(push.PUSH_TOKEN),
                     "set" if push.PUSH_TOKEN
                     else "missing - /push accepts anonymous posts"))

    from ..env import on_pythonanywhere
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if on_pythonanywhere() or proxy:
        rows.append(("Outbound proxy", bool(proxy),
                     proxy or "PythonAnywhere detected but no proxy set - "
                              "outbound calls will fail"))

    try:
        import flask  # noqa: F401
        rows.append(("Flask", True, "installed"))
    except ImportError:
        rows.append(("Flask", False, "missing - pip install flask"))

    try:
        import cryptography  # noqa: F401
        rows.append(("cryptography", True, "installed"))
    except ImportError:
        rows.append(("cryptography", False, "missing - pip install cryptography"))

    width = max(len(name) for name, _, _ in rows)
    blocking = 0
    for name, ok, detail in rows:
        mark = "OK  " if ok else "MISS"
        if not ok and name in ("Encryption key", "Google OAuth client", "Flask", "cryptography"):
            blocking += 1
        print(f"  [{mark}] {name.ljust(width)}  {detail}")

    print()
    if blocking:
        print(f"{blocking} blocking problem(s). The service will not start.")
        return 1
    print("Ready to serve.")
    return 0


def cmd_users() -> int:
    from .users import UserStore

    store = UserStore(os.environ.get("APPLITRACK_USERS_DB", "data/users.db"))
    users = store.all_active()
    if not users:
        print("No users connected yet.")
        return 0

    print(f"{'EMAIL':<36} {'SYNCED':<18} {'SHEET':<7} {'STATE'}")
    for user in users:
        synced = (user.last_sync_at[:16].replace("T", " ") if user.last_sync_at else "never")
        print(f"{user.email:<36} {synced:<18} "
              f"{'yes' if user.spreadsheet_id else 'no':<7} "
              f"{user.state}{' (' + user.last_error[:40] + ')' if user.last_error else ''}")
    print(f"\n{len(users)} active user(s)")
    return 0


def cmd_sync(args) -> int:
    from .service import sync_user
    from .users import UserStore

    store = UserStore(os.environ.get("APPLITRACK_USERS_DB", "data/users.db"))
    db_path = os.environ.get("APPLITRACK_DB", "data/applitrack.db")

    users = store.all_active()
    if args.email:
        users = [u for u in users if u.email.lower() == args.email.lower()]
        if not users:
            print(f"No active user with email {args.email}")
            return 1

    for user in users:
        result = sync_user(user, store, db_path, force_full=args.full)
        print(f"{user.email:<36} {result.summary_line}")
    return 0


def cmd_digest() -> int:
    from .scheduler import Scheduler
    from .users import UserStore

    store = UserStore(os.environ.get("APPLITRACK_USERS_DB", "data/users.db"))
    scheduler = Scheduler(store, db_path=os.environ.get("APPLITRACK_DB", "data/applitrack.db"))
    sent = scheduler.send_digests()
    print(f"Sent {sent} digest(s)")
    return 0


def cmd_serve(args) -> int:
    import logging

    logging.basicConfig(
        level=os.environ.get("APPLITRACK_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .app import create_app

    app = create_app()
    print(f"AppliTrack running on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
