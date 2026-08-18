"""
Custom fields — user-added columns on temp_trans.

Ported from DPL_project/backend/routers/mappings.py (create_custom_field /
delete_custom_field) and services/mappings.py (get_table_structure). The logic
is unchanged: a new field is a real column plus a fieldmap row, the column is
named `field_{type}_{n}` with n one past the highest existing n of that type,
and deleting removes both sides independently so a field missing either one
still cleans up.

Two things differ, both forced by the target table:

  * DPL added columns to `master`, a table whose every column was a custom
    field. Here the target is temp_trans, which has real structural columns
    (batch_id, amount, credit_debit, the master FKs). So "is this a custom
    field?" cannot be "is it not id" as it was in DPL.

  * That question is answered from the naming convention this module itself
    generates, never from a list of column names. A hardcoded list of
    structural columns would go stale the next time a migration adds one, and
    the first symptom would be a DROP COLUMN on a column that carries data.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# The two tables a custom field lives on. They are the same row at two stages
# of its life -- staged, then posted -- and finalize copies one into the other,
# so a column on one and not the other is a column finalize cannot carry. Every
# create and delete below touches both, in one transaction, so they cannot
# drift. Migration 007 brought existing schemas into line.
TARGET_TABLE = "temp_trans"
LEDGER_TABLE = "transactions"
SYNCED_TABLES = (TARGET_TABLE, LEDGER_TABLE)

# type -> (SQL type, column prefix). The keys are the values the API accepts;
# the SQL types match what fieldmap.data_type can describe, so a custom column
# and its fieldmap row always agree on what the parser should coerce it to.
TYPE_MAP = {
    "date": ("DATE", "field_date", "date"),
    "num": ("NUMERIC(18,2)", "field_num", "numeric"),
    "text": ("TEXT", "field_text", "text"),
}

# A column created by this module, and therefore one it may drop. Anything that
# does not match is structural and is refused — derived from the naming scheme
# in _next_column_name(), not from a list that has to be maintained by hand.
CUSTOM_COLUMN_RE = re.compile(r"^field_(?:date|num|text)_\d+$")

# A column name cannot be a bind parameter, so ALTER TABLE has to interpolate
# it. This is what makes that safe — nothing but a plain lowercase identifier
# ever reaches the SQL string. Copied from DPL's _SAFE_COLUMN.
SAFE_COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class CustomFieldError(RuntimeError):
    """Bad request from the caller — surfaced as a 400."""


def is_custom_field(fieldname: str) -> bool:
    return bool(CUSTOM_COLUMN_RE.match(fieldname or ""))


async def table_structure(conn) -> list[dict]:
    """Live columns on temp_trans, in ordinal order.

    DPL's /api/table-structure. The Custom Fields page reads it to show each
    fieldmap row's real column type, and to flag rows whose column was dropped
    straight from Postgres.
    """
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = $1
        ORDER BY ordinal_position
        """,
        TARGET_TABLE,
    )
    return [
        {
            "column_name": r["column_name"],
            "data_type": r["data_type"],
            "is_nullable": r["is_nullable"],
            "is_custom": is_custom_field(r["column_name"]),
        }
        for r in rows
    ]


async def _undroppable_columns(conn) -> dict[str, str]:
    """{column: why} for columns the database itself will not let go of.

    Asked of the catalog, never listed here. A hand-written set of "core"
    columns is wrong the moment a migration adds one, and it is also a lie to
    the user: the only columns that genuinely cannot be dropped are the ones
    something is structurally attached to.

    Two sources:
      * primary key   — dropping it breaks row identity, ordering, every join
      * foreign key   — dropping it breaks the link to a master table

    Everything else is the user's to remove, exactly as in DPL, where every
    column on `master` except the primary key was deletable.
    """
    rows = await conn.fetch(
        """
        -- contype is Postgres's "char" type, which asyncpg hands back as bytes
        -- (b'p'), not str. Cast it here so the lookup below is a plain string.
        SELECT DISTINCT a.attname AS column_name, c.contype::text AS contype
        FROM pg_constraint c
        JOIN unnest(c.conkey) AS k(attnum) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.conrelid = (current_schema() || '.' || $1)::regclass
          AND c.contype IN ('p', 'f')
        """,
        TARGET_TABLE,
    )
    reason = {"p": "primary key", "f": "foreign key to a master table"}
    return {r["column_name"]: reason[r["contype"]] for r in rows}


async def _next_column_name(conn, prefix: str) -> str:
    """field_num_1, field_num_2, ... — one past the highest n already present.

    Reads the live columns rather than counting fieldmap rows: a column dropped
    outside the app would otherwise let the next create collide with a name
    that still exists.
    """
    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = $1
          AND column_name LIKE $2
        """,
        TARGET_TABLE,
        prefix + "_%",
    )
    max_num = 0
    for r in rows:
        try:
            max_num = max(max_num, int(r["column_name"].split("_")[-1]))
        except ValueError:
            pass
    return f"{prefix}_{max_num + 1}"


async def create_custom_field(conn, field_type: str, displayname: str = "",
                              mapfields: str = "", method: str = "") -> dict:
    """Add a column to temp_trans and register it in the fieldmap.

    Both halves matter. Without the column the importer has nowhere to put the
    value; without the fieldmap row the parser has no alias to recognise the
    bank's header by, so the column would never be filled.
    """
    field_type = (field_type or "").strip().lower()
    if field_type not in TYPE_MAP:
        raise CustomFieldError(
            f"Invalid type '{field_type}'. Use one of: {', '.join(TYPE_MAP)}."
        )

    sql_type, prefix, data_type = TYPE_MAP[field_type]
    col_name = await _next_column_name(conn, prefix)

    # Belt and braces: col_name is generated above, never user input, but it is
    # interpolated so it is checked anyway.
    if not SAFE_COLUMN_RE.match(col_name):
        raise CustomFieldError(f"Generated an unsafe column name: {col_name!r}")

    for table in SYNCED_TABLES:
        await conn.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {sql_type}"
        )

    display = (displayname or "").strip() or col_name
    # The column's own name seeds mapfields so the field is matchable the moment
    # it exists; the user narrows it to the bank's real header afterwards.
    aliases = (mapfields or "").strip() or col_name

    row = await conn.fetchrow(
        """
        INSERT INTO fieldmap (fieldname, displayname, mapfields, data_type, method)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (fieldname) DO UPDATE
            SET displayname = EXCLUDED.displayname,
                mapfields   = EXCLUDED.mapfields,
                data_type   = EXCLUDED.data_type,
                method      = EXCLUDED.method,
                is_active   = true
        RETURNING id, fieldname, displayname, mapfields, data_type, method, is_active
        """,
        col_name, display, aliases, data_type, (method or "").strip(),
    )

    logger.info("[custom_fields] added %s.%s %s", TARGET_TABLE, col_name, sql_type)
    return {"column": col_name, "type": sql_type, "fieldmap": dict(row)}


async def delete_custom_field(conn, fieldname: str) -> dict:
    """Drop the column AND its fieldmap row.

    Every field is deletable. DPL's rule verbatim -- there, the only exception
    was `master`'s primary key, and here the exceptions come from
    _undroppable_columns(), which asks the catalog the same question instead of
    naming columns. Nothing is "core" because the fieldmap defines it; a field
    is only undeletable if the database is structurally holding on to it.

    The column and the mapping are removed independently, because the two drift
    apart in practice -- a column dropped straight from Postgres leaves its
    fieldmap row orphaned -- so a field missing either one still cleans up.
    """
    # No case folding. Column names here are always lowercase, so folding buys
    # nothing — but it would let "Field_Num_1" resolve to a real field the
    # caller never named, which is the wrong way for a DROP COLUMN to be
    # forgiving. SAFE_COLUMN_RE rejects anything uppercase outright.
    name = (fieldname or "").strip()
    if not SAFE_COLUMN_RE.match(name):
        raise CustomFieldError(f"Invalid field name: {fieldname!r}")

    blocked = await _undroppable_columns(conn)
    if name in blocked:
        raise CustomFieldError(
            f"'{name}' is the {blocked[name]} of {TARGET_TABLE} and cannot be "
            f"dropped. Every other field can."
        )

    record = await conn.fetchrow("SELECT id FROM fieldmap WHERE fieldname = $1", name)
    has_column = await conn.fetchval(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2
        """,
        TARGET_TABLE, name,
    )
    if not record and not has_column:
        return {"found": False}

    if has_column:
        # IF EXISTS on the ledger side: a field created before the two tables
        # were kept in step may only exist on temp_trans.
        await conn.execute(f'ALTER TABLE {TARGET_TABLE} DROP COLUMN "{name}"')
        await conn.execute(f'ALTER TABLE {LEDGER_TABLE} DROP COLUMN IF EXISTS "{name}"')
    if record:
        await conn.execute("DELETE FROM fieldmap WHERE fieldname = $1", name)

    logger.info("[custom_fields] deleted %s (column=%s, mapping=%s)",
                name, bool(has_column), bool(record))
    return {
        "found": True,
        "fieldname": name,
        "column_dropped": bool(has_column),
        "mapping_removed": bool(record),
        "message": f"Field '{name}' deleted",
    }


# Columns temp_trans carries to run the staging workflow rather than to hold
# statement data. They are the plumbing this module and the importer own, not
# fields the user defined, so they are not offered as display columns.
#
# This is the only place a column name is written down, and it is deliberately
# limited to the staging contract itself. Everything a user can add, rename or
# delete comes from the fieldmap.
_STAGING_INTERNALS = frozenset({
    "row_hash",      # dedup fingerprint
    "row_number",    # position in the source file
    "is_classified", # workflow state, rendered as the Status column
    "raw_data",      # the parser's untouched output
    "created_at",
    "updated_at",
})


# The two columns the withdrawal/deposits collapse produces. They have no
# fieldmap row by construction -- they are not a column any bank prints, they
# are what the importer makes of two that it does -- so there is nowhere else
# for their labels to come from. Every other header comes from the fieldmap.
_DERIVED_LABELS = {"amount": "Amount", "credit_debit": "DR/CR"}


async def data_columns(conn, hide_redundant: bool = True) -> list[dict]:
    """The columns of temp_trans that hold statement data, in table order.

    DPL's get_master_rows() shape: {name, displayname, type}. The display name
    comes from the fieldmap, so renaming a field renames the column header
    everywhere it is shown, and a custom field appears the moment it is created
    without anything being told about it.

    What is excluded is derived, not listed: primary and foreign keys come from
    the catalog via _undroppable_columns(), and the staging internals above are
    this table's own workflow machinery.
    """
    structure = await table_structure(conn)
    blocked = await _undroppable_columns(conn)
    fieldmap = [dict(r) for r in await conn.fetch(
        "SELECT fieldname, displayname, mapfields FROM fieldmap WHERE is_active = true"
    )]
    display = {r["fieldname"]: (r["displayname"] or r["fieldname"]) for r in fieldmap}

    # Imported here rather than at module scope: import_helpers already imports
    # this module for table_structure, so a top-level import would be a cycle.
    from import_helpers import fields_by_category

    # amount and credit_debit are the ledger's form of the money -- one value
    # plus a direction -- produced by collapsing the withdrawal and deposits
    # columns. When those two are themselves real columns, the same figure is on
    # screen twice, once as the bank printed it and once collapsed. Staging
    # shows what the statement said, so the collapsed pair is dropped and the
    # bank's own columns win. Decided from the fieldmap, so a company that maps
    # only one amount column still sees Amount and DR/CR.
    #
    # hide_redundant=False turns that off, and finalize passes it: the pair is
    # only visually redundant. transactions.credit_debit is NOT NULL, so a copy
    # that skipped it failed outright --
    #   null value in column "credit_debit" violates not-null constraint
    # -- which is the difference between "do not draw this twice" and "do not
    # carry this to the ledger".
    by_cat = fields_by_category(fieldmap)
    live = {c["column_name"] for c in structure}
    raw_amounts_shown = hide_redundant and (
        by_cat.get("withdrawal") in live and by_cat.get("deposits") in live
    )
    redundant = {"amount", "credit_debit"} if raw_amounts_shown else set()

    def label(name: str) -> str:
        if name in display:
            return display[name]
        # No fieldmap row: either one of the two derived columns above, or a
        # column added outside the app. Title-cased so it is at least readable
        # rather than printing a raw identifier as a header.
        return _DERIVED_LABELS.get(name) or name.replace("_", " ").title()

    return [
        {
            "name": c["column_name"],
            "displayname": label(c["column_name"]),
            "type": c["data_type"],
            "is_custom": c["is_custom"],
        }
        for c in structure
        if c["column_name"] not in blocked
        and c["column_name"] not in _STAGING_INTERNALS
        and c["column_name"] not in redundant
    ]


async def date_column(conn) -> str | None:
    """The column holding a transaction's date, for filtering and ordering.

    Resolved through the fieldmap's date category, so it follows a renamed or
    rebuilt field. Returns None when the company has no date field at all, and
    callers fall back to ordering by id -- a ledger with no date column is odd
    but it should still list rather than 500.
    """
    from import_helpers import fields_by_category

    rows = [dict(r) for r in await conn.fetch(
        "SELECT fieldname, displayname, mapfields FROM fieldmap WHERE is_active = true"
    )]
    name = fields_by_category(rows).get("date")
    if not name:
        return None
    live = {c["column_name"] for c in await table_structure(conn)}
    return name if name in live else None


async def list_custom_fields(conn) -> list[dict]:
    """Every fieldmap row, annotated with its real column type.

    Returns all of them, not just the ones matching the generated naming
    scheme, because that is what DPL's page showed and because on this page
    there is no such thing as a field the user does not own. `deletable` comes
    from the catalog, so the UI never has to decide it.
    """
    structure = {c["column_name"]: c for c in await table_structure(conn)}
    blocked = await _undroppable_columns(conn)
    rows = await conn.fetch(
        """
        SELECT id, fieldname, displayname, mapfields, data_type, method, is_active
        FROM fieldmap ORDER BY id
        """
    )
    out = []
    for r in rows:
        name = r["fieldname"]
        col = structure.get(name)
        out.append({
            **dict(r),
            "column_type": col["data_type"] if col else None,
            "has_column": col is not None,
            "is_custom": is_custom_field(name),
            "deletable": name not in blocked,
            "locked_reason": blocked.get(name),
        })
    return out
