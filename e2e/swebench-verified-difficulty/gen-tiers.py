#!/usr/bin/env python3
"""Regenerate the SWE-bench Verified difficulty-tier ID lists.

SWE-bench Verified ships a per-instance human `difficulty` annotation (OpenAI had
93 devs estimate fix-time). This script pulls all 500 rows from the HF
datasets-server (no `datasets`/pip deps — just stdlib urllib) and buckets the
instance_ids into four tiers, writing:

  <tier>.ids.txt   one instance_id per line   (for `--ids @file` style tooling)
  <tier>.csv       comma-separated on one line (paste straight into IDS=)
  all.tsv          instance_id \t repo \t difficulty \t tier   (for arbitrary slicing)

Tiers (raw dataset value -> tier): "<15 min fix"->easy, "15 min - 1 hour"->medium,
"1-4 hours"->hard, ">4 hours"->veryhard.

Usage:  python3 gen-tiers.py           # writes into this script's directory
        python3 gen-tiers.py /some/out
"""
import json, sys, os, urllib.request, collections

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BASE = ("https://datasets-server.huggingface.co/rows"
        "?dataset=princeton-nlp/SWE-bench_Verified&config=default&split=test")
TIER = {"<15 min fix": "easy", "15 min - 1 hour": "medium",
        "1-4 hours": "hard", ">4 hours": "veryhard"}

rows = []
for off in range(0, 500, 100):
    with urllib.request.urlopen(f"{BASE}&offset={off}&length=100", timeout=60) as r:
        for rr in json.load(r)["rows"]:
            row = rr["row"]
            rows.append((row["instance_id"], row["repo"], row["difficulty"]))
assert len(rows) == 500, f"expected 500 rows, got {len(rows)}"

byt = collections.defaultdict(list)
for iid, repo, diff in rows:
    byt[TIER.get(diff, diff)].append(iid)

os.makedirs(OUT, exist_ok=True)
for tier in ("easy", "medium", "hard", "veryhard"):
    ids = sorted(byt[tier])
    open(os.path.join(OUT, f"{tier}.ids.txt"), "w").write("\n".join(ids) + "\n")
    open(os.path.join(OUT, f"{tier}.csv"), "w").write(",".join(ids) + "\n")
    print(f"{tier:9} {len(ids):3}")
with open(os.path.join(OUT, "all.tsv"), "w") as f:
    f.write("instance_id\trepo\tdifficulty\ttier\n")
    for iid, repo, diff in sorted(rows):
        f.write(f"{iid}\t{repo}\t{diff}\t{TIER.get(diff, diff)}\n")
print(f"wrote per-tier .ids.txt/.csv + all.tsv -> {OUT}")
