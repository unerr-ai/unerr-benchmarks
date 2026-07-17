#!/usr/bin/env python3
"""Resolve a task set to a benchmark instance-id list for the distributed runner.

The host launcher (run-distributed.sh) calls this to turn a high-level SUITE
selector — or an explicit id list / file — into the comma-separated INSTANCE_IDS
it seeds the coordinator queue with (`-e TASKS=<ids>`).

Precedence (highest first): explicit --tasks/$TASKS  >  --file/$TASKS_FILE  >
--suite/$SUITE  >  default (full Verified). CLI flags override the matching env.

The suite branch delegates to `benchmarks.resolve_ids` — the ONE benchmark-aware
id source (SWE-bench Verified/Lite, SWE-bench Pro, Terminal-Bench). This file owns
only the precedence + dedupe glue so run commands and the CLI stay unchanged.

Suites (from benchmarks.py):
  full | verified   all SWE-bench_Verified test ids (HF dataset)          [default]
  mini              the fixed Mini-10 django ids (legacy Verified alias)
  lite              all SWE-bench_Lite test ids (HF dataset)
  pro | pro-mini    SWE-bench Pro (vendored snapshot) / its 5-id smoke set
  terminal | terminal-mini   Terminal-Bench (vendored task set) / 5-id smoke
  <bench>-mini      the 5-id smoke set for any benchmark (1 coord + 2 workers)

`datasets` (pip) is only needed for the full HF suites. Every *-mini suite and
explicit --tasks/--file resolve with the stdlib alone — the smoke path stays
dependency-free.

Usage:
  suite.py --suite mini            -> django__django-11790,django__django-11815,...
  suite.py --suite pro-mini        -> first 5 SWE-bench Pro ids
  suite.py --tasks "a,b,c"         -> a,b,c   (passthrough, deduped)
  suite.py --file ids.txt          -> reads newline/comma-separated ids
  suite.py                         -> full Verified (needs `datasets`)
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import benchmarks  # noqa: E402  — sibling module, the benchmark-aware id source

# Kept for back-compat with anything importing suite.MINI_IDS directly; the
# canonical list now lives in benchmarks._VERIFIED_MINI10.
MINI_IDS = list(benchmarks._VERIFIED_MINI10)


def _split_ids(s):
    """Split a newline/comma-separated id blob into a clean list."""
    return [p.strip() for p in s.replace("\n", ",").split(",") if p.strip()]


def _read_file(path):
    with open(path) as f:
        return _split_ids(f.read())


def resolve(suite=None, tasks=None, tasks_file=None, dataset=None, split="test"):
    """Return the ordered, deduped instance-id list for the given selectors.

    Explicit --tasks/--file win; otherwise the suite selector is resolved by the
    benchmark-aware `benchmarks.resolve_ids` (verified/lite/pro/terminal + minis)."""
    if tasks:
        ids = _split_ids(tasks)
    elif tasks_file:
        ids = _read_file(tasks_file)
    else:
        ids = benchmarks.resolve_ids(suite, dataset, split)

    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Resolve SUITE/TASKS/TASKS_FILE -> comma-separated instance ids"
    )
    ap.add_argument("--suite", default=os.environ.get("SUITE"))
    ap.add_argument("--tasks", default=os.environ.get("TASKS"))
    ap.add_argument("--file", dest="tasks_file", default=os.environ.get("TASKS_FILE"))
    ap.add_argument("--dataset", default=os.environ.get("DATASET"))
    ap.add_argument("--split", default=os.environ.get("SPLIT", "test"))
    a = ap.parse_args(argv)

    ids = resolve(a.suite, a.tasks, a.tasks_file, a.dataset, a.split)
    if not ids:
        sys.stderr.write("suite.py: resolved 0 instance ids\n")
        sys.exit(1)
    sys.stdout.write(",".join(ids) + "\n")


if __name__ == "__main__":
    main()
