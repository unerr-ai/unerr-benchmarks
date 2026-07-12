# Methodology

How the numbers in [`RESULTS.md`](RESULTS.md) are produced, and what they do and
don't claim.

## What's measured

Resolve rate, cost ($/instance), and token/turn counts for the **econ** coding
agent (unerr compiled in) against **reference agents** — Claude Code and Codex —
on the same [SWE-bench Verified](https://www.swebench.com/) instances, graded by
the standard SWE-bench harness. This is a **cross-agent** comparison, not an
unerr on/off delta within one harness: unerr is embedded in econ and has no
on/off flip, so there's nothing to toggle within that arm.

## Dataset

[SWE-bench Verified](https://www.swebench.com/) — the 500-instance
human-filtered split. Runs so far use **Verified Mini** subsets (Mini-10, ladder
to Mini-50) for cost reasons; the distributed fleet (`../e2e/distributed/`) is
built to run the full 500 as one pinned-image campaign (`CAMPAIGN=<name>`).

## One-variable protocol

Every arm is graded against the **same instance set** with the **same** grading
harness (`swebench.harness.run_evaluation`) — apply the patch, run the gold
tests, pass@1. The only thing that varies between rows in a results table is the
**agent under test**; each agent runs through its own native interface (econ
headless via `opencode run`, Claude Code CLI, Codex CLI) — no agent is driven
through another's harness or given another's system prompt.

- **No test knowledge** — agents never see `PASS_TO_PASS`/`FAIL_TO_PASS` or the
  `hints` field.
- **Web search off by default** (`WEBSEARCH=0` / `EXA_API_KEY` unset) — SWE-bench
  fixes are public, so an enabled web search risks answer-lookup rather than
  problem-solving.
- **External reference scores are labeled, not blended.** Frontier-model
  leaderboard numbers we didn't run ourselves (`REFERENCE-SCORES.md`) are kept as
  a separately dated, self-reported anchor — never mixed into our own measured
  rows.

## Scoring

- **Grading** — the standard `swebench.harness.run_evaluation`: apply the
  predicted patch, run the instance's tests, pass/fail per instance.
  Agent-agnostic — it only reads a predictions file.
- **Cost/tokens** — real provider `usage` where the arm's API exposes it (for
  econ, the real BYOK cost from its terminal `cost_breakdown` event, not the
  models.dev list-price estimate).
- **[SWE-Effi](https://arxiv.org/abs/2509.09853)** (token-bounded effectiveness)
  — scores resolve rate *against* resources spent, so a cheaper-but-worse run
  can't win purely by failing faster. Implemented in `../e2e/common/scoring/`.

## Honesty / limitations

- **Small n.** Mini-10 is 10 instances — noisy and directional, not a
  resolve-rate estimate with a tight confidence interval.
- **Self-reported reference scores.** The Claude/Codex leaderboard numbers in
  `REFERENCE-SCORES.md` are the vendors' own reported figures, scaffold-dependent,
  and not independently re-run by us except through our own reference arms
  (`../e2e/reference/`).
- **OSS-model variance.** econ routes across DeepSeek/GLM/gpt-oss-20b via a
  self-hosted gateway; transient upstream stalls (no client-side request timeout
  yet) can turn a resolvable instance into an empty patch — see the per-run
  notes in `RESULTS.md`.
- **No leaderboard submission.** The SWE-bench Verified leaderboard requires an
  academic-affiliated author and a published technical report (policy added
  2025-11-18); we have neither, so campaign runs are framed as a **reproducible,
  self-hosted run**, not a leaderboard entry — full gap analysis in
  [`SUBMISSION.md`](SUBMISSION.md).
