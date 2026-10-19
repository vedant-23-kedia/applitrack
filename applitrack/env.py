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
