"""
Field mapping (fieldmap table) routes.

GET    /fieldmap             — list all field mappings
GET    /fieldmap/change-log  — audit trail of every fieldmap edit
POST   /fieldmap             — create a new mapping
PATCH  /fieldmap/{id}        — update a mapping
DELETE /fieldmap/{id}        — delete a mapping (soft)

Every write here is recorded by services.changelog. company_connection wraps the
whole handler in one transaction, so a log row cannot survive a write that
failed, and a write cannot land unlogged.
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

import permissions
from database import company_connection
from routers.auth import get_current_schema, get_current_user, require_level
from services import changelog

router = APIRouter(prefix="/fieldmap", tags=["fieldmap"])

# Field mappings decide how every imported statement is read, so staff see
# them but cannot change them.
require_manager = require_level(permissions.MANAGER)

_ROW_COLUMNS = ("id, fieldname, displayname, mapfields, data_type, method, "
                "is_active, created_at, updated_at")


@router.get("/")
async def list_fieldmap(
    include_inactive: bool = Query(False),
    schema: str = Depends(get_current_schema),
):
    where = "" if include_inactive else "WHERE is_active = true"
    async with company_connection(schema) as conn:
        rows = await conn.fetch(f"SELECT {_ROW_COLUMNS} FROM fieldmap {where} ORDER BY id")
    return [dict(r) for r in rows]


# Declared before /{fieldmap_id} would matter if that route were a GET; it is
# not, so this is only here for readability. Managers only: the log names who
# made each change, which is not staff's to read.
@router.get("/change-log", dependencies=[Depends(require_manager)])
async def fieldmap_change_log(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    fieldname: str = Query(None, description="Only this field's history"),
    action: str = Query(None, description="created / updated / deleted"),
    schema: str = Depends(get_current_schema),
):
    """
    Every recorded fieldmap edit, newest first.

    Paged because this table only grows — nothing clears it, so it is the one
    table here guaranteed to outlive a full read.
    """
    async with company_connection(schema) as conn:
        result = await changelog.fetch(
            conn,
            limit=limit,
            offset=(page - 1) * limit,
            fieldname=(fieldname or "").strip() or None,
            action=(action or "").strip() or None,
        )
    result["page"] = page
    return result


# A field is a fieldmap row AND a real column, created together by
# POST /custom-fields. There is deliberately no endpoint here that makes the row
# on its own.
#
# There used to be. A mapping with no column behind it still competes for a
# statement's headers, and one named Debit/Credit matched the Axis header
# "Amount(INR) Debit/Credit" more specifically than the real amount column's
# "Credit" alias. It took the column and had nowhere to put the value, so every
# row lost its amount and the import failed with "No usable transaction rows
# were found" — an error that says nothing about the mapping that caused it.
#
# fieldname is not patchable below for the same reason: renaming the row does
# not rename the column, so a rename produces the same orphan a create did.


async def _current_fieldname(schema: str, fieldmap_id: int) -> str | None:
    """The name this mapping currently holds, for the rename guard below."""
    async with company_connection(schema) as conn:
        return await conn.fetchval(
            "SELECT fieldname FROM fieldmap WHERE id = $1", fieldmap_id
        )


@router.patch("/{fieldmap_id}", dependencies=[Depends(require_manager)])
async def update_fieldmap(
    fieldmap_id: int,
    body: dict,
    schema: str = Depends(get_current_schema),
    user: dict = Depends(get_current_user),
):
    # fieldname is absent from this list on purpose — it is the name of a real
    # column, and nothing here renames columns. Sending it unchanged is fine and
    # ignored, which is what the edit form does; sending a different one is
    # refused rather than quietly dropped, because a caller that asked to rename
    # a field and got a 200 would believe it happened.
    if 'fieldname' in body:
        current = await _current_fieldname(schema, fieldmap_id)
        if current is None:
            raise HTTPException(404, "Mapping not found.")
        if (body['fieldname'] or '').strip() != current:
            raise HTTPException(
                400,
                f"'{current}' is the name of a real column and cannot be renamed "
                f"here. Delete the field on the Custom Fields page and add it "
                f"again under the new name, or change its Display Name instead.",
            )

    sets = []
    params = []
    idx = 1
    for key in ('displayname', 'mapfields', 'data_type', 'method', 'is_active'):
        if key in body:
            sets.append(f"{key} = ${idx}")
            params.append(body[key])
            idx += 1
    if not sets:
        raise HTTPException(400, "No fields to update.")
    params.append(fieldmap_id)

    async with company_connection(schema) as conn:
        # Read first, so the log can state what each value changed FROM. The
        # UPDATE's RETURNING gives the after; there is no way to get the before
        # once it has run.
        before = await conn.fetchrow(
            f"SELECT {_ROW_COLUMNS} FROM fieldmap WHERE id = $1", fieldmap_id
        )
        if before is None:
            raise HTTPException(404, "Mapping not found.")

        try:
            row = await conn.fetchrow(
                f"UPDATE fieldmap SET {', '.join(sets)} WHERE id = ${idx} RETURNING {_ROW_COLUMNS}",
                *params,
            )
        except Exception as e:
            # Renaming a field onto a name another row already holds trips the
            # unique index. Caught for the same reason create_fieldmap catches
            # it: the caller sent bad input, which is a 400, and letting it
            # escape turned "that name is taken" into an opaque 500.
            raise HTTPException(400, f"Could not update mapping: {e}")
        if row is None:
            raise HTTPException(404, "Mapping not found.")
        await changelog.log_diff(
            conn, before=dict(before), after=dict(row), username=user["username"]
        )
    return dict(row)


@router.delete("/{fieldmap_id}", dependencies=[Depends(require_manager)])
async def delete_fieldmap(
    fieldmap_id: int,
    schema: str = Depends(get_current_schema),
    user: dict = Depends(get_current_user),
):
    async with company_connection(schema) as conn:
        row = await conn.fetchrow(
            f"UPDATE fieldmap SET is_active = false WHERE id = $1 AND is_active = true "
            f"RETURNING {_ROW_COLUMNS}",
            fieldmap_id,
        )
        if row is None:
            raise HTTPException(400, "Already deleted or not found.")
        # A soft delete, but logged as a delete: is_active = false is what
        # removes the field from the importer, so that is the event worth
        # recording. The aliases go into old_value because the row still holds
        # them and putting the field back means restoring exactly those.
        await changelog.log_deleted(conn, row=dict(row), username=user["username"])
    return {"status": "deleted"}
