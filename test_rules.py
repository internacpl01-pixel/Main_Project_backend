"""End-to-end check that the Rules grid and the condition engine both work.

Runs against the real ASGI app with get_current_user overridden -- no login, no
password, per the standing rule. Read-only apart from one condition it creates
in company_028 and deletes again.

Nothing here is pinned to a snapshot of the rules. Migration 033 removed the
three seeded grid rows, so every rule in the database is now one a person
entered and can change between runs; an assertion naming a head or a row count
would fail on the next edit and teach everyone to ignore this file. What it
asserts instead are the properties that must hold whatever the rules say: the
summary agrees with the grid, a condition outranks the grid for exactly the rows
it describes and no others, every rejection still rejects, and the state it
found is the state it leaves.
"""
import asyncio

import httpx

import database
from main import app
from routers.auth import get_current_user

SCHEMA = "company_028"
ACCOUNT = "045563200000264"

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")


def note(label, value):
    print(f"  [info] {label}: {value}")


async def main() -> None:
    await database.init_pool()
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "probe", "role": "company_admin",
        "level": 0, "company_id": 28, "schema": SCHEMA,
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as cl:

        print("\n-- grid --")
        r = await cl.get("/rules/matrix")
        check("GET /rules/matrix is 200", r.status_code, 200)
        m = r.json()
        check("directions are CR/DR only", m["directions"], ["CR", "DR"])
        check("target is the RERA head master",
              m["target"]["master_table"], "rera_head_master")
        check("no master_kind anywhere in the response",
              "master_kind" in r.text, False)
        note("heads", len(m["heads"]))
        note("account types", m["account_types"])
        head_ids = {h["id"] for h in m["heads"]}
        filled = {(h, t): d for h, row in m["cells"].items()
                  for t, d in row.items()}
        note("cells set", len(filled))
        check("every cell holds a direction the check accepts",
              set(filled.values()) <= set(m["directions"]), True)
        check("every cell names a type the page offers",
              {t for _h, t in filled} <= set(m["account_types"]), True)

        # Read once, up here, because the summary counts conditions too.
        r = await cl.get("/rules/conditions")
        check("GET /rules/conditions is 200", r.status_code, 200)
        opts = r.json()
        before = {c["id"] for c in opts["conditions"]}
        note("conditions already written", len(before))

        # 033 removed the seeded rules, so the grid holds nothing this file put
        # there — every row in it was entered by a person and can change between
        # runs. So assert the two endpoints agree with EACH OTHER rather than
        # with a snapshot, which is the property that was actually worth pinning.
        want: dict[str, dict] = {}
        for (h, t), d in filled.items():
            if int(h) not in head_ids:
                continue          # summary joins active heads only; match it
            e = want.setdefault(t, {"cr": 0, "dr": 0, "total": 0,
                                    "conditions": 0})
            e[d.lower()] += 1
            e["total"] += 1
        for c in opts["conditions"]:
            if c["is_active"]:
                want.setdefault(c["account_type"],
                                {"cr": 0, "dr": 0, "total": 0,
                                 "conditions": 0})["conditions"] += 1

        r = await cl.get("/rules/summary")
        check("GET /rules/summary is 200", r.status_code, 200)
        check("summary agrees with the grid and the conditions", r.json(), want)

        print("\n-- check rules, grid only --")
        r = await cl.post("/transactions/temp-trans/check-rules",
                          json={"account_type": "RERA",
                                "account_number": ACCOUNT})
        check("check-rules is 200", r.status_code, 200)
        res = r.json()
        note("summary", res["summary"])
        check("no row is judged by a condition that does not exist",
              {row["rule_id"] for row in res["rows"]} <= (before | {None}), True)
        note("rows already judged by a condition",
             sum(1 for row in res["rows"] if row["rule_id"] is not None))
        # Which rule owns each row before this run adds one, compared per row
        # further down: "nothing else changed hands" is the invariant, and a
        # count cannot tell a swap from a no-op.
        owner = {row["id"]: row["rule_id"] for row in res["rows"]}
        verdict = {row["id"]: row["status"] for row in res["rows"]}
        base_conflicts = res["summary"]["conflicts"]
        note("expected CR", [h["name"] for h in res["expected"]["CR"]])
        note("expected DR", [h["name"] for h in res["expected"]["DR"]])

        print("\n-- the columns the dialog can show --")
        offered = {c["name"] for c in res["columns"]}
        note("columns offered", len(offered))
        check("no alias leaks to the client",
              ('"d0"' in r.text) or ('"s0"' in r.text), False)
        check("the defaults are all on offer",
              set(res["default_columns"]) <= offered, True)
        check("every condition's subject column is offered by default",
              all(c["subject_field"] in res["default_columns"]
                  for c in res["conditions"].values()), True)
        flagged = [row for row in res["rows"] if row["status"] == "conflict"]
        check("conflicting rows carry their statement columns",
              all(set(row["values"]) == offered for row in flagged), True)
        check("rows the dialog does not draw carry none",
              any("values" in row for row in res["rows"]
                  if row["status"] != "conflict"), False)
        by_name = {c["name"]: c for c in res["columns"]}
        note("default columns",
             [by_name[n]["label"] for n in res["default_columns"]])

        print("\n-- the staging table can show only the flagged rows --")
        want_ids = {row["id"] for row in flagged}
        rr = await cl.get("/transactions/temp-trans",
                          params={"rule_conflicts": f"RERA:{ACCOUNT}",
                                  "limit": 200})
        check("filtered list is 200", rr.status_code, 200)
        listed = rr.json()
        check("it lists exactly what the check calls a conflict",
              {row["id"] for row in listed["rows"]}, want_ids)
        check("and counts them", listed["total"], len(want_ids))
        for bad, frag in [
            ("RERA", 'must be written "TYPE:ACCOUNT"'),
            ("NOPE:1", "not in the Bank master"),
            # Nothing typed reaches SQL: both halves go through _rule_context,
            # which looks them up rather than interpolating them.
            ("RERA:'; DROP TABLE temp_trans; --", "no digits in it"),
        ]:
            rr = await cl.get("/transactions/temp-trans",
                              params={"rule_conflicts": bad})
            ok = rr.status_code == 400 and frag in rr.json().get("detail", "")
            check(f"rejects rule_conflicts={bad[:24]!r}", ok, True)
        rr = await cl.get("/transactions/temp-trans", params={"limit": 200})
        check("the unfiltered list still works", rr.status_code, 200)

        print("\n-- conditions --")
        ops = {o["name"] for o in opts["operators"]}
        check("contains is offered", "contains" in ops, True)
        note("operators", len(ops))
        cols = {c["name"]: c for c in opts["columns"]}
        note("testable columns", len(cols))
        desc = next((c for c in opts["columns"] if c["label"] == "DESC"), None)
        check("DESC is a text column", desc and desc["kind"], "text")
        heads = {h["name"]: h["id"] for h in opts["heads"]}

        # A head deliberately NOT on the RERA/DR grid, to prove a condition can
        # admit one the grid leaves blank -- the semantics chosen on 2026-09-03.
        # Chosen from what the grid actually says rather than named outright:
        # since 033 the grid is the company's, and any head named here could be
        # on it by the next run.
        dr_ids = {h["id"] for h in res["expected"]["DR"]}
        off_grid_name = next(
            (n for n in ("Master to Free",) if heads.get(n) not in dr_ids
             and n in heads),
            next(n for n, i in sorted(heads.items()) if i not in dr_ids))
        off_grid = heads[off_grid_name]
        check(f"'{off_grid_name}' is not a grid answer for a RERA debit",
              off_grid in dr_ids, False)

        print("\n-- rejects bad input --")
        bad = {"account_type": "RERA", "direction": "DR",
               "subject_field": desc["name"], "operator": "contains",
               "value1": "x", "head_ids": [off_grid]}
        for label, patch, frag in [
            ("unknown column", {"subject_field": "no_such_col"}, "not a column"),
            ("SQL in the column name",
             {"subject_field": f'{desc["name"]}"; DROP TABLE temp_trans; --'},
             "not a column"),
            ("numeric test on a text column", {"operator": "gt"},
             "cannot be asked of"),
            ("unknown operator", {"operator": "nope"}, "not a test"),
            ("no heads", {"head_ids": []}, "at least one head"),
            ("bad direction", {"direction": "BOTH"}, "must be CR or DR"),
            ("missing value", {"value1": ""}, "needs a value"),
        ]:
            rr = await cl.post("/rules/conditions", json={**bad, **patch})
            ok = rr.status_code == 400 and frag in rr.json().get("detail", "")
            check(f"rejects {label}", ok, True)

        print("\n-- preview, then create --")
        rr = await cl.post("/rules/conditions/preview",
                           json={k: v for k, v in bad.items()
                                 if k != "head_ids"} |
                                {"value1": "master to free"})
        check("preview is 200", rr.status_code, 200)
        pv = rr.json()
        note("preview", f'{pv["matched"]} of {pv["scanned"]} scanned')
        check("preview finds the two 'master to free' debits",
              pv["matched"], 2)
        note("preview phrase", pv["phrase"])

        rr = await cl.post("/rules/conditions",
                           json={**bad, "value1": "master to free"})
        check("create is 200", rr.status_code, 200)
        cond = rr.json()
        cid = cond["id"]
        note("sentence", cond["sentence"])
        check("no problem flagged", cond["problem"], None)

        # The Check Rules dropdown reads this to say whether a type has a rule
        # at all, so a condition has to register there too.
        rr = await cl.get("/rules/summary")
        check("summary counts the new condition",
              rr.json()["RERA"]["conditions"],
              want.get("RERA", {}).get("conditions", 0) + 1)

        print("\n-- the condition outranks the grid --")
        r = await cl.post("/transactions/temp-trans/check-rules",
                          json={"account_type": "RERA",
                                "account_number": ACCOUNT})
        res2 = r.json()
        judged = [row for row in res2["rows"] if row["rule_id"] == cid]
        check("two rows judged by the condition", len(judged), 2)
        check("its heads are offered instead of the grid's",
              [h["name"] for h in res2["conditions"][str(cid)]["heads"]],
              [off_grid_name])
        # Per row, not per count: a condition that stole one row and handed back
        # another would keep every total identical.
        check("only the rows it describes changed hands",
              {row["id"] for row in res2["rows"]
               if row["rule_id"] != owner.get(row["id"])},
              {row["id"] for row in judged})
        # NOT "the conflict count is unchanged". It was, back when every head on
        # this account was unset; once heads are filled in, a row the condition
        # takes over can legitimately flip — the condition allows one head where
        # the grid allowed three. The invariant is narrower and truer: a row the
        # condition does not describe cannot have changed its mind.
        check("no row outside the condition changed verdict",
              {row["id"] for row in res2["rows"]
               if row["rule_id"] != cid and row["status"] != verdict.get(row["id"])},
              set())
        note("conflicts", f"{base_conflicts} before, "
                          f"{res2['summary']['conflicts']} with the condition")

        print("\n-- apply is re-checked server side --")
        # Needs a head the GRID allows for a RERA debit, to prove the condition
        # is what the server re-checks against. Since 033 the RERA/DR column can
        # legitimately be empty, and then there is no such head to send.
        if not res["expected"]["DR"]:
            note("skipped", "no head is on the RERA/DR grid to test against")
        else:
            target = judged[0]
            grid_head = res["expected"]["DR"][0]["id"]
            rr = await cl.post("/transactions/temp-trans/check-rules/apply",
                               json={"account_type": "RERA",
                                     "account_number": ACCOUNT,
                                     "rows": [{"id": target["id"],
                                               "head_id": grid_head}]})
            check("refuses a grid head on a row the condition owns",
                  rr.status_code, 400)
            note("refusal", rr.json().get("detail", "")[:120])

        print("\n-- cleanup --")
        rr = await cl.delete(f"/rules/conditions/{cid}")
        check("condition deleted", rr.status_code, 200)
        r = await cl.post("/transactions/temp-trans/check-rules",
                          json={"account_type": "RERA",
                                "account_number": ACCOUNT})
        check("back to the grid-only verdict",
              r.json()["summary"], res["summary"])
        r = await cl.get("/rules/matrix")
        check("grid unchanged by the whole run", r.json()["cells"], m["cells"])

        print("\n-- lock all / unlock all --")
        # Last, and it puts back exactly what it found: locking a row makes the
        # apply endpoint skip it, so anything above would start failing for a
        # reason that has nothing to do with rules.
        async def rows_now(**params):
            rr = await cl.get("/transactions/temp-trans",
                              params={"limit": 200, **params})
            return rr.json()["rows"]

        was_locked = {row["id"] for row in await rows_now() if row["is_locked"]}
        note("locked before", len(was_locked))
        conflict_ids = {row["id"] for row in await rows_now(
            rule_conflicts=f"RERA:{ACCOUNT}")}

        rr = await cl.post("/transactions/temp-trans/lock-all",
                           params={"rule_conflicts": f"RERA:{ACCOUNT}"},
                           json={"locked": True})
        check("lock-all is 200", rr.status_code, 200)
        check("it matched exactly the filtered rows",
              rr.json()["matched"], len(conflict_ids))
        check("locking locked exactly those rows",
              {row["id"] for row in await rows_now() if row["is_locked"]},
              was_locked | conflict_ids)

        rr = await cl.post("/transactions/temp-trans/lock-all",
                           params={"rule_conflicts": f"RERA:{ACCOUNT}"},
                           json={"locked": True})
        check("locking twice changes nothing", rr.json()["changed"], 0)

        rr = await cl.post("/transactions/temp-trans/lock-all",
                           params={"rule_conflicts": f"RERA:{ACCOUNT}"},
                           json={"locked": False})
        check("unlock-all is 200", rr.status_code, 200)

        # Put back anything this suite unlocked that was locked when it started.
        for row_id in was_locked:
            await cl.post(f"/transactions/temp-trans/{row_id}/lock",
                          json={"locked": True})
        check("lock state restored",
              {row["id"] for row in await rows_now() if row["is_locked"]},
              was_locked)

    await database.close_pool()
    print(f"\n{PASS} passed, {FAIL} failed")


asyncio.run(main())
