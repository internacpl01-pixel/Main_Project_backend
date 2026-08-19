"""
Company registry routes — super-admin only.

GET    /companies                    — every registered company (the registry table)
GET    /companies/{id}/clone-preview — what copying that company would bring across
POST   /companies                    — register a new company, blank or copied
PATCH  /companies/{id}               — rename, activate, or deactivate

Registering a company is not just an INSERT: it allocates a schema name, runs
CREATE SCHEMA, and applies every company migration to it. That work lives in
db.migrate.provision_company, shared with the `new-company` CLI command so the
two paths cannot drift.

Copying is the same endpoint with copy_from_id set, not a second one — the user
presses one button and gets one company either way, and whether it starts blank
or shaped like an existing company is a property of that request. The copying
itself lives in services/clone.py.

Restricted to super_admin throughout. A company user's token is bound to their
own schema and there is nothing here for them to act on.
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

import permissions
from database import company_connection, raw_connection
from db.migrate import ProvisionError, provision_company
from routers.auth import require_level
from services.accounts import AccountError, create_account
from routers.master import TABLE_LABELS
from services.clone import FIELDS_PART, CloneError, clone_into, clone_preview

router = APIRouter(prefix="/companies", tags=["companies"])

require_super_admin = require_level(permissions.SUPER_ADMIN)

# What each copyable thing is called on screen. The master tables take their
# names from the Master Data router, so the checkbox and the page it refers to
# cannot end up calling the same table two different things. Only the two that
# have no home elsewhere are written out here.
_PART_LABELS = {
    FIELDS_PART: "Field structure and labels",
    "projects": "Projects",
    **TABLE_LABELS,
}


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


async def _source_company(conn, company_id: int) -> dict:
    """The company a clone would copy from, or a 4xx explaining why it cannot.

    Inactive companies are refused. Deactivated normally means retired, and a
    retired company is exactly the one whose heads and beneficiaries have gone
    stale — copying from it is a quiet way to start a new company with bad
    reference data.
    """
    row = await conn.fetchrow(
        "SELECT id, name, schema_name, is_active FROM admin.companies WHERE id = $1",
        company_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No company with id {company_id} to copy from.",
        )
    if not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"'{row['name']}' is deactivated and cannot be copied from. "
                f"Reactivate it first if its setup is still the one you want."
            ),
        )
    return dict(row)


@router.get("/{company_id}/clone-preview")
async def preview_clone(company_id: int, user: dict = Depends(require_super_admin)):
    """
    What copying this company would bring across, counted live.

    `parts` is the list the register screen turns into checkboxes: one entry per
    thing that can be copied, each with its label and how many of it this company
    has. Sending the same keys back as copy_parts is what selects them.

    Anything absent from this response is not copied: no transactions, no
    imports, no accounts.
    """
    async with raw_connection() as conn:
        source = await _source_company(conn, company_id)
        counts = await clone_preview(conn, source["schema_name"])

    counts["parts"] = [
        {**p, "label": _PART_LABELS.get(p["key"], p["key"])} for p in counts["parts"]
    ]
    return {"company": {"id": source["id"], "name": source["name"]}, **counts}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def register_company(
    name: str = Body(..., description="Company name, unique across the install"),
    copy_from_id: int = Body(None, description="Optional: id of a company to copy the setup of"),
    copy_parts: list[str] = Body(
        None,
        description="Which parts to copy; omit for all. Keys come from the clone-preview response.",
    ),
    admin_username: str = Body(None, description="Optional: username for the company's first admin"),
    admin_password: str = Body(None, description="Optional: password for that first admin"),
    user: dict = Depends(require_super_admin),
):
    """
    Register a new company, blank or shaped like an existing one.

    Creates schema company_NNN, applies every company migration to it, and
    records the registration against the super-admin who made it.

    copy_from_id makes it a copy: the new company gets the source's columns,
    fieldmap, projects and master data. It does NOT get the source's
    transactions, imports or accounts — see services/clone.py. The copy is a
    snapshot, not a link; changing the source afterwards does not change this
    company.

    copy_parts narrows that to a chosen few. Omitting it copies everything;
    sending an empty list copies nothing, which is a blank company by a longer
    route. The distinction is deliberate — `copy_parts or None` would have read
    an explicit "nothing" as "everything", which is the one mistake here that
    silently does more than the user asked for.

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
        # Provisioning and copying share one transaction. Postgres DDL is
        # transactional, so a copy that fails halfway takes the schema and the
        # admin.companies row down with it — the alternative is a registered
        # company with half a fieldmap, which looks fine on the registry page
        # and is wrong everywhere else. provision_company opens its own
        # transaction, which nests here as a savepoint.
        copied = None
        try:
            async with conn.transaction():
                source = await _source_company(conn, copy_from_id) if copy_from_id else None
                company = await provision_company(conn, name, created_by=user.get("id"))
                if source:
                    copied = await clone_into(
                        conn,
                        source_schema=source["schema_name"],
                        target_schema=company["schema_name"],
                        parts=copy_parts,
                    )
                    copied["source_name"] = source["name"]
                    copied["part_labels"] = [
                        _PART_LABELS.get(p, p) for p in copied["parts"]
                    ]
        except ProvisionError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except CloneError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not copy '{name}': {e}",
            )

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
                # The schema is already built and committed at this point, copy
                # included — the transaction above closed before this ran, on
                # purpose. Report the company as created and say the admin was
                # not, rather than pretending the whole call failed and leaving
                # the user to wonder whether the company exists.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Company '{company['name']}' was registered as "
                        f"{company['schema_name']}, but its admin was not created: {e} "
                        f"Add one from the Users page after switching to it."
                    ),
                )

    # copied is None for a blank company, and a count of what came across for a
    # copy. The UI reports it; nothing depends on it.
    return {"company": company, "admin": first_admin, "copied": copied}


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
