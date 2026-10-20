"""Run AppliTrack on Windows with a production server.

Flask's built-in server is single-threaded and prints a warning for good
reason; waitress is the usual choice on Windows, where gunicorn does not run.

Started by Task Scheduler at logon (see install-autostart.ps1), via
pythonw.exe so no console window appears. Everything it prints goes to
data/server.log instead.

Run it by hand the same way:

    python deploy\\serve_windows.py
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

LOG_DIR = PROJECT / "data"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "server.log"

HOST = os.environ.get("APPLITRACK_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))


def setup_logging() -> None:
    """Log to a rotating file -- there is no console to print to."""
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(os.environ.get("APPLITRACK_LOG", "INFO"))
    root.addHandler(handler)

    # Anything that escapes logging still lands in the file.
    sys.stdout = sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)


def main() -> int:
    setup_logging()
    log = logging.getLogger("applitrack.serve")

    try:
        from waitress import serve
    except ImportError:
        log.error("waitress is not installed. Run: pip install waitress")
        return 1

    from applitrack.web.app import create_app

    try:
        app = create_app()
    except Exception:
        log.exception("could not start - check .env and credentials.json")
        return 1

    log.info("AppliTrack listening on http://%s:%s", HOST, PORT)
    serve(app, host=HOST, port=PORT, threads=8, channel_timeout=120,
          ident="AppliTrack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
