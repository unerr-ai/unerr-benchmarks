#!/usr/bin/env python3
"""tigris_archive.py — persist ONE distributed run's DATA (not code) to Tigris
(fly.io S3-compatible object storage), organized for later lookup, so the
coordinator + workers can be torn down and the run's traces / grading /
submission / overview still survive.

Runs on the COORDINATOR at end-of-run (coordinator-entrypoint.sh §7, after the
bundle is built, before the HOLD) — the fleet then archives itself; a host pull
is no longer the only way results escape the volume. Also runnable from the host
against a pulled bundle dir.

It does three things:
  1. GENERATE overview.json — a compact run summary (grade %, cost + per-tier
     token/turn breakdown from meta.jsonl, status counts, timing, per-instance
     rows). Same cost normalization the report/status tooling uses: econ from
     telemetry/tier_cost_db, claude from litellm_spend_logs — always real
     LiteLLM spend.
  2. GENERATE submission/ (resolve_then_grade only) via make_submission.py
     (best-effort — empty-patch runs still archive their partial submission).
  3. UPLOAD the organized tree to s3://<bucket>/<prefix>/<benchmark>/<arm>/
     <date>/<label>/ under stable category folders.

S3 KEY LAYOUT (bucket + prefix from flags/env):
    <prefix>/<benchmark>/<arm>/<YYYY-MM-DD>/<label>/
        overview.json                     run summary (grade + cost + counts + timing)
        bundle.tgz                        the full tarball (one-shot restore)
        submission/preds.json             resolve_then_grade only
        submission/all_preds.jsonl
        grading/merged.json               merged grade report
        grading/<iid>/report.json         per-instance swebench grade
        traces/<iid>/events.jsonl         per-instance execution trace ...
        traces/<iid>/err.txt
        traces/<iid>/engine.log
        traces/<iid>/opencode.db          (econ)
        traces/<iid>/trajectory.json      (terminal)
        traces/<iid>/sessions.cast        (terminal)
        results/{preds.json,meta.jsonl,dead.jsonl,cost-report.md}
        logs/<file>                       coordinator/server/merge/report logs
        db/queue.db                       the work-queue sqlite (durable audit)

Auth: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (Tigris keypair from
`flyctl storage create -a <fleet-app>`), endpoint AWS_ENDPOINT_URL_S3 (default
https://fly.storage.tigris.dev), region auto. Bucket from --bucket or
TIGRIS_BUCKET / BUCKET_NAME.

Exit codes: 0 = uploaded (or --dry-run), 2 = bad args, 3 = no creds/bucket,
4 = upload error. The coordinator treats a non-zero archive as NON-fatal (it
still holds the bundle for a host pull) — archival is additive, never a gate.
"""
import argparse
import importlib.util
import json
import os
import pathlib
import sys

DEFAULT_ENDPOINT = "https://fly.storage.tigris.dev"
_TOOLS_DIR = pathlib.Path(__file__).resolve().parent


def _log(msg):
    print(f"[tigris-archive] {msg}", file=sys.stderr)


# ── cost normalization (same 3-shape logic as status.sh --cost / report.py) ────
def _num(v, default=0):
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default


def _norm_cost(meta):
    """One meta record -> (usd, turns, by_tier, source). Prefer claude litellm cost,
    else econ tier_cost_db (sqlite), else the telemetry stream. `source` surfaces
    meta["cost"]["source"] verbatim — the claude-real arm stamps "claude-native"
    there (real-Anthropic $, never LiteLLM spend) so callers can label it distinctly
    instead of folding it into the claude/econ cost paths unlabeled."""
    tel = meta.get("telemetry") or {}
    tcd = meta.get("tier_cost_db") or {}
    cst = meta.get("cost") if isinstance(meta.get("cost"), dict) else {}
    usd = cst.get("usd")
    if usd is None:
        usd = tcd.get("usd")
    if usd is None:
        usd = tel.get("usd")
    turns = _num(tel.get("turns"), 0)
    by_tier = cst.get("by_tier") or tcd.get("by_tier") or tel.get("by_tier") or {}
    return ((float(usd) if usd is not None else None), turns,
            (by_tier if isinstance(by_tier, dict) else {}), cst.get("source"))


def build_overview(data_dir, label, arm, benchmark, dataset, started_at, finished_at):
    """Assemble overview.json from the coordinator's on-volume outputs
    (reports/merged.<label>.json + results/<label>/{meta.jsonl,dead.jsonl})."""
    data = pathlib.Path(data_dir)
    rundir = data / "results" / label
    merged_path = data / "reports" / f"merged.{label}.json"

    resolved = total = None
    grade = None
    if merged_path.is_file():
        try:
            g = json.loads(merged_path.read_text())
            resolved = g.get("resolved_instances")
            total = g.get("submitted_instances") or g.get("total_instances")
            grade = g
        except Exception:
            pass

    # per-instance cost/turns + per-tier aggregate from meta.jsonl
    fleet_usd = 0.0
    turns_total = tin = tout = priced = 0
    by_tier = {}
    per_instance = {}
    # claude-native (claude-real arm's real-Anthropic $, NOT litellm spend) tracked
    # separately so it's included in fleet_usd but never mislabeled — see _norm_cost.
    native_usd = 0.0
    native_priced = native_na = 0
    meta_path = rundir / "meta.jsonl"
    if meta_path.is_file():
        for line in meta_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            iid = m.get("instance_id") or "?"
            usd, turns, bt, cost_source = _norm_cost(m)
            is_native = cost_source == "claude-native"
            tel = m.get("telemetry") or {}
            per_instance[iid] = {
                "usd": round(usd, 6) if usd is not None else None,
                "turns": turns,
                "wall_s": m.get("wall_s"),
            }
            if is_native:
                per_instance[iid]["cost_source"] = "claude-native"
            if usd is not None:
                fleet_usd += usd
                priced += 1
                if is_native:
                    native_usd += usd
                    native_priced += 1
            elif is_native:
                native_na += 1
            turns_total += turns
            tin += _num(tel.get("in_tokens"), 0)
            tout += _num(tel.get("out_tokens"), 0)
            for tier, t in (bt or {}).items():
                if not isinstance(t, dict):
                    continue
                a = by_tier.setdefault(tier, dict(usd=0.0, in_tokens=0, out_tokens=0, calls=0, instances=0))
                a["usd"] += _num(t.get("usd"), 0.0)
                a["in_tokens"] += _num(t.get("in_tokens"), 0)
                a["out_tokens"] += _num(t.get("out_tokens"), 0)
                a["calls"] += _num(t.get("requests"), 0) or _num(t.get("messages"), 0)
                a["instances"] += 1
    for a in by_tier.values():
        a["usd"] = round(a["usd"], 6)

    dead = []
    dead_path = rundir / "dead.jsonl"
    if dead_path.is_file():
        for line in dead_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    dead.append(json.loads(line))
                except ValueError:
                    pass

    pct = round(100 * resolved / total, 1) if (resolved is not None and total) else None
    return {
        "label": label,
        "arm": arm,
        "benchmark": benchmark,
        "dataset": dataset,
        "started_at": started_at,
        "finished_at": finished_at,
        "grade": {"resolved": resolved, "total": total, "pct": pct},
        "cost": {
            "usd": round(fleet_usd, 6),
            "source": "real-litellm-spend",
            "priced_instances": priced,
            "turns": turns_total,
            "in_tokens": tin,
            "out_tokens": tout,
            "by_tier": by_tier,
            # Present only when the run has claude-native (claude-real arm)
            # records — its $ is already folded into "usd" above but broken out
            # here so it's never misread as litellm spend. usd:null + na>0 means
            # some/all instances had no tracked Anthropic-side $ (n/a, not $0).
            **({"claude_native": {
                    "usd": round(native_usd, 6) if native_priced else None,  # None (n/a) when every native row is untracked
                    "priced_instances": native_priced,
                    "na_instances": native_na,
                }} if (native_priced or native_na) else {}),
        },
        "counts": {"resolved": resolved, "total": total, "dead": len(dead)},
        "dead": [d.get("instance_id") for d in dead if isinstance(d, dict)],
        "instances": per_instance,
    }


# ── submission (resolve_then_grade only) ───────────────────────────────────────
def gen_submission(data_dir, label, model_name):
    """Best-effort: run make_submission.py against results/<label>/ so
    submission/{preds.json,all_preds.jsonl} exist to archive. Empty-patch runs
    still archive their partial submission (we swallow the coverage exit)."""
    rundir = pathlib.Path(data_dir) / "results" / label
    if not (rundir / "preds.json").is_file():
        _log("no preds.json — skipping submission (harness_run benchmark, or nothing resolved)")
        return
    try:
        spec = importlib.util.spec_from_file_location("make_submission", _TOOLS_DIR / "make_submission.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        argv = [str(rundir)]
        if model_name:
            argv += ["--model-name", model_name]
        rc = mod.main(argv)
        _log(f"submission built (make_submission rc={rc}, empty-patch rows are non-fatal for archive)")
    except SystemExit as e:
        _log(f"submission: make_submission exited {e.code} (archiving whatever it produced)")
    except Exception as e:  # noqa: BLE001 — never let submission-gen abort the archive
        _log(f"submission: skipped ({type(e).__name__}: {e})")


# ── the upload plan: (local_path, s3_key_suffix) pairs under the run prefix ─────
def plan_uploads(data_dir, label):
    data = pathlib.Path(data_dir)
    rundir = data / "results" / label
    pairs = []

    def add(local, key):
        p = pathlib.Path(local)
        if p.is_file():
            pairs.append((str(p), key))

    def add_tree(local_root, key_prefix):
        root = pathlib.Path(local_root)
        if not root.is_dir():
            return
        for f in sorted(root.rglob("*")):
            if f.is_file():
                pairs.append((str(f), f"{key_prefix}/{f.relative_to(root).as_posix()}"))

    # per-instance traces  (artifacts/<iid>/* -> traces/<iid>/*)
    add_tree(rundir / "artifacts", "traces")
    # grading: merged + per-instance reports
    add(data / "reports" / f"merged.{label}.json", "grading/merged.json")
    add_tree(data / "logs" / "grade-merged", "grading")
    # submission (generated)
    add_tree(rundir / "submission", "submission")
    # results data files (not the artifacts/ subtree — that's traces/)
    for name in ("preds.json", "meta.jsonl", "dead.jsonl", "cost-report.md", "cost-report.json"):
        add(rundir / name, f"results/{name}")
    # coordinator logs (skip grade-merged — already under grading/)
    logs = data / "logs"
    if logs.is_dir():
        for f in sorted(logs.rglob("*")):
            if f.is_file() and "grade-merged" not in f.relative_to(logs).parts:
                pairs.append((str(f), f"logs/{f.relative_to(logs).as_posix()}"))
    # the work-queue db (durable audit) + the full bundle (one-shot restore)
    add(data / "queue.db", "db/queue.db")
    add(data / "bundle.tgz", "bundle.tgz")
    add(rundir / "overview.json", "overview.json")   # generated into results/<label>/ so it also rides the bundle
    return pairs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="/data", help="coordinator volume root (has results/,reports/,logs/,bundle.tgz)")
    ap.add_argument("--label", required=True, help="run label / RUN_ID (unique per run)")
    ap.add_argument("--arm", default=os.environ.get("ARM", "econ"))
    ap.add_argument("--benchmark", default=os.environ.get("BENCHMARK", "verified"))
    ap.add_argument("--dataset", default=os.environ.get("DATASET", ""))
    ap.add_argument("--bucket", default=os.environ.get("TIGRIS_BUCKET") or os.environ.get("BUCKET_NAME"))
    ap.add_argument("--prefix", default=os.environ.get("TIGRIS_PREFIX", "runs"))
    ap.add_argument("--endpoint", default=os.environ.get("AWS_ENDPOINT_URL_S3", DEFAULT_ENDPOINT))
    ap.add_argument("--date", default=os.environ.get("RUN_DATE", ""), help="UTC YYYY-MM-DD (default: today)")
    ap.add_argument("--started-at", default=os.environ.get("RUN_STARTED_AT", ""))
    ap.add_argument("--finished-at", default=os.environ.get("RUN_FINISHED_AT", ""))
    ap.add_argument("--model-name", default=os.environ.get("SUBMISSION_MODEL_NAME", ""))
    ap.add_argument("--no-submission", action="store_true", help="skip submission generation (harness_run benchmarks)")
    ap.add_argument("--generate-only", action="store_true", help="write overview.json + submission into the run dir, then stop (no S3) — call before bundling so both ride bundle.tgz")
    ap.add_argument("--dry-run", action="store_true", help="print the upload plan + overview; do NOT touch S3")
    args = ap.parse_args(argv)

    data_dir = pathlib.Path(args.data_dir)
    if not data_dir.is_dir():
        _log(f"data-dir not found: {data_dir}")
        return 2

    # date: prefer explicit; else derive from finished/started; else today (UTC).
    date = args.date
    if not date:
        import datetime
        date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    # 1. submission (resolve_then_grade)
    if not args.no_submission:
        gen_submission(str(data_dir), args.label, args.model_name)

    # 2. overview.json — written into results/<label>/ so it ALSO rides bundle.tgz
    overview = build_overview(str(data_dir), args.label, args.arm, args.benchmark,
                              args.dataset, args.started_at or None, args.finished_at or None)
    ov_path = data_dir / "results" / args.label / "overview.json"
    ov_path.parent.mkdir(parents=True, exist_ok=True)
    ov_path.write_text(json.dumps(overview, indent=2), encoding="utf-8")
    g = overview["grade"]
    _log(f"overview: resolved={g['resolved']}/{g['total']} ({g['pct']}%)  cost=${overview['cost']['usd']}  tiers={list(overview['cost']['by_tier'])}")

    # --generate-only: overview + submission now exist under results/<label>/ (so
    # the bundle step includes them); the caller uploads in a later pass.
    if args.generate_only:
        _log(f"generate-only: wrote {ov_path} + submission (no upload)")
        return 0

    # 3. plan + upload
    base_key = "/".join(p for p in [args.prefix, args.benchmark, args.arm, date, args.label] if p)
    pairs = plan_uploads(str(data_dir), args.label)
    total_bytes = sum(os.path.getsize(lp) for lp, _ in pairs if os.path.isfile(lp))
    _log(f"{len(pairs)} object(s), {total_bytes/1e6:.1f} MB -> s3://{args.bucket or '<bucket?>'}/{base_key}/")

    if args.dry_run:
        for lp, key in pairs:
            print(f"  {base_key}/{key}\t<- {lp}")
        print(json.dumps(overview, indent=2))
        return 0

    if not args.bucket:
        _log("no bucket (set --bucket or TIGRIS_BUCKET/BUCKET_NAME) — cannot upload")
        return 3
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        _log("no AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in env — is the fleet app storage-attached? (flyctl storage create -a <app>)")
        return 3

    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        _log("boto3 not installed in this image — add it to the coordinator venv (Dockerfile.dist)")
        return 3

    s3 = boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        region_name=os.environ.get("AWS_REGION", "auto"),
        config=Config(retries={"max_attempts": 5, "mode": "standard"}, s3={"addressing_style": "path"}),
    )
    ok = err = 0
    for lp, key in pairs:
        full_key = f"{base_key}/{key}"
        try:
            s3.upload_file(lp, args.bucket, full_key)
            ok += 1
        except Exception as e:  # noqa: BLE001
            err += 1
            _log(f"  FAILED {full_key}: {type(e).__name__}: {e}")
    _log(f"uploaded {ok}/{len(pairs)} object(s) ({err} error(s)) -> s3://{args.bucket}/{base_key}/")
    return 0 if err == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
