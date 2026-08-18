"""
Per-company column-alias lookup that drives the importer.

DPL cached this globally with a 60s TTL. That cache is deliberately not ported:
a single process here serves every tenant, and a global cache keyed by nothing
would hand company_002 the aliases company_001 configured. The query is one
indexed read of a six-row table on a connection the caller already holds, so
the cache was buying almost nothing and risking a cross-tenant leak.
"""
from __future__ import annotations

from database import company_connection


async def get_field_mappings(schema: str) -> list[dict]:
    """Active fieldmap rows for one company, in the shape parsers.py expects.

    parsers.py calls .get() on each row, so these must be plain dicts --
    asyncpg Record objects have no .get() and would raise inside the parser.
    """
    async with company_connection(schema) as conn:
        rows = await conn.fetch(
            """
            SELECT id, fieldname, displayname, mapfields, data_type, method
            FROM fieldmap
            WHERE is_active = true
            ORDER BY id
            """
        )
    return [dict(r) for r in rows]


def live_col_types(fieldmap_rows: list[dict]) -> dict:
    """{fieldname: data_type}, the map parsers.py uses for type-driven roles.

    In DPL this came from information_schema against the user-defined `master`
    table. temp_trans has fixed columns instead, so the type is declared on the
    fieldmap row and synthesised here. The parser cannot tell the difference --
    it only ever reads this as a dict.
    """
    return {r["fieldname"]: r["data_type"] for r in fieldmap_rows}
