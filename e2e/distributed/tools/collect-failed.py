#!/usr/bin/env python3
"""Collect every failed (resolved=0) SWE-bench instance, PLUS every dead-lettered
(never-graded) instance, from a finished run's bundle into a durable, date-named
triage archive so a failure stays fully triageable long after the ephemeral
worker/coordinator machines are destroyed.

Auto-invoked at the end of run-distributed.sh (writes failed-runs/<label>/), and
runnable standalone against any out/dist-<label>/bundle:

    tools/collect-failed.py --bundle out/dist-<label>/bundle --dest failed-runs/<label>

For each failed instance it writes: model_patch.diff (what the agent produced),
report.json (raw grade), WHY_FAILED.txt (which FAIL_TO_PASS tests still fail /
PASS_TO_PASS regressions), engine.log, err.txt, events.jsonl, opencode.db — plus
an INDEX.md. For each dead instance (read from results/<label>/dead.jsonl) it
writes DEAD_REASON.txt with the captured failure_reason, so a 175-instance
dead-letter run no longer needs SSH+grep of server.log to diagnose. Best-effort:
never raises into the runner (main() catches + reports).
"""
import argparse
import json
import os
import shutil
import sys


def _load_preds(bundle, label):
    """iid -> model_patch, parsed from the run's preds.json (dict or list form)."""
    p = os.path.join(bundle, "results", label, "preds.json")
    out = {}
    if os.path.exists(p):
        with open(p) as f:
            data = json.load(f)
        rows = data.values() if isinstance(data, dict) else data
        for r in rows:
            if isinstance(r, dict) and r.get("instance_id"):
                out[r["instance_id"]] = r.get("model_patch") or ""
    return out


def _load_dead(bundle, label):
    """iid -> {failure_reason, attempt_count, worker_id, last_heartbeat}, parsed
    from results/<label>/dead.jsonl — the plain-file dump coordinator-entrypoint.sh
    writes while the WAL-mode queue DB is still live (an offline `sqlite3
    queue.db` copy in the bundle sees zero dead rows, since the -wal sidecar
    isn't bundled), so this file is the only durable record of a dead instance."""
    p = os.path.join(bundle, "results", label, "dead.jsonl")
    out = {}
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except (TypeError, ValueError):
                    continue
                iid = d.get("instance_id")
                if iid:
                    out[iid] = d
    return out


def _detect_label(bundle, label):
    if label:
        return label
    root = os.path.join(bundle, "results")
    subs = sorted(d for d in os.listdir(root)) if os.path.isdir(root) else []
    return subs[0] if subs else "unknown"


def collect(bundle, label, dest):
    label = _detect_label(bundle, label)
    grade = os.path.join(bundle, "logs", "grade-merged")
    art = os.path.join(bundle, "results", label, "artifacts")
    preds = _load_preds(bundle, label)
    dead = _load_dead(bundle, label)

    failed = []
    if os.path.isdir(grade):
        for iid in sorted(os.listdir(grade)):
            rep = os.path.join(grade, iid, "report.json")
            if not os.path.exists(rep):
                continue
            with open(rep) as f:
                rj = json.load(f)
            entry = rj.get(iid, rj)
            if entry.get("resolved") is False:
                failed.append(iid)

    os.makedirs(dest, exist_ok=True)
    idx = ["# Failed-instance triage — run %s" % label, ""]
    if not failed and not dead:
        idx += ["All instances resolved — no failures. ", ""]
        with open(os.path.join(dest, "INDEX.md"), "w") as f:
            f.write("\n".join(idx))
        print("collect-failed: 0 failed instances for %s -> %s" % (label, dest))
        return 0

    if failed:
        idx += ["%d instance(s) graded `resolved=0` (produced a patch but did not "
                "pass the hidden tests, or worse).\n" % len(failed)]
    for iid in failed:
        d = os.path.join(dest, iid)
        os.makedirs(d, exist_ok=True)
        copied = []
        for fn in ("engine.log", "err.txt", "events.jsonl", "opencode.db"):
            s = os.path.join(art, iid, fn)
            if os.path.exists(s):
                shutil.copy2(s, os.path.join(d, fn))
                copied.append(fn)
        tests = {}
        rep = os.path.join(grade, iid, "report.json")
        if os.path.exists(rep):
            shutil.copy2(rep, os.path.join(d, "report.json"))
            copied.append("report.json")
            with open(rep) as f:
                entry = json.load(f)
            entry = entry.get(iid, entry)
            ts = entry.get("tests_status", {})
            f2p, p2p = ts.get("FAIL_TO_PASS", {}), ts.get("PASS_TO_PASS", {})
            tests = {
                "pe": entry.get("patch_exists"),
                "pa": entry.get("patch_successfully_applied"),
                "f2p_ok": len(f2p.get("success", [])), "f2p_bad": f2p.get("failure", []),
                "p2p_ok": len(p2p.get("success", [])), "p2p_bad": p2p.get("failure", []),
            }
        patch = preds.get(iid, "")
        with open(os.path.join(d, "model_patch.diff"), "w") as f:
            f.write(patch or "(no patch in preds.json)\n")
        copied.append("model_patch.diff")
        with open(os.path.join(d, "WHY_FAILED.txt"), "w") as f:
            f.write("instance: %s\n" % iid)
            if tests:
                f.write("patch_exists=%s  patch_applied=%s  resolved=False\n" % (tests["pe"], tests["pa"]))
                f.write("\nFAIL_TO_PASS: %d passed, %d still failing\n" % (tests["f2p_ok"], len(tests["f2p_bad"])))
                for t in tests["f2p_bad"]:
                    f.write("  FAIL  %s\n" % t)
                f.write("\nPASS_TO_PASS: %d passed, %d regressed\n" % (tests["p2p_ok"], len(tests["p2p_bad"])))
                for t in tests["p2p_bad"]:
                    f.write("  REGRESSED  %s\n" % t)
            else:
                f.write("(no report.json found)\n")
        if tests:
            why = "patch applied, %d/%d FAIL_TO_PASS still failing%s" % (
                len(tests["f2p_bad"]), tests["f2p_ok"] + len(tests["f2p_bad"]),
                ", %d PASS_TO_PASS regressed" % len(tests["p2p_bad"]) if tests["p2p_bad"] else "")
        else:
            why = "no report"
        idx += ["## %s" % iid, "- **why:** %s" % why, "- files: %s" % ", ".join(copied), ""]

    if dead:
        idx += ["## Dead-lettered instances (%d)" % len(dead),
                "Never reached grading — pre-container crash or attempts exhausted. "
                "See DEAD_REASON.txt per instance for the captured error.\n"]
        for iid in sorted(dead):
            d = dead[iid]
            reason = d.get("failure_reason") or "(no failure_reason recorded)"
            ddir = os.path.join(dest, iid)
            os.makedirs(ddir, exist_ok=True)
            with open(os.path.join(ddir, "DEAD_REASON.txt"), "w") as f:
                f.write("instance: %s\n" % iid)
                f.write("attempt_count: %s\n" % d.get("attempt_count"))
                f.write("worker_id: %s\n" % d.get("worker_id"))
                f.write("last_heartbeat: %s\n" % d.get("last_heartbeat"))
                f.write("\nfailure_reason:\n%s\n" % reason)
            idx += ["### %s" % iid,
                    "- **status:** dead (attempt_count=%s)" % d.get("attempt_count"),
                    "- **reason:** %s" % reason.replace("\n", " |- ")[:300],
                    ""]

    with open(os.path.join(dest, "INDEX.md"), "w") as f:
        f.write("\n".join(idx))
    print("collect-failed: %d failed, %d dead instance(s) -> %s" % (len(failed), len(dead), dest))
    return len(failed) + len(dead)


def main():
    ap = argparse.ArgumentParser(description="Archive failed SWE-bench instances for triage.")
    ap.add_argument("--bundle", required=True, help="path to the extracted run bundle (out/dist-<label>/bundle)")
    ap.add_argument("--label", default=None, help="run label; auto-detected from bundle/results/* if omitted")
    ap.add_argument("--dest", required=True, help="output triage dir (e.g. failed-runs/<label>)")
    a = ap.parse_args()
    try:
        collect(a.bundle, a.label, a.dest)
    except Exception as e:  # never break the runner's teardown
        print("collect-failed: skipped (%s: %s)" % (type(e).__name__, e), file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
