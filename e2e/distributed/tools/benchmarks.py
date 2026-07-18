#!/usr/bin/env python3
"""Benchmark descriptor registry — the single source of truth for WHICH benchmark
a distributed run targets and HOW to resolve its ids, locate its images, grade a
prediction, time-box a task, and collect its traces.

Two ORTHOGONAL axes drive the fleet:

    ARM        which agent resolves the task    econ | claude      -> picks the RUNNER
    BENCHMARK  which task set + how to grade     verified|pro|terminal -> picks THIS descriptor

worker-loop.py already abstracts the ARM (the runner path: econ vs claude
run-benchmark.py). This module abstracts the BENCHMARK — everything that changes
when you point the SAME agent at a DIFFERENT task set:

    * id resolution   (which dataset / which ids a suite selector expands to)
    * image ref       (where the per-instance container image comes from)
    * flow            (resolve-then-grade  vs  a fused harness run)
    * grade           (how to score one prediction, and read "resolved")
    * timeout policy   (a deliberately never-binding ~24h cap applied ONLY to grade-side
                        subprocesses and harness_terminal's outer-wrapper fallback — the
                        resolve path itself enforces NO wall-clock; the agent owns its
                        own watchdog)
    * traces          (which artifact files to sync off the ephemeral VM)

suite.py imports `resolve_ids()` to turn a suite selector into the id list the
coordinator queue is seeded with. worker-loop.py imports `get(bench)` to grade,
time-box, and read traces per benchmark, and to branch on `flow`.

Design rules (match suite.py, which this generalizes):
  * ZERO third-party imports at module load. The heavy loaders (`datasets`, csv of
    a HF snapshot) are imported LAZILY inside the id-resolvers, so the smoke/mini
    path and the worker's grade/timeout/trace logic stay stdlib-only.
  * Plain data + small pure functions (no dataclasses-with-defaults gymnastics) so
    this runs identically in the slim image venv and on a laptop.
  * The `verified` descriptor reproduces worker-loop.py's CURRENT behaviour exactly
    — this is a refactor-to-data, not a behaviour change. New benchmarks are added
    alongside it; the Verified path must stay byte-for-byte equivalent in effect.
"""

import os
import sys


# ─────────────────────────────────────────────────────────────────────────────
# Flow kinds — the structural fork in how the worker runs one instance.
# ─────────────────────────────────────────────────────────────────────────────
# resolve_then_grade : the ARM runner produces preds.json (a git patch), THEN a
#                      separate benchmark grader scores it. SWE-bench Verified & Pro.
# harness_run        : the benchmark's OWN harness runs the agent inside its task
#                      container AND grades in one shot (no intermediate patch).
#                      Terminal-Bench / Harbor. The worker shells the harness and
#                      parses its result file; there is no swebench-style patch.
FLOW_RESOLVE_THEN_GRADE = "resolve_then_grade"
FLOW_HARNESS_RUN = "harness_run"


# ─────────────────────────────────────────────────────────────────────────────
# id resolution — each benchmark expands a suite selector to an ordered id list.
# Lazy-imported heavy deps live INSIDE these; the mini path is stdlib-only.
# ─────────────────────────────────────────────────────────────────────────────
def _hf_ids(dataset, split):
    """All instance_ids in an HF dataset split (needs the `datasets` package)."""
    try:
        from datasets import load_dataset
    except Exception as e:  # ImportError or a broken install
        sys.stderr.write(
            f"benchmarks: the `datasets` package is required for this suite "
            f"({dataset}): {e}\n"
            f"  pip install datasets   "
            f"(not needed for a *-mini suite or explicit --tasks/--file)\n"
        )
        sys.exit(2)
    ds = load_dataset(dataset, split=split)
    return [r["instance_id"] for r in ds]


def _jsonl_ids(path, id_field="instance_id"):
    """instance_ids from a local JSONL snapshot (Pro ships sweap_eval_full_v2.jsonl).
    Used when the dataset is vendored into the image rather than pulled from HF."""
    import json
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            v = obj.get(id_field)
            if v:
                out.append(v)
    return out


# SWE-bench Verified Mini-10 django ids (RUNBOOK §5) — the proven smoke set, kept
# hardcoded so the mini path needs no HF download. The 5-id smoke subset is the
# first five (one file each; fast, all django so one base image warms them).
_VERIFIED_MINI10 = [
    f"django__django-{n}"
    for n in ("11790", "11815", "11848", "11880", "11885",
              "11951", "11964", "11999", "12039", "12050")
]

# SWE-bench Live (verified split) 5-id smoke set — the head of the frozen verified
# split, hardcoded so the mini path stays HF-free (mirrors _VERIFIED_MINI10).
_LIVE_VERIFIED_MINI5 = [
    "pylint-dev__pylint-9771",
    "pylint-dev__pylint-9772",
    "jupyterlab__jupyter-ai-879",
    "aws-cloudformation__cfn-lint-3475",
    "pylint-dev__pylint-9785",
]

# Pro / Terminal mini ids are filled from the vendored snapshot the first time a
# *-mini suite is requested (so we don't hardcode ids that may not be mirrored).
# A run can always override with explicit --tasks/--file.
_PRO_MINI_FALLBACK = []      # populated by resolve_ids('pro-mini') from the jsonl head
_TERMINAL_MINI_FALLBACK = []  # populated from the vendored tb task list


# ─────────────────────────────────────────────────────────────────────────────
# Descriptors. One dict per benchmark; the fields below are the CONTRACT every
# consumer (suite.py, worker-loop.py, run-distributed.sh via env) reads.
# ─────────────────────────────────────────────────────────────────────────────
#
# key            canonical benchmark key (also the app-scoping token)
# aliases        alternate selectors that map here
# flow           FLOW_* — how the worker runs one instance
# dataset        default HF dataset name (resolve_then_grade grade reads it too)
# split          default split
# grade_dataset  dataset arg the grader wants (may differ from resolve dataset)
# mini_suite     the suite key that yields the 5-id smoke set for this benchmark
# image          human note on where per-instance images come from (docs/preflight)
# timeout        {"default_env": <env var>, "default": <s>} — a deliberately
#                 never-binding ~24h cap. It applies ONLY to grade-side subprocesses
#                 (grade_pro/grade_live use worker.timeout + 600; swebench
#                 run_evaluation --timeout) and harness_terminal's outer-wrapper
#                 fallback. The resolve path itself enforces NO wall-clock — the
#                 agent owns its own watchdog.
# traces         ordered list of (artifact filename, /complete field) to sync
#
# Consumers treat a MISSING optional key as "use the Verified default", so adding a
# benchmark never breaks an older worker that predates one of its fields.

_VERIFIED = {
    "key": "verified",
    "aliases": ("full", "swebench", "swe-bench", "swebench-verified"),
    "flow": FLOW_RESOLVE_THEN_GRADE,
    "dataset": "princeton-nlp/SWE-bench_Verified",
    "split": "test",
    "grade_dataset": "princeton-nlp/SWE-bench_Verified",
    "mini_suite": "verified-mini",
    "image": (
        "swebench harness builds/pulls swebench/sweb.eval.x86_64.<key> (key = iid "
        "with __ -> _1776_). Optional pull-through cache via SWEBENCH_REGISTRY_MIRROR; "
        "optional private mirror via SWEBENCH_NAMESPACE=51jaswanth15 (grade adds "
        "--namespace; mirror swebench/sweb.eval.x86_64.* -> 51jaswanth15/sweb.eval.x86_64.*, "
        "many-repos-one-tag, via e2e/swebench-pro/mirror-sweap-images.sh DATASET=verified)."
    ),
    "timeout": {
        "default_env": "PER_INSTANCE_TIMEOUT",
        # 24h — a never-binding cap on the grade-side subprocess only (swebench
        # run_evaluation --timeout). The resolve path enforces NO wall-clock — the
        # agent owns its own watchdog.
        "default": 86400,
    },
    # The AGENT (arm) writes these; they are the same across resolve_then_grade
    # benchmarks because they are the agent's transcript, not the task's.
    "traces": (
        ("events.jsonl", "events_jsonl"),
        ("err.txt", "err_txt"),
        ("engine.log", "engine_log"),
    ),
}

_LITE = dict(_VERIFIED, key="lite", aliases=("swebench-lite",),
             dataset="princeton-nlp/SWE-bench_Lite",
             grade_dataset="princeton-nlp/SWE-bench_Lite", mini_suite="lite-mini")

_PRO = {
    "key": "pro",
    "aliases": ("swebench-pro", "swe-bench-pro", "sweap"),
    "flow": FLOW_RESOLVE_THEN_GRADE,
    # Pro ships as a HF dataset + a vendored eval snapshot (sweap_eval_full_v2.jsonl).
    # ids resolve from the vendored jsonl so a run needs no HF pull; the CSV/jsonl
    # also carries the dockerhub_tag -> image mapping the Pro grader reads.
    "dataset": "ScaleAI/SWE-bench_Pro",
    "split": "test",
    "grade_dataset": "ScaleAI/SWE-bench_Pro",
    "ids_jsonl": "/work/swebench-pro/sweap_eval_full_v2.jsonl",
    # Resolve-side wiring (econ arm reads these off the descriptor in worker-loop
    # _resolve): the RUNNER must NOT default to Verified for Pro.
    #  - repo_dir: the Pro sweap images check the repo out at /app, not Verified's
    #    /testbed — swe_bench_pro_eval.py's entryscript does `cd /app` before
    #    `git apply`, and every run_scripts/*/run_script.sh does `cd /app`, so the
    #    agent MUST edit + diff at /app or grade applies an empty/mismatched patch.
    #  - dockerhub_username: resolve builds FROM the SAME private mirror image grade
    #    pulls (helper_code/image_uri.py -> <user>/sweap-images:<tag>), so the agent
    #    runs inside the exact image the grader scores in. Matches grade_pro's
    #    PRO_DOCKERHUB_USERNAME default.
    "repo_dir": "/app",
    "dockerhub_username": "51jaswanth15",
    "mini_suite": "pro-mini",
    # The worker's _grade delegates to this vendored module (worker.grade(...)) —
    # the Pro adapter (Wave #8) writes tools/grade_pro.py (swe_bench_pro_eval,
    # --use_local_docker, resolved = fail_to_pass|pass_to_pass ⊆ passed_tests).
    "grade_module": "grade_pro",
    "image": (
        "private mirror 51jaswanth15/sweap-images:<dockerhub_tag> (tag from the "
        "dataset's dockerhub_tag column; repo name + tag forced by the Pro eval's "
        "helper_code/image_uri.py). Grade with --dockerhub_username 51jaswanth15."
    ),
    "timeout": {
        "default_env": "PRO_PER_INSTANCE_TIMEOUT",
        # 24h — a never-binding cap on the grade-side subprocess only (grade_pro
        # uses worker.timeout + 600). The resolve path enforces NO wall-clock — the
        # agent owns its own watchdog.
        "default": 86400,
    },
    "traces": (
        ("events.jsonl", "events_jsonl"),
        ("err.txt", "err_txt"),
        ("engine.log", "engine_log"),
    ),
}

_TERMINAL = {
    "key": "terminal",
    "aliases": ("terminal-bench", "tb", "terminalbench", "tbench"),
    # Fused: Harbor runs the agent INSIDE the task container and grades with pytest;
    # there is no intermediate git patch. The worker shells `tb run` per task.
    "flow": FLOW_HARNESS_RUN,
    # Terminal-Bench via the Harbor framework — the 2.1 release, a Harbor REGISTRY
    # dataset (terminal-bench/terminal-bench-2-1, 89 tasks) vendored at build via
    # `harbor dataset download` (see Dockerfile.dist), NOT the public github repo and
    # NOT terminal-bench-core@0.1.1 (the legacy `tb` CLI dataset). dataset/split are
    # informational here: _terminal_task_ids resolves ids purely from the vendored
    # task dirs.
    "dataset": "terminal-bench-2-1",
    "split": "2.1",
    "grade_dataset": "terminal-bench-2-1",
    # ids come from the Harbor task list (directory listing), not HF.
    "ids_source": "/work/terminal-bench/tasks",
    "mini_suite": "terminal-mini",
    # harness_run flow: the worker's _run_harness delegates to this vendored module
    # (worker.run(...)) — the Terminal adapter (Wave #9) writes tools/harness_terminal.py
    # (tb run per task, pytest end-state, tmux/asciinema traces).
    "harness_module": "harness_terminal",
    "image": (
        "built per-task at RUN time from each task's Dockerfile (needs the worker's "
        "in-VM dockerd / DinD-build). No registry pull; nothing to mirror."
    ),
    "timeout": {
        "default_env": "TERMINAL_PER_INSTANCE_TIMEOUT",
        # 24h — a never-binding cap on harness_terminal's outer-wrapper fallback
        # only. Harbor enforces each task's OWN task.toml timeout internally as the
        # real benchmark scoring rule; this value never binds ahead of it. The
        # resolve path itself enforces no wall-clock beyond that.
        "default": 86400,
    },
    # Harbor writes its own transcript per trial (tmux/asciinema + trajectory) on
    # top of the agent's events.jsonl; the terminal adapter lists the exact files.
    "traces": (
        ("events.jsonl", "events_jsonl"),
        ("err.txt", "err_txt"),
        ("trajectory.json", "trajectory_json"),
        ("sessions.cast", "sessions_cast"),
        # Harbor's own captured stdout+stderr (harness_terminal.py run()) — the
        # only place a SETUP-phase RuntimeError (before the agent ever starts,
        # so trial.log/err.txt don't exist) is ever captured.
        ("harbor-run.log", "harbor_run_log"),
    ),
}


_LIVE_VERIFIED = dict(
    _VERIFIED,
    key="live_verified",
    aliases=("swe-bench-live-verified", "live-verified", "swebench-live-verified"),
    dataset="SWE-bench-Live/SWE-bench-Live",
    split="verified",
    grade_dataset="SWE-bench-Live/SWE-bench-Live",
    mini_suite="live_verified-mini",
    # Resolve-side image org — SWE-bench Live ships its own image namespace, not
    # swebench's. Other descriptors omit this field and keep the swebench default.
    image_namespace="starryzhang",
    # SWE-bench Live's OWN harness grades this, not stock swebench (Pro-style
    # vendored adapter — tools/grade_live.py, written separately).
    grade_module="grade_live",
    image=(
        "starryzhang/sweb.eval.x86_64.<key> (key = instance_id with __ -> _1776_, "
        "lowercased), tag latest. Graded by SWE-bench-Live's OWN harness via "
        "grade_live (NOT stock swebench). Repo checks out at /testbed (same as "
        "Verified) so no repo_dir override is needed."
    ),
)


_REGISTRY = {}
for _d in (_VERIFIED, _LITE, _PRO, _TERMINAL, _LIVE_VERIFIED):
    _REGISTRY[_d["key"]] = _d
    for _a in _d.get("aliases", ()):
        _REGISTRY[_a] = _d


DEFAULT_BENCHMARK = "verified"


def get(bench=None):
    """Return the descriptor dict for a benchmark key/alias (default: verified).
    Unknown keys exit(2) with the valid set — a typo must fail loud, not silently
    run the wrong benchmark."""
    b = (bench or DEFAULT_BENCHMARK).lower()
    d = _REGISTRY.get(b)
    if d is None:
        keys = sorted({v["key"] for v in _REGISTRY.values()})
        sys.stderr.write(
            f"benchmarks: unknown benchmark '{bench}' (known: {', '.join(keys)})\n")
        sys.exit(2)
    return d


def keys():
    """The canonical benchmark keys (no aliases)."""
    return sorted({v["key"] for v in _REGISTRY.values()})


# ─────────────────────────────────────────────────────────────────────────────
# Suite resolution — the ONE entry point suite.py calls. A suite selector is
# either "<benchmark>", "<benchmark>-mini", the legacy bare selectors
# (full|verified|lite|mini), or an explicit ids/file passthrough handled upstream.
# ─────────────────────────────────────────────────────────────────────────────
_MINI_SMOKE_N = 5  # a *-mini suite is the first N ids: 1 coordinator + 2 workers smoke


def resolve_ids(suite=None, dataset=None, split=None):
    """Expand a suite selector to an ordered id list for the coordinator queue.

    Handles benchmark-scoped suites (`pro`, `pro-mini`, `terminal`, ...) plus the
    legacy Verified selectors (`full`/`verified`/`lite`/`mini`) that predate the
    benchmark axis, so existing run commands keep working unchanged. Explicit
    --tasks/--file are resolved by suite.py itself, not here."""
    s = (suite or "full").lower()

    # Legacy bare selectors -> Verified (back-compat with pre-benchmark runs).
    if s in ("full", "verified"):
        return _hf_ids(dataset or _VERIFIED["dataset"], split or _VERIFIED["split"])
    if s == "lite":
        return _hf_ids(dataset or _LITE["dataset"], split or _LITE["split"])
    if s == "mini":  # historical alias for the Verified Mini-10
        return list(_VERIFIED_MINI10)

    # <benchmark>-mini  -> the N-id smoke set for that benchmark.
    if s.endswith("-mini"):
        base = s[: -len("-mini")]
        return _mini_ids(base)

    # <benchmark>  -> the full id set for that benchmark.
    d = get(s)
    return _full_ids(d, dataset, split)


def _full_ids(d, dataset, split):
    """All ids for a benchmark descriptor (dispatch by where its ids live)."""
    if d["key"] in ("verified", "lite"):
        return _hf_ids(dataset or d["dataset"], split or d["split"])
    if d["key"] == "pro":
        path = os.environ.get("PRO_IDS_JSONL", d["ids_jsonl"])
        if os.path.isfile(path):
            return _jsonl_ids(path)
        # Fallback to HF if the vendored snapshot isn't present (e.g. local dev).
        return _hf_ids(dataset or d["dataset"], split or d["split"])
    if d["key"] == "terminal":
        return _terminal_task_ids(os.environ.get("TB_TASKS_DIR", d["ids_source"]))
    # Unknown flow: fall back to HF by dataset.
    return _hf_ids(dataset or d["dataset"], split or d["split"])


def _mini_ids(base):
    """The first _MINI_SMOKE_N ids of a benchmark — the smoke set."""
    d = get(base)
    if d["key"] == "verified":
        return _VERIFIED_MINI10[:_MINI_SMOKE_N]
    if d["key"] == "lite":
        return _hf_ids(_LITE["dataset"], _LITE["split"])[:_MINI_SMOKE_N]
    if d["key"] == "live_verified":
        return list(_LIVE_VERIFIED_MINI5)
    # pro / terminal: head of the full list (vendored snapshot), so the smoke set
    # is always ids we can actually run.
    return _full_ids(d, None, None)[:_MINI_SMOKE_N]


def _terminal_task_ids(tasks_dir):
    """Terminal-Bench task ids = the subdirectory names under the vendored task
    set (each dir is one task with its own task.yaml + Dockerfile). Empty if the
    task set isn't vendored yet (the terminal adapter Wave populates it)."""
    if not os.path.isdir(tasks_dir):
        sys.stderr.write(
            f"benchmarks: terminal task dir not found: {tasks_dir} "
            f"(vendor Harbor terminal-bench-2-1 (2.1) tasks into the image, or set TB_TASKS_DIR)\n")
        return []
    return sorted(
        name for name in os.listdir(tasks_dir)
        if os.path.isdir(os.path.join(tasks_dir, name)) and not name.startswith("."))


if __name__ == "__main__":
    # `python benchmarks.py [suite]` prints the resolved id list — handy for a
    # preflight sanity check without going through suite.py/the launcher.
    import argparse
    ap = argparse.ArgumentParser(description="Resolve a benchmark suite to ids")
    ap.add_argument("suite", nargs="?", default=os.environ.get("SUITE", "verified"))
    ap.add_argument("--dataset", default=os.environ.get("DATASET"))
    ap.add_argument("--split", default=os.environ.get("SPLIT"))
    a = ap.parse_args()
    ids = resolve_ids(a.suite, a.dataset, a.split)
    if not ids:
        sys.stderr.write(f"benchmarks: resolved 0 ids for suite '{a.suite}'\n")
        sys.exit(1)
    sys.stdout.write("\n".join(ids) + "\n")
