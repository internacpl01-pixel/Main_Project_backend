"""
End-to-end check for company cloning.

    python test_clone.py [source_schema]      # default: company_001

Registers two throwaway companies through the real API — one blank, one copied
from the source — checks them against the source, and deletes both. Nothing it
creates survives a successful run, and it purges leftovers from an aborted one
before it starts.

It goes through the HTTP layer with an in-process ASGI transport rather than
calling the service directly, so the transaction wrapping in the router is
exercised too. get_current_user is overridden instead of logging in: the point
is to test cloning, not to need a password to do it.

The source company is only ever read.
"""
import asyncio
import re
import sys

import httpx
from httpx import ASGITransport

import permissions
from database import close_pool, init_pool, raw_connection
from main import app
from routers.auth import get_current_user
from services.clone import COPIED_TABLES, SHAPED_TABLES

BLANK_PROBE = "ZZ Blank Probe"
COPY_PROBE = "ZZ Copy Probe"
PROBE_NAMES = (BLANK_PROBE, COPY_PROBE)

# Nothing is dropped unless it matches this AND belongs to a probe company.
SCHEMA_RE = re.compile(r"^company_\d{3,}$")

# Tables the clone must leave empty. Copying any of these would make the new
# company the same company under a second name.
LEDGER_TABLES = ("transactions", "temp_trans", "import_batches", "fieldchange_log")

_passed = 0
_failed = 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  [PASS] {label}")
    else:
        _failed += 1
        print(f"  [FAIL] {label}")
        if detail:
            for line in str(detail).splitlines():
                print(f"         {line}")


async def columns(conn, schema, table):
    """{column: declared type} — the same question _sync_shape answers, asked
    independently so a bug in the module cannot agree with itself."""
    rows = await conn.fetch(
        """
        SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS coltype
        FROM pg_attribute a
        WHERE a.attrelid = (quote_ident($1) || '.' || quote_ident($2))::regclass
          AND a.attnum > 0 AND NOT a.attisdropped
        """,
        schema, table,
    )
    return {r["name"]: r["coltype"] for r in rows}


async def data_rows(conn, schema, table):
    """Every row, over the columns a copy carries, in id order."""
    cols = [
        r["column_name"]
        for r in await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
              AND column_name NOT IN ('created_at', 'updated_at')
            ORDER BY ordinal_position
            """,
            schema, table,
        )
    ]
    if not cols:
        return []
    sel = ", ".join(f'"{c}"' for c in cols)
    rows = await conn.fetch(f'SELECT {sel} FROM "{schema}"."{table}" ORDER BY id')
    return [tuple(r) for r in rows]


async def purge_probes():
    """Remove anything a previous run left behind. Safe to call twice."""
    async with raw_connection() as conn:
        rows = await conn.fetch(
            "SELECT id, name, schema_name FROM admin.companies WHERE name = ANY($1::text[])",
            list(PROBE_NAMES),
        )
        for r in rows:
            schema = r["schema_name"]
            if not SCHEMA_RE.match(schema):
                print(f"  [SKIP] refusing to drop odd schema name {schema!r}")
                continue
            # Schema first: project_members lives in it and points at
            # admin.users, so the users below cannot go until it is gone.
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.execute("DELETE FROM admin.users WHERE company_id = $1", r["id"])
            await conn.execute(
                "DELETE FROM admin.schema_migrations WHERE schema_name = $1", schema
            )
            await conn.execute("DELETE FROM admin.companies WHERE id = $1", r["id"])
            print(f"  [PURGE] {r['name']} ({schema})")


async def main():
    source_schema = sys.argv[1] if len(sys.argv) > 1 else "company_001"

    await init_pool()
    app.dependency_overrides[get_current_user] = lambda: {
        "id": None,
        "username": "probe-super",
        "role": "super_admin",
        "level": permissions.level_of("super_admin"),
        "company_id": None,
        "schema": "admin",
    }

    transport = ASGITransport(app=app)
    try:
        async with raw_connection() as conn:
            source = await conn.fetchrow(
                "SELECT id, name, schema_name FROM admin.companies WHERE schema_name = $1",
                source_schema,
            )
        if source is None:
            print(f"No company registered with schema {source_schema}.")
            return 1

        print(f"=== clone check: source {source['name']} ({source_schema}) ===\n")
        print("--- purge leftovers ---")
        await purge_probes()

        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # --- preview -----------------------------------------------------
            print("\n--- preview ---")
            r = await client.get(f"/companies/{source['id']}/clone-preview")
            check("preview returns 200", r.status_code == 200, r.text[:300])
            preview = r.json() if r.status_code == 200 else {}
            print(f"         {preview}")

            # --- blank company still works -----------------------------------
            print("\n--- blank company (regression) ---")
            r = await client.post("/companies/", json={"name": BLANK_PROBE})
            check("blank register returns 201", r.status_code == 201, r.text[:300])
            if r.status_code != 201:
                return 1
            blank = r.json()["company"]
            check("blank reports copied=null", r.json().get("copied") is None)

            async with raw_connection() as conn:
                n = await conn.fetchval(f'SELECT count(*) FROM "{blank["schema_name"]}".fieldmap')
                check("blank keeps the 6 seeded fieldmap rows", n == 6, f"got {n}")

            # --- copy --------------------------------------------------------
            print("\n--- copy ---")
            r = await client.post(
                "/companies/", json={"name": COPY_PROBE, "copy_from_id": source["id"]}
            )
            check("copy register returns 201", r.status_code == 201, r.text[:300])
            if r.status_code != 201:
                return 1
            body = r.json()
            clone = body["company"]
            copied = body.get("copied") or {}
            clone_schema = clone["schema_name"]
            print(f"         created {clone_schema}, copied={copied.get('fields')} fields, "
                  f"{copied.get('columns_added')} cols added, "
                  f"{copied.get('columns_dropped')} dropped")

            async with raw_connection() as conn:
                # Shape: exact match, both directions, including types.
                for table in SHAPED_TABLES:
                    src_cols = await columns(conn, source_schema, table)
                    new_cols = await columns(conn, clone_schema, table)
                    missing = sorted(set(src_cols) - set(new_cols))
                    extra = sorted(set(new_cols) - set(src_cols))
                    wrong = sorted(
                        f"{c}: {src_cols[c]} vs {new_cols[c]}"
                        for c in set(src_cols) & set(new_cols)
                        if src_cols[c] != new_cols[c]
                    )
                    check(
                        f"{table} columns match exactly ({len(src_cols)})",
                        not missing and not extra and not wrong,
                        f"missing={missing}\nextra={extra}\nwrong type={wrong}",
                    )

                # Fieldmap: row for row.
                src_fm = await data_rows(conn, source_schema, "fieldmap")
                new_fm = await data_rows(conn, clone_schema, "fieldmap")
                check(
                    f"fieldmap matches row for row ({len(src_fm)})",
                    src_fm == new_fm,
                    f"source={len(src_fm)} rows, clone={len(new_fm)} rows",
                )
                check(
                    "no seeded fieldmap rows survived the copy",
                    len(new_fm) == len(src_fm),
                    f"clone has {len(new_fm)}, source has {len(src_fm)}",
                )

                # Reference data: row for row.
                for table in COPIED_TABLES:
                    src_rows = await data_rows(conn, source_schema, table)
                    new_rows = await data_rows(conn, clone_schema, table)
                    check(
                        f"{table} matches row for row ({len(src_rows)})",
                        src_rows == new_rows,
                        f"source={len(src_rows)}, clone={len(new_rows)}",
                    )

                # Sequences: past the copied ids, so the next insert does not
                # collide with a row that was copied in.
                for table in COPIED_TABLES + ("fieldmap",):
                    seq = await conn.fetchval(
                        "SELECT pg_get_serial_sequence($1, 'id')",
                        f'"{clone_schema}"."{table}"',
                    )
                    if seq is None:
                        continue
                    last = await conn.fetchval(f"SELECT last_value FROM {seq}")
                    top = await conn.fetchval(
                        f'SELECT COALESCE(max(id), 0) FROM "{clone_schema}"."{table}"'
                    )
                    check(f"{table} sequence is past id {top}", last >= top, f"last_value={last}")

                # The ledger did not come across.
                for table in LEDGER_TABLES:
                    n = await conn.fetchval(f'SELECT count(*) FROM "{clone_schema}"."{table}"')
                    check(f"{table} is empty in the clone", n == 0, f"got {n} rows")

                # Accounts did not come across.
                n = await conn.fetchval(
                    "SELECT count(*) FROM admin.users WHERE company_id = $1", clone["id"]
                )
                check("clone has no accounts", n == 0, f"got {n}")

                # Every migration is recorded, so db.migrate upgrade keeps working.
                src_mig = await conn.fetchval(
                    "SELECT count(*) FROM admin.schema_migrations WHERE schema_name = $1",
                    source_schema,
                )
                new_mig = await conn.fetchval(
                    "SELECT count(*) FROM admin.schema_migrations WHERE schema_name = $1",
                    clone_schema,
                )
                check(
                    "clone is registered for future migrations",
                    new_mig >= src_mig,
                    f"source={src_mig}, clone={new_mig}",
                )

            # --- refusals ----------------------------------------------------
            print("\n--- refusals ---")
            r = await client.post(
                "/companies/", json={"name": "ZZ Never Created", "copy_from_id": 999999}
            )
            check("unknown source is a 404", r.status_code == 404, r.text[:200])

            async with raw_connection() as conn:
                await conn.execute(
                    "UPDATE admin.companies SET is_active = false WHERE id = $1", blank["id"]
                )
            r = await client.post(
                "/companies/", json={"name": "ZZ Never Created", "copy_from_id": blank["id"]}
            )
            check("deactivated source is a 400", r.status_code == 400, r.text[:200])
            async with raw_connection() as conn:
                await conn.execute(
                    "UPDATE admin.companies SET is_active = true WHERE id = $1", blank["id"]
                )

            # Atomicity: a name clash must leave no schema behind.
            async with raw_connection() as conn:
                before = await conn.fetchval(
                    "SELECT count(*) FROM information_schema.schemata WHERE schema_name LIKE 'company_%'"
                )
            r = await client.post(
                "/companies/", json={"name": COPY_PROBE, "copy_from_id": source["id"]}
            )
            check("duplicate name is a 400", r.status_code == 400, r.text[:200])
            async with raw_connection() as conn:
                after = await conn.fetchval(
                    "SELECT count(*) FROM information_schema.schemata WHERE schema_name LIKE 'company_%'"
                )
            check("failed copy left no schema behind", before == after, f"{before} -> {after}")

            r = await client.post(
                "/companies/", json={"name": "ZZ Never Created", "copy_from_id": 999999}
            )
            async with raw_connection() as conn:
                orphan = await conn.fetchval(
                    "SELECT count(*) FROM admin.companies WHERE name = 'ZZ Never Created'"
                )
            check("refused copy registered no company", orphan == 0, f"got {orphan}")

        # --- cleanup ---------------------------------------------------------
        print("\n--- cleanup ---")
        await purge_probes()
        async with raw_connection() as conn:
            left = await conn.fetchval(
                "SELECT count(*) FROM admin.companies WHERE name = ANY($1::text[])",
                list(PROBE_NAMES),
            )
            check("probe companies removed", left == 0, f"{left} left")
            # The source is only ever read. Prove it rather than asserting it.
            fm = await conn.fetchval(f'SELECT count(*) FROM "{source_schema}".fieldmap')
            check(
                f"source fieldmap untouched ({fm} rows)",
                fm == len(src_fm),
                f"was {len(src_fm)}, now {fm}",
            )

        print(f"\n=== {_passed} passed, {_failed} failed ===")
        return 1 if _failed else 0
    finally:
        app.dependency_overrides.clear()
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
