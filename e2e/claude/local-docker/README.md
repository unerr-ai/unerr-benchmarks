# e2e/claude/local-docker — Claude Code (± unerr) on SWE-bench

Paired A/B: the same **Claude Code** CLI agent solves the same SWE-bench
instances **with** unerr attached (arm B) and **without** (arm A). Same image,
one env flip, so the cost/turn/resolve delta is attributable to unerr.

This is the Claude-side parallel to `e2e/codex/local-docker/`. It reuses the
identical machinery (official SWE-bench images + a grafted toolbox + offline-Pro
entitlement + the standard grader) and only swaps the agent-specific seams:

| seam | codex arm | this (claude) arm |
|---|---|---|
| CLI | `@openai/codex` | `@anthropic-ai/claude-code` |
| auth | `OPENAI_API_KEY` → `codex login` | **`CLAUDE_CODE_OAUTH_TOKEN`** (subscription, no API key) |
| MCP wiring | `unerr install codex` (`.codex/`, `AGENTS.md`) | `unerr install claude-code` (`.mcp.json`, `.claude/settings.json`, `CLAUDE.md`) |
| run | `codex exec --json` | `claude -p --output-format stream-json` |
| model | `gpt-5.4-mini` (pinned) | **default config — no `--model`** |
| toolbox image | `unerr-codex-toolbox` | `unerr-claude-toolbox` |

The codex tree is untouched; this directory is self-contained.

## Auth: your subscription, no API key

Claude Code can run headless on a **long-lived subscription OAuth token** that
bills to your Pro/Max plan — no `ANTHROPIC_API_KEY` required. Mint it once:

```bash
./auth-bootstrap.sh                 # runs `claude setup-token`, writes .env.local (gitignored)
set -a; . .env.local; set +a        # load CLAUDE_CODE_OAUTH_TOKEN into your shell
```

(Or interactively: `! claude setup-token`, then `export CLAUDE_CODE_OAUTH_TOKEN=<token>`.)

The token is passed into each container via `docker run -e`. The macOS Keychain
(where an interactive `claude` login stores creds) can't be mounted into a
container, so the env-var token is the portable path. Tokens expire — re-run
`auth-bootstrap.sh` when a run starts 401-ing. `ANTHROPIC_API_KEY` also works as
a pay-per-token fallback if you prefer.

> Default-config note: we never pass `--model`/`--effort`. The arm runs whatever
> Claude's default model is for your subscription, so the A/B delta is purely
> "unerr on vs off", never a model choice.

## How it fits together

```
official instance image            toolbox (we build)            grader (standard)
swebench/sweb.eval.x86_64.<id>  +  Node + Claude + unerr    ->   swebench.harness
repo @ base_commit, deps, tests    + offline Pro entitlement      .run_evaluation
        │                                  │                            │
        └──────── derived image ───────────┘                            │
              run `claude -p` -> git diff = model_patch ──> preds.json ──┘
```

## Preflight — prove unerr works BEFORE spending tokens

Zero cost: **no token, no `claude -p`**. Builds the image and verifies the whole
chain inside it.

```bash
python run-benchmark.py --instances 1 --preflight
```

Checks (each prints `[PASS]`/`[FAIL]`):
1. toolbox binaries present (`node`, `claude`, `unerr`)
2. `unerr doctor` — native cozo/sqlite modules load in the grafted image
3. offline Pro entitlement minted (plan=pro, `max_active_repos:-1`)
4. `unerrd` socket up (started after the entitlement env)
5. `unerr install claude-code` wrote `.mcp.json` (references unerr) +
   `.claude/settings.json` + `CLAUDE.md`
6. MCP path works: `initialize` → `tools/list` returns the unerr tools (no
   `-32003` cap refusal = login-skip worked) → `tools/call file_read` executes

A non-zero exit means unerr is NOT correctly attached — fix before any paid run.

## Run it

```bash
# 0. prereqs
pip install datasets swebench
./auth-bootstrap.sh && set -a; . .env.local; set +a   # subscription token
docker info >/dev/null                                # daemon up; ~30GB free disk

# 1. build the toolbox from your unerr-cli checkout (re-run when unerr changes)
UNERR_REPO=/path/to/unerr-cli ./build-toolbox.sh

# 2a. PREFLIGHT — prove unerr runs + MCP tools work in-image. No token, $0.
python run-benchmark.py --instances 1 --preflight

# 2b. SMOKE — one instance, both arms. Prove the pipeline end-to-end.
python run-benchmark.py --instances 1 --mode both --label smoke

# 3. PILOT — five instances, both arms. Shape of the delta, catch flakes.
python run-benchmark.py --instances 5 --mode both --label pilot1

# 4. scale once green: Verified Mini (50), paced for subscription limits.
python run-benchmark.py --mini --mode both --label mini
```

## Scoring (resolve-rate + SWE-Effi)

After a run, grade both arms and produce the A/B report. The whole sequence is
wrapped by `score.sh`:

```bash
RESULTS_DIR=results/pilot1 DATASET=princeton-nlp/SWE-bench_Verified ./score.sh
```

Step by step (what `score.sh` runs):
1. `swebench.harness.run_evaluation` on `preds_on.json` and `preds_off.json`
   (`--run_id claude_on` / `claude_off`) → per-instance resolved verdicts.
2. `node score.mjs <results-dir> <on-report.json> <off-report.json> <out-dir>` —
   joins `meta_{on,off}.jsonl` with the grader verdicts, applies the fidelity
   gate (only instances **both** arms attempted), emits `arm-on.jsonl` /
   `arm-off.jsonl` in the shared SWE-Effi `Trajectory` shape
   (`instanceId, resolved, inputTokens, outputTokens, turns, breakages`).
3. `tsx e2e/common/scoring/run.ts arm-off.jsonl arm-on.jsonl` → the SWE-Effi
   A/B report (normalized resolve-vs-resources), `claude-off` (baseline) vs
   `claude-on` (treatment).

> Localization F1 is **out of scope** for this backend — that's the fly
> loc-runner's metric (`e2e/codex/fly-remote`). Here the metrics are resolve-rate
> and SWE-Effi (resolve normalized against tokens/$).

## Cost, pacing & rate limits (you're on Max)

Report cost **only on instances BOTH arms solved** (fidelity gate) so you compare
like-for-like.

| Step | Instances × arms | Purpose |
|---|---|---|
| Preflight | — | $0 health check, no token |
| Smoke | 1 × 2 | pipeline works end-to-end |
| Pilot | 5 × 2 | shape of the delta, catch flakes |
| Mini | 50 × 2 | the number (paired delta confident at n=50) |

Subscription runs are bounded by a **5-hour rolling window**, not $/token. A
50-instance mini = 100 agent runs, which can trip the window even on Max — so
`--mini` **paces by default** (`pace=30s` between instances). Tune it:

```bash
python run-benchmark.py --mini --mode both --pace 60 --label mini   # slower, safer
python run-benchmark.py --mini --mode both --pace 0  --label mini    # no pacing
```

If a run starts failing with auth/limit errors mid-batch, increase `--pace` or
split the mini across two windows (it resumes — `preds_*.json` and
`meta_*.jsonl` are appended/merged per label).

## Comparing to the codex numbers

Claude's default model is a **different anchor** than codex's `gpt-5.4-mini`
(see `e2e/REFERENCE-SCORES.md`). Do **not** read Claude-vs-codex absolutes as an
unerr result — report the **paired ON-vs-OFF delta within each harness**. A real
unerr win shows up as a positive delta in *both* harnesses.

## What you get

`results/<label>/preds_<mode>.json` (predictions) + `results/<label>/meta_<mode>.jsonl`
(per-instance wall time, exit code, patch size, telemetry: turns/tokens/$/
tool_calls/mcp_tool_calls) + `results/<label>/artifacts/<mode>/<iid>/`
(`claude-events.jsonl` stream + captured `.unerr/**`). After grading + `score.sh`,
the SWE-Effi A/B report lands in `e2e/common/results/`.

## Open items before a real run

- **`unerr install claude-code` MCP wiring in-image** — the one integration seam
  to watch. On the smoke run confirm `/tmp/unerr-install.log` is clean and that
  `claude -p` actually makes unerr tool calls (`mcp_tool_calls > 0` in the ON
  arm's telemetry). The driver loads the unerr server explicitly via
  `--mcp-config .mcp.json --strict-mcp-config` to avoid the headless trust prompt.
- **Headless permissions** — the driver uses `--dangerously-skip-permissions`
  (the container is the sandbox). The unerr guardrail hooks in
  `.claude/settings.json` still run (cascade-guard etc.), which is intended.
- **`REPO_DIR`** — defaults to `/testbed` (SWE-bench convention); override with
  `--repo-dir` if a pulled image checks out elsewhere.
- **Disk** — instance images are large; `docker image prune` between batches.
