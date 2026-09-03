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

On top of that grid sit CONDITIONS (`rule_condition`): "a RERA debit whose
narration mentions REFUND is a Cust Cancellation". A condition names the same
account type and direction the grid speaks in, plus one further test on one
column of the statement, and the head or heads that are the answer when that
test passes. Conditions are read first and the first one that matches decides
the row on its own; a row no condition matches falls back to the grid.

Nothing a user types on the Rules page becomes SQL. The column named by a
condition is only ever used to read a key out of a row already fetched, and it
is checked against the live column list before it is stored; the value is
compared here, in Python; the operator is a lookup in OPERATORS below and an
unknown one makes the rule refuse to run rather than fall through.

See company/028_rule_table.sql, company/030_rule_condition.sql and
company/031_rule_multi_master.sql.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# =============================================================================
# Multi-master target resolution
# =============================================================================

# What every rule judges and writes.
#
# Most of this file used to assume one master only (rera_head_master), because
# rera_head_id was the only column Check Rules ever wrote.  That is no longer
# true: MASTER accounts carry head_id, RERA accounts carry rera_head_id, and
# IDW/TCP accounts carry idw_head_id.  The grid on the Rules page shows all
# three masters together, and a rule row names the master it came from by
# pointing at it.
#
# TARGET_BY_TYPE is the answer to "which master does this account type use?".
# It is keyed on the upper-cased account type, because that is how every
# comparison in this module and in the routers is written.  An account type
# not listed here — one added on the Master Data page that nobody has given a
# rule to yet — is treated as the RERA master for the grid display, because
# the grid must show something; writes and checks both validate the head
# against the live master, so a mismatch is rejected rather than stored.
TARGET_BY_TYPE: dict[str, dict] = {
    "MASTER": {
        "field": "head_id",
        "mirrors": "head",
        "master_table": "head_master",
        "label": "Head",
    },
    "RERA": {
        "field": "rera_head_id",
        "mirrors": "rera_head",
        "master_table": "rera_head_master",
        "label": "RERA head",
    },
    "IDW": {
        "field": "idw_head_id",
        "mirrors": "idw_head",
        "master_table": "idw_head_master",
        "label": "TCP head",
    },
    "TCP": {
        "field": "idw_head_id",
        "mirrors": "idw_head",
        "master_table": "idw_head_master",
        "label": "TCP head",
    },
    "FREE": {
        "field": "head_id",
        "mirrors": "head",
        "master_table": "head_master",
        "label": "Head",
    },
}

# The single target this process was built around.  Kept for routers that
# have not yet been updated to resolve per-account-type — it is the same
# object as TARGET_BY_TYPE["RERA"], so writing it here does not duplicate
# anything.
TARGET = TARGET_BY_TYPE["RERA"]


# The rule table that backs an account type.  Returns the literal table name
# from the live mapping; RERA accounts land on rule_rera_head, IDW on
# rule_idw_head, MASTER/FREE on rule_head.  An unknown account type falls back
# to rule_rera_head so that a brand-new account type the user has not yet
# configured still produces a usable grid.
def rule_table(account_type: str) -> str:
    """The rule_* table this account type stores its grid in.

    The grid on the Rules page reads from rule_v (the union view), so this
    table is only used by write paths — set_rule_cell and the apply endpoint.
    Reads continue to query rule_v directly.

    An account type that has no entry in TARGET_BY_TYPE falls back to RERA's
    table: the grid still renders, the heads are still validated against the
    master_table listed in TARGET_BY_TYPE[fallback], and writes still produce
    a row that can be looked up via the same view.
    """
    return _RULE_TABLES_BY_TYPE.get(
        (account_type or "").strip().upper(),
        "rule_rera_head",
    )


# Built once.  The whole mapping is determined by TARGET_BY_TYPE above; if a
# second master is added, both dicts grow together.
_RULE_TABLES_BY_TYPE = {
    "MASTER": "rule_head",
    "RERA":   "rule_rera_head",
    "IDW":    "rule_idw_head",
    "TCP":    "rule_idw_head",
    "FREE":   "rule_head",
}

# Which master_kind string each master table carries on rule_v and on
# rule_condition_head.  Three lookups, built once, used by every query that
# has to join rule_v to a specific master.
_MASTER_KIND_BY_TABLE = {
    "head_master":      "head",
    "rera_head_master": "rera_head",
    "idw_head_master":  "idw_head",
}
_MASTER_KIND = {v: k for k, v in _MASTER_KIND_BY_TABLE.items()}


def _resolve_target(account_type: str,
                    master_table: str | None = None) -> dict:
    """The TARGET dict for an account type, from the mapping or the caller.

    The callers in transactions.py pass the master_table they already resolved
    (it is in the rule context), so this just looks it up.  Routers/rules.py
    does not have a per-type context, so it passes nothing and we look it up
    here.
    """
    if master_table:
        return next(
            (t for t in TARGET_BY_TYPE.values() if t["master_table"] == master_table),
            TARGET,
        )
    return TARGET_BY_TYPE.get(
        (account_type or "").strip().upper(), TARGET)


# =============================================================================
# The directions a row can be judged in, and — since 029 dropped BOTH — the only
# answers a cell of the grid can hold. A head means money in or money out; if it
# genuinely means either, that is two rules on two account types, not one cell
# saying nothing.
# =============================================================================

DIRECTIONS = ("CR", "DR")


class MissingRuleHeads(RuntimeError):
    """This account type has no rule, none its heads can satisfy, or one this
    code cannot read."""


# =============================================================================
# The rule as data — heads per direction for one account type.
# =============================================================================

async def allowed_heads(conn, account_type: str,
                        allow_empty: bool = False,
                        master_table: str | None = None) -> dict[str, list[dict]]:
    """The heads each direction accepts for an account type, from the grid.

    Returns {"CR": [{"id", "name"}, ...], "DR": [...]}, ordered by name so the
    dropdown reads the same on every run and the first entry — which is what a
    blanket fix applies — is stable across restarts.

    Only active heads count. A head switched off in Master Data is not an answer
    anyone should be offered, and leaving it in would let the fix write a value
    the row editor would not.

    master_table is resolved from account_type via TARGET_BY_TYPE when not
    passed explicitly — the callers in transactions.py already have it in the
    rule context, and the one in routers/rules.py is reading a single company's
    rules so it can pick the right table per column if it wants.
    """
    target = _resolve_target(account_type, master_table)
    master_kind = _MASTER_KIND_BY_TABLE[target["master_table"]]
    rows = await conn.fetch(
        f"""
        SELECT r.direction, h.id, h.name
          FROM rule_v r
          JOIN {target['master_table']} h ON h.id = r.head_id
         WHERE upper(btrim(r.account_type)) = $1
           AND r.master_kind = $2
           AND h.is_active = true
         ORDER BY h.name, h.id
        """,
        (account_type or "").strip().upper(),
        master_kind,
    )

    out: dict[str, list[dict]] = {d: [] for d in DIRECTIONS}
    for r in rows:
        # The CHECK constraint on each rule_* table permits nothing but CR/DR,
        # so anything else means the table was written around the API.  Said out
        # loud rather than skipped: a row quietly dropped here is a head that is
        # no answer in either direction, which on screen looks like a rule
        # nobody wrote.
        if r["direction"] not in out:
            raise MissingRuleHeads(
                f"'{r['name']}' is stored against {account_type} with the "
                f"direction {r['direction']!r}, which is not CR or DR. Open the "
                f"Rules page and set that cell again.")
        out[r["direction"]].append({"id": r["id"], "name": r["name"]})

    if not any(out.values()) and not allow_empty:
        raise MissingRuleHeads(
            f"No rule is set for {(account_type or '').strip().upper() or 'untyped'} "
            f"accounts yet. Open the Rules page and mark which heads are valid "
            f"for this account type, and in which direction.")
    return out


async def supported_types(conn) -> list[str]:
    """The account types that have at least one usable rule.

    Counts only rows whose head is still active, so a type whose every head was
    switched off is reported as having no rule — which is what running it would
    find anyway. A type with no grid cells but an active condition counts: the
    user did write a rule for it, and telling them otherwise would send them to
    fill in a grid they deliberately left blank.

    Reads from rule_v (the union view) so every account type that has a row in
    any of the three typed tables is counted.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT upper(btrim(r.account_type)) AS account_type
          FROM rule_v r
          JOIN (
              SELECT id, is_active, 'head' AS mk FROM head_master
              UNION ALL
              SELECT id, is_active, 'rera_head' FROM rera_head_master
              UNION ALL
              SELECT id, is_active, 'idw_head' FROM idw_head_master
          ) h ON h.id = r.head_id AND h.mk = r.master_kind
         WHERE h.is_active = true
        UNION
        SELECT DISTINCT upper(btrim(c.account_type))
          FROM rule_condition c
          JOIN rule_condition_head ch ON ch.condition_id = c.id
          JOIN (
              SELECT id, is_active, 'head' AS mk FROM head_master
              UNION ALL
              SELECT id, is_active, 'rera_head' FROM rera_head_master
              UNION ALL
              SELECT id, is_active, 'idw_head' FROM idw_head_master
          ) h ON h.id = ch.head_id AND h.mk = ch.master_kind
         WHERE c.is_active = true AND h.is_active = true
        ORDER BY 1
        """
    )
    return [r["account_type"] for r in rows]


# =============================================================================
# Human-readable sentences — built from the data, never stored.
# =============================================================================

def _listed(names: list[str]) -> str:
    """'a', 'a or b', 'a, b or c' — how a list of heads reads in a sentence."""
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def explain(account_type: str, expected: dict[str, list[dict]],
            conditions: list[dict] | None = None) -> dict[str, str]:
    """The sentence shown under each direction, built from the rule itself.

    Written from the data rather than stored beside it, so it can never drift
    from what the check actually does — the old hardcoded rule carried a
    sentence about IDW and cancellations that nothing verified.

    A direction with no heads on the grid is normally a dead end, and says so.
    It is not one when conditions cover that direction: the user chose to say
    only the specific thing, and the sentence should report that rather than
    claim nothing can be classified.
    """
    kind = (account_type or "").strip().upper() or "this"
    words = {"CR": "coming into", "DR": "leaving"}
    out: dict[str, str] = {}
    for d in DIRECTIONS:
        heads = expected.get(d) or []
        if not heads:
            covered = sum(1 for c in (conditions or []) if c["direction"] == d)
            side = "credit" if d == "CR" else "debit"
            out[d] = (
                f"No head is marked {d} for {kind} accounts, so a {side} here is "
                f"judged only by the {covered} condition"
                f"{'' if covered == 1 else 's'} below."
                if covered else
                f"No head is marked {d} for {kind} accounts, so a {side} here "
                f"cannot be classified under this rule.")
            continue
        out[d] = (f"Money {words[d]} a {kind} account is recorded as "
                  f"{_listed([h['name'] for h in heads])}.")
    return out


def judge(ids: set[int] | None, current_id: int | None) -> str:
    """What the rule says about one row: ok, conflict, or no_direction.

    Takes the ids that row in particular must carry — which is the grid's set
    for its direction, or a condition's set when one matched it — rather than
    the whole rule, because since 030 two rows on the same account and the same
    side can legitimately be judged against different answers.

    A row with no CR/DR marker cannot be judged at all and is reported
    separately rather than counted against the rule — it is missing the one
    thing the rule is keyed on, not breaking it.

    A row whose head is NULL is a conflict: "must be one of these" is not
    satisfied by nothing.
    """
    if ids is None:
        return "no_direction"
    return "ok" if current_id in ids else "conflict"


# =============================================================================
# Conditions: the exception to the grid.
# =============================================================================

def column_kind(data_type: str) -> str:
    """'text', 'number' or 'date' — which operators a column can be tested by.

    Decided from the column's real Postgres type rather than from its name, so
    a custom field called "Cheque No" that was created as text is offered text
    operators and a numeric one is offered numeric operators. Anything this does
    not recognise falls to text, which has an operator for every value and
    therefore cannot leave a column untestable.
    """
    t = (data_type or "").lower()
    if any(k in t for k in ("int", "numeric", "decimal", "real",
                            "double", "money")):
        return "number"
    if t.startswith("date") or "timestamp" in t:
        return "date"
    return "text"


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def _blank(v) -> bool:
    return _text(v) == ""


def _fold(v) -> str:
    """Text compared the way a person reads it: trimmed and case-insensitive.

    A narration is printed by a bank in whatever case its system uses, and the
    same statement will carry REFUND, Refund and refund in the same column. A
    rule that only matched one of them would look broken to the person who
    wrote it, and there is no use for a case-sensitive narration test here.
    """
    return _text(v).casefold()


def _num(v):
    """A Decimal, or None when the value is not a number.

    None means "this test cannot be answered for this row", which reads as no
    match — a blank amount is not "more than 100000".
    """
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    try:
        return Decimal(_text(v).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _date(v):
    """A date, or None. Handles the column's own type and the ISO the date
    input on the Rules page sends; anything else is not a date question."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(_text(v)[:10]).date()
    except ValueError:
        return None


def _cmp(cast, test):
    """A comparison that answers False rather than raising when either side
    will not cast — an unparseable row value is a row the test does not
    describe, not an error the whole check should die on."""
    def run(value, v1, _v2=None):
        a, b = cast(value), cast(v1)
        return False if a is None or b is None else test(a, b)
    return run


def _between(cast):
    def run(value, v1, v2):
        a, lo, hi = cast(value), cast(v1), cast(v2)
        if a is None or lo is None or hi is None:
            return False
        # Written either way round on purpose: "between 500 and 100" is a typo,
        # not a rule that matches nothing, and nobody would find the typo from
        # a rule that silently never fires.
        if lo > hi:
            lo, hi = hi, lo
        return lo <= a <= hi
    return run


# Every test a condition can make, and the ONLY place one is defined. `values`
# is how many boxes the Rules page draws; `kinds` is which column types offer
# it. The frontend reads this list from the API rather than carrying its own
# copy, so an operator cannot be offered that the check does not implement.
OPERATORS: dict[str, dict] = {
    "is":           {"label": "is", "kinds": ("text",), "values": 1,
                     "fn": lambda v, a, _b=None: _fold(v) == _fold(a)},
    "is_not":       {"label": "is not", "kinds": ("text",), "values": 1,
                     "fn": lambda v, a, _b=None: _fold(v) != _fold(a)},
    "contains":     {"label": "contains", "kinds": ("text",), "values": 1,
                     "fn": lambda v, a, _b=None: _fold(a) in _fold(v)},
    "not_contains": {"label": "does not contain", "kinds": ("text",), "values": 1,
                     "fn": lambda v, a, _b=None: _fold(a) not in _fold(v)},
    "starts_with":  {"label": "starts with", "kinds": ("text",), "values": 1,
                     "fn": lambda v, a, _b=None: _fold(v).startswith(_fold(a))},
    "ends_with":    {"label": "ends with", "kinds": ("text",), "values": 1,
                     "fn": lambda v, a, _b=None: _fold(v).endswith(_fold(a))},

    "eq":      {"label": "equals", "kinds": ("number",), "values": 1,
                "fn": _cmp(_num, lambda a, b: a == b)},
    "ne":      {"label": "does not equal", "kinds": ("number",), "values": 1,
                "fn": _cmp(_num, lambda a, b: a != b)},
    "gt":      {"label": "more than", "kinds": ("number",), "values": 1,
                "fn": _cmp(_num, lambda a, b: a > b)},
    "gte":     {"label": "at least", "kinds": ("number",), "values": 1,
                "fn": _cmp(_num, lambda a, b: a >= b)},
    "lt":      {"label": "less than", "kinds": ("number",), "values": 1,
                "fn": _cmp(_num, lambda a, b: a < b)},
    "lte":     {"label": "at most", "kinds": ("number",), "values": 1,
                "fn": _cmp(_num, lambda a, b: a <= b)},
    "between": {"label": "between", "kinds": ("number", "date"), "values": 2,
                "fn": lambda v, a, b: (_between(_num)(v, a, b)
                                       or _between(_date)(v, a, b))},

    "on":     {"label": "on", "kinds": ("date",), "values": 1,
               "fn": _cmp(_date, lambda a, b: a == b)},
    "before": {"label": "before", "kinds": ("date",), "values": 1,
               "fn": _cmp(_date, lambda a, b: a < b)},
    "after":  {"label": "after", "kinds": ("date",), "values": 1,
               "fn": _cmp(_date, lambda a, b: a > b)},

    # No value, and offered on every kind — "the bank left this blank" is a
    # question worth asking about any column.
    "is_empty":     {"label": "is empty", "kinds": ("text", "number", "date"),
                     "values": 0, "fn": lambda v, _a=None, _b=None: _blank(v)},
    "is_not_empty": {"label": "is not empty", "kinds": ("text", "number", "date"),
                     "values": 0, "fn": lambda v, _a=None, _b=None: not _blank(v)},
}


def operator_catalog() -> list[dict]:
    """OPERATORS as the Rules page needs it — without the functions."""
    return [{"name": name, "label": op["label"],
             "kinds": list(op["kinds"]), "values": op["values"]}
            for name, op in OPERATORS.items()]


def phrase(condition: dict, column_label: str | None = None) -> str:
    """The IF half of a condition: `a debit whose Narration contains "REFUND"`.

    Separate from the whole sentence because it is also what the errors below
    say, and a condition with no head left to name cannot be described by a
    sentence that ends in one.
    """
    op = OPERATORS.get(condition["operator"])
    side = "credit" if condition["direction"] == "CR" else "debit"
    subject = column_label or condition["subject_field"]
    label = op["label"] if op else f"[unknown test {condition['operator']!r}]"
    values = op["values"] if op else 1

    if values == 0:
        test = f"{subject} {label}"
    elif values == 1:
        test = f'{subject} {label} "{_text(condition["value1"])}"'
    else:
        test = (f'{subject} {label} "{_text(condition["value1"])}" '
                f'and "{_text(condition["value2"])}"')
    return f"a {side} whose {test}"


def describe(condition: dict, column_label: str | None = None) -> str:
    """One condition as the sentence shown on screen and in the check result.

    Generated from the stored row every time it is read, for the same reason
    `explain` is: a sentence saved beside a rule is a sentence that stops being
    true the moment somebody edits the rule and not the sentence.
    """
    names = [h["name"] for h in condition.get("heads") or []]
    # Only the first letter — str.capitalize() would lower-case the rest, and
    # the rest is the company's own column name and the user's own search text.
    said = phrase(condition, column_label)
    return f"{said[:1].upper()}{said[1:]} is {_listed(names)}."


# All masters in one place, so the UNION ALL below is written once.
_MASTER_UNION = (
    "SELECT id, is_active, 'head' AS mk FROM head_master "
    "UNION ALL "
    "SELECT id, is_active, 'rera_head' FROM rera_head_master "
    "UNION ALL "
    "SELECT id, is_active, 'idw_head' FROM idw_head_master"
)


async def load_conditions(conn, account_type: str,
                          columns: set[str] | None = None) -> list[dict]:
    """The active conditions for an account type, in the order they decide.

    Each carries its heads (active ones only) and the id set the check compares
    against, so the row loop does no work per row that could have been done
    once here.

    Refuses rather than skips, in three cases that all mean the same thing —
    this sentence can no longer be answered:

      * its heads are gone or every one of them is switched off,
      * its column has been dropped or renamed on the Field Mapping page,
      * its operator is not one this code implements.

    A skipped condition is worse than a refusal in every one of them: the rows
    it should have judged fall through to the grid and come back green, which
    is a clean bill of health nobody asked for.
    """
    rows = await conn.fetch(
        f"""
        SELECT c.id, c.direction, c.subject_field, c.operator,
               c.value1, c.value2, c.sort_order,
               coalesce(json_agg(json_build_object('id', h.id, 'name', h.name)
                                 ORDER BY ch.sort_order, h.name, h.id)
                        FILTER (WHERE h.id IS NOT NULL), '[]'::json) AS heads
          FROM rule_condition c
          LEFT JOIN rule_condition_head ch ON ch.condition_id = c.id
          LEFT JOIN ({_MASTER_UNION}) h
                 ON h.id = ch.head_id
                AND h.mk = ch.master_kind
                AND h.is_active = true
         WHERE upper(btrim(c.account_type)) = $1
           AND c.is_active = true
         GROUP BY c.id
         ORDER BY c.sort_order, c.id
        """,
        (account_type or "").strip().upper(),
    )

    out: list[dict] = []
    for r in rows:
        c = dict(r)
        c["heads"] = json.loads(c["heads"])
        if c["operator"] not in OPERATORS:
            raise MissingRuleHeads(
                f"A condition on {(account_type or '').strip().upper()} uses "
                f"the test {c['operator']!r}, which this version does not know. "
                f"Open the Rules page and set that condition again.")
        if not c["heads"]:
            raise MissingRuleHeads(
                f"The condition for {phrase(c)} has no head left to point at — "
                f"the ones it named were deleted or switched off in Master "
                f"Data. Open the Rules page and give it a head, or switch the "
                f"condition off.")
        if columns is not None and c["subject_field"] not in columns:
            raise MissingRuleHeads(
                f"A condition on {(account_type or '').strip().upper()} tests "
                f"the column '{c['subject_field']}', which no longer exists on "
                f"the imported rows. Open the Rules page and point it at a "
                f"column that does, or switch the condition off.")
        c["ids"] = {h["id"] for h in c["heads"]}
        out.append(c)
    return out


def subject_fields(conditions: list[dict]) -> list[str]:
    """The distinct columns a set of loaded conditions tests, in a fixed order.

    Fetched once and read per row. Ordered so the SELECT this builds is stable
    between the check and the fix, which have to agree row for row.
    """
    return sorted({c["subject_field"] for c in conditions})


def subject_sql(fields: list[str], alias: str = "t") -> str:
    """The extra SELECT list for those columns, aliased s0, s1, ...

    Positional aliases rather than each column's own name, because a subject
    column may legitimately be called `amount` or `id` — both already selected
    under those names by the queries this is appended to, and two columns of one
    row sharing a key is a silent wrong answer rather than an error.

    The identifier is double-quoted even though these names were checked against
    the live column list before they were stored and again when they were
    loaded: "somebody upstream validated this" is not a property this line can
    verify, and quoting costs nothing.
    """
    return "".join(
        f', {alias}."{f.replace(chr(34), chr(34) * 2)}" AS s{i}'
        for i, f in enumerate(fields))


def subject_values(record, fields: list[str]) -> dict:
    """{column name: value} for one fetched row, undoing subject_sql's aliases."""
    return {f: record[f"s{i}"] for i, f in enumerate(fields)}


def take_subjects(row: dict, fields: list[str]) -> dict:
    """subject_values, lifting the aliases out of the row dict on the way.

    The check hands its rows straight to the browser; s0 and s1 are this
    module's plumbing, not columns anybody asked to see.
    """
    return {f: row.pop(f"s{i}") for i, f in enumerate(fields)}


def match(condition: dict, subjects: dict) -> bool:
    """Does this condition's test pass for this row?

    `subjects` is {column name: value} for the columns the loaded conditions
    actually test — read out of a row that was already fetched. This is the
    whole of the user-entered logic, and it is a dict lookup and a function
    call: no expression is built, parsed or executed.
    """
    op = OPERATORS[condition["operator"]]
    return bool(op["fn"](subjects.get(condition["subject_field"]),
                         condition["value1"], condition["value2"]))


def resolve(direction: str | None, subjects: dict, conditions: list[dict],
            expected: dict[str, list[dict]],
            allowed_ids: dict[str, set[int]]):
    """Which heads this one row must carry, and which sentence says so.

    Returns (heads, ids, condition). The condition is None when the grid
    decided, and all three are None when the row has no CR/DR marker and so
    cannot be judged at all.

    Conditions first, first match wins — that is the whole precedence rule, and
    it is here rather than split between the check and the fix so the two
    endpoints cannot come to different answers about the same row.
    """
    if direction not in allowed_ids:
        return None, None, None
    for c in conditions:
        if c["direction"] == direction and match(c, subjects):
            return c["heads"], c["ids"], c
    return expected.get(direction, []), allowed_ids[direction], None
