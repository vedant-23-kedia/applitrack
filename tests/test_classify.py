"""Tests built from real emails in the target inbox.

The positive cases are genuine application mail; the negative cases are job
board marketing that imitates it almost word for word. If a change makes the
marketing pass, these tests fail.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from applitrack.classify import classify, score_email  # noqa: E402
from applitrack.models import Email, Status  # noqa: E402
from applitrack.pipeline import build_applications  # noqa: E402

NOW = datetime.now(timezone.utc)


def mk(sender, subject, body, name="", days_ago=10, labels=None) -> Email:
    return Email(
        msg_id=f"m{abs(hash((sender, subject))) % 10**8}",
        thread_id="t1",
        date=NOW - timedelta(days=days_ago),
        sender=sender,
        sender_name=name or sender.split("@")[0],
        subject=subject,
        body=body,
        labels=labels or ["INBOX"],
    )


# --------------------------------------------------------------------------
# Real application mail must be counted
# --------------------------------------------------------------------------

REAL = [
    (
        mk("donotreply@deloitte.com",
           "We are excited to receive your application!",
           "Deloitte India (Offices of the US) Dear Vedant, Thank you for applying for "
           "the position of Campus28-UPES Dehradun, ID number 363511. You can view the "
           "status of your application by navigating to the careers portal.",
           name="Deloitte India"),
        Status.ACKNOWLEDGED, "Deloitte",
    ),
    (
        mk("recruiting@dhl.com",
           "You are one step closer: Software Engineer ID 358837 at The DHL Group",
           "Dear Vedant Kedia, Thank you for applying for the position of Software "
           "Engineer ID 358837 at Global Business Services at The DHL Group. We are "
           "delighted that you are interested in joining us."),
        Status.ACKNOWLEDGED, "Dhl",
    ),
    (
        mk("DoNotReply@email.careers.microsoft.com",
           "Application Update: Data Science INTERN, 200041778",
           "Hi Vedant, Thank you for applying for the position of Data Science INTERN, "
           "200041778. We appreciate your interest in a career at Microsoft. "
           "Unfortunately, you have not met the requirements for this role."),
        Status.REJECTED, "Microsoft",
    ),
    (
        mk("rbs@myworkday.com",
           "Your application R-00279183 - Software Engineer, VP update",
           "Hi Vedant, Thank you for your interest in our Software Engineer, VP job at "
           "NatWest Group. We appreciate the time and effort you invested in your "
           "application. Unfortunately on this occasion, your application was "
           "unsuccessful."),
        Status.REJECTED, "NatWest",
    ),
    (
        mk("admin@hirepro.in",
           "DeloitteUSICampus - Online Test Credentials",
           "Deloitte US-India Offices recruitment process. Proctored online assessment "
           "login details. Dear Vedant Kedia, We are pleased to inform you that you "
           "have cleared our first round of screening."),
        Status.ASSESSMENT, None,
    ),
    (
        mk("sai.burgute@cummins.com",
           "Cummins India - Internship Interview Results 2026",
           "Dear Vedant, Greetings from Cummins India! Thank you for taking the time to "
           "interview for an internship with Cummins India. After careful consideration, "
           "we regret to inform you that we will not be moving forward with your "
           "application."),
        Status.REJECTED, None,
    ),
    (
        mk("talent@ibm.com",
           "Your IBM Application Status",
           "Ref: 83916 - Data Scientist - Public Sektor / Defense. Dear Vedant Kedia, "
           "We regret to inform you that, after careful consideration, we have decided "
           "to move forward with other candidates."),
        Status.REJECTED, "Ibm",
    ),
    (
        mk("noreply@mail.apply.careers.hsbc.com",
           "Thank You for Your Application to HSBC Group",
           "Application Received. Your Journey Starts Here! Hello Vedant, We have got "
           "your application for Modelling Analyst (Job ID: 46113) at HSBC Group."),
        Status.ACKNOWLEDGED, None,
    ),
    (
        mk("assess.support@lntedutech.com",
           "Larsen & Toubro Campus Recruitment Program 2027",
           "Dear Vedant Kedia, We have successfully received your application for the "
           "Larsen & Toubro Campus Recruitment Program 2027. Our team will review your "
           "submission."),
        Status.ACKNOWLEDGED, None,
    ),
    (
        mk("job@porsche.de",
           "Your application [RE-ID: ykyimmyswvyyxaognqzqmkppjudectqr]",
           "Your application as: Praktikum Data Science & Artificial Intelligence "
           "Application-ID: A001202486 Hello Vedant, we have received your application."),
        Status.ACKNOWLEDGED, None,
    ),
]


def test_real_application_mail_is_counted():
    failures = []
    for email, expected_status, expected_company in REAL:
        signal = classify(email)
        if signal is None:
            failures.append(f"MISSED: {email.subject!r} (score {score_email(email).value})")
            continue
        if signal.status != expected_status:
            failures.append(
                f"STATUS: {email.subject!r} -> {signal.status.value}, "
                f"expected {expected_status.value}"
            )
        if expected_company and expected_company.lower() not in signal.company.lower():
            failures.append(
                f"COMPANY: {email.subject!r} -> {signal.company!r}, "
                f"expected to contain {expected_company!r}"
            )
    assert not failures, "\n".join(failures)


# --------------------------------------------------------------------------
# Marketing that imitates application mail must be rejected
# --------------------------------------------------------------------------

NOISE = [
    mk("student@mail.internshala.com",
       "Update: Your application for L Oreal India | Complete your details",
       "Application window closes in 6 days! Apply now. Internshala. Not interested "
       "right now? Unsubscribe here. Privacy policy."),
    mk("student@updates.internshala.com",
       "Vedant, Urban Company is hiring | Confirm your application",
       "Hi Vedant, Some moments stay with you forever. Your first salary is one of them. "
       "Confirm your application today. Apply now on Internshala."),
    mk("noreply@unstop.news",
       "Maruti Suzuki wants to interview you (Exclusive PPI Offer)",
       "Apply Today! Direct PPI opportunity with Maruti Suzuki. Prize pool of 3 Lakhs. "
       "Register before it is too late!"),
    mk("student@internshala.com",
       "EvoNexis is hiring Machine Learning intern | Apply Now",
       "Vedant, earn high stipend in your preferred field and location. Apply for free "
       "on Internshala."),
    mk("resumenc@naukri.com",
       "Vedant, you are missing job shortlists",
       "Resume is outdated, update resume now. Your resume is 50+ days old. Recruiter "
       "searches work best when your profile is current."),
    mk("eventnc@naukri.com",
       "naukri campus : Quiz is open for registration !!",
       "naukri campus presents AI Skill Assessment - Test Your AI Knowledge. Rounds: 1. "
       "Duration: 1 Nov. Participants: Individual."),
    mk("alerts@courses.codingninjas.com",
       "Update: Coding Ninjas is hiring - Click here to confirm your application",
       "Hi Vedant, We are excited to let you know that Coding Ninjas is hiring Business "
       "Development Executive for its dynamic Sales Team."),
    mk("messages-noreply@linkedin.com",
       "Someone at HCLTech you may know",
       "Hi Vedant, meet shagun rohilla! Grow your network on LinkedIn."),
    mk("noreply@instamoney.app",
       "InstaMoney || Important Notice: Loan Application Status",
       "Dear Vedant Kedia, thank you for applying. Unfortunately, your loan application, "
       "facilitated by our lending partner, was not approved this time."),
    mk("passport.admin@passportindia.gov.in",
       "Email ID Verification",
       "Dear Applicant, Congratulations! You have been registered successfully on "
       "Passport Seva! This email is for verification of your Email ID."),
    mk("communication@cpc.incometax.gov.in",
       "Confirmation on e-Verification of Income Tax Return",
       "Dear VEDANT KEDIA, The Income Tax Return ITR4 of 2026-27 has been successfully "
       "e-Verified."),
    mk("noreply@goibibo.com",
       "Bus Booking failed",
       "Booking Failed. Dear null, Unfortunately your Bus booking has failed after a "
       "successful payment. The refund amount is being processed."),
]


def test_marketing_and_non_job_mail_is_rejected():
    leaked = []
    for email in NOISE:
        signal = classify(email)
        if signal is not None:
            leaked.append(
                f"LEAKED: {email.sender} | {email.subject!r} -> "
                f"{signal.company}/{signal.status.value} (score {score_email(email).value})"
            )
    assert not leaked, "\n".join(leaked)


# --------------------------------------------------------------------------
# A job board's genuine per-application mail must survive the filter
# --------------------------------------------------------------------------

def test_internshala_assignment_is_kept():
    email = mk(
        "student@updates.internshala.com",
        "Assignment from DeepThought CultureTech Ventures",
        "Hi Vedant, Good news! DeepThought CultureTech Ventures is interested in your "
        "application and has sent you an assignment for further evaluation: New "
        "Assignment Due on: 25 Apr, 2026.",
        days_ago=5,
    )
    signal = classify(email)
    assert signal is not None, f"filtered out (score {score_email(email).value})"
    assert signal.status == Status.ASSESSMENT
    assert "DeepThought" in signal.company


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------

def test_two_job_ids_at_one_company_stay_separate():
    emails = [
        mk("donotreply@email.careers.microsoft.com",
           "Thank you for your application!",
           "Hi Vedant, Thank you for taking the time to submit your application for "
           "Software Engineering INTERN (Job number: 200041085) at Microsoft.",
           days_ago=40),
        mk("DoNotReply@email.careers.microsoft.com",
           "Application Update: Software Engineering INTERN, 200041085",
           "Hi Vedant, Thank you for applying for the position of Software Engineering "
           "INTERN, 200041085. Unfortunately, you have not met the requirements.",
           days_ago=36),
        mk("donotreply@email.careers.microsoft.com",
           "Thank you for your application for Data Science",
           "Hi Vedant, Thank you for taking the time to submit your application for "
           "Data Science INTERN (Job number: 200041778) at Microsoft.",
           days_ago=32),
    ]
    signals = [s for s in (classify(e) for e in emails) if s]
    assert len(signals) == 3
    apps = build_applications(signals)
    assert len(apps) == 2, [f"{a.company}/{a.role}/{a.job_id}" for a in apps]


def test_latest_email_decides_status():
    """Interview then rejection is a rejection, not an interview."""
    emails = [
        mk("careers@acme.com",
           "Interview invitation",
           "We would like to invite you to an interview for the Data Analyst position "
           "at Acme. Job ID: ACME-4821.",
           days_ago=20),
        mk("careers@acme.com",
           "Acme application update",
           "Thank you for your application for the Data Analyst position. Job ID: "
           "ACME-4821. We regret to inform you that we will not be moving forward.",
           days_ago=12),
    ]
    signals = [s for s in (classify(e) for e in emails) if s]
    apps = build_applications(signals)
    assert len(apps) == 1
    assert apps[0].status == Status.REJECTED
    assert apps[0].email_count == 2


def test_silent_application_becomes_ghosted():
    email = mk("careers@quietco.com",
               "Thank you for applying",
               "Thank you for applying for the position of Backend Engineer at QuietCo. "
               "Job ID: QC-99. We will review your application.",
               days_ago=120)
    signal = classify(email)
    assert signal is not None
    apps = build_applications([signal], ghost_after_days=45)
    assert apps[0].status == Status.GHOSTED


def test_company_suffix_variants_merge():
    emails = [
        mk("recruiting@dhl.com", "Application received",
           "Thank you for applying for the position of Software Engineer ID 358837 at "
           "The DHL Group.", days_ago=30),
        mk("recruiting@dhl.com", "Your application update",
           "Your application for Software Engineer ID 358837 at DHL is under review.",
           days_ago=20),
    ]
    signals = [s for s in (classify(e) for e in emails) if s]
    apps = build_applications(signals)
    assert len(apps) == 1, [a.company for a in apps]


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
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
