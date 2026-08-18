"""
Company registry routes — super-admin only.

GET    /companies         — every registered company (the registry table)
POST   /companies         — register a new company, optionally with its first admin
PATCH  /companies/{id}    — rename, activate, or deactivate

Registering a company is not just an INSERT: it allocates a schema name, runs
CREATE SCHEMA, and applies every company migration to it. That work lives in
db.migrate.provision_company, shared with the `new-company` CLI command so the
two paths cannot drift.

Restricted to super_admin throughout. A company user's token is bound to their
own schema and there is nothing here for them to act on.
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

import permissions
from database import company_connection, raw_connection
from db.migrate import ProvisionError, provision_company
from routers.auth import require_level
from services.accounts import AccountError, create_account

router = APIRouter(prefix="/companies", tags=["companies"])

require_super_admin = require_level(permissions.SUPER_ADMIN)


@router.get("/")
async def list_companies(
    include_inactive: bool = Query(
        False, description="true also returns companies with is_active = false"
    ),
    user: dict = Depends(require_super_admin),
):
    """
    The company registry: one row per tenant, with who registered it, when, and
    how many accounts it has.

    schema_name is what you POST to /auth/switch-company.
    """
    where = "" if include_inactive else "WHERE c.is_active = true"
    async with company_connection("admin") as conn:
        rows = await conn.fetch(
            f"""
            SELECT c.id,
                   c.name,
                   c.schema_name,
                   c.is_active,
                   c.created_at,
                   c.created_by,
                   creator.username AS created_by_username,
                   (SELECT count(*) FROM admin.users u WHERE u.company_id = c.id) AS user_count
            FROM admin.companies c
            LEFT JOIN admin.users creator ON creator.id = c.created_by
            {where}
            ORDER BY c.created_at DESC, c.name
            """
        )
    return [dict(r) for r in rows]


@router.post("/", status_code=status.HTTP_201_CREATED)
async def register_company(
    name: str = Body(..., description="Company name, unique across the install"),
    admin_username: str = Body(None, description="Optional: username for the company's first admin"),
    admin_password: str = Body(None, description="Optional: password for that first admin"),
    user: dict = Depends(require_super_admin),
):
    """
    Register a new company.

    Creates schema company_NNN, applies every company migration to it, and
    records the registration against the super-admin who made it.

    Pass admin_username and admin_password to seed the company's first
    company_admin in the same step. Skip them and the company starts empty —
    only a super-admin can reach it, by switching in and adding users there.
    """
    seed_admin = bool(admin_username or admin_password)
    if seed_admin and not (admin_username and admin_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Give both admin_username and admin_password, or neither.",
        )

    async with raw_connection() as conn:
        try:
            company = await provision_company(conn, name, created_by=user.get("id"))
        except ProvisionError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        first_admin = None
        if seed_admin:
            try:
                first_admin = await create_account(
                    conn,
                    username=admin_username,
                    password=admin_password,
                    role="company_admin",
                    company_id=company["id"],
                )
            except AccountError as e:
                # The schema is already built and committed at this point.
                # Report the company as created and say the admin was not, rather
                # than pretending the whole call failed.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Company '{company['name']}' was registered as "
                        f"{company['schema_name']}, but its admin was not created: {e} "
                        f"Add one from the Users page after switching to it."
                    ),
                )

    return {"company": company, "admin": first_admin}


@router.patch("/{company_id}")
async def update_company(
    company_id: int,
    name: str = Body(None, description="New company name"),
    is_active: bool = Body(None, description="false hides the company and blocks switching into it"),
    user: dict = Depends(require_super_admin),
):
    """
    Rename a company or flip it active/inactive.

    Deactivating is the soft delete: /auth/switch-company refuses an inactive
    company and it drops off the default registry list, but the schema and every
    row in it stay exactly where they are. There is deliberately no endpoint
    that drops a company schema — that would destroy a tenant's whole ledger on
    one mis-click.
    """
    sets = []
    params = []

    if name is not None:
        name = name.strip()
        if len(name) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Company name must be at least 2 characters.",
            )
        sets.append(f"name = ${len(params) + 1}")
        params.append(name)

    if is_active is not None:
        sets.append(f"is_active = ${len(params) + 1}")
        params.append(is_active)

    if not sets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing to update. Send name, is_active, or both.",
        )

    params.append(company_id)

    async with company_connection("admin") as conn:
        clash = None
        if name is not None:
            clash = await conn.fetchval(
                "SELECT 1 FROM admin.companies WHERE lower(name) = lower($1) AND id <> $2",
                name,
                company_id,
            )
        if clash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"A company named '{name}' already exists.",
            )

        row = await conn.fetchrow(
            f"""
            UPDATE admin.companies
            SET {", ".join(sets)}
            WHERE id = ${len(params)}
            RETURNING id, name, schema_name, is_active, created_at, created_by
            """,
            *params,
        )

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")

    return dict(row)
