"""The part that means you do not have to read your inbox.

A digest answers three questions in order of urgency: what needs me to do
something, what changed since last time, and what has gone quiet. Everything
else is a footnote.

Renders to the terminal, Markdown, or HTML email, and can raise a Windows
toast so a status change reaches you without opening anything.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from .models import Application, Status

ICONS = {
    Status.OFFER: "[OFFER]",
    Status.INTERVIEW: "[INTERVIEW]",
    Status.ASSESSMENT: "[ASSESSMENT]",
    Status.ACTION_REQUIRED: "[ACTION]",
    Status.ACKNOWLEDGED: "[ack]",
    Status.APPLIED: "[applied]",
    Status.REJECTED: "[rejected]",
    Status.GHOSTED: "[quiet]",
    Status.WITHDRAWN: "[withdrawn]",
}


def build_digest(apps: list[Application], summary: dict, changes: list | None = None) -> dict:
    """Structure the digest once; each renderer formats the same dict."""
    needs_action = sorted(
        summary.get("needs_action", []),
        key=lambda a: (-a.status.rank, -a.last_update.timestamp()),
    )
    recent = sorted(summary.get("recent", []), key=lambda a: -a.last_update.timestamp())
    stale = sorted(summary.get("stale", []), key=lambda a: a.last_update.timestamp())

    return {
        "generated": datetime.now(timezone.utc),
        "total": summary.get("total", len(apps)),
        "active": summary.get("active", 0),
        "by_status": summary.get("by_status", {}),
        "response_rate": summary.get("response_rate", 0.0),
        "needs_action": needs_action,
        "recent": recent,
        "stale": stale[:10],
        "changes": changes or [],
    }


# -- terminal ---------------------------------------------------------------


def render_console(digest: dict, console=None) -> None:
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:
        print(render_markdown(digest))
        return

    console = console or Console()
    d = digest

    console.print()
    console.print(
        Panel.fit(
            f"[bold]{d['total']}[/bold] applications   "
            f"[bold cyan]{d['active']}[/bold cyan] in progress   "
            f"[bold]{d['response_rate']:.0%}[/bold] got a real response",
            title="Job Application Tracker",
            subtitle=d["generated"].strftime("%Y-%m-%d %H:%M UTC"),
        )
    )

    if d["needs_action"]:
        table = Table(title="\nNeeds you", title_justify="left", header_style="bold yellow")
        table.add_column("Company")
        table.add_column("Role")
        table.add_column("Stage")
        table.add_column("Since", justify="right")
        for app in d["needs_action"]:
            table.add_row(
                app.company, _short(app.role, 38),
                f"[yellow]{app.status.value}[/yellow]",
                f"{app.days_since_update}d",
            )
        console.print(table)

    if d["changes"]:
        table = Table(title="\nChanged since last sync", title_justify="left", header_style="bold green")
        table.add_column("Company")
        table.add_column("Role")
        table.add_column("Was")
        table.add_column("Now")
        for change in d["changes"]:
            table.add_row(
                change["company"], _short(change["role"], 34),
                f"[dim]{change['old']}[/dim]", f"[bold]{change['new']}[/bold]",
            )
        console.print(table)

    if d["recent"]:
        table = Table(title="\nLast 7 days", title_justify="left", header_style="bold blue")
        table.add_column("Date")
        table.add_column("Company")
        table.add_column("Role")
        table.add_column("Status")
        for app in d["recent"][:15]:
            table.add_row(
                app.last_update.strftime("%b %d"), app.company,
                _short(app.role, 34), _coloured(app.status),
            )
        console.print(table)

    if d["stale"]:
        table = Table(title="\nGone quiet - worth a follow-up", title_justify="left", header_style="bold magenta")
        table.add_column("Company")
        table.add_column("Role")
        table.add_column("Silent for", justify="right")
        for app in d["stale"]:
            table.add_row(app.company, _short(app.role, 38), f"{app.days_since_update}d")
        console.print(table)

    console.print()


def _coloured(status: Status) -> str:
    colors = {
        Status.OFFER: "bold green", Status.INTERVIEW: "cyan",
        Status.ASSESSMENT: "yellow", Status.ACTION_REQUIRED: "yellow",
        Status.REJECTED: "red", Status.GHOSTED: "dim",
    }
    return f"[{colors.get(status, 'white')}]{status.value}[/]"


def _short(text: str, width: int) -> str:
    # Plain "..." rather than an ellipsis character: the Windows console
    # still defaults to cp1252 and mangles it.
    text = text or ""
    return text if len(text) <= width else text[: width - 3] + "..."


# -- markdown ---------------------------------------------------------------


def render_markdown(digest: dict) -> str:
    d = digest
    out = [
        "# Job Application Tracker",
        "",
        f"_{d['generated']:%Y-%m-%d %H:%M UTC}_",
        "",
        f"**{d['total']}** applications · **{d['active']}** in progress · "
        f"**{d['response_rate']:.0%}** got a real response",
        "",
    ]

    if d["needs_action"]:
        out += ["## Needs you", "", "| Company | Role | Stage | Since |", "|---|---|---|---|"]
        out += [
            f"| {a.company} | {a.role} | {a.status.value} | {a.days_since_update}d |"
            for a in d["needs_action"]
        ]
        out.append("")

    if d["changes"]:
        out += ["## Changed since last sync", "", "| Company | Role | Was | Now |", "|---|---|---|---|"]
        out += [
            f"| {c['company']} | {c['role']} | {c['old']} | **{c['new']}** |"
            for c in d["changes"]
        ]
        out.append("")

    if d["recent"]:
        out += ["## Last 7 days", "", "| Date | Company | Role | Status |", "|---|---|---|---|"]
        out += [
            f"| {a.last_update:%b %d} | {a.company} | {a.role} | {a.status.value} |"
            for a in d["recent"][:15]
        ]
        out.append("")

    if d["stale"]:
        out += ["## Gone quiet", "", "| Company | Role | Silent for |", "|---|---|---|"]
        out += [f"| {a.company} | {a.role} | {a.days_since_update}d |" for a in d["stale"]]
        out.append("")

    out += ["## Breakdown", ""]
    out += [f"- {status}: {count}" for status, count in sorted(d["by_status"].items())]
    return "\n".join(out)


# -- html email -------------------------------------------------------------

_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1c2024;
 max-width:760px;margin:0 auto;padding:24px;line-height:1.5}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px;
 text-transform:uppercase;letter-spacing:.04em;color:#5b6472}
.meta{color:#6b7280;font-size:13px;margin-bottom:20px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.stat{background:#f3f4f6;border-radius:8px;padding:10px 14px;min-width:92px}
.stat b{display:block;font-size:22px;line-height:1.2}
.stat span{font-size:12px;color:#6b7280}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;padding:7px 10px;background:#f9fafb;border-bottom:2px solid #e5e7eb;
 font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:#6b7280}
td{padding:7px 10px;border-bottom:1px solid #f0f1f3}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
.Offer{background:#d1fae5;color:#065f46}.Interview{background:#dbeafe;color:#1e40af}
.Assessment{background:#fef3c7;color:#92400e}
.ActionRequired{background:#fef3c7;color:#92400e}
.Rejected{background:#fee2e2;color:#991b1b}.Ghosted{background:#f3f4f6;color:#6b7280}
.Applied,.Acknowledged{background:#f3f4f6;color:#4b5563}
"""


def render_html(digest: dict) -> str:
    d = digest
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>Job Application Tracker</h1>",
        f"<div class='meta'>{d['generated']:%A, %d %B %Y, %H:%M UTC}</div>",
        "<div class='stats'>",
        f"<div class='stat'><b>{d['total']}</b><span>tracked</span></div>",
        f"<div class='stat'><b>{d['active']}</b><span>in progress</span></div>",
        f"<div class='stat'><b>{len(d['needs_action'])}</b><span>need you</span></div>",
        f"<div class='stat'><b>{d['response_rate']:.0%}</b><span>response rate</span></div>",
        "</div>",
    ]

    if d["needs_action"]:
        parts.append("<h2>Needs you</h2>")
        parts.append(_html_table(
            ["Company", "Role", "Stage", "Since"],
            [[a.company, a.role, _pill(a.status), f"{a.days_since_update}d"]
             for a in d["needs_action"]],
        ))

    if d["changes"]:
        parts.append("<h2>Changed since last sync</h2>")
        parts.append(_html_table(
            ["Company", "Role", "Was", "Now"],
            [[c["company"], c["role"], c["old"], f"<b>{c['new']}</b>"] for c in d["changes"]],
        ))

    if d["recent"]:
        parts.append("<h2>Last 7 days</h2>")
        parts.append(_html_table(
            ["Date", "Company", "Role", "Status"],
            [[f"{a.last_update:%b %d}", a.company, a.role, _pill(a.status)]
             for a in d["recent"][:15]],
        ))

    if d["stale"]:
        parts.append("<h2>Gone quiet &mdash; worth a follow-up</h2>")
        parts.append(_html_table(
            ["Company", "Role", "Silent for"],
            [[a.company, a.role, f"{a.days_since_update}d"] for a in d["stale"]],
        ))

    parts.append("</body></html>")
    return "".join(parts)


def _pill(status: Status) -> str:
    cls = status.value.replace(" ", "")
    return f"<span class='pill {cls}'>{status.value}</span>"


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


# -- delivery ---------------------------------------------------------------


def send_email(html: str, to: str, smtp: dict, subject: str | None = None) -> None:
    """Send the digest over SMTP.

    `smtp` needs host, port, user and password. For Gmail, generate an App
    Password -- your normal password will not work with 2FA enabled.
    """
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject or f"Job applications - {datetime.now():%d %b}"
    msg["From"] = smtp["user"]
    msg["To"] = to
    msg.set_content("This digest is best viewed as HTML.")
    msg.add_alternative(html, subtype="html")

    port = int(smtp.get("port", 587))
    if port == 465:
        with smtplib.SMTP_SSL(smtp["host"], port) as server:
            server.login(smtp["user"], smtp["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp["host"], port) as server:
            server.starttls()
            server.login(smtp["user"], smtp["password"])
            server.send_message(msg)


def notify_windows(title: str, message: str) -> bool:
    """Raise a Windows toast. Returns False if the shell call did not work."""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
        "$x = $t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode('{_ps_escape(title)}')) > $null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode('{_ps_escape(message)}')) > $null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AppliTrack').Show($n);"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True, capture_output=True, timeout=20,
        )
        return True
    except Exception:
        return False


def _ps_escape(text: str) -> str:
    return text.replace("'", "''").replace("\n", " ")[:220]
