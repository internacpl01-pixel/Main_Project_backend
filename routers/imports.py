"""
Import routes — PDF, Excel and CSV bank statements.

POST   /imports/pdf              — parse a PDF; save=false previews, save=true stages
POST   /imports/excel/inspect    — list a workbook's sheets, without writing
POST   /imports/excel            — same as /pdf, for .xlsx / .xls; one batch per sheet
POST   /imports/csv              — same, for .csv
GET    /imports/batches          — list uploads for this company
GET    /imports/batches/{id}     — one batch with its staged rows
DELETE /imports/batches/{id}     — discard a batch that has not been finalized

Adapted from DPL_project/backend/routers/imports.py. The validation ladder
(missing file, wrong extension, empty body, size cap) is kept as-is; the auth
dependency and the batch endpoints are new, because DPL was single-tenant and
had no batch concept.

This replaced routers/upload.py, which imported the same three formats into the
same tables but parsed synchronously in the event loop, dropped failed rows
silently, and recorded a blank uploaded_by.
"""
import asyncio
import base64
import datetime
import json
import logging
import re

import httpx
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile, status)

import config
import permissions
from database import company_connection
from routers.auth import get_company_user, get_current_schema, require_level
from services import drive, jobs
from services.pdf_import import (PDF_BATCH_PAGES, process_pdf_import,
                                 start_pdf_job)
from services.drive_log import list_drive_log, log_drive_result
from services.settings import (get_drive_folder_history, get_drive_folder_id,
                               get_import_folder_id, get_raw_drive_folder_id,
                               get_raw_import_folder_id,
                               record_drive_folder_history, set_drive_folder_id,
                               set_import_folder_id)
from services.staging import (DuplicateFileError, bulk_bank_lookup,
                              find_bank_by_hint)
from services.tabular_import import (READERS, inspect_tabular,
                                     process_tabular_import, start_tabular_job)

router = APIRouter(prefix="/imports", tags=["imports"])

# Uploading and staging a statement is the day job — staff do it. Discarding
# a whole batch throws away staged work, so that is manager+.
require_manager = require_level(permissions.MANAGER)
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


async def _read_upload(file: UploadFile, allowed: tuple[str, ...]) -> bytes:
    """Shared upload validation for every import route."""
    if not file or not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No file provided")

    if not file.filename.lower().endswith(allowed):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only {' / '.join(allowed)} files are supported",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty")

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large (max 25 MB)"
        )

    return file_bytes


def _clean_bank_id(bank_id: int | None) -> int | None:
    """Read 0 (and anything below it) as "no bank chosen".

    bank_master ids come from a serial, so nothing at or under zero can ever
    name a row — the only thing such a value can do is fail. Both callers
    already mean "unset" by it: the React client skips a falsy id before it
    builds the form, and Swagger's generated form posts 0 for an integer field
    the user never touched, which made the optional bank field impossible to
    omit from /docs.
    """
    return bank_id if bank_id and bank_id > 0 else None


def _to_http(exc: Exception) -> HTTPException:
    """Map a service-layer failure onto a status code.

    A duplicate file is 409 and carries the colliding batch, so the UI can link
    to it. Anything the parser raises as RuntimeError is a problem with the
    document, not the server, so it is 422 with the message shown to the user.
    """
    if isinstance(exc, DuplicateFileError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": str(exc), "existing_batch": exc.batch["id"]},
        )
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.post("/pdf")
async def import_pdf(
    file: UploadFile = File(...),
    password: str = Form("", description="Password, if the PDF is protected"),
    save: bool = Form(False, description="false previews, true stages a batch"),
    bank_id: int = Form(None, description="bank_master.id this statement belongs to"),
    pages: str = Form("", description='Pages to read: "30", "31-65", or blank for all'),
    batch_pages: int = Form(
        None,
        description="Read the file in stretches of this many pages (0 = one pass). "
                    "Omit for the server default.",
    ),
    background: bool = Form(
        False,
        description="true returns a job id immediately; poll GET /imports/jobs/{id}",
    ),
    allow_reimport: bool = Form(
        False,
        description="true re-stages this exact file even though it was "
                    "already uploaded -- the single-file import screen's "
                    "'import anyway' confirm, after a first attempt without "
                    "this came back 409 DuplicateFileError.",
    ),
    user: dict = Depends(get_company_user),
):
    """
    Parse a PDF bank statement.

    save=false is a dry run — nothing is written. Use it first and check
    `headers_detected` and `fill_rates`: if `balance` is filled on 3 of 180
    rows, the balance column was not matched, and the fix is a fieldmap alias,
    not a re-upload.

    save=true stages the rows into temp_trans under a new batch. They are not
    in the ledger yet — classify and finalize move them there.

    pages reads part of the file: "30" is the first thirty pages, "31-65" a
    range. A range always carries page 1 with it, because that is where a bank
    prints the column header and later pages carry none — the response says so
    in `header_page_added`.

    background=true answers straight away with a job id and does the work
    behind it. That is the mode to use for anything long: a big statement
    parses for minutes, which is longer than most hosts will hold a request
    open, and it is the only way to show progress while it runs.
    """
    file_bytes = await _read_upload(file, (".pdf",))
    logger.info("[Import] PDF %s save=%s pages=%r background=%s schema=%s",
                file.filename, save, pages, background, user["schema"])

    call = dict(
        schema=user["schema"],
        file_bytes=file_bytes,
        filename=file.filename,
        username=user["username"],
        bank_id=_clean_bank_id(bank_id),
        password=password,
        save=save,
        pages_spec=pages,
        # None means "the server decides"; 0 is a real choice meaning one pass.
        batch_pages=PDF_BATCH_PAGES if batch_pages is None else batch_pages,
        allow_reimport=allow_reimport,
    )

    try:
        if background:
            return await start_pdf_job(**call)
        return await process_pdf_import(**call)
    except (DuplicateFileError, RuntimeError) as e:
        raise _to_http(e)
    except Exception as e:
        logger.exception("Unexpected error importing PDF")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to parse PDF: {e}"
        )


@router.get("/jobs/{job_id}")
async def get_import_job(job_id: str, user: dict = Depends(get_company_user)):
    """How far along a background import is, and its result once it is done.

    Poll this after POST /imports/pdf with background=true. While it runs the
    interesting fields are `percent` and `message`; on `state: "done"` the
    `result` field holds exactly what a direct upload would have returned, and
    on `state: "failed"` the `error` field holds the message it would have
    raised.
    """
    job = jobs.get(job_id)
    # A job belonging to another company reads as missing rather than
    # forbidden — the same rule the rest of the app follows, so this cannot be
    # used to find out which job ids exist elsewhere.
    if job is None or job.pop("_schema", None) != user["schema"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import job not found.")
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_import_job(job_id: str, user: dict = Depends(get_company_user)):
    """Stop a running background import (the Stop button on the progress screen).

    Best-effort and immediate from the user's side: the job stops reporting
    progress and the overlay comes down right away. A batch's own page-by-page
    read that is already under way on its executor thread runs to completion
    in the background regardless -- Python cannot interrupt a thread mid-call
    -- but its result is discarded (see services/jobs.py::cancel), and nothing
    it finds gets written: both runners only stage rows on the same task this
    cancels, before that write is reached on a cancelled run.
    """
    job = jobs.get(job_id)
    if job is None or job.get("_schema") != user["schema"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import job not found.")
    return {"cancelled": jobs.cancel(job_id)}


def _split_ext(filename: str) -> tuple[str, str]:
    if "." not in filename:
        return filename, ""
    stem, ext = filename.rsplit(".", 1)
    return stem, f".{ext.lower()}"


def _already_marked(filename: str) -> bool:
    """True for a file this endpoint should leave alone on a normal run.

    Checked against the stem, not the raw name, so "statement_done.pdf"
    matches regardless of case -- the same suffix this endpoint itself
    writes after each file. "_needs_password" counts too: that file has
    already been matched to a bank once and is waiting on a person to type a
    password via the retry endpoint below, not for this to guess again with
    the same missing password every run.

    "_failed" deliberately does NOT count -- confirmed with the user. Unlike
    the other two, a failure isn't necessarily permanent: the sender mapping
    or a bank_master row can change after the fact, and once it does the
    same file should be picked back up automatically rather than staying
    stuck until someone finds and re-uploads it by hand.

    Nor does "_bank_absent", for the same reason and more strongly: it means
    exactly "no bank_master row matched this yet". Adding that account is
    what un-hides it, so it has to be re-checked every listing rather than
    filtered out here. See _retryable_stem and the bank test in
    /imports/drive-files.
    """
    stem, _ = _split_ext(filename)
    return stem.lower().endswith(("_done", "_needs_password"))


# Suffixes this app writes that a later run may legitimately reconsider.
# Stripped before parsing so a retried file is read from the same clean base
# name as one seeing this for the first time, instead of stacking a second
# suffix onto the first.
_RETRYABLE_SUFFIXES = ("_failed", "_bank_absent")


def _retryable_stem(stem: str) -> tuple[str, bool]:
    """(clean stem, was_skipped). was_skipped marks a "_bank_absent" file."""
    lowered = stem.lower()
    for suffix in _RETRYABLE_SUFFIXES:
        if lowered.endswith(suffix):
            return stem[: -len(suffix)], suffix == "_bank_absent"
    return stem, False


# "yyyymmdd SHORTNAME LAST4" -- exactly what the Gmail Apps Script names a
# saved attachment. SHORTNAME is free text (a bank name can have a space,
# though today's short codes don't) but LAST4 is anchored to exactly four
# trailing digits so a filename that merely CONTAINS four digits somewhere
# in the middle doesn't false-match.
_DRIVE_FILENAME_RE = re.compile(r"^\d{8} (.+) (\d{4})$")


def _parse_drive_filename(stem: str) -> tuple[str, str] | None:
    """(shortname, last4) from a Gmail-Apps-Script-named file, or None.

    None covers both "doesn't look like our naming pattern at all" (a file
    someone dropped in by hand, or one the script saved under its original
    name because it couldn't extract an account number) and is the signal
    the caller uses to skip rather than guess a bank.
    """
    m = _DRIVE_FILENAME_RE.match(stem)
    return (m.group(1), m.group(2)) if m else None


_PASSWORD_PROBLEM_RE = re.compile(r"ENCRYPTED|password-protected|Incorrect password", re.I)


def _is_password_problem(message: str) -> bool:
    """Same check as the frontend's isPasswordProblem, mirrored here.

    Needed here specifically to tell "wrong/missing password" apart from
    every other reason process_pdf_import can fail (duplicate, unreadable
    file, no fieldmap) -- only the password case gets the softer
    "_needs_password" outcome instead of a hard "_failed".
    """
    return bool(_PASSWORD_PROBLEM_RE.search(message or ""))


# A Drive folder URL, in the shapes the browser actually produces:
#   .../drive/folders/<ID>?usp=sharing
#   .../drive/u/1/folders/<ID>
#   .../open?id=<ID>        (what the "Get link" dialog gives for some items)
# A bare id pasted on its own is accepted too -- someone copying from the
# existing Folder ID field rather than the address bar.
_DRIVE_URL_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_DRIVE_ID_PARAM_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")
_BARE_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def _extract_folder_id(value: str) -> str | None:
    """The folder id inside a pasted Drive URL, or None if there isn't one.

    Done here rather than asking a person to find the id themselves: the id
    is the part of a Drive URL nobody can pick out reliably by eye, and
    getting it subtly wrong (trailing "?usp=sharing", a "/u/1" slot number
    mistaken for it) produces a 404 that looks like a permission problem.
    """
    value = (value or "").strip()
    if not value:
        return None
    for pattern in (_DRIVE_URL_RE, _DRIVE_ID_PARAM_RE):
        m = pattern.search(value)
        if m:
            return m.group(1)
    if "/" not in value and _BARE_FOLDER_ID_RE.match(value):
        return value
    return None


# A Drive FILE link, in the shapes the browser actually produces:
#   .../file/d/<ID>/view?usp=sharing
#   .../open?id=<ID>
#   a Google Sheet's own URL: .../spreadsheets/d/<ID>/edit
# A bare id pasted on its own is accepted too, same as the folder box.
_DRIVE_FILE_URL_RE = re.compile(r"/(?:file|spreadsheets)/d/([A-Za-z0-9_-]+)")


def _extract_file_id(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    for pattern in (_DRIVE_FILE_URL_RE, _DRIVE_ID_PARAM_RE):
        m = pattern.search(value)
        if m:
            return m.group(1)
    if "/" not in value and _BARE_FOLDER_ID_RE.match(value):
        return value
    return None


def _file_id_or_400(url: str) -> str:
    file_id = _extract_file_id(url)
    if not file_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That doesn't look like a Drive file link. Open the file in "
            "Drive and copy the address bar, e.g. "
            "https://drive.google.com/file/d/1AbC.../view")
    return file_id


_GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _open_drive_file(file_id: str) -> dict:
    """Resolve a Drive file id to its metadata, or raise the 400 saying why
    not. Shared by the verify and fetch endpoints below, so "can this app
    open it at all" is answered identically by both -- verify passing and
    fetch then failing (or the reverse) would mean the two had drifted.
    """
    try:
        meta = await asyncio.to_thread(drive.get_file_meta, file_id)
    except Exception:                                  # noqa: BLE001
        # Logged, not swallowed -- same standing as _verify_folder below.
        # Without this the real cause (an expired/invalid DRIVE_TOKEN_JSON,
        # a malformed one, a revoked OAuth client) never reached the logs at
        # all, and every failure here looked identical from the outside: a
        # 400 access-log line with nothing behind it to diagnose.
        logger.exception("Could not open Drive file %s", file_id)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Couldn't open that file. Check the link, and make sure it's "
            "shared with the Google account this app signs in as.")

    if meta.get("mimeType") == "application/vnd.google-apps.folder":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That link points at a folder, not a file. Paste a link to the "
            "statement itself.")

    return meta


@router.post("/drive-link/verify")
async def verify_drive_link_file(
    url: str = Form(..., description="A Drive file (or Google Sheet) URL, or a bare file id"),
    user: dict = Depends(get_company_user),
):
    """Check a pasted Drive file link WITHOUT downloading or importing it.

    The same "well-formed link, wrong file" problem verify_drive_folder
    solves for the Settings-page folder link, here for the one-off file link
    on the Import page: resolves the id and opens it, so a person can see the
    file's own name and confirm it is the statement they meant before
    committing to the download-and-import the Fetch button actually runs.
    Nothing is written and no bytes are downloaded, so this is safe to call
    as often as the box is edited.
    """
    file_id = _file_id_or_400(url)
    meta = await _open_drive_file(file_id)
    return {"name": meta.get("name") or file_id, "mime_type": meta.get("mimeType")}


@router.post("/drive-link/fetch")
async def fetch_drive_link_file(
    url: str = Form(..., description="A Drive file (or Google Sheet) URL, or a bare file id"),
    user: dict = Depends(get_company_user),
):
    """Pulls a single pasted Drive file down to the browser as base64 bytes,
    so it can be handed to the exact same import flow a computer-picked
    file already goes through -- no separate import code path.

    A native Google Sheet has no underlying .xlsx bytes to download, so it
    is exported to one instead; an ordinary uploaded .xlsx/.xls/.csv/.pdf
    downloads as-is.
    """
    file_id = _file_id_or_400(url)
    meta = await _open_drive_file(file_id)

    if meta.get("mimeType") == _GOOGLE_SHEET_MIME:
        file_bytes = await asyncio.to_thread(drive.export_file, file_id, _XLSX_MIME)
        filename = f"{meta.get('name') or file_id}.xlsx"
    else:
        file_bytes = await asyncio.to_thread(drive.download_file, file_id)
        filename = meta.get("name") or file_id

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            "File too large (max 25 MB)")

    return {
        "filename": filename,
        "content_b64": base64.b64encode(file_bytes).decode("ascii"),
    }


def _folder_id_or_400(url: str) -> str:
    """_extract_folder_id, with the "that isn't a folder link" case as a 400."""
    folder_id = _extract_folder_id(url)
    if not folder_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That doesn't look like a Drive folder link. Open the folder in "
            "Drive and copy the address bar, e.g. "
            "https://drive.google.com/drive/folders/1AbC...")
    return folder_id


async def _verify_folder(folder_id: str) -> str:
    """Open the folder and return its name, or raise a 400 saying why not.

    Every folder is checked before it is saved rather than after. A link
    that was mistyped, points at a file, or was never shared with this
    app's Google account otherwise stores fine and fails later, mid-import,
    where it looks like the import itself is broken.
    """
    try:
        return await asyncio.to_thread(drive.get_folder_name, folder_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except Exception:                                  # noqa: BLE001
        logger.exception("Could not open Drive folder %s", folder_id)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Couldn't open that folder. Check the link, and make sure the "
            "folder is shared with the Google account this app signs in as.")


async def _folder_name_or_none(folder_id: str) -> str | None:
    """Best-effort name for a folder that is about to become history, not the
    one being saved. Unlike _verify_folder this never raises -- a folder that
    used to be current and is now deleted or unshared is still worth
    remembering by id, and a lookup failure here must not block saving the
    new one.
    """
    try:
        return await asyncio.to_thread(drive.get_folder_name, folder_id)
    except Exception:                                  # noqa: BLE001
        return None


@router.post("/drive-folder/verify")
async def verify_drive_folder(
    url: str = Form(..., description="A Drive folder URL, or a bare folder id"),
    user: dict = Depends(require_manager),
):
    """
    Check a pasted Drive link WITHOUT saving anything.

    Purely a read: pull the id out of the link, open the folder, and report
    its name and how many files are sitting in it. Nothing is written, so
    this is safe to call as often as someone edits the box.

    The point is that "the folder ID is correct" and "this is the folder I
    meant" are different questions. A well-formed link to the wrong folder
    passes every check the save path can make on its own -- only the folder's
    real name and file count let a person see they pasted last year's
    archive instead of this month's statements, and see it before the
    setting is live rather than after an import comes back empty.

    Manager+, matching the two settings it serves -- it opens an arbitrary
    folder using this app's own Drive grant, which is not something a staff
    account should be able to probe with.
    """
    folder_id = _folder_id_or_400(url)
    name = await _verify_folder(folder_id)
    files = await asyncio.to_thread(drive.list_folder_files, folder_id)
    return {"folder_id": folder_id, "name": name, "file_count": len(files)}


async def _import_folder_or_400(schema: str) -> str:
    """The folder Drive imports read, refusing clearly when none is set."""
    folder_id = await get_import_folder_id(schema)
    if not folder_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Drive folder is set up yet -- set one under Settings first.")
    return folder_id


@router.get("/drive-settings")
async def get_drive_settings(user: dict = Depends(get_company_user)):
    """
    Both Drive folders, for the Settings screen.

    export_folder_id is where the Gmail Apps Script saves attachments.
    import_folder_id is what every Drive import actually reads -- null when
    it simply follows the export folder, which is the usual setup and is
    why the two are reported separately rather than resolved into one. The
    UI needs to be able to say "same as the export folder" rather than echo
    the export folder's id back as though someone had typed it in.

    folder_id is the export folder under its old name, kept so an older
    frontend build still reads the field it expects.

    export_history/import_history are up to 3 folders each setting
    previously held, newest first, excluding whichever is current -- the
    Settings screen's "previously used" links (see
    services.settings.get_drive_folder_history).
    """
    export_id = await get_drive_folder_id(user["schema"])
    import_id = await get_raw_import_folder_id(user["schema"])
    return {
        "folder_id": export_id,
        "export_folder_id": export_id,
        "import_folder_id": import_id,
        "export_history": await get_drive_folder_history(
            user["schema"], "export", exclude_folder_id=export_id),
        "import_history": await get_drive_folder_history(
            user["schema"], "import", exclude_folder_id=import_id),
    }


@router.put("/import-folder")
async def update_import_folder(
    url: str = Form("", description="A Drive folder URL, or a bare folder id. "
                                    "Blank means follow the export folder."),
    user: dict = Depends(require_manager),
):
    """
    Set the folder this software imports FROM.

    Deliberately not the same setting as the export folder: that one is
    shared with the Gmail Apps Script and changing it redirects where
    statements are collected. This one is read-only as far as the script is
    concerned, so pointing it at a folder of statements that never came
    through Gmail changes nothing about the automatic collection.

    Blank clears it, putting imports back to reading the export folder --
    which is the usual setup, and is also what pasting the export folder's
    own URL here amounts to. Both are accepted; the same folder in both
    fields is a perfectly ordinary configuration, not a mistake to reject.

    Manager+, the same tier as changing the export folder: both decide where
    statements are read from.
    """
    # The folder about to stop being current -- recorded as history, not the
    # one being saved, so it shows up as "previously used" the moment it
    # actually becomes previous rather than one save later.
    previous_id = await get_raw_import_folder_id(user["schema"])

    if not url.strip():
        await set_import_folder_id(user["schema"], None)
        if previous_id:
            await record_drive_folder_history(
                user["schema"], "import", previous_id,
                await _folder_name_or_none(previous_id), user["username"])
        return {"import_folder_id": None, "folder_name": None}

    folder_id = _folder_id_or_400(url)
    name = await _verify_folder(folder_id)
    await set_import_folder_id(user["schema"], folder_id)
    if previous_id and previous_id != folder_id:
        await record_drive_folder_history(
            user["schema"], "import", previous_id,
            await _folder_name_or_none(previous_id), user["username"])
    return {"import_folder_id": folder_id, "folder_name": name}


@router.put("/drive-settings")
async def update_drive_settings(
    folder_id: str = Form("", description="A Drive folder URL, or a bare "
                                          "folder id. Alias: url."),
    url: str = Form("", description="Same thing under a clearer name."),
    user: dict = Depends(require_manager),
):
    """
    Change the EXPORT folder -- where the Gmail Apps Script saves statement
    attachments -- from the UI instead of editing config.DRIVE_FOLDER_ID /
    Render's env var by hand.

    Takes a pasted Drive folder URL as readily as a bare id: the id is the
    part of a Drive link nobody picks out reliably by eye, and getting it
    subtly wrong produces a 404 that reads like a permission problem.

    The script keeps its OWN copy of this id (it writes directly into that
    folder, this backend only reads from it) -- so before saving here, the
    new value is posted to the script's deployed web app
    (config.APPS_SCRIPT_WEB_APP_URL) carrying a shared secret the script
    checks in its doPost. If the two ever pointed at different folders, the
    script would keep saving statements one place while this backend looked
    in another, silently.

    Note what this does NOT touch: the import folder. Somebody who only
    wants to read a one-off folder of statements should set that instead
    (PUT /imports/import-folder) -- changing this field would redirect the
    collection itself, which is rarely what was meant.

    Manager+ only, same tier as discarding a batch -- this changes where
    every future statement is filed.
    """
    folder_id = _folder_id_or_400(url.strip() or folder_id.strip())
    name = await _verify_folder(folder_id)
    # The folder about to stop being current -- see update_import_folder's
    # own note on why this is recorded rather than the one being saved.
    previous_id = await get_raw_drive_folder_id(user["schema"])

    if config.APPS_SCRIPT_WEB_APP_URL:
        try:
            # Apps Script web apps answer via a redirect to
            # script.googleusercontent.com -- without follow_redirects the
            # response body here is empty and resp.json() fails on it.
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.post(config.APPS_SCRIPT_WEB_APP_URL, json={
                    "secret": config.APPS_SCRIPT_SHARED_SECRET,
                    "folderId": folder_id,
                })
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Could not reach the Apps Script to update it: {e}")
        if not body.get("ok"):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                body.get("error") or "The Apps Script rejected the update.")

    await set_drive_folder_id(user["schema"], folder_id)
    if previous_id and previous_id != folder_id:
        await record_drive_folder_history(
            user["schema"], "export", previous_id,
            await _folder_name_or_none(previous_id), user["username"])
    return {"folder_id": folder_id, "export_folder_id": folder_id}


@router.get("/drive-log")
async def get_drive_log(
    status_filter: str = Query(
        None, alias="status",
        description="done / failed / password_required / skipped"),
    date_from: str = Query(None, description="yyyy-mm-dd, inclusive"),
    date_to: str = Query(None, description="yyyy-mm-dd, inclusive"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    schema: str = Depends(get_current_schema),
):
    """
    Every file /imports/from-drive (or its retry-password endpoint) has ever
    recorded an outcome for, newest first -- what a manager checks the next
    day to see what happened and why a specific file failed, after the run
    itself has scrolled off screen and the in-memory job (services/jobs.py)
    that drove it has long since been pruned.

    Entries older than services.drive_log.RETENTION_DAYS are dropped on the
    next write, not read here at all -- so this can never show an entry the
    next import will have already deleted.
    """
    rows, total = await list_drive_log(
        schema, status=status_filter, date_from=date_from, date_to=date_to,
        limit=limit, offset=offset)
    return {"rows": rows, "total": total}


@router.post("/drive-cleanup")
async def cleanup_drive_done_files(
    older_than_days: int = Form(..., ge=1),
    user: dict = Depends(require_manager),
):
    """
    Moves every "_done" file in the chosen Drive folder to Drive's own
    Trash, once ITS OWN statement date -- the yyyymmdd embedded in its name,
    not when it happened to be imported -- is at least this many days old.
    Confirmed with the user: age is judged by the statement, not the import.

    Only "_done" files are ever touched. "_failed" and "_needs_password"
    still need a person's attention regardless of how old their statement
    date is, and a file whose name doesn't parse at all is left alone rather
    than guessed at -- the same "don't act without confidence" rule
    /imports/from-drive itself follows when it can't match a bank.

    Trash, not delete: this app's Drive access is Editor-level on the Shared
    Drive, which Google only allows to trash a file -- permanent deletion
    needs Organizer, a higher grant than this integration has. A trashed
    file is recoverable from Drive's own Trash for about 30 days before
    Google purges it there on its own. Manager+ only, the same tier as
    discarding a batch or changing the Drive folder itself.
    """
    folder_id = await _import_folder_or_400(user["schema"])

    cutoff = datetime.date.today() - datetime.timedelta(days=older_than_days)
    drive_files = await asyncio.to_thread(drive.list_folder_files, folder_id)

    trashed = []
    for f in drive_files:
        stem, ext = _split_ext(f["name"])
        if not stem.lower().endswith("_done"):
            continue
        base_stem = stem[: -len("_done")]
        if not _DRIVE_FILENAME_RE.match(base_stem):
            continue
        try:
            statement_date = datetime.datetime.strptime(
                base_stem[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if statement_date <= cutoff:
            await asyncio.to_thread(drive.trash_file, f["id"])
            trashed.append(f["name"])

    return {"trashed": trashed, "count": len(trashed)}


@router.get("/drive-files")
async def list_drive_files(
    user: dict = Depends(get_company_user),
):
    """
    Every file in the chosen Drive folder /imports/from-drive would pick
    up on a normal run -- i.e. not already "_done" or "_needs_password"
    ("_failed" is included: see _already_marked). Powers the picker the
    Import page shows before a Drive run, so a person can select just one or
    a few files instead of always importing everything pending.

    Returns {id, name, unmatched, reason} -- not bare names, since two files
    can legitimately share a name (the Apps Script's own collision race,
    before it was fixed, left some behind) and an id is the only way to act
    on one specific copy rather than "every file called this".

    unmatched is true for a file /imports/from-drive would mark "_failed" on
    sight -- an unparseable filename, or one naming a bank_master account
    that doesn't exist (never set up, or deactivated). Checked here, before
    anyone commits to a run, so the picker can warn about them and offer to
    skip them straight away instead of finding out only after importing.

    A file already skipped ("_bank_absent") is re-checked rather than
    filtered out, and only hidden while the reason it was skipped still
    holds. Adding that account to Master Data is therefore the whole of the
    undo: the next time this list loads, the file is simply back in it. The
    alternative -- hiding it permanently -- strands statements in Drive that
    nothing in the app can see, which is how files get lost.
    """
    folder_id = await _import_folder_or_400(user["schema"])
    drive_files = await asyncio.to_thread(drive.list_folder_files, folder_id)
    candidates = [f for f in drive_files if not _already_marked(f["name"])]

    async with company_connection(user["schema"]) as conn:
        bank_lookup = await bulk_bank_lookup(conn)

    pending = []
    for f in candidates:
        stem, ext = _split_ext(f["name"])
        stem, was_skipped = _retryable_stem(stem)
        hint = _parse_drive_filename(stem)
        if hint is None:
            # A skipped file whose name never parsed cannot start matching
            # later -- no Master Data change can fix an unreadable name --
            # so it stays hidden rather than reappearing on every load.
            if was_skipped:
                continue
            pending.append({
                "id": f["id"], "name": f["name"], "unmatched": True,
                "reason": "Filename doesn't match the expected "
                         "\"yyyymmdd BANK 1234\" pattern.",
            })
            continue
        shortname, last4 = hint
        bank_id = bank_lookup.get((shortname.upper(), last4))
        if was_skipped and bank_id is None:
            continue                    # still no bank -- stay skipped
        pending.append({
            "id": f["id"], "name": f["name"], "unmatched": bank_id is None,
            "reason": None if bank_id is not None else
                     f"No active bank account matches '{shortname}' ending {last4}.",
            # True for a file that was skipped and has since become
            # importable, so the picker can say so rather than having it
            # silently reappear among files nobody has seen before.
            "restored": was_skipped,
        })
    return {"files": pending}


@router.post("/drive-files/{file_id}/skip")
async def skip_drive_file(
    file_id: str,
    user: dict = Depends(get_company_user),
):
    """
    Set a not-yet-imported file aside: rename it "..._bank_absent.ext" so it
    stops appearing in the picker, while leaving it exactly where it is in
    Drive.

    This replaced a delete button. Deleting is the wrong answer to "no bank
    account matches this yet", because that sentence is about Master Data
    and not about the file -- the statement itself is perfectly good, and
    throwing it away to clear a warning loses real data to fix a bookkeeping
    gap. Renaming keeps the statement and clears the warning.

    Nothing has to be undone by hand afterwards: /imports/drive-files
    re-reads the bank out of the name every time it lists, so adding the
    missing account to Master Data brings the file straight back into the
    list. See _already_marked for why "_bank_absent" is deliberately not
    treated as permanently handled the way "_done" is.

    Same permission level as starting a Drive import -- this only ever
    touches a file nothing has processed yet, and it destroys nothing.
    """
    folder_id = await _import_folder_or_400(user["schema"])

    # Confirm the file is actually in that folder before renaming it. The id
    # arrives from the caller, and this app's Drive grant reaches far more
    # than one folder -- without this, a wrong (or invented) id would rename
    # whatever it happened to name, somewhere nobody was looking at.
    drive_files = await asyncio.to_thread(drive.list_folder_files, folder_id)
    match = next((f for f in drive_files if f["id"] == file_id), None)
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "That file isn't in this Drive folder.")

    stem, ext = _split_ext(match["name"])
    if stem.lower().endswith("_bank_absent"):
        return {"status": "skipped", "name": match["name"]}
    # Strip a stale "_failed" first, so a file that failed once and is now
    # being set aside doesn't end up as "..._failed_bank_absent.ext".
    stem, _ = _retryable_stem(stem)
    new_name = f"{stem}_bank_absent{ext}"

    await asyncio.to_thread(drive.rename_file, file_id, new_name)
    await log_drive_result(
        user["schema"], file_name=match["name"], status="skipped",
        imported_by=user["username"],
        error="Set aside — no matching account in Master Data.")
    return {"status": "skipped", "name": new_name}


@router.post("/drive-files/skip-batch")
async def skip_drive_files(
    file_ids: str = Form(..., description="Comma-separated Drive file ids"),
    user: dict = Depends(get_company_user),
):
    """Same as POST /drive-files/{file_id}/skip, for many files in one call.

    The single-file endpoint above lists the whole Drive folder on every
    call, purely to confirm the id it was given actually belongs to it. Fine
    for one file -- wrong for the "Skip N" button on the unmatched-files
    list (ImportPage.jsx's handleSkipUnmatched), which used to call that
    endpoint once per file with Promise.all: N files meant N *concurrent*
    full folder listings plus N renames, all competing for the same small
    executor thread pool and Drive API quota at once. On a small Render
    instance that was enough concurrent memory and connection pressure to
    crash the process outright -- seen in production as "Cannot reach the
    server" right after clicking Skip on ~70 files. This endpoint lists the
    folder exactly once and renames every requested file against that one
    snapshot, sequentially, instead.
    """
    folder_id = await _import_folder_or_400(user["schema"])
    ids = {i.strip() for i in file_ids.split(",") if i.strip()}
    drive_files = await asyncio.to_thread(drive.list_folder_files, folder_id)
    by_id = {f["id"]: f for f in drive_files}

    results = []
    for file_id in ids:
        match = by_id.get(file_id)
        if match is None:
            results.append({"id": file_id, "status": "not_found"})
            continue
        stem, ext = _split_ext(match["name"])
        if stem.lower().endswith("_bank_absent"):
            results.append({"id": file_id, "status": "skipped", "name": match["name"]})
            continue
        # Strip a stale "_failed" first, same as the single-file endpoint --
        # a file that failed once and is now being set aside must not end up
        # as "..._failed_bank_absent.ext".
        stem, _ = _retryable_stem(stem)
        new_name = f"{stem}_bank_absent{ext}"
        await asyncio.to_thread(drive.rename_file, file_id, new_name)
        await log_drive_result(
            user["schema"], file_name=match["name"], status="skipped",
            imported_by=user["username"],
            error="Set aside — no matching account in Master Data.")
        results.append({"id": file_id, "status": "skipped", "name": new_name})
    return {"files": results}


@router.post("/from-drive")
async def import_from_drive(
    pages: str = Form("", description='PDF pages to read: "30", "31-65", or blank for all'),
    batch_pages: int = Form(
        None,
        description="Read each PDF in stretches of this many pages (0 = one pass). "
                    "Omit for the server default.",
    ),
    files: str = Form(
        "",
        description="Comma-separated Drive file IDs to import (the `id` "
                    "field from GET /imports/drive-files). Blank imports "
                    "every pending file.",
    ),
    user: dict = Depends(get_company_user),
):
    """
    Import files sitting in this company's IMPORT folder (Settings, or PUT
    /imports/import-folder).

    That is normally the same folder the Gmail Apps Script exports into --
    the other end of the script that copies matching statement attachments
    there automatically, named "yyyymmdd SHORTNAME LAST4.ext". It can also
    be pointed somewhere else entirely, to take in a folder of statements
    that never came through Gmail; everything below applies unchanged
    either way, including the _done/_failed/_needs_password marking, so a
    part-finished run resumes identically wherever it is reading from.

    `files` restricts the run to exactly the file IDs named -- the Import
    page always sends this, letting a person pick individual files (or
    "select all") from the list GET /imports/drive-files returns rather than
    this always sweeping the whole folder. By id, not name: two files can
    legitimately share a name, and matching by name would import both or
    neither instead of just the one that was actually picked. Left blank,
    every pending file is imported, same as before this picker existed.

    Which bank each file belongs to is read out of that filename, not
    chosen by hand -- SHORTNAME + the account's last 4 digits are matched
    against bank_master (services.staging.find_bank_by_hint). A filename
    that doesn't parse, or names a bank that can't be matched confidently
    (none or more than one), is skipped and marked "_failed" rather than
    imported unassigned -- confirmed with the user.

    Once a bank is known, that bank's own saved PDF password (Master Data's
    Bank tab) is tried automatically -- see process_pdf_import's password
    fallback, which this shares with a plain upload and a Computer batch.
    If the file is protected and that password is missing or wrong, the file
    is marked "_needs_password" instead of "_failed": it has already been
    matched to a real bank, so this isn't a dead end, just something a
    person needs to supply once via POST /imports/from-drive/retry-password.

    pages/batch_pages are the same PDF page-range and batch-size controls
    /imports/pdf takes, applied to every PDF this run finds -- one shared
    setting for the whole run. They have no effect on an Excel/CSV file in
    the same run, same as the single-file form only showing them for a PDF.

    Runs as a background job (services/jobs.py, the same registry used
    elsewhere in this router and by the Farvision export) since this can be
    several files' worth of parsing. Poll GET /imports/jobs/{job_id}; once
    state is "done", result is
    {"files": [{"name", "status", "row_count"|"error"}], "imported", "failed"}.
    status is one of "done", "failed", "password_required", or "skipped".
    """
    folder_id = await _import_folder_or_400(user["schema"])

    clean_batch_pages = PDF_BATCH_PAGES if batch_pages is None else batch_pages
    selected = {n.strip() for n in files.split(",") if n.strip()} or None

    job_id = jobs.create(
        schema=user["schema"], username=user["username"],
        filename="Drive folder", total_units=1, total_pages=None,
    )

    async def _runner():
        jobs.set_state(job_id, jobs.PARSING, "Listing the Drive folder...")
        results = []

        # Records the outcome both in the in-memory job result (what this
        # run's own poller/overlay reads) and in drive_import_log (what
        # survives after the job is pruned) -- one call keeps the two from
        # drifting apart the way two separate call sites eventually would.
        async def _record(name, status_, *, error=None, row_count=None,
                          bank_id=None):
            entry = {"name": name, "status": status_}
            if error is not None:
                entry["error"] = error
            if row_count is not None:
                entry["row_count"] = row_count
            results.append(entry)
            await log_drive_result(
                user["schema"], file_name=name, status=status_,
                imported_by=user["username"], error=error,
                row_count=row_count, bank_id=bank_id)

        try:
            drive_files = await asyncio.to_thread(
                drive.list_folder_files, folder_id)
            pending = [f for f in drive_files if not _already_marked(f["name"])
                      and (selected is None or f["id"] in selected)]

            # One step per file, the same shape a workbook already reports one
            # step per sheet in -- this is what lets the frontend's existing
            # full-screen progress overlay (ImportProgressOverlay, built around
            # that same step_index/step_total/batches_done shape) show a real
            # per-file list for a Drive run without inventing a second
            # component. job_id is deliberately NOT forwarded into
            # process_pdf_import below: that would let a single large PDF's
            # own internal page-batch ticking overwrite this per-file step
            # numbering with its own, losing the "file i of N" framing.
            for i, f in enumerate(pending, start=1):
                jobs.start_step(
                    job_id, index=i, total=len(pending), label=f["name"],
                    units=1, message=f"Importing {f['name']}...")
                stem, ext = _split_ext(f["name"])
                # A previously-failed file being retried still carries the
                # suffix this endpoint wrote onto it last time -- strip it so
                # parsing and every rename below work from the same clean
                # base name as a file seeing this for the first time, instead
                # of stacking a second "_failed"/"_done" onto the first.
                # "_bank_absent" is stripped here too: a file only reaches
                # this run once its account exists, and it should then be
                # named as though it had never been set aside.
                stem, was_skipped = _retryable_stem(stem)

                if ext not in (".pdf", ".xlsx", ".xls", ".csv"):
                    await _record(f["name"], "skipped",
                                  error="Unsupported file type.")
                    jobs.complete_step(job_id, rows=0)
                    continue

                hint = _parse_drive_filename(stem)
                if hint is None:
                    await asyncio.to_thread(
                        drive.rename_file, f["id"], f"{stem}_failed{ext}")
                    await _record(
                        f["name"], "failed",
                        error="Filename doesn't match the expected "
                             "\"yyyymmdd BANK 1234\" pattern, so which "
                             "account this belongs to can't be told.")
                    jobs.complete_step(job_id, rows=0)
                    continue

                async with company_connection(user["schema"]) as conn:
                    matched_bank_id = await find_bank_by_hint(conn, *hint)
                if matched_bank_id is None:
                    # A file someone had already set aside, swept up again by
                    # a blank "import everything" run while its account still
                    # doesn't exist. Put the "_bank_absent" mark back rather
                    # than downgrading it to "_failed": nothing new has been
                    # learned about it, and losing that mark would make it
                    # reappear in the picker on the next load.
                    if was_skipped:
                        await asyncio.to_thread(
                            drive.rename_file, f["id"], f"{stem}_bank_absent{ext}")
                        await _record(
                            f["name"], "skipped",
                            error=f"Still set aside — no active bank account "
                                 f"matches '{hint[0]}' ending {hint[1]}.")
                        jobs.complete_step(job_id, rows=0)
                        continue
                    await asyncio.to_thread(
                        drive.rename_file, f["id"], f"{stem}_failed{ext}")
                    await _record(
                        f["name"], "failed",
                        error=f"No single active bank account matches "
                             f"'{hint[0]}' ending {hint[1]}.")
                    jobs.complete_step(job_id, rows=0)
                    continue

                try:
                    content = await asyncio.to_thread(drive.download_file, f["id"])
                    if ext == ".pdf":
                        res = await process_pdf_import(
                            schema=user["schema"], file_bytes=content,
                            filename=f["name"], username=user["username"],
                            bank_id=matched_bank_id, save=True,
                            pages_spec=pages, batch_pages=clean_batch_pages,
                        )
                    else:
                        res = await process_tabular_import(
                            schema=user["schema"], file_bytes=content,
                            filename=f["name"], username=user["username"],
                            kind="excel" if ext in (".xlsx", ".xls") else "csv",
                            bank_id=matched_bank_id, save=True,
                        )
                    await asyncio.to_thread(
                        drive.rename_file, f["id"], f"{stem}_done{ext}")
                    await _record(f["name"], "done",
                                 row_count=res.get("row_count", 0),
                                 bank_id=matched_bank_id)
                    jobs.complete_step(job_id, rows=res.get("row_count", 0))
                except (DuplicateFileError, RuntimeError) as e:
                    if ext == ".pdf" and _is_password_problem(str(e)):
                        await asyncio.to_thread(
                            drive.rename_file, f["id"], f"{stem}_needs_password{ext}")
                        await _record(f["name"], "password_required",
                                     error=str(e), bank_id=matched_bank_id)
                    else:
                        await asyncio.to_thread(
                            drive.rename_file, f["id"], f"{stem}_failed{ext}")
                        await _record(f["name"], "failed", error=str(e),
                                     bank_id=matched_bank_id)
                    jobs.complete_step(job_id, rows=0)
                except Exception as e:                      # noqa: BLE001
                    logger.exception("Drive import: %s failed", f["name"])
                    await asyncio.to_thread(
                        drive.rename_file, f["id"], f"{stem}_failed{ext}")
                    await _record(f["name"], "failed", error=str(e),
                                 bank_id=matched_bank_id)
                    jobs.complete_step(job_id, rows=0)

            imported = sum(1 for r in results if r["status"] == "done")
            failed = sum(1 for r in results if r["status"] == "failed")
            needs_password = sum(1 for r in results if r["status"] == "password_required")
            jobs.finish(job_id, {
                "files": results, "imported": imported, "failed": failed,
                "needs_password": needs_password,
            })
        except asyncio.CancelledError:
            # Stopped from the import screen (see services/jobs.py::cancel).
            # Whatever file was mid-import when this fired keeps whatever rows
            # it already staged (stage_batch commits before this runner's own
            # await resumes) -- the same partial-file outcome a real crash at
            # that instant would have left, and every file already recorded
            # in `results` is the same as if the run had been asked for only
            # that many files.
            jobs.mark_cancelled(job_id)
        except Exception as exc:                      # noqa: BLE001
            logger.exception("Drive import job %s failed", job_id)
            jobs.fail(job_id, str(exc))

    jobs.attach_task(job_id, asyncio.create_task(_runner()))
    return {"job_id": job_id, "state": jobs.QUEUED}


@router.post("/from-drive/retry-password")
async def retry_drive_file_with_password(
    file_name: str = Form(..., description="The file's current name in Drive, "
                                            "e.g. \"20260415 YES 2477_needs_password.pdf\""),
    password: str = Form(...),
    user: dict = Depends(get_company_user),
):
    """
    Re-attempt one file /imports/from-drive already matched to a bank but
    couldn't open -- its saved password (if any) was missing or wrong.

    Only reachable for a file already carrying "_needs_password": that
    suffix is what proves a bank match already succeeded once, so this
    re-derives the SAME bank_id from the filename rather than trusting a
    bank_id the caller could otherwise pass in for a file it was never
    actually matched to.

    Synchronous, not a background job -- this is always exactly one file, the
    same shape as a plain (non-background) /imports/pdf call.
    """
    folder_id = await _import_folder_or_400(user["schema"])

    stem, ext = _split_ext(file_name)
    if not stem.lower().endswith("_needs_password"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That file isn't waiting on a password -- only a file already "
            "marked \"_needs_password\" can be retried here.")
    base_stem = stem[: -len("_needs_password")]

    hint = _parse_drive_filename(base_stem)
    if hint is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Could not re-read the bank this file was matched to.")

    async with company_connection(user["schema"]) as conn:
        bank_id = await find_bank_by_hint(conn, *hint)
    if bank_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That bank match no longer resolves to exactly one account.")

    drive_files = await asyncio.to_thread(drive.list_folder_files, folder_id)
    match = next((f for f in drive_files if f["name"] == file_name), None)
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "That file is no longer in the Drive folder.")

    content = await asyncio.to_thread(drive.download_file, match["id"])
    try:
        res = await process_pdf_import(
            schema=user["schema"], file_bytes=content, filename=file_name,
            username=user["username"], bank_id=bank_id, password=password, save=True,
        )
    except (DuplicateFileError, RuntimeError) as e:
        # Still waiting on a password (this one was wrong too) rather than a
        # hard failure -- the file itself hasn't moved, so a future retry
        # against the same "_needs_password" name is still possible.
        await log_drive_result(
            user["schema"], file_name=file_name, status="password_required",
            imported_by=user["username"], error=str(e), bank_id=bank_id)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))

    await asyncio.to_thread(drive.rename_file, match["id"], f"{base_stem}_done{ext}")
    await log_drive_result(
        user["schema"], file_name=file_name, status="done",
        imported_by=user["username"], row_count=res.get("row_count", 0),
        bank_id=bank_id)
    return {"status": "done", "row_count": res.get("row_count", 0)}


async def _import_tabular(kind: str, file: UploadFile, save: bool, bank_id,
                          user: dict, sheets: str = "", background: bool = False,
                          allow_reimport: bool = False):
    """Shared body for the Excel and CSV routes — only the reader differs."""
    _, allowed = READERS[kind]
    file_bytes = await _read_upload(file, allowed)
    logger.info("[Import] %s %s save=%s sheets=%r background=%s schema=%s",
                kind, file.filename, save, sheets, background, user["schema"])

    call = dict(
        schema=user["schema"],
        file_bytes=file_bytes,
        filename=file.filename,
        username=user["username"],
        kind=kind,
        bank_id=_clean_bank_id(bank_id),
        save=save,
        sheets=sheets or "",
        allow_reimport=allow_reimport,
    )

    try:
        if background:
            return await start_tabular_job(**call)
        return await process_tabular_import(**call)
    except (DuplicateFileError, RuntimeError) as e:
        raise _to_http(e)
    except Exception as e:
        logger.exception("Unexpected error importing %s", kind)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to parse {kind}: {e}"
        )


@router.post("/excel/inspect")
async def inspect_excel(
    file: UploadFile = File(...),
    user: dict = Depends(get_company_user),
):
    """What is in this workbook, sheet by sheet. Nothing is written.

    This is the spreadsheet's answer to the PDF page selector, and it has to run
    before the user can choose anything: a workbook holds one sheet per account,
    and the tab names alone do not say which sheets are statements, how many
    rows each holds, or whether their columns were recognised.

    Each sheet comes back with `is_statement` — decided on structure, not on the
    sheet's name — plus its header row, the columns that matched, the ones that
    did not, and a five-row sample. A pivot table or a beneficiary list is
    reported with the reason it is not importable rather than left out, so a
    sheet that SHOULD have been a statement is visibly not one.
    """
    file_bytes = await _read_upload(file, (".xlsx", ".xls"))
    try:
        return await inspect_tabular(user["schema"], file_bytes, "excel")
    except RuntimeError as e:
        raise _to_http(e)
    except Exception as e:
        logger.exception("Unexpected error inspecting workbook")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to read workbook: {e}"
        )


@router.post("/excel")
async def import_excel(
    file: UploadFile = File(...),
    save: bool = Form(False, description="false previews, true stages a batch"),
    bank_id: int = Form(None, description="bank_master.id this statement belongs to"),
    sheets: str = Form(
        "",
        description="Comma-separated sheet names to import. Blank imports every "
                    "sheet that looks like a statement.",
    ),
    background: bool = Form(
        False,
        description="true returns a job id immediately; poll GET /imports/jobs/{id}",
    ),
    allow_reimport: bool = Form(
        False,
        description="true re-stages this exact file even though it was "
                    "already uploaded -- see /imports/pdf's own field.",
    ),
    user: dict = Depends(get_company_user),
):
    """Parse an Excel bank statement. Same flow and same options as /imports/pdf.

    A workbook is not one statement. Each sheet is staged as its own batch, so
    eight accounts on eight tabs become eight batches that can be discarded,
    filtered and tied to a bank independently. `sheets` chooses which tabs to
    take — the spreadsheet equivalent of the PDF page range — and blank takes
    every sheet that has a date column and a money column.

    background=true answers with a job id and reports progress per sheet, the
    same way a long PDF does.
    """
    return await _import_tabular("excel", file, save, bank_id, user,
                                 sheets=sheets, background=background,
                                 allow_reimport=allow_reimport)


@router.post("/csv")
async def import_csv(
    file: UploadFile = File(...),
    save: bool = Form(False, description="false previews, true stages a batch"),
    bank_id: int = Form(None, description="bank_master.id this statement belongs to"),
    background: bool = Form(
        False,
        description="true returns a job id immediately; poll GET /imports/jobs/{id}",
    ),
    allow_reimport: bool = Form(
        False,
        description="true re-stages this exact file even though it was "
                    "already uploaded -- see /imports/pdf's own field.",
    ),
    user: dict = Depends(get_company_user),
):
    """
    Parse a CSV bank statement. Same flow as /imports/pdf.

    The delimiter is sniffed (comma, semicolon, tab or pipe) and a UTF-8 BOM is
    stripped, so a CSV exported from Excel imports without pre-editing. A CSV is
    a single sheet, so there is nothing to select.
    """
    return await _import_tabular("csv", file, save, bank_id, user,
                                 background=background, allow_reimport=allow_reimport)


@router.get("/batches")
async def list_batches(
    status_filter: str = Query(None, alias="status", description="uploaded / classified / finalized / failed"),
    schema: str = Depends(get_current_schema),
):
    """
    Every upload for this company, newest first.

    This is the view that made import_batches worth a real table: filename, who
    uploaded it, when, row count and status all come from one row instead of a
    DISTINCT over transaction rows.
    """
    where, params = "", []
    if status_filter:
        where, params = "WHERE b.status = $1", [status_filter]

    async with company_connection(schema) as conn:
        rows = await conn.fetch(
            f"""
            SELECT b.id, b.filename, b.bank_id, b.uploaded_by, b.uploaded_at,
                   b.row_count, b.status, b.failure_reason, b.updated_at,
                   bm.bank_name,
                   (SELECT count(*) FROM temp_trans t WHERE t.batch_id = b.id) AS staged_rows,
                   (SELECT count(*) FROM transactions x
                     WHERE x.temp_trans_id IN (
                         SELECT t.id FROM temp_trans t WHERE t.batch_id = b.id
                     )) AS posted_rows
            FROM import_batches b
            LEFT JOIN bank_master bm ON bm.id = b.bank_id
            {where}
            ORDER BY b.uploaded_at DESC, b.id DESC
            """,
            *params,
        )
    return [dict(r) for r in rows]


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: int, schema: str = Depends(get_current_schema)):
    """One batch, with the rows it staged."""
    async with company_connection(schema) as conn:
        batch = await conn.fetchrow(
            """
            SELECT b.id, b.filename, b.file_hash, b.bank_id, b.uploaded_by,
                   b.uploaded_at, b.row_count, b.status, b.failure_reason,
                   b.parse_stats, bm.bank_name
            FROM import_batches b
            LEFT JOIN bank_master bm ON bm.id = b.bank_id
            WHERE b.id = $1
            """,
            batch_id,
        )
        if batch is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found.")

        # temp_trans has no txn_date / description / balance columns — those
        # values live in the company's field_* columns, whose names differ per
        # company, and in raw_data. raw_data is what carries the row's content
        # here; the fieldmap-shaped view of staging is GET /transactions/temp-trans.
        rows = await conn.fetch(
            """
            SELECT id, row_number, amount, credit_debit, is_classified,
                   project_id, beneficiary_id, head_id, rera_head_id,
                   idw_head_id, row_hash, raw_data
            FROM temp_trans
            WHERE batch_id = $1
            ORDER BY row_number
            """,
            batch_id,
        )

    out_rows = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("raw_data"), str):
            d["raw_data"] = json.loads(d["raw_data"])
        out_rows.append(d)
    out_batch = dict(batch)
    if isinstance(out_batch.get("parse_stats"), str):
        out_batch["parse_stats"] = json.loads(out_batch["parse_stats"])
    return {"batch": out_batch, "rows": out_rows}


@router.delete("/batches/{batch_id}", dependencies=[Depends(require_manager)])
async def discard_batch(batch_id: int, schema: str = Depends(get_current_schema)):
    """
    Discard a batch and everything it staged.

    Refused once any of its rows have been finalized. temp_trans rows cascade
    from the batch, but transactions.temp_trans_id is ON DELETE RESTRICT, so
    Postgres would block the delete anyway — this checks first and explains why
    instead of surfacing a foreign-key error.
    """
    async with company_connection(schema) as conn:
        batch = await conn.fetchrow(
            "SELECT id, filename, status FROM import_batches WHERE id = $1", batch_id
        )
        if batch is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found.")

        posted = await conn.fetchval(
            """
            SELECT count(*) FROM transactions
            WHERE temp_trans_id IN (SELECT id FROM temp_trans WHERE batch_id = $1)
            """,
            batch_id,
        )
        if posted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot discard '{batch['filename']}': {posted} of its rows are "
                f"already posted to the ledger. Reverse those transactions first.",
            )

        staged = await conn.fetchval(
            "SELECT count(*) FROM temp_trans WHERE batch_id = $1", batch_id
        )
        await conn.execute("DELETE FROM import_batches WHERE id = $1", batch_id)

    return {"status": "discarded", "batch_id": batch_id, "rows_removed": staged}
