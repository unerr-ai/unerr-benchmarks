# Token-overhead benchmark corpus (live Claude Code A/B)

Frozen task set for measuring unerr's token-overhead reduction **inside a real
Claude Code session** — the live, transcript-measured complement to the offline
benchmarks. Where this sits in the suite:

- **This corpus** — manual, runs the agent for real, reads the session transcript.
  Captures cache-amplification (round-trips, `cache_read ÷ cache_write`) that only
  shows up in a live session.
- **`internal/navigation/`** — the *deterministic* (no-LLM, no-session) version of
  the same "fewer tokens to navigate" claim. CI-friendly; use it for regression.
- **`e2e/`** — the end-to-end **total bill** ($, turns, resolve-rate) on SWE-bench.
  External anchors for that run are in `e2e/REFERENCE-SCORES.md`.

Run each task **twice** — once with unerr connected, once without — keeping the
prompts verbatim so before/after is comparable.

## How to measure

1. Run the task in a fresh Claude Code session.
2. Find that session's transcript (`~/.claude/projects/<encoded-repo>/<uuid>.jsonl`).
3. `node scripts/measure-token-baseline.mjs <that-file>.jsonl` — the measurement
   script ships in the **`unerr-cli`** checkout (`unerr-cli/scripts/`), not in this
   benchmarks repo; run it from there against the transcript path above.
4. Record `round-trips`, `cache_read`, `cache_write`, `total`, and the
   `cache_read ÷ cache_write` ratio.

The ratio is the headline: ~2× is healthy, ~10× is the amplification the work
targets. unerr's server-side `events.jsonl` also now logs `recon_pattern_hit`
(Sprint 0, T0.3) and the tool-call histogram — cross-check the round-trip story.

## The three tasks (do not edit — frozen)

### C1 — Trivial lookup (worst case for unerr; needs no graph)
> "How does shell compression work?"

Expectation: this is the task that showed ~3.2× in the research doc. Target
after the fixes = **parity** with no-unerr, not a win (footprint router keeps
unerr out of the way).

### C2 — Single-entity edit (the segment unerr is for)
> "Add a `getRecentTools()`-style accessor to the latency tracker in
> `src/proxy/session-stats.ts` and update its callers safely."

Expectation: no-unerr must blind-navigate (grep/glob/read many files) to find
callers + conventions = many amplified hops. unerr should resolve it in ~1
recon call + a couple targeted reads. Target = **fewer round-trips than
no-unerr** AND correct caller set.

### C3 — Large multi-file sweep
> "Find every place that writes to `events.jsonl` and confirm none of them go
> to stdout."

Expectation: no-unerr reads many large files; unerr points precisely. unerr was
already net-positive here; target = widen the margin, main-thread growth flat
via the Sprint-4 subagent path.

## Baseline table (fill in before changing code)

| Task | Mode | round-trips | cache_read | cache_write | ratio | total |
|---|---|---|---|---|---|---|
| C1 | no-unerr | | | | | |
| C1 | unerr (pre) | | | | | |
| C2 | no-unerr | | | | | |
| C2 | unerr (pre) | | | | | |
| C3 | no-unerr | | | | | |
| C3 | unerr (pre) | | | | | |

## Re-measure (fill in after the change)

| Task | Mode | round-trips | cache_read | cache_write | ratio | total | Δ vs pre |
|---|---|---|---|---|---|---|---|
| C1 | unerr (post) | | | | | | |
| C2 | unerr (post) | | | | | | |
| C3 | unerr (post) | | | | | | |
