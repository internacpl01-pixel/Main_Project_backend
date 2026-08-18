"""
User management routes — accounts inside one company.

GET    /users              — list this company's accounts        (manager+)
POST   /users              — create an account                   (manager+)
PATCH  /users/{id}         — change username and/or password     (manager+)
PUT    /users/{id}/role    — move an account to another rung     (company_admin+)
DELETE /users/{id}         — delete an account                   (manager+)
GET    /users/{id}/projects— which projects they are assigned to (manager+)
PUT    /users/{id}/projects— set that assignment list            (company_admin+)

"This company" is the company on the caller's token: their own for a company
user, the one they switched into for a super-admin. Every query is scoped to
that company_id, so a manager at company A cannot see, edit, or delete an
account at company B even by guessing ids.

Level alone does not decide these calls. A manager passes require_level for
POST /users but may still only create staff, and a company_admin may create a
peer admin yet not edit one — see the two matrices in permissions.py.
"""
from fastapi import APIRouter, Body, Depends, HTTPException, status

import permissions
from database import company_connection
from routers.auth import get_current_company_id, get_current_user, require_level
from services import accounts

router = APIRouter(prefix="/users", tags=["users"])


def _level_or_400(role: str) -> int:
    if role not in permissions.ROLE_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown role '{role}'. Expected one of: {', '.join(permissions.ROLE_LEVELS)}.",
        )
    return permissions.level_of(role)


async def _target_or_404(conn, user_id: int, company_id: int) -> dict:
    target = await accounts.get_account(conn, user_id, company_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return target


def _guard_target(actor: dict, target: dict) -> None:
    """
    Refuse to act on an account the caller does not outrank.

    Self is checked first and separately: an admin editing their own row is not
    blocked because of rank but because demoting or deleting yourself is how a
    company ends up with no administrator at all.
    """
    if actor.get("id") is not None and target["id"] == actor["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot modify your own account here.",
        )

    target_level = permissions.level_of(target["role"])
    if not permissions.can_edit(actor["level"], target_level):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"You cannot modify a {permissions.label_of(target['role'])} account "
                f"as a {permissions.label_of(actor['role'])}."
            ),
        )


@router.get("/")
async def list_users(
    actor: dict = Depends(require_level(permissions.MANAGER)),
    company_id: int = Depends(get_current_company_id),
):
    """
    Every account in this company, with the level number and label the UI needs
    to decide which row actions to show.
    """
    async with company_connection("admin") as conn:
        rows = await accounts.list_accounts(conn, company_id)

    return [
        {
            **row,
            "level": permissions.level_of(row["role"]),
            "role_label": permissions.label_of(row["role"]),
            "is_self": row["id"] == actor.get("id"),
        }
        for row in rows
    ]


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_user(
    username: str = Body(...),
    password: str = Body(...),
    role: str = Body(..., description="staff | manager | company_admin"),
    actor: dict = Depends(require_level(permissions.MANAGER)),
    company_id: int = Depends(get_current_company_id),
):
    """
    Create an account in this company.

    A manager may create staff. A company_admin may create staff, managers, and
    other company_admins. Nobody creates a super_admin here — that account has
    no company, and this endpoint only ever writes company-scoped rows.
    """
    target_level = _level_or_400(role)

    if target_level == permissions.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Super admins belong to no company and cannot be created here.",
        )
    if not permissions.can_create(actor["level"], target_level):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"You cannot create a {permissions.label_of(role)} account "
                f"as a {permissions.label_of(actor['role'])}."
            ),
        )

    async with company_connection("admin") as conn:
        try:
            row = await accounts.create_account(
                conn, username=username, password=password, role=role, company_id=company_id
            )
        except accounts.AccountError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return {**row, "level": target_level, "role_label": permissions.label_of(role)}


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    username: str = Body(None, description="New username"),
    password: str = Body(None, description="New password"),
    actor: dict = Depends(require_level(permissions.MANAGER)),
    company_id: int = Depends(get_current_company_id),
):
    """
    Change an account's username, password, or both. Omitted fields are left alone.
    """
    if username is None and password is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide a username or a password to update.",
        )

    async with company_connection("admin") as conn:
        target = await _target_or_404(conn, user_id, company_id)
        _guard_target(actor, target)

        try:
            row = await accounts.update_account(
                conn, user_id, company_id, username=username, password=password
            )
        except accounts.AccountError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    return {
        **row,
        "level": permissions.level_of(row["role"]),
        "role_label": permissions.label_of(row["role"]),
    }


@router.put("/{user_id}/role")
async def update_user_role(
    user_id: int,
    role: str = Body(..., embed=True, description="staff | manager | company_admin"),
    actor: dict = Depends(require_level(permissions.COMPANY_ADMIN)),
    company_id: int = Depends(get_current_company_id),
):
    """
    Move an account to a different rung. Company admins only.

    The caller must outrank both ends of the move: they cannot promote someone
    into a rank they could not have created, nor re-role an account they could
    not have edited. Without the first check a company_admin could promote a
    staff member to super_admin and hand out the whole install.
    """
    target_level = _level_or_400(role)

    if target_level == permissions.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Super admins belong to no company and cannot be set here.",
        )
    if not permissions.can_create(actor["level"], target_level):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"You cannot promote anyone to {permissions.label_of(role)} "
                f"as a {permissions.label_of(actor['role'])}."
            ),
        )

    async with company_connection("admin") as conn:
        target = await _target_or_404(conn, user_id, company_id)
        _guard_target(actor, target)
        row = await accounts.set_account_role(conn, user_id, company_id, role)

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    return {
        **row,
        "level": target_level,
        "role_label": permissions.label_of(role),
    }


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    actor: dict = Depends(require_level(permissions.MANAGER)),
    company_id: int = Depends(get_current_company_id),
):
    """
    Delete an account.

    Deleting the last company_admin is refused: the company would be left with
    nobody who can manage its users, recoverable only by a super-admin switching
    in. Demote or replace them instead.
    """
    async with company_connection("admin") as conn:
        target = await _target_or_404(conn, user_id, company_id)
        _guard_target(actor, target)

        if target["role"] == "company_admin":
            remaining = await conn.fetchval(
                """
                SELECT count(*) FROM admin.users
                WHERE company_id = $1 AND role = 'company_admin' AND id <> $2
                """,
                company_id,
                user_id,
            )
            if not remaining:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"'{target['username']}' is this company's only Company Admin. "
                        f"Create another one before deleting this account."
                    ),
                )

        deleted = await accounts.delete_account(conn, user_id, company_id)

    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    return {"status": "deleted", "username": deleted}


# ---------- Project assignment ------------------------------------------------
#
# A company runs several projects and a manager or staff member normally works
# on some of them, not all. The company admin decides which — that is what these
# two endpoints write, and what services/scoping.py reads on every request.


@router.get("/{user_id}/projects")
async def list_user_projects(
    user_id: int,
    actor: dict = Depends(require_level(permissions.MANAGER)),
    company_id: int = Depends(get_current_company_id),
):
    """
    Every project in the company, each flagged with whether this person is on it.

    Returning the full list rather than just the assigned ids is what lets the
    UI draw the assignment checkboxes in one call.

    Admins are reported as assigned to everything: they reach every project
    without an assignment row, and showing their boxes unticked would suggest
    ticking them changes something.
    """
    async with company_connection(actor["schema"]) as conn:
        target = await accounts.get_account(conn, user_id, company_id)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

        target_is_admin = permissions.has_authority(
            permissions.level_of(target["role"]), permissions.COMPANY_ADMIN
        )

        rows = await conn.fetch(
            """
            SELECT p.id, p.name, p.code,
                   (pm.user_id IS NOT NULL) AS assigned,
                   pm.assigned_at
            FROM projects p
            LEFT JOIN project_members pm
                   ON pm.project_id = p.id AND pm.user_id = $1
            WHERE p.is_active = true
            ORDER BY p.name
            """,
            user_id,
        )

    return {
        "user_id": user_id,
        "username": target["username"],
        "role": target["role"],
        "role_label": permissions.label_of(target["role"]),
        "sees_all_projects": target_is_admin,
        "projects": [
            {**dict(r), "assigned": target_is_admin or r["assigned"]} for r in rows
        ],
    }


@router.put("/{user_id}/projects")
async def set_user_projects(
    user_id: int,
    project_ids: list[int] = Body(..., embed=True, description="The complete set of project ids"),
    actor: dict = Depends(require_level(permissions.COMPANY_ADMIN)),
    company_id: int = Depends(get_current_company_id),
):
    """
    Replace this person's project assignments with exactly project_ids.

    Company admins only — assigning work is an admin's job, and a manager who
    could assign projects could add themselves to every project in the company.

    The whole set is sent rather than add/remove deltas: unticking three boxes
    and ticking one is a single request that cannot half-apply, and re-sending
    the same list is a no-op instead of an error.
    """
    async with company_connection(actor["schema"]) as conn:
        target = await accounts.get_account(conn, user_id, company_id)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

        if permissions.has_authority(
            permissions.level_of(target["role"]), permissions.COMPANY_ADMIN
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{permissions.label_of(target['role'])} accounts already reach every "
                    f"project in the company; there is nothing to assign."
                ),
            )

        wanted = sorted(set(project_ids))
        if wanted:
            found = await conn.fetch(
                "SELECT id FROM projects WHERE id = ANY($1) AND is_active = true", wanted
            )
            missing = set(wanted) - {r["id"] for r in found}
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"No active project in this company with id: {sorted(missing)}.",
                )

        async with conn.transaction():
            await conn.execute(
                "DELETE FROM project_members WHERE user_id = $1 AND NOT (project_id = ANY($2))",
                user_id,
                wanted,
            )
            if wanted:
                await conn.executemany(
                    """
                    INSERT INTO project_members (project_id, user_id, assigned_by)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (project_id, user_id) DO NOTHING
                    """,
                    [(pid, user_id, actor.get("id")) for pid in wanted],
                )

        assigned = await conn.fetch(
            """
            SELECT p.id, p.name, p.code
            FROM project_members pm
            JOIN projects p ON p.id = pm.project_id
            WHERE pm.user_id = $1
            ORDER BY p.name
            """,
            user_id,
        )

    return {
        "user_id": user_id,
        "username": target["username"],
        "projects": [dict(r) for r in assigned],
    }
