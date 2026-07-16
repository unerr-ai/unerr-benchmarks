#!/usr/bin/env python3
"""debug_instance.py — pull one SWE-bench instance's full debug artifact set
out of a distributed run's bundle into a clean per-instance folder, plus a
one-screen human-readable summary.

Complements collect-failed.py: that tool batch-archives every FAILED
instance's engine.log/err.txt/events.jsonl/opencode.db already (see its
`for fn in (...)` copy loop), but only writes a DEAD_REASON.txt (no
artifacts) for dead-lettered ones, and it never touches meta.jsonl (so no
cost/turns) or prints a human-readable summary — it's a whole-run batch
archiver. This is the single-instance, any-status (resolved / failed / dead)
targeted counterpart, for interactive "what happened to instance X" debugging
before/without a full collect-failed.py pass.

Reuses collect-failed.py's _load_preds / _load_dead / _detect_label and, for
CLAUDE bundles, cost_report.py's instance_row (both by file-path import —
"collect-failed.py" and hyphenated filenames generally aren't `import`-able
as normal modules). ECON bundles carry no top-level cost record — see
_row_from_meta() below for the small arm-detecting shim that handles both
shapes without re-implementing either arm's cost/turns/preds parsing.

CLI:
    debug_instance.py <bundle_dir> <instance_id> [--out <dir>]

Gathers into <out> (default: <bundle_dir>/debug/<instance_id>/):
  - artifacts/*        engine.log, err.txt, events.jsonl, opencode.db (best-effort)
  - report.json        logs/grade-merged/<iid>/report.json, copied verbatim
  - meta.json           this instance's meta.jsonl line, pretty-printed
  - model_patch.diff   this instance's preds.json patch
  - dead.json           this instance's dead.jsonl entry, if dead-lettered

Then prints a one-screen summary: resolved?, turns, cost, patch bytes,
FAIL_TO_PASS/PASS_TO_PASS counts, and the tail of engine.log.
"""
import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import sys

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent
_E2E_DIR = _TOOLS_DIR.parents[1]  # e2e/


def _import_by_path(name, path):
    """Import a sibling/cousin tool script by file path (reused for
    hyphenated filenames that aren't `import`-able as normal modules), so its
    parsing helpers get reused instead of re-implemented here."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect_failed = _import_by_path("collect_failed_tool", _TOOLS_DIR / "collect-failed.py")
try:
    cost_report = _import_by_path(
        "cost_report_tool", _E2E_DIR / "reference" / "claude" / "local-docker" / "cost_report.py"
    )
except Exception:
    # An econ-only checkout (or any other missing-module case) must not
    # crash debug_instance — _row_from_meta() falls back to the econ
    # telemetry shape when this is None.
    cost_report = None


def _load_meta_line(bundle, label, iid):
    """This instance's raw meta.jsonl record, or None if absent."""
    p = os.path.join(bundle, "results", label, "meta.jsonl")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("instance_id") == iid:
                return rec
    return None


def _load_report_entry(bundle, iid):
    """(raw report.json, this instance's entry) from logs/grade-merged/<iid>/report.json,
    tolerating both the per-instance shape {"<iid>": {...}} and a bare {...} entry
    (same tolerance as merge-reports.py's ids_from_report / collect-failed.py)."""
    p = os.path.join(bundle, "logs", "grade-merged", iid, "report.json")
    if not os.path.exists(p):
        return None, None
    with open(p, encoding="utf-8") as f:
        rj = json.load(f)
    return rj, rj.get(iid, rj)


def gather(bundle, iid, out_dir, label=None):
    """Copy every artifact this bundle has for `iid` into `out_dir`. Returns a
    dict of what was found (feeds summarize()); never raises on a missing
    piece — each source is independently best-effort."""
    label = label or collect_failed._detect_label(bundle, None)
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = []

    art_src = pathlib.Path(bundle) / "results" / label / "artifacts" / iid
    art_dst = out_dir / "artifacts"
    have_artifacts = False
    if art_src.is_dir():
        for fn in ("engine.log", "err.txt", "events.jsonl", "opencode.db"):
            s = art_src / fn
            if s.exists():
                art_dst.mkdir(exist_ok=True)
                shutil.copy2(s, art_dst / fn)
                copied.append(f"artifacts/{fn}")
                have_artifacts = True

    raw_report, report_entry = _load_report_entry(bundle, iid)
    if raw_report is not None:
        (out_dir / "report.json").write_text(json.dumps(raw_report, indent=2), encoding="utf-8")
        copied.append("report.json")

    meta_rec = _load_meta_line(bundle, label, iid)
    if meta_rec is not None:
        (out_dir / "meta.json").write_text(json.dumps(meta_rec, indent=2), encoding="utf-8")
        copied.append("meta.json")

    preds = collect_failed._load_preds(bundle, label)
    patch = preds.get(iid)
    if patch is not None:
        (out_dir / "model_patch.diff").write_text(patch or "(empty patch)\n", encoding="utf-8")
        copied.append("model_patch.diff")

    dead = collect_failed._load_dead(bundle, label)
    dead_entry = dead.get(iid)
    if dead_entry is not None:
        (out_dir / "dead.json").write_text(json.dumps(dead_entry, indent=2), encoding="utf-8")
        copied.append("dead.json")

    return dict(
        label=label, out_dir=out_dir, copied=copied,
        meta_rec=meta_rec, report_entry=report_entry, patch=patch,
        dead_entry=dead_entry, art_dst=art_dst if have_artifacts else None,
    )


def _row_from_meta(rec):
    """Arm-detecting cost/turns summary for one meta.jsonl record. CLAUDE
    records carry rec["cost"] and route through cost_report.instance_row
    unchanged; ECON records have no top-level "cost" — instead they carry
    telemetry{turns,usd,in_tokens,cached_in,out_tokens,by_tier} plus an
    optional tier_cost_db{by_tier,error}. For the per-tier breakdown prefer
    tier_cost_db.by_tier when present, non-empty and error-free, else fall
    back to telemetry.by_tier (mirrors e2e/econ/report.py::_effective_by_tier);
    the authoritative total is always telemetry.usd. Returns None if rec is
    falsy."""
    if not rec:
        return None
    if cost_report is not None and rec.get("cost") is not None:
        return cost_report.instance_row(rec, None)

    tel = rec.get("telemetry")
    tel = tel if isinstance(tel, dict) else {}
    tcd = rec.get("tier_cost_db")
    tcd = tcd if isinstance(tcd, dict) else {}
    db_by_tier = tcd.get("by_tier")
    by_tier = (
        db_by_tier
        if isinstance(db_by_tier, dict) and db_by_tier and not tcd.get("error")
        else (tel.get("by_tier") or {})
    )

    return dict(
        instance_id = rec.get("instance_id") or "?",
        resolved    = None,
        turns       = tel.get("turns") or 0,
        usd         = tel.get("usd") or 0.0,
        in_tokens   = tel.get("in_tokens") or 0,
        cached_in   = tel.get("cached_in") or 0,
        out_tokens  = tel.get("out_tokens") or 0,
        by_tier     = by_tier,
        wall_s      = rec.get("wall_s") or 0.0,
        rc          = rec.get("rc") or 0,
        patch_bytes = rec.get("patch_bytes") or 0,
    )


def summarize(iid, g):
    """Build the one-screen human-readable summary string."""
    lines = [f"=== {iid}  (label={g['label']}) ==="]

    entry = g["report_entry"] or {}
    resolved = entry.get("resolved")
    lines.append(f"resolved: {resolved if resolved is not None else 'n/a (no report.json)'}")

    row = _row_from_meta(g["meta_rec"])
    if row:
        lines.append(
            f"turns: {row['turns']}   cost: ${row['usd']:.4f}   "
            f"wall_s: {row['wall_s']:.1f}   rc: {row['rc']}"
        )
    else:
        lines.append("turns/cost: n/a (no meta.jsonl record found)")

    patch = g["patch"]
    patch_bytes = len(patch.encode("utf-8")) if patch else 0
    lines.append(f"patch: {patch_bytes} bytes" + ("" if patch else "  (EMPTY or missing)"))

    if entry:
        ts = entry.get("tests_status", {})
        f2p, p2p = ts.get("FAIL_TO_PASS", {}), ts.get("PASS_TO_PASS", {})
        lines.append(
            f"FAIL_TO_PASS: {len(f2p.get('success', []))} passed, "
            f"{len(f2p.get('failure', []))} failing"
        )
        lines.append(
            f"PASS_TO_PASS: {len(p2p.get('success', []))} passed, "
            f"{len(p2p.get('failure', []))} regressed"
        )

    if g["dead_entry"]:
        lines.append(
            f"DEAD: attempt_count={g['dead_entry'].get('attempt_count')}  "
            f"reason={g['dead_entry'].get('failure_reason')!r}"
        )

    if g["art_dst"] is not None and (g["art_dst"] / "engine.log").exists():
        tail = (g["art_dst"] / "engine.log").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[-15:]
        lines.append("--- engine.log (tail) ---")
        lines.extend(tail)
    else:
        lines.append("(no artifacts/engine.log found for this instance in the bundle)")

    lines.append(f"gathered -> {g['out_dir']}  ({', '.join(g['copied']) or 'nothing found'})")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle_dir", help="out/dist-<label>/bundle")
    ap.add_argument("instance_id")
    ap.add_argument("--out", default=None, help="output dir (default: <bundle_dir>/debug/<instance_id>)")
    args = ap.parse_args(argv)

    out_dir = args.out or (pathlib.Path(args.bundle_dir) / "debug" / args.instance_id)
    g = gather(args.bundle_dir, args.instance_id, out_dir)
    if not g["copied"]:
        sys.exit(f"debug_instance: found nothing for {args.instance_id} under {args.bundle_dir}")
    print(summarize(args.instance_id, g))
    return 0


if __name__ == "__main__":
    sys.exit(main())
