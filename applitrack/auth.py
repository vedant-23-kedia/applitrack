"""Google OAuth for Gmail (read-only), Sheets and Drive.

The first run opens a browser once; after that the refresh token in
`token.json` keeps things quiet. Gmail scope is read-only on purpose -- this
tool never needs to send, label or delete anything.
"""

from __future__ import annotations

import os
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

DEFAULT_CREDENTIALS = "credentials.json"
DEFAULT_TOKEN = "token.json"


def get_credentials(
    credentials_path: str = DEFAULT_CREDENTIALS,
    token_path: str = DEFAULT_TOKEN,
    scopes: list[str] | None = None,
):
    """Load cached credentials, refreshing or re-running the consent flow."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = scopes or SCOPES
    token_file = Path(token_path)
    creds = None

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
        except ValueError:
            # Scopes changed since the token was written; start over.
            token_file.unlink()
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            pass  # fall through to a fresh consent flow

    cred_file = Path(credentials_path)
    if not cred_file.exists():
        raise FileNotFoundError(
            f"{credentials_path} not found.\n\n"
            "Create an OAuth client (Desktop app) in Google Cloud Console, enable the\n"
            "Gmail API, Google Sheets API and Google Drive API, download the JSON and\n"
            f"save it as {credentials_path} in this folder. Full steps are in README.md."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), scopes)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_file.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token_file, 0o600) if os.name != "nt" else None
    return creds


def build_service(api: str, version: str, creds=None, **kwargs):
    """Thin wrapper so callers do not each import googleapiclient."""
    from googleapiclient.discovery import build

    creds = creds or get_credentials(**kwargs)
    return build(api, version, credentials=creds, cache_discovery=False)
