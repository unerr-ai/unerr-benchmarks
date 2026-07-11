#!/usr/bin/env python3
"""Root-cause an econ instance from its execution trace (events.jsonl).

Surfaces what the agent actually DID: tool sequence, shell/test commands it ran,
files it edited, tiers/models it used, and cheap root-cause signals (did it edit
tests? did it run the tests? what did the final test run report?).

Usage:
  bench-trace.py --bundle out/econ-v2-bundle django__django-11885
  bench-trace.py --bundle out/econ-v2-bundle --all          # every instance, terse
  bench-trace.py --events path/to/events.jsonl              # explicit file

Reads the opencode event stream (type=tool_use, part.tool / part.state.input).
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys

EDIT_TOOLS = {"edit", "write", "multiedit", "patch"}
TEST_HINT = ("runtests", "pytest", "django test", " test ", "tox", "unittest")


def events_for(bundle: str, iid: str) -> str:
    p = os.path.join(bundle, "results", "*", "artifacts", iid, "events.jsonl")
    hits = sorted(glob.glob(p))
    if not hits:
        sys.exit(f"no events.jsonl for {iid} under {bundle}")
    return hits[-1]


def all_iids(bundle: str) -> list[str]:
    p = os.path.join(bundle, "results", "*", "artifacts", "*", "events.jsonl")
    return sorted({os.path.basename(os.path.dirname(x)) for x in glob.glob(p)})


def parse(events_path: str) -> dict:
    tools = collections.Counter()
    seq = []
    bash = []          # (cmd, output_tail)
    edits = []         # filePath
    models = collections.Counter()
    for l in open(events_path):
        try:
            e = json.loads(l)
        except json.JSONDecodeError:
            continue
        m = e.get("model") or e.get("modelID")
        if m:
            models[m] += 1
        if e.get("type") != "tool_use":
            continue
        p = e.get("part", {}) or {}
        tool = p.get("tool", "?")
        st = p.get("state", {}) or {}
        inp = st.get("input", {}) or {}
        tools[tool] += 1
        seq.append(tool)
        if tool == "bash":
            cmd = inp.get("command", "") or ""
            out = st.get("output", "") or ""
            bash.append((cmd, out))
        if tool in EDIT_TOOLS:
            edits.append(inp.get("filePath") or inp.get("path") or "?")
    # compress consecutive dup tools for a readable flow
    flow = []
    for t in seq:
        if flow and flow[-1][0] == t:
            flow[-1][1] += 1
        else:
            flow.append([t, 1])
    test_cmds = [(c, o) for c, o in bash if any(h in c for h in TEST_HINT)]
    edited_tests = [f for f in edits if "/tests/" in f or f.endswith("tests.py") or "/test_" in f]
    return {"tools": dict(tools), "flow": flow, "bash": bash, "test_cmds": test_cmds,
            "edits": edits, "edited_tests": edited_tests, "models": dict(models)}


def last_test_verdict(test_cmds) -> str:
    """Best-effort: what did the final test run report?"""
    if not test_cmds:
        return "NO TEST RUN"
    _, out = test_cmds[-1]
    tail = "\n".join(out.splitlines()[-8:])
    for marker in ("FAILED", "OK (skipped", "OK", "Ran "):
        if marker in tail:
            line = [x for x in tail.splitlines() if marker in x]
            return (line[-1] if line else marker).strip()[:80]
    return tail.splitlines()[-1][:80] if tail.strip() else "?"


def report(iid: str, t: dict, terse: bool) -> str:
    L = [f"############ {iid} ############"]
    L.append(f"tools: {t['tools']}")
    L.append(f"models: {t['models']}")
    # root-cause signals up top
    sig = []
    if t["edited_tests"]:
        sig.append(f"⚠ EDITED TEST FILES: {sorted(set(t['edited_tests']))}")
    sig.append(f"test runs: {len(t['test_cmds'])}  →  last verdict econ saw: {last_test_verdict(t['test_cmds'])}")
    tiers = t["models"]
    L.append("SIGNALS:")
    for s in sig:
        L.append(f"  {s}")
    if terse:
        return "\n".join(L)
    L.append("flow: " + " → ".join(f"{n}×{c}" if c > 1 else n for n, c in t["flow"]))
    L.append(f"\nSHELL/TEST commands ({len(t['bash'])}):")
    for c, _ in t["bash"]:
        L.append("  $ " + (c or "").strip().replace("\n", " ⏎ ")[:170])
    L.append(f"\nEDIT targets ({len(t['edits'])}):")
    for f in t["edits"]:
        flag = "  ⚠(test)" if (f in t["edited_tests"]) else ""
        L.append(f"  ✎ {f}{flag}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iid", nargs="?", help="instance id (e.g. django__django-11885)")
    ap.add_argument("--bundle", help="extracted bundle dir")
    ap.add_argument("--events", help="explicit events.jsonl (instead of --bundle+iid)")
    ap.add_argument("--all", action="store_true", help="terse root-cause line for every instance")
    args = ap.parse_args()

    if args.events:
        print(report(os.path.basename(os.path.dirname(args.events)), parse(args.events), terse=False))
        return 0
    if not args.bundle:
        return ap.error("pass --bundle DIR (with an iid or --all), or --events FILE")
    if args.all:
        for iid in all_iids(args.bundle):
            print(report(iid, parse(events_for(args.bundle, iid)), terse=True))
            print()
        return 0
    if not args.iid:
        return ap.error("pass an instance id, or --all")
    print(report(args.iid, parse(events_for(args.bundle, args.iid)), terse=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
