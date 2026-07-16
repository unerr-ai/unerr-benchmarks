"""LiteLLM per-instance cost + token telemetry for the Claude-Code SWE-bench
"open-models" arm.

Why this exists: the open-models arm routes conductor/reasoner/fast/oracle
traffic through a shared econ-litellm gateway (Fireworks BYOK) instead of a
per-tier session DB the way econ does. To get REAL per-model spend (not the
upstream catalog price) we mint a short-lived virtual key scoped to one
benchmark instance, let Claude Code's traffic run against it, then read that
key's spend logs back and group them by model/tier. The BYOK rates below are
copied verbatim from e2e/econ/econ-tier-cost.py::ECON_COST_MATRIX — the
single source of truth for these prices; keep both in sync.

Runs on the benchmark HOST (plain python3, `time.sleep` is fine) — never
inside the sandboxed instance container.

Usage (by run-benchmark.py or similar):
    vk = mint_instance_key(base, master, alias="claude-om-<instance>", metadata={...})
    ... run the instance's Claude Code session against vk ...
    cost = fetch_cost(base, master, vk, alias="claude-om-<instance>")
"""

import json
import time
import urllib.error
import urllib.request
import uuid

# Fireworks-BYOK price matrix, keyed by BARE modelID (USD per 1M tokens).
# Copied verbatim from e2e/econ/econ-tier-cost.py::ECON_COST_MATRIX (the
# source of truth) — when that matrix changes, mirror the change here.
COST_MATRIX: dict[str, dict] = {
    # conductor (legacy, pre-OL-8.B4)
    "deepseek-v4-flash": {"input": 0.14, "cachedInput": 0.03, "cacheWrite": 0.14, "output": 0.28},
    # oracle
    "glm-5.2": {"input": 1.4, "cachedInput": 0.14, "cacheWrite": 1.4, "output": 4.4},
    # reasoner (OL-8.B4+, was shared glm-5.2)
    "deepseek-v4-pro": {"input": 1.74, "cachedInput": 0.15, "cacheWrite": 1.74, "output": 3.48},
    # fast/executor (OL-8.B4+, was gpt-oss-20b self-hosted $0) — real Fireworks serverless rate
    "gpt-oss-120b": {"input": 0.15, "cachedInput": 0.01, "cacheWrite": 0.15, "output": 0.60},
    # conductor (current) + catalog models available in the gateway/registry
    "minimax-m3": {"input": 0.3, "cachedInput": 0.06, "cacheWrite": 0.3, "output": 1.2},
    "kimi-k2p7-code": {"input": 0.95, "cachedInput": 0.19, "cacheWrite": 0.95, "output": 4.0},
    # legacy self-hosted executor (pre-OL-8.B4) — kept at $0 so old-run reports
    # don't false-flag it as unpriced; it was genuinely self-hosted at $0.
    "gpt-oss-20b": {"input": 0.0, "cachedInput": 0.0, "cacheWrite": 0.0, "output": 0.0},
}

# open-models arm's model map (CLAUDE_OPEN_MODELS): sonnet=minimax-m3,
# opus=deepseek-v4-pro, haiku=gpt-oss-120b, fable=glm-5.2. Keyed by BARE model.
TIER_BY_MODEL: dict[str, str] = {
    "minimax-m3": "conductor",
    "deepseek-v4-flash": "conductor",   # legacy conductor, pre-OL-8.B4
    "deepseek-v4-pro": "reasoner",
    "gpt-oss-120b": "fast",
    "gpt-oss-20b": "fast",              # legacy self-hosted executor, pre-OL-8.B4
    "glm-5.2": "oracle",
}


def _num(x):
    return x if isinstance(x, (int, float)) else 0


def _cache_hit_rate(in_tokens, cached_in) -> float:
    """Fraction of input tokens served from cache: cached_in / (in_tokens +
    cached_in), rounded to 4 decimals. Zero-guarded — returns 0.0 when there
    are no input tokens at all."""
    denom = _num(in_tokens) + _num(cached_in)
    return round(_num(cached_in) / denom, 4) if denom else 0.0


def _http_json(url, master_key, *, method="GET", body=None, timeout=10):
    """Minimal urllib GET/POST-JSON helper wrapping the whole call in a
    catch-all so a network/parse failure returns None instead of raising."""
    try:
        headers = {"Authorization": f"Bearer {master_key}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _as_dict(val):
    """Parse val as a dict, decoding a JSON string first; returns None if
    val is neither a dict nor a JSON-encoded dict."""
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return None
    return val if isinstance(val, dict) else None


def _usage_object(row: dict) -> dict:
    """Return a spend-log row's nested usage_object — anthropic_messages
    rows carry cache-token detail at metadata.usage_object, falling back to
    response.usage; metadata/response may arrive as a dict or a JSON string.
    Returns {} if neither source yields a usable object."""
    metadata = _as_dict(row.get("metadata"))
    if isinstance(metadata, dict):
        uo = metadata.get("usage_object")
        if isinstance(uo, dict):
            return uo

    response = _as_dict(row.get("response"))
    if isinstance(response, dict):
        uo = response.get("usage")
        if isinstance(uo, dict):
            return uo

    return {}


def bare_model(model: str) -> str:
    """Strip any provider/path prefix down to the last path segment, e.g.
    'fireworks_ai/accounts/fireworks/models/minimax-m3' -> 'minimax-m3'.
    @sem domain=observability role=normalization
    """
    if not isinstance(model, str) or not model:
        return ""
    return model.rsplit("/", 1)[-1]


def tier_of(model: str) -> str:
    """Map a (possibly prefixed) model id to its open-models-arm tier via
    TIER_BY_MODEL, defaulting to 'other' for anything unmapped.
    @sem domain=observability role=normalization
    """
    return TIER_BY_MODEL.get(bare_model(model), "other")


def mint_instance_key(base_url: str, master_key: str, *, alias: str, metadata: dict,
                       max_budget: float = 50.0) -> str | None:
    """Mint a per-instance LiteLLM virtual key (unique key_alias) so its spend
    logs can be read back in isolation from every other instance sharing the
    gateway. Returns None on any gateway failure — never raises — so a mint
    hiccup degrades to "no cost data" rather than aborting the run.
    @sem domain=observability role=provisioning
    """
    try:
        unique_alias = f"{alias}-{uuid.uuid4().hex}"
        resp = _http_json(
            base_url.rstrip("/") + "/key/generate", master_key, method="POST",
            body={"key_alias": unique_alias, "metadata": metadata or {},
                  "max_budget": max_budget, "models": []},
        )
        key = resp.get("key") if isinstance(resp, dict) else None
        return key if isinstance(key, str) and key else None
    except Exception:
        return None


def summarize_rows(rows: list) -> dict:
    """Group LiteLLM /spend/logs rows by model + tier into the cost-block
    schema fetch_cost returns; pure (no network) so it's unit-testable on a
    hand-built row list. `anthropic_messages` rows don't carry a top-level
    cache-token column — `prompt_tokens` is the fresh+cached total, so the
    cached count is read from the nested metadata/response usage_object (see
    _usage_object) and subtracted out; `in_tokens` is fresh-only and disjoint
    from `cached_in`, matching e2e/econ/econ-telemetry.py's convention.
    `usd` (sum of row spend) is authoritative; `usd_recomputed` cross-checks
    it against COST_MATRIX from the fresh/cached/output split. Each level
    (top, by_model, by_tier) also carries a derived `cache_hit_rate` =
    cached_in / (in_tokens + cached_in).
    @sem domain=observability role=aggregation
    """
    rows = rows if isinstance(rows, list) else []
    by_model: dict[str, dict] = {}
    by_tier: dict[str, dict] = {}
    usd = 0.0
    usd_recomputed = 0.0
    in_tokens = cached_in = out_tokens = cache_write = total_tokens = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("model") or ""
        bare = bare_model(model)
        tier = tier_of(model)
        spend = float(_num(row.get("spend")))

        usage_object = _usage_object(row)
        prompt_tokens_details = usage_object.get("prompt_tokens_details")
        row_cached = int(_num(
            prompt_tokens_details.get("cached_tokens", 0)
            if isinstance(prompt_tokens_details, dict) else 0
        ))
        row_prompt = int(_num(row.get("prompt_tokens")))
        row_in = max(0, row_prompt - row_cached)  # fresh (uncached) input only
        row_out = int(_num(row.get("completion_tokens")))
        row_cache_write = int(_num(usage_object.get("cache_creation_input_tokens", 0)))
        row_total = _num(row.get("total_tokens"))

        usd += spend
        in_tokens += row_in
        cached_in += row_cached
        out_tokens += row_out
        cache_write += row_cache_write
        total_tokens += row_total

        rate = COST_MATRIX.get(bare)
        if rate:
            usd_recomputed += (
                row_in * rate["input"]
                + row_cached * rate["cachedInput"]
                + row_cache_write * rate["cacheWrite"]
                + row_out * rate["output"]
            ) / 1_000_000

        m = by_model.setdefault(bare, dict(
            tier=tier, usd=0.0, in_tokens=0, cached_in=0, out_tokens=0,
            cache_write=0, requests=0,
        ))
        m["usd"] += spend
        m["in_tokens"] += row_in
        m["cached_in"] += row_cached
        m["out_tokens"] += row_out
        m["cache_write"] += row_cache_write
        m["requests"] += 1

        t = by_tier.setdefault(tier, dict(
            usd=0.0, in_tokens=0, out_tokens=0, cached_in=0, requests=0, models=set(),
        ))
        t["usd"] += spend
        t["in_tokens"] += row_in
        t["out_tokens"] += row_out
        t["cached_in"] += row_cached
        t["requests"] += 1
        t["models"].add(bare)

    for t in by_tier.values():
        t["models"] = sorted(t["models"])

    return {
        "source": "litellm_spend_logs",
        "vk_alias": "",
        "requests": len(rows),
        "usd": round(usd, 6),
        "usd_recomputed": round(usd_recomputed, 6),
        "in_tokens": in_tokens,
        "cached_in": cached_in,
        "out_tokens": out_tokens,
        "cache_write": cache_write,
        "total_tokens": total_tokens,
        "cache_hit_rate": _cache_hit_rate(in_tokens, cached_in),
        "by_model": {
            k: {**v, "usd": round(v["usd"], 6),
                "cache_hit_rate": _cache_hit_rate(v["in_tokens"], v["cached_in"])}
            for k, v in by_model.items()
        },
        "by_tier": {
            k: {**v, "usd": round(v["usd"], 6),
                "cache_hit_rate": _cache_hit_rate(v["in_tokens"], v["cached_in"])}
            for k, v in by_tier.items()
        },
    }


def fetch_cost(base_url: str, master_key: str, vk: str, *, alias: str = "",
                settle_timeout: float = 25.0) -> dict:
    """Poll a virtual key's /spend/logs until row count + spend are stable
    against /key/info (LiteLLM's spend logging is async/batched), then
    summarize_rows() them. On any gateway failure — including the gateway
    staying unreachable for the whole settle_timeout — returns the
    'unavailable' stub instead of raising, so a telemetry hiccup never
    aborts the benchmark run.
    @sem domain=observability role=aggregation
    """
    try:
        base = base_url.rstrip("/")
        start = time.time()
        prev_count = None
        rows: list = []
        ever_ok = False
        while True:
            fetched = _http_json(f"{base}/spend/logs?api_key={vk}", master_key)
            if isinstance(fetched, list):
                rows = fetched
                ever_ok = True
            info = _http_json(f"{base}/key/info?key={vk}", master_key)
            keyinfo_spend = None
            if isinstance(info, dict) and isinstance(info.get("info"), dict):
                ki = info["info"].get("spend")
                if isinstance(ki, (int, float)):
                    keyinfo_spend = float(ki)

            row_count = len(rows)
            unchanged = prev_count is not None and row_count == prev_count
            prev_count = row_count
            sum_spend = sum(float(_num(r.get("spend"))) for r in rows if isinstance(r, dict))
            spend_matches = (
                keyinfo_spend is None
                or abs(sum_spend - keyinfo_spend) <= max(1e-6, 0.02 * keyinfo_spend)
            )

            if (unchanged and spend_matches) or (time.time() - start) >= settle_timeout:
                break
            time.sleep(2.5)

        if not ever_ok:
            return {"source": "unavailable", "error": "spend/logs unreachable within settle_timeout",
                     "vk_alias": alias}

        result = summarize_rows(rows)
        result["vk_alias"] = alias
        return result
    except Exception as e:
        return {"source": "unavailable", "error": str(e), "vk_alias": alias}
