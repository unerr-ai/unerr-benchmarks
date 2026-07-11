# External SWE-bench reference scores (cross-reference snapshot)

> **What this is.** A dated snapshot of *published* SWE-bench scores for current
> frontier coding models, kept so an `e2e/` A/B run can be sanity-checked against
> the field. These are **not** our numbers — our `e2e/` benchmark measures a
> *paired, within-harness ±unerr delta* (resolve-rate / $ / turns) on the same
> agent. Use the table below as the **absolute anchor**; use our delta as the
> **unerr contribution** on top of it.
>
> **Last refreshed: 2026-06-28.** Scores move monthly — re-pull from the Sources
> before quoting. Most provider numbers are **self-reported** and **scaffold-dependent**.

## How to compare honestly

- **Match the scaffold.** Our headline arm runs **Codex CLI**, so the like-for-like
  external anchor is the **Codex-scaffold** row (e.g. GPT-5.3-Codex on Verified),
  *not* a raw model self-report in a different harness. The scaffold can move a
  score several points on its own.
- **n = 50 is noisy.** Verified Mini (50) has a wide confidence interval. Trust it
  for our *paired delta* (each instance run both ways), not for chasing an absolute
  leaderboard rank.
- **Self-reported ≠ independent.** Where an independent eval exists (e.g. vals.ai
  with the SWE-agent scaffold), it usually lands below the provider's number.

## Reference numbers online (June 2026)

Most frontier numbers are reported on full Verified (500), not Mini. Current standings:

| Model | SWE-bench Verified | Notes |
|---|--:|---|
| GPT-5.5 (OpenAI) | 88.7% | #1, self-reported Apr 2026 |
| Claude Opus 4.7 | 87.6% | #2; leads SWE-bench Pro at 64.3% |
| GPT-5.3-Codex | 85.0% | ← the Codex-scaffold number to beat |
| Claude Opus 4.6 | 80.8% | |
| Gemini 3.1 Pro | 80.6% | |
| Claude Sonnet 4.6 | 79.6% | ~5× cheaper than Opus |

## SWE-bench Pro — June 2026

| Model | Pro | Notes |
|---|--:|---|
| Claude Opus 4.7 | **64.3%** | #1, Anthropic-reported |
| GPT-5.4 (xHigh) | 59.1% | Scale SEAL mini-swe-agent |
| GPT-5.3-Codex CLI | 56.8% | |
| GPT-5.2-Codex | 56.4% | |

## SWE-bench Verified **Mini** (50 tasks) — our cheap short set

- A **random 50-instance** subset of Verified: **~5 GB** of images vs **130 GB**
  for the full set, built to preserve the difficulty/pass-rate distribution — i.e.
  a valid cheap proxy. HuggingFace `princeton-nlp/SWE-bench_Verified` (or the
  dedicated `*verified-mini` split for the exact 50).
- The **HAL Verified-Mini leaderboard** (Princeton) is the one that reports
  **per-task $ cost + scaffold**, but it is dominated by *older/cheaper* models on
  SWE-agent / HAL-Generalist scaffolds — its top is **~54%**, so frontier
  (GPT-5.5 / Opus) numbers are **not** found there; use full-Verified for those.
- **This is the slice our `e2e/` defaults to** (1 → 5 → 50 instance ladder). Open
  gap: confirm we pin the *official* Mini-50 split, not an arbitrary `0:50` slice,
  for direct comparability to the HAL board.

## Sources (re-pull before quoting)

- SWE-bench official leaderboards — https://www.swebench.com/
- HAL SWE-bench Verified Mini leaderboard (cost + scaffold) — https://hal.cs.princeton.edu/swebench_verified_mini
- SWE-bench leaderboard May 2026 roundup — https://www.marc0.dev/en/leaderboard
- Morph "best AI model for coding" (Pro + cost/task) — https://www.morphllm.com/best-ai-model-for-coding
- vals.ai independent SWE-bench evals — https://www.vals.ai/benchmarks/swebench
- SWE-Effi (token-bounded effectiveness) — https://arxiv.org/abs/2509.09853
