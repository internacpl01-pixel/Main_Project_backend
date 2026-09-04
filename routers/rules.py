"""
The rule grid: which heads are valid for which account type, in which direction.

One screen backs this router — a table with one row per head and one column per
account type, each cell holding CR, DR or nothing. That grid is the whole rule
now: Check Rules reads it to decide which staged rows are wrong AND which heads
its Replace dropdown offers, so the two can never disagree.

Both axes are read live and neither is written down anywhere:

  rows    -> one of the three head masters, chosen per request
  columns -> account_type_master, the same list the Bank master types accounts
             from

so adding a head or an account type on the Master Data page changes this grid
with no migration and no code change here.

Since 034 there are three grids, not one — a staged row carries an Internal
Head, a RERA Head and a TCP Head, each written by its own dropdown into its own
column, and each has its own rules. `target` names which one a request means.
It is one of three keys of `rules.TARGETS`, never a table name off the wire.

Conditions sit under the grid: "a RERA debit whose narration mentions REFUND is
a Cust Cancellation". Same two axes plus the head type, one test on one column
of the statement, and the heads that are the answer when it passes.

See company/028_rule_table.sql, company/030_rule_condition.sql,
company/032_rule_single_master.sql and company/034_rule_all_heads.sql.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query

import permissions
from database import company_connection
from routers import master
from routers.auth import get_company_user, require_level
from services import custom_fields, rules, scoping, staging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rules", tags=["rules"])

# Reading the grid is looking; writing it changes what every future check
# reports. Same split the Master Data tabs use, and the same level — a manager
# who may edit the heads themselves may say how they are used.
require_manager = require_level(permissions.MANAGER)

# What a cell may hold, and the same two the check judges by — one list, so the
# grid cannot offer an answer the check would not recognise. '' is not one of
# them: clearing a cell deletes its row, because "no rule" and "a rule saying
# nothing" are the same state and storing both would let them drift.
_DIRECTIONS = rules.DIRECTIONS


def _target(target: str | None) -> dict:
    """One head type, as a 400 rather than a 500 when it is not one.

    Every table and column name this module puts into a statement comes from
    here, and everything here comes from rules.TARGETS. A request carries the
    KEY only — 'head', 'rera_head', 'idw_head' — so no identifier off the wire
    can reach SQL even in principle.
    """
    try:
        return rules.target_def(target)
    except rules.UnknownTarget as e:
        raise HTTPException(400, str(e))


def _target_label(t: dict) -> str:
    """What Master Data calls this master.

    Read from routers.master rather than kept beside rules.TARGETS, because that
    is the screen where it is edited: 'idw_head' has been shown as "TCP Head"
    since a rename that deliberately touched nothing but the label, and a second
    copy here would still say IDW.
    """
    return master.TABLE_LABELS.get(t["master_table"], "Head")


async def _used_targets(conn) -> set[str]:
    """The head types this company's fieldmap actually mirrors.

    A head only reaches a staged row through a fieldmap row that mirrors its
    master, so a company that mirrors none of them — company_007 mirrors nothing
    at all — has nowhere to put a rule's answer. Offering it a grid to fill in
    would be offering a rule that can be written and never applied.

    Reported rather than enforced: an unused grid is shown greyed with the
    reason, so the fix (map the column on Field Mapping) is discoverable from
    the page that needs it.
    """
    rows = await conn.fetch(
        "SELECT DISTINCT mirrors FROM fieldmap "
        "WHERE mirrors = ANY($1::text[]) AND is_active = true",
        [t["mirrors"] for t in rules.TARGETS.values()],
    )
    mirrored = {r["mirrors"] for r in rows}
    return {key for key, t in rules.TARGETS.items() if t["mirrors"] in mirrored}


def _target_list(used: set[str], chosen: str) -> list[dict]:
    """Every head type the page can switch between, in TARGETS order."""
    return [
        {"target": key,
         "label": master.TABLE_LABELS.get(t["master_table"], "Head"),
         "used": key in used,
         "selected": key == chosen}
        for key, t in rules.TARGETS.items()
    ]


@router.get("/targets")
async def rule_targets(user: dict = Depends(get_company_user)):
    """The three head types, labelled, and whether this company can use each.

    Its own endpoint because the Check Rules dialog needs the list and nothing
    else from /matrix — which carries up to 97 heads and every cell of a grid
    the dialog does not draw. Three rows and a boolean should not cost that.
    """
    async with company_connection(user["schema"]) as conn:
        used = await _used_targets(conn)
    return {"targets": _target_list(used, rules.DEFAULT_TARGET),
            "default": rules.DEFAULT_TARGET}


@router.get("/matrix")
async def rule_matrix(
    target: str = Query(rules.DEFAULT_TARGET,
                        description="Which head master's grid: head, rera_head "
                                    "or idw_head."),
    user: dict = Depends(get_company_user),
):
    """One head type's grid: its rows, its columns, and every cell that is filled.

    Sent as one response rather than three requests, because the three are only
    meaningful together — a cell keyed by a head id the caller has not been
    given is not renderable.

    One head type per request. The three masters have 97, 22 and 17 rows here;
    a single grid of all of them would be 136 rows deep, and two heads with the
    same name in different masters would be two rows nothing on screen could
    tell apart. `targets` comes back alongside so the page can offer the switch
    without a second call.

    Inactive heads and inactive account types are left out. They cannot be
    picked in Master Data either, and offering a rule about a head nobody can
    choose would be a rule that reads as broken the moment anyone runs it.
    """
    t = _target(target)
    async with company_connection(user["schema"]) as conn:
        heads = await conn.fetch(
            f"SELECT id, name FROM {t['master_table']} "
            f"WHERE is_active = true ORDER BY name, id"
        )
        types = await conn.fetch(
            "SELECT upper(btrim(name)) AS name FROM account_type_master "
            "WHERE is_active = true AND btrim(name) <> '' "
            "GROUP BY 1 ORDER BY 1"
        )
        cells = await conn.fetch(
            f'SELECT "{t["column"]}" AS head_id, '
            f"       upper(btrim(account_type)) AS account_type, direction "
            f"  FROM rule WHERE target = $1",
            t["target"],
        )
        used = await _used_targets(conn)

    # Keyed by head then type, which is how the grid is drawn — the browser
    # should not have to index a flat list to paint a cell.
    by_head: dict[str, dict[str, str]] = {}
    for c in cells:
        by_head.setdefault(str(c["head_id"]), {})[c["account_type"]] = c["direction"]

    return {
        "heads": [{"id": h["id"], "name": h["name"]} for h in heads],
        "account_types": [t["name"] for t in types],
        "directions": list(_DIRECTIONS),
        "cells": by_head,
        # What this grid is for, and what the other two are, so the page can
        # label the switch without knowing the three names itself.
        "target": {"target": t["target"], "label": _target_label(t),
                   "master_table": t["master_table"],
                   "used": t["target"] in used},
        "targets": _target_list(used, t["target"]),
    }


@router.put("/cell", dependencies=[Depends(require_manager)])
async def set_rule_cell(
    head_id: int = Body(..., description="A row in the target's master table."),
    account_type: str = Body(..., description="An active account type."),
    direction: str | None = Body(
        None, description="CR or DR — or null to clear the cell."),
    target: str = Body(rules.DEFAULT_TARGET,
                       description="Which head master head_id belongs to: "
                                   "head, rera_head or idw_head."),
    user: dict = Depends(get_company_user),
):
    """Set or clear one cell of the grid.

    One cell per request, because that is how the grid is edited: a dropdown
    changes and the change is saved. Sending the whole grid back would make a
    stale tab able to undo somebody else's edit to a cell it never touched.

    The head, the account type and the head type are all checked against the
    live masters rather than trusted. account_type in particular is stored as
    text to match bank_master, so nothing but this check stops a typo becoming a
    rule that can never fire — an account type the Bank master will never
    produce. The head is looked up in the target's OWN master, so an id that
    exists in a different one is refused here rather than stored as a rule
    about a head this grid does not contain.
    """
    t = _target(target)
    label = _target_label(t)

    wanted_type = (account_type or "").strip().upper()
    if not wanted_type:
        raise HTTPException(400, "An account type is required.")

    if direction is not None:
        direction = direction.strip().upper()
        if direction not in _DIRECTIONS:
            raise HTTPException(
                400, f"Direction must be {' or '.join(_DIRECTIONS)}, "
                     f"or empty to clear the cell.")

    async with company_connection(user["schema"]) as conn:
        head = await conn.fetchrow(
            f"SELECT id, name, is_active FROM {t['master_table']} "
            f"WHERE id = $1", head_id,
        )
        if head is None:
            raise HTTPException(
                400, f"There is no {label} with id {head_id}.")
        if not head["is_active"]:
            raise HTTPException(
                400, f"'{head['name']}' is switched off in Master Data, so it "
                     f"cannot be given a rule. Reactivate it first.")

        known = await conn.fetchval(
            "SELECT 1 FROM account_type_master "
            "WHERE upper(btrim(name)) = $1 AND is_active = true",
            wanted_type,
        )
        if not known:
            raise HTTPException(
                400, f"'{wanted_type}' is not an active account type. Add it "
                     f"under Master Data → Type of Account first.")

        column = t["column"]
        if direction is None:
            await conn.execute(
                f'DELETE FROM rule WHERE "{column}" = $1 '
                f"AND upper(btrim(account_type)) = $2",
                head_id, wanted_type,
            )
        else:
            # ON CONFLICT names this target's own unique index. The other two
            # cannot collide: their columns are null on this row, and a unique
            # index does not constrain rows whose indexed column is null.
            await conn.execute(
                f"""
                INSERT INTO rule ("{column}", account_type, direction)
                VALUES ($1, $2, $3)
                ON CONFLICT ("{column}", account_type)
                DO UPDATE SET direction = EXCLUDED.direction, updated_at = now()
                """,
                head_id, wanted_type, direction,
            )

    logger.info("[rules] %s: %s %s / %s -> %s by %s", user["schema"], label,
                head["name"], wanted_type, direction or "cleared",
                user.get("username"))
    return {"status": "saved", "head_id": head_id, "target": t["target"],
            "account_type": wanted_type, "direction": direction}


# =============================================================================
# Conditions — the exception to the grid. See company/030_rule_condition.sql.
# =============================================================================

async def _subject_columns(conn) -> dict[str, dict]:
    """The columns a condition may test, keyed by name.

    custom_fields.data_columns is the same list the staging table draws its own
    headers from, so a field added on the Field Mapping page is testable under
    the name that company gave it and nothing here has to know it exists. Its
    `type` is the real Postgres type, which is what decides the operators.
    """
    return {
        c["name"]: {"name": c["name"],
                    "label": c["displayname"],
                    "kind": rules.column_kind(c["type"])}
        for c in await custom_fields.data_columns(conn)
    }


async def _fetch_conditions(conn, t: dict, where: str = "",
                            *params) -> list[dict]:
    """One head type's conditions, active or not, with heads and labels.

    Deliberately not services.rules.load_conditions: that one is the check's
    reader and it drops the inactive and refuses the broken, which is right for
    running a rule and wrong for a screen whose job is to show you the broken
    one so you can fix it.

    `where` is always ANDed onto the target filter rather than replacing it, so
    no caller can accidentally list another master's conditions beside heads
    read from this one — which would render every head as blank.
    """
    clause = f"WHERE c.target = ${len(params) + 1}"
    if where:
        clause += f" AND ({where})"
    rows = await conn.fetch(
        f"""
        SELECT c.*,
               coalesce(json_agg(json_build_object(
                            'id', h.id, 'name', h.name,
                            'is_active', h.is_active)
                        ORDER BY ch.sort_order, h.name, h.id)
                        FILTER (WHERE h.id IS NOT NULL), '[]'::json) AS heads
          FROM rule_condition c
          LEFT JOIN rule_condition_head ch ON ch.condition_id = c.id
          LEFT JOIN {t['master_table']} h ON h.id = ch."{t['column']}"
        {clause}
         GROUP BY c.id
         ORDER BY c.account_type, c.direction, c.sort_order, c.id
        """,
        *params, t["target"],
    )
    columns = await _subject_columns(conn)

    out = []
    for r in rows:
        c = dict(r)
        c["heads"] = json.loads(c["heads"])
        col = columns.get(c["subject_field"])
        c["subject_label"] = col["label"] if col else c["subject_field"]
        # The same three refusals load_conditions makes, said here as a note on
        # the row instead of as an error — this screen is where they get fixed,
        # and a condition you cannot see is a condition you cannot repair.
        live = [h for h in c["heads"] if h["is_active"]]
        if c["operator"] not in rules.OPERATORS:
            c["problem"] = (f"This uses a test ({c['operator']}) that no longer "
                            f"exists. Set it again.")
        elif not live:
            c["problem"] = ("Every head this points at has been deleted or "
                            "switched off in Master Data.")
        elif col is None:
            c["problem"] = (f"The column it tests ({c['subject_field']}) is no "
                            f"longer on the imported rows.")
        else:
            c["problem"] = None
        c["sentence"] = rules.describe(
            {**c, "heads": live or c["heads"]}, c["subject_label"])
        out.append(c)
    return out


@router.get("/conditions")
async def list_conditions(
    target: str = Query(rules.DEFAULT_TARGET,
                        description="Which head master's conditions."),
    user: dict = Depends(get_company_user),
):
    """One head type's conditions, plus everything the builder's dropdowns need.

    One response rather than five, for the same reason /matrix is one: the
    parts are only meaningful together. A condition rendered without the column
    list cannot say which column it tests in the company's own words, and a
    builder rendered without the operator list would have to carry its own copy
    of what the check implements — which is exactly the drift this feature
    exists to remove.

    Filtered to one head type, and its `heads` come from that same master, so
    the list and the dropdown that edits it can never be about different tables.
    """
    t = _target(target)
    async with company_connection(user["schema"]) as conn:
        conditions = await _fetch_conditions(conn, t)
        columns = await _subject_columns(conn)
        heads = await conn.fetch(
            f"SELECT id, name FROM {t['master_table']} "
            f"WHERE is_active = true ORDER BY name, id"
        )
        types = await conn.fetch(
            "SELECT upper(btrim(name)) AS name FROM account_type_master "
            "WHERE is_active = true AND btrim(name) <> '' "
            "GROUP BY 1 ORDER BY 1"
        )
        used = await _used_targets(conn)

    return {
        "conditions": conditions,
        "columns": list(columns.values()),
        "operators": rules.operator_catalog(),
        "heads": [{"id": h["id"], "name": h["name"]} for h in heads],
        "account_types": [t["name"] for t in types],
        "directions": list(_DIRECTIONS),
        "target": {"target": t["target"], "label": _target_label(t),
                   "master_table": t["master_table"],
                   "used": t["target"] in used},
        "targets": _target_list(used, t["target"]),
    }


async def _clean(conn, t, account_type, direction, subject_field, operator,
                 value1, value2, head_ids, require_heads: bool = True) -> dict:
    """Check a condition against the live masters and columns, or 400 saying why.

    Everything a condition names is checked here rather than trusted, because
    every one of them is a name that can stop existing: an account type can be
    deactivated, a field deleted from the Field Mapping page, a head switched
    off in Master Data. A condition that stored one of those anyway would be a
    sentence that reads fine on this screen and refuses to run on the next one.

    The operator is checked against the column's KIND too — "more than" on a
    narration is not a rule anyone can satisfy, and offering it and then storing
    it would be this file agreeing to something rules.py cannot answer.
    """
    wanted_type = (account_type or "").strip().upper()
    if not wanted_type:
        raise HTTPException(400, "An account type is required.")
    known = await conn.fetchval(
        "SELECT 1 FROM account_type_master "
        "WHERE upper(btrim(name)) = $1 AND is_active = true", wanted_type)
    if not known:
        raise HTTPException(
            400, f"'{wanted_type}' is not an active account type. Add it under "
                 f"Master Data → Type of Account first.")

    wanted_dir = (direction or "").strip().upper()
    if wanted_dir not in _DIRECTIONS:
        raise HTTPException(
            400, f"Direction must be {' or '.join(_DIRECTIONS)}.")

    columns = await _subject_columns(conn)
    field = (subject_field or "").strip()
    col = columns.get(field)
    if col is None:
        raise HTTPException(
            400, f"'{field or '(nothing)'}' is not a column on the imported "
                 f"rows. Pick one of the fields shown on Imported Rows.")

    op = rules.OPERATORS.get((operator or "").strip())
    if op is None:
        raise HTTPException(400, f"'{operator}' is not a test this can make.")
    if col["kind"] not in op["kinds"]:
        raise HTTPException(
            400, f"'{op['label']}' cannot be asked of {col['label']}, which "
                 f"holds {col['kind']} values.")

    v1 = (value1 or "").strip() or None
    v2 = (value2 or "").strip() or None
    if op["values"] >= 1 and v1 is None:
        raise HTTPException(400, f"'{op['label']}' needs a value to compare to.")
    if op["values"] == 2 and v2 is None:
        raise HTTPException(400, f"'{op['label']}' needs both values.")
    if op["values"] == 0:
        # Kept out rather than kept around: a value stored beside an operator
        # that ignores it is a value the sentence on screen would not mention
        # and nobody could explain later.
        v1 = v2 = None
    elif op["values"] == 1:
        v2 = None

    # require_heads=False is the preview, which asks only the IF half: what the
    # answer should be is still the user's decision at that point, and demanding
    # one before they can see how many rows the test describes gets the two
    # halves the wrong way round.
    ids: list[int] = []
    if require_heads:
        for raw in head_ids or []:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                raise HTTPException(400, "Each head must be an id.")
        # Order matters: the first is what Replace preselects, the same standing
        # the grid's first head has. dict.fromkeys de-duplicates without losing it.
        ids = list(dict.fromkeys(ids))
        if not ids:
            raise HTTPException(
                400, "A condition needs at least one head to point at — it is "
                     "the answer when the test passes.")

        # Looked up in THIS target's master, so a head that exists in another
        # one is "no such head" here rather than a condition stored against a
        # master it does not belong to. The database refuses that too — the
        # composite key 034 put on rule_condition_head — but a 400 naming the
        # head is a better answer than a constraint violation.
        found = {r["id"]: r for r in await conn.fetch(
            f"SELECT id, name, is_active FROM {t['master_table']} "
            f"WHERE id = ANY($1::bigint[])", ids)}
        for head_id in ids:
            row = found.get(head_id)
            if row is None:
                raise HTTPException(
                    400, f"There is no {_target_label(t)} with id {head_id}.")
            if not row["is_active"]:
                raise HTTPException(
                    400, f"'{row['name']}' is switched off in Master Data, so a "
                         f"condition cannot point at it. Reactivate it first.")

    return {"account_type": wanted_type, "direction": wanted_dir,
            "subject_field": field, "operator": (operator or "").strip(),
            "value1": v1, "value2": v2, "head_ids": ids,
            "target": t["target"], "column": col, "operator_def": op}


async def _write_heads(conn, t: dict, condition_id: int,
                       head_ids: list[int]) -> None:
    """Replace a condition's heads with exactly this list, in this order.

    Written into the target's own column. If this ever wrote the wrong one the
    INSERT fails outright: rule_condition_head derives its own `target` from
    which column was filled and carries a foreign key on (condition_id, target)
    back to the condition, so a head from the wrong master has nowhere to land.
    """
    await conn.execute(
        "DELETE FROM rule_condition_head WHERE condition_id = $1", condition_id)
    for i, head_id in enumerate(head_ids):
        await conn.execute(
            f'INSERT INTO rule_condition_head (condition_id, "{t["column"]}", '
            f"sort_order) VALUES ($1, $2, $3)", condition_id, head_id, i)


@router.post("/conditions", dependencies=[Depends(require_manager)])
async def create_condition(
    account_type: str = Body(..., description="An active account type."),
    direction: str = Body(..., description="CR or DR."),
    subject_field: str = Body(..., description="A column of the imported rows."),
    operator: str = Body(..., description="One of the tests from /rules/conditions."),
    value1: str | None = Body(None),
    value2: str | None = Body(None, description="Only 'between' uses this."),
    head_ids: list[int] = Body(..., description="The answer when the test "
                                                "passes; the first is what "
                                                "Replace preselects."),
    target: str = Body(rules.DEFAULT_TARGET,
                       description="Which head master head_ids belong to."),
    is_active: bool = Body(True),
    user: dict = Depends(get_company_user),
):
    """Write one condition. It lands last in its group, so it decides last.

    Appended rather than inserted anywhere clever: a new sentence must not
    change what the sentences already there do to rows the user has looked at,
    and moving it up is one click on the list.

    Its position is counted within its head type as well as its type and
    direction: three grids' conditions do not compete with each other, and
    numbering them together would leave gaps nobody could explain.
    """
    t = _target(target)
    async with company_connection(user["schema"]) as conn:
        clean = await _clean(conn, t, account_type, direction, subject_field,
                             operator, value1, value2, head_ids)
        nxt = await conn.fetchval(
            "SELECT coalesce(max(sort_order), -1) + 1 FROM rule_condition "
            "WHERE account_type = $1 AND direction = $2 AND target = $3",
            clean["account_type"], clean["direction"], t["target"])
        new_id = await conn.fetchval(
            """
            INSERT INTO rule_condition (account_type, direction, subject_field,
                                        operator, value1, value2, sort_order,
                                        is_active, target)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            clean["account_type"], clean["direction"], clean["subject_field"],
            clean["operator"], clean["value1"], clean["value2"], nxt,
            bool(is_active), t["target"],
        )
        await _write_heads(conn, t, new_id, clean["head_ids"])
        saved = await _fetch_conditions(conn, t, "c.id = $1", new_id)

    logger.info("[rules] %s: condition %s added by %s", user["schema"], new_id,
                user.get("username"))
    return saved[0]


@router.put("/conditions/{condition_id}", dependencies=[Depends(require_manager)])
async def update_condition(
    condition_id: int,
    account_type: str = Body(...),
    direction: str = Body(...),
    subject_field: str = Body(...),
    operator: str = Body(...),
    value1: str | None = Body(None),
    value2: str | None = Body(None),
    head_ids: list[int] = Body(...),
    target: str = Body(rules.DEFAULT_TARGET),
    is_active: bool = Body(True),
    user: dict = Depends(get_company_user),
):
    """Rewrite one condition, heads and all.

    The whole sentence at once, because that is how it is edited — the builder
    opens with every part filled in and saves what is on screen. Its position
    is not touched: changing what a condition says should not change when it is
    asked.

    Moving it to another account type, direction or head type resets its
    position to last in the group it arrives in, since its old number means
    nothing there. The head type moves with the heads: they are rewritten in the
    same statement, and the database's composite key would refuse them if the
    two disagreed.
    """
    t = _target(target)
    async with company_connection(user["schema"]) as conn:
        # Existence first: a condition somebody else deleted while this dialog
        # was open should say so, not report the first thing wrong with a form
        # that has nowhere to be saved.
        current = await conn.fetchrow(
            "SELECT account_type, direction, target FROM rule_condition "
            "WHERE id = $1", condition_id)
        if current is None:
            raise HTTPException(404, "That condition no longer exists.")

        clean = await _clean(conn, t, account_type, direction, subject_field,
                             operator, value1, value2, head_ids)
        moved = (current["account_type"] != clean["account_type"]
                 or current["direction"] != clean["direction"]
                 or current["target"] != t["target"])
        order = await conn.fetchval(
            "SELECT coalesce(max(sort_order), -1) + 1 FROM rule_condition "
            "WHERE account_type = $1 AND direction = $2 AND target = $3 "
            "AND id <> $4",
            clean["account_type"], clean["direction"], t["target"], condition_id,
        ) if moved else None

        # The old heads go first when the master changes. They belong to the
        # target the condition is leaving, and the composite key would refuse
        # the UPDATE while they were still attached to it.
        if current["target"] != t["target"]:
            await conn.execute(
                "DELETE FROM rule_condition_head WHERE condition_id = $1",
                condition_id)

        await conn.execute(
            """
            UPDATE rule_condition
               SET account_type = $2, direction = $3, subject_field = $4,
                   operator = $5, value1 = $6, value2 = $7, is_active = $8,
                   sort_order = coalesce($9, sort_order), target = $10,
                   updated_at = now()
             WHERE id = $1
            """,
            condition_id, clean["account_type"], clean["direction"],
            clean["subject_field"], clean["operator"], clean["value1"],
            clean["value2"], bool(is_active), order, t["target"],
        )
        await _write_heads(conn, t, condition_id, clean["head_ids"])
        saved = await _fetch_conditions(conn, t, "c.id = $1", condition_id)

    logger.info("[rules] %s: condition %s edited by %s", user["schema"],
                condition_id, user.get("username"))
    return saved[0]


@router.delete("/conditions/{condition_id}",
               dependencies=[Depends(require_manager)])
async def delete_condition(condition_id: int,
                           user: dict = Depends(get_company_user)):
    """Remove a condition. Its heads go with it, by CASCADE.

    Deleted rather than deactivated, because the list already has a switch for
    "not now" and a row that is neither on nor deletable is a row nobody can
    tidy up.
    """
    async with company_connection(user["schema"]) as conn:
        gone = await conn.fetchval(
            "DELETE FROM rule_condition WHERE id = $1 RETURNING id", condition_id)
    if gone is None:
        raise HTTPException(404, "That condition no longer exists.")
    logger.info("[rules] %s: condition %s deleted by %s", user["schema"],
                condition_id, user.get("username"))
    return {"status": "deleted", "id": gone}


@router.post("/conditions/reorder", dependencies=[Depends(require_manager)])
async def reorder_conditions(
    account_type: str = Body(...),
    direction: str = Body(...),
    ids: list[int] = Body(..., description="Every condition in that group, in "
                                           "the order they should decide."),
    target: str = Body(rules.DEFAULT_TARGET),
    user: dict = Depends(get_company_user),
):
    """Set the order in which one group's conditions are asked.

    The whole group is sent, not "move this one up", so the result cannot
    depend on what the browser thought the old order was. Ids that are not in
    that group are ignored rather than rejected: a stale tab reordering a list
    that has since lost a row should still be able to reorder the rest.

    A group is one head type, one account type and one direction — the only
    combination in which two conditions ever compete for the same row.
    """
    t = _target(target)
    wanted_type = (account_type or "").strip().upper()
    wanted_dir = (direction or "").strip().upper()
    if wanted_dir not in _DIRECTIONS:
        raise HTTPException(400, f"Direction must be {' or '.join(_DIRECTIONS)}.")

    async with company_connection(user["schema"]) as conn:
        live = {r["id"] for r in await conn.fetch(
            "SELECT id FROM rule_condition "
            "WHERE account_type = $1 AND direction = $2 AND target = $3",
            wanted_type, wanted_dir, t["target"])}
        ordered = [i for i in dict.fromkeys(ids or []) if i in live]
        # Whatever the caller did not mention keeps its relative order, after
        # the ones it did — so a partial list cannot silently move a row the
        # user never touched to the front.
        rest = sorted(live - set(ordered))
        for position, cid in enumerate(ordered + rest):
            await conn.execute(
                "UPDATE rule_condition SET sort_order = $2, updated_at = now() "
                "WHERE id = $1", cid, position)
        saved = await _fetch_conditions(
            conn, t, "c.account_type = $1 AND c.direction = $2",
            wanted_type, wanted_dir)
    return {"status": "reordered", "conditions": saved}


@router.post("/conditions/preview")
async def preview_condition(
    account_type: str = Body(...),
    direction: str = Body(...),
    subject_field: str = Body(...),
    operator: str = Body(...),
    value1: str | None = Body(None),
    value2: str | None = Body(None),
    target: str = Body(rules.DEFAULT_TARGET),
    user: dict = Depends(get_company_user),
):
    """Run an unsaved test against the rows that are actually staged.

    The one thing that stops somebody saving a sentence they have misread. It
    answers the only question that matters before saving — "how many of my rows
    does this describe, and are they the ones I mean?" — by running the real
    matcher over the real rows and handing back a few of them to look at.

    Reads only, and takes no heads: what the answer should be is the user's
    decision, and this is about the IF half. Scoped like every other read, so a
    manager who cannot see a project's rows cannot count them here either.
    """
    t = _target(target)
    async with company_connection(user["schema"]) as conn:
        clean = await _clean(conn, t, account_type, direction, subject_field,
                             operator, value1, value2, None, require_heads=False)

        account_col = await staging.account_column(conn)
        if not account_col:
            raise HTTPException(
                400, "This company has no account number field mapped, so rows "
                     "cannot be matched to an account. Map one on the Field "
                     "Mapping page first.")

        filters = [
            # Only rows printed under an account the Bank master gives this
            # type — the same population the check would judge, so the number
            # this reports is the number that would be judged.
            f"{staging.account_digits(f't.{account_col}')} IN ("
            f"  SELECT {staging.account_digits('b.account_number')}"
            f"    FROM bank_master b"
            f"   WHERE upper(btrim(coalesce(b.account_type, ''))) = $1"
            f"     AND {staging.account_digits('b.account_number')} <> '')",
            "upper(btrim(coalesce(t.credit_debit, ''))) = $2",
        ]
        params: list = [clean["account_type"], clean["direction"]]
        idx = 3

        scope = await scoping.visible_project_ids(conn, user)
        clause, scope_params, idx = scoping.project_filter(
            scope, "t.project_id", idx)
        if clause:
            filters.append(clause)
            params.extend(scope_params)
        elif scoping.scope_is_empty(scope):
            filters.append("t.project_id IS NULL")

        fields = [clean["subject_field"]]
        # The head this condition would decide, and only that one. Before 034
        # this had to join all three masters and coalesce, because a condition
        # did not say which head it was about; now it does, and showing the
        # Internal Head beside a RERA condition would be showing the column the
        # user is not editing.
        rows = await conn.fetch(
            f"""
            SELECT t.id, t.amount, m.name AS current_name
                   {rules.subject_sql(fields)}
              FROM temp_trans t
             LEFT JOIN {t['master_table']} m ON m.id = t.{t['field']}
             WHERE {' AND '.join(filters)}
             ORDER BY t.batch_id, t.row_number
            """,
            *params,
        )

    draft = {"direction": clean["direction"], "operator": clean["operator"],
             "subject_field": clean["subject_field"],
             "value1": clean["value1"], "value2": clean["value2"]}
    matched = []
    for r in rows:
        if rules.match(draft, rules.subject_values(r, fields)):
            matched.append(r)

    return {
        "scanned": len(rows),
        "matched": len(matched),
        "phrase": rules.phrase(draft, clean["column"]["label"]),
        "examples": [
            {"id": r["id"], "amount": r["amount"],
             "value": None if r["s0"] is None else str(r["s0"]),
             "current_name": r["current_name"]}
            for r in matched[:5]
        ],
    }


@router.get("/summary")
async def rule_summary(
    target: str = Query(rules.DEFAULT_TARGET,
                        description="Which head master to count."),
    user: dict = Depends(get_company_user),
):
    """How many heads each account type accepts, per direction.

    What the Rules page shows above the grid and what the Check Rules dialog
    uses to say whether a type has a rule at all — so a user picking a type with
    no rule learns it before running a check rather than from a 400 afterwards.

    Conditions are counted alongside the grid cells, because a type judged
    entirely by conditions does have a rule — reporting it as having none would
    send someone to fill in a grid column they deliberately left blank.

    Counted for one head type. Rolling all three together would tell someone
    about to run an Internal Head check that MASTER has four rules, when all
    four are RERA ones and the run would find nothing it could use.
    """
    t = _target(target)
    async with company_connection(user["schema"]) as conn:
        rows = await conn.fetch(
            f"""
            SELECT upper(btrim(r.account_type)) AS account_type,
                   count(*) FILTER (WHERE r.direction = 'CR') AS cr,
                   count(*) FILTER (WHERE r.direction = 'DR') AS dr,
                   count(*)                                   AS total
              FROM rule r
              JOIN {t['master_table']} h ON h.id = r."{t['column']}"
             WHERE r.target = $1 AND h.is_active = true
             GROUP BY 1
             ORDER BY 1
            """,
            t["target"],
        )
        conds = await conn.fetch(
            """
            SELECT upper(btrim(account_type)) AS account_type, count(*) AS n
              FROM rule_condition
             WHERE is_active = true AND target = $1
             GROUP BY 1
            """,
            t["target"],
        )

    out = {r["account_type"]: {"cr": r["cr"], "dr": r["dr"],
                               "total": r["total"], "conditions": 0}
           for r in rows}
    for c in conds:
        out.setdefault(c["account_type"],
                       {"cr": 0, "dr": 0, "total": 0, "conditions": 0})
        out[c["account_type"]]["conditions"] = c["n"]
    return dict(sorted(out.items()))
