#!/usr/bin/env python3
"""
report-runs.py — Post-run cost/efficiency comparison for codex ±unerr local-docker runs.

CLI:
    python3 report-runs.py <run_dir> [<run_dir2> ...]
    # If no dirs given, globs results/*/ that contain a meta_*.jsonl.

Each run_dir is a results/<label>/ directory containing meta_on.jsonl
and/or meta_off.jsonl (either may be absent for partial runs).

Line format of meta_<mode>.jsonl:
    {"instance_id", "mode", "model", "wall_s", "rc", "patch_bytes",
     "unerrd_up", "install_ok",
     "telemetry": {"turns","in_tokens","cached_in","out_tokens","usd",
                   "tool_calls","mcp_tool_calls","tools":{...}},
     "artifacts_dir": "artifacts/<mode>/<iid>"}
telemetry may be {} if the run failed; missing numeric fields → 0.

Writes:
    <first_run_dir>/cost-report.md   — also printed to stdout
    <first_run_dir>/cost-report.json — structured JSON of summaries + deltas
"""

import argparse
import json
import pathlib
import statistics
import sys
from collections import defaultdict


# ── Pricing ───────────────────────────────────────────────────────────────────
# USD per 1M tokens. Raw token counts in telemetry are EXACT; only $ is rate-
# dependent, so we recompute $ here (host-side) instead of trusting the in-
# container `usd` (which is hardcoded to gpt-5.4-mini). Edit a row to correct it.
# Rates from published OpenAI/morphllm June-2026 lists.
# Cached-input is assumed at 1/10 of input where not separately published.
# The ±unerr token deltas are rate-independent.
PRICING = {
    # model            : (input, cached_input, output)   $/1M
    "gpt-5.4-mini":      (0.25,  0.025,  2.00),
    "gpt-5.4-nano":      (0.05,  0.005,  0.40),
    "gpt-5.4":           (1.25,  0.125, 15.00),
    "gpt-5.3-codex":     (1.75,  0.175, 14.00),
    "gpt-5.2-codex":     (1.25,  0.125, 10.00),
    "gpt-5-codex":       (1.25,  0.125, 10.00),
    "gpt-5.5":           (1.25,  0.125, 30.00),
    "gpt-5.5-pro":      (15.00,  1.500,120.00),
}
_DEFAULT_PRICE = (1.25, 0.125, 10.00)  # unknown model → full-tier estimate


def price_usd(model, in_tokens, cached_in, out_tokens):
    """Recompute run cost from raw tokens at PRICING rates. cached_in is billed
    at the cached rate; only (in_tokens - cached_in) is billed at full input."""
    pin, pcached, pout = PRICING.get(model, _DEFAULT_PRICE)
    uncached = max(0, in_tokens - cached_in)
    return (uncached * pin + cached_in * pcached + out_tokens * pout) / 1e6


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _num(val, default=0):
    """Coerce val to the type of default; return default on failure/None."""
    if val is None:
        return default
    try:
        return type(default)(val)
    except (TypeError, ValueError):
        return default


# ── Loading ───────────────────────────────────────────────────────────────────

def load_meta_file(path):
    """
    Parse a meta JSONL file.
    Returns (records, no_tel_count).
    Malformed lines are skipped with a stderr warning.
    """
    records, no_tel_count = [], 0
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

                tel = rec.get("telemetry") or {}
                has_tel = (
                    isinstance(tel, dict)
                    and bool(tel)
                    and any(k in tel for k in
                            ("turns", "in_tokens", "out_tokens", "usd", "tool_calls"))
                )
                rec["_no_tel"] = not has_tel
                if not has_tel:
                    no_tel_count += 1
                records.append(rec)
    except OSError as exc:
        print(f"Warning: cannot open {path}: {exc}", file=sys.stderr)
    return records, no_tel_count


def load_all(run_dirs):
    """
    Load every meta_on/off.jsonl across run_dirs.
    Returns all_records: dict[(label, mode, model)] → [rec, …]
    """
    all_records = defaultdict(list)
    for run_dir in run_dirs:
        run_dir = pathlib.Path(run_dir)
        label = run_dir.name
        for mode in ("on", "off"):
            fpath = run_dir / f"meta_{mode}.jsonl"
            if not fpath.exists():
                continue
            recs, _ = load_meta_file(fpath)
            for rec in recs:
                model = rec.get("model") or "unknown"
                all_records[(label, mode, model)].append(rec)
    return all_records


# ── Statistics ────────────────────────────────────────────────────────────────

def summarise(records):
    """Return a stats dict for a list of records sharing (label, mode, model)."""
    n = len(records)
    if n == 0:
        return None

    no_tel = sum(1 for r in records if r.get("_no_tel"))

    def tel(r):
        t = r.get("telemetry")
        return t if isinstance(t, dict) else {}

    turns          = [_num(tel(r).get("turns"),          0)   for r in records]
    in_tokens      = [_num(tel(r).get("in_tokens"),      0)   for r in records]
    cached_in      = [_num(tel(r).get("cached_in"),      0)   for r in records]
    out_tokens     = [_num(tel(r).get("out_tokens"),     0)   for r in records]
    # Recompute $ from raw tokens at PRICING rates (authoritative, model-aware) —
    # the in-container telemetry.usd is hardcoded to gpt-5.4-mini and is ignored.
    usd            = [price_usd(r.get("model"),
                                _num(tel(r).get("in_tokens"),  0),
                                _num(tel(r).get("cached_in"),  0),
                                _num(tel(r).get("out_tokens"), 0)) for r in records]
    tool_calls     = [_num(tel(r).get("tool_calls"),     0)   for r in records]
    mcp_tool_calls = [_num(tel(r).get("mcp_tool_calls"), 0)   for r in records]

    unerrd_up     = [1 if r.get("unerrd_up")  else 0 for r in records]
    install_ok    = [1 if r.get("install_ok") else 0 for r in records]
    patch_nonzero = [1 if _num(r.get("patch_bytes"), 0) > 0 else 0 for r in records]

    total_usd    = sum(usd)
    total_in     = sum(in_tokens)
    total_cached = sum(cached_in)
    total_out    = sum(out_tokens)

    return dict(
        n                   = n,
        no_tel_count        = no_tel,
        mean_turns          = statistics.mean(turns),
        total_in_tokens     = total_in,
        mean_in_tokens      = statistics.mean(in_tokens),
        total_cached_in     = total_cached,
        mean_cached_in      = statistics.mean(cached_in),
        total_out_tokens    = total_out,
        mean_out_tokens     = statistics.mean(out_tokens),
        total_usd           = total_usd,
        mean_usd            = total_usd / n,
        mean_tool_calls     = statistics.mean(tool_calls),
        mean_mcp_tool_calls = statistics.mean(mcp_tool_calls),
        unerrd_up_rate      = sum(unerrd_up) / n,
        install_ok_rate     = sum(install_ok) / n,
        patch_nonzero_rate  = sum(patch_nonzero) / n,
    )


def all_summaries(all_records):
    return {key: summarise(recs) for key, recs in all_records.items()}


def compute_deltas(summaries):
    """
    For every (label, model) pair where at least one arm exists,
    compute delta metrics (on − off), operating on per-instance means.
    """
    pairs = sorted({(lbl, mdl) for (lbl, _mode, mdl) in summaries})
    deltas = {}
    for label, model in pairs:
        s_on  = summaries.get((label, "on",  model))
        s_off = summaries.get((label, "off", model))
        if s_on is None and s_off is None:
            continue

        def v(s, k):
            return s[k] if s else 0.0

        def dpct(a, b):
            return ((a - b) / abs(b) * 100) if b else None

        mt_on,  mt_off = v(s_on, "mean_turns"),     v(s_off, "mean_turns")
        mi_on,  mi_off = v(s_on, "mean_in_tokens"), v(s_off, "mean_in_tokens")
        mu_on,  mu_off = v(s_on, "mean_usd"),        v(s_off, "mean_usd")

        deltas[(label, model)] = dict(
            label               = label,
            model               = model,
            n_on                = s_on["n"] if s_on else 0,
            n_off               = s_off["n"] if s_off else 0,
            delta_turns         = mt_on - mt_off,
            delta_turns_pct     = dpct(mt_on, mt_off),
            delta_in_tokens     = mi_on - mi_off,
            delta_in_tokens_pct = dpct(mi_on, mi_off),
            delta_usd           = mu_on - mu_off,
            delta_usd_pct       = dpct(mu_on, mu_off),
            mcp_calls_on        = v(s_on, "mean_mcp_tool_calls"),
            unerrd_up_rate_on   = v(s_on, "unerrd_up_rate"),
        )
    return deltas


# ── Markdown rendering ────────────────────────────────────────────────────────

def _pct(v):
    return f"{v * 100:.1f}%"

def _usd(v):
    return f"${v:.4f}"

def _f(v, d=2):
    return f"{v:.{d}f}"

def _dpct(v):
    return "N/A" if v is None else f"{v:+.1f}%"


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


def sec_summary(summaries):
    hdrs = [
        "Label", "Mode", "Model", "N", "No-Tel",
        "Mean Turns",
        "Total In Tok", "Mean In Tok",
        "Total Cached", "Mean Cached",
        "Total Out Tok", "Mean Out Tok",
        "Total $", "Mean $/inst",
        "Mean Tools", "Mean MCP",
        "unerrd_up%", "install_ok%", "patch>0%",
    ]
    rows = []
    for (lbl, mode, mdl), s in sorted(summaries.items()):
        if s is None:
            continue
        rows.append([
            lbl, mode, mdl,
            s["n"], s["no_tel_count"],
            _f(s["mean_turns"]),
            s["total_in_tokens"],      _f(s["mean_in_tokens"], 0),
            s["total_cached_in"],      _f(s["mean_cached_in"], 0),
            s["total_out_tokens"],     _f(s["mean_out_tokens"], 0),
            _usd(s["total_usd"]),      _usd(s["mean_usd"]),
            _f(s["mean_tool_calls"]),  _f(s["mean_mcp_tool_calls"]),
            _pct(s["unerrd_up_rate"]), _pct(s["install_ok_rate"]),
            _pct(s["patch_nonzero_rate"]),
        ])
    body = md_table(hdrs, rows) if rows else "_No data found._"
    return f"## 1. Per-Run Summary\n\n{body}\n"


def sec_deltas(deltas):
    hdrs = [
        "Label", "Model", "N(on)", "N(off)",
        "Δ Turns", "Δ Turns%",
        "Δ Mean In Tok", "Δ In Tok%",
        "Δ Mean $/inst", "Δ $%",
        "MCP (on)", "unerrd_up% (on)",
    ]
    rows = []
    for (lbl, mdl), d in sorted(deltas.items()):
        rows.append([
            lbl, mdl,
            d["n_on"], d["n_off"],
            f"{d['delta_turns']:+.2f}",     _dpct(d["delta_turns_pct"]),
            f"{d['delta_in_tokens']:+.0f}",  _dpct(d["delta_in_tokens_pct"]),
            f"${d['delta_usd']:+.4f}",       _dpct(d["delta_usd_pct"]),
            _f(d["mcp_calls_on"]),           _pct(d["unerrd_up_rate_on"]),
        ])
    body = md_table(hdrs, rows) if rows else "_No paired on/off data found._"
    return f"## 2. ±unerr Delta (on vs off)\n\n{body}\n"


def sec_cross_model(summaries):
    entries = [
        (lbl, mdl, s)
        for (lbl, mode, mdl), s in sorted(summaries.items())
        if mode == "on" and s is not None
    ]
    title = "## 3. Cross-Model / Cross-Label Comparison (on-arm)\n\n"
    if len(entries) <= 1:
        return title + "_Only one label/model present; cross-comparison not applicable._\n"
    hdrs = ["Label", "Model", "N", "Mean $/inst", "Mean Turns", "Mean MCP"]
    rows = [
        [lbl, mdl, s["n"], _usd(s["mean_usd"]),
         _f(s["mean_turns"]), _f(s["mean_mcp_tool_calls"])]
        for lbl, mdl, s in entries
    ]
    return title + md_table(hdrs, rows) + "\n"


def sec_verdicts(summaries):
    labels = sorted({lbl for (lbl, _, _) in summaries})
    lines = []
    for lbl in labels:
        on_ents = sorted(
            (mdl, s)
            for (l, mode, mdl), s in summaries.items()
            if l == lbl and mode == "on" and s is not None
        )
        if not on_ents:
            lines.append(f"- **{lbl}**: FLAG — no on-arm data")
            continue
        for mdl, s in on_ents:
            mcp_ok   = s["mean_mcp_tool_calls"] > 0
            unerr_ok = s["unerrd_up_rate"] >= 1.0
            if mcp_ok and unerr_ok:
                tag    = "PASS"
                detail = (
                    f"mean mcp_tool_calls={s['mean_mcp_tool_calls']:.2f}, "
                    f"unerrd_up=100%"
                )
            else:
                tag = "FLAG"
                issues = []
                if not mcp_ok:
                    issues.append(
                        f"mean mcp_tool_calls={s['mean_mcp_tool_calls']:.2f} (want >0)"
                    )
                if not unerr_ok:
                    issues.append(
                        f"unerrd_up={_pct(s['unerrd_up_rate'])} (want 100%)"
                    )
                detail = "; ".join(issues)
            lines.append(f"- **{lbl} / {mdl}**: {tag} — {detail}")
    body = "\n".join(lines) if lines else "_No on-arm data._"
    return f"## 4. unerr Fired? Verdict\n\n{body}\n"


def render_report(summaries, deltas):
    return "\n".join([
        "# Codex ±unerr Cost/Efficiency Report",
        "",
        sec_summary(summaries),
        sec_deltas(deltas),
        sec_cross_model(summaries),
        sec_verdicts(summaries),
    ])


# ── JSON output ───────────────────────────────────────────────────────────────

def build_json(summaries, deltas):
    return {
        "summaries": {
            f"{lbl}|{mode}|{mdl}": s
            for (lbl, mode, mdl), s in summaries.items()
            if s is not None
        },
        "deltas": {
            f"{lbl}|{mdl}": d
            for (lbl, mdl), d in deltas.items()
        },
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def find_default_run_dirs():
    base = pathlib.Path("results")
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.iterdir()
        if p.is_dir() and any(p.glob("meta_*.jsonl"))
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "run_dirs", nargs="*", type=pathlib.Path, metavar="RUN_DIR",
        help="results/<label>/ dirs (default: auto-glob results/*/)",
    )
    args = ap.parse_args(argv)

    run_dirs = args.run_dirs or find_default_run_dirs()
    if not run_dirs:
        ap.error(
            "No run dirs found. Pass explicit paths or run from the repo root "
            "where results/ lives."
        )

    valid = [pathlib.Path(d) for d in run_dirs if pathlib.Path(d).is_dir()]
    if not valid:
        ap.error("None of the supplied paths are directories.")

    all_records = load_all(valid)
    sums        = all_summaries(all_records)
    deltas      = compute_deltas(sums)
    report      = render_report(sums, deltas)

    print(report)

    first = valid[0]
    (first / "cost-report.md").write_text(report, encoding="utf-8")
    (first / "cost-report.json").write_text(
        json.dumps(build_json(sums, deltas), indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"\nWritten: {first}/cost-report.md  {first}/cost-report.json",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
