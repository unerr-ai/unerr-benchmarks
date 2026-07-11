#!/usr/bin/env python3
"""Audit an econ full-resolve run: resolution + cost + tokens + per-tier + failures.

Reads a result bundle (the tar the fly job produces, extracted) OR a bare
meta.jsonl + grade report. Stdlib only — runs anywhere.

Usage:
  bench-audit.py --bundle out/econ-v2-bundle                 # auto-find meta + grade
  bench-audit.py --bundle out/econ-v2-bundle --label econ-v2 # pin the label
  bench-audit.py --meta results/econ/meta.jsonl [--grade reports/econ.econ.json]
  bench-audit.py --bundle out/econ-v2-bundle --json          # machine-readable

A "bundle dir" is expected to contain results/<label>/meta.jsonl and (optionally)
reports/*.<label>.json — exactly what entrypoint.sh tars up. If grading hasn't run
yet you still get cost/token numbers, just no resolved/unresolved column.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys


def find_meta(bundle: str, label: str | None) -> tuple[str, str | None]:
    """Return (meta_path, label). Auto-detects the single label under results/."""
    res = os.path.join(bundle, "results")
    if label:
        m = os.path.join(res, label, "meta.jsonl")
        return m, label
    cands = sorted(glob.glob(os.path.join(res, "*", "meta.jsonl")))
    if not cands:
        sys.exit(f"no results/*/meta.jsonl under {bundle}")
    m = cands[-1]
    return m, os.path.basename(os.path.dirname(m))


def find_grade(bundle: str, label: str) -> str | None:
    for pat in (f"reports/*.{label}.json", f"logs/grade-{label}/**/report.json"):
        hits = sorted(glob.glob(os.path.join(bundle, pat), recursive=True))
        if hits:
            return hits[-1]
    return None


def load_rows(meta_path: str) -> list[dict]:
    with open(meta_path) as f:
        return [json.loads(l) for l in f if l.strip()]


def load_grade(grade_path: str | None) -> dict:
    if not grade_path or not os.path.isfile(grade_path):
        return {}
    d = json.load(open(grade_path))
    return {
        "resolved": set(d.get("resolved_ids", [])),
        "unresolved": set(d.get("unresolved_ids", [])),
        "errored": set(d.get("error_ids", [])),
        "empty": set(d.get("empty_patch_ids", [])),
        "n_resolved": d.get("resolved_instances"),
        "n_submitted": d.get("submitted_instances") or d.get("total_instances"),
    }


def tc(row: dict) -> dict:
    return row.get("tier_cost_db") or {}


def audit(rows: list[dict], grade: dict) -> dict:
    tot = {"usd": 0.0, "in": 0, "cache": 0, "out": 0, "reason": 0, "msgs": 0, "wall": 0.0}
    tiers: dict[str, dict] = {}
    per = []
    resolved = grade.get("resolved", set())
    graded = bool(grade)
    for m in rows:
        c = tc(m)
        iid = m.get("instance_id", "?")
        usd = c.get("usd", 0) or 0
        row = {
            "instance": iid,
            "resolved": (iid in resolved) if graded else None,
            "wall": m.get("wall_s", 0) or 0,
            "rc": m.get("rc"),
            "patch_bytes": m.get("patch_bytes", 0) or 0,
            "msgs": c.get("messages", 0) or 0,
            "in": c.get("in_tokens", 0) or 0,
            "cache": c.get("cached_in", 0) or 0,
            "out": c.get("out_tokens", 0) or 0,
            "reason": c.get("reasoning_tokens", 0) or 0,
            "usd": usd,
            "tiers": {t: v.get("usd", 0) for t, v in (c.get("by_tier") or {}).items()},
        }
        per.append(row)
        tot["usd"] += usd
        tot["in"] += row["in"]; tot["cache"] += row["cache"]; tot["out"] += row["out"]
        tot["reason"] += row["reason"]; tot["msgs"] += row["msgs"]; tot["wall"] += row["wall"]
        for t, v in (c.get("by_tier") or {}).items():
            d = tiers.setdefault(t, {"usd": 0.0, "in": 0, "cache": 0, "out": 0, "msgs": 0, "inst": 0})
            d["usd"] += v.get("usd", 0) or 0
            d["in"] += v.get("in_tokens", 0) or 0
            d["cache"] += v.get("cached_in", 0) or 0
            d["out"] += v.get("out_tokens", 0) or 0
            d["msgs"] += v.get("messages", 0) or 0
            d["inst"] += 1
    n = len(rows)
    n_res = len(resolved & {r["instance"] for r in per}) if graded else None
    return {"n": n, "n_res": n_res, "graded": graded, "total": tot,
            "tiers": tiers, "per": per,
            "unresolved": sorted(grade.get("unresolved", set())) if graded else [],
            "errored": sorted(grade.get("errored", set())) if graded else []}


def fmt(a: dict, label: str) -> str:
    L = []
    n, nr = a["n"], a["n_res"]
    t = a["total"]
    L.append(f"========== econ run '{label}' — AUDIT ==========")
    if a["graded"]:
        L.append(f"resolved            : {nr}/{n}  ({100*nr/n:.0f}%)" if n else "resolved: 0/0")
    else:
        L.append(f"resolved            : (not graded yet — {n} instances resolved)")
    L.append(f"total cost          : ${t['usd']:.4f}")
    if n:
        L.append(f"  mean $/instance   : ${t['usd']/n:.4f}")
        if a["graded"] and nr:
            L.append(f"  $/resolved        : ${t['usd']/nr:.4f}")
    L.append(f"total turns/msgs    : {t['msgs']}   (mean {t['msgs']/n:.1f})" if n else "")
    L.append(f"total wall          : {t['wall']:.0f}s ({t['wall']/60:.1f} min)   mean {t['wall']/n:.0f}s/inst" if n else "")
    allin = t["in"] + t["cache"]
    L.append("")
    L.append("---------- TOKENS ----------")
    L.append(f"input (uncached)    : {t['in']:,}")
    L.append(f"cached input        : {t['cache']:,}   ({100*t['cache']/allin:.1f}% cache hit)" if allin else "cached input: 0")
    L.append(f"output              : {t['out']:,}   (reasoning {t['reason']:,})")
    L.append(f"TOTAL tokens        : {t['in']+t['cache']+t['out']:,}")
    L.append("")
    L.append("---------- PER-INSTANCE ----------")
    head = f"{'instance':<26}{'res':>4}{'wall':>7}{'msgs':>6}{'cache_in':>10}{'out':>8}{'usd':>9}  tiers"
    L.append(head); L.append("-" * len(head))
    for r in a["per"]:
        res = "✓" if r["resolved"] else ("✗" if r["resolved"] is False else "·")
        ts = " ".join(f"{t}=${u:.4f}" for t, u in r["tiers"].items())
        L.append(f"{r['instance']:<26}{res:>4}{r['wall']:>7.0f}{r['msgs']:>6}{r['cache']:>10,}{r['out']:>8,}{r['usd']:>9.4f}  {ts}")
    L.append("")
    L.append("---------- BY TIER ----------")
    for t_, d in sorted(a["tiers"].items(), key=lambda kv: -kv[1]["usd"]):
        pct = 100 * d["usd"] / a["total"]["usd"] if a["total"]["usd"] else 0
        L.append(f"  {t_:<11} ${d['usd']:.4f} ({pct:4.1f}%)  in={d['in']:,} cache={d['cache']:,} out={d['out']:,} msgs={d['msgs']} on {d['inst']} inst")
    for t_ in ("conductor", "explorer", "executor", "reasoner", "oracle"):
        if t_ not in a["tiers"]:
            L.append(f"  {t_:<11} $0.0000 ( 0.0%)  — never invoked")
    if a["graded"] and (a["unresolved"] or a["errored"]):
        L.append("")
        L.append("---------- FAILURES ----------")
        if a["unresolved"]:
            L.append(f"  unresolved: {', '.join(a['unresolved'])}")
        if a["errored"]:
            L.append(f"  errored   : {', '.join(a['errored'])}")
    return "\n".join(x for x in L if x is not None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", help="extracted bundle dir (results/ + reports/ + logs/)")
    ap.add_argument("--label", help="run label (auto-detected from the bundle if omitted)")
    ap.add_argument("--meta", help="explicit meta.jsonl (instead of --bundle)")
    ap.add_argument("--grade", help="explicit grade report json")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    if args.meta:
        meta_path, label = args.meta, (args.label or "econ")
        grade_path = args.grade
    elif args.bundle:
        meta_path, label = find_meta(args.bundle, args.label)
        grade_path = args.grade or find_grade(args.bundle, label)
    else:
        return ap.error("pass --bundle DIR or --meta FILE")

    rows = load_rows(meta_path)
    grade = load_grade(grade_path)
    a = audit(rows, grade)
    if args.json:
        print(json.dumps({"label": label, **a,
                          "tiers": a["tiers"], "per": a["per"]}, default=list, indent=2))
    else:
        print(fmt(a, label))
        if grade_path:
            print(f"\n(grade: {grade_path})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
