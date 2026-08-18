"""
Transaction routes.

GET    /transactions                  — list finalized transactions (paged)
GET    /transactions/summary          — totals by head for a date range
GET    /temp-trans                    — list raw staged rows (paged)
DELETE /temp-trans                    — clear the whole staging table
DELETE /temp-trans/{row_id}           — remove one staged row
POST   /temp-trans/{row_id}/classify  — tag a raw row with a head
POST   /temp-trans/{row_id}/finalize  — move a row into the ledger

Both list endpoints are paged and searchable, and both return
{columns, rows, total, page, limit}. They used to return every row in the table
on every render, which was fine at a few hundred and is not at a few hundred
thousand — one statement import is several hundred rows on its own.
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

import permissions
from database import company_connection
from routers.auth import get_current_schema, get_current_user, require_level
from services import custom_fields, scoping

router = APIRouter(prefix="/transactions", tags=["transactions"])

# Clearing staging throws away everyone's un-posted work at once, so it is
# manager and above — the same bar as discarding a single batch.
require_manager = require_level(permissions.MANAGER)

# Which master table backs each classification id. Nothing in this file names a
# head, a beneficiary or a project literally — a row is only classifiable
# against rows that exist in this company's own master tables right now, and
# every company keeps its own copies in its own schema.
_MASTER_LOOKUPS = {
    "head_id": ("head_master", "head"),
    "rera_head_id": ("rera_head_master", "RERA head"),
    "idw_head_id": ("idw_head_master", "IDW head"),
    "beneficiary_id": ("beneficiary_master", "beneficiary"),
    "project_id": ("projects", "project"),
}


# Page size ceiling. Export is the route for "give me everything" — it streams
# instead of building one JSON array in memory, which is the actual reason a
# list endpoint should not be asked for 200,000 rows.
MAX_PAGE_SIZE = 500


def _search_filter(term: str, columns: list[dict], extra_exprs: tuple[str, ...],
                   idx: int) -> tuple[str, list, int]:
    """A WHERE fragment matching *term* against everything visible on the row.

    Every data column plus the joined master names, so what the search matches
    is what the table draws — searching for "SALARY" or a UTR finds the row
    whether that text sits in the narration or in the beneficiary it was filed
    against.

    Non-text columns are cast rather than skipped, which is what makes a date
    findable as "2026-08" and an amount as "1500". DPL restricted its search to
    the id column to avoid "345" matching a narration ending in 345; that was
    the right call for a lookup-by-id box and the wrong one here, where the
    question is "where did this money go", not "show me row 345".

    One bind parameter reused across every column, so the term is never
    interpolated. Column names come from data_columns(), which reads the
    catalog — they are never user input.

    A leading-wildcard ILIKE cannot use an index; this is a sequential scan by
    construction. Fine at statement scale, and the reason limit is capped.
    """
    targets = [f"t.{c['name']}::text" for c in columns] + list(extra_exprs)
    if not targets:
        return "", [], idx
    ors = " OR ".join(f"{t} ILIKE ${idx}" for t in targets)
    return f"({ors})", [f"%{term}%"], idx + 1


async def _assert_live_master_ids(conn, values: dict) -> None:
    """Every non-null id must name an active row in this company's masters.

    Without this the only feedback on a stale dropdown is a raw Postgres
    foreign-key error, and an archived head stays bookable forever because the
    foreign key only checks existence, not is_active.
    """
    for field, value in values.items():
        if value is None:
            continue
        table, label = _MASTER_LOOKUPS[field]
        ok = await conn.fetchval(
            f"SELECT 1 FROM {table} WHERE id = $1 AND is_active = true", value
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No active {label} with id {value} in this company. "
                    f"It may have been archived since the page loaded — reload and retry."
                ),
            )


# ---------- Transactions (the ledger) ----------------------------------------

@router.get("/")
async def list_transactions(
    project_id: int = None,
    head_id: int = None,
    date_from: str = None,
    date_to: str = None,
    search: str = Query("", description="Match any column or master name"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(get_current_user),
):
    """
    List finalized transactions, optionally filtered.

    A manager or staff member sees only rows belonging to their assigned
    projects. Admins see everything. The project_id query param narrows within
    that; it cannot widen it.

    Query params (all optional):
      project_id  — only transactions for this project
      head_id     — only transactions for this head
      date_from   — start date (YYYY-MM-DD)
      date_to     — end date (YYYY-MM-DD)
      search      — free text, matched against every column on the row
      page, limit — pagination; `total` in the response is the unpaged count

    Returns {columns, rows, total, page, limit}. It returned a bare array until
    it was paged; anything summing the response has to read `total` now, because
    rows is one page and len(rows) is a page size, not a count.
    """
    filters = ["1=1"]
    params = []
    idx = 1

    if project_id is not None:
        filters.append(f"project_id = ${idx}")
        params.append(project_id)
        idx += 1
    if head_id is not None:
        filters.append(f"head_id = ${idx}")
        params.append(head_id)
        idx += 1
    async with company_connection(user["schema"]) as conn:
        # Which column holds the date is the fieldmap's answer, not a constant.
        # Applied here rather than above because it needs a connection.
        date_col = await custom_fields.date_column(conn)
        if date_col:
            for value, op in ((date_from, ">="), (date_to, "<=")):
                if value is not None:
                    filters.append(f"{date_col} {op} ${idx}")
                    params.append(value)
                    idx += 1

        # Same rule as staging: the ledger reports its own columns rather than
        # asserting a fixed set, and they are the same set because the two
        # tables are kept in step.
        columns = await custom_fields.data_columns(conn)

        scope = await scoping.visible_project_ids(conn, user)
        if scoping.scope_is_empty(scope):
            # Still report the columns. An empty result is a row count of zero,
            # not a table with no shape — the client draws its header from this.
            return {"columns": columns, "rows": [], "total": 0,
                    "page": page, "limit": limit}
        # include_unassigned=False: the "unfiled rows belong to everyone" rule
        # exists so a fresh import can be classified, which only concerns
        # staging. A row that reached the ledger with no project is filed data
        # nobody's project owns, and only admins see it.
        clause, scope_params, idx = scoping.project_filter(
            scope, "t.project_id", idx, include_unassigned=False
        )
        if clause:
            filters.append(clause)
            params.extend(scope_params)

        term = (search or "").strip()
        if term:
            clause, sp, idx = _search_filter(
                term, columns, ("p.name", "p.code", "h.name", "b.bank_name"), idx
            )
            if clause:
                filters.append(clause)
                params.extend(sp)

        where = " AND ".join(filters)
        data_cols = ", ".join(f"t.{c['name']}" for c in columns)

        # The joins are repeated in the count because the search reaches into
        # them — counting over a bare temp_trans would over-report the moment
        # someone searches a head name.
        joins = """
            FROM transactions t
            LEFT JOIN projects p ON p.id = t.project_id
            LEFT JOIN head_master h ON h.id = t.head_id
            LEFT JOIN bank_master b ON b.id = t.bank_id
        """

        total = await conn.fetchval(f"SELECT count(*) {joins} WHERE {where}", *params)

        rows = await conn.fetch(
            f"""
            SELECT t.id, t.temp_trans_id, t.created_at,
                   t.project_id, t.bank_id, t.beneficiary_id,
                   t.head_id, t.rera_head_id, t.idw_head_id,
                   {data_cols},
                   p.name AS project_name,
                   p.code AS project_code,
                   h.name AS head_name,
                   b.bank_name
            {joins}
            WHERE {where}
            ORDER BY {f't.{date_col} DESC,' if date_col else ''} t.id DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, limit, (page - 1) * limit,
        )
    return {
        "columns": columns,
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/summary")
async def transaction_summary(
    date_from: str = None,
    date_to: str = None,
    user: dict = Depends(get_current_user),
):
    """
    Total amounts by head, for a date range.
    Returns one row per head with total CR and DR.

    Scoped like /transactions, so the dashboard totals a manager sees are the
    totals of their own projects, not the company's.
    """
    filters = ["1=1"]
    params = []
    idx = 1

    async with company_connection(user["schema"]) as conn:
        # Which column holds the date is the fieldmap's answer, not a constant.
        # Applied here rather than above because it needs a connection.
        date_col = await custom_fields.date_column(conn)
        if date_col:
            for value, op in ((date_from, ">="), (date_to, "<=")):
                if value is not None:
                    filters.append(f"{date_col} {op} ${idx}")
                    params.append(value)
                    idx += 1

        scope = await scoping.visible_project_ids(conn, user)
        if scoping.scope_is_empty(scope):
            return []
        # include_unassigned=False: the "unfiled rows belong to everyone" rule
        # exists so a fresh import can be classified, which only concerns
        # staging. A row that reached the ledger with no project is filed data
        # nobody's project owns, and only admins see it.
        clause, scope_params, idx = scoping.project_filter(
            scope, "t.project_id", idx, include_unassigned=False
        )
        if clause:
            filters.append(clause)
            params.extend(scope_params)

        where = " AND ".join(filters)

        rows = await conn.fetch(
            f"""
            SELECT h.name AS head_name,
                   SUM(CASE WHEN t.credit_debit = 'CR' THEN t.amount ELSE 0 END) AS total_cr,
                   SUM(CASE WHEN t.credit_debit = 'DR' THEN t.amount ELSE 0 END) AS total_dr
            FROM transactions t
            LEFT JOIN head_master h ON h.id = t.head_id
            WHERE {where}
            GROUP BY h.name
            ORDER BY h.name
            """,
            *params,
        )
    return [dict(r) for r in rows]


# ---------- Temp Import (raw rows before finalization) -----------------------

@router.get("/temp-trans")
async def list_temp_trans(
    batch_id: int = None,
    classified: bool = None,
    search: str = Query("", description="Match any column or master name"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(get_current_user),
):
    """
    List raw rows from the last import, before they're finalized.

    Scoped rows follow the same rule as the ledger, and the "no project yet"
    arm carries the weight here: a freshly imported row has no project until
    someone classifies it, so every manager and staff member can see it and
    file it. Once it names a project, only that project's people keep seeing it.

    Params:
      batch_id    — filter by import batch (which PDF upload)
      classified  — true = only classified rows, false = only unclassified
      search      — free text, matched against every column on the row
      page, limit — pagination; `total` is the count matching the filters

    `total` is the filtered count and `summary` is deliberately not: the Clear
    button has to say how much it will delete, which is everything staged, not
    what the current tab and search happen to show.
    """
    filters = ["1=1"]
    params = []
    idx = 1

    if batch_id is not None:
        filters.append(f"t.batch_id = ${idx}")
        params.append(batch_id)
        idx += 1
    if classified is not None:
        filters.append(f"t.is_classified = ${idx}")
        params.append(classified)
        idx += 1

    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)
        clause, scope_params, idx = scoping.project_filter(scope, "t.project_id", idx)
        if clause:
            filters.append(clause)
            params.extend(scope_params)
        elif scoping.scope_is_empty(scope):
            # Scoped to nothing, but unfiled rows are still everyone's to claim.
            filters.append("t.project_id IS NULL")

        # The data columns are read from the live table, not written out here.
        # A custom field is a real column on temp_trans, and a fixed SELECT is
        # why one could be created, matched during parsing and stored, and still
        # never appear on this screen. Same approach as DPL's get_master_rows:
        # the server decides the column set, the client renders what it is sent.
        columns = await custom_fields.data_columns(conn)
        data_cols = ", ".join(f"t.{c['name']}" for c in columns)

        term = (search or "").strip()
        if term:
            clause, sp, idx = _search_filter(
                term, columns,
                ("p.name", "p.code", "h.name", "rh.name", "ih.name", "bn.name"), idx
            )
            if clause:
                filters.append(clause)
                params.extend(sp)

        where = " AND ".join(filters)

        # The joins resolve each id to the name the user picked in the master
        # tables, so the staging screen can show "Site Materials" rather than
        # "head_id: 4" without the browser holding a copy of every master list.
        # Repeated in the count query because the search reaches into them.
        joins = """
            FROM temp_trans t
            LEFT JOIN projects            p  ON p.id  = t.project_id
            LEFT JOIN head_master         h  ON h.id  = t.head_id
            LEFT JOIN rera_head_master    rh ON rh.id = t.rera_head_id
            LEFT JOIN idw_head_master     ih ON ih.id = t.idw_head_id
            LEFT JOIN beneficiary_master  bn ON bn.id = t.beneficiary_id
        """

        total = await conn.fetchval(f"SELECT count(*) {joins} WHERE {where}", *params)

        rows = await conn.fetch(
            f"""
            SELECT t.id, t.batch_id, t.row_number, t.is_classified, t.created_at,
                   t.project_id, t.beneficiary_id, t.head_id, t.rera_head_id,
                   t.idw_head_id,
                   {data_cols},
                   p.name  AS project_name,
                   p.code  AS project_code,
                   h.name  AS head_name,
                   rh.name AS rera_head_name,
                   ih.name AS idw_head_name,
                   bn.name AS beneficiary_name
            {joins}
            WHERE {where}
            ORDER BY t.batch_id, t.row_number
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, limit, (page - 1) * limit,
        )

        # Unfiltered totals, so the Clear button can state what it is about to
        # remove and grey itself out when there is nothing to remove. Taken
        # here rather than counted in the browser, which only ever holds the
        # rows matching the current tab.
        summary = dict(await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM temp_trans)      AS staged_total,
                   (SELECT count(*) FROM import_batches)  AS batches,
                   (SELECT count(*) FROM transactions
                     WHERE temp_trans_id IS NOT NULL)     AS posted
            """
        ))

    return {
        "columns": columns,
        "rows": [dict(r) for r in rows],
        "summary": summary,
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.delete("/temp-trans", dependencies=[Depends(require_manager)])
async def clear_temp_trans(schema: str = Depends(get_current_schema)):
    """
    Clear the staging table — every staged row, from every batch.

    DPL's "Truncate All Data" button, adapted to the one thing that differs
    here: `master` stood alone, but temp_trans has the ledger hanging off it.
    So this is a guarded DELETE, never TRUNCATE. `TRUNCATE temp_trans CASCADE`
    would silently take `transactions` with it, and losing the ledger to a
    "clear the import staging area" button is not a recoverable mistake.

    Refused outright if any staged row has been posted. transactions.
    temp_trans_id is ON DELETE RESTRICT, so Postgres would block it anyway —
    checking first turns a foreign-key error into a sentence that says which
    rows are in the way.

    The batches go too. They cascade to their rows, and leaving them behind
    would keep every file_hash on record, so re-importing the same statement
    you just cleared would come back 409 "already uploaded".

    Not scoped by project: this is an all-or-nothing reset, and clearing "the
    rows I can see" would leave a half-empty staging table that looks cleared
    to the person who pressed the button and not to anyone else.
    """
    async with company_connection(schema) as conn:
        posted = await conn.fetchval(
            "SELECT count(*) FROM transactions WHERE temp_trans_id IS NOT NULL"
        )
        if posted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot clear staging: {posted} staged "
                f"{'row is' if posted == 1 else 'rows are'} already posted to "
                f"the ledger. Reverse those transactions first, or discard the "
                f"unposted batches individually.",
            )

        rows = await conn.fetchval("SELECT count(*) FROM temp_trans")
        batches = await conn.fetchval("SELECT count(*) FROM import_batches")
        # One statement: temp_trans cascades from import_batches, so deleting
        # the parents clears both sides atomically.
        await conn.execute("DELETE FROM import_batches")
        # Anything left had no batch behind it — belt and braces.
        await conn.execute("DELETE FROM temp_trans")

    return {"status": "cleared", "rows_removed": rows, "batches_removed": batches}


@router.delete("/temp-trans/{row_id}", dependencies=[Depends(require_manager)])
async def delete_temp_row(row_id: int, user: dict = Depends(get_current_user)):
    """
    Remove one staged row.

    The narrow version of Clear All, and the reason it exists: a parser will
    occasionally turn a page header or a carried-forward balance line into a
    transaction, and the only fix available was to clear the entire staging
    table and re-import every statement in it.

    Manager and above, matching Clear All and discard-batch. Deleting a staged
    row destroys parsed work, and the three destructive operations on this data
    should not sit at two different levels.

    Two refusals, in this order:
      * outside your scope — 404, the same answer as a row that does not exist,
        so this cannot be used to probe which ids belong to other projects
      * already posted — 409. transactions.temp_trans_id is ON DELETE RESTRICT,
        so Postgres blocks it regardless; checking first names the transaction
        that is holding on instead of surfacing a foreign-key error.

    The batch is left alone even when this empties it. row_count records what
    the file produced at import time, which is history and stays true; the
    batches list already counts live rows separately.
    """
    async with company_connection(user["schema"]) as conn:
        row = await conn.fetchrow(
            "SELECT id, batch_id, row_number, project_id FROM temp_trans WHERE id = $1",
            row_id,
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Staged row not found.")

        scope = await scoping.visible_project_ids(conn, user)
        # can_use_project passes a NULL project, which is the rule the list uses
        # too: an unfiled row belongs to everyone, so a row nobody has
        # classified yet is still removable.
        if not scoping.can_use_project(scope, row["project_id"]):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Staged row not found.")

        posted = await conn.fetchval(
            "SELECT id FROM transactions WHERE temp_trans_id = $1", row_id
        )
        if posted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Row {row_id} is already posted to the ledger as transaction "
                f"{posted}. Reverse that transaction before removing the staged row.",
            )

        await conn.execute("DELETE FROM temp_trans WHERE id = $1", row_id)

    return {"status": "deleted", "row_id": row_id, "batch_id": row["batch_id"]}


@router.post("/temp-trans/{row_id}/classify")
async def classify_row(
    row_id: int,
    head_id: int = Body(None, description="head_master.id"),
    rera_head_id: int = Body(None, description="rera_head_master.id"),
    idw_head_id: int = Body(None, description="idw_head_master.id"),
    project_id: int = Body(None, description="projects.id"),
    beneficiary_id: int = Body(None, description="beneficiary_master.id"),
    user: dict = Depends(get_current_user),
):
    """
    Tag a raw row with a head (category) before finalizing.
    At least one of head_id, rera_head_id, idw_head_id must be provided.
    project_id and beneficiary_id are optional and carried into the ledger.

    Every id is checked against this company's master tables first, so a value
    can only come from a row someone actually created in Master Data.

    Body(...), not bare defaults. A scalar parameter with a plain default is a
    *query* parameter to FastAPI, so the JSON the frontend was posting never
    reached the handler and every classify attempt failed on "Provide at least
    one of: head_id, rera_head_id, idw_head_id". With several Body params
    FastAPI embeds them into one object, which is the shape already being sent.

    Two scope checks, not one. The row has to be visible to this user, and the
    project they are filing it under has to be one of theirs — otherwise
    classifying would be a way to push rows into a project you cannot see, or
    to move a row out of your own scope and lose it.
    """
    if not any([head_id, rera_head_id, idw_head_id]):
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: head_id, rera_head_id, idw_head_id",
        )

    sets = []
    params = []
    idx = 1

    if head_id is not None:
        sets.append(f"head_id = ${idx}")
        params.append(head_id)
        idx += 1
    if rera_head_id is not None:
        sets.append(f"rera_head_id = ${idx}")
        params.append(rera_head_id)
        idx += 1
    if idw_head_id is not None:
        sets.append(f"idw_head_id = ${idx}")
        params.append(idw_head_id)
        idx += 1
    if project_id is not None:
        sets.append(f"project_id = ${idx}")
        params.append(project_id)
        idx += 1
    if beneficiary_id is not None:
        sets.append(f"beneficiary_id = ${idx}")
        params.append(beneficiary_id)
        idx += 1

    sets.append("is_classified = true")
    params.append(row_id)

    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)

        if not scoping.can_use_project(scope, project_id):
            raise HTTPException(
                status_code=403,
                detail="You are not assigned to that project.",
            )

        await _assert_live_master_ids(conn, {
            "head_id": head_id,
            "rera_head_id": rera_head_id,
            "idw_head_id": idw_head_id,
            "beneficiary_id": beneficiary_id,
            "project_id": project_id,
        })

        current = await conn.fetchrow(
            "SELECT project_id FROM temp_trans WHERE id = $1", row_id
        )
        if current is None or not scoping.can_use_project(scope, current["project_id"]):
            raise HTTPException(status_code=404, detail="Row not found.")

        row = await conn.fetchrow(
            f"""
            UPDATE temp_trans
            SET {", ".join(sets)}
            WHERE id = ${idx} AND is_classified = false
            RETURNING id
            """,
            *params,
        )

    if row is None:
        raise HTTPException(
            status_code=400,
            detail="Row not found or already classified.",
        )

    return {"status": "classified", "row_id": row_id}


@router.post("/temp-trans/{row_id}/finalize")
async def finalize_row(
    row_id: int,
    user: dict = Depends(get_current_user),
):
    """
    Move a classified row from temp_trans into the transactions ledger.

    This is the point of no return — after this, the transaction exists in
    the real ledger. The UNIQUE (temp_trans_id) constraint on transactions
    means clicking this twice gives an error, not a double-post.
    """
    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)

        # First, grab the raw row and its linked data.
        raw = await conn.fetchrow(
            """
            SELECT t.batch_id, t.head_id, t.rera_head_id, t.idw_head_id,
                   t.project_id
            FROM temp_trans t
            WHERE t.id = $1 AND t.is_classified = true
            """,
            row_id,
        )

        if raw is None:
            raise HTTPException(
                status_code=400,
                detail="Row not found or not classified. Classify it first.",
            )

        if not scoping.can_use_project(scope, raw["project_id"]):
            raise HTTPException(status_code=404, detail="Row not found.")

        # The data columns are read from the live tables and carried across
        # one-for-one. temp_trans and transactions are kept to the same set of
        # them (migration 007, and every custom-field create/delete alters
        # both), so this copies whatever the company has configured today
        # instead of the five columns that happened to exist when it was
        # written. Naming them is what broke finalize when txn_date,
        # description and balance were deleted as fields.
        # hide_redundant=False: amount and credit_debit are hidden from the
        # staging and ledger views when the bank's own debit/credit columns are
        # present, but they are still real, still NOT NULL on transactions, and
        # still what the ledger totals on. Copying is not displaying.
        carried = [
            c["name"] for c in await custom_fields.data_columns(conn, hide_redundant=False)
        ]
        cols = ", ".join(carried)
        src = ", ".join(f"t.{c}" for c in carried)

        # UNIQUE (temp_trans_id) on transactions means a second call here
        # raises a Postgres error — double-click is safe.
        try:
            txn = await conn.fetchrow(
                f"""
                INSERT INTO transactions (
                    {cols},
                    project_id, bank_id, beneficiary_id, head_id, rera_head_id,
                    idw_head_id, temp_trans_id
                )
                SELECT
                    {src},
                    t.project_id,
                    (SELECT bank_id FROM import_batches WHERE id = t.batch_id),
                    t.beneficiary_id, t.head_id, t.rera_head_id, t.idw_head_id,
                    t.id
                FROM temp_trans t
                WHERE t.id = $1
                RETURNING id
                """,
                row_id,
            )
        except Exception as e:
            # UNIQUE violation = already finalized.
            if "unique" in str(e).lower():
                raise HTTPException(
                    status_code=400,
                    detail="This row is already finalized.",
                )
            raise

    # Only the id is echoed back: which data columns exist is the company's
    # choice, so there is no fixed set of them to report here. The caller
    # reloads the ledger, which describes its own columns.
    return {"status": "finalized", "transaction_id": txn["id"]}

