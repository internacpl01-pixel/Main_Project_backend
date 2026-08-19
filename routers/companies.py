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
from db.migrate import ProvisionError, normalize_company_code, provision_company
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
                   c.code,
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
    code: str = Body(..., description="Three lowercase letters; prefixes every username here"),
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
                company = await provision_company(
                    conn, name, code, created_by=user.get("id")
                )
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
    code: str = Body(None, description="Three letters; only settable while the company has none"),
    is_active: bool = Body(None, description="false hides the company and blocks new sign-ins"),
    user: dict = Depends(require_super_admin),
):
    """
    Rename a company, give a legacy company its code, or flip it active/inactive.

    Deactivating is the reversible one: the company drops off the default
    registry list, but the schema and every row in it stay exactly where they
    are. DELETE is the other one, and it refuses any company that holds data.

    A code can be set once, on a company registered before codes existed. It
    cannot be changed afterwards: every username in the company starts with it,
    and changing it would leave every existing account failing the rule its own
    company defines.
    """
    sets = []
    params = []

    if code is not None:
        try:
            code = normalize_company_code(code)
        except ProvisionError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if name is not None:
        name = name.strip()
        if len(name) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Company name must be at least 2 characters.",
            )
        sets.append(f"name = ${len(params) + 1}")
        params.append(name)

    if code is not None:
        sets.append(f"code = ${len(params) + 1}")
        params.append(code)

    if is_active is not None:
        sets.append(f"is_active = ${len(params) + 1}")
        params.append(is_active)

    if not sets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing to update. Send name, code, is_active, or a combination.",
        )

    params.append(company_id)

    async with company_connection("admin") as conn:
        current = await conn.fetchrow(
            "SELECT name, code FROM admin.companies WHERE id = $1", company_id
        )
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")

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

        if code is not None:
            if current["code"] and current["code"] != code:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"'{current['name']}' already has the code '{current['code']}', "
                        f"and it cannot be changed — every username in the company "
                        f"begins with it."
                    ),
                )
            taken = await conn.fetchval(
                "SELECT name FROM admin.companies WHERE code = $1 AND id <> $2",
                code,
                company_id,
            )
            if taken:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Code '{code}' is already used by '{taken}'.",
                )

        row = await conn.fetchrow(
            f"""
            UPDATE admin.companies
            SET {", ".join(sets)}
            WHERE id = ${len(params)}
            RETURNING id, name, code, schema_name, is_active, created_at, created_by
            """,
            *params,
        )

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")

    return dict(row)


# Tables whose contents mean the company is in use. import_batches counts even
# when every row in it was later deleted: a company that has taken a statement
# has a history, and history is the thing DELETE must never quietly discard.
_IN_USE_TABLES = ("transactions", "temp_trans", "import_batches")


@router.get("/{company_id}/delete-check")
async def check_delete(company_id: int, user: dict = Depends(require_super_admin)):
    """
    Whether this company can be deleted, and what is standing in the way.

    The confirm dialog asks this before offering the button, so the refusal is
    read before the decision rather than after it.
    """
    async with raw_connection() as conn:
        company = await conn.fetchrow(
            "SELECT id, name, schema_name FROM admin.companies WHERE id = $1", company_id
        )
        if company is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")

        counts = {}
        for table in _IN_USE_TABLES:
            counts[table] = int(
                await conn.fetchval(
                    f'SELECT count(*) FROM "{company["schema_name"]}"."{table}"'
                ) or 0
            )
        users = int(
            await conn.fetchval(
                "SELECT count(*) FROM admin.users WHERE company_id = $1", company_id
            ) or 0
        )

        # What the company has built up that is not a ledger. None of it blocks
        # a delete — a company can hold a carefully built field setup and still
        # be one somebody created by mistake — but it is the part that is easy
        # to forget is there. clone_preview already counts exactly these, so the
        # dialog warns with the same numbers the copy screen offers.
        holds = await clone_preview(conn, company["schema_name"])

    blocking = {k: v for k, v in counts.items() if v}
    return {
        "company": {"id": company["id"], "name": company["name"]},
        "can_delete": not blocking,
        "blocking": blocking,
        "holds": {
            "fields": holds["fields"],
            "projects": holds["projects"],
            "masters": holds["masters"],
        },
        "users": users,
        "reason": (
            None if not blocking else
            "This company holds "
            + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in blocking.items())
            + ". Deactivate it instead — that hides it without destroying anything."
        ),
    }


@router.delete("/{company_id}")
async def delete_company(
    company_id: int,
    confirm_name: str = Body(..., embed=True, description="The company's exact name"),
    user: dict = Depends(require_super_admin),
):
    """
    Permanently remove a company that holds no data.

    Drops the schema, its accounts and its migration records. There is no undo
    and this app keeps no backups, so two things gate it:

      * the company must hold no transactions, no staged rows and no import
        batches. A company with a ledger is deactivated, never deleted — the
        point of this endpoint is throwing away a mistake, not destroying a
        tenant's books.
      * the caller has to type the company's name back. An id in a URL is easy
        to get wrong by one digit; a name is not.

    One transaction, so a failure part-way leaves the company intact rather than
    half-erased.
    """
    async with raw_connection() as conn:
        async with conn.transaction():
            company = await conn.fetchrow(
                "SELECT id, name, code, schema_name FROM admin.companies WHERE id = $1",
                company_id,
            )
            if company is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Company not found."
                )

            if (confirm_name or "").strip() != company["name"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Type the company's name exactly to confirm. Expected "
                        f"'{company['name']}'."
                    ),
                )

            schema = company["schema_name"]
            held = {}
            for table in _IN_USE_TABLES:
                n = int(await conn.fetchval(f'SELECT count(*) FROM "{schema}"."{table}"') or 0)
                if n:
                    held[table] = n
            if held:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"'{company['name']}' holds "
                        + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in held.items())
                        + " and cannot be deleted. Deactivate it instead — that hides "
                        "it without destroying anything."
                    ),
                )

            # Schema first: project_members lives in it and references
            # admin.users, so the accounts cannot go until it is gone.
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            removed_users = await conn.fetchval(
                "WITH gone AS (DELETE FROM admin.users WHERE company_id = $1 RETURNING 1) "
                "SELECT count(*) FROM gone",
                company_id,
            )
            await conn.execute(
                "DELETE FROM admin.schema_migrations WHERE schema_name = $1", schema
            )
            await conn.execute("DELETE FROM admin.companies WHERE id = $1", company_id)

    return {
        "deleted": True,
        "name": company["name"],
        "code": company["code"],
        "schema_name": schema,
        "users_removed": int(removed_users or 0),
    }


@router.post("/{company_id}/admin", status_code=status.HTTP_201_CREATED)
async def add_company_admin(
    company_id: int,
    username: str = Body(..., description="Must start with the company's code, e.g. dpl-ravi"),
    password: str = Body(...),
    user: dict = Depends(require_super_admin),
):
    """
    Create a company_admin for a company.

    This is the super admin's only way to put a person inside a company, and it
    exists because they cannot go in themselves. Without it, a company whose
    last admin was deleted would be unreachable by anyone.

    The username has to carry the company's code, same as every other account
    there — enforced in services/accounts.py, not here, so the rule is one rule.
    """
    async with raw_connection() as conn:
        company = await conn.fetchrow(
            "SELECT id, name, code, is_active FROM admin.companies WHERE id = $1", company_id
        )
        if company is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")
        if not company["is_active"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{company['name']}' is deactivated. Reactivate it first.",
            )
        try:
            account = await create_account(
                conn,
                username=username,
                password=password,
                role="company_admin",
                company_id=company_id,
            )
        except AccountError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return {"company": {"id": company["id"], "name": company["name"]}, "admin": account}
