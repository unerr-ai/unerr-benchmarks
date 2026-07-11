# e2e/common/scoring — End-to-end A/B (SWE-bench Verified Mini × SWE-Effi)

The credible, externally-comparable claim: run the **same agent + model** twice
on the same instances, changing **one variable** — whether unerr's MCP tools are
available — and measure real resolve rate, turns, tokens, and dollars.

This is the only track that needs **Docker + an API key + a real budget**, so
the agent runs happen via the steps below; the scoring (`swe-effi.ts`) and the
report (`run.ts`) are ready now and run offline on the resulting trajectories.

## Why this matters

The localization benchmark proves the *tool output* is smaller. This scorer proves that translates into
a real agent spending fewer tokens to solve real issues — and, via SWE-Effi's
token-bounded AUC, that we win by being **more efficient**, not by **failing
faster** (the standard confound when you report token deltas alone).

## Dataset

**SWE-bench Verified Mini** — 50 human-validated instances, the cheapest
credible end-to-end set. `HuggingFace: princeton-nlp/SWE-bench_Verified` (take
the Mini 50) — harness: `github.com/SWE-bench/SWE-bench`. Scale to Lite (300) or
Verified (500) for a publishable number once the 50-instance signal is clear.

Requirements (per SWE-bench docs): x86_64, ~120 GB free disk, 16 GB RAM, Docker.

## Protocol (one variable: the toolset)

1. Pick a fixed model snapshot + temperature + system prompt. Freeze them.
2. **Arm A (baseline):** agent scaffold with only built-in grep/read/glob and
   unchunked file reads.
3. **Arm B (treatment):** identical scaffold, unerr MCP tools added
   (`search_code`, `get_references`, `file_read`, `file_outline`, etc.) — point
   the agent's `.mcp.json` at `unerr --mcp` in each instance repo.
4. Run both arms over the same 50 instances. Recommended harness:
   `mini-SWE-agent` (built for SWE-bench, emits per-instance token/turn data).
5. For each instance, record a `Trajectory` (see `swe-effi.ts`):
   ```json
   {"instanceId":"django__django-12345","resolved":true,
    "inputTokens":83120,"outputTokens":4210,"turns":11,"breakages":0}
   ```
   `inputTokens`/`outputTokens` MUST come from the provider's real `usage`
   (Anthropic returns it per response) — not an estimate. Report cached and
   uncached separately if the harness caches the trajectory prefix.
6. Write each arm to a JSONL file, then:
   ```bash
   tsx e2e/common/scoring/run.ts armA-baseline.jsonl armB-unerr.jsonl
   ```

## Metrics produced (`swe-effi.ts`)

- **Resolve rate** per arm (must not regress vs baseline; ideally improves).
- **Total input tokens** + the % reduction — the headline "% off the bill".
- **Mean turns-to-solution** — the "fewer turns" claim.
- **Breakages** — patch-apply failures / malformed tool calls.
- **Token-bounded effectiveness AUC** — resolves achievable per token budget.
  Treatment AUC ≥ baseline AUC is the proof we're efficient, not just cheap.
- **$/run** saved across the top provider rates (illustrative).

## Cost note

50 instances × 2 arms, each up to ~100K tokens and tens of turns, is a real but
bounded spend (tens of dollars at Sonnet rates; more at Opus). Start with a
5-instance smoke run to validate both scaffolds emit valid trajectories before
committing the full 50.
