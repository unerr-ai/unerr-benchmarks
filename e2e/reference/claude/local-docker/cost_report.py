#!/usr/bin/env python3
"""
cost_report.py — Open-models run cost aggregation: token + cost report.

CLI:
    python3 cost_report.py <run_dir> [--mode on] [--grade <path/to/report.json>] [--json] [--detailed]

<run_dir> is the output directory (e.g. <out>/<label>/) containing:
  - meta_{mode}.jsonl — one JSON object per instance with fields:
      instance_id, mode, wall_s, patch_bytes, rc
      telemetry: {turns, in_tokens, cached_in, out_tokens, tool_calls, ...} (may be absent)
      cost: {source, usd, usd_recomputed, requests, in_tokens, cached_in, out_tokens,
             total_tokens, by_model, by_tier} (may be absent on old runs)
  - grade report (optional): JSON with resolved_ids/unresolved_ids lists

If --grade is provided, instances are marked resolved/unresolved.
If omitted, resolve status is unknown and resolve% reports as "n/a".

--detailed (default off, so base output is byte-identical) appends two more
sections built by pivoting the SAME parsed cost.by_tier data instance_row()
already carries — no re-fetch from LiteLLM: a per-task x tier cost matrix,
and a long-format per-task x tier detail (turns/$/tokens/cache%/resolved).

Writes stdout + cost-report.md/cost-report.json to <run_dir>.
"""

import argparse
import collections
import json
import pathlib
import sys


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _num(val, default=0):
    """Coerce val to the type of default; return default on None/failure."""
    if val is None:
        return default
    try:
        return type(default)(val)
    except (TypeError, ValueError):
        return default


def _cache_hit_rate(in_tokens, cached_in) -> float:
    """Fraction of input tokens served from cache: cached_in / (in_tokens +
    cached_in). Zero-guarded — returns 0.0 when there are no input tokens.
    Derived here (not read off a cost block's `cache_hit_rate` field) so
    older meta records that predate the field still aggregate correctly."""
    denom = in_tokens + cached_in
    return round(cached_in / denom, 4) if denom else 0.0


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
    """Parse a swebench grade report. Returns a set of resolved instance_ids,
    or None if path is None/unreadable (resolve status unknown)."""
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: cannot parse grade report {path}: {exc}", file=sys.stderr)
        return None
    return set(data.get("resolved_ids") or [])


def instance_row(rec, resolved_ids):
    """Flatten one meta record + resolve status into a flat stats dict,
    defensive on missing/None cost fields (treated as 0)."""
    iid = rec.get("instance_id") or "?"
    resolved = None if resolved_ids is None else (iid in resolved_ids)
    cost = rec.get("cost")
    cost = cost if isinstance(cost, dict) else {}
    cost_source = cost.get("source")
    has_cost_data = cost_source == "litellm_spend_logs"
    telemetry = rec.get("telemetry")
    telemetry = telemetry if isinstance(telemetry, dict) else {}

    return dict(
        instance_id     = iid,
        mode            = rec.get("mode") or "unknown",
        resolved        = resolved,
        wall_s          = _num(rec.get("wall_s"), 0.0),
        patch_bytes     = _num(rec.get("patch_bytes"), 0),
        rc              = _num(rec.get("rc"), 0),
        turns           = _num(telemetry.get("turns"), 0),
        usd             = _num(cost.get("usd"), 0.0) if has_cost_data else 0.0,
        usd_recomputed  = _num(cost.get("usd_recomputed"), 0.0) if has_cost_data else 0.0,
        requests        = _num(cost.get("requests"), 0) if has_cost_data else 0,
        in_tokens       = _num(cost.get("in_tokens"), 0) if has_cost_data else 0,
        cached_in       = _num(cost.get("cached_in"), 0) if has_cost_data else 0,
        out_tokens      = _num(cost.get("out_tokens"), 0) if has_cost_data else 0,
        total_tokens    = _num(cost.get("total_tokens"), 0) if has_cost_data else 0,
        by_model        = cost.get("by_model") or {} if has_cost_data else {},
        by_tier         = cost.get("by_tier") or {} if has_cost_data else {},
        cost_source     = cost_source,
        has_cost_data   = has_cost_data,
    )


# ── Statistics ────────────────────────────────────────────────────────────────

def summarise(rows):
    """Return an aggregate stats dict for a list of instance rows."""
    n = len(rows)
    if n == 0:
        return None

    resolved_known = [r["resolved"] for r in rows if r["resolved"] is not None]
    resolved_n   = sum(1 for r in resolved_known if r) if resolved_known else None
    resolve_rate = (resolved_n / len(resolved_known)) if resolved_known else None

    # Cost metrics
    has_cost = [r for r in rows if r["has_cost_data"]]
    n_cost = len(has_cost)
    usd = [r["usd"] for r in has_cost]
    requests = [r["requests"] for r in has_cost]
    in_tokens = [r["in_tokens"] for r in has_cost]
    cached_in = [r["cached_in"] for r in has_cost]
    out_tokens = [r["out_tokens"] for r in has_cost]
    wall_s = [r["wall_s"] for r in rows]

    total_usd = sum(usd)
    usd_per_resolved = (total_usd / resolved_n) if resolved_n and resolved_n > 0 else None
    total_in_tokens = sum(in_tokens)
    total_cached_in = sum(cached_in)

    return dict(
        n                   = n,
        n_with_cost         = n_cost,
        resolved_n          = resolved_n,
        resolve_rate        = resolve_rate,
        total_usd           = total_usd,
        mean_usd            = total_usd / n_cost if n_cost else 0.0,
        usd_per_resolved    = usd_per_resolved,
        total_requests      = sum(requests),
        mean_requests       = sum(requests) / n_cost if n_cost else 0.0,
        total_in_tokens     = total_in_tokens,
        mean_in_tokens      = sum(in_tokens) / n_cost if n_cost else 0.0,
        total_cached_in     = total_cached_in,
        mean_cached_in      = sum(cached_in) / n_cost if n_cost else 0.0,
        total_out_tokens    = sum(out_tokens),
        mean_out_tokens     = sum(out_tokens) / n_cost if n_cost else 0.0,
        mean_wall_s         = sum(wall_s) / n if n else 0.0,
        cache_hit_rate      = _cache_hit_rate(total_in_tokens, total_cached_in),
    )


# ── Tier and model aggregation ─────────────────────────────────────────────────

def aggregate_by_tier(rows):
    """Sum each row's by_tier into one dict keyed by tier name.
    Returns (agg_dict, instances_with_tier_data)."""
    agg = {}
    instances_with_data = 0
    for r in rows:
        if not r["has_cost_data"] or not r["by_tier"]:
            continue
        instances_with_data += 1
        for tier, t in r["by_tier"].items():
            if not isinstance(t, dict):
                continue
            a = agg.setdefault(tier, dict(
                usd=0.0, in_tokens=0, cached_in=0, out_tokens=0,
                requests=0, models=set(), instances=0,
            ))
            a["usd"]         += _num(t.get("usd"), 0.0)
            a["in_tokens"]   += _num(t.get("in_tokens"), 0)
            a["cached_in"]   += _num(t.get("cached_in"), 0)
            a["out_tokens"]  += _num(t.get("out_tokens"), 0)
            a["requests"]    += _num(t.get("requests"), 0)
            a["instances"]   += 1
            models = t.get("models")
            if isinstance(models, list):
                a["models"].update(models)
    for a in agg.values():
        a["cache_hit_rate"] = _cache_hit_rate(a["in_tokens"], a["cached_in"])
    return agg, instances_with_data


def aggregate_by_model(rows):
    """Sum each row's by_model into one dict keyed by model name.
    Each model entry includes its tier. Returns agg_dict."""
    agg = {}
    for r in rows:
        if not r["has_cost_data"] or not r["by_model"]:
            continue
        for model, m in r["by_model"].items():
            if not isinstance(m, dict):
                continue
            a = agg.setdefault(model, dict(
                tier=m.get("tier") or "unknown",
                usd=0.0, in_tokens=0, cached_in=0, out_tokens=0,
                requests=0, instances=0,
            ))
            a["usd"]         += _num(m.get("usd"), 0.0)
            a["in_tokens"]   += _num(m.get("in_tokens"), 0)
            a["cached_in"]   += _num(m.get("cached_in"), 0)
            a["out_tokens"]  += _num(m.get("out_tokens"), 0)
            a["requests"]    += _num(m.get("requests"), 0)
            a["instances"]   += 1
    for a in agg.values():
        a["cache_hit_rate"] = _cache_hit_rate(a["in_tokens"], a["cached_in"])
    return agg


# ── Per-task x tier pivot (--detailed only) ─────────────────────────────────────

CANONICAL_TIER_ORDER = ["sonnet", "haiku", "opus", "fable"]


def _tier_columns(rows):
    """Ordered tier-column list for the per-task x tier tables: the canonical
    claude model classes (sonnet/haiku/opus/fable) first, any other tier
    names seen in the data appended alphabetically after."""
    seen = set()
    for r in rows:
        seen.update(r["by_tier"].keys())
    ordered = [t for t in CANONICAL_TIER_ORDER if t in seen]
    ordered += sorted(t for t in seen if t not in CANONICAL_TIER_ORDER)
    return ordered


def build_task_tier_matrix(rows):
    """Per-task x tier cost matrix: one row per instance, one $ column per
    tier (zero-filled for a tier the task didn't use) plus a Total column.
    Pivots the already-parsed cost.by_tier off instance_row — no re-fetch."""
    tiers = _tier_columns(rows)
    matrix = []
    for r in rows:
        tier_usd = {t: _num((r["by_tier"].get(t) or {}).get("usd"), 0.0) for t in tiers}
        matrix.append(dict(
            instance_id=r["instance_id"],
            resolved=r["resolved"],
            tiers=tier_usd,
            total=sum(tier_usd.values()),
        ))
    return tiers, matrix


def build_task_tier_detail(rows):
    """Long-format per-task x tier detail: one row per (instance, tier) with
    $, tokens, cache%, requests, the task's turns (telemetry-level — same
    value repeated on every tier row for that task, since turns aren't
    tracked per-tier) and its resolved status (joined via --grade).
    Pivots the already-parsed cost.by_tier off instance_row — no re-fetch."""
    detail = []
    for r in rows:
        if not r["by_tier"]:
            detail.append(dict(
                instance_id=r["instance_id"], tier="(no cost data)",
                turns=r["turns"], resolved=r["resolved"],
                usd=0.0, in_tokens=0, cached_in=0, out_tokens=0,
                cache_hit_rate=0.0, requests=0,
            ))
            continue
        for tier, t in sorted(r["by_tier"].items()):
            in_tok = _num(t.get("in_tokens"), 0)
            cached = _num(t.get("cached_in"), 0)
            hit_rate = t.get("cache_hit_rate")
            if not isinstance(hit_rate, (int, float)):
                hit_rate = _cache_hit_rate(in_tok, cached)
            detail.append(dict(
                instance_id=r["instance_id"], tier=tier,
                turns=r["turns"], resolved=r["resolved"],
                usd=_num(t.get("usd"), 0.0),
                in_tokens=in_tok, cached_in=cached,
                out_tokens=_num(t.get("out_tokens"), 0),
                cache_hit_rate=hit_rate,
                requests=_num(t.get("requests"), 0),
            ))
    return detail


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


def sec_summary(instance_rows):
    """Generate summary section with run-level stats."""
    s = summarise(instance_rows)
    if s is None:
        return "## 1. Summary\n\n_No data found._\n"

    hdrs = [
        "N", "N w/ cost", "Resolved", "Resolve%",
        "Total $", "Mean $/inst", "$/Resolved",
        "Total Turns", "Mean Turns",
        "Total In Tok", "Mean In Tok",
        "Total Cached", "Mean Cached",
        "Total Out Tok", "Mean Out Tok",
        "Cache Hit%",
        "Mean Wall(s)",
    ]
    rows = [
        [
            s["n"], s["n_with_cost"],
            _ratio(s["resolved_n"], s["n"]), _pct(s["resolve_rate"]),
            _usd(s["total_usd"]), _usd(s["mean_usd"]), _usd(s["usd_per_resolved"]),
            s["total_requests"], _f(s["mean_requests"]),
            s["total_in_tokens"], _f(s["mean_in_tokens"], 0),
            s["total_cached_in"], _f(s["mean_cached_in"], 0),
            s["total_out_tokens"], _f(s["mean_out_tokens"], 0),
            _pct(s["cache_hit_rate"]),
            _f(s["mean_wall_s"]),
        ]
    ]
    body = md_table(hdrs, rows)
    return f"## 1. Summary\n\n{body}\n"


def sec_per_instance(rows):
    """Generate per-instance table sorted by cost descending."""
    hdrs = [
        "Instance", "Resolved", "Mode", "$", "Turns",
        "In Tok", "Cached", "Out Tok",
        "Wall(s)", "rc", "patch(bytes)",
    ]
    body_rows = []
    for r in sorted(rows, key=lambda r: r["usd"], reverse=True):
        mark = "?" if r["resolved"] is None else ("✓" if r["resolved"] else "✗")
        body_rows.append([
            r["instance_id"], mark, r["mode"],
            _usd(r["usd"]), r["requests"],
            r["in_tokens"], r["cached_in"], r["out_tokens"],
            _f(r["wall_s"]), r["rc"], r["patch_bytes"],
        ])
    body = md_table(hdrs, body_rows) if body_rows else "_No instances found._"
    return f"## 2. Per-Instance\n\n{body}\n"


def sec_by_tier(rows):
    """Generate per-tier cost breakdown table."""
    agg, n_with_data = aggregate_by_tier(rows)
    grand_total = sum(a["usd"] for a in agg.values())

    hdrs = [
        "Tier", "Total $", "% of Total $", "Mean $/inst",
        "Total In Tok", "Total Cached", "Total Out Tok",
        "Total Tokens", "Cache Hit%", "Turns", "Models",
    ]
    body_rows = []
    for tier, a in sorted(agg.items(), key=lambda kv: kv[1]["usd"], reverse=True):
        n_used = a["instances"]
        mean_usd = (a["usd"] / n_used) if n_used else 0.0
        pct_ratio = (a["usd"] / grand_total) if grand_total else None
        total_tokens = a["in_tokens"] + a["cached_in"] + a["out_tokens"]
        models_str = ", ".join(sorted(a["models"])) if a["models"] else "n/a"
        body_rows.append([
            tier, _usd(a["usd"]), _pct(pct_ratio), _usd(mean_usd),
            a["in_tokens"], a["cached_in"], a["out_tokens"],
            total_tokens, _pct(a["cache_hit_rate"]), a["requests"], models_str,
        ])
    body = (
        md_table(hdrs, body_rows) if body_rows
        else "_No per-tier data found._"
    )
    caption = (
        f"\n_{n_with_data} instance(s) have per-tier cost data "
        f"(source: litellm_spend_logs)._\n"
    )
    return f"## 3. By Tier\n\n{body}\n{caption}"


def sec_by_model(rows):
    """Generate per-model cost breakdown table."""
    agg = aggregate_by_model(rows)
    grand_total = sum(a["usd"] for a in agg.values())

    hdrs = [
        "Model", "Tier", "Total $", "% of Total $", "Mean $/inst",
        "Total In Tok", "Total Cached", "Total Out Tok",
        "Total Tokens", "Cache Hit%", "Turns",
    ]
    body_rows = []
    for model, a in sorted(agg.items(), key=lambda kv: kv[1]["usd"], reverse=True):
        n_used = a["instances"]
        mean_usd = (a["usd"] / n_used) if n_used else 0.0
        pct_ratio = (a["usd"] / grand_total) if grand_total else None
        total_tokens = a["in_tokens"] + a["cached_in"] + a["out_tokens"]
        body_rows.append([
            model, a["tier"], _usd(a["usd"]), _pct(pct_ratio), _usd(mean_usd),
            a["in_tokens"], a["cached_in"], a["out_tokens"],
            total_tokens, _pct(a["cache_hit_rate"]), a["requests"],
        ])
    body = (
        md_table(hdrs, body_rows) if body_rows
        else "_No per-model data found._"
    )
    return f"## 4. By Model\n\n{body}\n"


def sec_task_tier_matrix(rows):
    """Generate the per-task x tier cost matrix section (--detailed only)."""
    tiers, matrix = build_task_tier_matrix(rows)
    if not matrix:
        return "## 6. Per-Task x Tier Cost Matrix\n\n_No data found._\n"

    hdrs = ["Instance", "Resolved"] + [t.capitalize() for t in tiers] + ["Total $"]
    body_rows = []
    for m in sorted(matrix, key=lambda m: m["total"], reverse=True):
        mark = "?" if m["resolved"] is None else ("✓" if m["resolved"] else "✗")
        body_rows.append(
            [m["instance_id"], mark] + [_usd(m["tiers"][t]) for t in tiers] + [_usd(m["total"])]
        )
    body = md_table(hdrs, body_rows)
    return f"## 6. Per-Task x Tier Cost Matrix\n\n{body}\n"


def sec_task_tier_detail(rows):
    """Generate the long-format per-task x tier detail section (--detailed only)."""
    detail = build_task_tier_detail(rows)
    if not detail:
        return "## 7. Per-Task x Tier Detail\n\n_No data found._\n"

    hdrs = ["Instance", "Tier", "Turns", "$", "In Tok", "Cached", "Out Tok", "Cache%", "Requests", "Resolved"]
    body_rows = []
    for d in detail:
        mark = "?" if d["resolved"] is None else ("✓" if d["resolved"] else "✗")
        body_rows.append([
            d["instance_id"], d["tier"], d["turns"], _usd(d["usd"]),
            d["in_tokens"], d["cached_in"], d["out_tokens"],
            _pct(d["cache_hit_rate"]), d["requests"], mark,
        ])
    body = md_table(hdrs, body_rows)
    return f"## 7. Per-Task x Tier Detail\n\n{body}\n"


def sec_notes(rows):
    """Generate notes section with flagged instances."""
    n = len(rows)
    no_cost = sum(1 for r in rows if not r["has_cost_data"])
    rc_nonzero = sum(1 for r in rows if r["rc"] != 0)
    empty_patch = sum(1 for r in rows if r["patch_bytes"] == 0)

    lines = []
    if no_cost > 0:
        no_cost_ids = [r["instance_id"] for r in rows if not r["has_cost_data"]]
        lines.append(
            f"- {no_cost}/{n} instance(s) have no cost data "
            f"(no litellm_spend_logs): {', '.join(sorted(no_cost_ids))}"
        )
    lines.append(f"- {rc_nonzero}/{n} instance(s) exited non-zero (rc != 0).")
    lines.append(f"- {empty_patch}/{n} instance(s) produced an empty patch (patch_bytes == 0).")

    if not lines:
        lines.append("- No flagged instances.")

    return "## 5. Notes\n\n" + "\n".join(lines) + "\n"


def render_report(rows, detailed=False):
    """Render all sections into a single report string. detailed=True (off
    by default, so the base report stays byte-identical) appends the
    per-task x tier matrix + long-format detail sections after Notes."""
    sections = [
        "# Open-Models Run — Cost & Token Report",
        "",
        sec_summary(rows),
        sec_per_instance(rows),
        sec_by_tier(rows),
        sec_by_model(rows),
        sec_notes(rows),
    ]
    if detailed:
        sections += [sec_task_tier_matrix(rows), sec_task_tier_detail(rows)]
    return "\n".join(sections)


# ── JSON output ───────────────────────────────────────────────────────────────

def build_json(rows, detailed=False):
    """Build a structured JSON aggregate. detailed=True (off by default, so
    the base JSON stays byte-identical) adds task_tier_matrix/task_tier_detail
    keys pivoting the same per-instance cost.by_tier data — no re-fetch."""
    n = len(rows)
    rows_with_cost = [r for r in rows if r["has_cost_data"]]
    n_with_cost = len(rows_with_cost)

    resolved_known = [r["resolved"] for r in rows if r["resolved"] is not None]
    resolved_n   = sum(1 for r in resolved_known if r) if resolved_known else None
    resolve_rate = (resolved_n / len(resolved_known)) if resolved_known else None

    total_usd = sum(r["usd"] for r in rows_with_cost)
    usd_per_resolved = (total_usd / resolved_n) if resolved_n and resolved_n > 0 else None
    total_in_tokens = sum(r["in_tokens"] for r in rows_with_cost)
    total_cached_in = sum(r["cached_in"] for r in rows_with_cost)

    by_tier_agg, _ = aggregate_by_tier(rows)
    by_tier_json = {
        tier: {
            "usd":           a["usd"],
            "in_tokens":     a["in_tokens"],
            "cached_in":     a["cached_in"],
            "out_tokens":    a["out_tokens"],
            "requests":      a["requests"],
            "instances_used": a["instances"],
            "models":        sorted(a["models"]),
            "cache_hit_rate": a["cache_hit_rate"],
        }
        for tier, a in by_tier_agg.items()
    }

    by_model_agg = aggregate_by_model(rows)
    by_model_json = {
        model: {
            "tier":          a["tier"],
            "usd":           a["usd"],
            "in_tokens":     a["in_tokens"],
            "cached_in":     a["cached_in"],
            "out_tokens":    a["out_tokens"],
            "requests":      a["requests"],
            "instances_used": a["instances"],
            "cache_hit_rate": a["cache_hit_rate"],
        }
        for model, a in by_model_agg.items()
    }

    per_instance = [
        {
            "instance_id":   r["instance_id"],
            "mode":          r["mode"],
            "resolved":      r["resolved"],
            "usd":           r["usd"],
            "requests":      r["requests"],
            "in_tokens":     r["in_tokens"],
            "cached_in":     r["cached_in"],
            "out_tokens":    r["out_tokens"],
            "wall_s":        r["wall_s"],
            "rc":            r["rc"],
            "patch_bytes":   r["patch_bytes"],
            "has_cost_data": r["has_cost_data"],
        }
        for r in sorted(rows, key=lambda r: r["usd"], reverse=True)
    ]

    out = {
        "n":                n,
        "n_with_cost":      n_with_cost,
        "resolved":         resolved_n,
        "resolve_rate":     resolve_rate,
        "total_usd":        total_usd,
        "mean_usd":         total_usd / n_with_cost if n_with_cost else 0.0,
        "usd_per_resolved": usd_per_resolved,
        "total_requests":   sum(r["requests"] for r in rows_with_cost),
        "total_in_tokens":  total_in_tokens,
        "total_cached_in":  total_cached_in,
        "total_out_tokens": sum(r["out_tokens"] for r in rows_with_cost),
        "cache_hit_rate":   _cache_hit_rate(total_in_tokens, total_cached_in),
        "by_tier":          by_tier_json,
        "by_model":         by_model_json,
        "per_instance":     per_instance,
    }

    if detailed:
        tiers, matrix = build_task_tier_matrix(rows)
        out["task_tier_matrix"] = {
            "tiers": tiers,
            "rows": [
                {"instance_id": m["instance_id"], "resolved": m["resolved"],
                 "by_tier": m["tiers"], "total": m["total"]}
                for m in matrix
            ],
        }
        out["task_tier_detail"] = build_task_tier_detail(rows)

    return out


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "run_dir", type=pathlib.Path,
        help="run directory containing meta_<mode>.jsonl",
    )
    ap.add_argument(
        "--mode", default="on",
        help="mode name in meta_<mode>.jsonl (default: on)",
    )
    ap.add_argument(
        "--grade", type=pathlib.Path, default=None,
        help="path to swebench grade report JSON (optional)",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="output as JSON instead of markdown tables",
    )
    ap.add_argument(
        "--detailed", action="store_true",
        help="append per-task x tier cost matrix + long-format detail sections (off by default)",
    )
    args = ap.parse_args(argv)

    run_dir = args.run_dir
    meta_path = run_dir / f"meta_{args.mode}.jsonl"
    if not meta_path.is_file():
        # fall back to the distributed bundle's merged, unqualified meta file
        meta_path = run_dir / "meta.jsonl"

    if not meta_path.is_file():
        print(f"Error: meta file not found: {meta_path}", file=sys.stderr)
        sys.exit(1)

    raw_records = load_meta(meta_path)
    if not raw_records:
        print(f"Error: no records parsed from {meta_path}", file=sys.stderr)
        sys.exit(1)

    resolved_ids = load_resolved_ids(args.grade)
    rows = [instance_row(rec, resolved_ids) for rec in raw_records]

    if args.json:
        output = json.dumps(build_json(rows, detailed=args.detailed), indent=2, default=str)
        print(output)
    else:
        report = render_report(rows, detailed=args.detailed)
        print(report)

    # Always write both files to run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "cost-report.md").write_text(
        render_report(rows, detailed=args.detailed), encoding="utf-8"
    )
    (run_dir / "cost-report.json").write_text(
        json.dumps(build_json(rows, detailed=args.detailed), indent=2, default=str), encoding="utf-8"
    )
    print(f"\nWritten: {run_dir}/cost-report.md  {run_dir}/cost-report.json", file=sys.stderr)


if __name__ == "__main__":
    main()
