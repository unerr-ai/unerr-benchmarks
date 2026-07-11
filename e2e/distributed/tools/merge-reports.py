#!/usr/bin/env python3
"""
merge-reports.py — Slice D: combine per-worker swebench per-instance
report.json files into one grade summary for the distributed runner's
coordinator.

Each worker in the fleet grades exactly ONE claimed instance
(`--instance_ids <one>`, PLAN.md §1 decision 4) and POSTs its report.json
back to the coordinator, which the coordinator-entrypoint.sh dumps under
--reports-dir as <grade_dir>/<instance_id>/report.json before calling this
script. Two evidence sources are merged:

  1. --reports-dir: per-instance swebench reports (globbed recursively,
     *.json). The worker posts the harness's per-instance report VERBATIM —
     {"<iid>": {"resolved": bool, "patch_*": ..., "tests_status": {...}}} —
     NOT the aggregate {resolved_ids: [...]} summary. ids_from_report() below
     normalizes that per-instance shape (and still tolerates the aggregate
     shape) into resolved/unresolved id-sets.
  2. --db: the coordinator's queue.db (schema.sql). Rows with status='dead'
     (attempt_count exhausted at 2, PLAN.md §1 decision 5) never produced a
     report.json — they are folded into error_ids/dead_ids here so a fleet
     failure surfaces in the report instead of silently vanishing from the
     totals.

Output shape matches what swebench.harness.run_evaluation writes at the top
level (resolved_ids/unresolved_ids/... + counts), so it drops straight into
report.py's --grade-report unchanged: report.py's load_resolved_ids() reads
only the top-level "resolved_ids" list from that file (see
e2e/econ/report.py lines 94-105, 548).

CLI:
    merge-reports.py --reports-dir logs/grade-merged --db /data/queue.db \
        --run-id <run_id> --out reports/merged.<label>.json
"""

import argparse
import json
import pathlib
import sqlite3
import sys


def load_instance_reports(reports_dir):
    """Glob every *.json under reports_dir (any depth — flat <iid>.json or
    nested <iid>/report.json layouts both work) and parse it as a swebench
    per-instance report. Malformed files are skipped with a stderr warning,
    matching report.py's load_meta() tolerance for bad records."""
    reports = []
    root = pathlib.Path(reports_dir)
    if not root.is_dir():
        return reports
    for path in sorted(root.rglob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                reports.append(json.load(fh))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: skipping {path}: {exc}", file=sys.stderr)
    return reports


def load_dead_ids(db_path, run_id=None):
    """Query the queue DB for dead-lettered rows (attempt_count exhausted,
    see schema.sql / PLAN.md §1 decision 5) — these never produced a
    report.json and would otherwise vanish from the merged totals."""
    if not db_path or not pathlib.Path(db_path).is_file():
        return []
    con = sqlite3.connect(db_path)
    try:
        query = "SELECT instance_id FROM tasks WHERE status='dead'"
        params = ()
        if run_id:
            query += " AND run_id=?"
            params = (run_id,)
        return [row[0] for row in con.execute(query, params).fetchall()]
    except sqlite3.Error as exc:
        # A dead-id lookup failure (missing/locked/empty DB — e.g. a WAL-mode
        # copy bundled without its -wal) must NOT sink the whole grade summary;
        # warn and fall back to no dead ids so the report.json ids still merge.
        print(f"Warning: dead-id query failed on {db_path}: {exc}", file=sys.stderr)
        return []
    finally:
        con.close()


def ids_from_report(rep):
    """Extract (submitted, resolved, unresolved, error) id-sets from ONE
    report, tolerating BOTH shapes the pipeline can hand us:

      * aggregate swebench summary — {"resolved_ids": [...], "submitted_ids":
        [...], ...} (the top-level shape run_evaluation writes and report.py's
        --grade-report reader consumes); and
      * per-instance harness report — {"<iid>": {"resolved": bool, "patch_*":
        ..., "tests_status": {...}}}. This is what the distributed worker
        actually posts: worker-loop.py _grade reads the harness's
        logs/run_evaluation/.../<iid>/report.json VERBATIM, and the coordinator
        dumps it to grade-merged/<iid>/report.json. So it is the common case —
        reading only the *_ids keys (absent in this shape) is what produced the
        0/0 merge despite correct per-instance grades.

    A graded per-instance report is submitted+completed: resolved=True → the
    resolved set, resolved=False → unresolved (patch applied but tests failed,
    or patch missing — either way it was submitted and NOT resolved). Dead
    (never-graded) ids are folded in separately by merge() from the queue DB."""
    submitted, resolved, unresolved, error = set(), set(), set(), set()
    if not isinstance(rep, dict):
        return submitted, resolved, unresolved, error
    # Aggregate shape: explicit *_ids lists take precedence when present.
    if any(k in rep for k in ("submitted_ids", "resolved_ids", "unresolved_ids", "error_ids")):
        submitted.update(rep.get("submitted_ids") or [])
        resolved.update(rep.get("resolved_ids") or [])
        unresolved.update(rep.get("unresolved_ids") or [])
        error.update(rep.get("error_ids") or [])
        return submitted, resolved, unresolved, error
    # Per-instance shape: {"<iid>": {"resolved": bool, ...}}.
    for iid, inner in rep.items():
        if not isinstance(inner, dict) or "resolved" not in inner:
            continue
        submitted.add(iid)
        (resolved if bool(inner.get("resolved")) else unresolved).add(iid)
    return submitted, resolved, unresolved, error


def merge(reports, dead_ids):
    """Union each per-instance report's id sets into one combined grade
    summary, then fold in dead-lettered ids as errors. A dead id is removed
    from resolved/unresolved first so it can't double-count against a stale
    report for the same instance."""
    resolved, unresolved, error, submitted = set(), set(), set(), set()
    for rep in reports:
        s, r, u, e = ids_from_report(rep)
        submitted |= s
        resolved |= r
        unresolved |= u
        error |= e

    dead = set(dead_ids)
    resolved -= dead
    unresolved -= dead
    error |= dead
    submitted |= dead

    return {
        "total_instances": len(submitted),
        "submitted_instances": len(submitted),
        "completed_instances": len(resolved) + len(unresolved),
        "resolved_instances": len(resolved),
        "unresolved_instances": len(unresolved),
        "error_instances": len(error),
        "submitted_ids": sorted(submitted),
        "resolved_ids": sorted(resolved),
        "unresolved_ids": sorted(unresolved),
        "error_ids": sorted(error),
        "dead_ids": sorted(dead),
        "schema_version": "distributed-merge-1",
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--reports-dir", required=True, type=pathlib.Path,
        help="dir of per-instance swebench report.json files (searched recursively)",
    )
    ap.add_argument(
        "--db", type=pathlib.Path, default=None,
        help="coordinator queue.db, for dead-lettered (status='dead') ids",
    )
    ap.add_argument(
        "--run-id", default=None,
        help="restrict the --db dead-id lookup to this run_id",
    )
    ap.add_argument(
        "--out", required=True, type=pathlib.Path,
        help="path to write the merged grade summary JSON",
    )
    args = ap.parse_args(argv)

    reports = load_instance_reports(args.reports_dir)
    if not reports:
        print(f"Warning: no report.json files found under {args.reports_dir}", file=sys.stderr)

    dead_ids = load_dead_ids(args.db, args.run_id)
    merged = merge(reports, dead_ids)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(
        f"Written: {args.out}  (resolved {merged['resolved_instances']}/{merged['submitted_instances']})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
