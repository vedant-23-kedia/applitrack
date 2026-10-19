"""Pull company, role and job id out of an application email.

Company is the awkward one. When a company mails you directly the sending
domain gives it away, but most application mail arrives through an ATS, so
`rbs@myworkday.com` has to be resolved to "NatWest Group" from the body text.
We try the cheap, reliable sources first and fall back to prose patterns.
"""

from __future__ import annotations

import re

from .models import Email
from .rules import ATS_DOMAINS, AGGREGATOR_DOMAINS

# Legal suffixes and filler we strip off a captured company name.
_SUFFIXES = re.compile(
    r"\b(?:pvt\.?|private|ltd\.?|limited|llp|inc\.?|incorporated|corp\.?|"
    r"corporation|co\.?|gmbh|ag|plc|llc|s\.?a\.?|b\.?v\.?|technologies|"
    r"technology|solutions|services|systems|labs|group|global|holdings)\b\.?",
    re.I,
)

# Mail-infrastructure labels that are never a company name.
_NOT_A_COMPANY = {
    "the", "our", "your", "this", "that", "we", "us", "all", "any", "new",
    "dear", "hi", "hello", "thank", "thanks", "please", "note", "team",
    "careers", "recruiting", "talent", "hr", "no", "job", "jobs", "apply",
    "application", "applications", "candidate", "position", "role", "india",
    "email", "mail", "noreply", "donotreply", "updates", "notification",
    "gmail", "googlemail", "outlook", "yahoo", "hotmail",
}

# Sending domains that tell you nothing about the employer.
_GENERIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "yahoo.in", "rediffmail.com", "protonmail.com", "icloud.com",
    "sendgrid.net", "mailgun.org", "amazonses.com", "mandrillapp.com",
}

# "...your application for X at ACME", "...opportunity with ACME".
# The lead-in phrase is matched case-insensitively via a scoped (?i:...) group,
# but the captured name stays case-sensitive -- capitalisation is most of what
# distinguishes a company name from the surrounding prose.
# A name token stays case-sensitive and excludes "." so a capture stops at a
# sentence boundary ("...with KPMG India. Your candidate id..." -> "KPMG India").
# Tokens join on a space, "&" or "and", so "Larsen & Toubro" survives intact.
_TOKEN = r"[A-Z][\w&'\u2019-]*"
_JOIN = r"(?: and | ?& ?| )"
_NAME = r"(" + _TOKEN + r"(?:" + _JOIN + _TOKEN + r"){0,%d})"

_COMPANY_FROM_TEXT = [
    re.compile(r"(?i:\bjob at )" + _NAME % 3),
    re.compile(r"(?i:\bposition at )" + _NAME % 3),
    re.compile(r"(?i:\brole at )" + _NAME % 3),
    re.compile(r"(?i:\b(?:internship|intern) (?:at|with) )" + _NAME % 3),
    re.compile(r"(?i:\bcareer opportunity with )" + _NAME % 3),
    re.compile(r"(?i:\bopportunity (?:at|with) )" + _NAME % 3),
    re.compile(r"(?i:\binterest in (?:joining |a career (?:at|with) ))" + _NAME % 3),
    re.compile(r"(?i:\bjoining )" + _NAME % 3),
    re.compile(r"(?i:\bassignment from )" + _NAME % 4),
    re.compile(r"(?i:\bgreetings from )" + _NAME % 3),
    re.compile(r"(?i:\bshowcased towards )(?:[A-Z\u00c0-\u00ff]+ by )?" + _NAME % 4),
    re.compile(_NAME % 3 + r"(?i: is interested in your application)"),
    re.compile(r"(?i:\bthe )" + _NAME % 2 + r"(?i: campus recruitment)"),
    re.compile(_NAME % 3 + r"(?i: campus recruitment program)"),
    re.compile(r"(?i:\b(?:at|with) )" + _NAME % 3 + r"[.,]?(?i: (?:we|you will|our team))"),
    # ATS mail often names the employer only in a process heading, e.g.
    # "Deloitte US-India Offices recruitment process".
    re.compile(_NAME % 3 + r"(?i:[\w -]{0,24}recruitment process)"),
    # Campus drives relayed by a board: "Myntra RampUp SDE Internship Hiring".
    re.compile(_NAME % 0 + r"(?i:[\w ]{0,24}(?:internship hiring|hiring program|"
                           r"recruitment drive|campus drive|hiring drive))"),
    re.compile(r"(?i:\binterest in the )" + _NAME % 0),
]

# Words that attach to a company name in recruiting prose but are not part of
# it. Stripped from the end of a capture, repeatedly.
_TRAILING_NOISE = re.compile(
    r"\s+(?:offices?|coe|centre|center|careers?|campus|recruitment|recruiting|"
    r"hiring|team|talent|hr|programme?|portal)\.?$",
    re.I,
)

# "...for the position of ROLE", "Application Update: ROLE, 20004"
# Lead-ins reliable enough that whatever follows is the job title, even when
# it contains no recognisable role keyword ("the position of Campus28-UPES").
_ROLE_PATTERNS_TRUSTED = [
    re.compile(r"\bposition of ([^,.\n(]{3,60})", re.I),
    re.compile(r"\bthe role of ([^,.\n(]{3,60})", re.I),
    re.compile(r"\bapplication for (?:the )?([^,.\n(]{3,60}?)(?: position| role)\b", re.I),
    re.compile(r"\bapplying for (?:the )?([^,.\n(]{3,60}?)(?: position| role)\b", re.I),
    re.compile(r"\byour application as:?\s*([^,.\n(]{3,60})", re.I),
    re.compile(r"\bapplication to the role of ([^,.\n(]{3,60})", re.I),
]

# Looser lead-ins -- only trusted when the result reads like a job title.
_ROLE_PATTERNS = [
    re.compile(r"\bapplying (?:for|to) (?:the |an? )?([A-Z][^,.\n(]{3,60})"),
    re.compile(r"\byour application for ([^,.\n(]{3,60})", re.I),
    re.compile(r"\binterview for (?:an? )?([^,.\n(]{3,40}?) (?:with|at)\b", re.I),
    re.compile(r"\bhiring ([A-Z][^,.\n(]{3,50}?) (?:intern|role|position)\b"),
    re.compile(r"\bapply for (?:the )?([^,.\n(]{3,60}?) role\b", re.I),
    re.compile(r"\binterest in (?:our|the) ([^,.\n(]{3,60}?) (?:job|role|position)\b", re.I),
]

_ROLE_FROM_SUBJECT = [
    re.compile(r"^Application Update:\s*(.+?)(?:,\s*\d+)?$", re.I),
    re.compile(r"^Your application \S+\s*-\s*(.+?)\s+update$", re.I),
    re.compile(r"\bfor the position of (.+?)(?:,|$)", re.I),
    re.compile(r"\b(?:position|role):\s*(.+?)(?:,|$)", re.I),
]

_ROLE_NOISE = re.compile(
    r"\b(?:id|job|number|no\.?|ref|req|requisition)\b\s*[:#-]?\s*[A-Z0-9-]*$", re.I
)

# A role title should look like one.
_ROLE_KEYWORDS = re.compile(
    r"\b(?:engineer|developer|analyst|scientist|intern|internship|manager|"
    r"consultant|associate|trainee|designer|architect|specialist|lead|"
    r"executive|officer|administrator|researcher|technician|graduate|"
    r"apprentice|manager|manag|manage|data|software|product|business|"
    r"marketing|sales|finance|operations|security|cloud|ml|ai|devops|"
    r"full.?stack|frontend|backend|qa|test|support|hr|praktikum)\b",
    re.I,
)


def clean_company(name: str) -> str:
    """Trim a captured company phrase down to something sheet-worthy."""
    if not name:
        return ""
    name = name.strip(" \t\r\n-–,.;:!|")
    name = re.sub(r"\s+", " ", name)
    # Drop a trailing clause the capture ran into.
    name = re.split(r"\s+(?:for|to|is|are|has|have|we|you|the)\b", name, maxsplit=1, flags=re.I)[0]
    name = name.strip(" ,.;:-")
    # "Deloitte US-India Offices" -> "Deloitte US-India"
    while True:
        trimmed = _TRAILING_NOISE.sub("", name).strip(" ,.;:-")
        if trimmed == name or not trimmed:
            break
        name = trimmed
    if not name or name.lower() in _NOT_A_COMPANY:
        return ""
    if len(name) > 60:
        return ""
    return name


def company_from_domain(domain: str) -> str:
    """`recruiting@dhl.com` -> `DHL`; `email.careers.microsoft.com` -> `Microsoft`."""
    parts = [p for p in domain.split(".") if p]
    if not parts:
        return ""
    # Walk past infrastructure labels to the registrable name.
    skip = {"email", "mail", "careers", "career", "jobs", "recruiting", "apply",
            "notify", "notification", "smtp", "mta", "e", "em", "go", "info", "my"}
    tld_like = {"com", "net", "org", "io", "co", "in", "de", "uk", "us", "ai",
                "gov", "edu", "ac", "events", "news", "app", "hr", "work"}
    candidates = [p for p in parts if p not in skip and p not in tld_like]
    if not candidates:
        candidates = [p for p in parts if p not in tld_like] or parts
    name = candidates[-1]
    if name.lower() in _NOT_A_COMPANY:
        return ""
    # `quest-global` -> `Quest-Global`, `ibm` -> `IBM`
    return "-".join(
        part.upper() if len(part) <= 4 else part.capitalize()
        for part in name.split("-")
    )


def extract_company(email: Email) -> tuple[str, str]:
    """Return `(company, source_label)` for an email.

    `source_label` records where the application came from -- the ATS or job
    board if one is involved, otherwise "Direct".
    """
    domain = email.domain
    root = email.root_domain

    ats = ATS_DOMAINS.get(root) or ATS_DOMAINS.get(domain)
    board = AGGREGATOR_DOMAINS.get(root) or AGGREGATOR_DOMAINS.get(domain)
    source = ats or board or "Direct"

    # A company that mails from its own domain has already told us who it is.
    if not ats and not board and root not in _GENERIC_MAIL_DOMAINS:
        from_domain = company_from_domain(domain)
        if from_domain:
            return from_domain, source

    # Otherwise read it out of the text.
    text = email.text
    for rx in _COMPANY_FROM_TEXT:
        m = rx.search(text)
        if m:
            name = clean_company(m.group(1))
            if name and name.lower() not in _NOT_A_COMPANY:
                return name, source

    # The display name is often the employer even when the domain is not --
    # but not when the client just echoed the mailbox name back at us.
    display = clean_company(_SUFFIXES.sub("", email.sender_name).strip())
    if (
        display
        and display.lower() not in _NOT_A_COMPANY
        and "@" not in display
        and display.lower() != email.local_part
        and display.lower() not in email.local_part
    ):
        return display, source

    fallback = company_from_domain(domain)
    return fallback or "Unknown", source


def extract_role(email: Email) -> str:
    """Best-effort job title."""
    for rx in _ROLE_FROM_SUBJECT:
        m = rx.search(email.subject)
        if m:
            role = _tidy_role(m.group(1))
            if role:
                return role

    for rx in _ROLE_PATTERNS_TRUSTED:
        m = rx.search(email.text)
        if m:
            role = _tidy_role(m.group(1))
            if role:
                return role

    for rx in _ROLE_PATTERNS:
        m = rx.search(email.text)
        if m:
            role = _tidy_role(m.group(1))
            if role and _ROLE_KEYWORDS.search(role):
                return role

    # Last resort: a keyword-bearing fragment of the subject.
    if _ROLE_KEYWORDS.search(email.subject):
        role = _tidy_role(re.sub(r"^\W+|\W+$", "", email.subject))
        if role:
            return role
    return "Unspecified"


def _tidy_role(role: str) -> str:
    role = role.strip(" \t\r\n-–,.;:!|\"'")
    role = re.sub(r"\s+", " ", role)
    # Drop a leading article the capture swept up.
    role = re.sub(r"^(?:the|a|an|our|your)\s+", "", role, flags=re.I)
    # A title ends where the job id or the employer begins:
    # "Software Engineer ID 358837 at Global Business Services" -> "Software Engineer"
    role = re.split(r"\s+(?:ID|at)\s+", role, maxsplit=1)[0]
    role = _ROLE_NOISE.sub("", role).strip(" ,.;:-")
    role = re.sub(r"\s*\(\s*(?:job number|id)[^)]*\)\s*$", "", role, flags=re.I)
    role = re.sub(r",?\s*\d{5,}$", "", role)
    role = role.strip(" ,.;:-")
    if len(role) < 3 or len(role) > 70:
        return ""
    if role.lower() in _NOT_A_COMPANY:
        return ""
    # "internship" -> "Internship"; leave mixed case ("iOS Developer") alone.
    if role.islower():
        role = role.title()
    return role


def extract_job_id(email: Email) -> str:
    """A requisition / job / candidate id, if the email carries one."""
    from .rules import RX_JOB_ID

    for rx in RX_JOB_ID:
        m = rx.search(email.text)
        if m:
            value = m.group(1).strip(" .,;:-")
            # Reject matches that are obviously a year or a tiny number.
            if value.isdigit() and (len(value) < 4 or 1990 <= int(value) <= 2100):
                continue
            if len(value) >= 3:
                return value.upper()
    return ""
