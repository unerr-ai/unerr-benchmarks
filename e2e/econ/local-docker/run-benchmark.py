#!/usr/bin/env python3
"""Run econ on SWE-bench instances and write a SWE-bench predictions file + cost meta.

SINGLE ARM (not A/B): unerr is compiled directly INTO econ, so econ runs ONCE per
instance — no on/off, no MODES, no `unerr install`, no unerrd, no MCP wiring.

Per instance:
  1. derive an image  FROM the official swebench/sweb.eval... image + the econ toolbox
  2. docker run the driver -> unified diff (the prediction) on stdout, econ event
     stream + session SQLite on a mounted artifact dir
  3. host-side: econ-telemetry.py (event stream) + econ-tier-cost.py (session DB)
     -> the telemetry / tier_cost_db / session_id fields report.py consumes
  4. record {instance_id, model_name_or_path, model_patch} into preds.json and a
     full cost record into meta.jsonl

Grading is a separate, standard step: `swebench.harness.run_evaluation` scores
preds.json; then report.py joins meta.jsonl + the grade report into a cost report.

Prereqs: docker, the `econ-toolbox` image (built by entrypoint.sh /
Dockerfile.toolbox), LITELLM_API_KEY in the env, `pip install datasets`.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# econ-telemetry.py / econ-tier-cost.py / report.py sit one level up (e2e/econ on
# a laptop, /work on the fly VM — same relative layout).
ECON_ROOT = HERE.parent
TELEMETRY_PY = ECON_ROOT / "econ-telemetry.py"
TIERCOST_PY = ECON_ROOT / "econ-tier-cost.py"

# Host-side persistent graph-index cache, keyed per-repo (see solve_instance).
# MUST live outside out_dir/artifacts: that tree is fresh per instance, while
# this survives for the worker's lifetime so sequential instances of the same
# repo warm-start off each other's graph.db instead of cold-indexing every time.
GRAPH_CACHE_ROOT = Path(os.environ.get("OC_GRAPH_CACHE", "/var/tmp/oc-graph-cache"))


def docker_image_for(instance: dict) -> str:
    """Official per-instance image name (mirrors mini-swe-agent's mapping)."""
    name = instance.get("image_name") or instance.get("docker_image")
    if name:
        return name
    iid = instance["instance_id"].replace("__", "_1776_")
    return f"docker.io/swebench/sweb.eval.x86_64.{iid}:latest".lower()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def build_instance_image(instance_image: str, tag: str) -> None:
    """FROM <official image> + COPY the econ toolbox. One cached layer."""
    run(
        ["docker", "build", "-f", str(HERE / "Dockerfile.instance"),
         "--build-arg", f"INSTANCE_IMAGE={instance_image}", "-t", tag, str(HERE)],
        check=True,
    )


def _run_json(script: Path, args: list[str]) -> dict:
    """Run a stdlib-only econ helper, return its parsed stdout JSON (or {})."""
    if not script.is_file():
        return {}
    try:
        proc = run([sys.executable, str(script), *args],
                   capture_output=True, text=True, timeout=120)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def _apply_cross_session_totals(telemetry: dict, tier_cost_db: dict) -> dict:
    """Correct the headline turns/usd for multi-session runs (S7b).

    econ-telemetry.py parses ONLY the events.jsonl stream, which — when econ
    restarts sessions mid-run for a context-fill checkpoint — reflects the
    LAST session alone (django-11964 reported $0.0155/5 turns from telemetry
    vs the DB's real 8 sessions/116 messages/$0.304). tier_cost_db (built from
    opencode.db, which persists every session) is the source of truth for the
    across-all-sessions totals. When it shows more than one session, or its
    usd_upstream exceeds the last session's, promote its `messages`/
    `usd_upstream` to the headline `turns`/`usd`, keeping the last-session
    figures under `turns_last_session`/`usd_last_session` so both remain
    inspectable. No-op (unchanged telemetry) for the single-session case,
    where the two sources already agree.
    """
    if not isinstance(tier_cost_db, dict) or tier_cost_db.get("error"):
        return telemetry
    sessions = tier_cost_db.get("sessions")
    db_usd_upstream = tier_cost_db.get("usd_upstream")
    last_usd_upstream = telemetry.get("usd_upstream") or 0.0
    multi_session = isinstance(sessions, (int, float)) and sessions > 1
    db_exceeds_last = (
        isinstance(db_usd_upstream, (int, float)) and db_usd_upstream > last_usd_upstream
    )
    if not (multi_session or db_exceeds_last):
        return telemetry
    telemetry["turns_last_session"] = telemetry.get("turns", 0)
    telemetry["usd_last_session"] = telemetry.get("usd", 0.0)
    telemetry["turns"] = tier_cost_db.get("messages", telemetry.get("turns", 0))
    if isinstance(db_usd_upstream, (int, float)):
        telemetry["usd"] = db_usd_upstream
    telemetry["usd_source"] = "tier_cost_db_multi_session"
    telemetry["multi_session_corrected"] = True
    return telemetry


def solve_instance(instance: dict, api_key: str, repo_dir: str, timeout: int,
                   label: str, out_dir: Path) -> tuple[str, dict]:
    """Build + run one instance, return (patch, meta). Empty patch on failure."""
    iid = instance["instance_id"]
    instance_image = docker_image_for(instance)
    tag = f"unerr-econ-run:{iid.replace('__', '_1776_').lower()}"

    # ensure the official env image is present, then graft the toolbox onto it
    mirror_on = bool(os.environ.get("SWEBENCH_REGISTRY_MIRROR"))
    pull_t0 = time.time()
    run(["docker", "pull", instance_image], check=False)
    pull_s = round(time.time() - pull_t0, 1)
    print(f"[pull-time] {iid} {pull_s}s mirror={'on' if mirror_on else 'off'}",
          file=sys.stderr)
    build_instance_image(instance_image, tag)

    problem = instance["problem_statement"]

    # host artifact dir mounted at /work-out — run-instance.sh writes events.jsonl,
    # err.txt, opencode.db, session_id.txt straight into it.
    art_host_dir = out_dir / "artifacts" / iid
    art_host_dir.mkdir(parents=True, exist_ok=True)

    # container env for econ. EXA_API_KEY is OPTIONAL — passed through only when set
    # on the host (run.sh resolves it); econ reads process.env.EXA_API_KEY for its
    # websearch tool. Absent → web search is disabled (the baseline-comparable default;
    # on SWE-bench the fixes are public, so web search is an answer-lookup risk).
    docker_env = [
        "-e", f"LITELLM_API_KEY={api_key}",
        "-e", f"ECON_TIMEOUT={timeout}",
        "-e", f"REPO_DIR={repo_dir}",
    ]
    exa_key = os.environ.get("EXA_API_KEY")
    if exa_key:
        docker_env += ["-e", f"EXA_API_KEY={exa_key}"]

    # Per-repo graph-index cache mount. opencode's graph db path is
    # Hash.fast(repoRoot) and every instance mounts its repo at the SAME
    # REPO_DIR (/testbed) — so without per-repo keying on the host side, every
    # repo's graph db would collide at one container path. Keying by repo_key
    # (instance_id minus its trailing "-<n>") isolates repos on the host while
    # letting sequential instances of the SAME repo share one persisted
    # graph.db: run-instance.sh's `opencode init` (now incremental, no
    # --force) reconciles changed/added/deleted files by content hash, so
    # warm-starting across commits of one repo is safe.
    docker_mounts = ["-v", f"{art_host_dir.resolve()}:/work-out"]
    if os.environ.get("OC_GRAPH_CACHE_PERSIST", "1") != "0":
        repo_key = iid.rsplit("-", 1)[0]
        repo_key = re.sub(r"[^A-Za-z0-9._-]", "_", repo_key)
        repo_cache = GRAPH_CACHE_ROOT / repo_key
        repo_cache.mkdir(parents=True, exist_ok=True)
        docker_mounts += ["-v", f"{repo_cache.resolve()}:/root/.local/share/opencode/graph"]

    t0 = time.time()
    proc = run(
        ["docker", "run", "--rm", "-i",
         *docker_mounts,
         *docker_env,
         tag,
         "bash", "-c",
         "cat > /tmp/problem.txt && /opt/toolbox/run-instance.sh /tmp/problem.txt"],
        input=problem, text=True, capture_output=True, timeout=timeout + 120,
    )
    patch = proc.stdout
    (art_host_dir / "patch.diff").write_text(patch)
    # full per-instance driver log (stderr)
    (out_dir / f"log_{iid}.txt").write_text(proc.stderr or "")

    events = art_host_dir / "events.jsonl"
    db = art_host_dir / "opencode.db"
    sid_file = art_host_dir / "session_id.txt"
    sid = sid_file.read_text().strip() if sid_file.is_file() else ""

    # ── host-side telemetry (event stream) + per-tier cost (session SQLite) ──
    telemetry = _run_json(TELEMETRY_PY, [str(events)]) if events.is_file() else {}
    tiercost_args = ["--db", str(db)] + (["--session", sid] if sid else [])
    tier_cost_db = _run_json(TIERCOST_PY, tiercost_args) if db.is_file() else {}
    telemetry = _apply_cross_session_totals(telemetry, tier_cost_db)

    meta = {
        "instance_id": iid,
        "label": label,
        "wall_s": round(time.time() - t0, 1),
        "pull_s": pull_s,
        "mirror": "on" if mirror_on else "off",
        "rc": proc.returncode,
        "patch_bytes": len(patch),
        "telemetry": telemetry,
        "artifacts_dir": f"artifacts/{iid}",
        "stderr_tail": (proc.stderr or "")[-2000:],
        "session_id": sid or None,
        "tier_cost_db": tier_cost_db,
    }
    return patch, meta


# Official SWE-bench Verified **Mini-50** instance ids (the curated 50-task subset,
# == MariusHobbhahn/swe-bench-verified-mini; every id is also in the official
# princeton-nlp/SWE-bench_Verified). Pinned here so the run is reproducible against
# the official dataset rather than a community mirror. django(25) + sphinx(25).
MINI_50_IDS = [
    "django__django-11790", "django__django-11815", "django__django-11848",
    "django__django-11880", "django__django-11885", "django__django-11951",
    "django__django-11964", "django__django-11999", "django__django-12039",
    "django__django-12050", "django__django-12143", "django__django-12155",
    "django__django-12193", "django__django-12209", "django__django-12262",
    "django__django-12273", "django__django-12276", "django__django-12304",
    "django__django-12308", "django__django-12325", "django__django-12406",
    "django__django-12708", "django__django-12713", "django__django-12774",
    "django__django-9296", "sphinx-doc__sphinx-10323", "sphinx-doc__sphinx-10435",
    "sphinx-doc__sphinx-10466", "sphinx-doc__sphinx-10673", "sphinx-doc__sphinx-11510",
    "sphinx-doc__sphinx-7590", "sphinx-doc__sphinx-7748", "sphinx-doc__sphinx-7757",
    "sphinx-doc__sphinx-7985", "sphinx-doc__sphinx-8035", "sphinx-doc__sphinx-8056",
    "sphinx-doc__sphinx-8265", "sphinx-doc__sphinx-8269", "sphinx-doc__sphinx-8475",
    "sphinx-doc__sphinx-8548", "sphinx-doc__sphinx-8551", "sphinx-doc__sphinx-8638",
    "sphinx-doc__sphinx-8721", "sphinx-doc__sphinx-9229", "sphinx-doc__sphinx-9230",
    "sphinx-doc__sphinx-9281", "sphinx-doc__sphinx-9320", "sphinx-doc__sphinx-9367",
    "sphinx-doc__sphinx-9461", "sphinx-doc__sphinx-9698",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified")
    ap.add_argument("--split", default="test")
    ap.add_argument("--instances", type=int, default=0,
                    help="cap to the first N of the Mini-50 allowlist (0 = all filtered)")
    ap.add_argument("--ids", default="",
                    help="comma-separated instance_ids to run (overrides --instances; "
                         "for targeted re-runs of specific instances)")
    ap.add_argument("--repo-dir", default="/testbed", help="repo root inside the instance image")
    ap.add_argument("--timeout", type=int, default=1800, help="per-instance seconds")
    ap.add_argument("--parallel", type=int, default=1,
                    help="resolve up to N instances concurrently (each is an independent "
                         "docker build+run; the model work is remote so this is I/O-bound)")
    ap.add_argument("--out", default="results")
    ap.add_argument("--label", default="econ",
                    help="run label; output goes to <out>/<label>/")
    args = ap.parse_args()

    # SWE-bench instance images are linux/amd64 only; force amd64 for build/run/pull
    # so the grafted toolbox (amd64) execs inside the x86_64 instance image.
    os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

    api_key = os.environ.get("LITELLM_API_KEY")
    if not api_key:
        print("ERROR: set LITELLM_API_KEY (econ routes all model tiers via it)", file=sys.stderr)
        return 1

    from datasets import load_dataset  # lazy: only needed at run time
    rows = list(load_dataset(args.dataset, split=args.split))
    # --ids selects directly from the FULL dataset (targeted re-runs of ANY
    # Verified instance); the Mini-50 allowlist does NOT gate it. Only when no
    # explicit ids are given do we fall back to the pinned Mini-50 default.
    if args.ids:
        want = {s.strip() for s in args.ids.split(",") if s.strip()}
        rows = [r for r in rows if r["instance_id"] in want]
        absent = want - {r["instance_id"] for r in rows}
        if absent:
            print(f"WARNING: {len(absent)} requested ids absent from {args.dataset}: "
                  f"{sorted(absent)[:3]}...", file=sys.stderr)
    else:
        allow = set(MINI_50_IDS)
        rows = [r for r in rows if r["instance_id"] in allow]
        missing = allow - {r["instance_id"] for r in rows}
        if missing:
            print(f"WARNING: {len(missing)} Mini-50 ids absent from {args.dataset}: "
                  f"{sorted(missing)[:3]}...", file=sys.stderr)
        if args.instances > 0:
            rows = rows[: args.instances]
    if not rows:
        print("no instances after filter", file=sys.stderr)
        return 1

    out = Path(args.out) / args.label
    out.mkdir(parents=True, exist_ok=True)
    preds_path = out / "preds.json"
    meta_path = out / "meta.jsonl"
    preds = json.loads(preds_path.read_text()) if preds_path.exists() else {}

    par = max(1, args.parallel)
    total = len(rows)
    print(f"instances={total} dataset={args.dataset} label={args.label} parallel={par}",
          file=sys.stderr)

    # preds.json is rewritten whole and meta.jsonl is appended after each instance;
    # with parallelism several workers finish at once, so guard both writes. done[]
    # is a 1-elem list so the closure can mutate the completion counter under lock.
    write_lock = threading.Lock()
    done = [0]

    def _persist(iid: str, patch: str, meta: dict) -> None:
        with write_lock:
            preds[iid] = {"instance_id": iid, "model_name_or_path": "econ", "model_patch": patch}
            preds_path.write_text(json.dumps(preds, indent=2))
            with meta_path.open("a") as mf:
                mf.write(json.dumps(meta) + "\n"); mf.flush()
            done[0] += 1
            print(f"[{done[0]}/{total}] DONE {iid} rc={meta.get('rc')} patch={len(patch)}B",
                  file=sys.stderr)

    def _solve(idx: int, inst: dict) -> str:
        iid = inst["instance_id"]
        print(f"[{idx}/{total}] START {iid}", file=sys.stderr)
        try:
            patch, meta = solve_instance(
                inst, api_key, args.repo_dir, args.timeout, args.label, out)
        except subprocess.TimeoutExpired:
            patch, meta = "", {"instance_id": iid, "label": args.label, "rc": "timeout"}
        except Exception as e:  # a build/run crash on ONE instance must not sink the pool
            patch, meta = "", {"instance_id": iid, "label": args.label,
                               "rc": f"error:{type(e).__name__}", "error": str(e)[:800]}
        _persist(iid, patch, meta)
        return iid

    if par == 1:
        for i, inst in enumerate(rows, 1):
            _solve(i, inst)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=par) as ex:
            futs = [ex.submit(_solve, i, inst) for i, inst in enumerate(rows, 1)]
            for f in concurrent.futures.as_completed(futs):
                f.result()  # _solve swallows per-instance errors; surfaces only executor faults

    print("\n=== predictions written ===", file=sys.stderr)
    print(f"  {preds_path}", file=sys.stderr)
    print(f"  {meta_path}", file=sys.stderr)
    print("\n=== grade them (standard SWE-bench harness) ===", file=sys.stderr)
    print(
        f"  python -m swebench.harness.run_evaluation \\\n"
        f"    --dataset_name {args.dataset} --split {args.split} \\\n"
        f"    --predictions_path {preds_path} \\\n"
        f"    --run_id {args.label} --max_workers 4 --cache_level env",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
