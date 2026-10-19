"""Sender and phrase rules that separate real application mail from job spam.

The hard part of this problem is not finding "thank you for applying" emails --
it is that job boards send marketing with the exact same vocabulary. A single
inbox can hold a hundred "Confirm your application" blasts from Internshala
and one genuine assessment invite, and only the second one matters.

So every email is scored. Positive signals come from applicant-tracking
systems, corporate recruiting addresses and transactional phrasing; negative
signals come from bulk-mail infrastructure, promotional phrasing, and
application-shaped mail about things that are not jobs (loans, passports,
tax returns). Only emails that clear `MIN_SCORE` reach the classifier.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Applicant tracking systems. Mail from these domains is almost always
# transactional -- the company name then has to come from the subject or body,
# because the sending domain belongs to the ATS vendor, not the employer.
# --------------------------------------------------------------------------
ATS_DOMAINS = {
    "myworkday.com": "Workday",
    "myworkdayjobs.com": "Workday",
    "myworkdaysite.com": "Workday",
    "greenhouse.io": "Greenhouse",
    "greenhouse-mail.io": "Greenhouse",
    "lever.co": "Lever",
    "ashbyhq.com": "Ashby",
    "smartrecruiters.com": "SmartRecruiters",
    "icims.com": "iCIMS",
    "taleo.net": "Taleo",
    "successfactors.com": "SuccessFactors",
    "sapsf.com": "SuccessFactors",
    "avature.net": "Avature",
    "jobvite.com": "Jobvite",
    "bamboohr.com": "BambooHR",
    "workable.com": "Workable",
    "workablemail.com": "Workable",
    "recruitee.com": "Recruitee",
    "teamtailor.com": "Teamtailor",
    "breezy.hr": "Breezy",
    "brassring.com": "BrassRing",
    "eightfold.ai": "Eightfold",
    "phenompeople.com": "Phenom",
    "oraclecloud.com": "Oracle Recruiting",
    "hirepro.in": "HirePro",
    "superset.work": "Superset",
    "peoplestrong.com": "PeopleStrong",
    "darwinbox.com": "Darwinbox",
    "zohorecruit.com": "Zoho Recruit",
    "freshteam.com": "Freshteam",
    "lntedutech.com": "L&T EduTech",
    "unstop.events": "Unstop",
    "hackerrank.com": "HackerRank",
    "hackerearth.com": "HackerEarth",
    "mettl.com": "Mercer Mettl",
    "codility.com": "Codility",
    "amcatmail.com": "AMCAT",
    "aspiringminds.com": "AMCAT",
}

# Job boards that send BOTH real application updates and heavy marketing.
# Mail from these is only accepted when it also carries a transactional
# phrase from TRANSACTIONAL_BOARD_PATTERNS below.
AGGREGATOR_DOMAINS = {
    "internshala.com": "Internshala",
    "mail.internshala.com": "Internshala",
    "updates.internshala.com": "Internshala",
    "naukri.com": "Naukri",
    "linkedin.com": "LinkedIn",
    "indeed.com": "Indeed",
    "indeedemail.com": "Indeed",
    "glassdoor.com": "Glassdoor",
    "monsterindia.com": "foundit",
    "foundit.in": "foundit",
    "instahyre.com": "Instahyre",
    "cutshort.io": "Cutshort",
    "wellfound.com": "Wellfound",
    "hirist.com": "Hirist",
    "shine.com": "Shine",
    "timesjobs.com": "TimesJobs",
    "apna.co": "Apna",
}

# Pure marketing senders. Nothing transactional has ever come from these.
NEVER_SENDERS = {
    "unstop.news",
    "news.unstop.com",
    "courses.codingninjas.com",
    "scaler.com",
    "upgrad.com",
    "jobalerts-noreply@linkedin.com",
    "jobs-listings@linkedin.com",
    "messages-noreply@linkedin.com",
    "updates-noreply@linkedin.com",
    "invitations@linkedin.com",
    "news-noreply@linkedin.com",
    "info@naukri.com",
    "resumenc@naukri.com",
    "eventnc@naukri.com",
    "alerts@naukri.com",
    "jos@mail.internshala.com",
    "studyabroad@updates.internshala.com",
}

# Recruiting mailbox names on a company's own domain.
RECRUITING_LOCALPARTS = {
    "careers", "career", "recruiting", "recruitment", "recruiter", "talent",
    "talentacquisition", "talentacquisitions", "jobs", "hiring", "hr", "people", "campus",
    "campushiring", "university", "universityrecruiting", "apply", "applications",
    "noreply-careers", "no-reply-careers", "donotreply", "do-not-reply", "job",
}

RECRUITING_SUBDOMAIN_HINTS = (
    "careers.", "jobs.", "recruiting.", "recruitment.", "talent.", "apply.",
    "hiring.", "campus.", "hire.", "myworkday",
)

# --------------------------------------------------------------------------
# Phrase signals
# --------------------------------------------------------------------------

# Unambiguous "you applied and we got it" phrasing.
STRONG_APPLICATION_PATTERNS = [
    r"thank(?:s| you)[^.]{0,40}\bfor applying\b",
    r"thank(?:s| you)[^.]{0,60}\byour application\b",
    r"\bwe(?:.ve| have)? received your application\b",
    r"\bwe(?:.ve| have)? got your application\b",
    r"\byour application (?:has been|was) (?:received|submitted|successfully)",
    r"\bapplication (?:received|submitted|confirmation)\b",
    r"\bsuccessfully (?:submitted|received) your\b.{0,30}application",
    r"\bthank you for (?:your interest in|submitting)\b.{0,60}\b(?:position|role|job|opportunity|vacancy)\b",
    r"\byour application for\b",
    r"\byour application to\b",
    r"\bfor applying (?:to|for)\b",
    r"\bapplying for the (?:position|role)\b",
    r"\bapplication id\b",
    r"\bcandidate id\b",
    r"\bapplication-?id\b",
    r"\byour candidature\b",
]

# Weaker, but meaningful in combination.
WEAK_APPLICATION_PATTERNS = [
    r"\bposition of\b",
    r"\bthe role of\b",
    r"\brequisition\b",
    r"\bjob (?:id|number|code|req)\b",
    r"\brecruitment process\b",
    r"\bhiring process\b",
    r"\btalent acquisition\b",
    r"\bcampus (?:recruitment|hiring|drive)\b",
    r"\byour profile has been\b",
]

# Marketing phrasing -- heavily penalised. Drawn from real job-board blasts
# that deliberately imitate transactional subject lines.
MARKETING_PATTERNS = [
    r"\bapply (?:now|today|for free)\b",
    r"\bis hiring\b",
    r"\bare hiring\b",
    r"\bnow hiring\b",
    r"\bhiring alert\b",
    r"\bjob alert\b",
    r"\binternship alert\b",
    r"\bopportunity alert\b",
    r"\bconfirm your application\b",
    r"\bconfirm your (?:spot|seat|details)\b",
    r"\bcomplete your details\b",
    r"\bregistration (?:closes|deadline|open)\b",
    r"\byour profile (?:is a|matches)\b",
    r"\bperfect match for\b",
    r"\bmatching your profile\b",
    r"\btop (?:internships|jobs|companies)\b",
    r"\b\d+\+? new (?:internships|jobs)\b",
    r"\bearn (?:high |up ?to )?(?:stipend|inr)",
    r"\bstipend up ?to\b",
    r"\bprize pool\b",
    r"\bwin .?\d+ ?(?:lakh|l\b|crore)",
    r"\bunsubscribe here\b",
    r"\bdeadline extended\b",
    r"\blast (?:day|few days) to (?:enroll|register|apply)\b",
    r"\bonly \d+ days? left\b",
    r"\b\d+ days? left\b",
    r"\bwebinar\b",
    r"\bmasterclass\b",
    r"\bplacement assistance\b",
    r"\bupdate your resume\b",
    r"\bmissing job shortlists\b",
    r"\bexplore (?:jobs|opportunities)\b",
    r"\bkeep exploring\b",
    r"\brecommended for you\b",
    r"\bpicked for you\b",
    r"\byou are eligible\b",
    r"\bppi (?:opportunit|access|offer)",
    r"\bcampus ambassador\b",
    r"\bquiz\b",
    r"\bcoding contest\b",
    r"\bhackathon\b",
    r"\bcompetition\b",
    r"\bskill badge\b",
    r"\bget my badge\b",
]

# Subject prefixes job boards bolt onto marketing to make it look personal.
MARKETING_SUBJECT_PREFIXES = (
    "update:", "alert:", "action needed:", "reminder:", "final call",
    "[hiring alert]", "[final call]", "hiring alert:", "opportunity alert:",
    "few hours", "deadline extended", "last day", "only a few hours",
)

# Phrases that redeem an aggregator email -- these are sent per-application.
TRANSACTIONAL_BOARD_PATTERNS = [
    r"\bassignment from\b",
    r"\bassignment due from\b",
    r"\byour assignment\b",
    r"\bhas (?:shortlisted|hired) you\b",
    r"\byou(?:.ve| have) been shortlisted\b",
    r"\byour application (?:to|for)\b.{0,60}\bhas been\b",
    r"\bupdate on your application\b",
    r"\bapplication status\b",
    r"\binterview (?:scheduled|invitation|invite)\b",
    r"\bwe regret to inform\b",
    r"\bnot been shortlisted\b",
]

# "Application" mail that has nothing to do with jobs.
NON_JOB_CONTEXT_PATTERNS = [
    r"\bloan application\b",
    r"\bcredit (?:card|limit)\b",
    r"\bpassport\b",
    r"\bvisa application\b",
    r"\bincome tax\b",
    r"\bitr\d?\b",
    r"\btax return\b",
    r"\bbooking (?:failed|confirmed|id)\b",
    r"\brefund\b",
    r"\bpolicy (?:number|renewal)\b",
    r"\binsurance\b",
    r"\bscholarship application\b",
    r"\bverify your email\b",
    r"\bemail (?:id )?verification\b",
    r"\botp for\b",
    r"\breset your password\b",
    r"\byour order\b",
    r"\binvoice\b",
]

NON_JOB_DOMAINS = {
    "passportindia.gov.in", "incometax.gov.in", "cpc.incometax.gov.in",
    "instamoney.app", "goibibo.com", "makemytrip.com", "irctc.co.in",
    "notify.docker.com", "docker.com", "github.com", "atlassian.com",
    "uidai.gov.in", "epfindia.gov.in",
}

# --------------------------------------------------------------------------
# Status detection. Order matters: the first family that matches wins, so an
# "unfortunately, after your interview..." email is a rejection, not an
# interview invite.
# --------------------------------------------------------------------------
STATUS_PATTERNS: list[tuple[str, list[str]]] = [
    ("Offer", [
        r"\b(?:pleased|delighted|happy|excited) to (?:offer|extend)\b",
        r"\boffer (?:letter|of employment)\b",
        r"\bwe would like to offer you\b",
        r"\bcongratulations[^.]{0,80}\b(?:selected|offer|onboard)\b",
        r"\bwelcome (?:aboard|to the team)\b",
        r"\bjoining (?:letter|date|formalities)\b",
        r"\bpre-?placement offer\b",
    ]),
    ("Rejected", [
        r"\bregret to inform\b",
        r"\bwe regret\b",
        r"\bnot (?:been )?(?:selected|shortlisted|successful|chosen)\b",
        r"\bnot (?:be )?moving forward\b",
        r"\bnot (?:be )?progressing\b",
        r"\bnot proceed(?:ing)? (?:with|further)\b",
        r"\bdecided (?:not to proceed|to move forward with other)\b",
        r"\bmove forward with other (?:candidates|applicants|profiles)\b",
        r"\bother candidates (?:whose|who)\b",
        r"\bunfortunately[^.]{0,120}\b(?:application|candidature|profile|not|unable)\b",
        r"\bhaven.?t met the requirements\b",
        r"\bdo(?:es)? not match (?:our|the) (?:requirements|criteria)\b",
        r"\bwas unsuccessful\b",
        r"\bapplication (?:has been )?(?:rejected|closed|declined)\b",
        r"\bwill not be (?:considered|taken forward)\b",
        r"\bkeep your (?:profile|resume|cv) on file\b",
        r"\bwish you (?:all the best|the best|success) (?:in|with) your (?:future|job)\b",
    ]),
    ("Interview", [
        r"\binterview (?:invitation|invite|scheduled|schedule|round|call)\b",
        r"\binvite you (?:to|for) (?:an? )?interview\b",
        r"\binvitation to interview\b",
        r"\bschedule (?:an? )?(?:interview|call|discussion)\b",
        r"\bbook (?:a|your) (?:slot|time|interview)\b",
        r"\byour availability\b",
        r"\bhr round\b",
        r"\btechnical (?:round|discussion|interview)\b",
        r"\bface[- ]to[- ]face\b",
        r"\bnext round\b",
        r"\bshortlisted for (?:an? )?interview\b",
        r"\bmeet (?:the|our) (?:team|hiring manager)\b",
    ]),
    ("Assessment", [
        r"\bonline (?:test|assessment|exam)\b",
        r"\btest credentials\b",
        r"\bassessment (?:link|invitation|login|details)\b",
        r"\bcoding (?:test|challenge|round|assessment)\b",
        r"\baptitude test\b",
        r"\bproctored\b",
        r"\btake[- ]home (?:assignment|task)\b",
        r"\bassignment (?:from|due|pending)\b",
        r"\bhackerrank\b|\bcodility\b|\bhackerearth\b|\bmettl\b|\bamcat\b",
        r"\bpsychometric\b",
        r"\bcleared (?:our|the)\b.{0,40}\b(?:round|screening|stage|test)\b",
        r"\bqualified for the next\b",
        r"\bshortlisted for (?:the )?(?:test|assessment)\b",
    ]),
    ("Action Required", [
        r"\baction (?:required|needed)\b",
        r"\bcomplete your application\b",
        r"\badditional information (?:is )?(?:required|needed)\b",
        r"\bplease (?:submit|upload|provide)\b.{0,40}\b(?:documents?|details|transcript)\b",
        r"\bpending (?:documents?|verification)\b",
        r"\bbackground (?:check|verification)\b",
    ]),
    ("Acknowledged", [
        r"\bwe(?:.ve| have)? received your application\b",
        r"\bapplication (?:received|submitted)\b",
        r"\bthank you for applying\b",
        r"\bthank(?:s| you)[^.]{0,40}\byour application\b",
        r"\bsuccessfully submitted\b",
        r"\bunder review\b",
        r"\bbeing reviewed\b",
        r"\bwill (?:review|get back to you)\b",
        r"\bone step closer\b",
        r"\bregistration notification\b",
    ]),
]

# A hiring decision is only a hiring decision if the email is about hiring.
# Used to qualify the outcome signal below, so "unfortunately your booking
# failed" never reads as a rejection.
HIRING_CONTEXT = re.compile(
    r"\b(?:application|candidature|candidate|interview|internship|intern|"
    r"position|role|vacancy|recruitment|recruiting|hiring|job|employment|"
    r"assignment|assessment|shortlist)\b",
    re.I,
)

# Status families that, on their own, mean someone is making a decision about
# you. "Acknowledged" is excluded -- it is already covered by the strong
# application phrases and would otherwise let marketing in.
DECISIVE_STATUSES = {"Offer", "Rejected", "Interview", "Assessment"}

# Score thresholds
MIN_SCORE = 4

SCORES = {
    "ats_domain": 5,
    "outcome_phrase": 4,
    "recruiting_localpart": 3,
    "recruiting_subdomain": 3,
    "strong_phrase": 4,
    "weak_phrase": 1,
    "job_id": 2,
    "transactional_board": 4,
    "marketing_phrase": -3,
    "marketing_subject_prefix": -4,
    "aggregator_domain": -3,
    "never_sender": -20,
    "non_job_context": -10,
    "non_job_domain": -20,
    "promotions_label": -2,
}


def compile_all(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


RX_STRONG = compile_all(STRONG_APPLICATION_PATTERNS)
RX_WEAK = compile_all(WEAK_APPLICATION_PATTERNS)
RX_MARKETING = compile_all(MARKETING_PATTERNS)
RX_TRANSACTIONAL = compile_all(TRANSACTIONAL_BOARD_PATTERNS)
RX_NON_JOB = compile_all(NON_JOB_CONTEXT_PATTERNS)
RX_STATUS = [(name, compile_all(pats)) for name, pats in STATUS_PATTERNS]

# Job / requisition identifiers seen across Workday, Microsoft, SAP, Porsche...
RX_JOB_ID = [
    # Requisition identifiers first: they name the posting, so two emails
    # about one job agree on them. A candidate id names *you*, and differs
    # between the acknowledgement and the decision, so it is the last resort.
    re.compile(r"\b(?:job|requisition|req|position|vacancy|posting)\s*(?:id|no\.?|number|code)\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-_/]{2,24})", re.I),
    re.compile(r"\bID\s+number[,:]?\s*([A-Z0-9][A-Z0-9\-_/]{2,24})", re.I),
    # \bRef\b, not \bRef -- otherwise "future reference is 214650" matches.
    re.compile(r"\bref\b\.?:?\s*([A-Z0-9][A-Z0-9\-_/]{3,24})", re.I),
    re.compile(r"\b(R-\d{5,})\b"),
    re.compile(r"\b(P-\d{4,})\b"),
    re.compile(r"\bapplication-?\s*id\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-_/]{2,24})", re.I),
    re.compile(r"\bID\s+(\d{5,})\b"),
    re.compile(r",\s*(\d{9})\b"),
    re.compile(r"\b(\d{5,6})\s*-\s*[A-Z]"),
    re.compile(r"\bcandidate[\s-]*id\s*(?:for future reference )?(?:is )?[:#-]?\s*([A-Z0-9][A-Z0-9\-_/]{2,24})", re.I),
]
