#!/usr/bin/env python3
"""make_submission.py — emit the SWE-bench Verified authorized-submission
format from a distributed run's bundle.

Reads results/<label>/preds.json (parsed via collect-failed.py's own
_load_preds — the run's existing SWE-format dict-or-list parser, reused here
by file-path import rather than re-implemented, since "collect-failed.py"
isn't a valid module name to `import`) and re-emits it as the leaderboard's
line-delimited JSON: one {instance_id, model_name_or_path, model_patch} row
per line, stamping every row with ONE submission-level model_name_or_path
(default "unerr-claude-openmodels", override with --model-name — every run's
preds.json already carries its own internal placeholder like "econ", which is
not a fit name for a leaderboard submission). Validates coverage (every
instance has a non-empty patch) and exits non-zero on any empty/missing
patch, so a broken submission never ships silently.

CLI:
    make_submission.py <bundle_or_results_dir> [--model-name <name>] [--out <dir>]

<bundle_or_results_dir> accepts either the bundle root (out/dist-<label>/bundle,
containing results/<label>/) or the results/<label> dir itself.

Writes <out>/all_preds.jsonl + a preds.json copy; <out> defaults to
<results_dir>/submission/.
"""
import argparse
import importlib.util
import json
import pathlib
import shutil
import sys

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_MODEL_NAME = "unerr-claude-openmodels"


def _import_tool(name):
    """Import a sibling tools/<name>.py (hyphenated — not `import`-able as a
    normal module) by file path, so its parsing helpers get reused instead
    of re-implemented here."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), _TOOLS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect_failed = _import_tool("collect-failed")


def resolve_bundle_label(path):
    """Accept either a bundle root (containing results/<label>/) or a
    results/<label> dir directly; return (bundle, label) matching
    collect_failed._load_preds's (bundle, label) contract."""
    p = pathlib.Path(path).resolve()
    if (p / "preds.json").is_file():
        return str(p.parent.parent), p.name
    if (p / "results").is_dir():
        return str(p), collect_failed._detect_label(str(p), None)
    sys.exit(
        f"make_submission: no preds.json found under {p} "
        f"(expected a bundle root or a results/<label> dir)"
    )


def build_rows(preds, model_name):
    """preds: {instance_id: model_patch} -> submission row dicts, in sorted
    instance_id order (stable, diffable output)."""
    return [
        {"instance_id": iid, "model_name_or_path": model_name, "model_patch": preds[iid] or ""}
        for iid in sorted(preds)
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle_or_results_dir", help="out/dist-<label>/bundle, or its results/<label> subdir")
    ap.add_argument(
        "--model-name", default=DEFAULT_MODEL_NAME,
        help=f"model_name_or_path stamped on every row (default: {DEFAULT_MODEL_NAME})",
    )
    ap.add_argument("--out", default=None, help="output dir (default: <results_dir>/submission/)")
    args = ap.parse_args(argv)

    bundle, label = resolve_bundle_label(args.bundle_or_results_dir)
    src_preds_path = pathlib.Path(bundle) / "results" / label / "preds.json"
    preds = collect_failed._load_preds(bundle, label)
    if not preds:
        sys.exit(f"make_submission: 0 predictions parsed from {src_preds_path}")

    rows = build_rows(preds, args.model_name)
    out_dir = pathlib.Path(args.out) if args.out else pathlib.Path(bundle) / "results" / label / "submission"
    out_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = out_dir / "all_preds.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    dst_preds_path = out_dir / "preds.json"
    if src_preds_path.resolve() != dst_preds_path.resolve() and src_preds_path.is_file():
        shutil.copy2(src_preds_path, dst_preds_path)

    n_total = len(rows)
    empty_ids = [r["instance_id"] for r in rows if not r["model_patch"]]
    n_with_patch = n_total - len(empty_ids)
    coverage = n_with_patch / n_total if n_total else 0.0

    print(f"make_submission: label={label}  {n_total} instance(s), "
          f"{n_with_patch} with a non-empty patch ({coverage * 100:.1f}% coverage)")
    print(f"Written: {jsonl_path}")
    print(f"Written: {dst_preds_path}")

    if empty_ids:
        print(f"EMPTY PATCH ({len(empty_ids)}): {', '.join(empty_ids)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
