"""
Company cloning — give a new company the shape and reference data of an
existing one.

A company on this install is five separable things:

  1. a schema with migrations 001..NNN applied      <- provision_company builds this
  2. the columns a user added to or removed from temp_trans / transactions
  3. the fieldmap: labels, aliases, types, methods
  4. reference data: projects and the five master tables
  5. its ledger: import_batches, temp_trans rows, transactions, fieldchange_log

This module copies 2, 3 and 4 onto a schema that provision_company has already
built. It never touches 5. A clone that carried the ledger would not be a new
company, it would be the same company under a second name — and the counts on
its dashboard would be somebody else's money.

Accounts are not copied either. admin.users.username is UNIQUE across the whole
install (login resolves a user before any company is known), so copying them is
not possible without mangling names. The caller seeds a first admin instead,
exactly as registering a blank company does.

Everything here is schema-qualified rather than driven by search_path: this is
the one operation that legitimately reads one company's tables while writing
another's, so there is no single "current" schema to set.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The tables whose column list a user can change, through the Custom Fields
# page. They are the same row at two stages of its life, and custom_fields.py
# adds to and drops from both together, so both are matched to the source.
SHAPED_TABLES = ("temp_trans", "transactions")

# Reference data the clone starts with. This is the part that saves the work:
# re-typing forty heads and a dozen beneficiaries into a new company is exactly
# the tedium a copy is for.
#
# projects comes first so that anything later wanting a project_id finds one.
# project_members is deliberately absent — it maps projects to admin.users, and
# the clone has no users to map yet.
COPIED_TABLES = (
    "projects",
    "bank_master",
    "beneficiary_master",
    "head_master",
    "rera_head_master",
    "idw_head_master",
    # Added with 014. Neither carries a foreign key, so position here is free —
    # appended rather than grouped alphabetically to keep the existing order
    # stable in the clone preview.
    "company_master",
    "account_type_master",
)

# Row timestamps are not carried across. A bank in the clone was added when the
# clone was made, not when someone added it to the source company two years
# ago; copying the old date would put a created_at on a row that predates the
# company it belongs to.
_NOT_COPIED_COLUMNS = frozenset({"created_at", "updated_at"})

# The columns and the fieldmap are one choice, not two. A fieldmap row whose
# column does not exist is a field the importer cannot fill, and a column with
# no fieldmap row is a header with no label — custom_fields.py creates and
# deletes the pair together for exactly this reason, so a copy offers them
# together too.
FIELDS_PART = "fields"

# Everything a copy can bring across, in the order it is applied. The keys are
# table names rather than invented ones so that adding a table to COPIED_TABLES
# adds a checkbox to the register screen with no other edit.
CLONE_PARTS = (FIELDS_PART,) + COPIED_TABLES


class CloneError(RuntimeError):
    """The clone could not be completed. Carries a message fit to show a user."""


def resolve_parts(parts) -> tuple[str, ...]:
    """Normalise a requested selection into canonical order.

    None means everything — the default for a caller that does not care. An
    empty list is a real answer meaning nothing, and is left as such: a copy
    with nothing selected produces the same company a blank registration does,
    which is odd but not wrong, and silently turning it into "everything" would
    be far worse than letting it be empty.
    """
    if parts is None:
        return CLONE_PARTS
    wanted = set(parts)
    unknown = sorted(wanted - set(CLONE_PARTS))
    if unknown:
        raise CloneError(
            f"Cannot copy {', '.join(unknown)}. Choose from: {', '.join(CLONE_PARTS)}."
        )
    return tuple(p for p in CLONE_PARTS if p in wanted)


def _ident(name: str) -> str:
    """Quote one identifier for interpolation into SQL.

    Column names here come from the catalog and schema names are generated
    (company_NNN), so neither is user input — but they are interpolated, since
    Postgres has no bind parameter for an identifier, so they are quoted anyway.
    """
    return '"' + str(name).replace('"', '""') + '"'


def _rel(schema: str, table: str) -> str:
    return f"{_ident(schema)}.{_ident(table)}"


async def _require_table(conn, schema: str, table: str):
    exists = await conn.fetchval(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = $1 AND table_name = $2
        """,
        schema, table,
    )
    if not exists:
        raise CloneError(f"{schema}.{table} does not exist.")


async def _columns(conn, schema: str, table: str) -> dict[str, dict]:
    """{column_name: {coltype, notnull, coldefault, ordinal}} for one table.

    format_type() reproduces the declared type exactly, so numeric(18,2)
    arrives as numeric(18,2) rather than a bare numeric — the same reason
    007_sync_ledger_columns.sql uses it.
    """
    await _require_table(conn, schema, table)
    rows = await conn.fetch(
        """
        SELECT a.attname                                AS name,
               format_type(a.atttypid, a.atttypmod)     AS coltype,
               a.attnotnull                             AS notnull,
               a.attnum                                 AS ordinal,
               pg_get_expr(d.adbin, d.adrelid)          AS coldefault
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d
               ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = (quote_ident($1) || '.' || quote_ident($2))::regclass
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        schema, table,
    )
    return {r["name"]: dict(r) for r in rows}


async def _protected_columns(conn, schema: str, table: str) -> set[str]:
    """Columns the database is structurally holding on to: keys, in or out.

    Asked of the catalog, never listed here — the same question
    custom_fields._undroppable_columns() asks before a DROP COLUMN, for the
    same reason.

    In practice the diff below never proposes dropping one of these: both
    schemas come from the same migrations, and the only supported way to change
    a company's columns is the Custom Fields page, which refuses to drop a key.
    The guard is for the unsupported way — a column dropped by hand in Postgres
    — where the difference between a faithful copy and a broken table is
    whether something checked.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT a.attname AS name
        FROM pg_constraint c
        JOIN unnest(c.conkey) AS k(attnum) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.conrelid = (quote_ident($1) || '.' || quote_ident($2))::regclass
          AND c.contype IN ('p', 'f')
        """,
        schema, table,
    )
    return {r["name"] for r in rows}


async def _sync_shape(conn, source_schema: str, target_schema: str, table: str) -> dict:
    """Make target.table carry exactly the columns source.table carries.

    Both directions, and both are needed. A fresh schema is at the migration
    baseline; a company that has been used is not. company_001 has custom
    fields the baseline lacks AND has had txn_date, description and balance
    deleted, which dropped those columns for real. Add-only would leave the
    clone with three phantom columns the source does not have, and they would
    show up on the staging screen as headers with no fieldmap row behind them.

    No list of structural columns appears here, unlike 007_sync_ledger_columns,
    which compares two different tables and so has to know which columns belong
    to which. This compares the same table in two schemas: anything structural
    is present in both by construction, so the diff leaves it alone on its own.
    """
    src = await _columns(conn, source_schema, table)
    tgt = await _columns(conn, target_schema, table)
    target_rel = _rel(target_schema, table)

    added: list[str] = []
    for name, col in sorted(src.items(), key=lambda kv: kv[1]["ordinal"]):
        if name in tgt:
            continue
        sql = f"ALTER TABLE {target_rel} ADD COLUMN {_ident(name)} {col['coltype']}"
        default = col["coldefault"]
        # A serial's default is nextval() against a sequence in the SOURCE
        # schema. Copying that verbatim would have two companies drawing ids
        # from one counter. Identity columns exist in both schemas anyway, so
        # this branch only ever skips a default that would have been wrong.
        if default and "nextval(" not in default:
            sql += f" DEFAULT {default}"
        if col["notnull"]:
            sql += " NOT NULL"
        await conn.execute(sql)
        added.append(name)

    protected = await _protected_columns(conn, target_schema, table)
    dropped: list[str] = []
    for name in sorted(tgt, key=lambda n: tgt[n]["ordinal"]):
        if name in src:
            continue
        if name in protected:
            logger.warning(
                "[clone] %s.%s is a key on the target and %s does not have it — kept",
                table, name, source_schema,
            )
            continue
        await conn.execute(f"ALTER TABLE {target_rel} DROP COLUMN {_ident(name)}")
        dropped.append(name)

    return {"added": added, "dropped": dropped}


async def _shared_columns(conn, source_schema: str, target_schema: str, table: str) -> list[str]:
    """Columns present on both sides, in the source's order, minus timestamps."""
    src = await _columns(conn, source_schema, table)
    tgt = await _columns(conn, target_schema, table)
    return [
        name
        for name, col in sorted(src.items(), key=lambda kv: kv[1]["ordinal"])
        if name in tgt and name not in _NOT_COPIED_COLUMNS
    ]


async def _reset_sequence(conn, schema: str, table: str, column: str = "id"):
    """Point the table's sequence past the highest id just inserted.

    Rows are copied with their ids intact, which leaves the target's sequence
    still sitting at 1. Without this the first bank anyone adds to the clone
    collides with a row that was copied into it.
    """
    seq = await conn.fetchval(
        "SELECT pg_get_serial_sequence($1, $2)", f"{_ident(schema)}.{_ident(table)}", column
    )
    if seq is None:
        return
    rel = _rel(schema, table)
    # is_called = false when the table came out empty, so the next value is 1
    # rather than 2.
    await conn.execute(
        f"""
        SELECT setval(
            $1,
            COALESCE((SELECT max({_ident(column)}) FROM {rel}), 1),
            (SELECT count(*) > 0 FROM {rel})
        )
        """,
        seq,
    )


async def _copy_rows(conn, source_schema: str, target_schema: str, table: str) -> int:
    """Copy every row of one table across, ids and all. Returns the row count."""
    cols = await _shared_columns(conn, source_schema, target_schema, table)
    if not cols:
        return 0
    col_list = ", ".join(_ident(c) for c in cols)
    copied = await conn.fetchval(
        f"""
        WITH moved AS (
            INSERT INTO {_rel(target_schema, table)} ({col_list})
            SELECT {col_list} FROM {_rel(source_schema, table)} ORDER BY id
            RETURNING 1
        )
        SELECT count(*) FROM moved
        """
    )
    await _reset_sequence(conn, target_schema, table)
    return int(copied or 0)


async def _copy_fieldmap(conn, source_schema: str, target_schema: str) -> int:
    """Replace the target's fieldmap with the source's.

    DELETE first, deliberately. Migration 003 seeds six rows into every new
    schema, so the target is not empty when this runs. An upsert would let the
    seeded row win on fieldname conflict and silently discard the source's
    version — the clone's custom fields would look right while txn_date and
    description quietly kept default labels, which is the kind of wrong that is
    only noticed weeks later.
    """
    await conn.execute(f"DELETE FROM {_rel(target_schema, 'fieldmap')}")
    return await _copy_rows(conn, source_schema, target_schema, "fieldmap")


async def clone_into(
    conn, *, source_schema: str, target_schema: str, parts=None
) -> dict:
    """Shape and stock target_schema to match source_schema.

    Expects a schema provision_company has already built and registered. Does
    not open a transaction: the caller wraps this together with the
    provisioning, so a failure here leaves no half-built company behind.

    parts selects what to bring across; None brings everything. The parts are
    independent — master data does not depend on the field setup, so copying a
    company's heads onto an otherwise default company is a legal answer.

    Returns what was copied, for the caller to report.
    """
    if source_schema == target_schema:
        raise CloneError("A company cannot be cloned onto itself.")

    chosen = resolve_parts(parts)

    shape: dict[str, dict] = {}
    fields = 0
    if FIELDS_PART in chosen:
        for table in SHAPED_TABLES:
            shape[table] = await _sync_shape(conn, source_schema, target_schema, table)
        fields = await _copy_fieldmap(conn, source_schema, target_schema)

    rows: dict[str, int] = {}
    for table in COPIED_TABLES:
        if table in chosen:
            rows[table] = await _copy_rows(conn, source_schema, target_schema, table)

    added = sum(len(s["added"]) for s in shape.values())
    dropped = sum(len(s["dropped"]) for s in shape.values())
    logger.info(
        "[clone] %s -> %s: %d fields, %d columns added, %d dropped, %d reference rows",
        source_schema, target_schema, fields, added, dropped, sum(rows.values()),
    )

    return {
        "source_schema": source_schema,
        "target_schema": target_schema,
        "parts": list(chosen),
        "fields": fields,
        "columns_added": added,
        "columns_dropped": dropped,
        "projects": rows.get("projects", 0),
        "masters": sum(v for k, v in rows.items() if k != "projects"),
        "tables": rows,
        "shape": shape,
    }


async def clone_preview(conn, source_schema: str) -> dict:
    """What cloning this company would copy, for the confirmation screen.

    Counted live rather than described in prose, so the number the user agrees
    to is the number they get. Everything not listed here is not copied.
    """
    # Imported here rather than at module scope to keep this module's import
    # graph flat — custom_fields pulls in import_helpers, which pulls in more.
    from services.custom_fields import is_custom_field

    fields = await conn.fetchval(
        f"SELECT count(*) FROM {_rel(source_schema, 'fieldmap')}"
    )
    columns = await _columns(conn, source_schema, "temp_trans")
    custom = sum(1 for name in columns if is_custom_field(name))

    counts: dict[str, int] = {}
    for table in COPIED_TABLES:
        counts[table] = int(
            await conn.fetchval(f"SELECT count(*) FROM {_rel(source_schema, table)}") or 0
        )

    # One entry per thing the user can tick, in the order it would be applied.
    # The register screen builds its checkbox list from this rather than from a
    # list of its own, so a table added to COPIED_TABLES appears there with no
    # frontend edit — and a company with none of something shows a zero instead
    # of the option quietly vanishing.
    part_counts = {FIELDS_PART: int(fields or 0), **counts}

    return {
        "schema_name": source_schema,
        "fields": int(fields or 0),
        "custom_columns": custom,
        "projects": counts.get("projects", 0),
        "masters": sum(v for k, v in counts.items() if k != "projects"),
        "tables": counts,
        "parts": [{"key": k, "count": part_counts.get(k, 0)} for k in CLONE_PARTS],
    }
