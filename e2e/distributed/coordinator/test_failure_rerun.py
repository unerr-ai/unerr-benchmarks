#!/usr/bin/env python3
"""Deterministic test of the coordinator failure-rerun-and-persist contract.

Drives the REAL Queue (open_db + schema.sql + Config, same module) through the
full lifecycle documented in README §8.1:

  claim -> fail (attempt 1, < MAX_ATTEMPTS)   -> back to 'pending'  (mid-run retry)
  claim -> fail (attempt 2, == MAX_ATTEMPTS)  -> 'failed'           (rerun budget left)
  claim (fresh queue drained, pending==0)     -> failure-rerun: 'failed'->'pending'->leased
  complete(resolved=1, patch=RERUN_WINS)      -> 'done', OVERWRITES the earlier failure in place
  claim                                       -> {done:true}        (rerun budget spent)

Asserts the persisted final row is the RERUN's outcome, not the initial failure
("a rerun's success overwrites the earlier failure in place"), and that complete()
is unconditional on prior status (a direct 'dead' -> 'done' overwrite) — the
invariant that makes the rerun's result win.

Since 2026-07-21 (silent-session-death fix), scenarios C/D/E cover the SECOND
failure-rerun eligibility shape (`Queue._eligible_rerun_ids` /
`_is_silent_death_meta`, server.py): a 'done'+resolved=0 row whose meta_json
carries `silent_death: true` (harness_terminal.py's terminal-only discriminator
— see DEBUG_FAILED_TASK.md Step 0/3) is ALSO eligible, same budget, same
pending==0 gate — C also proves the budget still caps a SECOND silent death on
the same instance; D proves a 'done'+resolved=0 row WITHOUT the flag (a genuine
capability miss, or malformed/absent meta_json) is NEVER requeued; E proves
eligibility still waits for pending==0 (fresh work drains first).

No fly, no network, no docker. Run:  python3 coordinator/test_failure_rerun.py
Exit 0 = all pass, 1 = any assertion failed.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  (server.py lives alongside this test)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def row_of(q, iid):
    r = q._conn.execute(
        "SELECT status, attempt_count, fail_reruns, resolved, patch, failure_reason "
        "FROM tasks WHERE instance_id=?",
        (iid,),
    ).fetchone()
    return dict(status=r[0], attempt_count=r[1], fail_reruns=r[2],
               resolved=r[3], patch=r[4], failure_reason=r[5])


def new_queue(max_attempts=2, max_failure_rerun=1):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    cfg = server.Config(
        port=0, db_path=db, lease_ttl=2700, max_attempts=max_attempts,
        heartbeat_timeout=120, reaper_interval=30,
        max_failure_rerun=max_failure_rerun, armed=True)
    return server.Queue(server.open_db(cfg), cfg), db


def scenario_a():
    print("== A: fail -> failed -> failure-rerun -> complete persists rerun outcome ==")
    q, db = new_queue(max_attempts=2, max_failure_rerun=1)
    q.seed("run-A", ["INST-A"])
    check("seeded 1 pending", q._counts().get("pending") == 1)

    check("attempt1 claimed INST-A", q.claim("w1").get("instance_id") == "INST-A")
    q.fail("INST-A", "w1", "boom-attempt-1")
    r = row_of(q, "INST-A")
    check("after fail#1 -> pending (mid-run retry)", r["status"] == "pending", f"status={r['status']}")

    check("attempt2 claimed INST-A", q.claim("w2").get("instance_id") == "INST-A")
    q.fail("INST-A", "w2", "boom-attempt-2")
    r = row_of(q, "INST-A")
    check("after fail#2 -> 'failed' (attempts exhausted)", r["status"] == "failed", f"status={r['status']}")
    check("initial result set is a FAILURE (resolved NULL, no patch)",
          r["resolved"] is None and r["patch"] is None, f"resolved={r['resolved']} patch={r['patch']}")
    check("failure_reason durably persisted", r["failure_reason"] == "boom-attempt-2")

    check("fresh queue drained (pending==0)", q._counts().get("pending") == 0)
    check("failure-rerun re-leased INST-A", q.claim("w3").get("instance_id") == "INST-A")
    check("fail_reruns bumped to 1", row_of(q, "INST-A")["fail_reruns"] == 1)

    q.complete("INST-A", "w3", patch="RERUN_PATCH_WINS", report_json={"ok": 1},
               meta_json={"cost": {"usd": 0.42}}, resolved=1)
    r = row_of(q, "INST-A")
    check("final status = 'done'", r["status"] == "done", f"status={r['status']}")
    check("PERSIST: resolved = rerun's 1 (not initial NULL)", r["resolved"] == 1)
    check("PERSIST: patch = rerun's 'RERUN_PATCH_WINS'", r["patch"] == "RERUN_PATCH_WINS")
    check("fail_reruns retained = 1", r["fail_reruns"] == 1)

    check("budget spent -> claim returns {done:true}", q.claim("w4").get("done") is True)
    os.unlink(db)


def scenario_b():
    print("== B: complete() is unconditional on prior status (terminal -> done overwrite) ==")
    q, db = new_queue(max_attempts=1, max_failure_rerun=0)  # rerun disabled -> 'dead' terminal row
    q.seed("run-B", ["INST-B"])
    q.claim("wb")
    q.fail("INST-B", "wb", "boom")
    check("rerun disabled -> exhausted row is 'dead'", row_of(q, "INST-B")["status"] == "dead")
    q.complete("INST-B", "wb", patch="LATE_WINS", report_json={}, meta_json={}, resolved=1)
    r = row_of(q, "INST-B")
    check("complete() overwrote a terminal row -> 'done'", r["status"] == "done")
    check("overwrite carried the new patch", r["patch"] == "LATE_WINS")
    check("overwrite carried the new resolved", r["resolved"] == 1)
    os.unlink(db)


def scenario_c():
    print("== C: silent-death 'done' row is eligible for exactly one rerun, budget-capped ==")
    q, db = new_queue(max_attempts=2, max_failure_rerun=1)
    q.seed("run-C", ["INST-C"])

    check("claimed INST-C", q.claim("w1").get("instance_id") == "INST-C")
    q.complete("INST-C", "w1", patch="", report_json={"ok": 0},
               meta_json={"silent_death": True}, resolved=0)
    r = row_of(q, "INST-C")
    check("silent death lands as 'done' resolved=0 (harness reports a clean exit)",
          r["status"] == "done" and r["resolved"] == 0, f"status={r['status']} resolved={r['resolved']}")

    check("fresh queue drained (pending==0)", q._counts().get("pending") == 0)
    claim2 = q.claim("w2")
    check("failure-rerun re-leased the silent-death row",
          claim2.get("instance_id") == "INST-C", f"claim={claim2}")
    check("fail_reruns bumped to 1", row_of(q, "INST-C")["fail_reruns"] == 1)

    # The rerun ALSO dies silently — proves the budget caps a SECOND silent
    # death on the same instance, it doesn't just cap the first shape's count.
    q.complete("INST-C", "w2", patch="", report_json={"ok": 0},
               meta_json={"silent_death": True}, resolved=0)
    r = row_of(q, "INST-C")
    check("rerun also died silently -> 'done' resolved=0, fail_reruns stays 1 (not re-bumped)",
          r["status"] == "done" and r["resolved"] == 0 and r["fail_reruns"] == 1)
    check("budget spent -> claim returns {done:true}, NOT requeued a 2nd time",
          q.claim("w3").get("done") is True)
    os.unlink(db)


def scenario_d():
    print("== D: 'done'+resolved=0 WITHOUT silent_death is a real capability miss, NEVER requeued ==")
    q, db = new_queue(max_attempts=2, max_failure_rerun=1)
    q.seed("run-D", ["INST-D"])
    q.claim("w1")
    q.complete("INST-D", "w1", patch="", report_json={"ok": 0},
               meta_json={"cost": {"usd": 0.10}}, resolved=0)  # no silent_death key at all
    r = row_of(q, "INST-D")
    check("finished 'done' resolved=0, no silent_death flag",
          r["status"] == "done" and r["resolved"] == 0)
    check("fresh queue drained (pending==0)", q._counts().get("pending") == 0)
    claim2 = q.claim("w2")
    check("NOT re-leased -> claim returns {done:true} straight away (a real miss costs nothing to rerun)",
          claim2.get("done") is True, f"claim={claim2}")
    check("fail_reruns untouched — no budget spent on a genuine capability miss",
          row_of(q, "INST-D")["fail_reruns"] == 0)
    os.unlink(db)

    # Conservative-by-construction: explicit silent_death=false and malformed
    # meta_json both read False, never crash the eligibility scan.
    q2, db2 = new_queue(max_attempts=2, max_failure_rerun=1)
    q2.seed("run-D2", ["INST-D2-a", "INST-D2-b"])
    q2.claim("wa")
    q2.complete("INST-D2-a", "wa", patch="", report_json={}, meta_json={"silent_death": False}, resolved=0)
    q2.claim("wb")
    q2.complete("INST-D2-b", "wb", patch="", report_json={}, meta_json="not-json{{", resolved=0)
    check("fresh queue drained (pending==0) [D2]", q2._counts().get("pending") == 0)
    claim3 = q2.claim("wc")
    check("silent_death=false / malformed meta_json -> conservative False -> {done:true}, no crash",
          claim3.get("done") is True, f"claim={claim3}")
    os.unlink(db2)


def scenario_e():
    print("== E: silent-death rerun waits for pending==0 -> fresh work drains first ==")
    q, db = new_queue(max_attempts=2, max_failure_rerun=1)
    q.seed("run-E", ["INST-E1", "INST-E2"])

    check("claimed INST-E1", q.claim("w1").get("instance_id") == "INST-E1")
    q.complete("INST-E1", "w1", patch="", report_json={"ok": 0},
               meta_json={"silent_death": True}, resolved=0)
    check("INST-E2 still pending -> pending!=0", q._counts().get("pending") == 1)

    claim2 = q.claim("w2")
    check("claim() picks the FRESH pending row, not the silent-death rerun",
          claim2.get("instance_id") == "INST-E2", f"claim={claim2}")

    q.complete("INST-E2", "w2", patch="p", report_json={"ok": 1}, meta_json={}, resolved=1)
    check("now pending==0", q._counts().get("pending") == 0)

    claim3 = q.claim("w3")
    check("NOW eligible -> failure-rerun picks up the silent-death row",
          claim3.get("instance_id") == "INST-E1", f"claim={claim3}")
    os.unlink(db)


def scenario_f():
    print("== F: no-gradeable-verdict harbor death ('no_result_death') is eligible for exactly one rerun, budget-capped ==")
    q, db = new_queue(max_attempts=2, max_failure_rerun=1)
    q.seed("run-F", ["INST-F"])

    check("claimed INST-F", q.claim("w1").get("instance_id") == "INST-F")
    # harbor produced NO result.json (idle-watchdog / timeout kill / crash before
    # grading) -> harness_terminal.py run() sets no_result_death=true, still
    # reports /complete as done+resolved=0 (never /fail) — see that flag's docstring.
    q.complete("INST-F", "w1", patch="", report_json={"ok": 0},
               meta_json={"no_result_death": True}, resolved=0)
    r = row_of(q, "INST-F")
    check("no-verdict death lands as 'done' resolved=0 (harness reports a clean /complete)",
          r["status"] == "done" and r["resolved"] == 0, f"status={r['status']} resolved={r['resolved']}")

    check("fresh queue drained (pending==0)", q._counts().get("pending") == 0)
    claim2 = q.claim("w2")
    check("failure-rerun re-leased the no-verdict-death row",
          claim2.get("instance_id") == "INST-F", f"claim={claim2}")
    check("fail_reruns bumped to 1", row_of(q, "INST-F")["fail_reruns"] == 1)

    # The rerun ALSO produces no verdict — proves the budget caps a SECOND
    # no-result death on the same instance, not just the first flag-shape's count.
    q.complete("INST-F", "w2", patch="", report_json={"ok": 0},
               meta_json={"no_result_death": True}, resolved=0)
    r = row_of(q, "INST-F")
    check("rerun also no-verdict -> 'done' resolved=0, fail_reruns stays 1 (not re-bumped)",
          r["status"] == "done" and r["resolved"] == 0 and r["fail_reruns"] == 1)
    check("budget spent -> claim returns {done:true}, NOT requeued a 2nd time",
          q.claim("w3").get("done") is True)
    os.unlink(db)


if __name__ == "__main__":
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()
    print()
    if FAILS:
        print(f"RESULT: {len(FAILS)} assertion(s) FAILED: {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASS — failure-rerun fires and the rerun's outcome persists over the initial failure.")
