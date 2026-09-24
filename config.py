"""Environment configuration. Reads Backend/.env once at import time."""

import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is missing or empty in Backend/.env — fill it in before starting the app."
        )
    return value


# --- Database ---
DATABASE_URL = _require("DATABASE_URL")
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))

# --- Auth ---
JWT_SECRET = _require("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))

# --- Imports ---
# How long one PDF may be parsed before the request gives up.
#
# Measured cost is seconds per page, and it varies with how the statement is
# drawn: a 4-page AU statement parses in ~4s, a 65-page KVB one in ~145s,
# because the latter carries about 1,200 ruled lines per page. So this cannot
# be a number that assumes "a PDF is small".
#
# It is also the wrong lever on a host that imposes its OWN request deadline.
# A platform that cuts the connection at, say, 100s means any value above that
# can never be reported: the timeout fires into a socket nobody is holding, and
# the user sees a dead connection instead of this module's explanation. Setting
# it BELOW the platform's limit restores the explanation but refuses files that
# would have parsed correctly given the time.
#
# Imports now run as background jobs, so this no longer has to fit inside a
# request at all — it is only here to stop a file that will never finish from
# occupying a worker thread forever.
#
# It is a FLOOR, not the whole answer: see PARSE_SECONDS_PER_PAGE. A flat
# deadline gives a 4-page statement and a 65-page one the same budget, which
# means it is either too tight for the long file or pointlessly slack for the
# short one. This is the short-file end of that.
PARSE_TIMEOUT_SECONDS = float(os.getenv("PARSE_TIMEOUT_SECONDS", "240"))

# The deadline grows by this much per page, and the larger of the two wins.
#
# Measured cost is about 2.2 s/page on a densely ruled statement, so 8 gives
# roughly a 3.5x margin for a slower machine or a busy one — a 65-page file
# gets ~520 s instead of a flat 240 s, while a 4-page file still fails fast.
# Raise it if a legitimate statement is being cut off; the only thing a bigger
# number costs is how long a hopeless parse is allowed to run.
PARSE_SECONDS_PER_PAGE = float(os.getenv("PARSE_SECONDS_PER_PAGE", "8"))

# Read a PDF in stretches of this many pages, stitching the rows back into one
# import. 0 reads the whole file in one parse.
#
# It costs about 10% extra, because page 1 is re-read for its header with
# every stretch. What it buys today is memory and feedback: only one stretch
# of pages is open at a time, so peak RSS is set by the batch rather than the
# file — which matters on a 512 MB host — and the progress bar restarts per
# batch instead of crawling once across the whole document.
#
# Historical note: this began as an ACCURACY setting. The parser competes
# extraction strategies over the whole document and keeps one winner, and on
# a 65-page KVB statement the winner arrived without the Branch and Cheque No
# columns — batching contained that damage to one stretch. The real cure is
# parsers._graft_missing_columns, which hands the winner the columns a losing
# strategy read cleanly, so accuracy no longer depends on this being on.
PDF_BATCH_PAGES = int(os.getenv("PDF_BATCH_PAGES", "20"))

# Refuse a PDF longer than this many pages before parsing it. 0 disables the
# check, which is the default: page count alone does not make a file bad, and a
# limit invented here would refuse statements that import perfectly well.
# It exists for constrained hosts, where a file that cannot finish in the time
# available is better refused in one second, by name, than after three minutes
# of work nobody can deliver.
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "0"))

# --- App ---
APP_ENV = os.getenv("APP_ENV", "development")

# --- Google Drive import (services/drive.py) ---
# The one flat folder auto-collected statements land in (see the Gmail Apps
# Script). Its id is the segment after /folders/ in the folder's own URL.
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")

# Both live in backend/credentials/, which .gitignore excludes wholesale —
# same reasoning as .env: per-machine secrets, never committed.
# drive_credentials.json is the OAuth client downloaded from Google Cloud
# Console; drive_token.json is written by services.drive after the one-time
# browser consent and lets every run after the first skip that consent.
DRIVE_CREDENTIALS_PATH = os.getenv(
    "DRIVE_CREDENTIALS_PATH", "credentials/drive_credentials.json")
DRIVE_TOKEN_PATH = os.getenv("DRIVE_TOKEN_PATH", "credentials/drive_token.json")

# The Apps Script's own deployed web app URL (ends in /exec) and a secret
# only it and this backend know. Changing the Drive folder ID from the UI
# (routers/imports.py's PUT /imports/drive-settings) posts the new value to
# this URL so the script's own copy (Script Properties) stays in sync with
# the one this backend reads from the DB -- otherwise the two would silently
# point at different folders. The secret is checked inside the script's
# doPost, since the deployment has to allow "Anyone" to be reachable from a
# server-to-server call at all.
APPS_SCRIPT_WEB_APP_URL = os.getenv("APPS_SCRIPT_WEB_APP_URL", "")
APPS_SCRIPT_SHARED_SECRET = os.getenv("APPS_SCRIPT_SHARED_SECRET", "")

# --- Google sign-in (routers/auth.py's POST /auth/google) -------------------
# The OAuth Client ID from Google Cloud Console (Credentials -> OAuth client
# ID -> Web application), same project the Drive import already uses or a new
# one -- this is a DIFFERENT client from DRIVE_CREDENTIALS_PATH's, since that
# one is a Desktop-app client used server-side for the one-time Drive consent,
# while this is a Web client whose id is public (it goes into the frontend
# bundle so Google Identity Services' button can initialize) and only ever
# used to check WHO the frontend's sign-in button says signed in, never to
# access anything on the user's behalf. Blank disables the feature outright --
# routers.auth.google_login 400s with a plain "not configured" rather than
# crashing the whole app at startup, since a company that never sets this up
# should keep working exactly as it does today.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

# --- Magic-link login (routers/auth.py's POST /auth/otp/*, services/supabase_auth.py) --
# Supabase Auth (GoTrue) generates the link, emails it through its own
# default mailer (no SMTP setup needed anywhere in this app) and verifies a
# click on it over its REST API -- this app sends nothing of its own.
#
# SUPABASE_URL is "https://<project-ref>.supabase.co" -- the project-ref is
# the part of DATABASE_URL's username before the dot
# (postgresql://postgres.<project-ref>:...), NOT the pooler hostname, so it
# has to be its own setting rather than derived from DATABASE_URL here.
# SUPABASE_ANON_KEY is the public "anon" key from Project Settings -> API --
# safe to expose (it is what a browser's own supabase-js client uses too),
# but still required explicitly rather than guessed.
#
# Blank SUPABASE_URL disables the feature the same way a blank
# GOOGLE_CLIENT_ID does: a clear 400 from the endpoint, not a startup crash.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# Where the frontend actually lives, e.g. "https://app.yourdomain.com" -- the
# clicked link has to land back on THIS app's own callback route
# (FRONTEND_URL + /auth/callback), not on Supabase's own placeholder page.
# Also has to be added to that project's Authentication -> URL Configuration
# "Redirect URLs" allow-list in the Supabase dashboard, or Supabase silently
# ignores it and falls back to whatever its Site URL is set to.
FRONTEND_URL = os.getenv("FRONTEND_URL", "")
