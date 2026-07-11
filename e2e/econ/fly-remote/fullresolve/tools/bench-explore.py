#!/usr/bin/env python3
"""Mine an econ/opencode session DB (opencode.db) to characterize how the agent
EXPLORED and LOCALIZED — the part we want to optimize.

Unlike bench-trace.py (which reads the events.jsonl tail), this reads the full
SQLite session store: every tier is its own session (session.agent/model), and
each part carries the tool input AND output the model actually saw, plus its
reasoning/text. That lets us answer: what did it search for, did the search
return anything, how deep did it explore before committing to an edit, did it
thrash (re-edit / re-run), and what did its reasoning say at the turning points.

Usage:
  bench-explore.py --db out/econ-v3-dbs/django__django-11885/opencode.db
  bench-explore.py --db <db> --timeline          # full ordered narrative
  bench-explore.py --db <db> --json              # machine-readable signals
  bench-explore.py --bundle out/econ-v3-bundle --all   # every instance, signals only

Signals it computes per tier session:
  searches          : each query + mode + hit-count (⌀ = empty result)
  empty_search_rate : fraction of searches that returned nothing (localization miss)
  reads             : distinct files read, in order
  time_to_1st_edit  : # of tool calls of exploration before the first edit
  edit_thrash       : files edited more than once (churn / uncertain fix)
  edited_tests      : test files it modified (false-confidence risk)
  test_runs         : bash test invocations + the last verdict it saw
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys

EDIT_TOOLS = {"edit", "write", "multiedit", "patch"}
TEST_HINT = ("runtests", "pytest", "django test", " test ", "tox", "unittest", "test_sqlite")


def _is_test(fp: str) -> bool:
    fp = fp or ""
    return "/tests/" in fp or fp.endswith("tests.py") or "/test_" in fp or "test_" in os.path.basename(fp)


def load_parts(db: str):
    """Return ordered [(session_id, agent, model, part_dict)] across all tiers."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    sess = {}
    for r in con.execute("SELECT id, agent, model, parent_id, cost, tokens_output FROM session"):
        m = r["model"]
        try:
            m = json.loads(m).get("id", m) if m and m.startswith("{") else m
        except Exception:
            pass
        sess[r["id"]] = {"agent": r["agent"], "model": m, "sub": bool(r["parent_id"]),
                         "cost": r["cost"], "out": r["tokens_output"]}
    rows = []
    for r in con.execute("SELECT session_id, data, time_created FROM part ORDER BY time_created, id"):
        try:
            d = json.loads(r["data"])
        except Exception:
            continue
        rows.append((r["session_id"], d, r["time_created"]))
    con.close()
    return sess, rows


def _hitcount(tool: str, out) -> int | None:
    """Best-effort: how many results a search returned (None if not a search)."""
    if tool != "search":
        return None
    s = out if isinstance(out, str) else json.dumps(out)
    s = s.strip()
    if s in ("[]", "", "null"):
        return 0
    # search output is a list-ish or newline-joined hits; count lines that look like paths/hits
    if s.startswith("["):
        try:
            return len(json.loads(s))
        except Exception:
            pass
    return sum(1 for ln in s.splitlines() if ln.strip())


def analyze(db: str) -> dict:
    sess, rows = load_parts(db)
    tiers = {}  # sid -> signals
    order = []  # flat (sid, kind, payload) for timeline
    for sid, d, _t in rows:
        tinfo = sess.get(sid, {"agent": "?", "model": "?"})
        T = tiers.setdefault(sid, {
            "agent": tinfo["agent"], "model": tinfo["model"], "cost": tinfo.get("cost"),
            "searches": [], "reads": [], "edits": [], "edited_tests": [],
            "test_runs": [], "bash": [], "reasoning": [], "tool_seq": [],
            "first_edit_at": None,
        })
        typ = d.get("type")
        if typ == "reasoning":
            txt = (d.get("text") or "").strip()
            if txt:
                T["reasoning"].append(txt)
                order.append((sid, "reason", txt))
        elif typ == "text":
            txt = (d.get("text") or "").strip()
            if txt and len(txt) > 4:
                order.append((sid, "say", txt))
        elif typ == "tool":
            tool = d.get("tool", "?")
            st = d.get("state", {}) or {}
            inp = st.get("input", {}) or {}
            out = st.get("output", "")
            T["tool_seq"].append(tool)
            if tool == "search":
                hc = _hitcount(tool, out)
                q = inp.get("query") or inp.get("pattern") or ""
                T["searches"].append({"q": q, "mode": inp.get("mode", ""), "hits": hc})
                order.append((sid, "search", f"[{inp.get('mode','')}] {q!r} -> {hc} hits"))
            elif tool == "read":
                fp = inp.get("filePath") or inp.get("path") or "?"
                T["reads"].append(fp)
                order.append((sid, "read", fp))
            elif tool in EDIT_TOOLS:
                fp = inp.get("filePath") or inp.get("path") or "?"
                T["edits"].append(fp)
                if _is_test(fp):
                    T["edited_tests"].append(fp)
                if T["first_edit_at"] is None:
                    T["first_edit_at"] = len(T["tool_seq"]) - 1
                order.append((sid, "EDIT", fp + ("  ⚠test" if _is_test(fp) else "")))
            elif tool == "bash":
                cmd = inp.get("command", "") or ""
                T["bash"].append(cmd)
                if any(h in cmd for h in TEST_HINT):
                    verdict = _verdict(out)
                    T["test_runs"].append((cmd, verdict))
                    order.append((sid, "TEST", verdict + "   $ " + cmd.strip()[:100]))
                else:
                    order.append((sid, "bash", cmd.strip()[:110]))
    return {"sess": sess, "tiers": tiers, "order": order}


def _verdict(out) -> str:
    s = out if isinstance(out, str) else json.dumps(out)
    tail = "\n".join(s.splitlines()[-12:])
    for marker in ("FAILED", "OK (skipped", "OK", "Ran "):
        hits = [x for x in tail.splitlines() if marker in x]
        if hits:
            return hits[-1].strip()[:70]
    return (tail.splitlines()[-1][:70] if tail.strip() else "?")


def signals(T: dict) -> dict:
    searches = T["searches"]
    empty = sum(1 for s in searches if s["hits"] == 0)
    from collections import Counter
    ec = Counter(T["edits"])
    thrash = {f: n for f, n in ec.items() if n > 1}
    return {
        "tier": T["agent"], "model": T["model"], "cost": T.get("cost"),
        "n_search": len(searches), "empty_search": empty,
        "empty_rate": round(empty / len(searches), 2) if searches else None,
        "n_read": len(T["reads"]), "distinct_read": len(set(T["reads"])),
        "n_edit": len(T["edits"]), "time_to_1st_edit": T["first_edit_at"],
        "edit_thrash": thrash, "edited_tests": sorted(set(T["edited_tests"])),
        "n_test_run": len(T["test_runs"]),
        "last_verdict": T["test_runs"][-1][1] if T["test_runs"] else "NO TEST RUN",
    }


def print_report(db: str, timeline: bool):
    a = analyze(db)
    iid = os.path.basename(os.path.dirname(db))
    print(f"\n################ {iid} ################")
    for sid, T in a["tiers"].items():
        s = signals(T)
        print(f"\n── tier={s['tier']}  model={s['model']}  cost=${s['cost']:.4f}" if s['cost'] is not None
              else f"\n── tier={s['tier']}  model={s['model']}")
        print(f"   search: {s['n_search']}  (empty {s['empty_search']} = {s['empty_rate']})   "
              f"read: {s['n_read']} ({s['distinct_read']} distinct)   edit: {s['n_edit']}   test-runs: {s['n_test_run']}")
        print(f"   time-to-1st-edit: {s['time_to_1st_edit']} tool-calls of exploration before committing")
        if s["edit_thrash"]:
            print(f"   ⚠ edit thrash (re-edited): {s['edit_thrash']}")
        if s["edited_tests"]:
            print(f"   ⚠ EDITED TESTS: {s['edited_tests']}")
        print(f"   last test verdict econ saw: {s['last_verdict']}")
        # show the searches — the localization attempts
        if T["searches"]:
            print("   SEARCHES (localization attempts):")
            for q in T["searches"]:
                flag = "  ⌀EMPTY" if q["hits"] == 0 else f"  {q['hits']} hits"
                print(f"      [{q['mode']}] {q['q'][:80]!r}{flag}")
    if timeline:
        print("\n   ───── TIMELINE ─────")
        for sid, kind, payload in a["order"]:
            tag = {"reason": "🤔", "say": "💬", "search": "🔎", "read": "📖",
                   "EDIT": "✎ ", "TEST": "🧪", "bash": "$ "}.get(kind, kind)
            txt = payload.replace("\n", " ⏎ ")
            if kind in ("reason", "say"):
                txt = txt[:300]
            print(f"   {tag} {txt}")
    return a


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="path to an opencode.db")
    ap.add_argument("--bundle", help="a bundle dir (with results/*/artifacts/*/opencode.db)")
    ap.add_argument("--all", action="store_true", help="every instance in --bundle (signals only)")
    ap.add_argument("--timeline", action="store_true", help="full ordered exploration narrative")
    ap.add_argument("--json", action="store_true", help="machine-readable signals")
    args = ap.parse_args()

    if args.db:
        if args.json:
            a = analyze(args.db)
            print(json.dumps({os.path.basename(os.path.dirname(args.db)):
                              [signals(T) for T in a["tiers"].values()]}, indent=2))
        else:
            print_report(args.db, args.timeline)
        return 0

    if args.bundle:
        dbs = sorted(glob.glob(os.path.join(args.bundle, "results", "*", "artifacts", "*", "opencode.db")))
        if not dbs:
            sys.exit(f"no opencode.db under {args.bundle}")
        out = {}
        for db in dbs:
            if args.json or args.all:
                a = analyze(db)
                out[os.path.basename(os.path.dirname(db))] = [signals(T) for T in a["tiers"].values()]
            else:
                print_report(db, args.timeline)
        if args.json or args.all:
            print(json.dumps(out, indent=2))
        return 0

    return ap.error("pass --db FILE or --bundle DIR")


if __name__ == "__main__":
    raise SystemExit(main())
