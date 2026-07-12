# e2e/econ — econ agent (unerr embedded) on SWE-bench

Single-arm benchmark: **econ**, the team's OpenCode-fork coding agent, solving
SWE-bench instances headless. Unlike the `e2e/reference/codex/` and `e2e/reference/claude/` arms,
there is nothing to attach or flip here — see below — so this directory just
runs econ and measures its resolve rate, cost, and token usage.

## Why single-arm (unerr is embedded)

unerr is compiled **directly into** econ, not attached as an external tool.
The embedded package is `packages/code-intelligence`
(`@opencode-ai/code-intelligence` — "in-process code intelligence: graph
queries, risk signals, recon bundles, context compression"). Its graph tools
are wired into econ's tool registry unconditionally
(`packages/opencode/src/tool/graph-tools.ts`, registered in
`packages/opencode/src/tool/registry.ts:126-129`); the built-in `read` tool is
also routed through it via `tool/read.ts`.

That wiring defines this benchmark:

- There is **no `unerr install`, no `unerrd` daemon, no offline-Pro
  entitlement, no `.mcp.json` / MCP config** — nothing to attach, nothing to
  mint, nothing to wire per-instance.
- There is **no extra system-prompt text** injected into the agent's context.
  In the codex/claude arms, unerr rides in as an external MCP server plus
  `AGENTS.md` policy text the agent has to read; here it's native to the agent
  framework, so there's zero prompt-space overhead and no instruction
  pollution to control for.
- Consequently there is **no on/off flip** to measure a within-agent delta.
  This directory simply runs econ and records what it does.

The comparison this benchmark supports is **cross-agent**: run the same
SWE-bench instance set through this arm and through `e2e/reference/codex/` (and
`e2e/reference/claude/`) to build a Codex-vs-econ table — resolve rate, $/instance,
tokens/instance, turns/instance — rather than an on/off delta within one
agent.

## How it fits together

```
SWE-bench instance                econ (headless)                    grader (standard)
repo @ base_commit        ->   opencode run --format json        ->  swebench.harness
problem_statement              --dir <repo>                          .run_evaluation
                                --dangerously-skip-permissions              │
        │                              │                                   │
        │                       NDJSON events (stdout)                     │
        │                              │                                   │
        └── git diff ───────> preds.jsonl ─────────────────────────────────┘
                                       │
                                       v
                          econ-telemetry.py (per-run parse)
                                       │
                                       v
                            results/meta.jsonl
                                       │
                                       v
                                  report.py -> cost-report.md / cost-report.json
```

## Files in this directory

- **`run-econ.sh`** — single-arm orchestrator. Loops over instances, runs econ
  headless against each checked-out repo, captures `git diff` as the
  prediction, writes `results/preds.jsonl` (SWE-bench predictions) plus the
  raw econ JSON events and `results/meta.jsonl` (per-instance run metadata).
  Flags: `--preflight` (zero-cost sanity check), `--instances N`,
  `--slice A:B`, `--dataset HF_ID`, `--repo-dir PATH`, `--label NAME`,
  `--timeout SECONDS`.
- **`econ-telemetry.py`** — parses one run's econ `--format json` NDJSON
  event stream into a telemetry object: turns, input/output/cached/reasoning
  tokens, usd, usd_upstream, tool_calls, graph_tool_calls (calls into the
  embedded code-intelligence tools — recon, get_references, search,
  file_outline, read), a per-tool call histogram, and a `by_tier` /
  `by_model` cost+token breakdown.
- **`report.py`** — aggregates `results/meta.jsonl` (optionally joined with a
  swebench grade report) into `results/cost-report.md` and
  `cost-report.json`: resolve %, mean turns, mean tokens, $/instance,
  $/resolved instance, graph-tool-call counts, and a per-tier cost table
  (conductor / oracle / executor).

## Prerequisites

```bash
# 1. econ built, or run from source (sibling repo: ../../econ-coding-agent)
cd ../../econ-coding-agent
bun install && bun run --cwd packages/opencode build
# binary lands under packages/opencode/dist/
# (single-platform alt: bun run script/build-econ-binary.ts)

# — or, run without building —
bun run --cwd packages/opencode --conditions=browser src/index.ts run ...

# 2. SWE-bench Python harness
pip install datasets swebench

# 3. The one required credential — econ's model routing is config-driven
#    (opencode.json: conductor=DeepSeek V4 Flash, oracle/reasoner=GLM,
#    executor=gpt-oss-20b, all via the self-hosted LiteLLM gateway with
#    Fireworks BYOK). Do not pass --model; econ picks the tier per step.
export LITELLM_API_KEY=...
```

## Run commands

```bash
# Preflight — zero cost, confirms the econ binary + dataset access
bash run-econ.sh --preflight

# Smoke — one instance
bash run-econ.sh --instances 1 --label smoke

# Pilot — five instances
bash run-econ.sh --instances 5 --label pilot

# Scale — 50-instance Verified Mini
bash run-econ.sh --slice 0:50 --instances 50 --label mini

# Grade (after any run above)
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified --split test \
  --predictions_path results/preds.jsonl --run_id econ_mini --max_workers 4

# Report — resolve rate, cost, tokens, tool-call breakdown
python report.py --meta results/meta.jsonl --grade-report <run_evaluation output>
```

### Targeted runs by difficulty tier

To run specific instances by hardness rather than the default Mini set, SWE-bench
Verified's per-instance `difficulty` annotation is bucketed into ready-to-run id
lists under **`../swebench-verified-difficulty/`** (easy 194 · medium 261 · hard
42 · veryhard 3). The fly full-resolve runner takes an `IDS=` override
(comma-separated, bypasses the Mini-10 default); see
`fly-remote/fullresolve/RUNBOOK.md` §2b and the difficulty dir's README. This is
the only way to exercise the non-django/cross-repo hard tier — the Mini-10/Mini-50
subsets are django/sphinx only.

## What performance & cost you get

- **Performance** — resolve rate from the standard
  `swebench.harness.run_evaluation` grader, which is agent-agnostic and just
  reads `results/preds.jsonl` and applies each patch.
- **Cost/token telemetry** — parsed from the `--format json` NDJSON stream.
  Each `step_finish` event carries per-step `part.cost` (usd) and
  `part.tokens.{input,output,reasoning,cache:{read,write}}`, but `part.cost`
  is the upstream **models.dev** catalog price, not econ's real spend (econ
  runs BYOK on Fireworks, and the executor tier is self-hosted at ~$0). To
  get the real number, `econ-telemetry.py` reads the terminal
  **`cost_breakdown`** event — `cumulative.cost` (econ's real BYOK total) plus
  a `models` map of per-model tokens/cost/label — falling back to summing the
  enriched `step_finish` events (which carry `modelID` + `econCost`) if
  `cost_breakdown` is absent. The benchmark records both: `usd` (econ's real
  BYOK cost, authoritative) and `usd_upstream` (the models.dev figure, for
  reference). `tool_use` events (`part.tool`) give tool-call counts, split
  into total calls and graph/context-tool calls so you can confirm unerr's
  in-process tools actually fired on a run.
- Because econ routes to cheap OSS models (DeepSeek/GLM/gpt-oss-20b via the
  self-hosted LiteLLM gateway) rather than frontier-model pricing, expect
  costs well below the Codex and Claude arms — that routing is econ's reason
  for existing.

### Per-tier cost (conductor / oracle / executor)

econ routes each step to one of three tiers, each on a different model, with
its own Fireworks BYOK price (USD / 1M tokens):

| Tier | Model | Agent mode | input | cached-read | output |
|---|---|---|---|---|---|
| conductor | `deepseek/deepseek-v4-flash` | primary | $0.14 | $0.03 | $0.28 |
| oracle / reasoner | `z-ai/glm-5.2` | primary | $1.40 | $0.14 | $4.40 |
| executor | `openai/gpt-oss-20b` | subagent | $0 | $0 | $0 (self-hosted) |

(Price matrix lives in the econ source at
`packages/code-intelligence/src/econ-cost.ts`, `ECON_COST_MATRIX`.)

`econ-telemetry.py` turns the `cost_breakdown` event's `models` map (or the
per-step `modelID`/`econCost` fallback) into `by_model` and `by_tier`
breakdowns; `report.py` renders a "Per-Tier Cost" table (total $, % of $,
tokens per tier) in the cost report.

Two caveats worth knowing before reading that table:

1. conductor / oracle / reasoner are `primary` agents, so they run in the
   main session and are fully captured by the `--format json` stream. The
   **executor is a `subagent`**, which runs in a child session the parent
   stream filters out — so executor *token volume* doesn't show up here.
   Its cost is unaffected (it's $0 by definition), but if complete per-tier
   token volume including the executor is needed, it lives in econ's session
   SQLite (`opencode.db`): each assistant message row stores its tier under
   `data.agent`, so grouping by `agent` there gives a full parent+child
   per-tier rollup. That path isn't wired up in this benchmark yet.
2. oracle and reasoner both run `z-ai/glm-5.2`, so a modelID-keyed breakdown
   merges them under one entry (harmless — they share a price, so the cost
   total is still correct). If you need them split, `data.agent` in the
   SQLite session log is what distinguishes them.

## How it compares to e2e/reference/codex & e2e/reference/claude

There is no in-arm A/B here. The comparison is cross-agent: run the same
SWE-bench instance set through `e2e/reference/codex/` and `e2e/reference/claude/` and this arm,
then line up the three `cost-report.json` outputs into one table — resolve %,
$/instance, $/resolved instance, tokens/instance, turns/instance. That table
is the actual "does unerr help" signal for econ: not an on/off flip, but
whether econ (unerr embedded, cheap OSS routing) resolves competitively at a
fraction of Codex/Claude's per-instance cost.

## Open items

- **Per-instance repo checkout / containerisation** — `run-econ.sh` currently
  assumes `REPO_DIR` is already the instance's checked-out repo at
  `base_commit` (the same `/testbed`-style assumption the codex local arm
  makes). A Docker-per-instance wrapper mirroring `e2e/reference/codex/local-docker` is
  a reasonable future add, but building and maintaining per-instance repo
  state is the harness's responsibility, not this script's.
- **Exact dist binary path** — confirm
  `../../econ-coding-agent/packages/opencode/dist/...` resolves to the actual
  `opencode` executable on the target runner (CI box vs. local machine) before
  a real run; `run-econ.sh`'s `ECON_BIN` may need pinning per environment.
