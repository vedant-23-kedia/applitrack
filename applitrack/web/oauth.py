"""Google OAuth for many users at once.

The CLI uses the installed-app flow, which opens a browser on the same
machine. A hosted service cannot do that, so this is the web server flow:
redirect the person to Google, receive a code on a callback URL, trade it for
tokens, store them against their Google account id.

Scopes are kept as narrow as they can be:

  gmail.readonly   read mail; cannot send, label or delete
  drive.file       only files this app itself creates -- it cannot see the
                   rest of their Drive
  openid/email     to know who just signed in

`drive.file` is enough to create a spreadsheet in the person's own Drive and
keep editing it, while leaving every other file in that Drive invisible.
"""

from __future__ import annotations

import json
import os

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

CLIENT_SECRETS = os.environ.get("APPLITRACK_CLIENT_SECRETS", "credentials.json")


def _flow(redirect_uri: str, state: str | None = None, code_verifier: str | None = None):
    from google_auth_oauthlib.flow import Flow

    if not os.path.exists(CLIENT_SECRETS):
        raise FileNotFoundError(
            f"{CLIENT_SECRETS} not found. Create an OAuth client of type "
            '"Web application" in Google Cloud Console, add your callback URL to '
            "its authorised redirect URIs, and save the downloaded JSON here."
        )
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS, scopes=SCOPES, state=state, code_verifier=code_verifier
    )
    flow.redirect_uri = redirect_uri
    return flow


def authorization_url(redirect_uri: str) -> tuple[str, str, str]:
    """Where to send the person, plus the two things to remember.

    Returns `(url, state, code_verifier)`.

    PKCE matters here. The library generates a one-time `code_verifier`, sends
    only its hash to Google, and the token exchange later has to present the
    original. Because sign-in starts in one request and finishes in another,
    the verifier has to be carried across in the session -- a second `Flow`
    object built from the same file does not inherit it, and the exchange
    fails with "Missing code verifier".
    """
    flow = _flow(redirect_uri)
    url, state = flow.authorization_url(
        access_type="offline",      # we need a refresh token to sync later
        include_granted_scopes="true",
        prompt="consent",           # force a refresh token even on re-auth
    )
    return url, state, flow.code_verifier


def exchange_code(
    redirect_uri: str,
    authorization_response: str,
    state: str,
    code_verifier: str | None = None,
):
    """Trade the callback for credentials.

    `code_verifier` must be the one handed out by `authorization_url`.
    """
    flow = _flow(redirect_uri, state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=authorization_response)
    return flow.credentials


def identity(credentials) -> dict:
    """Who signed in: the stable Google id, their address and name."""
    from googleapiclient.discovery import build

    service = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
    info = service.userinfo().get().execute()
    return {
        "user_id": info["id"],
        "email": info.get("email", ""),
        "name": info.get("name", ""),
    }


def credentials_to_json(credentials) -> str:
    return json.dumps({
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or SCOPES),
    })


def credentials_from_dict(data: dict):
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", SCOPES),
    )


def load_credentials(user_store, user_id: str):
    """Credentials for a user, refreshed and re-saved if they had expired.

    Returns None when the person has revoked access -- the caller should stop
    syncing them rather than retrying forever.
    """
    from google.auth.transport.requests import Request

    data = user_store.load_credentials(user_id)
    if not data:
        return None

    creds = credentials_from_dict(data)
    if creds.valid:
        return creds

    if creds.refresh_token:
        try:
            creds.refresh(Request())
            user_store.save_credentials(user_id, credentials_to_json(creds))
            return creds
        except Exception as exc:
            user_store.record_error(user_id, f"token refresh failed: {exc}")
            return None
    return None


def revoke(credentials) -> bool:
    """Tell Google to forget the grant. Best effort."""
    import requests

    try:
        response = requests.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": credentials.refresh_token or credentials.token},
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return response.status_code == 200
    except Exception:
        return False
