#!/usr/bin/env python3
"""make_tb_submission.py — emit a Terminal-Bench 2.1 leaderboard-format
submission mirror from a distributed run's on-disk data (meta.jsonl +
reports/merged.<label>.json + dead.jsonl + results/<label>/artifacts/), fully
parameterized via TB_* env knobs (see e2e/distributed/out/SOL_AB_PLAN.md
"Parameters").

This is a LOCAL mirror of the schema documented in leaderboard/SUBMIT.md, NOT
a hub-derived submission: `source_jobs` is intentionally empty (this harness
never runs `harbor run --upload` to hub.harborframework.com) and `metrics` is
computed locally from this run's meta.jsonl/merged.json rather than
CI-derived from Harbor-hub trials. See README-gaps.md (written alongside the
submission JSON, one per invocation) for the 4 HARD leaderboard-eligibility
requirements this run does or doesn't satisfy, and the env knob that fixes
each up for the next run.

Reuses tigris_archive.py's build_overview()/_norm_cost() data model (same
meta.jsonl/merged.json parsing already used for overview.json) rather than
re-implementing it.

CLI:
    make_tb_submission.py --label <RUN_ID> [--data-dir <dir>] [--out-dir <dir>]
        [--agent-name ...] [--agent-version ...] [--model-id ...]
        [--reasoning-effort ...] [--display-name/--display-url/--display-org/--org-url ...]
        [--dataset-ref ...] [--trials-per-task N]

Every flag has a TB_* env fallback (TB_AGENT_NAME, TB_AGENT_VERSION,
TB_MODEL_ID, TB_REASONING_EFFORT, TB_DISPLAY_NAME, TB_DISPLAY_URL,
TB_DISPLAY_ORG, TB_ORG_URL, TB_DATASET_REF, TB_TRIALS_PER_TASK) so a
coordinator or CI step can drive this without CLI wiring.

Writes into --out-dir (default <data-dir>/results/<label>/tb-submission/):
    <date>-<model>-<effort>-<agent>.json   the leaderboard submission (model
                                            id's '/' sanitized to '-')
    trials.jsonl                            one row per task: {instance_id,
                                            reward, in_tokens, out_tokens,
                                            cost_usd, wall_s, trajectory_path}
    README-gaps.md                          the 4 HARD requirements + which
                                            are unmet by this run + the env
                                            knob that fixes each up

Exit codes: always 0 — best-effort throughout (never crash on a missing
file; a fully empty/absent run still gets a well-formed, zero-metric
submission plus a note in README-gaps.md).
"""
import argparse
import datetime
import importlib.util
import json
import os
import pathlib
import sys

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_AGENT_NAME = "claude-code-unerr"
DEFAULT_AGENT_VERSION = "1"
DEFAULT_MODEL_ID_FALLBACK = "openai/gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "none"
DEFAULT_TRIALS_PER_TASK = 1

# per-instance trajectory candidates under artifacts/<iid>/, most ATIF-like
# first — mirrors harness_terminal.py's _collect_traces artifact naming.
_TRAJECTORY_CANDIDATES = ("trajectory.json", "claude-session.jsonl", "sessions.cast")


def _log(msg):
    print(f"[make-tb-submission] {msg}", file=sys.stderr)


def _import_tool(module_filename):
    """Import a sibling tools/<name>.py by file path (same pattern
    make_submission.py uses for collect-failed.py) so tigris_archive's
    meta.jsonl/merged.json parsing is reused rather than re-implemented."""
    spec = importlib.util.spec_from_file_location(module_filename, _TOOLS_DIR / f"{module_filename}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tigris_archive = _import_tool("tigris_archive")


def _env_int(name, default):
    v = os.environ.get(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def resolve_model_id(cli_value):
    """--model-id / TB_MODEL_ID, else ANTHROPIC_DEFAULT_OPUS_MODEL (the
    conductor's actual model alias for the claude-gpt/sol arm), else the
    hardcoded fallback."""
    if cli_value:
        return cli_value
    return os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL") or DEFAULT_MODEL_ID_FALLBACK


def sanitize_model_for_filename(model_id):
    return model_id.replace("/", "-")


def load_merged(data_dir, label):
    """Best-effort load of reports/merged.<label>.json -> dict (or {} if
    missing/unparseable — never raises)."""
    p = pathlib.Path(data_dir) / "reports" / f"merged.{label}.json"
    if not p.is_file():
        _log(f"no merged.{label}.json — resolved_ids will be empty (reward=0 for every trial)")
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _log(f"merged.{label}.json unreadable ({type(e).__name__}: {e}) — treating as empty")
        return {}


def load_meta_rows(data_dir, label):
    """Read results/<label>/meta.jsonl -> list of raw dict rows, skipping any
    unparseable line (mirrors tigris_archive.build_overview's own loop)."""
    p = pathlib.Path(data_dir) / "results" / label / "meta.jsonl"
    rows = []
    if not p.is_file():
        return rows
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict):
            rows.append(m)
    return rows


def find_trajectory_path(data_dir, label, iid):
    """Best-effort locate a trajectory-shaped artifact under
    results/<label>/artifacts/<iid>/ for ATIF trajectory_path. Returns a path
    relative to data_dir, or None when no artifact exists."""
    art_dir = pathlib.Path(data_dir) / "results" / label / "artifacts" / iid
    for name in _TRAJECTORY_CANDIDATES:
        cand = art_dir / name
        if cand.is_file():
            return str(cand.relative_to(pathlib.Path(data_dir)))
    return None


def build_trials(data_dir, label, resolved_ids):
    """meta.jsonl rows -> one trials.jsonl row per task: {instance_id,
    reward, in_tokens, out_tokens, cost_usd, wall_s, trajectory_path}."""
    rows = []
    for m in load_meta_rows(data_dir, label):
        iid = m.get("instance_id") or "?"
        usd, _turns, _by_tier, _source = tigris_archive._norm_cost(m)
        tel = m.get("telemetry") or {}
        rows.append({
            "instance_id": iid,
            "reward": 1 if iid in resolved_ids else 0,
            "in_tokens": tigris_archive._num(tel.get("in_tokens"), 0),
            "out_tokens": tigris_archive._num(tel.get("out_tokens"), 0),
            "cost_usd": round(usd, 6) if usd is not None else None,
            "wall_s": m.get("wall_s"),
            "trajectory_path": find_trajectory_path(data_dir, label, iid),
        })
    rows.sort(key=lambda r: r["instance_id"])
    return rows


def build_submission(overview, args, date_str):
    """Assemble the TB-2.1 leaderboard submission dict (see module docstring
    + SOL_AB_PLAN.md for the exact schema this mirrors)."""
    resolved = overview.get("grade", {}).get("resolved")
    total = overview.get("grade", {}).get("total")
    accuracy = round(resolved / total, 6) if (resolved is not None and total) else None
    cost = overview.get("cost", {}) or {}
    try:
        display_date = (
            datetime.datetime.strptime(date_str, "%Y-%m-%d").strftime("%B %d, %Y") if date_str else None
        )
    except ValueError:
        display_date = None
    return {
        "source_jobs": [],
        "source_filter": {
            "agent": args.agent_name,
            "agent_version": args.agent_version,
            "model": args.model_id,
            "reasoning_effort": args.reasoning_effort,
        },
        "metadata": {
            "date": date_str,
            "display_date": display_date,
            "reasoning_effort": args.reasoning_effort,
            "display_name": args.display_name,
            "display_url": args.display_url,
            "display_org": args.display_org,
            "org_url": args.org_url,
        },
        "metrics": {
            "accuracy": accuracy,
            "resolved": resolved,
            "total": total,
            "n_trials_per_task": args.trials_per_task,
            "pass@1": accuracy,
            "total_in_tokens": cost.get("in_tokens"),
            "total_out_tokens": cost.get("out_tokens"),
            "total_cost_usd": cost.get("usd"),
            "cost_source": "real-litellm-spend",
        },
        "disqualified_trials": [],
        "_local": True,
        "_note": (
            "Locally-computed mirror of the TB-2.1 leaderboard submission schema — "
            "NOT hub-derived CI metrics. No `harbor run --upload` was performed "
            "(source_jobs is intentionally empty); metrics are computed directly from "
            "this run's meta.jsonl/merged.json. See README-gaps.md for the HARD "
            "leaderboard-eligibility requirements unmet by this run."
        ),
    }


def build_gaps_readme(args, trials, merged_loaded):
    """The 4 HARD requirements (SOL_AB_PLAN.md) + this run's status against
    each + the env knob that fixes it up for the next run."""
    n_trials = args.trials_per_task
    req1_met = n_trials is not None and n_trials >= 5
    resolved_missing_traj = sorted(
        t["instance_id"] for t in trials if t["reward"] == 1 and not t["trajectory_path"]
    )
    req4_met = bool(trials) and not resolved_missing_traj

    lines = [
        "# TB-2.1 leaderboard submission — HARD requirement gaps",
        "",
        "This submission is a LOCAL mirror of the TB-2.1 leaderboard schema (see "
        "`e2e/distributed/out/SOL_AB_PLAN.md`), generated from this distributed run's "
        "on-disk data — not from a Harbor-hub upload. Four HARD requirements gate real "
        "leaderboard eligibility; below is this run's status against each, and the env "
        "knob that fixes it up for the next run.",
        "",
        "## 1. >=5 trials per task, ALL tasks",
        f"- Status: {'MET' if req1_met else 'GAP'} (this run recorded TB_TRIALS_PER_TASK={n_trials})",
        "- Fix: set `TB_TRIALS_PER_TASK=5` (or higher) and actually execute each task 5x "
        "before capture — this tool only records the param on the submission; it does "
        "not itself re-run tasks.",
        "",
        "## 2. Upload to Harbor hub (`harbor run --upload`)",
        "- Status: GAP (always, for this local capture path) — `source_jobs` is "
        "intentionally `[]`.",
        "- Fix: no env knob here — requires the run pipeline to push through "
        "`harbor run --upload` to hub.harborframework.com; until that lands this tool "
        "remains a local mirror of the schema for archival/inspection only.",
        "",
        "## 3. Unmodified dataset + default execution settings",
        (
            f"- Status: {'dataset ref recorded' if args.dataset_ref else 'UNCONFIRMED — no TB_DATASET_REF set'} "
            f"(TB_DATASET_REF={args.dataset_ref or '(unset)'}; merged.json "
            f"{'found' if merged_loaded else 'NOT found — grade/reward data may be incomplete'})"
        ),
        "- Fix: set `TB_DATASET_REF=<DATASET@REF>` (from core/hub.py) to pin+record the "
        "dataset revision used, and confirm no timeout_multiplier/resource overrides "
        "were layered on top of this run (task-level enforcement is already removed "
        "repo-wide as of 2026-07-18 — this only confirms no per-run override was added).",
        "",
        "## 4. trajectory_path (ATIF) per rewarded trial",
        (
            f"- Status: {'MET' if req4_met else 'GAP'} "
            f"({len(resolved_missing_traj)} resolved instance(s) missing a trajectory_path)"
        ),
        (
            f"- Missing: {', '.join(resolved_missing_traj)}" if resolved_missing_traj else
            "- Every resolved instance has a trajectory_path (see trials.jsonl)."
        ),
        "- Fix: no env knob needed — trajectory_path is derived automatically from "
        "results/<label>/artifacts/<iid>/{trajectory.json,claude-session.jsonl,"
        "sessions.cast} when present; keep per-instance artifact capture enabled for "
        "the run.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"),
                     help="coordinator volume root or a pulled bundle dir (has results/,reports/)")
    ap.add_argument("--label", required=True, help="run label / RUN_ID (unique per run)")
    ap.add_argument("--out-dir", default=None,
                     help="output dir (default: <data-dir>/results/<label>/tb-submission)")
    ap.add_argument("--arm", default=os.environ.get("ARM", ""))
    ap.add_argument("--benchmark", default=os.environ.get("BENCHMARK", "terminal"))
    ap.add_argument("--dataset", default=os.environ.get("DATASET", ""))
    ap.add_argument("--date", default=os.environ.get("RUN_DATE", ""), help="UTC YYYY-MM-DD (default: today)")
    ap.add_argument("--agent-name", dest="agent_name",
                     default=os.environ.get("TB_AGENT_NAME") or DEFAULT_AGENT_NAME)
    ap.add_argument("--agent-version", dest="agent_version",
                     default=os.environ.get("TB_AGENT_VERSION") or DEFAULT_AGENT_VERSION)
    ap.add_argument("--model-id", dest="model_id", default=os.environ.get("TB_MODEL_ID", ""))
    ap.add_argument("--reasoning-effort", dest="reasoning_effort",
                     default=os.environ.get("TB_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT)
    ap.add_argument("--display-name", dest="display_name", default=os.environ.get("TB_DISPLAY_NAME") or None)
    ap.add_argument("--display-url", dest="display_url", default=os.environ.get("TB_DISPLAY_URL") or None)
    ap.add_argument("--display-org", dest="display_org", default=os.environ.get("TB_DISPLAY_ORG") or None)
    ap.add_argument("--org-url", dest="org_url", default=os.environ.get("TB_ORG_URL") or None)
    ap.add_argument("--dataset-ref", dest="dataset_ref", default=os.environ.get("TB_DATASET_REF") or None)
    ap.add_argument("--trials-per-task", dest="trials_per_task", type=int,
                     default=_env_int("TB_TRIALS_PER_TASK", DEFAULT_TRIALS_PER_TASK))
    args = ap.parse_args(argv)

    args.model_id = resolve_model_id(args.model_id)

    data_dir = pathlib.Path(args.data_dir)
    if not data_dir.is_dir():
        _log(f"data-dir not found: {data_dir} — proceeding best-effort (empty/zero submission)")

    out_dir = pathlib.Path(args.out_dir) if args.out_dir else data_dir / "results" / args.label / "tb-submission"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log(f"cannot create out-dir {out_dir} ({type(e).__name__}: {e}) — aborting")
        return 1

    date_str = args.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    try:
        overview = tigris_archive.build_overview(
            str(data_dir), args.label, args.arm or "unknown", args.benchmark, args.dataset, None, None
        )
    except Exception as e:  # noqa: BLE001 — never let a bad run dir crash this tool
        _log(f"build_overview failed ({type(e).__name__}: {e}) — using an empty overview")
        overview = {"grade": {"resolved": None, "total": None, "pct": None},
                    "cost": {"usd": None, "in_tokens": 0, "out_tokens": 0}}

    try:
        merged = load_merged(str(data_dir), args.label)
    except Exception as e:  # noqa: BLE001
        _log(f"load_merged failed ({type(e).__name__}: {e}) — treating as empty")
        merged = {}
    resolved_ids = set(merged.get("resolved_ids") or [])

    try:
        trials = build_trials(str(data_dir), args.label, resolved_ids)
    except Exception as e:  # noqa: BLE001
        _log(f"build_trials failed ({type(e).__name__}: {e}) — writing 0 trial rows")
        trials = []

    submission = build_submission(overview, args, date_str)

    fname = f"{date_str}-{sanitize_model_for_filename(args.model_id)}-{args.reasoning_effort}-{args.agent_name}.json"
    sub_path = out_dir / fname
    sub_path.write_text(json.dumps(submission, indent=2), encoding="utf-8")

    trials_path = out_dir / "trials.jsonl"
    with open(trials_path, "w", encoding="utf-8") as f:
        for row in trials:
            f.write(json.dumps(row) + "\n")

    gaps_path = out_dir / "README-gaps.md"
    gaps_path.write_text(build_gaps_readme(args, trials, bool(merged)), encoding="utf-8")

    g = overview.get("grade", {})
    _log(f"tb-submission: {g.get('resolved')}/{g.get('total')} resolved, "
         f"{len(trials)} trial row(s) -> {sub_path.name}")
    _log(f"written: {sub_path}")
    _log(f"written: {trials_path}")
    _log(f"written: {gaps_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
