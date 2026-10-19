# Running AppliTrack as a service

The CLI tracks **your** inbox. The service lets **anyone** connect their own
Gmail, get their own spreadsheet in their own Drive, and be emailed whenever
one of their applications changes.

```
   person clicks "Connect Gmail"
            |
   Google asks their permission  (read-only mail + one new sheet)
            |
   we read a year of their mail, classify it, build their sheet
            |
   welcome email with the link
            |
   new mail arrives --> Gmail --> Pub/Sub --> our /push --> sync --> email them
```

Everyone's data is separated by their Google account id. Nobody can see
anybody else's applications, and the test suite enforces that.

---

## Read this before you launch publicly

**Google treats `gmail.readonly` as a restricted scope.** Until your app is
verified it is capped at **100 users**, and each of them must be added by
hand as a Test user in the Cloud Console. That is fine for yourself, friends
and a college batch.

To open it to the public, Google requires OAuth verification plus an annual
third-party security assessment (CASA). That costs money (roughly
$500&ndash;$4,500/year depending on the assessor) and takes weeks. Budget for
that before promising anyone a public launch.

Nothing below changes because of this — the service runs identically either
way. The only difference is how many people may connect.

---

## 1. Install

```powershell
cd D:\applitarck
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Make an encryption key

Every user's Google refresh token is encrypted with this before it touches
disk.

```powershell
python -m applitrack.web.cli keygen
```

```powershell
$env:APPLITRACK_SECRET_KEY="<the key it printed>"
```

> Keep it safe and back it up. **If you lose this key, every user has to
> reconnect** — the stored tokens become unreadable.

## 3. Create a Google OAuth client

In <https://console.cloud.google.com/>:

1. **APIs & Services → Library** — enable **Gmail API**, **Google Sheets
   API**, **Google Drive API**.
2. **OAuth consent screen** — type *External*. Fill in app name, support
   email, and a privacy policy URL (Google requires one for restricted
   scopes). Add the scopes `gmail.readonly`, `drive.file`, `userinfo.email`,
   `userinfo.profile`.
3. **Audience → Test users** — add every email that will connect, up to 100.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
   Under *Authorised redirect URIs* add:
   - `http://localhost:5000/oauth2callback` (for testing)
   - `https://yourdomain.com/oauth2callback` (for production)
5. Download the JSON, save it as `credentials.json` here.

> It must be a **Web application** client, not Desktop. `applitrack.web.cli
> check` will tell you if you picked the wrong one.

## 4. Configure email

Users get a welcome email and an alert whenever an application changes.
Without this the tracker still works — it just stays silent.

```powershell
$env:APPLITRACK_SMTP_USER="you@gmail.com"
$env:APPLITRACK_SMTP_PASSWORD="your-16-char-app-password"
$env:APPLITRACK_FROM_NAME="AppliTrack"
```

Use a Gmail [App Password](https://myaccount.google.com/apppasswords). For
real volume use a sending service (SendGrid, Postmark, SES) instead — Gmail
caps you at about 500 messages a day.

## 5. Check and run

```powershell
python -m applitrack.web.cli check
python -m applitrack.web.cli serve
```

Open <http://localhost:5000>.

---

## Real-time updates

Without extra setup the service **polls every 15 minutes** — fine, and the
simplest thing that works.

For updates within *seconds* of a company emailing, wire up Gmail push
notifications. This needs a public HTTPS URL.

```bash
# 1. a topic Gmail can publish to
gcloud pubsub topics create applitrack-gmail

# 2. let Gmail publish to it
gcloud pubsub topics add-iam-policy-binding applitrack-gmail \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

# 3. have it POST to your webhook
gcloud pubsub subscriptions create applitrack-push \
  --topic=applitrack-gmail \
  --push-endpoint="https://yourdomain.com/push?token=YOUR_SHARED_SECRET"
```

```powershell
$env:APPLITRACK_PUBSUB_TOPIC="projects/YOUR-PROJECT/topics/applitrack-gmail"
$env:APPLITRACK_PUSH_TOKEN="YOUR_SHARED_SECRET"
```

The service renews each mailbox's watch automatically — they expire after 7
days — and falls back to a full re-read if a cursor goes stale. The poller
keeps running underneath as a safety net, so a dropped notification costs you
minutes, not an update.

---

## Deploying

The dev server is single-threaded; use a real one in production.

**Linux / macOS**

```bash
gunicorn -w 2 -b 0.0.0.0:8000 "applitrack.web.app:create_app()"
```

**Windows**

```powershell
pip install waitress
waitress-serve --listen=0.0.0.0:8000 --call applitrack.web.app:create_app
```

Put nginx, Caddy or Cloudflare in front for HTTPS. Google will not accept a
plain-HTTP redirect URI on a real domain, and `/push` must be HTTPS.

Run **one** instance. The scheduler and the Gmail watch renewal both assume a
single process; two would duplicate every sync. To scale past that, set
`APPLITRACK_SCHEDULER=0` on the extra web instances and run the poller once
separately.

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
ENV APPLITRACK_HTTPS=1
CMD ["gunicorn","-w","2","-b","0.0.0.0:8000","applitrack.web.app:create_app()"]
```

Mount `data/` on a volume — it holds both databases.

---

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `APPLITRACK_SECRET_KEY` | **yes** | — | encrypts users' OAuth tokens |
| `APPLITRACK_CLIENT_SECRETS` | | `credentials.json` | OAuth client file |
| `APPLITRACK_REDIRECT_URI` | | from request host | pin this in production |
| `APPLITRACK_HTTPS` | | `1` | `0` for local HTTP testing |
| `APPLITRACK_SMTP_USER` / `_PASSWORD` | | — | sending notifications |
| `APPLITRACK_SMTP_HOST` / `_PORT` | | gmail / 587 | mail server |
| `APPLITRACK_FROM_NAME` | | `AppliTrack` | sender name |
| `APPLITRACK_PUBSUB_TOPIC` | | — | enables real-time |
| `APPLITRACK_PUSH_TOKEN` | | — | shared secret for `/push` |
| `APPLITRACK_POLL_MINUTES` | | `15` | fallback poll interval |
| `APPLITRACK_DAILY_DIGEST` | | `0` | `1` to send a daily summary |
| `APPLITRACK_DB` | | `data/applitrack.db` | applications |
| `APPLITRACK_USERS_DB` | | `data/users.db` | accounts and tokens |
| `APPLITRACK_SCHEDULER` | | `1` | `0` disables background work |

---

## Operating it

```powershell
python -m applitrack.web.cli check                    # configuration health
python -m applitrack.web.cli users                    # who is connected
python -m applitrack.web.cli sync                     # sync everyone now
python -m applitrack.web.cli sync --email a@b.com --full
python -m applitrack.web.cli digest                   # send digests now
```

`GET /healthz` returns user count and whether real-time and email are live —
point your uptime monitor at it.

---

## What users are promised

The landing page and the footer state this, so it has to stay true:

- **Read-only Gmail.** The scope granted cannot send, delete or label mail.
- **`drive.file` only.** The app can see the one spreadsheet it creates and
  nothing else in their Drive.
- **Disconnect deletes everything.** `/disconnect` revokes the Google grant,
  stops the Gmail watch, and erases their rows from both databases. Their
  spreadsheet stays in their Drive, because they own it.
- **Tokens encrypted at rest.** A stolen database file is not enough to read
  anyone's mail.

A privacy policy saying the same thing is required by Google before
verification. Write it before you invite people.

## Limits worth knowing

- **Gmail API quota** is 250 units/second per project, and a full first sync
  of one mailbox costs a few thousand units. Twenty people signing up at once
  will hit rate limits; the sync retries on the next poll.
- **SQLite** is fine into the low thousands of users on one box. Beyond that
  move to Postgres — the stores are the only thing that would change.
- **The first sync takes 1&ndash;2 minutes** and runs in a background thread.
  The dashboard shows a waiting state and refreshes itself.
