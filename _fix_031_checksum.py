"""Repair the checksum _apply_031.py recorded with the wrong hash function.

The migration runner records sha256_of_file(); the hand-written script that
bypassed it recorded Postgres md5() of the same text. The file itself was never
edited, so this rewrites the ledger to the value the runner would have written.

Refuses unless md5(file) still equals what is stored -- that equality is the
proof the file is unchanged. If it ever fails, the file DID change and the
right answer is a new migration, not a rewritten checksum.
"""
import asyncio
import hashlib
import pathlib

import database

FILE = pathlib.Path("db/migrations/company/031_rule_multi_master.sql")
NAME = FILE.name


async def main() -> None:
    raw = FILE.read_bytes()
    md5 = hashlib.md5(raw).hexdigest()
    sha = hashlib.sha256(raw).hexdigest()

    await database.init_pool()
    async with database.company_connection("company_028") as conn:
        rows = await conn.fetch(
            "SELECT schema_name, checksum FROM admin.schema_migrations "
            "WHERE filename = $1 ORDER BY schema_name", NAME)

        stale = [r["schema_name"] for r in rows if r["checksum"] == md5]
        other = [(r["schema_name"], r["checksum"]) for r in rows
                 if r["checksum"] not in (md5, sha)]
        if other:
            raise SystemExit(f"REFUSING: unexpected checksums {other}")

        print(f"{len(rows)} rows for {NAME}; {len(stale)} carry the md5")
        if stale:
            n = await conn.execute(
                "UPDATE admin.schema_migrations SET checksum = $1 "
                "WHERE filename = $2 AND checksum = $3", sha, NAME, md5)
            print("  ->", n)
    await database.close_pool()
    print("[OK] ledger now records the runner's own sha256.")


asyncio.run(main())
