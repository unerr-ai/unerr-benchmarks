# Track 4 — Single-repo host-local A/B (full platform)

Runs a real coding agent against **one** open-source repo twice (or thrice) — once
with unerr, once without — on the same seeded tasks, and measures the difference
across every platform pillar: **savings, guardrails, memory, cost**.

No Docker. No API key. The agent is driven by `claude -p` so it uses your Claude
Code login, and the JSON output gives real token / turn / cost numbers.

## Why one repo, not SWE-bench's full set

SWE-bench's multi-repo test oracle needs each instance's exact dependency + OS
image — that's what Docker is for. Reproducing 300 repos' environments by hand on
one machine is the part that breaks. One repo you set up **once** sidesteps that
entirely while still giving a real agent, real edits, and a real test oracle.

## The three arms

| Arm | unerr installed | Memory kept across tasks |
|---|:--:|:--:|
| `baseline` | no (`--strict-mcp-config` + empty MCP config) | n/a |
| `unerr` | yes | yes (warm) |
| `unerr-nomemory` | yes | no — notes/facts wiped between tasks |

`unerr` vs `baseline` measures the whole platform. `unerr` vs `unerr-nomemory`
isolates the **memory** pillar while keeping every other unerr surface on.

## What each pillar reads from

- **Savings / resolve** — real `usage` tokens + turns from the `claude -p` JSON,
  scored with the existing SWE-Effi token-bounded AUC (`../../e2e/common/scoring/swe-effi.ts`).
- **Guardrails** — `behavior_events` rows in the run window of each unerr arm's
  `.unerr/metrics.db`, classified by `metrics-reader.ts`. A "guardrail save" is a
  task the baseline left red, unerr passed, and a guardrail fired during the unerr run.
- **Memory** — warm `unerr` vs `unerr-nomemory` on tasks that declare `dependsOn`
  (a fact learned in the earlier task should help the dependent one).
- **Cost** — `total_cost_usd` from the JSON (0 on a subscription run — there the
  savings signal is total input tokens + turns, not dollars).

## Prerequisites

1. `claude` CLI logged in (`claude` once interactively to confirm).
2. `unerr` linked on PATH so the unerr arms can `unerr install claude-code`
   inside their worktree: `pnpm run build && pnpm link --global`.
3. A target repo checked out locally with a known-green commit and a test command.

## Authoring a manifest

Copy `tasks.example.json` and fill in:

- `repo` — absolute path to the checked-out target repo.
- `baseCommit` — a green commit; every arm resets here between tasks.
- `setupCommand` — one-time per-arm install (e.g. `pnpm install`).
- each task's `breakCommand` (introduces the bug — omit if the repo ships red),
  `prompt` (handed to the agent verbatim), and `testCommand` (exit 0 = resolved).
- `dependsOn` on any task that builds on an earlier one — that's what makes it a
  memory probe.

## Running

```bash
# Validate the manifest + arm wiring with NO agent spend:
tsx run.ts tasks.json --dry-run

# Real run (spends Claude Code budget). Defaults: all 3 arms, 1 rep, acceptEdits.
tsx run.ts tasks.json --out ./out

# Knobs:
tsx run.ts tasks.json --arms baseline,unerr --reps 3 --model <snapshot> --max-turns 40

# Re-score an existing out/runs.jsonl without re-running the agent:
tsx run.ts tasks.json --score-only --out ./out
```

Outputs land in `--out` (default `./out` next to the manifest):

- `runs.jsonl` — one `RunRecord` per (task, arm, rep).
- `REPORT.md` — the scorecard across all four pillars.

## Honesty notes

- `--dry-run` only validates wiring; it never reports savings.
- Cost is 0 on a subscription — read total input tokens + turns instead.
- A guardrail "save" is a countable cross-check (baseline red, unerr green, a
  guardrail fired), not a causal proof. The report names the tasks so you can read
  the trajectory yourself.
- Claude-only: the driver is `claude -p`. Porting an OpenAI arm would also need the
  Claude Code hooks (note injection, the edit gate) re-implemented, or the
  guardrail/memory pillars go dark.
