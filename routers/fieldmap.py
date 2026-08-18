"""
Field mapping (fieldmap table) routes.

GET   /fieldmap           — list all field mappings
POST   /fieldmap           — create a new mapping
PATCH /fieldmap/{id}      — update a mapping
DELETE /fieldmap/{id}     — delete a mapping (soft)
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

import permissions
from database import company_connection
from routers.auth import get_current_schema, require_level

router = APIRouter(prefix="/fieldmap", tags=["fieldmap"])

# Field mappings decide how every imported statement is read, so staff see
# them but cannot change them.
require_manager = require_level(permissions.MANAGER)


@router.get("/")
async def list_fieldmap(
    include_inactive: bool = Query(False),
    schema: str = Depends(get_current_schema),
):
    cols = ['id', 'fieldname', 'displayname', 'mapfields', 'data_type', 'method', 'is_active', 'created_at', 'updated_at']
    where = "" if include_inactive else "WHERE is_active = true"
    async with company_connection(schema) as conn:
        rows = await conn.fetch(f"SELECT {', '.join(cols)} FROM fieldmap {where} ORDER BY id")
    return [dict(r) for r in rows]


@router.post("/", status_code=201, dependencies=[Depends(require_manager)])
async def create_fieldmap(
    body: dict,
    schema: str = Depends(get_current_schema),
):
    fieldname = body.get('fieldname')
    displayname = body.get('displayname')
    mapfields = body.get('mapfields', '')
    data_type = body.get('data_type', 'text')
    if data_type not in ('date', 'text', 'numeric'):
        data_type = 'text'
    # Free text: what the field is for (import / selection / rule / ...).
    method = (body.get('method') or '').strip()

    async with company_connection(schema) as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO fieldmap (fieldname, displayname, mapfields, data_type, method)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING id, fieldname, displayname, mapfields, data_type, method, is_active, created_at, updated_at""",
                fieldname, displayname, mapfields, data_type, method,
            )
        except Exception as e:
            raise HTTPException(400, f"Could not create mapping: {e}")
    return dict(row)


@router.patch("/{fieldmap_id}", dependencies=[Depends(require_manager)])
async def update_fieldmap(
    fieldmap_id: int,
    body: dict,
    schema: str = Depends(get_current_schema),
):
    sets = []
    params = []
    idx = 1
    for key in ('fieldname', 'displayname', 'mapfields', 'data_type', 'method', 'is_active'):
        if key in body:
            sets.append(f"{key} = ${idx}")
            params.append(body[key])
            idx += 1
    if not sets:
        raise HTTPException(400, "No fields to update.")
    params.append(fieldmap_id)

    async with company_connection(schema) as conn:
        row = await conn.fetchrow(
            f"UPDATE fieldmap SET {', '.join(sets)} WHERE id = ${idx} RETURNING id, fieldname, displayname, mapfields, data_type, method, is_active, created_at, updated_at",
            *params,
        )
    if row is None:
        raise HTTPException(404, "Mapping not found.")
    return dict(row)


@router.delete("/{fieldmap_id}", dependencies=[Depends(require_manager)])
async def delete_fieldmap(
    fieldmap_id: int,
    schema: str = Depends(get_current_schema),
):
    async with company_connection(schema) as conn:
        row = await conn.fetchrow(
            "UPDATE fieldmap SET is_active = false WHERE id = $1 AND is_active = true RETURNING id",
            fieldmap_id,
        )
    if row is None:
        raise HTTPException(400, "Already deleted or not found.")
    return {"status": "deleted"}
