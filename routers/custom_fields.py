"""
Custom field routes — add and remove columns on temp_trans.

GET    /custom-fields/                 — every fieldmap row + its real column type
POST   /custom-fields/                 — add a date / num / text column
DELETE /custom-fields/{fieldname}      — drop the column and its fieldmap row
GET    /custom-fields/table-structure  — live columns on temp_trans

Ported from DPL_project's /api/custom-fields and /api/table-structure. Same
request and response shapes, so the page behaves the way it did there.

Separate from /fieldmap even though both write the fieldmap table: /fieldmap
edits how an existing column is recognised, this creates and destroys the
column itself. Mixing them is what let a page that only ever edited aliases get
named "Custom Fields" and look like this feature while doing none of it.
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, status

import permissions
from database import company_connection
from routers.auth import get_current_schema, get_current_user, require_level
from services import changelog
from services import custom_fields as cf

router = APIRouter(prefix="/custom-fields", tags=["custom-fields"])
logger = logging.getLogger(__name__)

# Adding or dropping a column changes the shape of every future import, so this
# is manager and above. Reading the list is not gated — staff need to see which
# fields exist to make sense of the import preview.
require_manager = require_level(permissions.MANAGER)


@router.get("/")
async def list_fields(schema: str = Depends(get_current_schema)):
    """Every fieldmap row with the type of the column behind it.

    `has_column: false` means the fieldmap row is orphaned — the column was
    dropped outside the app. Shown rather than hidden so it can be cleaned up.
    """
    async with company_connection(schema) as conn:
        return await cf.list_custom_fields(conn)


@router.get("/table-structure")
async def table_structure(schema: str = Depends(get_current_schema)):
    """Live columns on temp_trans. DPL's /api/table-structure."""
    async with company_connection(schema) as conn:
        return await cf.table_structure(conn)


@router.post("/", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_manager)])
async def create_field(
    type: str = Body(..., embed=True, description="date | num | text"),
    displayname: str = Body("", embed=True),
    mapfields: str = Body("", embed=True, description="Comma-separated header aliases"),
    method: str = Body("", embed=True, description="What the field is for: import / selection / rule / ..."),
    schema: str = Depends(get_current_schema),
    user: dict = Depends(get_current_user),
):
    """
    Add a custom field.

    The column is named for you — field_num_1, field_num_2, ... — because the
    name has to be a safe SQL identifier and letting it be typed in is how a
    DROP COLUMN gets an injection surface. The label you actually see is
    `displayname`, which is free text and editable afterwards.
    """
    async with company_connection(schema) as conn:
        try:
            result = await cf.create_custom_field(conn, type, displayname, mapfields, method)
        except cf.CustomFieldError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        # Creating a custom field writes a fieldmap row, so it belongs in the
        # same history as an edit made from the Field Mapping page. Logging it
        # in both places is what keeps a field's whole life on one screen.
        await changelog.log_created(conn, row=result["fieldmap"], username=user["username"])
    return result


@router.delete("/by-id/{fieldmap_id}", dependencies=[Depends(require_manager)])
async def delete_field_by_id(
    fieldmap_id: int,
    schema: str = Depends(get_current_schema),
    user: dict = Depends(get_current_user),
):
    """
    Delete a field by its fieldmap id: drop the column and remove the mapping.

    This is what the Custom Fields page calls. The by-name route below still
    exists for an orphaned column that has no fieldmap row to have an id, but a
    name cannot address every row — see delete_field_by_id in the service for
    the 'Debit/Credit' case, which no amount of URL encoding reaches.
    """
    async with company_connection(schema) as conn:
        doomed = await conn.fetchrow(
            "SELECT id, fieldname, mapfields FROM fieldmap WHERE id = $1", fieldmap_id
        )
        try:
            result = await cf.delete_field_by_id(conn, fieldmap_id)
        except cf.CustomFieldError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

        if result.get("found") and doomed:
            await changelog.log_deleted(
                conn, row=dict(doomed), username=user["username"]
            )

    if not result.get("found"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No field with id {fieldmap_id}")
    return result


@router.delete("/{fieldname}", dependencies=[Depends(require_manager)])
async def delete_field(
    fieldname: str,
    schema: str = Depends(get_current_schema),
    user: dict = Depends(get_current_user),
):
    """
    Delete a custom field: drop the column and remove its fieldmap row.

    Refused for built-in temp_trans columns. Every value stored in the column
    goes with it, including on rows already staged — this cannot be undone.
    """
    async with company_connection(schema) as conn:
        # Read the mapping before it is deleted. This is a hard delete of the
        # fieldmap row, so afterwards there is nowhere left to learn what the
        # field's aliases were — and those are exactly what someone recreating
        # it by hand needs.
        doomed = await conn.fetchrow(
            "SELECT id, fieldname, mapfields FROM fieldmap WHERE fieldname = $1",
            (fieldname or "").strip(),
        )
        try:
            result = await cf.delete_custom_field(conn, fieldname)
        except cf.CustomFieldError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

        if result.get("found"):
            await changelog.log_deleted(
                conn,
                # An orphaned column with no fieldmap row still deletes, and
                # still gets logged — under the name the caller gave.
                row=dict(doomed) if doomed else {"fieldname": result["fieldname"]},
                username=user["username"],
            )

    if not result.get("found"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No field named '{fieldname}'")
    return result
