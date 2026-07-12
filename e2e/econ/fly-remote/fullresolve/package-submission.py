#!/usr/bin/env python3
"""Package a fly fullresolve bundle into a SWE-bench leaderboard submission dir.

Closes the *technical* packaging gaps from SUBMISSION.md §4 — everything except
the two policy items (academic affiliation + a published report, which are not
files). Given a pulled bundle (`out/<label>-bundle/`) it emits the submission
layout the leaderboard expects:

    <out>/
      preds.json                 # predictions (copied — already canonical)
      metadata.yaml              # SCAFFOLD (fill authors/report/org before PR)
      README.md                  # SCAFFOLD (paste get_results output + report)
      logs/<iid>/                # flattened swebench eval artifacts (→ S3)
        {report.json, test_output.txt, patch.diff, run_instance.log, eval.sh}
      trajs/<iid>.jsonl          # reasoning trace = the live events.jsonl (→ S3)
      trajs/<iid>.md             # human-readable render of the same trace
      results/results.json       # PREVIEW (resolved/no_generation/no_logs);
                                 #   `analysis.get_results` regenerates canonically

It never runs `analysis.get_results` (that lives in the experiments repo and
deletes non-required files) — it prints the command. It never uploads to S3 —
it prints the paths. Nothing here is destructive to the bundle.

Handles the real edge case: an empty-patch instance (e.g. django-12039) has NO
grade dir → classified `no_generation`, still gets a traj (it did run). An
instance with a patch but no report → `no_logs`.

Usage:
  python3 package-submission.py --bundle out/econ-v9-bundle --label econ-v9 \
      --model minimax-m2 --system econ [--date 20260712] [--out DIR] \
      [--name "econ + MiniMax-M2 (unerr)"] [--org unerr] [--site URL] \
      [--report URL] [--authors "A, B"] [--oss false]

Stdlib only. Run from e2e/econ/fly-remote/fullresolve/ (or pass absolute paths).
"""
import argparse, json, os, shutil, sys, datetime
from pathlib import Path

# swebench per-instance eval files → which land in the submission logs/<iid>/.
# report.json/test_output.txt/patch.diff are the required trio; the last two are
# "not necessary" per the spec but harmless and aid reproducibility.
LOG_FILES = ["report.json", "test_output.txt", "patch.diff", "run_instance.log", "eval.sh"]


def render_traj_md(events_path: Path, iid: str) -> str:
    """Render events.jsonl → a human-readable Markdown reasoning trace.

    The leaderboard wants a text trace that "reflects the intermediate steps" and
    is human-readable. We map the live event stream: turn_phase → section header
    (step / phase / agent tier), reasoning → prose, tool_use → the tool + its
    input and a truncated output. Unknown event types are skipped, so a schema
    drift degrades to a shorter trace rather than crashing.
    """
    out = [f"# Reasoning trace — `{iid}`\n",
           "_Generated with the inference process (live event stream), "
           "not post-hoc. Source: `events.jsonl`._\n"]
    n_reason = n_tool = 0
    for line in _iter_jsonl(events_path):
        t = line.get("type")
        if t == "turn_phase":
            step, phase, agent = line.get("step"), line.get("phase"), line.get("agent")
            out.append(f"\n## Step {step} — {phase} · `{agent}`\n")
        elif t == "reasoning":
            txt = (line.get("part") or {}).get("text", "").strip()
            if txt:
                n_reason += 1
                out.append(f"{txt}\n")
        elif t == "tool_use":
            part = line.get("part") or {}
            tool = part.get("tool", "?")
            state = part.get("state") or {}
            inp = state.get("input")
            outp = state.get("output")
            status = state.get("status", "")
            n_tool += 1
            out.append(f"\n**🔧 tool: `{tool}`** ({status})")
            if inp is not None:
                inp_s = inp if isinstance(inp, str) else json.dumps(inp, indent=2)
                out.append(f"\n_input:_\n```\n{_clip(inp_s, 1500)}\n```")
            if outp:
                out.append(f"\n_output:_\n```\n{_clip(str(outp), 1500)}\n```")
            out.append("")
    out.insert(2, f"\n_{n_reason} reasoning steps · {n_tool} tool calls._\n")
    return "\n".join(out)


def _iter_jsonl(path: Path):
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n… [+{len(s) - n} chars truncated]"


def find_grade_dir(bundle: Path, label: str, model: str, iid: str) -> Path | None:
    """Locate the swebench per-instance eval dir for this run's label.

    Path shape: <bundle>/logs/grade-<label>/logs/run_evaluation/<label>/<model>/<iid>/.
    Scoped to THIS label's grade dir only — the /data volume carries every prior
    run's grade dirs, so a loose search would pull a stale instance's report
    (the exact stale-report trap from prior runs). Returns None when absent
    (empty-patch/no_generation instances have no grade dir).
    """
    base = bundle / "logs" / f"grade-{label}" / "logs" / "run_evaluation" / label
    cand = base / model / iid
    if cand.is_dir():
        return cand
    # tolerate a different model-subdir name: match by iid under THIS label only
    if base.is_dir():
        for m in base.iterdir():
            if (m / iid).is_dir():
                return m / iid
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, help="pulled bundle dir, e.g. out/econ-v9-bundle")
    ap.add_argument("--label", required=True, help="run label, e.g. econ-v9")
    ap.add_argument("--model", required=True, help="model id for metadata, e.g. minimax-m2")
    ap.add_argument("--system", default="econ", help="agent/system name (default econ)")
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y%m%d"),
                    help="submission date YYYYMMDD (default today)")
    ap.add_argument("--out", default="", help="submission dir (default submissions/<date>_<system>_<model>)")
    ap.add_argument("--name", default="", help="leaderboard display name")
    ap.add_argument("--org", default="TODO-org")
    ap.add_argument("--site", default="TODO-site-url")
    ap.add_argument("--report", default="TODO-arxiv-or-report-url (REQUIRED to submit)")
    ap.add_argument("--authors", default="TODO-authors (>=1 must be academic/research-lab affiliated for Verified)")
    ap.add_argument("--oss", default="false")
    args = ap.parse_args()

    bundle = Path(args.bundle).resolve()
    results_dir = bundle / "results" / args.label
    preds_path = results_dir / "preds.json"
    if not preds_path.is_file():
        print(f"ERROR: no preds.json at {preds_path}", file=sys.stderr)
        return 1
    preds = json.loads(preds_path.read_text())

    dirname = f"{args.date}_{args.system}_{args.model}"
    out = Path(args.out).resolve() if args.out else (bundle.parent / "submissions" / dirname)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    (out / "trajs").mkdir(parents=True, exist_ok=True)
    (out / "results").mkdir(parents=True, exist_ok=True)

    # A. predictions — already canonical, copy verbatim
    shutil.copyfile(preds_path, out / "preds.json")

    resolved, no_generation, no_logs, no_traj = [], [], [], []
    for iid, pred in sorted(preds.items()):
        model = pred.get("model_name_or_path", args.system)
        patch = pred.get("model_patch", "") or ""

        # C. trajs — copy events.jsonl + render md (present whenever the instance ran)
        art = results_dir / "artifacts" / iid
        events = art / "events.jsonl"
        if events.is_file():
            shutil.copyfile(events, out / "trajs" / f"{iid}.jsonl")
            (out / "trajs" / f"{iid}.md").write_text(render_traj_md(events, iid))
        else:
            no_traj.append(iid)

        # B. logs — flatten the swebench eval dir → logs/<iid>/
        gdir = find_grade_dir(bundle, args.label, model, iid)
        has_report = False
        if gdir is not None:
            dst = out / "logs" / iid
            dst.mkdir(parents=True, exist_ok=True)
            for fn in LOG_FILES:
                src = gdir / fn
                if src.is_file():
                    shutil.copyfile(src, dst / fn)
            rep = dst / "report.json"
            if rep.is_file():
                has_report = True
                try:
                    r = json.loads(rep.read_text())
                    if r.get(iid, {}).get("resolved"):
                        resolved.append(iid)
                except json.JSONDecodeError:
                    pass

        # classify for results.json (mirrors analysis.get_results buckets)
        if not patch.strip():
            no_generation.append(iid)
        elif not has_report:
            no_logs.append(iid)

    # F. results.json — PREVIEW (get_results regenerates canonically)
    results = {"resolved": sorted(resolved),
               "no_generation": sorted(no_generation),
               "no_logs": sorted(no_logs)}
    (out / "results" / "results.json").write_text(json.dumps(results, indent=2))

    # D. metadata.yaml — scaffold with computed + placeholder fields
    display = args.name or f"{args.system} + {args.model}"
    s3 = f"s3://swe-bench-submissions/verified/{dirname}"
    (out / "metadata.yaml").write_text(
        "assets:\n"
        f"  logs:  {s3}/logs\n"
        f"  trajs: {s3}/trajs\n"
        f"name: {display!r}\n"
        f"oss: {args.oss}\n"
        f"site: {args.site}\n"
        "verified: false            # set by the SWE-bench team, not you\n"
        "info:\n"
        f"  authors: [{args.authors}]\n"
        f"  report: {args.report}   # REQUIRED — arXiv/tech report\n"
        "tags:\n"
        f"  model: [{args.model}]\n"
        f"  org: [{args.org}]\n"
        "  system:\n"
        "    attempts: 1            # pass@1 — bump to \"2+\" only if best@k\n"
    )

    # E. README.md — scaffold
    total = len(preds)
    (out / "README.md").write_text(
        f"# {display}\n\n"
        "## Description\n\n"
        "TODO: describe the system + link the technical report.\n\n"
        f"Resolved {len(resolved)}/{total} of this run "
        f"({100*len(resolved)/total:.1f}%).\n\n"
        "## get_results\n\n"
        "Paste the output of:\n\n```\n"
        f"python analysis/get_results.py evaluation/verified/{dirname}\n"
        "```\n\n"
        "## Authors\n\n"
        f"TODO: {args.authors}. First author site/LinkedIn: TODO.\n\n"
        "> Verified eligibility (policy 2025-11-18): >=1 author must be affiliated\n"
        "> with an academic institution / established research lab, and a report\n"
        "> is required. See ../SUBMISSION.md §2.\n"
    )

    # summary
    print(f"submission dir: {out}")
    print(f"  instances     : {total}")
    print(f"  resolved      : {len(resolved)}")
    print(f"  no_generation : {len(no_generation)}  {no_generation or ''}")
    print(f"  no_logs       : {len(no_logs)}  {no_logs or ''}")
    print(f"  trajs written : {total - len(no_traj)} jsonl+md" + (f"  (no events: {no_traj})" if no_traj else ""))
    print(f"  logs dirs     : {sum(1 for _ in (out / 'logs').iterdir())}")
    print("\nNEXT (not done here):")
    print(f"  1. edit metadata.yaml + README.md (authors, report, org, site)")
    print(f"  2. aws s3 cp --recursive {out}/logs  {s3}/logs")
    print(f"     aws s3 cp --recursive {out}/trajs {s3}/trajs")
    print(f"  3. cp -r {out} <experiments>/evaluation/verified/{dirname} && "
          f"python -m analysis.get_results evaluation/verified/{dirname}")
    print(f"  4. open the PR (grant @john-b-yang push access)")
    print("  ⚠ Verified also needs: academic-affiliated author + published report (SUBMISSION.md §2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
