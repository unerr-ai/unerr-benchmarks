#!/usr/bin/env python3
"""Resolve a task set to a SWE-bench instance-id list for the distributed runner.

The host launcher (run-distributed.sh) calls this to turn a high-level SUITE
selector — or an explicit id list / file — into the comma-separated INSTANCE_IDS
it seeds the coordinator queue with (`-e TASKS=<ids>`).

Precedence (highest first): explicit --tasks/$TASKS  >  --file/$TASKS_FILE  >
--suite/$SUITE  >  default (full Verified). CLI flags override the matching env.

Suites:
  full | verified  all SWE-bench_Verified test ids (from the HF dataset)  [default]
  mini             the fixed Mini-10 django ids from the fullresolve runbook
  lite             all SWE-bench_Lite test ids (from the HF dataset)

`datasets` (pip) is only needed for full/verified/lite. --suite mini and explicit
--tasks/--file resolve with the stdlib alone — the common smoke/mini path stays
dependency-free.

Usage:
  suite.py --suite mini            -> django__django-11790,django__django-11815,...
  suite.py --tasks "a,b,c"         -> a,b,c   (passthrough, deduped)
  suite.py --file ids.txt          -> reads newline/comma-separated ids
  suite.py                         -> full Verified (needs `datasets`)
"""

import argparse
import os
import sys

# The Mini-10 django ids (RUNBOOK §5) — hardcoded so the smoke/mini path needs no
# HF download. Keep in sync with e2e/econ/fly-remote/fullresolve/RUNBOOK.md.
MINI_IDS = [
    f"django__django-{n}"
    for n in (
        "11790", "11815", "11848", "11880", "11885",
        "11951", "11964", "11999", "12039", "12050",
    )
]

VERIFIED = "princeton-nlp/SWE-bench_Verified"
LITE = "princeton-nlp/SWE-bench_Lite"


def _hf_ids(dataset, split):
    """All instance_ids in an HF SWE-bench dataset split."""
    try:
        from datasets import load_dataset
    except Exception as e:  # ImportError or a broken install
        sys.stderr.write(
            f"suite.py: the `datasets` package is required for this suite "
            f"({dataset}): {e}\n"
            f"  pip install datasets   "
            f"(not needed for --suite mini or explicit --tasks/--file)\n"
        )
        sys.exit(2)
    ds = load_dataset(dataset, split=split)
    return [r["instance_id"] for r in ds]


def _split_ids(s):
    """Split a newline/comma-separated id blob into a clean list."""
    return [p.strip() for p in s.replace("\n", ",").split(",") if p.strip()]


def _read_file(path):
    with open(path) as f:
        return _split_ids(f.read())


def resolve(suite=None, tasks=None, tasks_file=None, dataset=None, split="test"):
    """Return the ordered, deduped instance-id list for the given selectors."""
    if tasks:
        ids = _split_ids(tasks)
    elif tasks_file:
        ids = _read_file(tasks_file)
    else:
        s = (suite or "full").lower()
        if s in ("full", "verified"):
            ids = _hf_ids(dataset or VERIFIED, split)
        elif s == "mini":
            ids = list(MINI_IDS)
        elif s == "lite":
            ids = _hf_ids(dataset or LITE, split)
        else:
            sys.stderr.write(
                f"suite.py: unknown suite '{suite}' (full|verified|mini|lite)\n"
            )
            sys.exit(2)

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
