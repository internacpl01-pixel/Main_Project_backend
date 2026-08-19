"""
Project routes.

GET    /projects              — list all projects for the current company
POST   /projects              — create a new project
GET    /projects/{id}         — get one project
PATCH  /projects/{id}         — edit a project
DELETE /projects/{id}         — archive a project (or delete it permanently)
"""
import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

import permissions
from database import company_connection
from routers.auth import get_company_user, get_current_schema, require_level
from services import scoping

router = APIRouter(prefix="/projects", tags=["projects"])

# Reading a project is part of doing the job, so staff can. Creating, renaming
# and archiving one reshapes what the whole company books against — manager+.
require_manager = require_level(permissions.MANAGER)


@router.get("/")
async def list_projects(user: dict = Depends(get_company_user)):
    """
    List the projects this user may work on.

    Admins get the whole company. A manager or staff member gets only the
    projects they have been assigned — an empty list if they have none yet.
    """
    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)
        if scoping.scope_is_empty(scope):
            return []

        clause, params, _ = scoping.project_filter(
            scope, "id", 1, include_unassigned=False
        )
        where = "WHERE is_active = true" + (f" AND {clause}" if clause else "")

        rows = await conn.fetch(
            f"""
            SELECT id, name, code, address, is_active, created_at, updated_at
            FROM projects
            {where}
            ORDER BY name
            """,
            *params,
        )
    return [dict(r) for r in rows]


@router.post("/", dependencies=[Depends(require_manager)])
async def create_project(
    name: str = Body(..., description="Project name (required)"),
    code: str = Body(None, description="Short code like SKY-001"),
    address: str = Body(None, description="Project address"),
    schema: str = Depends(get_current_schema),
):
    """
    Create a new project for the logged-in company.

    Requires: name
    Optional:   code (short code like "SKY-001"), address
    """
    async with company_connection(schema) as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO projects (name, code, address)
                VALUES ($1, $2, $3)
                RETURNING id, name, code, address, is_active, created_at, updated_at
                """,
                name,
                code,
                address,
            )
        except Exception as e:
            # Most likely a duplicate code (UNIQUE constraint).
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not create project: {e}",
            )

    return dict(row)


@router.get("/{project_id}")
async def get_project(project_id: int, user: dict = Depends(get_company_user)):
    """
    Get one project by ID.

    A project the caller is not assigned to reads as missing rather than
    forbidden — otherwise the 403 itself would confirm which project ids exist.
    """
    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)
        if scope is not scoping.UNRESTRICTED and project_id not in (scope or []):
            raise HTTPException(status_code=404, detail="Project not found.")

        row = await conn.fetchrow(
            "SELECT id, name, code, address, is_active, created_at, updated_at "
            "FROM projects WHERE id = $1",
            project_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    return dict(row)


@router.patch("/{project_id}", dependencies=[Depends(require_manager)])
async def update_project(
    project_id: int,
    name: str = Body(None, description="New project name"),
    code: str = Body(None, description="New short code like SKY-001"),
    address: str = Body(None, description="New project address"),
    is_active: bool = Body(None, description="false archives it, true restores it"),
    schema: str = Depends(get_current_schema),
):
    """
    Edit a project. Send only the fields you want to change.

    A field left out (or sent as null) is left alone — this endpoint cannot
    blank a value. To clear a code or address, send an empty string "".

    updated_at is set by the projects_set_updated_at trigger, not here.
    """
    sets = []
    params = []
    idx = 1

    if name is not None:
        sets.append(f"name = ${idx}")
        params.append(name)
        idx += 1
    if code is not None:
        sets.append(f"code = ${idx}")
        params.append(code)
        idx += 1
    if address is not None:
        sets.append(f"address = ${idx}")
        params.append(address)
        idx += 1
    if is_active is not None:
        sets.append(f"is_active = ${idx}")
        params.append(is_active)
        idx += 1

    if not sets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing to update. Send at least one of: name, code, address, is_active.",
        )

    params.append(project_id)

    async with company_connection(schema) as conn:
        try:
            row = await conn.fetchrow(
                f"""
                UPDATE projects
                SET {", ".join(sets)}
                WHERE id = ${idx}
                RETURNING id, name, code, address, is_active, created_at, updated_at
                """,
                *params,
            )
        except asyncpg.UniqueViolationError:
            # projects.code carries a UNIQUE constraint.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Another project already uses the code '{code}'.",
            )

    if row is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    return dict(row)


@router.delete("/{project_id}", dependencies=[Depends(require_manager)])
async def delete_project(
    project_id: int,
    permanent: bool = Query(
        False,
        description="true = really delete the row. Refused if anything references it.",
    ),
    schema: str = Depends(get_current_schema),
):
    """
    Archive a project (default) or delete it permanently.

    Default is a soft delete — is_active = false. The project disappears from
    GET /projects but every transaction that points at it keeps a readable name.
    Hard-deleting a project referenced by the ledger would either destroy history
    or leave transactions pointing at a missing row, which is why
    transactions.project_id is ON DELETE RESTRICT.

    permanent=true is for correcting a mistake — a project created by typo, with
    nothing booked against it yet. It counts references first and refuses with
    409 rather than letting Postgres raise a raw foreign-key error.
    """
    async with company_connection(schema) as conn:
        exists = await conn.fetchrow(
            "SELECT id, name, is_active FROM projects WHERE id = $1", project_id
        )
        if exists is None:
            raise HTTPException(status_code=404, detail="Project not found.")

        if not permanent:
            row = await conn.fetchrow(
                """
                UPDATE projects
                SET is_active = false
                WHERE id = $1 AND is_active = true
                RETURNING id, name, code, address, is_active, created_at, updated_at
                """,
                project_id,
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Project '{exists['name']}' is already archived.",
                )
            return {"status": "archived", "project": dict(row)}

        # Permanent delete — count what points at this project first.
        txn_count = await conn.fetchval(
            "SELECT count(*) FROM transactions WHERE project_id = $1", project_id
        )
        staged_count = await conn.fetchval(
            "SELECT count(*) FROM temp_trans WHERE project_id = $1", project_id
        )

        if txn_count or staged_count:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Cannot permanently delete '{exists['name']}': "
                    f"{txn_count} transaction(s) and {staged_count} staged import row(s) "
                    f"reference it. Archive it instead (omit permanent=true)."
                ),
            )

        await conn.execute("DELETE FROM projects WHERE id = $1", project_id)

    return {"status": "deleted", "project_id": project_id, "name": exists["name"]}


@router.get("/{project_id}/members")
async def list_project_members(
    project_id: int,
    user: dict = Depends(require_manager),
):
    """
    Who is assigned to this project.

    The project-centric view of the same table that PUT /users/{id}/projects
    writes. Read-only here — assigning is done per person, by a company admin.
    Admins are not listed: they reach every project without an assignment row.
    """
    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)
        if scope is not scoping.UNRESTRICTED and project_id not in (scope or []):
            raise HTTPException(status_code=404, detail="Project not found.")

        exists = await conn.fetchval("SELECT 1 FROM projects WHERE id = $1", project_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Project not found.")

        rows = await conn.fetch(
            """
            SELECT u.id, u.username, u.role, pm.assigned_at,
                   assigner.username AS assigned_by_username
            FROM project_members pm
            JOIN admin.users u ON u.id = pm.user_id
            LEFT JOIN admin.users assigner ON assigner.id = pm.assigned_by
            WHERE pm.project_id = $1
            ORDER BY u.username
            """,
            project_id,
        )

    return [
        {**dict(r), "role_label": permissions.label_of(r["role"])}
        for r in rows
    ]
