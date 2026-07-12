#!/usr/bin/env python3
"""Run Claude Code (+/- unerr) on SWE-bench instances and write a predictions file.

Parallel to e2e/reference/codex/local-docker/run-benchmark.py — same A/B design, same
output layout, swapped agent: stock Claude Code CLI driven headless with
`claude -p`, authenticated by your PERSONAL SUBSCRIPTION (no API key).

Per instance:
  1. derive an image  FROM the official swebench/sweb.eval... image + toolbox
  2. docker run the driver -> unified diff (the prediction) on stdout
  3. record {instance_id, model_name_or_path, model_patch} into preds_<mode>.json

Grading is a separate, standard step (printed at the end) — this script only
produces the predictions; `swebench.harness.run_evaluation` scores them.

MODEL PINNED (--claude-model, default opus), otherwise default config. The bare
container has no ~/.claude/settings.json so it would fall back to sonnet-4-6;
pinning makes the run use the user's real default. The SAME model runs on both
arms, so the A/B delta stays purely "unerr on vs off".

Auth: run `claude setup-token` ONCE on your laptop, then
  export CLAUDE_CODE_OAUTH_TOKEN=...
This script passes that token into each container; billing goes to your Pro/Max
plan. (ANTHROPIC_API_KEY also works as a fallback if you prefer pay-per-token.)

Prereqs: docker, the `unerr-claude-toolbox` image (run ./build-toolbox.sh),
CLAUDE_CODE_OAUTH_TOKEN in the env, `pip install datasets`, ~30GB free disk.
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


def _run_tag(iid: str) -> str:
    return f"unerr-claude-run:{iid.replace('__', '_1776_').lower()}"


def preflight_instance(instance: dict, repo_dir: str, timeout: int) -> int:
    """Build + run the zero-cost preflight (no token, no claude). Returns rc."""
    instance_image = docker_image_for(instance)
    tag = _run_tag(instance["instance_id"])
    run(["docker", "pull", instance_image], check=False)
    build_instance_image(instance_image, tag)
    proc = run(
        ["docker", "run", "--rm", "-e", f"REPO_DIR={repo_dir}",
         tag, "/opt/toolbox/preflight.sh"],
        text=True, timeout=timeout,
    )
    return proc.returncode


def solve_instance(instance: dict, mode: str, auth_env: dict[str, str], repo_dir: str,
                   timeout: int, out_dir: Path, claude_model: str) -> tuple[str, dict]:
    """Build + run one instance, return (patch, meta). Empty patch on failure."""
    iid = instance["instance_id"]
    instance_image = docker_image_for(instance)
    tag = _run_tag(iid)

    # ensure the official env image is present, then graft the toolbox onto it
    run(["docker", "pull", instance_image], check=False)
    build_instance_image(instance_image, tag)

    problem = instance["problem_statement"]

    # create host artifact dir and mount it into the container
    art_host_dir = out_dir / "artifacts" / mode / iid
    art_host_dir.mkdir(parents=True, exist_ok=True)

    # Internal claude budget MUST be < this docker timeout so run-instance.sh's
    # own `timeout` fires first and still captures the partial diff/telemetry
    # (a host-side kill of `docker run` would lose all of it). Reserve ~1200s for
    # the ON-arm graph warm-up + image/exfil overhead.
    claude_timeout = max(300, timeout - 1200)
    docker_cmd = ["docker", "run", "--rm", "-i",
                  "-v", f"{art_host_dir.resolve()}:/work-out",
                  "-e", "ART_DIR=/work-out",
                  "-e", f"UNERR_MODE={mode}",
                  "-e", f"CLAUDE_TIMEOUT={claude_timeout}",
                  "-e", f"CLAUDE_MODEL={claude_model}",
                  "-e", f"REPO_DIR={repo_dir}"]
    # DEBUG-ONLY: forward the gated MCP-heartbeat flags into the instance container
    # so run-instance.sh can probe unerr health concurrently with claude -p.
    for passthru in ("DEBUG_MCP_PROBE", "PROBE_INTERVAL"):
        if os.environ.get(passthru):
            docker_cmd += ["-e", f"{passthru}={os.environ[passthru]}"]
    # subscription / api auth — pass through whichever is present (token preferred)
    for k, v in auth_env.items():
        docker_cmd += ["-e", f"{k}={v}"]
    docker_cmd += [tag, "bash", "-c",
                   "cat > /tmp/problem.txt && /opt/toolbox/run-instance.sh /tmp/problem.txt"]

    t0 = time.time()
    proc = run(docker_cmd, input=problem, text=True, capture_output=True, timeout=timeout)
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
    install_ok = "unerr install claude-code: ok" in (proc.stderr or "")

    meta = {
        "instance_id": iid, "mode": mode,
        "model": telemetry.get("model", ""),   # observed default model (not pinned)
        "wall_s": round(time.time() - t0, 1),
        "rc": proc.returncode, "patch_bytes": len(patch),
        "unerrd_up": unerrd_up, "install_ok": install_ok,
        "telemetry": telemetry,
        "artifacts_dir": f"artifacts/{mode}/{iid}",
        "stderr_tail": (proc.stderr or "")[-2000:],
    }
    return patch, meta


# Official SWE-bench Verified **Mini-50** instance ids (django 25 + sphinx 25).
# Same pinned allowlist as the codex runner so the two harnesses score the same
# tasks. Every id is in princeton-nlp/SWE-bench_Verified.
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
                    help="HF dataset id. Verified Mini = this filtered to 50 (use --mini).")
    ap.add_argument("--split", default="test")
    ap.add_argument("--slice", default="", help="e.g. 0:1 (smoke) or 0:50 (Verified Mini)")
    ap.add_argument("--filter", default="", help="regex on instance_id")
    ap.add_argument("--mini", action="store_true",
                    help="restrict to the pinned SWE-bench Verified Mini-50 (django+sphinx); "
                         "ignores --slice. Defaults to paced runs (see --pace).")
    ap.add_argument("--instances", type=int, default=0,
                    help="cap after slice/filter (0 = auto: 1 normally, all-50 with --mini; "
                         "an explicit value is always honored, e.g. --instances 1 --mini = smoke)")
    ap.add_argument("--mode", choices=["on", "off", "both"], default="both")
    ap.add_argument("--pace", type=int, default=-1,
                    help="seconds to sleep between instances to stay under subscription "
                         "rate limits. -1 = auto (0 normally, 30 with --mini). 0 = no pacing.")
    ap.add_argument("--preflight", action="store_true",
                    help="health-check only: verify unerr runs + MCP tools work in-image. "
                         "No token, no claude, zero cost.")
    ap.add_argument("--claude-model", default="opus",
                    help="model passed to `claude -p` (alias like 'opus'/'sonnet' or a full "
                         "id), IDENTICAL on both arms. Default 'opus' = the user's real default; "
                         "the bare container would otherwise fall back to sonnet-4-6.")
    ap.add_argument("--repo-dir", default="/testbed", help="repo root inside the instance image")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="per-instance TOTAL docker seconds (ceiling). The in-container "
                         "claude budget is this minus ~1200s reserved for the ON-arm graph "
                         "warm-up + overhead, so run-instance.sh times out first and still "
                         "captures any partial diff.")
    ap.add_argument("--out", default="results")
    ap.add_argument("--label", default="run",
                    help="label for this run; output goes to <out>/<label>/.")
    args = ap.parse_args()

    # SWE-bench instance images are linux/amd64 only; on Apple Silicon every
    # docker build/run/pull must target amd64 so the grafted toolbox (amd64) can
    # exec inside the x86_64 instance image (under emulation).
    os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

    # Subscription auth: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) is
    # preferred; ANTHROPIC_API_KEY is an accepted fallback. Either is passed
    # through to the container untouched.
    auth_env = {}
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        auth_env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        auth_env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    if not auth_env and not args.preflight:
        print("ERROR: set CLAUDE_CODE_OAUTH_TOKEN (run `claude setup-token` once) "
              "or ANTHROPIC_API_KEY — or use --preflight for the zero-cost check",
              file=sys.stderr)
        return 1

    from datasets import load_dataset  # lazy: only needed at run time
    rows = list(load_dataset(args.dataset, split=args.split))
    if args.mini:
        allow = set(MINI_50_IDS)
        rows = [r for r in rows if r["instance_id"] in allow]
        missing = allow - {r["instance_id"] for r in rows}
        if missing:
            print(f"WARNING: {len(missing)} Mini-50 ids absent from {args.dataset}: "
                  f"{sorted(missing)[:3]}...", file=sys.stderr)
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

    # Auto-pace: paced by default for --mini (many runs against a subscription's
    # 5h window); off otherwise. Explicit --pace always wins.
    pace = args.pace
    if pace < 0:
        pace = 30 if args.mini else 0

    if args.preflight:
        print(f"=== PREFLIGHT on {len(rows)} instance(s) — no token, zero cost ===", file=sys.stderr)
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

    print(f"instances={len(rows)} modes={modes} dataset={args.dataset} pace={pace}s", file=sys.stderr)
    for mode in modes:
        preds_path = out / f"preds_{mode}.json"
        meta_path = out / f"meta_{mode}.jsonl"
        preds = json.loads(preds_path.read_text()) if preds_path.exists() else {}
        with meta_path.open("a") as mf:
            for i, inst in enumerate(rows, 1):
                iid = inst["instance_id"]
                print(f"[{mode} {i}/{len(rows)}] {iid}", file=sys.stderr)
                try:
                    patch, meta = solve_instance(
                        inst, mode, auth_env, args.repo_dir, args.timeout, out,
                        args.claude_model)
                except subprocess.TimeoutExpired:
                    patch, meta = "", {"instance_id": iid, "mode": mode, "rc": "timeout"}
                preds[iid] = {
                    "instance_id": iid,
                    "model_name_or_path": f"claude-{mode}",
                    "model_patch": patch,
                }
                preds_path.write_text(json.dumps(preds, indent=2))
                mf.write(json.dumps(meta) + "\n"); mf.flush()
                if pace and i < len(rows):
                    time.sleep(pace)

    print("\n=== predictions written ===", file=sys.stderr)
    for mode in modes:
        print(f"  {out}/preds_{mode}.json", file=sys.stderr)
    print("\n=== grade them (standard SWE-bench harness) ===", file=sys.stderr)
    for mode in modes:
        print(
            f"  python -m swebench.harness.run_evaluation \\\n"
            f"    --dataset_name {args.dataset} --split {args.split} \\\n"
            f"    --predictions_path {out}/preds_{mode}.json \\\n"
            f"    --run_id claude_{mode} --max_workers 4 --cache_level env",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
