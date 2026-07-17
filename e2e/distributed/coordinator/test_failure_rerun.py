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


if __name__ == "__main__":
    scenario_a()
    scenario_b()
    print()
    if FAILS:
        print(f"RESULT: {len(FAILS)} assertion(s) FAILED: {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASS — failure-rerun fires and the rerun's outcome persists over the initial failure.")
