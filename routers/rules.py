"""
The rule grid: which heads are valid for which account type, in which direction.

One screen backs this router — a table with one row per head and one column per
account type, each cell holding CR, DR or nothing. That grid is the whole rule
now: Check Rules reads it to decide which staged rows are wrong AND which heads
its Replace dropdown offers, so the two can never disagree.

Both axes are read live and neither is written down anywhere:

  rows    -> rera_head_master, the master Check Rules writes its answer into
  columns -> account_type_master, the same list the Bank master types accounts
             from

so adding a head or an account type on the Master Data page changes this grid
with no migration and no code change here. See company/028_rule_table.sql.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException

import permissions
from database import company_connection
from routers.auth import get_company_user, require_level
from services import rules

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


@router.get("/matrix")
async def rule_matrix(user: dict = Depends(get_company_user)):
    """The whole grid: its rows, its columns, and every cell that is filled.

    Sent as one response rather than three requests, because the three are only
    meaningful together — a cell keyed by a head id the caller has not been
    given is not renderable.

    Inactive heads and inactive account types are left out. They cannot be
    picked in Master Data either, and offering a rule about a head nobody can
    choose would be a rule that reads as broken the moment anyone runs it.
    """
    async with company_connection(user["schema"]) as conn:
        heads = await conn.fetch(
            f"SELECT id, name FROM {rules.TARGET['master_table']} "
            f"WHERE is_active = true ORDER BY name, id"
        )
        types = await conn.fetch(
            "SELECT upper(btrim(name)) AS name FROM account_type_master "
            "WHERE is_active = true AND btrim(name) <> '' "
            "GROUP BY 1 ORDER BY 1"
        )
        cells = await conn.fetch(
            "SELECT head_id, upper(btrim(account_type)) AS account_type, direction "
            "FROM rule"
        )

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
        # What the grid is for, named by the same constants the check uses.
        "target": {"label": rules.TARGET["label"],
                   "master_table": rules.TARGET["master_table"]},
    }


@router.put("/cell", dependencies=[Depends(require_manager)])
async def set_rule_cell(
    head_id: int = Body(..., description="A row in the RERA Head master."),
    account_type: str = Body(..., description="An active account type."),
    direction: str | None = Body(
        None, description="CR or DR — or null to clear the cell."),
    user: dict = Depends(get_company_user),
):
    """Set or clear one cell of the grid.

    One cell per request, because that is how the grid is edited: a dropdown
    changes and the change is saved. Sending the whole grid back would make a
    stale tab able to undo somebody else's edit to a cell it never touched.

    Both the head and the account type are checked against the live masters
    rather than trusted. account_type in particular is stored as text to match
    bank_master, so nothing but this check stops a typo becoming a rule that can
    never fire — an account type the Bank master will never produce.
    """
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
            f"SELECT id, name, is_active FROM {rules.TARGET['master_table']} "
            f"WHERE id = $1", head_id,
        )
        if head is None:
            raise HTTPException(
                400, f"There is no {rules.TARGET['label']} with id {head_id}.")
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

        if direction is None:
            await conn.execute(
                "DELETE FROM rule WHERE head_id = $1 "
                "AND upper(btrim(account_type)) = $2",
                head_id, wanted_type,
            )
        else:
            await conn.execute(
                """
                INSERT INTO rule (head_id, account_type, direction)
                VALUES ($1, $2, $3)
                ON CONFLICT (head_id, account_type)
                DO UPDATE SET direction = EXCLUDED.direction, updated_at = now()
                """,
                head_id, wanted_type, direction,
            )

    logger.info("[rules] %s: %s / %s -> %s by %s", user["schema"], head["name"],
                wanted_type, direction or "cleared", user.get("username"))
    return {"status": "saved", "head_id": head_id,
            "account_type": wanted_type, "direction": direction}


@router.get("/summary")
async def rule_summary(user: dict = Depends(get_company_user)):
    """How many heads each account type accepts, per direction.

    What the Rules page shows above the grid and what the Check Rules dialog
    uses to say whether a type has a rule at all — so a user picking a type with
    no rule learns it before running a check rather than from a 400 afterwards.
    """
    async with company_connection(user["schema"]) as conn:
        rows = await conn.fetch(
            f"""
            SELECT upper(btrim(r.account_type)) AS account_type,
                   count(*) FILTER (WHERE r.direction = 'CR') AS cr,
                   count(*) FILTER (WHERE r.direction = 'DR') AS dr,
                   count(*)                                   AS total
              FROM rule r
              JOIN {rules.TARGET['master_table']} h ON h.id = r.head_id
             WHERE h.is_active = true
             GROUP BY 1
             ORDER BY 1
            """
        )
    return {r["account_type"]: {"cr": r["cr"], "dr": r["dr"], "total": r["total"]}
            for r in rows}
