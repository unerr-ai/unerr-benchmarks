#!/usr/bin/env python3
"""SWE-bench Pro grade adapter — delegated to by Worker._grade_via_module
(worker-loop.py) when the active benchmark descriptor sets
grade_module="grade_pro" (benchmarks.py _PRO).

Runs Scale AI's vendored evaluator (e2e/distributed/swebench-pro/, baked into
the fleet image — see that dir's contents + Dockerfile.dist for the exact bake
lines) for ONE instance against our private image mirror
(51jaswanth15/sweap-images:<dockerhub_tag>, see e2e/swebench-pro/README.md),
then computes the resolved verdict OURSELVES from the vendored dataset row's
FAIL_TO_PASS/PASS_TO_PASS and the evaluator's per-instance output.json. We do
NOT trust the evaluator's own eval_results.json: its accuracy loop reads
raw_sample["fail_to_pass"]/["pass_to_pass"] (lowercase) but our vendored
sweap_eval_full_v2.jsonl only carries FAIL_TO_PASS/PASS_TO_PASS (uppercase),
so that column would KeyError -> silently graded False for every instance if
relied upon.

Mirrors Worker._grade_swebench's structure and best-effort contract: any
crash/timeout -> (False, "") with worker.log, never raise (a grader failure
must not sink an otherwise-valid patch's /complete).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess

# Vendored eval surface root (swe_bench_pro_eval.py, helper_code/image_uri.py,
# dockerfiles/, run_scripts/, sweap_eval_full_v2.jsonl). In the fleet image
# Dockerfile.dist COPYs `distributed/swebench-pro` -> `/work/swebench-pro` — the
# `distributed/` path level is FLATTENED away — and benchmarks._PRO["ids_jsonl"]
# points resolve at exactly /work/swebench-pro/sweap_eval_full_v2.jsonl. Grade MUST
# read the SAME tree, so _pro_dir() derives the dir from the active descriptor's
# ids_jsonl (single source of truth with resolve), preferring an explicit
# SWEBENCH_PRO_DIR override, then falling back to a repo-checkout path relative to
# this file. The prior code used ONLY that relative fallback, which resolves to
# /work/distributed/swebench-pro in the image — a path the COPY never creates — so
# every grade read OSError'd and every Pro instance graded False (the silent
# grade=0/0 bug). See the econ-pro-resolve-wiring memory.
_REPO_PRO_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swebench-pro")


def _pro_dir(worker=None) -> str:
    """Resolve the vendored SWE-bench-Pro eval dir the same way resolve does.
    Order: explicit SWEBENCH_PRO_DIR env override -> the active descriptor's
    ids_jsonl directory (what run-benchmark.py resolved against) -> repo-checkout
    fallback relative to this file."""
    env = os.environ.get("SWEBENCH_PRO_DIR")
    if env:
        return env
    ids = (getattr(worker, "bench", None) or {}).get("ids_jsonl") if worker else None
    if ids:
        return os.path.dirname(ids)
    return _REPO_PRO_DIR

# swe_bench_pro_eval.py and the swebench harness both live in the image's
# /work/.venv (Dockerfile.dist) — mirrors worker-loop.py's own VENV_PY.
VENV_PY = os.environ.get("VENV_PY", "/work/.venv/bin/python")

# Private Docker Hub mirror of Scale AI's per-instance eval images (e2e/swebench-pro/
# mirror-sweap-images.sh). helper_code/image_uri.py forces the ref to
# {dockerhub_username}/sweap-images:{dockerhub_tag} — overridable for testing.
PRO_DOCKERHUB_USERNAME = os.environ.get("PRO_DOCKERHUB_USERNAME", "51jaswanth15")


def _as_list(v):
    """FAIL_TO_PASS/PASS_TO_PASS ride the vendored jsonl inconsistently typed
    per row — sometimes a real JSON list, sometimes a stringified Python-list
    literal. Normalize both shapes to a plain list; unparseable -> []."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return []
        return list(parsed) if isinstance(parsed, (list, tuple, set)) else []
    return []


def _find_row(iid: str, eval_jsonl: str) -> dict | None:
    """Linear-scan the vendored jsonl (~700 rows) for iid's dataset row."""
    try:
        with open(eval_jsonl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("instance_id") == iid:
                    return obj
    except OSError:
        return None
    return None


def _tail_eval_log(worker, iid: str, eval_log: str, n: int = 30) -> None:
    """Echo the last `n` lines of the captured swe_bench_pro_eval output into
    worker.log so a grade that produced no output.json isn't a black box (the
    only surviving diagnostic once the fleet is torn down)."""
    try:
        with open(eval_log, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        worker.log(f"{iid}: grade eval log unreadable at {eval_log}")
        return
    worker.log(f"{iid}: grade eval log tail ({len(lines)} lines total):")
    for line in lines[-n:]:
        worker.log(f"{iid}:   | {line.rstrip()}")


def grade(worker, iid: str, scratch: str, preds_path: str,
          model_name: str | None) -> tuple[bool, str]:
    """Grade one Pro prediction for `iid` and return (resolved, report_text).
    Best-effort throughout: any crash/timeout yields (False, "") so a valid
    patch still /completes rather than sinking the instance."""
    try:
        patch, _ = worker._read_patch(preds_path, iid)
        if not patch:
            return False, ""

        # Resolve the vendored eval tree the same way resolve did (descriptor
        # ids_jsonl dir) — NOT a path relative to this file, which mislands in the
        # image (see _pro_dir). eval_jsonl is the exact jsonl resolve indexed.
        pro_dir = _pro_dir(worker)
        eval_jsonl = os.path.join(pro_dir, "sweap_eval_full_v2.jsonl")

        row = _find_row(iid, eval_jsonl)
        if row is None:
            worker.log(f"{iid}: grade_pro found no dataset row in {eval_jsonl} — unresolved")
            return False, ""

        grade_cwd = os.path.join(scratch, "grade")
        os.makedirs(grade_cwd, exist_ok=True)
        # create_entryscript() in swe_bench_pro_eval.py reads
        # dockerfiles/{base,instance}_dockerfile/<iid>/Dockerfile via a path
        # RELATIVE to CWD — symlink the vendored copy in so the per-instance
        # scratch CWD (mirrors _grade_swebench's grade_cwd) still resolves it.
        try:
            os.symlink(os.path.join(pro_dir, "dockerfiles"),
                       os.path.join(grade_cwd, "dockerfiles"))
        except OSError:
            pass  # missing symlink surfaces as a per-instance eval failure below

        output_dir = os.path.join(grade_cwd, "out")
        os.makedirs(output_dir, exist_ok=True)
        raw_sample_path = os.path.join(grade_cwd, "raw_sample.jsonl")
        patch_path = os.path.join(grade_cwd, "patch.json")
        with open(raw_sample_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        with open(patch_path, "w", encoding="utf-8") as f:
            json.dump([{"instance_id": iid, "model_patch": patch}], f)

        cmd = [
            VENV_PY, os.path.join(pro_dir, "swe_bench_pro_eval.py"),
            "--raw_sample_path", raw_sample_path,
            "--patch_path", patch_path,
            "--output_dir", output_dir,
            "--scripts_dir", os.path.join(pro_dir, "run_scripts"),
            "--dockerhub_username", PRO_DOCKERHUB_USERNAME,
            "--use_local_docker",
            "--num_workers", "1",
        ]
        worker.log(f"{iid}: grade -> {' '.join(cmd)}")
        # Capture the evaluator's stdout+stderr to a file (it drives docker
        # build/run for the test suite). Without this the eval output inherits
        # worker stdout and vanishes on fleet teardown, so a grade that yields no
        # output.json (the silent unresolved path below) is undebuggable — we tail
        # this log into worker.log on every non-success exit.
        eval_log = os.path.join(grade_cwd, "eval-output.log")
        try:
            with open(eval_log, "wb") as lf:
                subprocess.run(cmd, cwd=grade_cwd, timeout=worker.timeout + 600,
                               stdout=lf, stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired:
            _tail_eval_log(worker, iid, eval_log)
            worker.log(f"{iid}: grade timed out — treating as unresolved")
            return False, ""

        output_path = os.path.join(output_dir, iid, "_output.json")
        if not os.path.isfile(output_path):
            _tail_eval_log(worker, iid, eval_log)
            worker.log(f"{iid}: no output.json at {output_path} — treating as unresolved")
            return False, ""
        with open(output_path, "r", encoding="utf-8") as f:
            output = json.load(f)

        passed = {t["name"] for t in output.get("tests", []) if t.get("status") == "PASSED"}
        f2p, p2p = _as_list(row.get("FAIL_TO_PASS")), _as_list(row.get("PASS_TO_PASS"))
        resolved = (set(f2p) | set(p2p)) <= passed
        report = {
            "instance_id": iid,
            "resolved": resolved,
            "fail_to_pass": f2p,
            "pass_to_pass": p2p,
            "passed_tests": sorted(passed),
            "tests": output.get("tests", []),
        }
        return resolved, json.dumps(report)
    except Exception as e:  # noqa: BLE001 — best-effort: any crash -> unresolved
        worker.log(f"{iid}: grade_pro crashed ({e}) — treating as unresolved")
        return False, ""
