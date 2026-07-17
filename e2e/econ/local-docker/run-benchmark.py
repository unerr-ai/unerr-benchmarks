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


def _pro_image_uri(iid: str, username: str, repo_name: str) -> str:
    """SWE-bench Pro per-instance image ref — a byte-for-byte replica of the
    vendored e2e/distributed/swebench-pro/helper_code/image_uri.py
    get_dockerhub_image_uri(). Resolve MUST land on the same
    <user>/sweap-images:<tag> the Pro grader (swe_bench_pro_eval.py
    --dockerhub_username) pulls, or the agent runs in a different/nonexistent
    image than the one scored. The vendored module is imported in preference to
    this replica when present (_load_pro_image_uri); this is the off-box
    fallback, kept in sync with it."""
    repo_base, repo_name_only = repo_name.lower().split("/")
    hsh = iid.replace("instance_", "")
    if iid == "instance_element-hq__element-web-ec0f940ef0e8e3b61078f145f34dc40d1938e6c5-vnan":
        repo_name_only = "element-web"
    elif "element-hq" in repo_name.lower() and "element-web" in repo_name.lower():
        repo_name_only = "element"
        if hsh.endswith("-vnan"):
            hsh = hsh[:-5]
    elif hsh.endswith("-vnan"):
        hsh = hsh[:-5]
    tag = f"{repo_base}.{repo_name_only}-{hsh}"
    if len(tag) > 128:
        tag = tag[:128]
    return f"{username}/sweap-images:{tag}"


def _load_pro_image_uri():
    """Return the vendored get_dockerhub_image_uri when importable (the fleet
    worker bakes it at /work/swebench-pro/helper_code/image_uri.py — the SAME
    copy grade_pro's evaluator uses, so resolve/grade agree by construction),
    else the inline _pro_image_uri replica."""
    for base in (os.environ.get("SWEBENCH_PRO_DIR", "/work/swebench-pro"),
                 str(ECON_ROOT.parent / "distributed" / "swebench-pro")):
        if (Path(base) / "helper_code" / "image_uri.py").is_file():
            sys.path.insert(0, base)
            try:
                from helper_code.image_uri import get_dockerhub_image_uri
                return get_dockerhub_image_uri
            except Exception:
                pass
    return _pro_image_uri


def docker_image_for(instance: dict, pro_username: str = "", pro_fn=None,
                     namespace: str = "swebench") -> str:
    """Per-instance image ref. Verified/Lite: the official swebench eval image
    (or an image_name the row already carries), built under `namespace` (the
    Docker Hub org — default "swebench"; "starryzhang" for SWE-bench Live).
    SWE-bench Pro (pro_username set): the private sweap mirror ref forced by
    the Pro eval's image_uri rule — the Pro row's own image_name is Scale's
    private ECR URL we can't pull, so it is deliberately IGNORED in favour of
    <pro_username>/sweap-images:<tag>."""
    if pro_username:
        return (pro_fn or _pro_image_uri)(
            instance["instance_id"], pro_username, instance["repo"])
    name = instance.get("image_name") or instance.get("docker_image")
    if name:
        return name
    iid = instance["instance_id"].replace("__", "_1776_")
    return f"docker.io/{namespace}/sweb.eval.x86_64.{iid}:latest".lower()


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
                   label: str, out_dir: Path,
                   pro_username: str = "", pro_fn=None,
                   namespace: str = "swebench") -> tuple[str, dict]:
    """Build + run one instance, return (patch, meta). Empty patch on failure.
    pro_username set → SWE-bench Pro: image is the sweap mirror ref and repo_dir
    is /app (the caller passes --repo-dir /app for Pro)."""
    iid = instance["instance_id"]
    instance_image = docker_image_for(instance, pro_username, pro_fn, namespace)
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
    ap.add_argument("--ids-jsonl", default="",
                    help="load instance rows from this JSONL (one row per line) instead of "
                         "HF load_dataset — the SWE-bench Pro path (rows carry repo/"
                         "problem_statement; no HF pull, no Mini-50 allowlist)")
    ap.add_argument("--dockerhub-username", default="",
                    help="when set, resolve builds each instance FROM the private Pro image "
                         "mirror <user>/sweap-images:<tag> (the image_uri rule) instead of "
                         "the Verified swebench eval image — MUST match the Pro grader's "
                         "--dockerhub_username so resolve+grade share one image")
    ap.add_argument("--image-namespace", default="swebench",
                    help="Docker Hub org for per-instance eval images (default swebench; "
                         "starryzhang for SWE-bench Live)")
    args = ap.parse_args()

    # SWE-bench instance images are linux/amd64 only; force amd64 for build/run/pull
    # so the grafted toolbox (amd64) execs inside the x86_64 instance image.
    os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

    api_key = os.environ.get("LITELLM_API_KEY")
    if not api_key:
        print("ERROR: set LITELLM_API_KEY (econ routes all model tiers via it)", file=sys.stderr)
        return 1

    # Row source: the Pro path reads the vendored JSONL (no HF pull; rows carry the
    # `repo` field the sweap image_uri rule needs). Everything else loads the HF
    # dataset named by --dataset.
    if args.ids_jsonl:
        if not os.path.isfile(args.ids_jsonl):
            print(f"ERROR: --ids-jsonl {args.ids_jsonl} not found (SWE-bench Pro id source; "
                  f"Dockerfile.dist bakes it at /work/swebench-pro/)", file=sys.stderr)
            return 1
        rows = []  # tolerant load — one bad line must not sink the whole resolve
        with open(args.ids_jsonl, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError as e:
                    print(f"WARNING: skipping malformed --ids-jsonl line ({e})", file=sys.stderr)
        src = args.ids_jsonl
    else:
        from datasets import load_dataset  # lazy: only needed at run time
        rows = list(load_dataset(args.dataset, split=args.split))
        src = args.dataset
    # --ids selects directly from the FULL source (targeted re-runs of ANY
    # instance); the Mini-50 allowlist does NOT gate it. Only for the Verified HF
    # path with no explicit ids do we fall back to the pinned Mini-50 default —
    # the Pro jsonl has its own id space, so it runs whole (or --instances-capped).
    if args.ids:
        want = {s.strip() for s in args.ids.split(",") if s.strip()}
        rows = [r for r in rows if r["instance_id"] in want]
        absent = want - {r["instance_id"] for r in rows}
        if absent:
            print(f"WARNING: {len(absent)} requested ids absent from {src}: "
                  f"{sorted(absent)[:3]}...", file=sys.stderr)
    elif args.ids_jsonl:
        if args.instances > 0:
            rows = rows[: args.instances]
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
    # Pro image mode (from --dockerhub-username): resolve the image_uri callable
    # ONCE (prefer the vendored copy the grader uses) and share it across the pool.
    pro_username = args.dockerhub_username.strip()
    pro_fn = _load_pro_image_uri() if pro_username else None
    print(f"instances={total} src={src} label={args.label} parallel={par}"
          + (f" pro_user={pro_username} repo_dir={args.repo_dir} "
             f"image_uri={'vendored' if pro_fn is not _pro_image_uri else 'inline'}"
             if pro_username else ""),
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
                inst, api_key, args.repo_dir, args.timeout, args.label, out,
                pro_username, pro_fn, args.image_namespace)
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
