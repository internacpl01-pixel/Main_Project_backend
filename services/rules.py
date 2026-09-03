"""
Account-type rules for staged rows, read from the company's own `rule` table.

The Bank master types every account — MASTER, RERA, IDW, FREE, whatever rows
account_type_master holds — and the `rule` table says which heads a row printed
under such an account may legitimately carry, and in which direction. The Check
Rules button on Imported Rows picks one account and judges its staged rows
against the rows recorded for that account's type.

One row of `rule` is one fact: this head, on this account type, means money in
(CR) or money out (DR). A head with no row for a type is simply not an answer
there — which is how a blank cell on the Rules grid is stored, and why nothing
here has to carry a list of what is "not allowed".

This module used to hold the rule as a Python literal, with the heads named by
spelling and matched against the company's master rows after folding. None of
that is needed now: the rule points at head ids, so 'Master 2 RERA' versus
'Master to RERA' is a question the user answered once by picking a row from a
dropdown, not a question this code has to keep guessing at. Adding a rule for
MASTER, IDW or FREE is data entry on the Rules page, not an edit here.

See company/028_rule_table.sql.
"""
from __future__ import annotations

# What every rule judges and writes.
#
# Fixed, and honestly so: the heads on the Rules grid come from
# rera_head_master because rera_head_id is the column Check Rules puts its
# answer into. A head offered from any other master could be shown in the
# dropdown and then not saved, which is worse than not offering it. If a second
# master ever needs its own rules, this becomes a per-rule column rather than a
# constant — the rest of this module already treats it as data.
TARGET = {
    "field": "rera_head_id",
    "mirrors": "rera_head",
    "master_table": "rera_head_master",
    "label": "RERA head",
}

# The directions a row can be judged in, and — since 029 dropped BOTH — the only
# answers a cell of the grid can hold. A head means money in or money out; if it
# genuinely means either, that is two rules on two account types, not one cell
# saying nothing.
DIRECTIONS = ("CR", "DR")


class MissingRuleHeads(RuntimeError):
    """This account type has no rule, none its heads can satisfy, or one this
    code cannot read."""


async def allowed_heads(conn, account_type: str) -> dict[str, list[dict]]:
    """The heads each direction accepts for an account type.

    Returns {"CR": [{"id", "name"}, ...], "DR": [...]}, ordered by name so the
    dropdown reads the same on every run and the first entry — which is what a
    blanket fix applies — is stable across restarts.

    Only active heads count. A head switched off in Master Data is not an answer
    anyone should be offered, and leaving it in would let the fix write a value
    the row editor would not.

    Raises MissingRuleHeads when nothing is recorded, rather than returning two
    empty lists: "0 conflicts" from a rule that accepts nothing reads as a clean
    bill of health, and it is the opposite.
    """
    rows = await conn.fetch(
        f"""
        SELECT r.direction, h.id, h.name
          FROM rule r
          JOIN {TARGET['master_table']} h ON h.id = r.head_id
         WHERE upper(btrim(r.account_type)) = $1
           AND h.is_active = true
         ORDER BY h.name, h.id
        """,
        (account_type or "").strip().upper(),
    )

    out: dict[str, list[dict]] = {d: [] for d in DIRECTIONS}
    for r in rows:
        # The CHECK constraint on `rule` permits nothing but these, so anything
        # else means the table was written around the API. Said out loud rather
        # than skipped: a row quietly dropped here is a head that is no answer
        # in either direction, which on screen looks like a rule nobody wrote.
        if r["direction"] not in out:
            raise MissingRuleHeads(
                f"'{r['name']}' is stored against {account_type} with the "
                f"direction {r['direction']!r}, which is not CR or DR. Open the "
                f"Rules page and set that cell again.")
        out[r["direction"]].append({"id": r["id"], "name": r["name"]})

    if not any(out.values()):
        raise MissingRuleHeads(
            f"No rule is set for {(account_type or '').strip().upper() or 'untyped'} "
            f"accounts yet. Open the Rules page and mark which heads are valid "
            f"for this account type, and in which direction."
        )
    return out


async def supported_types(conn) -> list[str]:
    """The account types that have at least one usable rule row.

    Counts only rows whose head is still active, so a type whose every head was
    switched off is reported as having no rule — which is what running it would
    find anyway.
    """
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT upper(btrim(r.account_type)) AS account_type
          FROM rule r
          JOIN {TARGET['master_table']} h ON h.id = r.head_id
         WHERE h.is_active = true
         ORDER BY 1
        """
    )
    return [r["account_type"] for r in rows]


def explain(account_type: str, expected: dict[str, list[dict]]) -> dict[str, str]:
    """The sentence shown under each direction, built from the rule itself.

    Written from the data rather than stored beside it, so it can never drift
    from what the check actually does — the old hardcoded rule carried a
    sentence about IDW and cancellations that nothing verified.
    """
    kind = (account_type or "").strip().upper() or "this"
    words = {"CR": "coming into", "DR": "leaving"}
    out: dict[str, str] = {}
    for d in DIRECTIONS:
        heads = expected.get(d) or []
        if not heads:
            out[d] = (f"No head is marked {d} for {kind} accounts, so a "
                      f"{'credit' if d == 'CR' else 'debit'} here cannot be "
                      f"classified under this rule.")
            continue
        names = [h["name"] for h in heads]
        listed = names[0] if len(names) == 1 else (
            ", ".join(names[:-1]) + " or " + names[-1])
        out[d] = (f"Money {words[d]} a {kind} account is recorded as {listed}.")
    return out


def judge(direction: str | None, current_id: int | None,
          allowed_ids: dict[str, set[int]]) -> str:
    """What the rule says about one row: ok, conflict, or no_direction.

    A row with no CR/DR marker cannot be judged at all and is reported
    separately rather than counted against the rule — it is missing the one
    thing the rule is keyed on, not breaking it.

    A row whose head is NULL is a conflict: "must be one of these" is not
    satisfied by nothing.
    """
    if direction not in allowed_ids:
        return "no_direction"
    return "ok" if current_id in allowed_ids[direction] else "conflict"
