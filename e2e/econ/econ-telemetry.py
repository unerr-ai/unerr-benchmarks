#!/usr/bin/env python3
"""Parse an econ (`opencode run --format json`) NDJSON event stream into one
per-run telemetry object — the cost/turn/token/tool/PER-TIER signal for the
econ benchmark's single arm.

econ is the team's OpenCode-fork coding agent with unerr compiled in-process
(packages/code-intelligence). It routes each turn across model TIERS
(conductor / oracle / executor), so a single overall cost hides the story: the
savings live in the per-tier split (cheap conductor carries the bulk; the
expensive oracle is a rare tail; the self-hosted executor is ~free). econ emits
that split itself.

Wire facts this parser depends on (verified against econ source
packages/opencode/src/cli/cmd/run.ts + packages/code-intelligence/src/econ-cost.ts):

  * Every emitted line is  {"type", "timestamp", "sessionID", ...data}.

  * COST — econ prices tokens on its OWN Fireworks-BYOK matrix, NOT the upstream
    models.dev figure. The upstream figure rides on `part.cost`; econ's real
    figure rides on `econCost` (per step) and the terminal `cost_breakdown`
    event. So:
      - `cost_breakdown` (emitted once at idle) is the source of truth:
          {"type":"cost_breakdown","sessionID":"…",
           "cumulative":{"tokens":{input,output,reasoning,cache:{read,write}},"cost":<econ usd>},
           "models":{"<modelID>":{"label":"…","tokens":{…},"cost":<econ usd>}, …}}
        cumulative.cost == Σ per-model econ cost (our real cost).
      - each `step_finish` also carries top-level `modelID`, `providerID`,
        `econCost` (econ usd for that step) alongside `part` (whose `part.cost`
        is the upstream models.dev price + `part.tokens.*`).

  * TOOLS — a `tool_use` line (completed/errored) names the tool on `part.tool`.
    GRAPH_TOOLS below are the embedded code-intelligence tools; counting them
    confirms unerr's in-process context tools fired.

Primary cost = econ's cost_breakdown.cumulative.cost (falls back to Σ econCost,
then Σ upstream part.cost). `usd_upstream` (Σ part.cost) is always reported too,
so the BYOK-vs-catalog gap is visible.

Usage:
    econ-telemetry.py <events.jsonl>     # or "-" / no arg to read stdin

Emits ONE JSON object to stdout (fields default to 0 when absent):
    turns, in_tokens, cached_in, out_tokens, reasoning_tokens, cache_write,
    usd, usd_upstream, usd_source, tool_calls, graph_tool_calls, tools{},
    by_model{modelID:{label,tier,usd,in_tokens,cached_in,out_tokens,
                       reasoning_tokens,cache_write}},
    by_tier{tier:{usd,in_tokens,cached_in,out_tokens,reasoning_tokens,
                  cache_write,models[]}}
"""

import json
import sys

# Embedded code-intelligence / graph-backed tool ids (packages/opencode/src/
# tool/graph-tools.ts + read.ts). Calls to these are unerr's in-process context
# work — tallied separately from plain shell/edit tools (bash, edit, write, ...).
GRAPH_TOOLS = frozenset({"recon", "get_references", "search", "file_outline", "read"})

# Bare modelID -> econ tier name (packages/code-intelligence/src/econ-cost.ts:
# ECON_COST_MATRIX + LABELS). A "litellm/" provider prefix is stripped first.
TIER_BY_MODEL = {
    "deepseek/deepseek-v4-flash": "conductor",
    "z-ai/glm-5.2": "oracle",
    "openai/gpt-oss-20b": "executor",
}
_LITELLM_PREFIX = "litellm/"


def _num(x):
    """Coerce a possibly-missing/None numeric field to a plain number."""
    return x if isinstance(x, (int, float)) else 0


def _bare_model(model_id):
    if isinstance(model_id, str) and model_id.startswith(_LITELLM_PREFIX):
        return model_id[len(_LITELLM_PREFIX):]
    return model_id if isinstance(model_id, str) else ""


def _tier_of(model_id, label=""):
    """conductor/oracle/executor from modelID; fall back to a tier name parsed
    from econ's "(conductor)"-style label; else 'other'."""
    tier = TIER_BY_MODEL.get(_bare_model(model_id))
    if tier:
        return tier
    if isinstance(label, str):
        for t in ("conductor", "oracle", "reasoner", "executor"):
            if "(" + t in label or "(" + t + " " in label:
                return "oracle" if t == "reasoner" else t
    return "other"


def _tokens_from(tok):
    """Normalise a tokens object {input,output,reasoning,cache:{read,write}}."""
    if not isinstance(tok, dict):
        return dict(input=0, cached_in=0, out=0, reasoning=0, cache_write=0)
    cache = tok.get("cache") or {}
    if not isinstance(cache, dict):
        cache = {}
    return dict(
        input=_num(tok.get("input")),
        cached_in=_num(cache.get("read")),
        out=_num(tok.get("output")),
        reasoning=_num(tok.get("reasoning")),
        cache_write=_num(cache.get("write")),
    )


def _empty_model_row(label, tier):
    return dict(
        label=label, tier=tier, usd=0.0,
        in_tokens=0, cached_in=0, out_tokens=0, reasoning_tokens=0, cache_write=0,
    )


def parse(lines):
    turns = 0
    tool_calls = graph_tool_calls = 0
    tools: dict[str, int] = {}

    # upstream (models.dev) and econ (BYOK) per-step accumulators — fallbacks
    # used only when no terminal cost_breakdown event is present.
    upstream_usd = 0.0
    step_econ_usd = 0.0
    step_tok = dict(input=0, cached_in=0, out=0, reasoning=0, cache_write=0)
    step_by_model: dict[str, dict] = {}
    saw_econ_step = False

    breakdown = None  # the authoritative terminal cost_breakdown event

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue

        etype = ev.get("type")

        if etype == "cost_breakdown":
            breakdown = ev  # last one wins (there is exactly one per run)
            continue

        if etype == "step_finish":
            turns += 1
            part = ev.get("part") or {}
            if not isinstance(part, dict):
                part = {}
            upstream_usd += _num(part.get("cost"))
            t = _tokens_from(part.get("tokens"))
            for k in step_tok:
                step_tok[k] += t[k]
            # econ's per-step figure + model attribution (enriched on the event)
            if "econCost" in ev or ev.get("modelID"):
                saw_econ_step = True
                ec = _num(ev.get("econCost"))
                step_econ_usd += ec
                mid = ev.get("modelID") or "<unknown>"
                row = step_by_model.get(mid)
                if row is None:
                    row = _empty_model_row(mid, _tier_of(mid))
                    step_by_model[mid] = row
                row["usd"] += ec
                row["in_tokens"] += t["input"]
                row["cached_in"] += t["cached_in"]
                row["out_tokens"] += t["out"]
                row["reasoning_tokens"] += t["reasoning"]
                row["cache_write"] += t["cache_write"]
            continue

        if etype == "tool_use":
            part = ev.get("part") or {}
            name = part.get("tool") if isinstance(part, dict) else None
            if not isinstance(name, str) or not name:
                name = "<unknown>"
            tools[name] = tools.get(name, 0) + 1
            tool_calls += 1
            if name in GRAPH_TOOLS:
                graph_tool_calls += 1

    # ── choose the authoritative cost/token/per-model source ────────────────
    by_model: dict[str, dict] = {}
    if breakdown is not None:
        usd_source = "cost_breakdown"
        cum = breakdown.get("cumulative") or {}
        ct = _tokens_from(cum.get("tokens"))
        in_tokens, cached_in = ct["input"], ct["cached_in"]
        out_tokens, reasoning_tokens, cache_write = ct["out"], ct["reasoning"], ct["cache_write"]
        usd = _num(cum.get("cost"))
        models = breakdown.get("models") or {}
        if isinstance(models, dict):
            for mid, m in models.items():
                if not isinstance(m, dict):
                    continue
                label = m.get("label") if isinstance(m.get("label"), str) else mid
                mt = _tokens_from(m.get("tokens"))
                by_model[mid] = dict(
                    label=label, tier=_tier_of(mid, label), usd=_num(m.get("cost")),
                    in_tokens=mt["input"], cached_in=mt["cached_in"], out_tokens=mt["out"],
                    reasoning_tokens=mt["reasoning"], cache_write=mt["cache_write"],
                )
    else:
        # no terminal event — roll up from step_finish
        in_tokens, cached_in = step_tok["input"], step_tok["cached_in"]
        out_tokens, reasoning_tokens, cache_write = step_tok["out"], step_tok["reasoning"], step_tok["cache_write"]
        if saw_econ_step:
            usd_source = "econ_step_sum"
            usd = round(step_econ_usd, 6)
            by_model = step_by_model
        else:
            usd_source = "upstream_fallback"  # old binary: no econ figure at all
            usd = round(upstream_usd, 6)

    # ── aggregate per-model -> per-tier ─────────────────────────────────────
    by_tier: dict[str, dict] = {}
    for mid, row in by_model.items():
        tier = row.get("tier", "other")
        agg = by_tier.get(tier)
        if agg is None:
            agg = dict(usd=0.0, in_tokens=0, cached_in=0, out_tokens=0,
                       reasoning_tokens=0, cache_write=0, models=[])
            by_tier[tier] = agg
        agg["usd"] += _num(row.get("usd"))
        for k in ("in_tokens", "cached_in", "out_tokens", "reasoning_tokens", "cache_write"):
            agg[k] += _num(row.get(k))
        agg["models"].append(mid)
    for agg in by_tier.values():
        agg["usd"] = round(agg["usd"], 6)

    return {
        "turns": turns,
        "in_tokens": in_tokens,
        "cached_in": cached_in,
        "out_tokens": out_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_write": cache_write,
        "usd": round(usd, 6),
        "usd_upstream": round(upstream_usd, 6),
        "usd_source": usd_source,
        "tool_calls": tool_calls,
        "graph_tool_calls": graph_tool_calls,
        "tools": tools,
        "by_model": by_model,
        "by_tier": by_tier,
    }


def main(argv):
    path = argv[1] if len(argv) > 1 else "-"
    if path == "-":
        out = parse(sys.stdin)
    else:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                out = parse(fh)
        except FileNotFoundError:
            # Empty/absent event file -> zero telemetry (still valid JSON so the
            # caller's meta record is well-formed and the run isn't lost).
            out = parse([])
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
