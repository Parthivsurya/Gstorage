"""OAuth 2.0 login via the PKCE loopback flow for desktop apps.

Uses google-auth-oauthlib's InstalledAppFlow, which performs the standard
serverless desktop flow: generate a PKCE code verifier/challenge, open the
system browser to Google's consent screen, run a temporary localhost listener
to catch the redirect, and exchange the authorization code for tokens. No
client secret needs protecting and no server of ours is involved.
"""

from __future__ import annotations

import json

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from gstore import config

# `drive.file` lets the app create/manage its own files (upload, download,
# delete of Gstore-placed files). The full `drive` scope additionally lets it
# *see* every file already in each account's Drive (powering the live combined
# "Scan Drive now" view) and *modify* them — used to move a scanned file to
# Trash on request. about.get (Phase 2 storage view) works under all of these.
#
# `drive` is a **restricted** scope (broader than the earlier `drive.readonly`):
# Google only grants it to a verified app holding a current CASA assessment, so
# it is NOT requested by default — asking for it unconditionally would block
# every public user behind an "app not verified" wall. It is requested only when
# the user opts in, and that account then re-consents on its next link.
# Each token keeps whatever it was granted until the account is re-linked
# (Storage tab → re-link); DRIVE_READONLY_SCOPE is retained so older read-only
# tokens are still recognized as able to see the whole Drive.
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FULL_SCOPE = "https://www.googleapis.com/auth/drive"

# What a normal sign-in asks for. `drive.file` alone is not a restricted scope,
# so the published app can be used by anyone without Gstore holding a current
# CASA assessment; storage pooling, uploads, and managing Gstore's own files all
# work under it. Only the combined "Scan Drive now" view over pre-existing files
# needs more, and that is opt-in — see config.full_drive_requested().
BASE_SCOPES = [DRIVE_FILE_SCOPE]
FULL_DRIVE_SCOPES = [DRIVE_FILE_SCOPE, DRIVE_FULL_SCOPE]

# Every scope the app may ever have been granted, newest-first. Used only as the
# fallback when rehydrating a legacy token blob that predates scope recording;
# never as a login request (that would demand `drive` of every user).
SCOPES = FULL_DRIVE_SCOPES


def requested_scopes() -> list[str]:
    """Scopes to request at the next sign-in, honouring the full-Drive opt-in."""
    return list(FULL_DRIVE_SCOPES if config.full_drive_requested() else BASE_SCOPES)


# Shown in the browser tab after a successful redirect.
SUCCESS_MESSAGE = "Gstore: account linked. You can close this tab and return to the app."


class ClientSecretsNotFound(FileNotFoundError):
    """Raised when no OAuth Desktop-app client can be resolved."""


def _load_client_config() -> dict:
    """Resolve the OAuth Desktop client: user override, else bundled.

    A user-supplied file (``GSTORE_CLIENT_SECRETS`` or the app-data
    ``client_secret.json``) wins; otherwise the client shipped with the app is
    used, so end users never touch Google Cloud Console.
    """
    path = config.client_secrets_path()
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))

    bundled = config.read_bundled_client_config()
    if bundled is not None:
        return bundled

    raise ClientSecretsNotFound(
        "No OAuth client available. Expected a bundled client shipped with the "
        f"app, or a user-supplied JSON via ${config.CLIENT_SECRETS_ENV} or at "
        f"{config.data_dir() / config.CLIENT_SECRETS_FILENAME}."
    )


def run_login_flow() -> Credentials:
    """Drive the interactive login and return authorized credentials.

    Opens the system browser and blocks until the user consents (or the flow
    times out / is cancelled). Call this off the UI thread.
    """
    flow = InstalledAppFlow.from_client_config(
        _load_client_config(),
        scopes=requested_scopes(),
        autogenerate_code_verifier=True,  # enables PKCE (S256)
    )
    # port=0 -> an ephemeral loopback port; Desktop clients accept any.
    return flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=True,
        success_message=SUCCESS_MESSAGE,
    )


def fetch_account_email(credentials: Credentials) -> str:
    """Look up the email of the account that just authorized.

    Uses Drive about.get, which returns the user's email under `drive.file`
    without requesting additional identity scopes.
    """
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    about = service.about().get(fields="user(emailAddress)").execute()
    return about["user"]["emailAddress"]
