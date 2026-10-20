"""WSGI entry point for PythonAnywhere.

Copy the contents of this file into the WSGI configuration file that
PythonAnywhere creates for you (Web tab -> "WSGI configuration file"), and
replace YOURUSERNAME throughout.

Delete everything else that is already in that file -- PythonAnywhere puts a
Django/Flask example in there by default.
"""

import os
import sys

# ---------------------------------------------------------------------------
# 1. Make the project importable
# ---------------------------------------------------------------------------
PROJECT = "/home/YOURUSERNAME/applitrack"

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# The app finds .env relative to the working directory, so point it here.
os.chdir(PROJECT)

# ---------------------------------------------------------------------------
# 2. Settings for this host
#
# Secrets (APPLITRACK_SECRET_KEY, APPLITRACK_CLIENT_SECRETS_JSON) belong in
# /home/YOURUSERNAME/applitrack/.env, which is gitignored. Do not paste them
# into this file -- PythonAnywhere's WSGI file is readable by their support
# staff and is easy to screenshot by accident.
# ---------------------------------------------------------------------------
os.environ.setdefault("APPLITRACK_DB", f"{PROJECT}/data/applitrack.db")
os.environ.setdefault("APPLITRACK_USERS_DB", f"{PROJECT}/data/users.db")
os.environ.setdefault("APPLITRACK_LOCK_DIR", f"{PROJECT}/data/locks")

# Served over HTTPS by PythonAnywhere, so secure cookies are correct.
os.environ.setdefault("APPLITRACK_HTTPS", "1")

# A free account cuts a web request off after about five minutes, and a full
# year of mail takes longer than that. Capping the chunk size lets the
# dashboard work through the backlog across several short requests instead.
os.environ.setdefault("APPLITRACK_MAX_FETCH_PER_SYNC", "200")

# Each user can change their own lookback on the dashboard; the
# default is 90 days.

# The background poller runs inside the web worker here. It is not guaranteed
# to be alive -- free workers get recycled -- so the daily scheduled task is
# the reliable path. An hour keeps CPU use well inside the free allowance.
os.environ.setdefault("APPLITRACK_SCHEDULER", "1")
os.environ.setdefault("APPLITRACK_POLL_MINUTES", "60")

# ---------------------------------------------------------------------------
# 3. The app
# ---------------------------------------------------------------------------
from applitrack.web.app import create_app  # noqa: E402

application = create_app()
