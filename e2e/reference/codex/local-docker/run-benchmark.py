#!/usr/bin/env python3
"""Run Codex (+/- unerr) on SWE-bench instances and write a SWE-bench predictions file.

Per instance:
  1. derive an image  FROM the official swebench/sweb.eval... image + toolbox
  2. docker run the driver -> unified diff (the prediction) on stdout
  3. record {instance_id, model_name_or_path, model_patch} into preds_<mode>.json

Grading is a separate, standard step (printed at the end) — this script only
produces the predictions; `swebench.harness.run_evaluation` scores them.

Smoke-test first: default is ONE instance. Scale with --instances after it works.

Prereqs: docker, the `unerr-codex-toolbox` image (run ./build-toolbox.sh),
OPENAI_API_KEY in the env, `pip install datasets`, ~30GB free disk for images.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


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
    """FROM <official image> + COPY the toolbox. One cached layer."""
    here = Path(__file__).parent
    run(
        ["docker", "build", "-f", str(here / "Dockerfile.instance"),
         "--build-arg", f"INSTANCE_IMAGE={instance_image}", "-t", tag, str(here)],
        check=True,
    )


def preflight_instance(instance: dict, repo_dir: str, timeout: int) -> int:
    """Build + run the zero-cost preflight (no API key, no codex). Returns rc."""
    iid = instance["instance_id"]
    instance_image = docker_image_for(instance)
    tag = f"unerr-codex-run:{iid.replace('__', '_1776_').lower()}"
    run(["docker", "pull", instance_image], check=False)
    build_instance_image(instance_image, tag)
    proc = run(
        ["docker", "run", "--rm", "-e", f"REPO_DIR={repo_dir}",
         tag, "/opt/toolbox/preflight.sh"],
        text=True, timeout=timeout,
    )
    return proc.returncode


def solve_instance(instance: dict, mode: str, api_key: str, repo_dir: str, timeout: int, model_id: str, out_dir: Path) -> tuple[str, dict]:
    """Build + run one instance, return (patch, meta). Empty patch on failure."""
    iid = instance["instance_id"]
    instance_image = docker_image_for(instance)
    tag = f"unerr-codex-run:{iid.replace('__', '_1776_').lower()}"

    # ensure the official env image is present, then graft the toolbox onto it
    run(["docker", "pull", instance_image], check=False)
    build_instance_image(instance_image, tag)

    problem = instance["problem_statement"]

    # create host artifact dir and mount it into the container
    art_host_dir = out_dir / "artifacts" / mode / iid
    art_host_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    proc = run(
        ["docker", "run", "--rm", "-i",
         "-v", f"{art_host_dir.resolve()}:/work-out",
         "-e", "ART_DIR=/work-out",
         "-e", f"OPENAI_API_KEY={api_key}",
         "-e", f"UNERR_MODE={mode}",
         "-e", f"CODEX_MODEL={model_id}",
         "-e", f"REPO_DIR={repo_dir}",
         tag,
         "bash", "-c", f"cat > /tmp/problem.txt && /opt/toolbox/run-instance.sh /tmp/problem.txt"],
        input=problem, text=True, capture_output=True, timeout=timeout,
    )
    patch = proc.stdout

    # full per-instance log (the 2000-char tail drops the unerrd/install lines)
    (out_dir / f"log_{mode}_{iid}.txt").write_text(proc.stderr or "")

    # pull the telemetry summary the in-container driver emitted to stderr
    telemetry: dict = {}
    for line in (proc.stderr or "").splitlines():
        if line.startswith("UNERR_TELEMETRY "):
            try:
                telemetry = json.loads(line[len("UNERR_TELEMETRY "):])
            except json.JSONDecodeError:
                pass
    # cheap signals lifted straight from the driver's log lines
    unerrd_up = "unerrd: up" in (proc.stderr or "")
    install_ok = "unerr install codex: ok" in (proc.stderr or "")

    meta = {
        "instance_id": iid, "mode": mode, "model": model_id,
        "wall_s": round(time.time() - t0, 1),
        "rc": proc.returncode, "patch_bytes": len(patch),
        "unerrd_up": unerrd_up, "install_ok": install_ok,
        "telemetry": telemetry,
        "artifacts_dir": f"artifacts/{mode}/{iid}",
        "stderr_tail": (proc.stderr or "")[-2000:],
    }
    return patch, meta


# Official SWE-bench Verified **Mini-50** instance ids (the curated 50-task subset,
# == MariusHobbhahn/swe-bench-verified-mini; every id is also in the official
# princeton-nlp/SWE-bench_Verified). Pinned here so --mini is reproducible against
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
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified",
                    help="HF dataset id. Verified Mini = this filtered to 50 (use --slice / --filter).")
    ap.add_argument("--split", default="test")
    ap.add_argument("--slice", default="", help="e.g. 0:1 (smoke) or 0:50 (Verified Mini)")
    ap.add_argument("--filter", default="", help="regex on instance_id")
    ap.add_argument("--mini", action="store_true",
                    help="restrict to the official SWE-bench Verified Mini-50 (django+sphinx). "
                         "Filters --dataset to the pinned 50-id allowlist; ignores --slice.")
    ap.add_argument("--instances", type=int, default=0,
                    help="cap after slice/filter (0 = auto: 1 normally, all-50 with --mini; "
                         "an explicit value is always honored, e.g. --instances 1 --mini = smoke)")
    ap.add_argument("--mode", choices=["on", "off", "both"], default="both")
    ap.add_argument("--model", default="gpt-5.4-mini", help="codex model id (forwarded into the container as CODEX_MODEL)")
    ap.add_argument("--preflight", action="store_true",
                    help="health-check only: verify unerr runs + MCP tools work in-image. No API key, no codex, zero cost.")
    ap.add_argument("--repo-dir", default="/testbed", help="repo root inside the instance image")
    ap.add_argument("--timeout", type=int, default=1800, help="per-instance seconds")
    ap.add_argument("--out", default="results")
    ap.add_argument("--label", default="run",
                    help="label for this run; output goes to <out>/<label>/. "
                         "Use distinct labels for different models to avoid overwriting.")
    args = ap.parse_args()

    # SWE-bench instance images are linux/amd64 only; on Apple Silicon every
    # docker build/run/pull must target amd64 so the grafted toolbox (amd64) can
    # exec inside the x86_64 instance image (under emulation). One env var covers
    # build + run + pull. Allow an explicit override.
    os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key and not args.preflight:
        print("ERROR: set OPENAI_API_KEY (or use --preflight for the zero-cost check)", file=sys.stderr)
        return 1

    from datasets import load_dataset  # lazy: only needed at run time
    rows = list(load_dataset(args.dataset, split=args.split))
    if args.mini:
        # Official Verified Mini-50: filter to the pinned allowlist, ignore --slice,
        # and default to all 50 (don't let the smoke default of 1 truncate it).
        allow = set(MINI_50_IDS)
        rows = [r for r in rows if r["instance_id"] in allow]
        missing = allow - {r["instance_id"] for r in rows}
        if missing:
            print(f"WARNING: {len(missing)} Mini-50 ids absent from {args.dataset}: "
                  f"{sorted(missing)[:3]}...", file=sys.stderr)
        # Auto (0) = all 50; an explicit --instances N (e.g. 1 for a smoke) wins.
        if args.instances == 0:
            args.instances = len(MINI_50_IDS)
    if args.filter:
        import re
        rows = [r for r in rows if re.match(args.filter, r["instance_id"])]
    if args.slice and not args.mini:
        lo, _, hi = args.slice.partition(":")
        rows = rows[int(lo or 0):int(hi) if hi else None]
    if args.instances == 0:   # non-mini default = 1 (smoke)
        args.instances = 1
    rows = rows[: args.instances]
    if not rows:
        print("no instances after slice/filter", file=sys.stderr)
        return 1

    if args.preflight:
        print(f"=== PREFLIGHT on {len(rows)} instance(s) — no API key, zero cost ===", file=sys.stderr)
        failed = 0
        for i, inst in enumerate(rows, 1):
            print(f"\n[preflight {i}/{len(rows)}] {inst['instance_id']}", file=sys.stderr)
            rc = preflight_instance(inst, args.repo_dir, args.timeout)
            if rc != 0:
                failed += 1
        print(f"\n=== preflight: {len(rows) - failed}/{len(rows)} instances ALL-PASS ===", file=sys.stderr)
        return 1 if failed else 0

    modes = ["on", "off"] if args.mode == "both" else [args.mode]
    out = Path(args.out) / args.label
    out.mkdir(parents=True, exist_ok=True)
    model_name = f"codex+unerr" if "on" in modes else "codex"

    print(f"instances={len(rows)} modes={modes} dataset={args.dataset}", file=sys.stderr)
    for mode in modes:
        preds_path = out / f"preds_{mode}.json"
        meta_path = out / f"meta_{mode}.jsonl"
        preds = json.loads(preds_path.read_text()) if preds_path.exists() else {}
        with meta_path.open("a") as mf:
            for i, inst in enumerate(rows, 1):
                iid = inst["instance_id"]
                print(f"[{mode} {i}/{len(rows)}] {iid}", file=sys.stderr)
                try:
                    patch, meta = solve_instance(inst, mode, api_key, args.repo_dir, args.timeout, args.model, out)
                except subprocess.TimeoutExpired:
                    patch, meta = "", {"instance_id": iid, "mode": mode, "rc": "timeout"}
                preds[iid] = {
                    "instance_id": iid,
                    "model_name_or_path": f"codex-{mode}",
                    "model_patch": patch,
                }
                preds_path.write_text(json.dumps(preds, indent=2))
                mf.write(json.dumps(meta) + "\n"); mf.flush()

    print("\n=== predictions written ===", file=sys.stderr)
    for mode in modes:
        print(f"  {out}/preds_{mode}.json", file=sys.stderr)
    print("\n=== grade them (standard SWE-bench harness) ===", file=sys.stderr)
    for mode in modes:
        print(
            f"  python -m swebench.harness.run_evaluation \\\n"
            f"    --dataset_name {args.dataset} --split {args.split} \\\n"
            f"    --predictions_path {out}/preds_{mode}.json \\\n"
            f"    --run_id codex_{mode} --max_workers 4 --cache_level env",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
