import asyncio, database

MASTERS = [("head_master","Internal Head"),
           ("rera_head_master","RERA Head"),
           ("idw_head_master","TCP Head")]

async def main():
    await database.init_pool()
    async with database._pool.acquire() as c:
        schemas = [r["schema"] for r in await c.fetch(
            "SELECT schema_name AS schema FROM admin.companies ORDER BY schema_name")]
    for s in schemas:
        async with database.company_connection(s) as c:
            bits = []
            for t, label in MASTERS:
                n = await c.fetchval(
                    f"SELECT count(*) FROM {t} WHERE is_active")
                bits.append(f"{label}={n}")
            rules = await c.fetchval("SELECT count(*) FROM rule")
            conds = await c.fetchval("SELECT count(*) FROM rule_condition")
            types = await c.fetchval(
                "SELECT count(*) FROM account_type_master WHERE is_active")
            print(f"{s:22} {' '.join(bits):48} types={types} rules={rules} conds={conds}")
    # what a row can actually be classified into, per company, from the fieldmap
    print()
    for s in schemas[:3]:
        async with database.company_connection(s) as c:
            rows = await c.fetch(
                "SELECT fieldname, mirrors FROM fieldmap "
                "WHERE mirrors IN ('project','head','rera_head','idw_head') "
                "AND is_active ORDER BY mirrors")
            print(s, {r['mirrors']: r['fieldname'] for r in rows})

asyncio.run(main())
