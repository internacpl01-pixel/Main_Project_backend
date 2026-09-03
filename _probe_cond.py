import asyncio, json
import database
from services import custom_fields, rules, staging

SCHEMA = "company_028"
ACCT = "045563200000264"


async def main():
    await database.init_pool()
    async with database.company_connection(SCHEMA) as conn:
        cols = await custom_fields.data_columns(conn)
        print("COLUMNS:")
        for c in cols:
            print(f"   {c['name']:<28} {c['displayname']:<28} "
                  f"{c['type']:<30} -> {rules.column_kind(c['type'])}")

        acol = await staging.account_column(conn)
        print("\naccount column:", acol)
        d = staging.normalise_account(ACCT)
        rows = await conn.fetch(
            f"SELECT * FROM temp_trans t "
            f"WHERE {staging.account_digits('t.' + acol)} = $1 "
            f"ORDER BY batch_id, row_number", d)
        print(f"\n{len(rows)} staged rows on {ACCT}:")
        keep = [c["name"] for c in cols] + ["id", "credit_debit", "amount",
                                            "rera_head_id"]
        for r in rows:
            print("  ", {k: v for k, v in dict(r).items()
                         if k in keep and v not in (None, "")})

        heads = await conn.fetch(
            "SELECT id, name, is_active FROM rera_head_master ORDER BY name")
        print("\nHEADS:", [(h["id"], h["name"]) for h in heads])
        grid = await conn.fetch("SELECT * FROM rule_v ORDER BY master_kind, head_id")
        print("\nGRID:", [dict(g) for g in grid])
    await database.close_pool()


asyncio.run(main())
