"""
Database migration runner.

Commands:
  init               Create admin schema + schema_migrations, apply admin files,
                     seed super-admin (from SUPER_ADMIN_USERNAME / _PASSWORD).
  new-company "Name" CODE
                     Allocate next company_NNN schema, CREATE SCHEMA,
                     apply all company migrations, register in admin.companies.
                     CODE is three lowercase letters and prefixes every
                     username in that company.
  upgrade            Apply any new migration files to admin and every company.
  status             Print applied migrations per schema.
"""
import asyncio
import hashlib
import re
import sys
from pathlib import Path

import asyncpg
import bcrypt

import config

# Paths
BACKEND_DIR    = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BACKEND_DIR / "db" / "migrations"
ADMIN_DIR      = MIGRATIONS_DIR / "admin"
COMPANY_DIR    = MIGRATIONS_DIR / "company"


def sha256_of_file(path: Path) -> str:
    """sha256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(), b""):
            h.update(chunk)
    return h.hexdigest()


async def get_applied(conn, schema: str) -> dict:
    """Return {filename: checksum} for migrations already applied to schema.
    Returns empty dict if schema_migrations doesn't exist yet (first run)."""
    try:
        rows = await conn.fetch(
            "SELECT filename, checksum FROM admin.schema_migrations WHERE schema_name = $1",
            schema,
        )
        return {r["filename"]: r["checksum"] for r in rows}
    except asyncpg.exceptions.UndefinedTableError:
        return {}


async def apply_file(conn, schema: str, file_path: Path):
    """Apply a single SQL file to a schema. Records checksum in schema_migrations."""
    sql = file_path.read_text(encoding="utf-8")
    checksum = sha256_of_file(file_path)

    async with conn.transaction():
        # Switch search_path so unqualified names land in the right schema.
        # 'admin' is included so admin.set_updated_at() is callable.
        await conn.execute(f'SET LOCAL search_path TO "{schema}", admin')
        await conn.execute(sql)
        await conn.execute(
            """
            INSERT INTO admin.schema_migrations (schema_name, filename, checksum)
            VALUES ($1, $2, $3)
            ON CONFLICT (schema_name, filename) DO NOTHING
            """,
            schema,
            file_path.name,
            checksum,
        )


async def apply_set(conn, schema: str, files_dir: Path, label: str):
    """Apply every .sql file in files_dir to schema, in name order."""
    files = sorted(files_dir.glob("*.sql"))
    applied = await get_applied(conn, schema)

    for f in files:
        if f.name in applied:
            # Already applied. Refuse if file changed since.
            if applied[f.name] != sha256_of_file(f):
                print(f"  [REFUSE] {label}/{f.name} checksum mismatch")
                print(f"    file was edited after apply.")
                print(f"    applied checksum: {applied[f.name]}")
                print(f"    current checksum:  {sha256_of_file(f)}")
                print(f"    fix: write a NEW migration file (002_*.sql), do not edit 001.")
                sys.exit(1)
            continue
        print(f"  [APPLY]  {label}/{f.name}")
        await apply_file(conn, schema, f)


class ProvisionError(Exception):
    """A company could not be created. Carries a message fit to show a user."""


# Exactly three lowercase letters, matching the CHECK in
# admin/003_company_code.sql. Every username in the company is prefixed with it,
# so it is short on purpose — 'dpl-ravi' stays readable in a way that
# 'dpl-homes-gurgaon-ravi' does not.
COMPANY_CODE_RE = re.compile(r"^[a-z]{3}$")


def normalize_company_code(code: str) -> str:
    """Fold a typed code to its stored form, or refuse it.

    Lowercasing here is what makes the code case-insensitive everywhere else:
    'DPL' and 'dpl' become the same three characters before they reach the
    unique index, so nobody can register both.
    """
    code = (code or "").strip().lower()
    if not COMPANY_CODE_RE.match(code):
        raise ProvisionError(
            "Company code must be exactly three letters (a-z), no digits or "
            "punctuation. It is stored lowercase and matched case-insensitively, "
            "and every username in the company begins with it."
        )
    return code


async def provision_company(
    conn, name: str, code: str, created_by: int | None = None
) -> dict:
    """
    Create one company: allocate a schema name, CREATE SCHEMA, register it in
    admin.companies, and apply every company migration to it.

    Shared by the `new-company` CLI command and POST /companies so the two can
    never drift — a company registered from the UI is byte-for-byte the same as
    one made from the terminal.

    The whole thing runs in a single transaction. Postgres DDL is transactional,
    so a migration that fails halfway leaves no schema and no companies row
    behind, rather than a half-built tenant someone has to clean up by hand.
    (The sequence value is spent either way; sequences do not roll back. That is
    correct — a burnt number is cheaper than two companies racing for one name.)

    Returns the new admin.companies row as a dict.
    Raises ProvisionError with a user-facing message on bad input or a clash.
    """
    name = (name or "").strip()
    if not name:
        raise ProvisionError("Company name is required.")
    if len(name) < 2:
        raise ProvisionError("Company name must be at least 2 characters.")

    code = normalize_company_code(code)

    clash = await conn.fetchval(
        "SELECT 1 FROM admin.companies WHERE lower(name) = lower($1)", name
    )
    if clash:
        raise ProvisionError(f"A company named '{name}' already exists.")

    taken = await conn.fetchval("SELECT name FROM admin.companies WHERE code = $1", code)
    if taken:
        raise ProvisionError(f"Code '{code}' is already used by '{taken}'.")

    files = sorted(COMPANY_DIR.glob("*.sql"))
    if not files:
        raise ProvisionError(f"No company migrations found in {COMPANY_DIR}.")

    async with conn.transaction():
        n = await conn.fetchval("SELECT nextval('admin.company_schema_seq')")
        schema = f"company_{n:03d}"
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        row = await conn.fetchrow(
            """
            INSERT INTO admin.companies (name, code, schema_name, created_by)
            VALUES ($1, $2, $3, $4)
            RETURNING id, name, code, schema_name, is_active, created_at, created_by
            """,
            name,
            code,
            schema,
            created_by,
        )
        # apply_file opens a nested transaction (a savepoint), so a failure here
        # still unwinds the CREATE SCHEMA above.
        for f in files:
            await apply_file(conn, schema, f)

    return dict(row)


async def ensure_schema(conn, name: str):
    """Make sure a top-level schema exists."""
    exists_row = await conn.fetchval(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1",
        name,
    )
    if not exists_row:
        print(f"  [CREATE] schema '{name}'")
        await conn.execute(f'CREATE SCHEMA "{name}"')


# ----------------------------- Commands -------------------------------------

async def cmd_init():
    """init: bootstrap admin schema, apply all admin migrations, seed super-admin."""
    print("=== init ===")
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        await ensure_schema(conn, "admin")

        admin_files = sorted(ADMIN_DIR.glob("*.sql"))
        if not admin_files:
            print(f"  [ERROR] No SQL files found in {ADMIN_DIR}")
            sys.exit(1)
        await apply_set(conn, "admin", ADMIN_DIR, "admin")

        user_count = await conn.fetchval("SELECT count(*) FROM admin.users")
        if user_count == 0:
            pw_hash = bcrypt.hashpw(
                config.SUPER_ADMIN_PASSWORD.encode("utf-8"),
                bcrypt.gensalt(rounds=12),
            ).decode("utf-8")
            await conn.execute(
                """
                INSERT INTO admin.users (username, password_hash, role, company_id)
                VALUES ($1, $2, 'super_admin', NULL)
                """,
                config.SUPER_ADMIN_USERNAME,
                pw_hash,
            )
            print(f"  [SEED]   super-admin '{config.SUPER_ADMIN_USERNAME}' created")
            print(f"           (company_id = NULL, so this user sees all companies)")
        else:
            print(f"  [SKIP]   super-admin already exists ({user_count} user(s))")

        print("\n[OK] init complete.")
    finally:
        await conn.close()


async def cmd_new_company(name: str, code: str):
    """new-company 'Name' CODE: create schema + apply migrations + register."""
    if not name or not code:
        print('Usage: python -m db.migrate new-company "Company Name" abc')
        sys.exit(1)

    print(f"=== new-company '{name}' [{code}] ===")
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        try:
            company = await provision_company(conn, name, code)
        except ProvisionError as e:
            print(f"  [ERROR] {e}")
            sys.exit(1)

        schema = company["schema_name"]
        print(f"  [CREATE] schema '{schema}' + registered in admin.companies")
        for f in sorted(COMPANY_DIR.glob("*.sql")):
            print(f"  [APPLY]  {schema}/{f.name}")

        print(f"\n[OK] company '{name}' ready in schema {schema}.")
        print(f"     Login as '{config.SUPER_ADMIN_USERNAME}' and add company users next.")
    finally:
        await conn.close()


async def cmd_upgrade():
    """upgrade: apply new files to admin + every active company schema."""
    print("=== upgrade ===")
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        await apply_set(conn, "admin", ADMIN_DIR, "admin")

        rows = await conn.fetch(
            "SELECT schema_name FROM admin.companies WHERE is_active = true ORDER BY schema_name"
        )
        for r in rows:
            await apply_set(conn, r["schema_name"], COMPANY_DIR, r["schema_name"])

        print("\n[OK] upgrade complete.")
    finally:
        await conn.close()


async def cmd_status():
    """status: print applied migrations per schema."""
    print("=== status ===")
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        admin_applied = await get_applied(conn, "admin")
        admin_avail = sorted(p.name for p in ADMIN_DIR.glob("*.sql"))
        print(f"\nadmin:")
        for f in admin_avail:
            mark = "[x]" if f in admin_applied else "[ ]"
            print(f"  {mark} {f}")

        rows = await conn.fetch(
            "SELECT schema_name FROM admin.companies WHERE is_active = true ORDER BY schema_name"
        )
        company_avail = sorted(p.name for p in COMPANY_DIR.glob("*.sql"))
        for r in rows:
            schema = r["schema_name"]
            applied = await get_applied(conn, schema)
            print(f"\n{schema}:")
            for f in company_avail:
                mark = "[x]" if f in applied else "[ ]"
                print(f"  {mark} {f}")
    finally:
        await conn.close()


# ----------------------------- Entry point ----------------------------------

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("init", "new-company", "upgrade", "status"):
        print("Usage: python -m db.migrate <command> [args]")
        print("  init")
        print('  new-company "Company Name" abc')
        print("  upgrade")
        print("  status")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "init":
        asyncio.run(cmd_init())
    elif cmd == "new-company":
        if len(sys.argv) < 4:
            print('Usage: python -m db.migrate new-company "Company Name" abc')
            sys.exit(1)
        asyncio.run(cmd_new_company(sys.argv[2], sys.argv[3]))
    elif cmd == "upgrade":
        asyncio.run(cmd_upgrade())
    elif cmd == "status":
        asyncio.run(cmd_status())


if __name__ == "__main__":
    main()