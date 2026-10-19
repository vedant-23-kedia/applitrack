"""Load configuration from a `.env` file.

Written by hand rather than pulling in python-dotenv, because the need is
small and one fewer dependency in a file that holds secrets is worth it.

Two rules matter:

* A variable already set in the real environment always wins. The shell is
  the override, `.env` is the default -- otherwise a quick
  `$env:FOO="bar"` in a terminal would be silently ignored.
* Values are read literally. No shell expansion, no command substitution,
  so a `$` or a backtick in a secret key cannot do anything surprising.
"""

from __future__ import annotations

import os
from pathlib import Path

FILENAME = ".env"


def find_dotenv(start: str | Path | None = None) -> Path | None:
    """Look for `.env` in the working directory, then beside the package."""
    candidates = []

    if start:
        candidates.append(Path(start))
    candidates.append(Path.cwd() / FILENAME)
    # Project root, i.e. the directory containing the `applitrack` package.
    candidates.append(Path(__file__).resolve().parent.parent / FILENAME)

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def load_dotenv(path: str | Path | None = None, override: bool = False) -> dict[str, str]:
    """Read `.env` into the environment. Returns what was applied."""
    found = Path(path) if path else find_dotenv()
    if not found or not found.is_file():
        return {}

    applied: dict[str, str] = {}
    try:
        lines = found.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()

        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        # Strip one matching pair of surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value

    return applied


# PythonAnywhere free accounts have no direct route to the internet. Outbound
# HTTP has to go through their proxy, and anything that tries to connect
# straight out fails with "[Errno 101] Network is unreachable".
#
# Consoles get the proxy variables set for them; web app processes do not, so
# sign-in works right up until the server tries to swap the code for a token.
PYTHONANYWHERE_PROXY = "http://proxy.server:3128"


def on_pythonanywhere() -> bool:
    return bool(
        os.environ.get("PYTHONANYWHERE_DOMAIN")
        or os.environ.get("PYTHONANYWHERE_SITE")
        or Path("/var/www").is_dir() and Path("/home").joinpath(
            os.environ.get("USER", "")
        ).joinpath(".pythonanywhere").exists()
    )


def configure_proxy() -> str | None:
    """Point outbound HTTP at a proxy when the host requires one.

    `requests` reads these variables directly, and httplib2 -- which
    google-api-python-client uses -- defaults to
    `proxy_info_from_environment`, so setting them covers both the OAuth
    token exchange and every Gmail, Sheets and Drive call afterwards.

    An explicit APPLITRACK_PROXY always wins; otherwise PythonAnywhere is
    detected and its proxy used. Returns whatever was applied.
    """
    proxy = os.environ.get("APPLITRACK_PROXY", "").strip()

    if not proxy and on_pythonanywhere():
        proxy = PYTHONANYWHERE_PROXY

    if not proxy:
        return None

    # Never override a proxy the host already configured.
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.setdefault(var, proxy)

    # Keep loopback direct, or health checks would go out and come back.
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    return proxy


def write_dotenv(values: dict[str, str], path: str | Path | None = None) -> Path:
    """Create or update `.env`, keeping any keys already in it.

    Existing lines are rewritten in place so comments and ordering survive.
    The file is written with owner-only permissions where the OS supports it.
    """
    target = Path(path) if path else (Path.cwd() / FILENAME)

    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    remaining = dict(values)

    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    target.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")

    if os.name != "nt":
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return target
