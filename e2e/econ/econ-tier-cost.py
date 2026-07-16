#!/usr/bin/env python3
"""Read econ's session SQLite (`opencode.db`) and produce the COMPLETE per-tier
cost + token breakdown for one run — including the executor tier, which the
`--format json` stdout stream cannot see.

Why this exists (verified against econ source):
  econ routes a task across tiers. conductor / oracle / reasoner are `primary`
  agents (they run in the top-level session, so the run's stdout `cost_breakdown`
  captures them). The **executor is a `subagent`** (opencode.json) — it runs in a
  CHILD session, and the parent's `--format json` loop filters every part whose
  `sessionID` isn't the top-level one (packages/opencode/src/cli/cmd/run.ts). So
  the stream misses the executor's TOKEN VOLUME entirely (its cost is $0, self-
  hosted, so the run TOTAL is still right — but the volume is invisible). This
  reader walks the session tree in the DB to recover it.

Data model (packages/core/src/session/sql.ts, packages/core/src/v1/session.ts):
  * `session`  : id (PK), parent_id, agent, cost, tokens_* columns.
  * `message`  : id (PK), session_id (FK), data (JSON text). An assistant
                 message's `data` carries role="assistant", `agent` (the TIER
                 name — conductor/oracle/reasoner/executor/small_model), modelID,
                 providerID, cost (UPSTREAM models.dev price), and
                 tokens{input,output,reasoning,cache{read,write}}.

Cost basis: the DB's stored `data.cost` is the UPSTREAM catalog price (wrong for
econ's Fireworks BYOK path). So per tier we recompute econ's real cost by
applying ECON_COST_MATRIX to the DB token counts (identical to
packages/code-intelligence/src/econ-cost.ts::econStepCost), and keep the stored
figure as `usd_upstream`. Tiers are labelled by `data.agent` — the ONLY field
that distinguishes oracle vs reasoner (shared glm-5.2) and executor vs
small_model (shared gpt-oss-20b).

Usage:
    econ-tier-cost.py --db <opencode.db> [--session <sessionID>]

  --session restricts to that session + all descendants (parent_id tree). Omit
  it when OPENCODE_DB was pinned fresh per instance (the DB then holds only this
  run, so aggregating every session is correct).

Emits ONE JSON object to stdout (all-zero, source="sqlite", on any failure):
    {source, db, session_id, sessions, usd, usd_upstream, in_tokens, cached_in,
     out_tokens, reasoning_tokens, cache_write, messages,
     by_tier{tier:{usd,usd_upstream,in_tokens,cached_in,out_tokens,
                   reasoning_tokens,cache_write,messages,models[]}},
     by_model{modelID:{tier,usd,usd_upstream,...,messages}}}
"""

import argparse
import json
import sqlite3
import sys

# econ's Fireworks-BYOK price matrix, keyed by BARE modelID (USD per 1M tokens).
# MUST mirror packages/code-intelligence/src/econ-cost.ts::ECON_COST_MATRIX (the
# source of truth) in the econ-coding-agent repo. When econ rebinds a tier to a
# new model, add it here in the same change — otherwise that tier's per-tier USD
# silently reads $0 (exactly the drift that hid the minimax-m3 conductor cost).
# The `unpriced_models` surfacing in build() turns any future gap into a visible
# flag instead of a silent zero.
ECON_COST_MATRIX = {
    # conductor (legacy, pre-OL-8.B4)
    "deepseek/deepseek-v4-flash": {"input": 0.14, "cachedInput": 0.03, "cacheWrite": 0.14, "output": 0.28},
    # oracle
    "z-ai/glm-5.2": {"input": 1.4, "cachedInput": 0.14, "cacheWrite": 1.4, "output": 4.4},
    # reasoner (OL-8.B4+, was shared glm-5.2)
    "deepseek/deepseek-v4-pro": {"input": 1.74, "cachedInput": 0.15, "cacheWrite": 1.74, "output": 3.48},
    # executor (OL-8.B4+, was gpt-oss-20b self-hosted $0) — real Fireworks serverless rate
    "openai/gpt-oss-120b": {"input": 0.15, "cachedInput": 0.01, "cacheWrite": 0.15, "output": 0.60},
    # conductor (current) + catalog models available in the gateway/registry
    "minimax/minimax-m3": {"input": 0.3, "cachedInput": 0.06, "cacheWrite": 0.3, "output": 1.2},
    "moonshotai/kimi-k2p7-code": {"input": 0.95, "cachedInput": 0.19, "cacheWrite": 0.95, "output": 4.0},
    # legacy self-hosted executor (pre-OL-8.B4) — kept at $0 so old-run reports
    # don't false-flag it as unpriced; it was genuinely self-hosted at $0.
    "openai/gpt-oss-20b": {"input": 0.0, "cachedInput": 0.0, "cacheWrite": 0.0, "output": 0.0},
}
_LITELLM_PREFIX = "litellm/"


def _num(x):
    return x if isinstance(x, (int, float)) else 0


def _bare_model(model_id):
    if isinstance(model_id, str) and model_id.startswith(_LITELLM_PREFIX):
        return model_id[len(_LITELLM_PREFIX):]
    return model_id if isinstance(model_id, str) else ""


def is_priced(model_id):
    """True iff this model has a rate in ECON_COST_MATRIX (so its USD is real,
    not a silent $0 from an unknown-model fallthrough)."""
    return _bare_model(model_id) in ECON_COST_MATRIX


def econ_step_cost(model_id, tok):
    """econ BYOK USD for one message's tokens — mirrors
    packages/code-intelligence/src/econ-cost.ts::econStepCost (cache WRITE billed
    at its own cacheWrite rate; reasoning billed at the output rate). Returns 0.0
    for a model absent from the matrix — callers must use is_priced() to tell a
    real $0 from an unpriced-model $0."""
    rate = ECON_COST_MATRIX.get(_bare_model(model_id))
    if not rate:
        return 0.0
    cache = tok.get("cache") or {}
    return (
        _num(tok.get("input")) * rate["input"]
        + _num(cache.get("read")) * rate["cachedInput"]
        + _num(cache.get("write")) * rate.get("cacheWrite", rate["input"])
        + _num(tok.get("output")) * rate["output"]
        + _num(tok.get("reasoning")) * rate["output"]
    ) / 1_000_000


def _empty_tier():
    return dict(usd=0.0, usd_upstream=0.0, in_tokens=0, cached_in=0, out_tokens=0,
               reasoning_tokens=0, cache_write=0, messages=0, priced=True, models=set())


def _zero_result(db, session_id, error=None):
    out = dict(
        source="sqlite", db=db, session_id=session_id, sessions=0,
        usd=0.0, usd_upstream=0.0, in_tokens=0, cached_in=0, out_tokens=0,
        reasoning_tokens=0, cache_write=0, messages=0,
        unpriced_models=[], all_priced=True, by_tier={}, by_model={},
    )
    if error:
        out["error"] = error
    return out


def _fetch_message_data(con, session_id):
    """Return (list of message `data` JSON strings, session_count)."""
    cur = con.cursor()
    if session_id:
        # parent + all descendants via recursive parent_id walk.
        rows = cur.execute(
            """
            WITH RECURSIVE run_sessions(id) AS (
              SELECT id FROM session WHERE id = ?
              UNION ALL
              SELECT s.id FROM session s JOIN run_sessions r ON s.parent_id = r.id
            )
            SELECT m.data FROM message m
            WHERE m.session_id IN (SELECT id FROM run_sessions)
            """,
            (session_id,),
        ).fetchall()
        sessions = cur.execute(
            """
            WITH RECURSIVE run_sessions(id) AS (
              SELECT id FROM session WHERE id = ?
              UNION ALL
              SELECT s.id FROM session s JOIN run_sessions r ON s.parent_id = r.id
            )
            SELECT COUNT(*) FROM run_sessions
            """,
            (session_id,),
        ).fetchone()[0]
    else:
        rows = cur.execute("SELECT data FROM message").fetchall()
        sessions = cur.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    return [r[0] for r in rows], sessions


def build(db, session_id):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    except sqlite3.Error as e:
        return _zero_result(db, session_id, "open failed: %s" % e)
    try:
        try:
            datas, sessions = _fetch_message_data(con, session_id)
        except sqlite3.Error as e:
            return _zero_result(db, session_id, "query failed: %s" % e)
    finally:
        con.close()

    by_tier: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    totals = _empty_tier()
    unpriced: dict[str, int] = {}  # model_id -> token volume for models absent from the matrix

    for raw in datas:
        try:
            d = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("role") != "assistant":
            continue
        tok = d.get("tokens") or {}
        if not isinstance(tok, dict):
            tok = {}
        cache = tok.get("cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        model_id = d.get("modelID") or "<unknown>"
        tier = d.get("agent") or "<unknown>"
        econ_usd = econ_step_cost(model_id, tok)
        upstream_usd = _num(d.get("cost"))
        priced = is_priced(model_id)

        def add(bucket):
            bucket["usd"] += econ_usd
            bucket["usd_upstream"] += upstream_usd
            bucket["in_tokens"] += _num(tok.get("input"))
            bucket["cached_in"] += _num(cache.get("read"))
            bucket["out_tokens"] += _num(tok.get("output"))
            bucket["reasoning_tokens"] += _num(tok.get("reasoning"))
            bucket["cache_write"] += _num(cache.get("write"))
            bucket["messages"] += 1
            if not priced:
                bucket["priced"] = False

        if not priced:
            unpriced[model_id] = unpriced.get(model_id, 0) + (
                _num(tok.get("input")) + _num(cache.get("read"))
                + _num(tok.get("output")) + _num(tok.get("reasoning"))
            )

        t = by_tier.setdefault(tier, _empty_tier())
        add(t)
        t["models"].add(model_id)

        m = by_model.setdefault(model_id, _empty_tier())
        add(m)
        m["models"].add(tier)  # reuse the set to record the tier(s) for this model

        add(totals)

    # finalise: round + serialise the model sets
    def fin(bucket, keep_models=True):
        b = dict(bucket)
        b["usd"] = round(b["usd"], 6)
        b["usd_upstream"] = round(b["usd_upstream"], 6)
        b["models"] = sorted(bucket["models"]) if keep_models else None
        if not keep_models:
            del b["models"]
        return b

    result = dict(
        source="sqlite", db=db, session_id=session_id, sessions=sessions,
        usd=round(totals["usd"], 6), usd_upstream=round(totals["usd_upstream"], 6),
        in_tokens=totals["in_tokens"], cached_in=totals["cached_in"],
        out_tokens=totals["out_tokens"], reasoning_tokens=totals["reasoning_tokens"],
        cache_write=totals["cache_write"], messages=totals["messages"],
        # models that ran but had no matrix entry (their per-tier USD is a silent
        # $0 — a drift signal, e.g. econ rebound a tier to an unpriced model).
        unpriced_models=sorted(unpriced),
        all_priced=(not unpriced),
        by_tier={k: fin(v) for k, v in by_tier.items()},
        by_model={k: {**fin(v), "tier": (sorted(v["models"])[0] if v["models"] else None)}
                  for k, v in by_model.items()},
    )
    # by_model's "models" set actually held tier names; expose it as "tiers".
    for k, v in result["by_model"].items():
        v["tiers"] = v.pop("models")
    return result


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="path to opencode.db")
    ap.add_argument("--session", default=None, help="restrict to this session + descendants")
    args = ap.parse_args(argv[1:])
    out = build(args.db, args.session)
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
