"""
Fieldmap audit trail.

DPL's services/mappings.log_field_change, widened to record what changed and
who changed it. See migration 008 for why those two additions matter.

Scope is the fieldmap and only the fieldmap -- the table where an edit changes
how every future import is read. Classify and finalize are not logged here;
those write rows that carry their own author and timestamp, so they are already
traceable without a second copy.

Every function takes an open connection rather than a schema name. Logging must
land inside the caller's transaction: a fieldmap update that rolls back has to
take its log row with it, or the trail says a change happened that did not.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The fieldmap columns worth a history. `id` and the timestamps are excluded --
# they are not edits anyone makes, and logging them would bury the two lines
# that matter under noise on every save.
TRACKED_COLUMNS = ("fieldname", "displayname", "mapfields", "data_type",
                   "method", "is_active")

CREATED = "created"
UPDATED = "updated"
DELETED = "deleted"


def _text(value) -> str | None:
    """Render a fieldmap value for storage.

    None stays None so "was unset" and "was empty" stay distinguishable; is_active
    is a bool and has to be stringified or asyncpg rejects it against a text
    column.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def log(conn, *, fieldname: str, action: str, username: str,
              fieldmap_id: int | None = None, field_changed: str | None = None,
              old_value=None, new_value=None) -> None:
    """Write one audit row.

    Never raises. A failed insert here must not take down the edit it was
    describing -- losing a log line is bad, losing the user's change because
    logging it failed is worse. The failure is logged to the application log so
    it is not silent.
    """
    try:
        await conn.execute(
            """
            INSERT INTO fieldchange_log
                (fieldmap_id, fieldname, action, field_changed,
                 old_value, new_value, changed_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            fieldmap_id, fieldname, action, field_changed,
            _text(old_value), _text(new_value), username or "",
        )
    except Exception:
        logger.exception("[changelog] could not record %s on %s", action, fieldname)


async def log_diff(conn, *, before: dict, after: dict, username: str) -> int:
    """Log one row per column that actually changed. Returns how many.

    Comparing before against after rather than trusting the request body is what
    keeps the log honest: a PATCH that sets displayname to the value it already
    had is not a change, and recording it as one makes the history unreadable
    for the person scrolling it later.
    """
    written = 0
    for col in TRACKED_COLUMNS:
        if col not in before or col not in after:
            continue
        old, new = before[col], after[col]
        if old == new:
            continue
        await log(
            conn,
            fieldname=after.get("fieldname") or before.get("fieldname") or "",
            action=UPDATED,
            username=username,
            fieldmap_id=after.get("id") or before.get("id"),
            field_changed=col,
            old_value=old,
            new_value=new,
        )
        written += 1
    return written


async def log_created(conn, *, row: dict, username: str) -> None:
    """One row recording the create, with the starting aliases as new_value.

    mapfields is the field singled out because it is the one that decides
    whether the column ever fills. Seeing what it started as is what makes a
    later narrowing legible as a narrowing.
    """
    await log(
        conn,
        fieldname=row.get("fieldname") or "",
        action=CREATED,
        username=username,
        fieldmap_id=row.get("id"),
        field_changed="mapfields",
        new_value=row.get("mapfields"),
    )


async def log_deleted(conn, *, row: dict, username: str) -> None:
    """Record a delete, keeping the aliases that went with it.

    old_value is the point of this row. A deleted fieldmap row is gone from
    fieldmap entirely, so without the aliases stored here there is no way to
    reconstruct what the field used to match -- and "put it back the way it
    was" is the most likely reason anyone opens this page.
    """
    await log(
        conn,
        fieldname=row.get("fieldname") or "",
        action=DELETED,
        username=username,
        fieldmap_id=row.get("id"),
        field_changed="mapfields",
        old_value=row.get("mapfields"),
    )


async def fetch(conn, *, limit: int = 50, offset: int = 0,
                fieldname: str | None = None, action: str | None = None) -> dict:
    """A page of the log, newest first, with the unpaginated total.

    Paged from the start rather than added later. This table only grows -- there
    is no clear, no archive and no cascade that trims it -- so it is the one
    table in the schema guaranteed to outgrow a full read.
    """
    filters, params, idx = ["1=1"], [], 1
    if fieldname:
        filters.append(f"fieldname = ${idx}")
        params.append(fieldname)
        idx += 1
    if action:
        filters.append(f"action = ${idx}")
        params.append(action)
        idx += 1
    where = " AND ".join(filters)

    total = await conn.fetchval(
        f"SELECT count(*) FROM fieldchange_log WHERE {where}", *params
    )
    rows = await conn.fetch(
        f"""
        SELECT id, fieldmap_id, fieldname, action, field_changed,
               old_value, new_value, changed_by, changed_at
        FROM fieldchange_log
        WHERE {where}
        ORDER BY changed_at DESC, id DESC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, limit, offset,
    )
    return {
        "rows": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
