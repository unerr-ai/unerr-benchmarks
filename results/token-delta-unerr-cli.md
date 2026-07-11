# unerr Benchmark — Track 1 (deterministic token deltas)

Repo `/Users/jaswanth/IdeaProjects/unerr-cli` @ `844e9fd+dirty` · 2026-05-25
12340 entities, 15352 edges · tokenizer `o200k_base` · 32 tasks

## Headline

**On the code-navigation and file-reading work unerr handles, it removed 92.6% of the tokens** the naive grep+read path would have put into the agent's context to answer the same 32 questions.

- Baseline tokens (without unerr): **400.5K**
- unerr tokens: **29.8K**
- Saved: **370.7K** (92.6%)

### What this percentage covers

This is 92.6% of the tokens spent on **code navigation, file reading, and outlining** — the slice of an agent's work unerr intercepts and answers from its graph. It is **not** 92.6% off a coding agent's *total* bill.

A real session also spends tokens on code generation and file edits, model reasoning, sub-agent context, system prompts, and conversation history — none of which this track touches. So treat this as the per-operation gain on the retrieval slice, not a whole-session discount. Track 3 (end-to-end A/B) measures the whole-session effect; this track isolates the retrieval slice so the per-operation reduction is measured cleanly. Because it is a ratio of tokens, it holds at any model price.

### Context-window impact

Tokens in an agent are cumulative. Anything pulled into context stays there, is re-sent on every following turn, and counts against the window until the agent compacts — Claude Code auto-compacts at its 1M-token window. So the reduction above is not a one-time, per-turn saving: it is context the agent never has to carry for the rest of the session.

A session that ran all 32 of these operations would accumulate **400.5K** tokens of navigation/read context the naive way, versus **29.8K** via unerr. Measured against the context window itself:

| Context window | Naive footprint | unerr footprint | Window reclaimed |
|---|--:|--:|--:|
| 200K — standard Claude / GPT-4o context | 400.5K (200.2%) | 29.8K (14.9%) | 185.4% |
| 1M — Claude Code auto-compaction window | 400.5K (40.0%) | 29.8K (3.0%) | 37.1% |

A footprint over 100% does not fit — the agent must compact or drop context mid-task, losing earlier reasoning. Prompt caching discounts the *dollar* cost of re-sent context but does **not** return window space, so reclaimed headroom is the durable, model-agnostic benefit: more of the window stays free for the actual code and reasoning, and auto-compaction triggers far later.

## By capability bucket

| Bucket | Tasks | Baseline | unerr | Saved | Reduction | Fidelity |
|---|--:|--:|--:|--:|--:|--:|
| navigation | 24 | 330.3K | 21.4K | 308.9K | 93.5% | 24/24 |
| compression | 8 | 70.2K | 8.3K | 61.9K | 88.2% | 8/8 |

## By task category

| Category | Tasks | Baseline | unerr | Reduction | Fidelity |
|---|--:|--:|--:|--:|--:|
| find-symbol | 8 | 88.5K | 3.9K | 95.6% | 8/8 |
| get-entity | 8 | 130.3K | 10.8K | 91.7% | 8/8 |
| find-callers | 8 | 111.5K | 6.7K | 94.0% | 8/8 |
| understand-file | 8 | 70.2K | 8.3K | 88.2% | 8/8 |

## Appendix — illustrative cost translation

The product reports only tokens and turns. The figures below exist solely for communication: an illustrative scaling of the measured token reduction by current list rates (per 1M input tokens, reviewed 2026-05). The percentage above is the durable claim; list prices change.

Per-month projection assumes **40 nav/read operations/day × 22 days**, avg **12.5K** baseline tokens/op → **10.2M** input tokens saved/developer/month.

| Provider (agent) | $/1M in | Saved $/dev/month |
|---|--:|--:|
| Claude Opus 4.x — Claude Code (Opus) | $15 | $152.93 |
| Claude Sonnet 4.x — Claude Code / Cursor (Sonnet) | $3 | $30.59 |
| Claude Haiku 4.5 — Claude Code (Haiku) | $1 | $10.20 |
| GPT-4o — Cursor / Copilot (GPT-4o) | $2.5 | $25.49 |
| GPT-4.1 — Cursor / Copilot (GPT-4.1) | $2 | $20.39 |
| Gemini 2.5 Pro — Gemini CLI (Pro) | $1.25 | $12.74 |
| Gemini 2.5 Flash — Gemini CLI (Flash) | $0.3 | $3.06 |

## Fidelity

A token saving only counts if unerr actually returned the answer. Of 32 tasks, 32 were fidelity-checked against the graph's ground truth and **32 passed**.

No fidelity failures — every measured saving returned the answer.

## Method

Baseline = real `grep` output + real file reads for the same question (conservative: top-N files only). unerr = the real QueryRouter tool result (post enrichment + compression). Both counted with the same `o200k_base` tokenizer, so the percentage is robust to tokenizer bias. See `benchmarks/README.md` for the full baseline model and its assumptions.
