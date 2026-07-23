# TERMINAL_HARNESS — Terminal-Bench 2.1: our harness, Harbor integration, cost, and leaderboard submission

Authoritative reference for how the **claude + unerr** agent runs against Terminal-Bench 2.1,
how it plugs into the **Harbor** framework, how cost is attributed, and exactly what it takes to
put a row on the **tbench.ai leaderboard**. Synthesised 2026-07-22 from three code-grounded
investigations (leaderboard-assets, Harbor-cost, Gate-V) + the run-B conductor=sol A/B.

Cross-refs: [`HARNESS_UNIVERSAL.md`](HARNESS_UNIVERSAL.md) (the universal harness — gates,
escalation, env), [`HARNESS_IMPROVEMENT_PLAN.md`](HARNESS_IMPROVEMENT_PLAN.md) (Gate-V plan),
[`README.md`](README.md) (distributed orchestration hub).

---

## 0. TL;DR — the decisions and why

| Decision | Choice | Why |
|---|---|---|
| Leaderboard from the distributed fleet? | **No — impossible** | Our fleet emits 89 single-trial jobs; the hub needs ONE job × 89 tasks × ≥5 trials (§2). |
| How to submit | **Native `harbor run -d … -k 5 --upload --public`** | The only hub-eligible shape (§2, §5). |
| Isolation for the native run | **`-e daytona`/e2b/modal (cloud sandbox), not single-host `-n`** | Per-trial sandbox = the resource isolation our fleet had, without a coordinator (§1, §5). |
| Row identity — agent | **`unerr` / `ClaudeUnerrAgent`**, version-tagged | The named custom agent, importable from the public repo (§4a, §6). |
| Row identity — model | **`anthropic/claude-opus-4-8`** (opus headline of the ensemble) | Single headline label = our top tier; the full opus+sonnet+haiku mix + summed cost ride in the trajectory the maintainers audit (§4a). |
| Submitted model must be REAL | **Real Anthropic Claude — never gateway-GPT** | A row labelled `claude-opus-4-8` that routed to GPT would be false; a real model also makes cost correct for free (§4). |
| Token accounting | **Agent self-reports `total_cost_usd` — no manual math** | Claude Code emits it (incl. sub-agent spend); correct with a real model (§4b). |
| Repo | **COPY into a clean new repo — NOT a destructive move** | Harness is shared with verified/pro/live; monorepo keeps it. New repo: `unerr-ai/unerr-terminal-bench` (§6). |
| Gate-V | **Already implemented + grounded; add the missing tests** (§3) | Feature is live/baked; only the CI test coverage is a gap. |

---

## 1. Two execution models — and why they're different

**(a) Distributed fleet (internal — how we've been running).**
`e2e/distributed` stands up a coordinator + N fly worker machines; each worker leases ONE task and
runs it via `harbor run --path <task_dir> --jobs-dir <dir> -n 1` (harness_terminal.py:776). So
concurrency = **N machines × 1 task**, each task on a dedicated performance-4x + 50 GB disk + its own
Docker daemon. This buys per-task **resource isolation** (the compute-heavy tail — caffe-cifar-10,
train-fasttext, video-processing — never starves) and **fault isolation** (one machine dies → the
coordinator reassigns just that task), plus our own orchestration (silent-death rerun, tracing to
`queue.db`, Tigris archival, the gate ledger). It is **not** Harbor-hub-native.

**(b) Native Harbor run (leaderboard — how we must submit).**
One `harbor run -d terminal-bench/terminal-bench-2-1 … -k 5 -n <conc> -e <sandbox> --upload --public`
invocation produces ONE job = 89 tasks × 5 trials in one job-dir with one UUID, uploaded to the hub.
Concurrency comes from Harbor's `-n` (`--n-concurrent`); isolation comes from the **environment**
(`-e`): `docker` = one host's daemon (contention on the heavy tail); **`-e daytona`/`e2b`/`modal` =
one cloud sandbox per trial** — the same isolation as the fleet, in native format. The docs explicitly
recommend a sandbox provider "to increase parallelization."

> Key knobs, don't conflate them: **`-k` = trials per task** (leaderboard needs **≥5**); **`-n` =
> concurrent trials** (throughput). 89 × 5 = **445 trials** per submission run.

---

## 2. Why the distributed run CAN'T become a leaderboard submission (Track A)

The hub/leaderboard is built around **one Harbor job**:

- Job-dir schema (`harbor/upload/uploader.py:_load_job_from_disk` ~140–170): top-level
  `config.json` (the single JobConfig — agent/model/reasoning_effort) + `result.json` (JobResult with
  all `trial_results[]`), and per-trial `<trial>/{result.json,config.json,agent/,verifier/,artifacts/,trial.log}`.
- `harbor upload <job-dir> --public` works **post-hoc** (README: "Already ran without `--upload`? Upload
  after") — but only for a dir that is *already one job*.
- `lb submit <hub-job-uuid>` → PR; CI **re-fetches from the hub** and does static analysis, validating
  that every trial record matches **the** (singular) job config, and that each rewarded trial has an
  **ATIF `trajectory_path`** on the hub for the `/judge` audit.

Our fleet produces **89 separate single-trial jobs** (89 UUIDs, 89 configs). They cannot be merged into
one hub job: job identity is frozen on the hub, and CI would see 89 configs where it expects one. Our own
`make_tb_submission.py` already flags this (`"_local": True`, empty `source_jobs` — a **local mirror of
the schema, never hub-eligible**).

**Eligibility checklist (SUBMIT.md):**
1. **Unmodified dataset**, default execution (no timeout/resource overrides).
2. **≥5 trials/task, all tasks** (errors count as reward 0).
3. **Trials on the hub, public** (`--upload --public` or post-hoc `harbor upload … --public`).
4. **Complete metadata + valid `source_filter`** (agent, agent_version, model=`provider/model`, reasoning_effort).
5. **ATIF `trajectory_path` per rewarded trial** on the hub (custom-log-only fails).

---

## 3. Gate-V status (Track C) — implemented + grounded, tests missing

The grounded Gate-V is **live and baked** (not a todo):
- Verify-strength classifier `cc-harness-hooks.py:_verify_strength()` (~746) flags the 4 weak shapes
  (tamper / self-referential / existence-only / no-comparison).
- `_last_green_verify_ts()` **skips weak greens** so they don't satisfy Gate V; weak-only sessions
  **couple into Gate-E escalation**. Hardwired on (`_harness_verify_strict()` / `_harness_escalate_on_weak()`).
- Live-verified: `pytest tests/` passes the gate; `test -f f.txt`, bare `gcc main.c`, and self-referential
  checks **block** with the weak-verify message; `# independent: <why>` overrides (non-banned classes only).

**Gap:** the plan claims "Selftest Cases 18/18b/19/20/21/22/22b cover it" — **those tests don't exist**
in `test_cc_harness_hooks.py` (29 tests, none for weak-verify/tamper/weak-escalation). Feature works;
**no CI guard against regression** → add the tests.

**Limit worth knowing:** Gate-V catches *weak verify commands*; it cannot force the agent to run *any*
verification, and it can't run the task's **hidden** verifier (Harbor runs that after the agent exits).
So a false-green where the agent writes a plausible-but-wrong solution and simply doesn't self-verify can
still slip — this likely explains several of run-B's ~9 false-green misses even with Gate-V baked in.

---

## 4. Submission identity & cost — what the row truthfully says (Track B)

### 4a. What identity goes on the row (and why it's truthful)

- **Agent:** `unerr` — Claude Code + the unerr harness — version-tagged, importable from the public
  repo as `ClaudeUnerrAgent`. This is the agent name/version on the row.
- **Model (headline):** **`anthropic/claude-opus-4-8`** — our top / most-capable tier (the conductor).
  We run an *ensemble* (opus conductor + sonnet + haiku sub-tiers via Task sub-agents), but the
  leaderboard `model` field is a **single headline label**, by the standard "report your primary model"
  convention. Reporting the opus tier is truthful because the full ensemble rides in the two things the
  maintainers actually audit:
  - the **ATIF trajectory** (per rewarded trial) records the *real per-step model name* → the
    opus/sonnet/haiku mix is visible and honest there;
  - the **cost** is the *summed* spend across every tier, not just opus (§4b).
- **The one hard requirement:** the headline must be a model we *actually run*. The row MUST execute
  against **real Anthropic Claude**, never GPT-via-our-gateway — a row labelled `claude-opus-4-8` that
  secretly routed to GPT would be false. Real opus → label, cost, and trajectory are all honest.
- A **GPT** row would be a *different* agent (Codex / GPT-native), not this one — the agent *is* Claude Code.

### 4b. Cost attribution — how Harbor derives it, and why "real model" fixes it

**Harbor does NOT re-price tokens.** Cost is **agent-owned**: `AgentContext.cost_usd`
(`harbor/models/agent/context.py:18`; also `n_input_tokens`/`n_cache_tokens`/`n_output_tokens`). The agent
sets it in `populate_context_post_run()`; `trial/result.py` aggregates `ctx.cost_usd` into
`result.json` `stats.cost_usd`.

**How ClaudeCode fills it** (`claude_code.py`):
- **Path 1:** parse `total_cost_usd` from Claude Code's own stdout stream-json (accurate).
- **Path 2 (fallback):** `litellm.cost_per_token(model=step.model_name, …)`.

**The gateway mispricing:** under our gateway swap, Claude Code's `model_name` is `"claude-opus-4-8"`
while the real tokens are GPT-5.6. Both Path 2 **and the hub's trajectory re-derivation** then price
Anthropic rates on GPT tokens. Our `ClaudeUnerrAgent` does **not** override
`populate_context_post_run` (harbor_agents.py:569 — a thin `ClaudeCode` subclass), so `result.json`
inherits the mispriced number. The coordinator patches it **post-hoc** for internal reporting only
(harness_terminal.py:~919 `fetch_cost()` → `meta["cost"]` → `tigris_archive._norm_cost`), which fixes
`queue.db`/archive but **not** `result.json` or the trajectory the hub reads.

**Why the real-model decision solves it with zero code:** with a **real Claude (Anthropic)** model there
is no swap — Claude Code's stdout `total_cost_usd` is correct, and the trajectory `model_name` matches
the real model, so the **hub re-prices correctly**. No `context.cost_usd` override, no trajectory rewrite.

**Do we count tokens ourselves? No — the agent self-reports.** `populate_context_post_run()` parses
Claude Code's `total_cost_usd` straight into `AgentContext.cost_usd` (Path 1), and that number **already
includes the sub-agent / ensemble spend** (Task sub-agents run inside the same Claude Code session). So
with a real model there is **no manual token math** — rely on Claude Code's own cost and spot-check one
trial for sanity. Manual counting / the `fetch_cost` override is needed *only* under the gateway swap,
where CC's self-reported model ≠ the billed model.

- The agent **is** Claude Code, so "real, reproducible model" = **real Claude/Anthropic**. A GPT
  leaderboard row would be a *different* agent (Codex/GPT-native), not this one.
- The gateway-GPT path + the `fetch_cost` override remain **internal-only**, for our distributed
  cost/economics studies (e.g. the conductor=sol A/B: real LiteLLM spend by tier). See
  [[cost-means-litellm]] / `PRICING.md`.

*(If we ever DID want to submit the gateway-GPT config: it's un-reproducible without our infra and
would require rewriting both `context.cost_usd` AND the trajectory `model_name` to the real GPT model
before serialization — not worth it. Use a real model.)*

---

## 5. Leaderboard submission runbook (native)

```bash
# 1. Install Harbor + a sandbox provider, sign in
uv tool install "harbor[daytona]"
harbor auth login

# 2. Native full-dataset run — ONE job, 89 tasks × 5 trials, per-trial sandbox isolation
harbor run \
  -d terminal-bench/terminal-bench-2-1 \
  -a <repo>.harbor_agents:ClaudeUnerrAgent \   # our custom agent, from the separate repo (§6)
  -m claude-<real-model> \                      # REAL Anthropic model — honest + reproducible + correct cost
  --ak reasoning_effort=<none|low|medium|high> \
  -e daytona \                                  # per-trial cloud sandbox (isolation; raise -n safely)
  -k 5 \                                         # ≥5 trials/task (REQUIRED)
  -n <10-20> \                                  # concurrency (sandboxes isolate, so higher is safe)
  --upload --public

# (or: run without --upload, then `harbor upload <job-dir> --public` once)

# 3. Submit to the leaderboard
git clone https://github.com/harbor-framework/terminal-bench-2-1.git
cd terminal-bench-2-1/leaderboard
uv run lb submit https://hub.harborframework.com/jobs/<uuid>
# → opens a PR; CI runs static analysis (§2 checklist) + maintainers /judge the trajectories.
```

Pre-submit gate (all must be true): unmodified dataset · ≥5 trials/task/all tasks · job public on hub ·
`source_filter` matches the real agent+model+effort · ATIF `trajectory_path` per rewarded trial.

---

## 6. The separate terminal-agent repo

**Goal:** a clean, public, reproducible repo that IS the agent — so anyone can `harbor run -a <it>` and
reproduce our leaderboard row, and so custom-agent variants are easy to build.

**Target repo:** `https://github.com/unerr-ai/unerr-terminal-bench` (created 2026-07-22).

**Copy, don't move — the harness is shared.** `cc-harness-hooks.py` and `ClaudeUnerrAgent` are the
*universal* harness: the same files drive verified / pro / live_verified through the distributed fleet, so
a literal `git mv` of "the terminal parts" would break those benchmarks in the monorepo. The new repo is
therefore a **clean, self-contained, Harbor-native COPY** — agent + harness + reproducible recipe, adapted
to run against a real model with zero gateway/fly/Tigris/econ code — while the monorepo keeps its shared
infra for internal runs. Genuinely terminal-*specific* internal files (`make_tb_submission.py`, this doc)
are relocated / referenced into the new repo. An exact file-by-file extraction manifest + proposed layout
is produced (recon) before any files are written into it.

**Include (the agent + harness only):**
- `ClaudeUnerrAgent` (the `harbor_agents.py` class — a `ClaudeCode` subclass) + its `build_cli_flags`
  bypass and `ENV_VARS` forwarding.
- The universal harness hooks: `cc-harness-hooks.py` (the Z/V/R/E gates + grounded Gate-V + escalation)
  and its tests (+ the missing 18/18b/19/20/21/22/22b — §3).
- The unerr install step (MCP + active-cognition hooks) as a documented, optional dependency.
- A minimal reproducible recipe: the exact `harbor run` command (§5) + an `.env.example`.
- `HARNESS_UNIVERSAL.md` (trimmed to the agent, not the fleet).

**Exclude (internal scaling infra — unpublishable):**
- The distributed coordinator/workers, `run-distributed.sh`, fly/Tigris, `bench.sh`, monitoring.
- The **econ-litellm gateway** (config/secrets), the model-swap, `fetch_cost`, econ, SWE/Pro/live_verified.
- Any `.env`, org ids, cost matrices.

**Integrity guardrail:** the public repo's agent must run against a **real, nameable model**; it must not
require our private gateway. The gateway-GPT cost experiment stays in the monorepo. (Aligns with the
existing public-release-reorg plan.)

---

## 7. Open items

- [ ] Populate `unerr-ai/unerr-terminal-bench` from the extraction manifest (copy + adapt: agent, harness,
      tests, recipe; strip all gateway/fly/Tigris/econ code) — §6.
- [ ] Confirm the row identity on the first submission: agent `unerr` / `ClaudeUnerrAgent`, model
      `anthropic/claude-opus-4-8`, `reasoning_effort` set — §4a.
- [ ] First native `-e daytona -k 5` submission run with a real Claude model; verify one hub job + costs
      re-derive correctly (they should — §4b).
- [ ] Add the missing Gate-V tests (§3) so the grounded behavior is CI-guarded.
- [ ] `make_tb_submission.py` remains our LOCAL mirror for internal inspection; it is NOT the hub path.
