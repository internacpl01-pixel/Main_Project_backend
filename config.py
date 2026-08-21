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
# This is an accuracy setting, not a performance one — it costs about 10% extra
# because page 1 is re-read for its header with every stretch.
#
# The parser competes extraction strategies over the WHOLE document and keeps
# one winner. So a stretch of pages that the winning strategy handles badly does
# not spoil only those pages: it decides the strategy for everything. Measured on
# a 65-page KVB statement, pages 41-60 defeat column detection, and read in one
# pass that cost the Branch and Cheque No columns on all 928 rows — the empty
# Branch then back-filled from the page-1 header, so every row claimed the
# account's home branch instead of the code printed against it.
#
# Reading in stretches confines that to the stretch containing those pages: 630
# of 928 rows keep their real branch code instead of none of them. It contains
# the damage rather than curing it — the underlying strategy choice is still
# wrong for those pages, and a smaller batch size does not help, because the
# problem is those particular pages and not the length of the document.
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
