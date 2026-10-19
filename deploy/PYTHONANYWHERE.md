# Hosting AppliTrack free on PythonAnywhere

Free forever, no card, always online, and a stable HTTPS address — which
matters because Google pins the OAuth redirect URI and a changing URL breaks
sign-in every time.

Replace `YOURUSERNAME` throughout with your PythonAnywhere username. Your app
will live at `https://YOURUSERNAME.pythonanywhere.com`.

## What the free tier can and cannot do

| | |
|---|---|
| Always online, no laptop needed | ✅ |
| Stable HTTPS URL | ✅ |
| Persistent storage (both databases survive) | ✅ |
| Reaching Google's APIs | ✅ — `*.googleapis.com` and `accounts.google.com` are on PythonAnywhere's allowlist |
| A web request running longer than ~5 minutes | ❌ — handled by chunking, below |
| An always-on background worker | ❌ — one scheduled task per day instead |

**What this means in practice:** a first read of a year's mail takes about ten
minutes, so it cannot happen inside one web request. The sync is resumable, so
the dashboard fetches ~200 messages per request and continues automatically
until the backlog is clear. Your own first read is quicker done from a console
(step 8), which has no time limit.

Ongoing updates are incremental — a handful of messages, a second or two — so
they fit comfortably in a request or the daily task.

---

## 1. Create the account

Sign up for a free **Beginner** account at
<https://www.pythonanywhere.com/registration/register/beginner/>.

No card required. Note your username.

## 2. Get the code

**Consoles** tab → **Bash**:

```bash
git clone https://github.com/vedant-23-kedia/applitrack.git
cd applitrack
```

## 3. Install the dependencies

```bash
mkvirtualenv --python=/usr/bin/python3.10 applitrack
pip install -r requirements.txt
```

Takes a few minutes. The virtualenv path you will need later is:

```
/home/YOURUSERNAME/.virtualenvs/applitrack
```

## 4. Upload your OAuth client

**Files** tab → navigate into `applitrack` → **Upload a file** → choose the
`credentials.json` from your laptop.

Uploading beats pasting the JSON into a terminal: the secret never goes
through your clipboard or shell history, and `credentials.json` is gitignored
so `git pull` will not touch it.

> If you would rather not upload a file, the whole JSON can go in `.env` as
> `APPLITRACK_CLIENT_SECRETS_JSON={"web":{...}}` on one line instead.

## 5. Create the `.env`

```bash
cd ~/applitrack
mkdir -p data
python -m applitrack.web.cli keygen          # copy the key it prints
nano .env
```

Paste this, putting the generated key on the first line:

```
APPLITRACK_SECRET_KEY=<the key you just generated>
APPLITRACK_REDIRECT_URI=https://YOURUSERNAME.pythonanywhere.com/oauth2callback
APPLITRACK_HTTPS=1
APPLITRACK_MAX_FETCH_PER_SYNC=200
APPLITRACK_POLL_MINUTES=60
```

`Ctrl+O`, `Enter`, `Ctrl+X` to save. Then lock both files down:

```bash
chmod 600 .env credentials.json
```

Keep a copy of that key somewhere safe. Losing it means every user has to
reconnect their Gmail.

## 6. Create the web app

**Web** tab → **Add a new web app**

1. Domain: accept the default
2. Framework: **Manual configuration** (*not* "Flask")
3. Python version: **3.10**

Then on the Web tab:

- **Source code**: `/home/YOURUSERNAME/applitrack`
- **Working directory**: `/home/YOURUSERNAME/applitrack`
- **Virtualenv**: `/home/YOURUSERNAME/.virtualenvs/applitrack`
- **WSGI configuration file**: click it, delete everything inside, and paste
  the contents of `deploy/pythonanywhere_wsgi.py` from this repo — replacing
  `YOURUSERNAME`. Save.

Click the green **Reload** button.

## 7. Tell Google about the new address

<https://console.cloud.google.com/auth/clients> → open your client →
**Authorised redirect URIs** → **+ Add URI**:

```
https://YOURUSERNAME.pythonanywhere.com/oauth2callback
```

And under **Authorised JavaScript origins**:

```
https://YOURUSERNAME.pythonanywhere.com
```

**Save.** Keep the `localhost:5000` entries too — both can coexist, so your
laptop setup keeps working.

## 8. Your own first read

Do this from a **Bash console**, which has no request timeout:

```bash
cd ~/applitrack
workon applitrack
python -m applitrack.web.cli sync --email you@gmail.com --full
```

Run it a few times until it reports no remaining messages — each pass picks up
where the last stopped. Only needed once; your friends' first reads are handled
automatically by the dashboard.

## 9. The daily task

**Tasks** tab → add a daily scheduled task (pick a quiet hour):

```bash
cd /home/YOURUSERNAME/applitrack && /home/YOURUSERNAME/.virtualenvs/applitrack/bin/python -m applitrack.web.cli sync
```

This keeps everyone's tracker current even if the web worker has been recycled.

## 10. Let your friends in

**This is required — without it they get `access_denied`.**

<https://console.cloud.google.com/auth/audience> → **Test users** →
**+ Add users** → their Gmail address → **Save**.

Up to 100 people. Google requires this for any unverified app using Gmail
scopes; it is not something the code can work around. Going properly public
needs OAuth verification plus a paid annual security assessment.

Then send them `https://YOURUSERNAME.pythonanywhere.com`. They will see a
"Google hasn't verified this app" warning — **Advanced** → **Go to AppliTrack
(unsafe)** — which is expected for a test-mode app.

---

## Keeping it alive

Free web apps expire after three months. PythonAnywhere emails you; click the
**"Run until 3 months from today"** button on the Web tab to extend. No cost.

## Updating after a code change

```bash
cd ~/applitrack && git pull
```

Then **Reload** on the Web tab.

## If something breaks

- **Error log**: Web tab → *Error log* — tracebacks land here
- **Configuration check**: `workon applitrack && python -m applitrack.web.cli check`
- **`redirect_uri_mismatch`**: step 7 was missed, or `APPLITRACK_REDIRECT_URI`
  in `.env` does not match the Console exactly (`https`, no trailing slash)
- **`access_denied`**: that person is not a Test user — step 10
- **Sync seems stuck**: `python -m applitrack.web.cli users` shows each
  account's last sync and last error
