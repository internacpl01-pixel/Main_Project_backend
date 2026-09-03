"""Put back the two Rules cells company_028 lost when 031 was re-applied.

Migration 032 re-seeds the three rows 028 seeded, because those are the same in
every company and derive from the head names. These two are not seeded -- they
were entered by hand on the Rules page this morning (created_at 07:24:47 and
07:26:38 UTC) and were read back verbatim at 13:14 UTC, before the wipe:

    head_id 17  HO - Admin    RERA  DR
    head_id  3  RERA 2 IDW    IDW   CR

Matched by head NAME rather than by the id in that snapshot: ids differ per
company and, more to the point, a name that no longer resolves should stop this
script rather than plant a rule against whatever now sits at id 17.

Idempotent -- ON CONFLICT DO NOTHING -- so running it twice changes nothing, and
running it after the user has re-entered a cell by hand leaves theirs alone.
"""
import asyncio

import database

SCHEMA = "company_028"

# (head name, account type, direction)
CELLS = [
    ("HO - Admin", "RERA", "DR"),
    ("RERA 2 IDW", "IDW",  "CR"),
]


async def main() -> None:
    await database.init_pool()
    async with database.company_connection(SCHEMA) as conn:
        before = await conn.fetchval("SELECT count(*) FROM rule")
        print(f"{SCHEMA}: {before} rule rows before")

        for name, acct_type, direction in CELLS:
            head = await conn.fetchrow(
                "SELECT id, is_active FROM rera_head_master "
                "WHERE btrim(lower(name)) = btrim(lower($1))", name)
            if head is None:
                print(f"  [SKIP] no head named {name!r} -- not restoring it")
                continue
            if not head["is_active"]:
                print(f"  [SKIP] {name!r} is switched off in Master Data")
                continue
            done = await conn.fetchval(
                """
                INSERT INTO rule (head_id, account_type, direction)
                VALUES ($1, $2, $3)
                ON CONFLICT (head_id, account_type) DO NOTHING
                RETURNING id
                """,
                head["id"], acct_type, direction)
            print(f"  [{'OK' if done else 'ALREADY THERE'}] "
                  f"{name} / {acct_type} -> {direction}")

        rows = await conn.fetch(
            "SELECT h.name, r.account_type, r.direction FROM rule r "
            "JOIN rera_head_master h ON h.id = r.head_id "
            "ORDER BY r.account_type, r.direction, h.name")
        print(f"\n{SCHEMA} grid is now {len(rows)} cells:")
        for r in rows:
            print(f"   {r['name']:<20} {r['account_type']:<8} {r['direction']}")
    await database.close_pool()


asyncio.run(main())
