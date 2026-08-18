"""
Import routes — PDF, Excel and CSV bank statements.

POST   /imports/pdf              — parse a PDF; save=false previews, save=true stages
POST   /imports/excel            — same, for .xlsx / .xls
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
import logging

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile, status)

import permissions
from database import company_connection
from routers.auth import get_current_schema, require_level, get_current_user
from services.pdf_import import process_pdf_import
from services.staging import DuplicateFileError
from services.tabular_import import READERS, process_tabular_import

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
    user: dict = Depends(get_current_user),
):
    """
    Parse a PDF bank statement.

    save=false is a dry run — nothing is written. Use it first and check
    `headers_detected` and `fill_rates`: if `balance` is filled on 3 of 180
    rows, the balance column was not matched, and the fix is a fieldmap alias,
    not a re-upload.

    save=true stages the rows into temp_trans under a new batch. They are not
    in the ledger yet — classify and finalize move them there.
    """
    file_bytes = await _read_upload(file, (".pdf",))
    logger.info("[Import] PDF %s save=%s schema=%s", file.filename, save, user["schema"])

    try:
        return await process_pdf_import(
            schema=user["schema"],
            file_bytes=file_bytes,
            filename=file.filename,
            username=user["username"],
            bank_id=bank_id,
            password=password,
            save=save,
        )
    except (DuplicateFileError, RuntimeError) as e:
        raise _to_http(e)
    except Exception as e:
        logger.exception("Unexpected error importing PDF")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to parse PDF: {e}"
        )


async def _import_tabular(kind: str, file: UploadFile, save: bool, bank_id, user: dict):
    """Shared body for the Excel and CSV routes — only the reader differs."""
    _, allowed = READERS[kind]
    file_bytes = await _read_upload(file, allowed)
    logger.info("[Import] %s %s save=%s schema=%s", kind, file.filename, save, user["schema"])

    try:
        return await process_tabular_import(
            schema=user["schema"],
            file_bytes=file_bytes,
            filename=file.filename,
            username=user["username"],
            kind=kind,
            bank_id=bank_id,
            save=save,
        )
    except (DuplicateFileError, RuntimeError) as e:
        raise _to_http(e)
    except Exception as e:
        logger.exception("Unexpected error importing %s", kind)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to parse {kind}: {e}"
        )


@router.post("/excel")
async def import_excel(
    file: UploadFile = File(...),
    save: bool = Form(False, description="false previews, true stages a batch"),
    bank_id: int = Form(None, description="bank_master.id this statement belongs to"),
    user: dict = Depends(get_current_user),
):
    """Parse an Excel bank statement. Same two-step flow as /imports/pdf."""
    return await _import_tabular("excel", file, save, bank_id, user)


@router.post("/csv")
async def import_csv(
    file: UploadFile = File(...),
    save: bool = Form(False, description="false previews, true stages a batch"),
    bank_id: int = Form(None, description="bank_master.id this statement belongs to"),
    user: dict = Depends(get_current_user),
):
    """
    Parse a CSV bank statement. Same two-step flow as /imports/pdf.

    The delimiter is sniffed (comma, semicolon, tab or pipe) and a UTF-8 BOM is
    stripped, so a CSV exported from Excel imports without pre-editing.
    """
    return await _import_tabular("csv", file, save, bank_id, user)


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

        rows = await conn.fetch(
            """
            SELECT id, row_number, txn_date, description, amount, credit_debit,
                   balance, is_classified, project_id, beneficiary_id,
                   head_id, rera_head_id, idw_head_id, row_hash
            FROM temp_trans
            WHERE batch_id = $1
            ORDER BY row_number
            """,
            batch_id,
        )

    return {"batch": dict(batch), "rows": [dict(r) for r in rows]}


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
