"""Command line entry point.

    applitrack auth                 one-time Google sign-in
    applitrack sync                 read mail, update tracker, export
    applitrack report               show the tracker without touching Gmail
    applitrack digest               the "do not read my inbox" summary
    applitrack explain "<subject>"  why an email was or was not counted
    applitrack watch --every 60     keep syncing in the background
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .models import Status


def main(argv: list[str] | None = None) -> int:
    from .env import configure_proxy, load_dotenv

    load_dotenv()
    configure_proxy()

    parser = argparse.ArgumentParser(
        prog="applitrack",
        description="Track job applications straight from your Gmail inbox.",
    )
    parser.add_argument("-c", "--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="run the Google sign-in flow once")

    p_sync = sub.add_parser("sync", help="read Gmail and refresh the tracker")
    p_sync.add_argument("--days", type=int, help="how far back to look")
    p_sync.add_argument("--max", type=int, dest="max_emails", help="cap on messages fetched")
    p_sync.add_argument("--sheet", action="store_true", help="also write the Google Sheet")
    p_sync.add_argument("--no-excel", action="store_true", help="skip the .xlsx export")
    p_sync.add_argument("--llm", action="store_true", help="use Claude on ambiguous emails")
    p_sync.add_argument("--notify", action="store_true", help="toast / email on changes")
    p_sync.add_argument("--full", action="store_true", help="re-read everything, ignore cache")
    p_sync.add_argument("--json", action="store_true", help="print results as JSON")

    p_report = sub.add_parser("report", help="show the stored tracker")
    p_report.add_argument("--status", help="filter to one status")
    p_report.add_argument("--open", action="store_true", help="only applications still live")
    p_report.add_argument("--json", action="store_true")

    p_digest = sub.add_parser("digest", help="summary of what needs you")
    p_digest.add_argument("--email", action="store_true", help="send it over SMTP")
    p_digest.add_argument("--html", metavar="PATH", help="write an HTML file")
    p_digest.add_argument("--markdown", metavar="PATH", help="write a Markdown file")

    p_explain = sub.add_parser("explain", help="show the scoring for matching emails")
    p_explain.add_argument("query", help="text to match in the subject")
    p_explain.add_argument("--days", type=int, default=365)

    p_watch = sub.add_parser("watch", help="sync on a loop")
    p_watch.add_argument("--every", type=int, default=60, help="minutes between syncs")
    p_watch.add_argument("--sheet", action="store_true")

    p_sheet = sub.add_parser("sheet", help="create or open the Google Sheet")
    p_sheet.add_argument("--create", action="store_true", help="make a new spreadsheet")

    args = parser.parse_args(argv)
    cfg = Config.load(args.config)

    try:
        return _dispatch(args, cfg)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except FileNotFoundError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2


def _dispatch(args, cfg: Config) -> int:
    if args.command == "auth":
        return cmd_auth(cfg)
    if args.command == "sync":
        return cmd_sync(args, cfg)
    if args.command == "report":
        return cmd_report(args, cfg)
    if args.command == "digest":
        return cmd_digest(args, cfg)
    if args.command == "explain":
        return cmd_explain(args, cfg)
    if args.command == "watch":
        return cmd_watch(args, cfg)
    if args.command == "sheet":
        return cmd_sheet(args, cfg)
    return 1


# --------------------------------------------------------------------------


def cmd_auth(cfg: Config) -> int:
    from .auth import get_credentials

    creds = get_credentials()
    print("Signed in. Token saved to token.json")
    print(f"Scopes: {', '.join(creds.scopes or [])}")
    return 0


def cmd_sync(args, cfg: Config) -> int:
    from .gmail_client import GmailClient
    from .pipeline import process_emails, summarise
    from .store import Store

    days = args.days or cfg.lookback_days
    max_emails = args.max_emails or cfg.max_emails

    print(f"Reading Gmail (last {days} days, up to {max_emails} messages)...")
    client = GmailClient()

    def progress(done, total):
        print(f"\r  fetched {done}/{total}", end="", flush=True)

    emails = client.search_and_fetch(
        since_days=days,
        extra_query=cfg.extra_query,
        max_results=max_emails,
        progress=progress,
    )
    print(f"\r  fetched {len(emails)} messages        ")

    if cfg.ignore_senders:
        ignore = {s.lower() for s in cfg.ignore_senders}
        emails = [e for e in emails if e.sender not in ignore and e.domain not in ignore]

    apps, signals, skipped = process_emails(emails, cfg.ghost_after_days)

    if args.llm or cfg.use_llm:
        apps, signals = _llm_pass(skipped, signals, cfg)

    if cfg.ignore_companies:
        ignore = {c.lower() for c in cfg.ignore_companies}
        apps = [a for a in apps if a.company.lower() not in ignore]

    summary = summarise(apps)
    changes = []

    with Store(cfg.db_path) as store:
        for signal in signals:
            store.record_signal(signal)
        for app in apps:
            previous = store.upsert_application(app)
            if previous:
                changes.append({
                    "company": app.company, "role": app.role,
                    "old": previous, "new": app.status.value,
                })
        store.set_meta("last_sync", datetime.now(timezone.utc).isoformat())
        store.commit()

    print(f"\n{len(signals)} application emails -> {len(apps)} applications "
          f"({len(skipped)} messages filtered out as noise)")

    # Exports
    if not args.no_excel:
        from .excel import write_csv, write_xlsx

        xlsx = write_xlsx(apps, cfg.xlsx_path, summary)
        csv_path = write_csv(apps, cfg.csv_path)
        print(f"Excel: {xlsx}")
        print(f"CSV:   {csv_path}")

    if args.sheet:
        url = _write_sheet(apps, summary, cfg)
        print(f"Sheet: {url}")

    # Report
    from .digest import build_digest, render_console

    digest = build_digest(apps, summary, changes)
    if args.json:
        print(json.dumps({
            "applications": [a.to_row() for a in apps],
            "summary": {k: v for k, v in summary.items()
                        if k in ("total", "active", "by_status", "response_rate")},
            "changes": changes,
        }, indent=2, default=str))
    else:
        render_console(digest)

    if args.notify and changes:
        _notify(digest, changes, cfg)

    return 0


def _llm_pass(skipped, signals, cfg: Config):
    """Re-examine near-threshold emails with Claude, then rebuild."""
    from .classify import classify_with_llm, score_email
    from .extract import extract_company
    from .models import Signal
    from .pipeline import build_applications

    ambiguous = [e for e in skipped if score_email(e).is_ambiguous][:40]
    if not ambiguous:
        return build_applications(signals, cfg.ghost_after_days), signals

    print(f"Asking Claude about {len(ambiguous)} ambiguous emails...")
    try:
        verdicts = classify_with_llm(ambiguous, cfg.llm_model)
    except RuntimeError as exc:
        print(f"  skipped: {exc}")
        return build_applications(signals, cfg.ghost_after_days), signals

    added = 0
    for email in ambiguous:
        verdict = verdicts.get(email.msg_id) or {}
        if not verdict.get("is_application"):
            continue
        try:
            status = Status(verdict.get("status", "Applied"))
        except ValueError:
            status = Status.APPLIED
        _, source = extract_company(email)
        signals.append(Signal(
            email=email, status=status,
            confidence=float(verdict.get("confidence", 0.6)),
            company=verdict.get("company") or "Unknown",
            role=verdict.get("role") or "Unspecified",
            source=source, reasons=["llm"],
        ))
        added += 1

    print(f"  Claude recovered {added} more")
    return build_applications(signals, cfg.ghost_after_days), signals


def _write_sheet(apps, summary, cfg: Config) -> str:
    from .sheets import SheetsWriter

    writer = SheetsWriter(spreadsheet_id=cfg.spreadsheet_id or None)
    if not cfg.spreadsheet_id:
        writer.create(cfg.spreadsheet_title)
        cfg.spreadsheet_id = writer.spreadsheet_id
        cfg.save()
        print(f"Created a new spreadsheet (id saved to {cfg.path})")
    return writer.write(apps, summary)


def _notify(digest, changes, cfg: Config):
    from .digest import notify_windows, render_html, send_email

    headline = ", ".join(f"{c['company']}: {c['new']}" for c in changes[:3])
    if cfg.notify.get("desktop", True):
        notify_windows(f"{len(changes)} application update(s)", headline)
    if cfg.can_email:
        send_email(render_html(digest), cfg.notify["email_to"], cfg.smtp)
        print(f"Digest emailed to {cfg.notify['email_to']}")


def cmd_report(args, cfg: Config) -> int:
    from .pipeline import summarise
    from .store import Store

    with Store(cfg.db_path) as store:
        apps = store.all_applications()

    if not apps:
        print("Nothing tracked yet. Run: applitrack sync")
        return 0

    if args.open:
        apps = [a for a in apps if a.status.is_open]
    if args.status:
        wanted = args.status.lower()
        apps = [a for a in apps if a.status.value.lower() == wanted]

    if args.json:
        print(json.dumps([a.to_row() for a in apps], indent=2, default=str))
        return 0

    from .digest import build_digest, render_console

    render_console(build_digest(apps, summarise(apps)))
    return 0


def cmd_digest(args, cfg: Config) -> int:
    from .digest import build_digest, render_console, render_html, render_markdown, send_email
    from .pipeline import summarise
    from .store import Store

    with Store(cfg.db_path) as store:
        apps = store.all_applications()
        last_sync = store.get_meta("last_sync")
        since = (
            datetime.fromisoformat(last_sync) - timedelta(days=1)
            if last_sync else datetime.now(timezone.utc) - timedelta(days=7)
        )
        rows = store.recent_changes(since)

    if not apps:
        print("Nothing tracked yet. Run: applitrack sync")
        return 0

    changes = [
        {"company": r["company"] or "?", "role": r["role"] or "",
         "old": r["old_status"], "new": r["new_status"]}
        for r in rows
    ]
    digest = build_digest(apps, summarise(apps), changes)

    if args.markdown:
        Path(args.markdown).write_text(render_markdown(digest), encoding="utf-8")
        print(f"Markdown: {Path(args.markdown).resolve()}")
    if args.html:
        Path(args.html).write_text(render_html(digest), encoding="utf-8")
        print(f"HTML: {Path(args.html).resolve()}")
    if args.email:
        if not cfg.can_email:
            print("Email not configured. Set notify.email_to and the smtp block "
                  "in config.yaml, or the APPLITRACK_SMTP_* environment variables.")
            return 2
        send_email(render_html(digest), cfg.notify["email_to"], cfg.smtp)
        print(f"Sent to {cfg.notify['email_to']}")

    if not (args.markdown or args.html or args.email):
        render_console(digest)
    return 0


def cmd_explain(args, cfg: Config) -> int:
    """Show why specific emails were kept or dropped -- the debugging tool."""
    from .classify import classify, score_email
    from .gmail_client import GmailClient

    client = GmailClient()
    emails = client.search_and_fetch(since_days=args.days, max_results=400)
    needle = args.query.lower()
    matches = [e for e in emails if needle in e.subject.lower() or needle in e.sender]

    if not matches:
        print(f"No email in the last {args.days} days matches {args.query!r}")
        return 1

    for email in matches[:20]:
        score = score_email(email)
        signal = classify(email)
        verdict = "COUNTED" if signal else "ignored"
        print(f"\n[{verdict}] score {score.value} (threshold {_threshold(cfg)})")
        print(f"  {email.date:%Y-%m-%d}  {email.sender}")
        print(f"  {email.subject}")
        for reason in score.reasons:
            print(f"    - {reason}")
        if signal:
            print(f"  -> {signal.company} | {signal.role} | {signal.status.value} "
                  f"| job id {signal.job_id or '-'} | {signal.confidence:.0%}")
    return 0


def _threshold(cfg: Config) -> int:
    from . import rules

    return cfg.min_score if cfg.min_score is not None else rules.MIN_SCORE


def cmd_watch(args, cfg: Config) -> int:
    interval = max(5, args.every) * 60
    print(f"Watching Gmail every {args.every} minutes. Ctrl-C to stop.")
    sync_args = argparse.Namespace(
        days=None, max_emails=None, sheet=args.sheet, no_excel=False,
        llm=False, notify=True, full=False, json=False,
    )
    while True:
        print(f"\n--- {datetime.now():%Y-%m-%d %H:%M} ---")
        try:
            cmd_sync(sync_args, cfg)
        except Exception as exc:
            print(f"Sync failed: {exc}")
        time.sleep(interval)


def cmd_sheet(args, cfg: Config) -> int:
    from .pipeline import summarise
    from .sheets import SheetsWriter
    from .store import Store

    with Store(cfg.db_path) as store:
        apps = store.all_applications()

    writer = SheetsWriter(spreadsheet_id=cfg.spreadsheet_id or None)
    if args.create or not cfg.spreadsheet_id:
        writer.create(cfg.spreadsheet_title)
        cfg.spreadsheet_id = writer.spreadsheet_id
        cfg.save()

    url = writer.write(apps, summarise(apps)) if apps else writer.url
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
