"""
Account-type rules for staged rows.

The Bank master types every account — MASTER, RERA, IDW, FREE, whatever rows
account_type_master holds — and some of those types carry a rule about how a
row printed under such an account must be classified. The Check Rules button
on Imported Rows picks one account and checks its staged rows against the
rule for that account's type.

Only RERA has a rule so far:

  - money coming IN (CR) must be classified "Master to RERA" — a RERA
    account is funded by transfer from the Master collection account;
  - money going OUT (DR) must be "RERA to IDW", unless it is a customer
    cancellation refund.

The rule is written against meanings, not stored strings. The heads it names
live in each company's own master table under that company's own spelling —
'Master 2 RERA', 'RERA 2 IDW' and 'Cust Cancellation' in the seeded data —
so each expectation is a set of accepted spellings, matched against the live
master rows after normalisation. A company that renames a head within reason
keeps a working rule; a company missing one is told which entry the rule
needs, rather than shown a rule that can never pass.
"""
import re

# Which classification each rule is about: the id column on temp_trans, the
# fieldmap.mirrors key that names its display column, and the master table its
# values come from. 'label' is what messages call that master when the company
# has no mirroring column to borrow a name from.
#
# Per direction, 'expected' lists the acceptable answers in order of
# preference — the first is what a blanket fix applies, the rest are the
# legitimate alternatives the dialog offers. 'accept' holds the spellings that
# count as that answer, in normalise_head() form.
RULES = {
    "RERA": {
        "field": "rera_head_id",
        "mirrors": "rera_head",
        "master_table": "rera_head_master",
        "label": "RERA head",
        "directions": {
            "CR": {
                "why": "Money coming into a RERA account is the transfer "
                       "from the Master account.",
                "expected": [
                    {"label": "Master to RERA",
                     "accept": {"master to rera"}},
                ],
            },
            "DR": {
                "why": "Money leaving a RERA account goes to IDW, unless it "
                       "is a customer cancellation refund.",
                "expected": [
                    {"label": "RERA to IDW",
                     "accept": {"rera to idw"}},
                    {"label": "Customer Cancellation",
                     "accept": {"customer cancellation", "cust cancellation"}},
                ],
            },
        },
    },
}


def normalise_head(name: str) -> str:
    """Fold a head name for matching: 'Master 2 RERA' -> 'master to rera'.

    Lower-cased, punctuation collapsed to spaces, and a lone digit 2 read as
    'to' — bookkeepers write transfer heads both ways, and in that position it
    is the join word, not a quantity.
    """
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    return " ".join("to" if w == "2" else w for w in words)


def rule_for(account_type: str) -> dict | None:
    """The rule for an account type, or None — most types have none yet."""
    return RULES.get((account_type or "").strip().upper())


def supported_types() -> list[str]:
    """The account types that carry a rule, for messages that must say so."""
    return sorted(RULES)


class MissingRuleHeads(RuntimeError):
    """The master table lacks a head the rule needs to name."""


async def resolve_expected(conn, rule: dict) -> dict[str, list[dict]]:
    """Map each direction to the live master rows the rule accepts.

    Returns {"CR": [{"id", "name"}, ...], "DR": [...]} resolved against this
    company's own active rows, in the rule's order of preference. Raises
    MissingRuleHeads naming what is absent — a rule that cannot name its own
    heads must refuse to run, because "0 conflicts" from a rule matching
    nothing reads as a clean bill.
    """
    rows = await conn.fetch(
        f"SELECT id, name FROM {rule['master_table']} "
        f"WHERE is_active = true ORDER BY id"
    )
    by_norm: dict[str, list[dict]] = {}
    for r in rows:
        by_norm.setdefault(normalise_head(r["name"]), []).append(
            {"id": r["id"], "name": r["name"]})

    out: dict[str, list[dict]] = {}
    missing: list[str] = []
    for direction, spec in rule["directions"].items():
        found: list[dict] = []
        for exp in spec["expected"]:
            # EVERY live row matching any accepted spelling counts. A company
            # holding both 'RERA 2 IDW' and 'RERA to IDW' meant the same head
            # twice; a row classified with either complies, and flagging one
            # of them would rewrite correct data to its duplicate. The
            # spellings are walked in sorted order and the rows arrive
            # id-ordered, so the fix default — the first entry — is the same
            # row on every run.
            hits = [h for a in sorted(exp["accept"]) for h in by_norm.get(a, [])]
            if not hits:
                missing.append(exp["label"])
            else:
                found.extend(hits)
        out[direction] = found

    if missing:
        names = sorted(set(missing))
        raise MissingRuleHeads(
            f"The {rule['label']} master has no entry for: {', '.join(names)}. "
            f"The rule cannot check or fix anything until "
            f"{'this head exists' if len(names) == 1 else 'these heads exist'} — "
            f"add {'it' if len(names) == 1 else 'them'} under Master Data."
        )
    return out
