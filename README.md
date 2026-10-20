# AppliTrack

Reads your Gmail, works out which job applications you have in flight, and
keeps a spreadsheet and a daily digest up to date so you never have to open
the inbox to find out where you stand.

There are two ways to run it:

| | |
|---|---|
| **Just for you** | The CLI, below. Your mailbox, your machine. |
| **For anyone** | A web service where people connect their own Gmail and get their own sheet. See **[SERVICE.md](SERVICE.md)**. |
| **Hosted, free** | Running it on PythonAnywhere's free tier: **[deploy/PYTHONANYWHERE.md](deploy/PYTHONANYWHERE.md)**. |

```
56 applications   6 in progress   25% got a real response

Needs you
  Northwind Systems    Graduate Engineer          Assessment      2d
  Initech              Data Analyst               Interview       9d

Gone quiet - worth a follow-up
  Globex Corp          Modelling Analyst         103d
  Umbrella Labs        AI / Data Engineer        103d
```

## What it actually does

1. Searches Gmail for anything that smells like recruitment mail.
2. **Scores each email** to separate real application mail from job-board
   marketing. This is the hard part: Internshala and Unstop send blasts
   titled *"Confirm your application"* and *"Application update for L'Oréal"*
   that look identical to transactional mail. Those are filtered out; a
   genuine *"Assignment from DeepThought CultureTech Ventures"* from the same
   sender is kept.
3. Pulls out company, role and job/requisition id.
4. Groups emails into applications by `(company, job id)`, so two Microsoft
   internships with different job numbers stay separate while five emails
   about one Workday requisition become one row.
5. Works out the current status from the **newest** email, so an interview
   followed by a rejection is a rejection.
6. Writes an Excel file, a CSV and a Google Sheet, and gives you a digest.

Statuses: `Applied` → `Acknowledged` → `Action Required` → `Assessment` →
`Interview` → `Offer`, plus `Rejected`, and `Ghosted` for anything silent for
45 days.

## Setup

### 1. Install

```powershell
cd D:\applitarck
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Get Google credentials (one time, ~5 minutes)

1. Open <https://console.cloud.google.com/> and create a project.
2. **APIs & Services → Library** — enable **Gmail API**, **Google Sheets API**
   and **Google Drive API**.
3. **APIs & Services → OAuth consent screen** — choose *External*, fill in the
   app name and your own email, and under **Audience** add your Gmail address
   as a **Test user**. (Without this, sign-in fails with "app not verified".)
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Desktop app**. Download the JSON.
5. Save it as `credentials.json` in this folder.

```powershell
python -m applitrack.cli auth
```

A browser opens once. After that the token in `token.json` keeps you signed
in. Gmail access is **read-only** — this tool cannot send, label or delete
anything.

### 3. First sync

```powershell
python -m applitrack.cli sync --sheet
```

This reads the last 3 months of mail, builds the tracker, writes
`data/applications.xlsx` and creates a Google Sheet. The spreadsheet id is
saved back into `config.yaml`, so later syncs update the same sheet instead of
making new ones.

## Daily use

```powershell
python -m applitrack.cli sync            # refresh from Gmail
python -m applitrack.cli sync --sheet    # ...and update the Google Sheet
python -m applitrack.cli digest          # what needs you, no Gmail call
python -m applitrack.cli report --open   # only live applications
python -m applitrack.cli watch --every 60
```

Or double-click **`run-sync.bat`** / **`show-digest.bat`**.

### Run it automatically

Task Scheduler → Create Basic Task → Daily at 9am → Start a program:

| Field | Value |
|---|---|
| Program | `D:\applitarck\run-sync.bat` |
| Start in | `D:\applitarck` |

You get a Windows toast whenever an application changes status.

### Get the digest by email

```powershell
$env:APPLITRACK_SMTP_USER="you@gmail.com"
$env:APPLITRACK_SMTP_PASSWORD="your-16-char-app-password"
$env:APPLITRACK_EMAIL_TO="you@gmail.com"
python -m applitrack.cli digest --email
```

Gmail needs an [App Password](https://myaccount.google.com/apppasswords), not
your account password. Setting these as environment variables keeps the
password out of `config.yaml`.

## When it gets something wrong

`explain` shows the scoring for any email, so you can see exactly why it was
kept or dropped:

```powershell
python -m applitrack.cli explain "L'Oreal"
```

```
[ignored] score -4 (threshold 4)
  2026-10-19  student@mail.internshala.com
  Update: Your application for L Oreal India | Complete your details
    - job-board:Internshala
    - marketing x3:\bapply (?:now|today|for free)\b
    - marketing-subject-prefix
```

Then fix it in `config.yaml`:

```yaml
ignore_senders: ["noreply@somewhere.com"]
ignore_companies: ["Some Agency"]
```

Or edit the patterns in `applitrack/rules.py` directly — the sender lists and
phrase lists are all at the top of that file, and `tests/test_classify.py`
will tell you if a change lets marketing through.

### Optional: let Claude handle the hard ones

```powershell
pip install anthropic
$env:ANTHROPIC_API_KEY="sk-ant-..."
python -m applitrack.cli sync --llm
```

Only emails that scored *near* the threshold get sent to Claude — typically a
handful per sync, not the whole inbox.

## The spreadsheet

| Column | Meaning |
|---|---|
| Company, Role | extracted from the email |
| Status | from the newest email in the thread |
| Applied On | first submission email |
| Days Since Update | how long it has been quiet |
| Source | Workday, Greenhouse, Internshala, Direct… |
| Job ID | requisition id, used to group emails |
| Confidence | how sure the classifier is |
| Thread | link straight to the Gmail thread |
| **My Notes** | yours — never overwritten by a sync |

Everything except **My Notes** is rebuilt on each sync, since the mailbox is
the source of truth.

## Known limits

- An application only appears once a company emails you. Applications that
  are never acknowledged are invisible — nothing in the mailbox records them.
- Emails that mention no job id and no role can end up as separate rows from
  their own follow-ups. The `Job ID` column shows when this is a risk.
- Campus drives relayed through a job board sometimes land under the board's
  name rather than the employer's.
- An invitation to apply (a campus recruitment nomination, say) is counted as an
  application at low confidence. Check the `Confidence` column.

## Layout

```
applitrack/
  rules.py        sender lists and phrase patterns  <- tune this first
  classify.py     scoring + status detection
  extract.py      company / role / job id
  pipeline.py     grouping emails into applications
  gmail_client.py Gmail search and MIME handling
  store.py        SQLite: seen messages, status history
  sheets.py       Google Sheets writer
  excel.py        .xlsx / .csv export
  digest.py       terminal, Markdown, HTML, toast, email
  cli.py          commands
tests/
  test_classify.py  real emails + real marketing, 7 tests
```

```powershell
python tests\test_classify.py
```
