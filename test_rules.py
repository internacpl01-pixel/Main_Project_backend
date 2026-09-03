"""End-to-end check that the Rules grid and the condition engine both work.

Runs against the real ASGI app with get_current_user overridden -- no login, no
password, per the standing rule. Read-only apart from one condition it creates
in company_028 and deletes again, and one grid cell it sets back to what it was.
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
        filled = {(h, t): d for h, row in m["cells"].items()
                  for t, d in row.items()}
        check("five cells set", len(filled), 5)

        r = await cl.get("/rules/summary")
        check("GET /rules/summary is 200", r.status_code, 200)
        check("summary matches the restored grid", r.json(),
              {"IDW": {"cr": 1, "dr": 0, "total": 1, "conditions": 0},
               "RERA": {"cr": 1, "dr": 3, "total": 4, "conditions": 0}})

        print("\n-- check rules, grid only --")
        r = await cl.post("/transactions/temp-trans/check-rules",
                          json={"account_type": "RERA",
                                "account_number": ACCOUNT})
        check("check-rules is 200", r.status_code, 200)
        res = r.json()
        note("summary", res["summary"])
        check("no conditions yet", res["conditions"], {})
        check("every row judged by the grid",
              {row["rule_id"] for row in res["rows"]}, {None})
        base_conflicts = res["summary"]["conflicts"]
        note("expected CR", [h["name"] for h in res["expected"]["CR"]])
        note("expected DR", [h["name"] for h in res["expected"]["DR"]])

        print("\n-- conditions --")
        r = await cl.get("/rules/conditions")
        check("GET /rules/conditions is 200", r.status_code, 200)
        opts = r.json()
        check("starts with none", opts["conditions"], [])
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
        off_grid = heads["Master to Free"]
        check("'Master to Free' is not a grid answer for a RERA debit",
              off_grid in {h["id"] for h in res["expected"]["DR"]}, False)

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
        check("summary counts the condition",
              rr.json()["RERA"]["conditions"], 1)

        print("\n-- the condition outranks the grid --")
        r = await cl.post("/transactions/temp-trans/check-rules",
                          json={"account_type": "RERA",
                                "account_number": ACCOUNT})
        res2 = r.json()
        judged = [row for row in res2["rows"] if row["rule_id"] == cid]
        check("two rows judged by the condition", len(judged), 2)
        check("its heads are offered instead of the grid's",
              [h["name"] for h in res2["conditions"][str(cid)]["heads"]],
              ["Master to Free"])
        check("other rows still judged by the grid",
              all(row["rule_id"] is None
                  for row in res2["rows"] if row["rule_id"] != cid), True)
        check("conflict count unchanged (heads are still unset)",
              res2["summary"]["conflicts"], base_conflicts)

        print("\n-- apply is re-checked server side --")
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

    await database.close_pool()
    print(f"\n{PASS} passed, {FAIL} failed")


asyncio.run(main())
