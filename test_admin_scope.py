"""
End-to-end check for company codes, the super-admin boundary, and delete.

    python test_admin_scope.py

Creates throwaway companies through the real API, checks them, deletes them.
Purges leftovers from an aborted run before it starts. Existing companies are
only ever read.

Identity is supplied with dependency_overrides rather than a login, so no
password is needed to run this.
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

PROBE_NAMES = ("ZZ Scope Probe", "ZZ Scope Probe Two")
PROBE_CODE = "zzq"
PROBE_CODE_2 = "zzr"
SCHEMA_RE = re.compile(r"^company_\d{3,}$")

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


def as_user(role, company_id, schema):
    return lambda: {
        "id": None,
        "username": f"probe-{role}",
        "role": role,
        "level": permissions.level_of(role),
        "company_id": company_id,
        "schema": schema,
    }


async def purge():
    async with raw_connection() as conn:
        rows = await conn.fetch(
            "SELECT id, name, schema_name FROM admin.companies "
            "WHERE name = ANY($1::text[]) OR code = ANY($2::text[])",
            list(PROBE_NAMES), [PROBE_CODE, PROBE_CODE_2],
        )
        for r in rows:
            if not SCHEMA_RE.match(r["schema_name"]):
                print(f"  [SKIP] odd schema {r['schema_name']!r}")
                continue
            await conn.execute(f'DROP SCHEMA IF EXISTS "{r["schema_name"]}" CASCADE')
            await conn.execute("DELETE FROM admin.users WHERE company_id = $1", r["id"])
            await conn.execute(
                "DELETE FROM admin.schema_migrations WHERE schema_name = $1", r["schema_name"]
            )
            await conn.execute("DELETE FROM admin.companies WHERE id = $1", r["id"])
            print(f"  [PURGE] {r['name']} ({r['schema_name']})")


async def main():
    await init_pool()
    sa = as_user("super_admin", None, "admin")
    app.dependency_overrides[get_current_user] = sa
    transport = ASGITransport(app=app)

    try:
        print("=== admin scope check ===\n--- purge leftovers ---")
        await purge()

        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # --- code validation ---------------------------------------------
            print("\n--- company code ---")
            for bad, why in [("", "empty"), ("ab", "two letters"), ("abcd", "four letters"),
                             ("a1c", "a digit"), ("a-c", "punctuation")]:
                r = await client.post("/companies/", json={"name": "ZZ Never", "code": bad})
                check(f"code {why!r} refused", r.status_code in (400, 422), f"{r.status_code} {r.text[:120]}")

            r = await client.post(
                "/companies/", json={"name": PROBE_NAMES[0], "code": PROBE_CODE.upper()}
            )
            check("uppercase code accepted and folded", r.status_code == 201, r.text[:200])
            if r.status_code != 201:
                return 1
            one = r.json()["company"]
            check(f"stored lowercase as {PROBE_CODE!r}", one["code"] == PROBE_CODE, one["code"])

            r = await client.post(
                "/companies/", json={"name": PROBE_NAMES[1], "code": PROBE_CODE}
            )
            check("duplicate code refused", r.status_code == 400, r.text[:160])

            r = await client.post(
                "/companies/", json={"name": PROBE_NAMES[1], "code": PROBE_CODE_2}
            )
            check("second company with its own code", r.status_code == 201, r.text[:200])
            two = r.json()["company"] if r.status_code == 201 else None

            # --- username prefix ---------------------------------------------
            print("\n--- username prefix ---")
            r = await client.post(
                f"/companies/{one['id']}/admin", json={"username": "ravi", "password": "pw1234"}
            )
            check("unprefixed username refused", r.status_code == 400, r.text[:160])
            check(
                "refusal suggests the right name",
                f"{PROBE_CODE}-ravi" in r.text,
                r.text[:200],
            )

            r = await client.post(
                f"/companies/{one['id']}/admin",
                json={"username": f"{PROBE_CODE_2}-ravi", "password": "pw1234"},
            )
            check("another company's prefix refused", r.status_code == 400, r.text[:160])

            r = await client.post(
                f"/companies/{one['id']}/admin",
                json={"username": f"{PROBE_CODE}-ravi", "password": "pw1234"},
            )
            check("prefixed username accepted", r.status_code == 201, r.text[:200])
            admin_id = r.json()["admin"]["id"] if r.status_code == 201 else None

            r = await client.post(
                f"/companies/{one['id']}/admin",
                json={"username": f"{PROBE_CODE.upper()}-anita", "password": "pw1234"},
            )
            check("uppercase prefix accepted", r.status_code == 201, r.text[:200])
            if r.status_code == 201:
                check(
                    "prefix folded to lowercase",
                    r.json()["admin"]["username"].startswith(f"{PROBE_CODE}-"),
                    r.json()["admin"]["username"],
                )

            # --- super admin cannot reach company data -------------------------
            print("\n--- super admin boundary ---")
            for method, path in [
                ("get", "/transactions/"), ("get", "/master/bank"), ("get", "/fieldmap/"),
                ("get", "/projects/"), ("get", "/users/"), ("get", "/custom-fields/"),
                ("get", "/transactions/temp-trans"), ("get", "/transactions/summary"),
                ("get", "/export/transactions"), ("get", "/imports/batches"),
            ]:
                r = await getattr(client, method)(path)
                check(f"super admin refused {path}", r.status_code == 403, f"{r.status_code} {r.text[:100]}")

            r = await client.post("/auth/switch-company", json={"schema_name": one["schema_name"]})
            check("switch-company is gone", r.status_code == 404, r.status_code)

            me = (await client.get("/auth/me")).json()
            check("super admin /me has no company", me["company_id"] is None and me["company_code"] is None, me)

            # --- a company user is unaffected ----------------------------------
            print("\n--- company user still works ---")
            app.dependency_overrides[get_current_user] = as_user(
                "company_admin", one["id"], one["schema_name"]
            )
            r = await client.get("/fieldmap/")
            check("company admin reaches its fieldmap", r.status_code == 200, r.text[:160])
            me = (await client.get("/auth/me")).json()
            check("company admin /me carries the code", me["company_code"] == PROBE_CODE, me)
            app.dependency_overrides[get_current_user] = sa

            # --- delete ---------------------------------------------------------
            print("\n--- delete ---")
            r = await client.get(f"/companies/{one['id']}/delete-check")
            check("empty company is deletable", r.json().get("can_delete") is True, r.text[:200])

            r = await client.request(
                "DELETE", f"/companies/{one['id']}", json={"confirm_name": "wrong name"}
            )
            check("wrong confirmation refused", r.status_code == 400, r.text[:160])

            # Give it a transaction so the in-use guard has something to catch.
            async with raw_connection() as conn:
                await conn.execute(
                    f'INSERT INTO "{one["schema_name"]}".import_batches '
                    "(filename, file_hash, uploaded_by, row_count) "
                    "VALUES ('probe.pdf', 'zz-probe-hash', 'probe', 0)"
                )
            r = await client.get(f"/companies/{one['id']}/delete-check")
            check("in-use company reports not deletable", r.json().get("can_delete") is False, r.text[:200])
            r = await client.request(
                "DELETE", f"/companies/{one['id']}", json={"confirm_name": one["name"]}
            )
            check("in-use company refuses delete (409)", r.status_code == 409, f"{r.status_code} {r.text[:160]}")

            async with raw_connection() as conn:
                still = await conn.fetchval(
                    "SELECT 1 FROM admin.companies WHERE id = $1", one["id"]
                )
                check("refused delete left the company intact", still == 1)
                await conn.execute(f'DELETE FROM "{one["schema_name"]}".import_batches')

            r = await client.request(
                "DELETE", f"/companies/{one['id']}", json={"confirm_name": one["name"]}
            )
            check("empty company deletes", r.status_code == 200, r.text[:200])
            if r.status_code == 200:
                check("its accounts went with it", r.json()["users_removed"] == 2, r.json())

            async with raw_connection() as conn:
                gone = await conn.fetchval(
                    "SELECT count(*) FROM information_schema.schemata WHERE schema_name = $1",
                    one["schema_name"],
                )
                check("schema dropped", gone == 0)
                orphan = await conn.fetchval(
                    "SELECT count(*) FROM admin.users WHERE id = $1", admin_id
                ) if admin_id else 0
                check("no orphan accounts", orphan == 0)
                mig = await conn.fetchval(
                    "SELECT count(*) FROM admin.schema_migrations WHERE schema_name = $1",
                    one["schema_name"],
                )
                check("migration records cleared", mig == 0, mig)
                freed = await conn.fetchval(
                    "SELECT count(*) FROM admin.companies WHERE code = $1", PROBE_CODE
                )
                check("code freed for reuse", freed == 0)

            # --- legacy company: code backfill, set once ------------------------
            print("\n--- code backfill ---")
            if two:
                r = await client.patch(f"/companies/{two['id']}", json={"code": "zzs"})
                check("changing an existing code refused", r.status_code == 400, r.text[:160])

        print("\n--- cleanup ---")
        await purge()
        async with raw_connection() as conn:
            left = await conn.fetchval(
                "SELECT count(*) FROM admin.companies WHERE name = ANY($1::text[])",
                list(PROBE_NAMES),
            )
            check("probes removed", left == 0, left)

        print(f"\n=== {_passed} passed, {_failed} failed ===")
        return 1 if _failed else 0
    finally:
        app.dependency_overrides.clear()
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
