#!/usr/bin/env python3
"""
report.py — Single-arm econ agent benchmark aggregation: performance + cost report.

CLI:
    python3 report.py --meta results/meta.jsonl \
        [--grade-report grade_report.json] [--label econ] [--out DIR]

Line format of --meta (JSONL, one record per instance):
    {"instance_id", "label", "wall_s", "rc", "patch_bytes",
     "telemetry": {"turns", "in_tokens", "cached_in", "out_tokens",
                   "reasoning_tokens", "cache_write", "usd", "tool_calls",
                   "graph_tool_calls", "tools": {...},
                   "usd_upstream", "usd_source", "by_model": {...},
                   "by_tier": {"<tier>": {"usd", "in_tokens", "cached_in",
                                            "out_tokens", "reasoning_tokens",
                                            "cache_write", "models": [...]}}},
     "session_id", "tier_cost_db": {"source"="sqlite", "usd", "usd_upstream",
                   "in_tokens", ..., "by_tier": {"<tier>": {...}}, "by_model": {...},
                   "sessions", "error"?},
     "artifacts_dir", "stderr_tail"}
telemetry may be missing/None/partial — every numeric field is treated as 0
when absent. usd_upstream/usd_source/by_tier/by_model are newer fields;
older meta records lack them and are treated as 0/None/{}. tier_cost_db is
the SQLite session-DB per-tier reader's output (econ-tier-cost.py) — it
captures the executor tier's token volume, which the stream cannot see, and
is preferred over telemetry.by_tier when present and error-free (see
_effective_by_tier). When a run restarts sessions (context-fill checkpoints),
telemetry.turns/usd would otherwise reflect only the LAST session; run-
benchmark.py's _apply_cross_session_totals already corrects the headline
telemetry.turns/usd to the across-all-sessions totals in that case (from
tier_cost_db.messages/usd_upstream), stashing the last-session-only figures
under telemetry.turns_last_session/usd_last_session and flagging
telemetry.multi_session_corrected=true. No change needed here — report.py
reads the already-corrected headline fields.

--grade-report is the JSON written by swebench.harness.run_evaluation
(top-level "resolved_ids"/"unresolved_ids"/... lists). An instance is
resolved iff its instance_id is in resolved_ids. If omitted, resolve status
is unknown and resolve%/$-per-resolved report as "n/a".

Writes:
    <out>/cost-report.md   — also printed to stdout
    <out>/cost-report.json — structured JSON aggregate
"""

import argparse
import json
import pathlib
import statistics
import sys


# ── Model-tier constants ────────────────────────────────────────────────────
# tier ∈ conductor (cheap bulk) / oracle / reasoner (expensive glm tier,
# may be merged with oracle if they share a model) / executor (self-hosted,
# $0) / small / other.
TIER_ORDER = ["conductor", "oracle", "reasoner", "executor", "small", "other"]
TIER_ABBR  = {"conductor": "cond", "executor": "exec"}


def _tier_abbr(tier):
    return TIER_ABBR.get(tier, tier)


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _num(val, default=0):
    """Coerce val to the type of default; return default on None/failure."""
    if val is None:
        return default
    try:
        return type(default)(val)
    except (TypeError, ValueError):
        return default


# ── Loading ───────────────────────────────────────────────────────────────────

def load_meta(path):
    """Parse a meta JSONL file. Malformed lines are skipped with a stderr warning."""
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(f"Warning: {path}:{lineno}: {exc}", file=sys.stderr)
                    continue
                records.append(rec)
    except OSError as exc:
        print(f"Error: cannot open {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    return records


def load_resolved_ids(path):
    """Parse a swebench run_evaluation grade report. Returns a set of resolved
    instance_ids, or None if path is None/unreadable (resolve status unknown)."""
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: cannot parse grade report {path}: {exc}", file=sys.stderr)
        return None
    return set(data.get("resolved_ids") or [])


def instance_row(rec, resolved_ids, default_label):
    """Flatten one meta record + resolve status into a flat stats dict,
    defensive on missing/None telemetry fields (treated as 0)."""
    tel = rec.get("telemetry")
    tel = tel if isinstance(tel, dict) else {}
    by_tier = tel.get("by_tier")
    by_tier = by_tier if isinstance(by_tier, dict) else {}
    tier_cost_db = rec.get("tier_cost_db")
    tier_cost_db = tier_cost_db if isinstance(tier_cost_db, dict) else {}
    iid = rec.get("instance_id") or "?"
    resolved = None if resolved_ids is None else (iid in resolved_ids)
    return dict(
        instance_id      = iid,
        label            = rec.get("label") or default_label,
        resolved         = resolved,
        turns            = _num(tel.get("turns"), 0),
        in_tokens        = _num(tel.get("in_tokens"), 0),
        cached_in        = _num(tel.get("cached_in"), 0),
        out_tokens       = _num(tel.get("out_tokens"), 0),
        reasoning_tokens = _num(tel.get("reasoning_tokens"), 0),
        usd              = _num(tel.get("usd"), 0.0),
        usd_upstream     = _num(tel.get("usd_upstream"), 0.0),
        usd_source       = tel.get("usd_source") or None,
        by_tier          = by_tier,
        tier_cost_db     = tier_cost_db,
        tool_calls       = _num(tel.get("tool_calls"), 0),
        graph_tool_calls = _num(tel.get("graph_tool_calls"), 0),
        wall_s           = _num(rec.get("wall_s"), 0.0),
        rc               = _num(rec.get("rc"), 0),
        patch_bytes      = _num(rec.get("patch_bytes"), 0),
    )


def group_by_label(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["label"], []).append(r)
    return groups


# ── Statistics ────────────────────────────────────────────────────────────────

def summarise(rows):
    """Return an aggregate stats dict for a list of instance rows."""
    n = len(rows)
    if n == 0:
        return None

    resolved_known = [r["resolved"] for r in rows if r["resolved"] is not None]
    resolved_n   = sum(1 for r in resolved_known if r) if resolved_known else None
    resolve_rate = (resolved_n / len(resolved_known)) if resolved_known else None

    turns       = [r["turns"] for r in rows]
    in_tokens   = [r["in_tokens"] for r in rows]
    cached_in   = [r["cached_in"] for r in rows]
    out_tokens  = [r["out_tokens"] for r in rows]
    usd         = [r["usd"] for r in rows]
    tool_calls  = [r["tool_calls"] for r in rows]
    graph_calls = [r["graph_tool_calls"] for r in rows]
    wall_s      = [r["wall_s"] for r in rows]
    patch_nonzero = [1 if r["patch_bytes"] > 0 else 0 for r in rows]

    total_usd = sum(usd)
    usd_per_resolved = (total_usd / resolved_n) if resolved_n else None

    return dict(
        n                   = n,
        resolved_n          = resolved_n,
        resolve_rate        = resolve_rate,
        mean_turns          = statistics.mean(turns),
        total_in_tokens     = sum(in_tokens),
        mean_in_tokens      = statistics.mean(in_tokens),
        total_cached_in     = sum(cached_in),
        mean_cached_in      = statistics.mean(cached_in),
        total_out_tokens    = sum(out_tokens),
        mean_out_tokens     = statistics.mean(out_tokens),
        total_usd           = total_usd,
        mean_usd            = total_usd / n,
        usd_per_resolved    = usd_per_resolved,
        mean_tool_calls     = statistics.mean(tool_calls),
        mean_graph_calls    = statistics.mean(graph_calls),
        mean_wall_s         = statistics.mean(wall_s),
        patch_nonzero_rate  = sum(patch_nonzero) / n,
    )


# ── Tier aggregation ──────────────────────────────────────────────────────────

def _effective_by_tier(row):
    """Pick the authoritative per-tier source for one row: prefer
    tier_cost_db.by_tier (econ-tier-cost.py's SQLite session-DB reader —
    captures the executor tier's token volume, which the --format json
    stream cannot see, and splits oracle vs reasoner) when present,
    non-empty, and error-free; else fall back to the stream's
    telemetry.by_tier. Returns (by_tier_dict, source) with source ∈
    {"sqlite", "stream"}."""
    tcd = row.get("tier_cost_db") or {}
    db_by_tier = tcd.get("by_tier")
    if isinstance(db_by_tier, dict) and db_by_tier and not tcd.get("error"):
        return db_by_tier, "sqlite"
    return row.get("by_tier") or {}, "stream"


def aggregate_by_tier(rows):
    """Sum each row's effective by_tier (see _effective_by_tier) into one
    dict keyed by tier name. Rows with no usable by_tier from either source
    contribute nothing (defensive on older meta records that predate both
    fields). Returns (agg, source_counts) where source_counts is
    {"sqlite": n, "stream": n} — how many contributing rows used each
    source."""
    agg = {}
    source_counts = {"sqlite": 0, "stream": 0}
    for r in rows:
        by_tier, source = _effective_by_tier(r)
        if not by_tier:
            continue
        source_counts[source] += 1
        for tier, t in by_tier.items():
            if not isinstance(t, dict):
                continue
            a = agg.setdefault(tier, dict(
                usd=0.0, in_tokens=0, cached_in=0, out_tokens=0,
                reasoning_tokens=0, cache_write=0, instances=0, priced=True,
            ))
            a["usd"]              += _num(t.get("usd"), 0.0)
            a["in_tokens"]        += _num(t.get("in_tokens"), 0)
            a["cached_in"]        += _num(t.get("cached_in"), 0)
            a["out_tokens"]       += _num(t.get("out_tokens"), 0)
            a["reasoning_tokens"] += _num(t.get("reasoning_tokens"), 0)
            a["cache_write"]      += _num(t.get("cache_write"), 0)
            a["instances"]        += 1
            # a tier is "unpriced" if ANY contributing row's tier bucket was
            # unpriced (a model missing from ECON_COST_MATRIX → silent $0).
            if t.get("priced") is False:
                a["priced"] = False
    return agg, source_counts


# ── Markdown rendering ────────────────────────────────────────────────────────

def _pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}%"

def _usd(v):
    return "n/a" if v is None else f"${v:.4f}"

def _f(v, d=2):
    return f"{v:.{d}f}"

def _ratio(numer, denom):
    return "n/a" if numer is None else f"{numer}/{denom}"


def md_table(headers, rows):
    """Render an auto-width Markdown table."""
    str_rows = [[str(c) for c in row] for row in rows]
    widths = [
        max(len(h), max((len(r[i]) for r in str_rows), default=0))
        for i, h in enumerate(headers)
    ]

    def fmt(cells):
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    return "\n".join([
        fmt(headers),
        fmt(["-" * w for w in widths]),
        *[fmt(r) for r in str_rows],
    ])


def sec_summary(instance_rows, groups):
    hdrs = [
        "Label", "N", "Resolve%", "Resolved/N",
        "Mean Turns",
        "Total In Tok", "Mean In Tok",
        "Total Cached", "Mean Cached",
        "Total Out Tok", "Mean Out Tok",
        "Total $", "Mean $/inst", "$/Resolved",
        "Mean Tool Calls", "Mean Graph-Tool Calls",
        "Mean Wall(s)", "patch>0%",
    ]
    rows = []
    for label, group_rows in sorted(groups.items()):
        s = summarise(group_rows)
        if s is None:
            continue
        rows.append([
            label, s["n"],
            _pct(s["resolve_rate"]), _ratio(s["resolved_n"], s["n"]),
            _f(s["mean_turns"]),
            s["total_in_tokens"],  _f(s["mean_in_tokens"], 0),
            s["total_cached_in"],  _f(s["mean_cached_in"], 0),
            s["total_out_tokens"], _f(s["mean_out_tokens"], 0),
            _usd(s["total_usd"]),  _usd(s["mean_usd"]), _usd(s["usd_per_resolved"]),
            _f(s["mean_tool_calls"]), _f(s["mean_graph_calls"]),
            _f(s["mean_wall_s"]),
            _pct(s["patch_nonzero_rate"]),
        ])
    body = md_table(hdrs, rows) if rows else "_No data found._"

    total_usd          = sum(r["usd"] for r in instance_rows)
    total_usd_upstream = sum(r["usd_upstream"] for r in instance_rows)
    cost_note = ""
    if total_usd > 0 and total_usd_upstream > 0:
        delta_pct = (total_usd_upstream - total_usd) / total_usd * 100
        cost_note = (
            f"\n_Cost basis: econ BYOK ${total_usd:.4f} "
            f"(upstream models.dev would be ${total_usd_upstream:.4f}, "
            f"{delta_pct:+.1f}%)._\n"
        )
    return f"## 1. Summary\n\n{body}\n{cost_note}"


def _fmt_tier_usd(by_tier):
    """Compact per-tier $ split for one instance, e.g.
    'cond $0.0003 · oracle $0.0154'. Abbreviates conductor/executor,
    omits zero-cost tiers; 'n/a' if by_tier is missing/empty."""
    if not by_tier:
        return "n/a"
    ordered = TIER_ORDER + sorted(k for k in by_tier if k not in TIER_ORDER)
    parts = []
    for tier in ordered:
        t = by_tier.get(tier)
        if not isinstance(t, dict):
            continue
        usd = _num(t.get("usd"), 0.0)
        if usd == 0:
            continue
        parts.append(f"{_tier_abbr(tier)} ${usd:.4f}")
    return " · ".join(parts) if parts else "n/a"


def sec_per_instance(rows):
    hdrs = [
        "Instance", "Resolved", "Turns", "In Tok", "Cached", "Out Tok",
        "$", "Tier $", "Tool Calls", "Graph Calls", "Wall(s)", "rc", "patch(bytes)",
    ]
    body_rows = []
    for r in sorted(rows, key=lambda r: (r["label"], r["instance_id"])):
        mark = "?" if r["resolved"] is None else ("✓" if r["resolved"] else "✗")
        # Use the SAME effective source (SQLite tier_cost_db when present) as the
        # Per-Tier table so the two sections reconcile — the raw stream by_tier
        # mislabels reasoner spend as oracle and zeroes unpriced tiers.
        eff_by_tier, _src = _effective_by_tier(r)
        body_rows.append([
            r["instance_id"], mark, r["turns"], r["in_tokens"], r["cached_in"],
            r["out_tokens"], _usd(r["usd"]), _fmt_tier_usd(eff_by_tier),
            r["tool_calls"], r["graph_tool_calls"],
            _f(r["wall_s"]), r["rc"], r["patch_bytes"],
        ])
    body = md_table(hdrs, body_rows) if body_rows else "_No instances found._"
    return f"## 2. Per-Instance\n\n{body}\n"


def sec_per_tier(rows):
    agg, source_counts = aggregate_by_tier(rows)
    grand_total = sum(a["usd"] for a in agg.values())
    unpriced_tiers = [t for t, a in agg.items() if a.get("priced") is False]
    hdrs = [
        "Tier", "Total $", "% of Total $", "Mean $/inst",
        "Total In Tok", "Total Cached", "Total Out Tok", "Reasoning Tok",
        "Cache Write", "Total Tokens", "Cache-Hit %", "Instances Used",
    ]
    body_rows = []
    for tier, a in sorted(agg.items(), key=lambda kv: kv[1]["usd"], reverse=True):
        n_used = a["instances"]
        mean_usd = (a["usd"] / n_used) if n_used else 0.0
        pct_ratio = (a["usd"] / grand_total) if grand_total else None
        total_tokens = a["in_tokens"] + a["cached_in"] + a["out_tokens"]
        # cache-hit % = cache-read input / all input seen (uncached + cached read)
        cache_denom = a["in_tokens"] + a["cached_in"]
        cache_hit = (a["cached_in"] / cache_denom) if cache_denom else None
        # ⚠️ marks a tier whose USD is understated — a model it used is missing
        # from ECON_COST_MATRIX, so its cost silently read $0.
        label = f"{tier} ⚠️" if a.get("priced") is False else tier
        body_rows.append([
            label, _usd(a["usd"]), _pct(pct_ratio), _usd(mean_usd),
            a["in_tokens"], a["cached_in"], a["out_tokens"], a["reasoning_tokens"],
            a["cache_write"], total_tokens, _pct(cache_hit), n_used,
        ])
    body = (
        md_table(hdrs, body_rows) if body_rows
        else "_No per-tier telemetry found (older meta records lack by_tier)._"
    )
    if source_counts["sqlite"] and source_counts["stream"]:
        source_line = (
            f"Source: {source_counts['sqlite']} instance(s) from the econ "
            f"session DB (includes executor volume) · "
            f"{source_counts['stream']} instance(s) fall back to the "
            f"--format json stream (executor volume not captured for those)."
        )
    elif source_counts["sqlite"]:
        source_line = "Source: econ session DB (includes executor volume)."
    elif source_counts["stream"]:
        source_line = "Source: --format json stream (executor volume not captured)."
    else:
        source_line = "Source: n/a (no per-tier telemetry found)."
    caption = (
        f"\n_{source_line} conductor = cheap bulk tier (minimax-m3) · "
        "reasoner = deepseek-v4-pro · oracle = glm-5.2 (most expensive/1M) · "
        "executor = gpt-oss-120b. USD is econ's Fireworks-BYOK price "
        "(ECON_COST_MATRIX), recomputed per tier from the session DB token "
        "counts._\n"
    )
    if unpriced_tiers:
        caption += (
            f"\n> ⚠️ **Per-tier USD understated.** These tier(s) used a model "
            f"absent from ECON_COST_MATRIX, so their cost silently read $0: "
            f"**{', '.join(sorted(unpriced_tiers))}**. Add the model's rate to "
            f"`e2e/econ/econ-tier-cost.py` (mirror econ-cost.ts) and re-run the "
            f"report.\n"
        )
    return f"## 4. Per-Tier Cost\n\n{body}\n{caption}"


def sec_notes(rows):
    n = len(rows)
    rc_nonzero  = sum(1 for r in rows if r["rc"] != 0)
    empty_patch = sum(1 for r in rows if r["patch_bytes"] == 0)
    no_graph    = sum(1 for r in rows if r["graph_tool_calls"] == 0)
    # cache-hit % = cache-read input / all input seen (uncached + cache-read),
    # so it stays in 0–100% (cached_in/in_tokens alone exceeds 100% here because
    # cache reads dwarf uncached input).
    cache_ratios = [r["cached_in"] / (r["in_tokens"] + r["cached_in"])
                    for r in rows if (r["in_tokens"] + r["cached_in"]) > 0]
    mean_cache_ratio = statistics.mean(cache_ratios) if cache_ratios else None

    lines = [
        f"- {rc_nonzero}/{n} instance(s) exited non-zero (rc != 0).",
        f"- {empty_patch}/{n} instance(s) produced an empty patch (patch_bytes == 0).",
    ]
    flag = ""
    if no_graph > 0:
        flag = " — FLAG: econ's embedded graph tools didn't fire (want >0)"
    lines.append(f"- {no_graph}/{n} instance(s) recorded zero graph_tool_calls{flag}.")
    lines.append(f"- Mean cache-hit ratio (cache-read / all input): {_pct(mean_cache_ratio)}")

    unpriced_tiers = [t for t, a in aggregate_by_tier(rows)[0].items()
                      if a.get("priced") is False]
    if unpriced_tiers:
        lines.append(
            f"- {len(unpriced_tiers)} tier(s) have UNPRICED models (per-tier USD "
            f"understated to $0) — FLAG: add the model rate(s) to "
            f"econ-tier-cost.py::ECON_COST_MATRIX: {', '.join(sorted(unpriced_tiers))}"
        )

    fallback_ids = [r["instance_id"] for r in rows if r.get("usd_source") == "upstream_fallback"]
    if fallback_ids:
        lines.append(
            f"- {len(fallback_ids)}/{n} instance(s) have usd_source == "
            f"\"upstream_fallback\" — FLAG: that instance's cost is the "
            f"models.dev catalog price, not econ's BYOK matrix (the econ "
            f"binary didn't emit a cost_breakdown / is out of date): "
            f"{', '.join(fallback_ids)}"
        )
    return "## 5. Notes\n\n" + "\n".join(lines) + "\n"


def render_report(label, rows, groups):
    return "\n".join([
        f"# {label} Benchmark — Performance & Cost Report",
        "",
        sec_summary(rows, groups),
        sec_per_instance(rows),
        sec_per_tier(rows),
        sec_notes(rows),
    ])


# ── JSON output ───────────────────────────────────────────────────────────────

def build_json(label, rows):
    n = len(rows)
    resolved_known = [r["resolved"] for r in rows if r["resolved"] is not None]
    resolved_n   = sum(1 for r in resolved_known if r) if resolved_known else None
    resolve_rate = (resolved_n / len(resolved_known)) if resolved_known else None

    keys = ["in_tokens", "cached_in", "out_tokens", "usd", "turns",
            "tool_calls", "graph_tool_calls"]
    totals = {k: sum(r[k] for r in rows) for k in keys}
    means  = {k: (totals[k] / n if n else 0.0) for k in keys}

    total_usd = totals["usd"]
    usd_per_resolved = (total_usd / resolved_n) if resolved_n else None
    usd_upstream_total = sum(r["usd_upstream"] for r in rows)

    by_tier_agg, source_counts = aggregate_by_tier(rows)
    if source_counts["sqlite"] and source_counts["stream"]:
        tier_cost_source = "mixed"
    elif source_counts["sqlite"]:
        tier_cost_source = "sqlite"
    elif source_counts["stream"]:
        tier_cost_source = "stream"
    else:
        tier_cost_source = None
    by_tier_json = {
        tier: {
            "usd":              a["usd"],
            "in_tokens":        a["in_tokens"],
            "cached_in":        a["cached_in"],
            "out_tokens":       a["out_tokens"],
            "reasoning_tokens": a["reasoning_tokens"],
            "cache_write":      a["cache_write"],
            "instances_used":   a["instances"],
            # False → this tier used a model missing from ECON_COST_MATRIX, so
            # its USD is a silent $0 (drift signal).
            "priced":           a.get("priced", True),
        }
        for tier, a in by_tier_agg.items()
    }
    unpriced_tiers = sorted(t for t, a in by_tier_agg.items() if a.get("priced") is False)

    per_instance = [
        {
            "instance_id":      r["instance_id"],
            "label":            r["label"],
            "resolved":         r["resolved"],
            "turns":            r["turns"],
            "in_tokens":        r["in_tokens"],
            "cached_in":        r["cached_in"],
            "out_tokens":       r["out_tokens"],
            "reasoning_tokens": r["reasoning_tokens"],
            "usd":              r["usd"],
            "tool_calls":       r["tool_calls"],
            "graph_tool_calls": r["graph_tool_calls"],
            "wall_s":           r["wall_s"],
            "rc":               r["rc"],
            "patch_bytes":      r["patch_bytes"],
        }
        for r in sorted(rows, key=lambda r: (r["label"], r["instance_id"]))
    ]

    return {
        "label":            label,
        "n":                n,
        "resolved":         resolved_n,
        "resolve_rate":     resolve_rate,
        "totals":           totals,
        "means":            means,
        "usd_per_resolved": usd_per_resolved,
        "usd_upstream_total": usd_upstream_total,
        "tier_cost_source": tier_cost_source,
        "by_tier":          by_tier_json,
        "unpriced_tiers":   unpriced_tiers,
        "per_instance":     per_instance,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--meta", required=True, type=pathlib.Path,
        help="path to a meta.jsonl file (one record per instance)",
    )
    ap.add_argument(
        "--grade-report", type=pathlib.Path, default=None,
        help="path to the swebench.harness.run_evaluation JSON report (optional)",
    )
    ap.add_argument(
        "--label", default="econ",
        help="report label (default: econ)",
    )
    ap.add_argument(
        "--out", type=pathlib.Path, default=None,
        help="output dir for cost-report.md/.json (default: --meta's directory)",
    )
    args = ap.parse_args(argv)

    if not args.meta.is_file():
        print(f"Error: meta file not found: {args.meta}", file=sys.stderr)
        sys.exit(1)

    raw_records = load_meta(args.meta)
    if not raw_records:
        print(f"Error: no records parsed from {args.meta} — nothing to report.", file=sys.stderr)
        sys.exit(1)

    resolved_ids = load_resolved_ids(args.grade_report)
    rows   = [instance_row(rec, resolved_ids, args.label) for rec in raw_records]
    groups = group_by_label(rows)

    report = render_report(args.label, rows, groups)
    print(report)

    out_dir = args.out or args.meta.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cost-report.md").write_text(report, encoding="utf-8")
    (out_dir / "cost-report.json").write_text(
        json.dumps(build_json(args.label, rows), indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nWritten: {out_dir}/cost-report.md  {out_dir}/cost-report.json", file=sys.stderr)


if __name__ == "__main__":
    main()
