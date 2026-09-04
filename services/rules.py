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

Since 034 a rule also names WHICH head it is about — Internal Head, RERA Head or
TCP Head, the three masters a staged row carries a column for. One run of the
check judges one of them, so everything below takes a `target` and reads only
the rules written for it.

See company/028_rule_table.sql, company/030_rule_condition.sql,
company/032_rule_single_master.sql and company/034_rule_all_heads.sql.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# What a rule can judge and write — one entry per head master, keyed by the
# same word `fieldmap.mirrors` stores and `_EDITABLE_PICKERS` maps to.
#
# A staged row carries three heads, each written by its own dropdown into its
# own column, and since 034 a rule names which of the three it is about. The
# whole of that choice is here: `column` is the column on `rule` and on
# `rule_condition_head` that holds the id, `field` is the column on temp_trans
# the check writes, and `master_table` is where the heads come from.
#
# This is a fixed set on purpose, and it is not the thing 031 got wrong. 031
# keyed its masters on the words MASTER / RERA / IDW / TCP / FREE — account TYPE
# names, which are each company's own rows in account_type_master, so every type
# nobody thought to list silently fell back to the RERA master. These three are
# tables in the schema every company shares; the same three `_MIRROR_TABLES` and
# `_MASTER_SCHEMA` already name. What stays live is which of them a given
# company actually uses — that is the fieldmap's answer, read per request — and
# the heads themselves, which are always that company's own rows.
#
# `label` is deliberately absent. It belongs to Master Data, which is where it
# is edited ('idw_head' has been shown as "TCP Head" since a rename that touched
# nothing else), so routers/rules.py reads it from there rather than keeping a
# second copy here to fall out of step.
TARGETS: dict[str, dict] = {
    "head": {
        "column": "head_id",
        "field": "head_id",
        "mirrors": "head",
        "master_table": "head_master",
    },
    "rera_head": {
        "column": "rera_head_id",
        "field": "rera_head_id",
        "mirrors": "rera_head",
        "master_table": "rera_head_master",
    },
    "idw_head": {
        "column": "idw_head_id",
        "field": "idw_head_id",
        "mirrors": "idw_head",
        "master_table": "idw_head_master",
    },
}

# Which one a request means when it does not say. Every rule written before 034
# is one of these, and every caller that has not been taught to ask still gets
# the grid it has always got.
DEFAULT_TARGET = "rera_head"


class UnknownTarget(ValueError):
    """A head type that is not one of TARGETS."""


def target_def(target: str | None) -> dict:
    """One entry of TARGETS, or UnknownTarget.

    Every table and column name this module interpolates into SQL comes back
    through here. The names themselves are module constants — nothing a request
    carries is ever formatted into a statement — and this is the gate that keeps
    it that way: a caller passes the KEY, and only a key already in TARGETS
    yields anything to interpolate.
    """
    key = (target or DEFAULT_TARGET).strip()
    if key not in TARGETS:
        raise UnknownTarget(
            f"'{target}' is not a head type. It must be one of "
            f"{', '.join(TARGETS)}.")
    return {**TARGETS[key], "target": key}

# The directions a row can be judged in, and — since 029 dropped BOTH — the only
# answers a cell of the grid can hold. A head means money in or money out; if it
# genuinely means either, that is two rules on two account types, not one cell
# saying nothing.
DIRECTIONS = ("CR", "DR")


class MissingRuleHeads(RuntimeError):
    """This account type has no rule, none its heads can satisfy, or one this
    code cannot read."""


# =============================================================================
# The rule as data — heads per direction for one account type.
# =============================================================================

async def allowed_heads(conn, account_type: str,
                        target: str = DEFAULT_TARGET,
                        allow_empty: bool = False) -> dict[str, list[dict]]:
    """The heads each direction accepts for an account type, from the grid.

    Returns {"CR": [{"id", "name"}, ...], "DR": [...]}, ordered by name so the
    dropdown reads the same on every run and the first entry — which is what a
    blanket fix applies — is stable across restarts.

    Only active heads count. A head switched off in Master Data is not an answer
    anyone should be offered, and leaving it in would let the fix write a value
    the row editor would not.

    Raises MissingRuleHeads when nothing is recorded, rather than returning two
    empty lists: "0 conflicts" from a rule that accepts nothing reads as a clean
    bill of health, and it is the opposite.

    allow_empty=True is passed when the type has conditions but a blank grid
    column — that type does have a rule, just not a general one, and refusing to
    run would be refusing to run the conditions the user did write.

    `target` names which head master this is about. The rules for the other two
    are not consulted at all: a run judges one column of the row, so mixing in a
    head it will not write could only produce a conflict nothing can fix.
    """
    t = target_def(target)
    rows = await conn.fetch(
        f"""
        SELECT r.direction, h.id, h.name
          FROM rule r
          JOIN {t['master_table']} h ON h.id = r."{t['column']}"
         WHERE r.target = $2
           AND upper(btrim(r.account_type)) = $1
           AND h.is_active = true
         ORDER BY h.name, h.id
        """,
        (account_type or "").strip().upper(), t["target"],
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

    if not any(out.values()) and not allow_empty:
        raise MissingRuleHeads(
            f"No rule is set for {(account_type or '').strip().upper() or 'untyped'} "
            f"accounts yet. Open the Rules page and mark which heads are valid "
            f"for this account type, and in which direction.")
    return out


async def supported_types(conn, target: str = DEFAULT_TARGET) -> list[str]:
    """The account types that have at least one usable rule for this head type.

    Counts only rows whose head is still active, so a type whose every head was
    switched off is reported as having no rule — which is what running it would
    find anyway. A type with no grid cells but an active condition counts: the
    user did write a rule for it, and telling them otherwise would send them to
    fill in a grid they deliberately left blank.

    Per target, because "MASTER has a rule" is only ever true of one head type
    at a time — a company can easily have written its Internal Head column and
    left the TCP one blank, and offering the second because the first exists is
    how somebody runs a check that can find nothing.
    """
    t = target_def(target)
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT upper(btrim(r.account_type)) AS account_type
          FROM rule r
          JOIN {t['master_table']} h ON h.id = r."{t['column']}"
         WHERE r.target = $1 AND h.is_active = true
        UNION
        SELECT DISTINCT upper(btrim(c.account_type))
          FROM rule_condition c
          JOIN rule_condition_head ch ON ch.condition_id = c.id
          JOIN {t['master_table']} h ON h.id = ch."{t['column']}"
         WHERE c.target = $1 AND c.is_active = true AND h.is_active = true
         ORDER BY 1
        """,
        t["target"],
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
            conditions: list[dict] | None = None,
            target_label: str = "head") -> dict[str, str]:
    """The sentence shown under each direction, built from the rule itself.

    Written from the data rather than stored beside it, so it can never drift
    from what the check actually does — the old hardcoded rule carried a
    sentence about IDW and cancellations that nothing verified.

    A direction with no heads on the grid is normally a dead end, and says so.
    It is not one when conditions cover that direction: the user chose to say
    only the specific thing, and the sentence should report that rather than
    claim nothing can be classified.

    `target_label` names the head type in the sentence, because since 034 the
    same account type can be judged three different ways and a sentence that
    only said "no head is marked CR" would be read as a verdict on all three.
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
                f"No {target_label} is marked {d} for {kind} accounts, so a "
                f"{side} here is judged only by the {covered} condition"
                f"{'' if covered == 1 else 's'} below."
                if covered else
                f"No {target_label} is marked {d} for {kind} accounts, so a "
                f"{side} here cannot be classified under this rule.")
            continue
        out[d] = (f"Money {words[d]} a {kind} account takes the {target_label} "
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


async def load_conditions(conn, account_type: str,
                          target: str = DEFAULT_TARGET,
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

    Filtered to one `target` for the same reason the grid is: a check writes one
    column, and a condition about a different head could only ever produce an
    answer this run cannot save.
    """
    t = target_def(target)
    rows = await conn.fetch(
        f"""
        SELECT c.id, c.direction, c.subject_field, c.operator,
               c.value1, c.value2, c.sort_order,
               coalesce(json_agg(json_build_object('id', h.id, 'name', h.name)
                                 ORDER BY ch.sort_order, h.name, h.id)
                        FILTER (WHERE h.id IS NOT NULL), '[]'::json) AS heads
          FROM rule_condition c
          LEFT JOIN rule_condition_head ch ON ch.condition_id = c.id
          LEFT JOIN {t['master_table']} h
                 ON h.id = ch."{t['column']}" AND h.is_active = true
         WHERE upper(btrim(c.account_type)) = $1
           AND c.target = $2
           AND c.is_active = true
         GROUP BY c.id
         ORDER BY c.sort_order, c.id
        """,
        (account_type or "").strip().upper(), t["target"],
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


def subject_sql(fields: list[str], alias: str = "t", prefix: str = "s") -> str:
    """The extra SELECT list for those columns, aliased s0, s1, ...

    Positional aliases rather than each column's own name, because a subject
    column may legitimately be called `amount` or `id` — both already selected
    under those names by the queries this is appended to, and two columns of one
    row sharing a key is a silent wrong answer rather than an error.

    The identifier is double-quoted even though these names were checked against
    the live column list before they were stored and again when they were
    loaded: "somebody upstream validated this" is not a property this line can
    verify, and quoting costs nothing.

    `prefix` lets one query carry two independent sets of columns. The check
    fetches the columns its conditions test AND the columns the dialog displays,
    and those two lists overlap freely — a condition on DESC while DESC is also
    on screen is the normal case, not an edge one. Under a single prefix the
    second set would renumber the first. The engine keeps "s"; anything else
    passes its own.
    """
    return "".join(
        f', {alias}."{f.replace(chr(34), chr(34) * 2)}" AS {prefix}{i}'
        for i, f in enumerate(fields))


def subject_values(record, fields: list[str], prefix: str = "s") -> dict:
    """{column name: value} for one fetched row, undoing subject_sql's aliases."""
    return {f: record[f"{prefix}{i}"] for i, f in enumerate(fields)}


def take_subjects(row: dict, fields: list[str], prefix: str = "s") -> dict:
    """subject_values, lifting the aliases out of the row dict on the way.

    The check hands its rows straight to the browser; s0 and d1 are this
    module's plumbing, not columns anybody asked to see. Every alias is popped
    whether or not its value is kept, so none can reach the wire.
    """
    return {f: row.pop(f"{prefix}{i}") for i, f in enumerate(fields)}


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
