#!/usr/bin/env python3
"""Collect every failed (resolved=0) SWE-bench instance, PLUS every dead-lettered
(never-graded) instance, from a finished run's bundle into a durable, date-named
triage archive so a failure stays fully triageable long after the ephemeral
worker/coordinator machines are destroyed.

Auto-invoked at the end of run-distributed.sh (writes failed-runs/<label>/), and
runnable standalone against any out/dist-<label>/bundle:

    tools/collect-failed.py --bundle out/dist-<label>/bundle --dest failed-runs/<label>

For each failed instance it writes: model_patch.diff (what the agent produced,
'' for a harness_run benchmark — see below), report.json (raw grade),
WHY_FAILED.txt, plus whichever of engine.log/err.txt/events.jsonl/opencode.db/
trajectory.json/sessions.cast/harbor-run.log/claude-session.jsonl actually exist
— an INDEX.md ties it together. WHY_FAILED.txt's content is BENCHMARK-AWARE:
run-distributed.sh never passes this tool a --benchmark flag, so the flow is
inferred per-instance from the report.json shape itself (presence of a
"harbor_result" key — the field only harness_terminal.py's run() ever writes —
rather than string-matching the run label/benchmark name). resolve_then_grade
benchmarks (SWE-bench Verified/Pro/Live) get the original FAIL_TO_PASS/
PASS_TO_PASS/patch_exists report; harness_run benchmarks (Terminal-Bench via
Harbor) get the real signal instead: Harbor reward, whether the trial
COMPLETED or was KILLED mid-run, which trace artifacts survived, and — from
trajectory.json when present — whether the session ended on the agent (a
genuine capability miss) or on an unanswered nudge (a transient silent
session death, see DEBUG_FAILED_TASK.md). For each dead instance (read from
results/<label>/dead.jsonl) it writes DEAD_REASON.txt with the captured
failure_reason, so a 175-instance dead-letter run no longer needs SSH+grep of
server.log to diagnose. Best-effort: never raises into the runner (main()
catches + reports).
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import benchmarks  # noqa: E402  — sibling module, the flow/traces contract WHY_FAILED.txt reads

# The harness_run trace filenames, sourced from the descriptor (benchmarks.py
# `_TERMINAL["traces"]`) rather than hardcoded, so a future harness_run
# benchmark's own trace list is picked up automatically. Falls back to the
# terminal set if the registry ever changes shape underneath this (best-effort
# tool, must never crash on an otherwise-working bundle).
try:
    _HARNESS_TRACE_FILES = tuple(fn for fn, _ in benchmarks.get("terminal")["traces"])
except Exception:
    _HARNESS_TRACE_FILES = (
        "events.jsonl", "err.txt", "trajectory.json", "sessions.cast",
        "harbor-run.log", "claude-session.jsonl",
    )

# Every artifact filename collect() will copy into an instance's triage dir —
# the resolve_then_grade set plus whichever harness_run ones aren't already
# in it, preserving the original order for that first group. A file that
# doesn't exist for a given instance's flow is simply skipped (os.path.exists
# guard below), so this union is safe for both flows.
_ARTIFACT_COPY_FILES = ("engine.log", "err.txt", "events.jsonl", "opencode.db") + tuple(
    fn for fn in _HARNESS_TRACE_FILES if fn not in ("err.txt", "events.jsonl")
)


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


def _is_harness_report(entry):
    """True when a report.json entry came from a harness_run benchmark
    (benchmarks.FLOW_HARNESS_RUN — currently only Terminal-Bench/Harbor) rather
    than resolve_then_grade. harness_terminal.py's run() is the only writer
    that ever puts a "harbor_result" key in the per-instance report shape
    (the swebench/grade_pro/grade_live graders never do) — that's the real
    field this branches on, not the run label or benchmark name, since
    run-distributed.sh calls this tool with no --benchmark flag to match."""
    return isinstance(entry, dict) and "harbor_result" in entry


def _reward_from_harbor(hr):
    """Best-effort reward extraction from a Harbor result.json, mirroring
    harness_terminal._extract_reward's two shapes: trial-level
    (verifier_result.rewards.reward, written once a trial COMPLETES) and
    job-level (stats.evals[*].reward_stats/metrics, all that exists for a
    trial killed mid-run). Reimplemented rather than imported — this is a
    post-hoc triage tool, not part of the run path, and importing
    harness_terminal would pull in its harbor/subprocess-oriented module-level
    setup for no benefit here."""
    if not isinstance(hr, dict):
        return None
    vr = (hr.get("verifier_result") or {}).get("rewards") or {}
    if isinstance(vr.get("reward"), (int, float)):
        return float(vr["reward"])
    best = None
    for ev in ((hr.get("stats") or {}).get("evals") or {}).values():
        if not isinstance(ev, dict):
            continue
        rs = (ev.get("reward_stats") or {}).get("reward") or {}
        for k, ids in rs.items():
            if ids:
                try:
                    best = max(best if best is not None else float("-inf"), float(k))
                except (TypeError, ValueError):
                    pass
        for m in (ev.get("metrics") or []):
            if isinstance(m, dict) and isinstance(m.get("mean"), (int, float)):
                best = max(best if best is not None else float("-inf"), float(m["mean"]))
    return best


def _harness_trial_killed(rc, hr):
    """True if the Harbor trial was killed mid-run (never completed) rather
    than finishing — with a good or bad outcome. Signature confirmed live
    (caffe-cifar-10, 2026-07-21, exhausted [agent] timeout_sec — see
    DEBUG_FAILED_TASK.md): our own subprocess rc stays None (harness_terminal's
    run() only assigns it inside the clean-exit branch of its wait loop, never
    on the idle-watchdog/timeout/abandon kill branches), and Harbor's job-level
    result.json (the only one written — the trial-level one needs a COMPLETED
    trial) carries finished_at=None with stats.n_completed_trials=0 and
    stats.n_running_trials>=1. A killed trial is a fundamentally different
    diagnosis from a completed-but-wrong one and must not be reported the
    same way."""
    if rc is not None or not isinstance(hr, dict):
        return False
    if hr.get("finished_at") is not None:
        return False
    stats = hr.get("stats") or {}
    return stats.get("n_completed_trials") == 0 and (stats.get("n_running_trials") or 0) >= 1


def _last_trajectory_source(traj_path):
    """steps[-1]['source'] from a Harbor/Claude trajectory.json ('agent' or
    'user') — confirmed against real traces: chess-best-move ends 'agent' (it
    answered and was graded wrong — a genuine capability miss), while
    build-pmars/build-cython-ext (2026-07-21) ended 'user' — Claude Code's own
    "[Your previous response had no visible output...]" nudge went unanswered,
    a TRANSIENT silent session death, not a capability miss (both later passed
    on an unchanged rerun). Returns None if trajectory.json is absent, empty,
    unreadable, or has no steps — the caller must treat that as "unknown",
    never as either verdict."""
    if not os.path.exists(traj_path):
        return None
    try:
        with open(traj_path, encoding="utf-8") as f:
            traj = json.load(f)
    except (OSError, ValueError):
        return None
    steps = traj.get("steps") if isinstance(traj, dict) else None
    if not steps or not isinstance(steps[-1], dict):
        return None
    return steps[-1].get("source")


def _why_failed_harness(iid, entry, art_dir):
    """Build the harness_run WHY_FAILED.txt body (everything after the shared
    "instance: <iid>" line collect() already writes) + a one-line INDEX.md
    summary. Harbor grades in-container on reward, not test transitions, and
    there is no patch — so the resolve_then_grade fields (FAIL_TO_PASS/
    patch_exists) are structurally meaningless here and must never be printed;
    this reports the real signal instead: reward, killed-vs-completed, which
    trace artifacts survived, and (from trajectory.json) whether the session
    ended on the agent or on an unanswered user-role nudge.
    @sem domain=benchmark-triage role=reporting
    """
    rc = entry.get("rc")
    hr = entry.get("harbor_result") or {}
    reward = _reward_from_harbor(hr)
    exceptions = entry.get("exceptions") or []
    killed = _harness_trial_killed(rc, hr)
    stats = hr.get("stats") or {}

    lines = [
        "flow: harness_run (Harbor grades in-container — no patch, no test transitions)",
        "resolved=False  harbor_reward=%s  rc=%s" % (reward, rc),
    ]
    if exceptions:
        lines.append("exceptions: %s" % ", ".join(exceptions))
    lines.append("")
    if killed:
        lines += [
            "trial status: KILLED MID-RUN — never completed (this is NOT a wrong answer).",
            "  finished_at=%r n_running_trials=%r n_completed_trials=%r"
            % (hr.get("finished_at"), stats.get("n_running_trials"), stats.get("n_completed_trials")),
            "  Harbor's job-level result.json exists but the trial-level one (written ONLY on",
            "  completion) does not — the task was still running when it was reaped (task/agent",
            "  timeout, idle watchdog, or lease reap). Check the task budget before assuming a",
            "  capability miss.",
        ]
    else:
        lines.append("trial status: COMPLETED (reached a result — not killed mid-run).")
    lines.append("")

    present = {fn: os.path.exists(os.path.join(art_dir, fn)) for fn in _HARNESS_TRACE_FILES}
    lines.append("trace artifacts present: " + ", ".join(
        "%s=%s" % (fn, "yes" if ok else "no") for fn, ok in present.items()))
    lines.append("")

    traj_path = os.path.join(art_dir, "trajectory.json")
    last_src = _last_trajectory_source(traj_path) if present.get("trajectory.json") else None
    if last_src == "agent":
        lines += [
            "last trajectory step source: agent — the agent finished cleanly and was graded",
            "  wrong. Likely a genuine capability miss; rerunning wastes money unless the",
            "  killed/exceptions signals above say otherwise.",
        ]
    elif last_src == "user":
        lines += [
            "last trajectory step source: user — the session ended on an unanswered nudge",
            "  (Claude Code's own \"[Your previous response had no visible output...]\"). This is",
            "  a TRANSIENT silent session death, not a capability miss — worth a rerun before",
            "  further investigation.",
        ]
    elif present.get("trajectory.json"):
        lines.append("last trajectory step source: unknown (steps[] empty or unreadable).")
    else:
        lines += [
            "trajectory.json missing — cannot tell agent-finished from silent-death here.",
            "  If claude-session.jsonl is present, that trace survives a trial killed mid-run",
            "  (Harbor only writes trajectory.json on completion) — check it next.",
        ]

    lines += [
        "",
        "CAVEATS:",
        "- err.txt is Harbor's SETUP log (trial.log), not the agent transcript — it ENDS with",
        "  the full --append-system-prompt text. Grepping it for harness strings (unerr:verify,",
        "  Gate, escalate) yields FALSE POSITIVES (prompt text, not events that occurred). Use",
        "  trajectory.json / claude-session.jsonl for agent behavior.",
        "- Harbor's own final_metrics.total_cost_usd inside trajectory.json is NOT reliable",
        "  (list pricing, observed reporting 0 cached tokens) — real cost is LiteLLM spend.",
    ]

    if killed:
        why = "harbor trial KILLED mid-run (never completed) — rc=%s reward=%s" % (rc, reward)
    elif last_src == "user":
        why = "TRANSIENT silent session death (last step=user, unanswered nudge) — rerun-worthy"
    elif last_src == "agent":
        why = ("completed, harbor reward=%s — genuine capability miss "
               "(agent finished, graded wrong)" % reward)
    else:
        why = "completed, harbor reward=%s (trajectory unavailable — check claude-session.jsonl)" % reward

    return "\n".join(lines) + "\n", why


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
        art_dir = os.path.join(art, iid)
        for fn in _ARTIFACT_COPY_FILES:
            s = os.path.join(art_dir, fn)
            if os.path.exists(s):
                shutil.copy2(s, os.path.join(d, fn))
                copied.append(fn)
        tests = {}
        harness_entry = None
        rep = os.path.join(grade, iid, "report.json")
        if os.path.exists(rep):
            shutil.copy2(rep, os.path.join(d, "report.json"))
            copied.append("report.json")
            with open(rep) as f:
                entry = json.load(f)
            entry = entry.get(iid, entry)
            if _is_harness_report(entry):
                harness_entry = entry
            else:
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
        harness_body, harness_why = (None, None)
        if harness_entry is not None:
            harness_body, harness_why = _why_failed_harness(iid, harness_entry, art_dir)
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
            elif harness_body is not None:
                f.write(harness_body)
            else:
                f.write("(no report.json found)\n")
        if tests:
            why = "patch applied, %d/%d FAIL_TO_PASS still failing%s" % (
                len(tests["f2p_bad"]), tests["f2p_ok"] + len(tests["f2p_bad"]),
                ", %d PASS_TO_PASS regressed" % len(tests["p2p_bad"]) if tests["p2p_bad"] else "")
        elif harness_why is not None:
            why = harness_why
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
