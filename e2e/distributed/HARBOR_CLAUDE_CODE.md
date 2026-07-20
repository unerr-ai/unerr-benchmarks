# Harbor + Claude Code custom agents — integration reference

How the `claude-<mix>` / `claude-native` benchmark arms (legacy aliases `claude` / `claude-real`) drive
**Claude Code** (the CLI) through **Harbor** (the Terminal-Bench harness) as a *custom Harbor agent* staged
with the full unerr harness — and every non-obvious defect we root-caused getting
it to actually resolve tasks. Read this before touching `harbor_agents.py`,
`harness_terminal.py`, or before wiring Claude Code into any other Harbor
benchmark. Companion to the distributed runbook ([README.md](README.md), §8 =
terminal orchestration); this file is the **agent-integration** deep-dive.

> **Golden rule:** Harbor's `ClaudeCode` agent (`harbor.agents.installed.claude_code`)
> was written for real-Anthropic auth on a laptop. Every fix below exists because
> a benchmark runs Claude Code **as root, non-interactive (`claude -p`), behind a
> custom gateway (`ANTHROPIC_BASE_URL`), with sub-agents**. Four independent Harbor
> assumptions break under that combination. Do not "simplify" a fix away without
> re-reading its root cause — each was proven live.

---

## 0. TL;DR — the four fixes

| # | Symptom | Root cause | Fix | Where |
|---|---------|-----------|-----|-------|
| 1 | Every Write/Edit/Bash denied → `NonZeroAgentExitCodeError`, 0 resolved | Harbor's default `--permission-mode=bypassPermissions` is **silently downgraded to interactive** when `claude -p` runs as **root** | Drop `--permission-mode`, append `--dangerously-skip-permissions` (honored under root because `run()` exports `IS_SANDBOX=1`) | `ClaudeUnerrAgent.build_cli_flags()` |
| 2 | Whole GPT tier ensemble collapses to one model | `run()` **flattens** all `ANTHROPIC_DEFAULT_*_MODEL` aliases to the single `--model` value whenever `ANTHROPIC_BASE_URL` is set | Pass **empty `--model`** on the gateway path → flatten never fires | `harness_terminal._arm_agent_config` (returns `""`) |
| 3 | Turn-1 `API Error: 400 Invalid model name ... claude-opus-4-8` | `run()` builds the container env from a **hardcoded key list** and drops arbitrary aliases; with no `--model` the main loop falls back to Claude Code's built-in default id `claude-opus-4-8`, which the gateway doesn't publish | Declare the 4 tier aliases + `ANTHROPIC_MODEL` as **`ENV_VARS`** (merged into the container env *last*, after the flatten) | `ClaudeUnerrAgent.ENV_VARS` |
| 4 | Task **sub-agents** get "Permission to use Bash has been denied" (main agent is fine) | `--dangerously-skip-permissions` applies to the **main session only** — it does **not** propagate to Task sub-agents; nor do settings `defaultMode` / sub-agent frontmatter `permissionMode` | A **PreToolUse auto-approve hook** in `.claude/settings.local.json` (inherited by sub-agents, fires before the permission resolver) | `_hooks_settings_command()` (written always by `install()`) |
| 5 | Terminal-Bench (89 tasks) gates inert or misfire: `HARNESS_HOOKS=1` fires on test edits that don't exist on terminal | SWE-bench gates (pytest/tox/test-file deny) are structurally absent on terminal (1/89 tasks exposes a pytest suite; leaderboard forbids peeking). Gates were OFF by default, and forcing them ON breaks every task | **Profile-driven gates** (§3.5): `HARNESS_PROFILE=generic` for terminal tasks — records Bash outcomes, agent DEC LARES verification via `# unerr:verify` marker, gates on marked-check success/regression/thrash, never on non-existent tests | `cc-harness-hooks.py` (both profiles inlined by `_hooks_settings_command()` / `run-instance.sh`) |

Fixes 1–4: commits `41f0694` + `56c1460`. Fix 5 (profile-driven gates): 2026-07-20 (this doc).

---

## 1. Architecture

`ClaudeUnerrAgent` (`tools/harbor_agents.py`) **subclasses** Harbor's first-party
`ClaudeCode` and reuses its `run()` **completely unchanged** — no auth, model, or
output-teeing logic is duplicated. It stages the unerr harness and makes exactly
three integration points:

```
ClaudeCode (Harbor, harbor.agents.installed.claude_code)
  └── ClaudeUnerrAgent (tools/harbor_agents.py)
        ├─ build_cli_flags()   → override: drop --permission-mode, add --dangerously-skip-permissions   (fix 1)
        ├─ ENV_VARS            → class attr: forward the 4 tier aliases + ANTHROPIC_MODEL               (fix 3)
        ├─ __init__()          → inject the ON operator prompt into --append-system-prompt; register unerr MCP
        └─ install()           → stage unerr (npm tgz + entitlement + index + daemon + agents),
                                  then write .claude/settings.local.json with the PreToolUse
                                  auto-approve hook (fix 4). Empty --model comes from harness_terminal (fix 2).
```

- **Arm selection** lives in `harness_terminal._arm_agent_config(worker)`:
  - `claude-<mix>` (legacy alias `claude` → `claude-open`) → gateway (the mix's model ensemble via
    econ-litellm — e.g. `claude-gpt` for GPT-5.6, §2 — map defined once in `run-distributed.sh`):
    `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` + the 4 tier aliases, **empty `--model`**.
  - `claude-native` (legacy alias `claude-real`) → real Anthropic (`CLAUDE_CODE_OAUTH_TOKEN` only), concrete `--model`.
  - `TERMINAL_STOCK_AGENT=1` → Harbor's **bare** `claude-code` agent (the control; no unerr).
- **Why subclass instead of fork:** `ClaudeCode.run()` reads gateway/auth off
  `os.environ`/extra_env, builds `claude --print --output-format=stream-json …`,
  pipes the instruction over stdin, tees to `/logs/agent/claude-code.txt`, and
  parses the trajectory to ATIF. All of that is reused verbatim.

---

## 2. Gateway routing (the `claude-<mix>` arms)

> **Naming (2026-07-20):** arms are `econ` | `claude-<mix>` | `claude-native` — the GPT-5.6 map below is
> the **`claude-gpt`** mix (a second mix, `claude-open`, routes the open-weight ensemble instead). Legacy
> `claude`/`claude-real` values are auto-normalized to `claude-open`/`claude-native`. The tier→model map
> for every mix is defined **once**, in a single `case` statement in `run-distributed.sh` — adding a mix
> is one case entry there, no changes needed in `harbor_agents.py`/`harness_terminal.py`.

Claude Code speaks the Anthropic API; the econ-litellm gateway translates to the
target mix's model ensemble. Tier map (host env → gateway model), forwarded by
`run-distributed.sh` → `harness_terminal` → `ENV_VARS`:

| Claude tier | Host env var | `claude-gpt` (GPT-5.6) example |
|---|---|---|
| main loop / sonnet / conductor | `ANTHROPIC_MODEL` (= SONNET alias) / `ANTHROPIC_DEFAULT_SONNET_MODEL` | `openai/gpt-5.6-terra` |
| opus (escalation) | `ANTHROPIC_DEFAULT_OPUS_MODEL` | `openai/gpt-5.6-sol` |
| haiku (sub-agents) | `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `openai/gpt-5.6-luna` |
| fable | `ANTHROPIC_DEFAULT_FABLE_MODEL` | `openai/gpt-5.6-sol-high` |

`ANTHROPIC_AUTH_TOKEN` = the LiteLLM key. `run()` reads it as the fallback for
`ANTHROPIC_API_KEY` (claude_code.py ~1381), so it reaches the container. **Empty
`--model` is mandatory** (fix 2) — a concrete `--model` re-fires the flatten,
which also pins `CLAUDE_CODE_SUBAGENT_MODEL` to one tier.

---

## 3. The four fixes, in detail

### Fix 1 — Permission bypass under root (main session)
`bypassPermissions` mode is silently downgraded to interactive prompt-mode when
`claude -p` runs as root (Terminal-Bench containers do). In `-p` that prompt
can't be answered → every tool denied → exit non-zero. `--dangerously-skip-permissions`
IS honored under root **because** `run()` exports `IS_SANDBOX=1` (claude_code.py
~1474). We drop `--permission-mode` at the source (`self._resolved_flags["permission_mode"]=None`),
never by regex — the rendered flags string carries the **unquoted**
`--append-system-prompt` value and a regex could bite into it. Mirrors
`run-instance.sh` (the proven SWE-bench flow) byte-for-byte.

### Fix 2 — Tier flatten
`run()` (claude_code.py ~1460): *"set all model aliases to the same model"* — fires
whenever `ANTHROPIC_BASE_URL` **and** `ANTHROPIC_MODEL` are both in env. A concrete
`--model` sets `ANTHROPIC_MODEL` → flatten collapses SONNET/OPUS/HAIKU +
`CLAUDE_CODE_SUBAGENT_MODEL` to that one value. Passing empty `--model` leaves
`ANTHROPIC_MODEL` unset at flatten time → flatten skipped → aliases survive.

### Fix 3 — Tier-alias forwarding (`claude-opus-4-8` 400)
`run()` builds the CONTAINER env from a **hardcoded key list** (API key, base URL,
OAuth, max-tokens) + the flatten + `env.update(self._resolved_env_vars)` (~1477).
It does **not** forward arbitrary host/extra_env vars — so the 4 tier aliases that
`harness_terminal` sets never reached the CLI. With no `--model` (fix 2) **and** no
aliases in-container, the main loop fell back to Claude Code's built-in default id
`claude-opus-4-8` → gateway 400 on turn 1. Declaring them as `ENV_VARS` routes them
through `_resolved_env_vars`, merged **last** (after the flatten), so `ANTHROPIC_MODEL`
= the SONNET/conductor tier and OPUS/HAIKU/FABLE keep distinct tiers. `env_fallback`
resolves from extra_env→os.environ; an unset fallback drops to None → **absent stays
absent**, so `claude-real` (concrete `--model`, no aliases) is unaffected.

### Fix 4 — Sub-agent permission gap  ⭐ (the one that made the GPT ensemble 0/5)
**Symptom:** fixes 1–3 land, models route correctly, the task runs — but every
task 0-resolves. Tools work for the **main** agent and are denied for **Task
sub-agents**.

**Proof (build-pmars, one task):**

| Run | main Bash / denials | sub-agent Bash / denials | reward |
|---|---|---|---|
| claude-code 2.1.212, no hook | 1 / **0** | 4 / **11** | 0 |
| claude-code 2.1.215, no hook | 2 / **0** | 1 / **1** | 0 |
| **2.1.215 + PreToolUse allow-hook** | 14 / **0** | 16 / **0** | **1** ✅ |

**Root cause:** `--dangerously-skip-permissions` bypasses the resolver for the
**main session only**. It does not propagate to Task sub-agents; neither does
settings `permissions.defaultMode` nor the sub-agent's frontmatter `permissionMode`
(all confirmed not to propagate in `-p` mode). The ON prompt tells the model to
*"delegate independent sub-tasks to unerr subagents"*, so it offloads the real
work (Bash/Read/WebSearch/WebFetch) into a sub-agent — where it's all denied →
nothing lands. **Version-independent** (2.1.212 ≡ 2.1.215) — do NOT try to "fix"
this by pinning an older CLI.

**Fix:** a **PreToolUse hook** is inherited by sub-agents from settings and fires
before the permission resolver on *every* tool call. Emitting the documented
`{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow",…}}`
grants the sub-agent's tools. Written unconditionally to `.claude/settings.local.json`
by `_hooks_settings_command()` (+ a tiny `allow-all.sh` helper). Silence does **not**
approve — you must emit `"allow"` explicitly.

**Coexistence with unerr's own hooks:** `unerr install claude-code` writes
`.claude/settings.json` with unerr's *own* PreToolUse hooks — `unerr hook pre-bash |
pre-read | pre-grep | pre-glob | pre-write | pre-edit` (for the graph /
active-cognition), plus the MCP server registration and the `UserPromptSubmit`/`Stop`
cognition hooks. Those are **observability, not permission gates** — they don't deny
(the main agent's Bash = 0 denials before we added anything). Our allow-hook goes in a
**separate** `.claude/settings.local.json`; Claude Code **UNIONS** hook arrays across
both files, so unerr keeps observing every tool call while our `"*"` entry supplies the
`allow` decision. Never write `.claude/settings.json` yourself — unerr owns it.

**Interaction with `HARNESS_HOOKS=1`:** the read-only-test **deny** + record + Stop
gate hooks are folded into the *same* `settings.local.json`. Claude Code evaluates a
`deny` before an `allow`, so a test-file edit stays blocked even though our allow-hook
matches `"*"` — and likewise any `deny` unerr's own hooks might emit still wins.

---

## 3.5. Generic harness profile (HARNESS_PROFILE=generic — built for Terminal-Bench, usable on any benchmark)

> Plain-language explainer — the full rationale, research findings (single-sensor
> collapse, TB2.1 hidden-tests inventory), and the generic-vs-swe policy:
> [HARNESS_PROFILES.md](HARNESS_PROFILES.md).

**The problem:** SWE-bench gates (pytest/tox sensors, test-file deny, datetime rule) are
structurally irrelevant on Terminal-Bench-2.1 tasks. Of 89 tasks, only 1 exposes a pytest
suite to the agent; leaderboard rules forbid test-peeking. On terminal the gates therefore
stayed OFF by default (`HARNESS_HOOKS=0`), keeping escalation inert. Forcing them ON misses the problem
— it spawns escalation on EVERY terminal task because Gate V demands a `tests/` module that
doesn't exist. Terminal grades via a *hidden* pytest suite mounted post-hoc (`tests/test_outputs.py`),
not an agent-visible one, and on a "reward = arbitrary Bash command + exit status" model.
The solution: a **profile-driven harness** (2026-07-20) that swaps gate semantics for terminal.

**How to enable:** set `HARNESS_HOOKS=generic` (e.g., `bench.sh` or `run-distributed.sh`).
Pairs with `HARNESS_PROFILE=generic` (or omit it and the hooks read `HARNESS_PROFILE` from
env, defaulting to `"swe"` for backward compat). SWE-bench combos are unchanged:
`run-instance.sh` hard-enables the hooks there with profile `swe` (byte-identical legacy
behavior); only terminal combos need `HARNESS_HOOKS=generic`.

| Gate | SWE-bench (`HARNESS_PROFILE=swe`, default) | Terminal (`HARNESS_PROFILE=generic`) |
|---|---|---|
| **Z** (no-op finish) | Block finish when no source edit was recorded | Block finish with no edits AND no successful command in the ledger |
| **V** (verify) | Block finish when an edit exists but no BROAD suite run (pytest/tox/`manage.py test`; `-k`/`::test` don't count) happened after it (cap 2) | Block finish when no marked check (`# unerr:verify`) has ever succeeded, OR an edit happened after the last green marked run (cap 2) |
| **R** (regression) | Block when a once-passing recognized test command now fails | Block when a once-passing marked check now fails |
| **E** (escalation) | (R-block or 2× V-block) and not yet escalated → demand unerr-opus + unerr-fable | identical skeleton, fed by the generic R/V |
| **B** (thrash deny) | Deny the 6th+ edit to one file since the last green test run (requires a prior V/R block; cap 2) | same counters, keyed on the last green marked run |
| Test-file deny (T) | Read-only block on `tests/` + `test_*.py` edits | No-op (the agent SHOULD write its own checks) |
| Datetime rule (C) | Deny introducing `datetime.now()` into a file using `utcnow` (Python time-source convention) | No-op |

**The marker protocol:** On terminal, the agent declares a command as the verification (the
proof that the task is solved) by appending **`# unerr:verify`** to the Bash line:

```bash
# Example: the task asks to build the project and prove it works
npm run build       # unerr:verify
npm run test        # no marker → just tracking, not verification
```

The command's **exit status** is what matters (0 = success). Gate V blocks the finish until
at least one marked check has succeeded — and blocks again if an edit lands after the last
green marked run — so every task is expected to establish one (cap 2 blocks, then Gate E
demands escalation). A marked command that once passed and now fails blocks the finish as a
regression (R). 5+ edits piling onto one file since the last green marked run trip the
thrash deny (B). Marked checks can appear anywhere in the Bash transcript; unmarked
commands are recorded but never gate.

**Plumbing:** the profile is selected at run-time via `HARNESS_PROFILE` env var (set by
`run-distributed.sh` and read by `_hooks_settings_command()` in `harbor_agents.py` and
`run-instance.sh`). The profile name is inlined into the hook commands in
`.claude/settings.local.json` — no separate profile file to version. Unit tests (gate
semantics, ledger protocol) live at
[`e2e/reference/claude/local-docker/tests/test_cc_harness_hooks.py`](../reference/claude/local-docker/tests/test_cc_harness_hooks.py).

**Kill switch:** unset `HARNESS_HOOKS` or set it to `"0"` to turn off all gates. Both
profiles are present but inactive. Default (`HARNESS_HOOKS` unset) = OFF on every benchmark.

---

## 3.6. Escalation mode: ladder vs panel (`ESCALATION_PANEL`)

Gate E forces escalation when verification fails repeatedly. Two modes:

| Mode | `ESCALATION_PANEL` | Behavior |
|---|---|---|
| **Ladder** | unset / `"0"` (default) | Rung 1: spawn opus alone. If trouble persists (new V/R block after opus lands), Rung 2: spawn fable with opus's proposal + why it failed. Max 2 rounds. |
| **Panel** | `"1"` | Spawn opus AND fable in parallel, same brief, independent reads, reconcile. Agreement = confidence; disagreement = ambiguous evidence. |

**Per-arm default:** ladder everywhere. Recommend panel (`ESCALATION_PANEL=1`) only on
`claude-open` where opus (deepseek-pro) and fable (glm) are distinct families; on
`claude-gpt` both tiers are the same family (gpt-5.6-sol) at different effort, so panel
doubles the most expensive tier for correlated reads. On `claude-native` ladder is cheaper;
enable panel explicitly for high-value runs.

**Rationale:** the ladder avoids self-assessment (gate state decides the rung, not the model's
opinion of task complexity) and avoids redundant expensive reads when the tiers are
correlated. The panel's value is model diversity.

**How:** `ESCALATION_PANEL=1 ./bench.sh …` or `ESCALATION_PANEL=1 BENCHMARK=verified ./run-distributed.sh …`.
Forwarded by `run-distributed.sh` / `run-benchmark.py` (only when set), inlined into the
gate-hook commands in `.claude/settings.local.json`. Plain-language explainer and live-evidence
note: [HARNESS_PROFILES.md §6](HARNESS_PROFILES.md).

---

## 4. Run it locally (no fly cost — the debug loop)

Reproduces the exact fly `claude`-arm container on your machine via `harbor run`.
Local harbor is pinned to **0.20.0** to match the dist image. Never print the key.

```bash
# scratch venv with harbor==0.20.0; tools/ on PYTHONPATH so -a resolves the agent
export ANTHROPIC_BASE_URL="https://econ-litellm.fly.dev"
export ANTHROPIC_AUTH_TOKEN="$(grep -E '^LITELLM_MASTER_KEY=' infra/litellm/.env.local | cut -d= -f2-)"
export ANTHROPIC_DEFAULT_SONNET_MODEL="openai/gpt-5.6-terra"
export ANTHROPIC_DEFAULT_OPUS_MODEL="openai/gpt-5.6-sol"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="openai/gpt-5.6-luna"
export ANTHROPIC_DEFAULT_FABLE_MODEL="openai/gpt-5.6-sol-high"
export PYTHONPATH="$PWD/e2e/distributed/tools"
unset ANTHROPIC_MODEL HARNESS_HOOKS         # match the fly gateway path exactly

harbor run \
  -d terminal-bench/terminal-bench-2-1 \
  -a harbor_agents:ClaudeUnerrAgent \
  -i terminal-bench/build-pmars \
  -o out/local-claude
  # --ak version=2.1.212   # optional: pin the claude-code CLI (BaseInstalledAgent 'version' kwarg)
  # NO -m  → empty model (fix 2). A concrete -m re-fires the flatten.
```

- Task names are **namespaced** (`terminal-bench/<name>`); the 89-task dataset
  auto-downloads to `~/.cache/harbor/tasks/`.
- `--ak/--agent-kwarg version=<X>` pins the claude-code CLI (default = npm latest).
- Bare control (isolates model vs harness): `-a` a stock `ClaudeCode` subclass that
  only adds `--dangerously-skip-permissions`, no unerr.
- Harbor's `result.json` `cost_usd` is **NOT** LiteLLM spend — "cost" = real LiteLLM only.

---

## 5. Debugging playbook (main session + sub-agents)

- **Is it a sub-agent permission problem?** Split denials by transcript:
  `agent/sessions/projects/-app/<uuid>.jsonl` (main) vs
  `.../subagents/<agent>.jsonl` (sub). `grep -c 'has been denied'` each. Main=0 +
  sub>0 ⇒ fix 4 (the allow-hook is missing / not firing).
- **Is the model routing right?** Check the trajectory `model_name` per step — main
  loop should be the SONNET/conductor tier, sub-agents the HAIKU tier, escalation
  OPUS/FABLE. `claude-opus-4-8` anywhere ⇒ fix 3 regressed.
- **Did the CLI even get the flag?** The tee'd command is in `trial.log`
  ("Running command: … | claude …"); confirm `--dangerously-skip-permissions` is present.
- **`unerr install claude-code` writes its OWN PreToolUse hooks** (`unerr hook
  pre-bash|pre-read|pre-grep|pre-glob|pre-write|pre-edit` in `.claude/settings.json`) —
  but they're observability, not gates; they don't deny (main-agent Bash = 0 denials).
  Confirm by reading the *installed* file inside the container, not by grepping the tgz
  (the config is built dynamically). Our allow-hook is a separate `settings.local.json`
  entry that Claude Code unions in; a `deny` still beats our `allow`.
- **Don't chase the CLI version.** The sub-agent gap is identical on 2.1.212 and
  2.1.215. Version pinning is a diagnostic lever, not a fix.

---

## 6. Decisions & rationale (for reuse on other benchmarks)

1. **Subclass `ClaudeCode`, reuse `run()`.** Never fork it — inject via `build_cli_flags`,
   `ENV_VARS`, `__init__`, `install()`. Keeps the class arm-agnostic (no hard-coded
   provider/gateway).
2. **Empty `--model` for any custom-gateway ensemble.** A concrete `--model` behind
   `ANTHROPIC_BASE_URL` flattens every tier (incl. sub-agents) to one model.
3. **Forward per-tier aliases via `ENV_VARS`, not extra_env.** `run()` drops extra_env;
   only `ENV_VARS`-declared vars survive, and they merge *after* the flatten.
4. **`--dangerously-skip-permissions` + `IS_SANDBOX=1` for root**, never `bypassPermissions`.
5. **PreToolUse auto-approve hook is the ONLY thing that unblocks sub-agent tools**
   in `-p`/root. Required for *any* Harbor agent that delegates via Task/sub-agents.
   Keep it ordered so a `deny` hook can still win when read-only enforcement is on.
6. **Version-independence is a finding, not a workaround.** Pinning the CLI does not
   fix permission behavior.

**Reusing for a new Harbor benchmark:** the agent (`ClaudeUnerrAgent`) is
benchmark-agnostic — point a new `harness_<bench>.py` at it the same way
`harness_terminal` does (arm → agent name + model + env), and all four fixes come
for free. Only the dataset/grade/flow descriptor changes (see `tools/benchmarks.py`).

---

## 7. Files

| File | Role |
|---|---|
| `tools/harbor_agents.py` | `ClaudeUnerrAgent` (fixes 1, 3, 4) + `_hooks_settings_command` |
| `tools/harness_terminal.py` | `_arm_agent_config` (fix 2 + gateway/auth wiring) |
| `reference/claude/local-docker/context/` | staged unerr artifacts (tgz, entitlement, `agents/`, `cc-harness-hooks.py`) |
| `reference/claude/local-docker/run-instance.sh` | the SWE-bench flow this mirrors (permission bypass, IS_SANDBOX, HARNESS_ON hooks) |
| [README.md](README.md) §8 | distributed terminal orchestration (fleets, monitoring, archive) |

> **Maintenance rule:** change the agent-integration or a fix → update this file in
> the SAME commit. Change the run/results/orchestration flow → update
> [README.md](README.md). Extend `tools/` scripts for new data needs rather than
> writing throwaway parsers.
