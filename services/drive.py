"""Google Drive access for the flat "auto-collected statements" folder.

The folder itself is filled by a Gmail Apps Script (outside this app) that
copies matching bank-statement attachments into it. This module is the other
end: list what's sitting there, pull a file's bytes for import, and rename a
file after it's been handled so it's never picked up again.

Auth is a one-time browser consent (InstalledAppFlow), not a service account
-- this app acts as the same Google account that owns the folder, the
simplest grant for one person's own Drive. That consent can only ever happen
on a machine with a real browser, which a host like Render is not -- there is
no local browser for InstalledAppFlow.run_local_server() to open, and no way
to click Allow on a headless server. So the consent is done once, locally,
and the resulting token -- which Google keeps refreshing on its own from
here on -- is what a deployed instance actually runs on, supplied as the
DRIVE_TOKEN_JSON env var rather than a file it could never have produced
itself. Locally, the same token is cached to DRIVE_TOKEN_PATH instead, purely
so repeat local runs skip the browser too. Both the downloaded OAuth client
(DRIVE_CREDENTIALS_PATH) and the cached token file are gitignored -- see
backend/.gitignore's `credentials/` entry -- the same per-machine-secret
treatment as .env; DRIVE_TOKEN_JSON on Render is the deployed equivalent of
that same secret.
"""
from __future__ import annotations

import io
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import config

# Read-only would be enough for listing/downloading, but renaming a file
# after import needs write access to that one file -- drive.file scopes the
# grant to files this app created or opens via its own picker/API calls, not
# the account's whole Drive.
_SCOPES = ["https://www.googleapis.com/auth/drive"]

_service = None


def _load_credentials() -> Credentials:
    creds = None

    # A deployed instance (Render) has no local browser and no file this app
    # itself could ever have written -- it arrives with the already-consented
    # token as a plain env var instead. Checked first so a machine that
    # happens to have both prefers the one meant for it to run on.
    token_json = os.getenv("DRIVE_TOKEN_JSON")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), _SCOPES)
    elif os.path.exists(config.DRIVE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(config.DRIVE_TOKEN_PATH, _SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        # Only reached locally, ever: opens the real default browser and
        # blocks until the user clicks Allow. A host with no DRIVE_TOKEN_JSON
        # and no browser to open would fail here with a clear file-not-found
        # rather than hang, which is the correct outcome -- it has no way to
        # complete this step itself.
        flow = InstalledAppFlow.from_client_secrets_file(
            config.DRIVE_CREDENTIALS_PATH, _SCOPES)
        creds = flow.run_local_server(port=0)

    # Cache the (possibly just-refreshed) token back to disk so the next run
    # on THIS machine skips both the browser and, once DRIVE_TOKEN_JSON is
    # set, even needs it again. Best-effort: on a host with a read-only or
    # ephemeral filesystem this simply doesn't persist, which is fine -- the
    # env var or the browser consent covers it next time either way.
    try:
        with open(config.DRIVE_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    except OSError:
        pass

    return creds


def _get_service():
    global _service
    if _service is None:
        _service = build("drive", "v3", credentials=_load_credentials())
    return _service


def list_folder_files(folder_id: str) -> list[dict]:
    """Every non-trashed file directly in this folder: [{id, name}, ...].

    Not recursive -- the folder is flat by design (confirmed with the user:
    no per-bank subfolders), so one level is the whole answer.

    supportsAllDrives/includeItemsFromAllDrives=True is required if the
    folder lives inside a Shared Drive rather than the account's own My
    Drive -- without them the API reports even a folder this account has
    real access to as a plain 404, which is indistinguishable from actually
    having no access at all.
    """
    service = _get_service()
    files = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def download_file(file_id: str) -> bytes:
    service = _get_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def rename_file(file_id: str, new_name: str) -> None:
    service = _get_service()
    service.files().update(fileId=file_id, body={"name": new_name},
                           supportsAllDrives=True).execute()
