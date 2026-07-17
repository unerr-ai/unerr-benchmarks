#!/usr/bin/env python3
"""SWE-bench-Live grade adapter — delegated to by Worker._grade_via_module
(worker-loop.py) when the active benchmark descriptor sets
grade_module="grade_live" (benchmarks.py _LIVE_VERIFIED).

Runs SWE-bench-Live's OWN vendored evaluator (Dockerfile.dist clones
microsoft/SWE-bench-Live into /work/swebench-live at a pinned commit and
installs it editable — see that file for the exact bake lines) for ONE
instance via `python -m evaluation.evaluation`, then TRUSTS the harness's own
per-instance report.json "resolved" verdict rather than recomputing it from
raw FAIL_TO_PASS/PASS_TO_PASS lists — unlike grade_pro, which has to work
around a key-casing bug in its vendored dataset, SWE-bench-Live's own
evaluator has no such gap to route around here.

Mirrors grade_pro's structure and best-effort contract: any crash/timeout ->
(False, <diagnostic text>) with worker.log, never raise (a grader failure
must not sink an otherwise-valid patch's /complete).
"""
from __future__ import annotations

import json
import os
import subprocess

# swebench-live's `evaluation.evaluation` runs from its OWN isolated venv
# /work/.venv-live (Dockerfile.dist): SWE-bench-Live's package is named
# `swebench` v1.0.0, so it must NOT share the main /work/.venv, which holds the
# real swebench>=4.1 the Verified/Pro grade path needs. Overridable via VENV_PY.
VENV_PY = os.environ.get("VENV_PY", "/work/.venv-live/bin/python")


def _live_dir() -> str:
    """Resolve the vendored SWE-bench-Live harness dir. SWEBENCH_LIVE_DIR
    overrides for local/dev runs where the harness lives elsewhere; the fleet
    image always has it at /work/swebench-live (Dockerfile.dist git clone)."""
    return os.environ.get("SWEBENCH_LIVE_DIR", "/work/swebench-live")


def _tail_eval_log(worker, iid: str, eval_log: str, n: int = 30) -> str:
    """Echo the last `n` lines of the captured evaluation.evaluation output
    into worker.log (the only surviving diagnostic once the fleet is torn
    down) and return that same tail so a harness-error return value can carry
    it as its report_text diagnostic."""
    try:
        with open(eval_log, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        worker.log(f"{iid}: grade eval log unreadable at {eval_log}")
        return ""
    tail = lines[-n:]
    worker.log(f"{iid}: grade eval log tail ({len(lines)} lines total):")
    for line in tail:
        worker.log(f"{iid}:   | {line.rstrip()}")
    return "".join(tail)


def grade(worker, iid: str, scratch: str, preds_path: str,
          model_name: str | None) -> tuple[bool, str]:
    """Grade one SWE-bench-Live prediction for `iid` and return
    (resolved, report_text). Best-effort throughout: any crash/timeout yields
    (False, <diagnostic>) so a valid patch still /completes rather than
    sinking the instance."""
    try:
        # Still grade an empty patch — the harness marks it empty_patch (not
        # resolved) rather than us short-circuiting before it ever runs.
        patch, _ = worker._read_patch(preds_path, iid)

        live_dir = _live_dir()
        grade_cwd = os.path.join(scratch, "grade")
        os.makedirs(grade_cwd, exist_ok=True)

        # evaluation.evaluation's --patch_dir wants {iid: {model_patch}} for
        # exactly this instance, not run-benchmark's full preds.json shape.
        live_preds_path = os.path.join(grade_cwd, "live_preds.json")
        with open(live_preds_path, "w", encoding="utf-8") as f:
            json.dump({iid: {"model_patch": patch}}, f)

        output_dir = os.path.join(grade_cwd, "out")
        os.makedirs(output_dir, exist_ok=True)

        cmd = [
            VENV_PY, "-m", "evaluation.evaluation",
            "--dataset", worker.dataset,
            "--platform", "linux",
            "--split", worker.split,
            "--patch_dir", live_preds_path,
            "--output_dir", output_dir,
            "--workers", "1",
            "--overwrite", "1",
            "--instance_ids", iid,
        ]
        worker.log(f"{iid}: grade -> {' '.join(cmd)}")
        # Capture the evaluator's stdout+stderr to a file (it drives docker
        # build/run for the test suite) — mirrors grade_pro's eval_log so a
        # grade that produced no report.json isn't a black box once the fleet
        # tears down.
        eval_log = os.path.join(grade_cwd, "eval-output.log")
        # launch/ (a submodule) + evaluation/ are imported as source packages
        # rooted at live_dir; put it on PYTHONPATH so the `-m` import resolves
        # them regardless of how the editable install mapped its packages.
        live_env = os.environ.copy()
        live_env["PYTHONPATH"] = live_dir + os.pathsep + live_env.get("PYTHONPATH", "")
        try:
            with open(eval_log, "wb") as lf:
                subprocess.run(cmd, cwd=live_dir, env=live_env,
                               timeout=worker.timeout + 600,
                               stdout=lf, stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired:
            tail = _tail_eval_log(worker, iid, eval_log)
            worker.log(f"{iid}: grade timed out — treating as unresolved")
            return False, f"{iid}: grade timed out; harness output tail:\n{tail}"

        # Trust ONLY report.json's own "resolved" verdict — SWE-bench-Live's
        # official evaluation logic, never recomputed from raw test lists here.
        report_path = os.path.join(output_dir, iid, "report.json")
        if os.path.isfile(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                report_text = f.read()
            try:
                report = json.loads(report_text)
                resolved = bool(report.get("resolved")) if isinstance(report, dict) else False
            except Exception:
                resolved = False
            return resolved, report_text

        # No per-instance report.json -> harness error path. The fallback below
        # only picks WHAT text to carry as report_text; the verdict stays False.
        tail = _tail_eval_log(worker, iid, eval_log)
        worker.log(f"{iid}: no report.json at {report_path} — treating as unresolved")
        results_path = os.path.join(output_dir, "results.json")
        if os.path.isfile(results_path):
            with open(results_path, "r", encoding="utf-8") as f:
                report_text = f.read()
        else:
            report_text = (f"{iid}: no report.json at {report_path}; "
                            f"harness output tail:\n{tail}")
        return False, report_text
    except Exception as e:  # noqa: BLE001 — best-effort: any crash -> unresolved
        worker.log(f"{iid}: grade_live crashed ({e}) — treating as unresolved")
        return False, f"{iid}: grade_live crashed: {e}"
