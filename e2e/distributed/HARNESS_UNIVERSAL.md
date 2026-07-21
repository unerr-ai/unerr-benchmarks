# The Universal Harness (claude arm)

> **Scope: `claude-*` arms ONLY** — `claude-gpt`, `claude-open`, `claude-native`.
> Does NOT apply to the `econ` arm (compiled unerr CLI + opencode; a different
> agent with different vocabulary — conductor/oracle/reasoner/executor). Everything
> below is about driving **Claude Code + unerr** as a Harbor / SWE-bench agent.
>
> **Status: IMPLEMENTED (2026-07-21).** The single `universal` profile replaced the
> old `HARNESS_PROFILE ∈ {swe, generic}` split. **This doc is the single source of
> truth** — it absorbs and REPLACES the two retired docs `HARNESS_PROFILES.md`
> (profile rationale/policy) and `HARBOR_CLAUDE_CODE.md` (Claude-Code integration
> deep-dive). Both are gone; their still-valid content lives here.

---

## 0. The thesis

The capability is the model's. The harness's only job is to make an
already-capable agent **reliable under unsupervised autonomy on an unknown
environment**. Every part of a good harness that isn't "the loop" exists to:

1. **learn the environment** (what builds / tests / runs this repo?),
2. **prove work is really done** (external, grounded, non-fakeable), and
3. **not get stuck** (detect thrash, escalate, cap cost).

None of those three is language- or framework-specific. Therefore there is
**exactly one harness** — not one per language, not one per framework, and no
SWE-vs-generic split. Building a harness per language/framework combination would
be an unbounded matrix; the generic harness *is* the harness, and anything it
lacks about a specific repo it **discovers at runtime** (from the repo's own
CI/config/lockfiles) or **fetches** (web search / docs / unerr graph).

### Why the old SWE/generic split was wrong

The split was the same missing abstraction — **discovery** — solved two bad ways:

- **`swe`** *hardcoded* `pytest` as both environment-knowledge and done-signal
  (a `TEST_CMD_RE` regex + broad-vs-narrow test detection). Strong, but only on
  Python repos that use pytest.
- **`generic`** had *neither* — it fell back to the agent marking its own homework
  with a `# unerr:verify` comment, which alone is false-green-prone.

Replacing the hardcoding with **discovery + reproduce-first** collapses the two
into one: SWE-bench is just "a repo where discovery finds pytest + FAIL_TO_PASS";
Terminal-Bench 2.1 is just "a repo/task where discovery finds a Makefile / build
script / run command." Same harness. The hidden grader stays **external** (Harbor
/ SWE-bench run it after the agent exits) — the agent never needs to know which
benchmark it's in.

---

## 1. What we KEEP (the core is already right)

The strongest general coding agents (mini-swe-agent, the Claude Agent SDK loop,
OpenHands, Aider) are the **same shape we already have**. The core —
`Claude Code + unerr + hook-install + PreToolUse auto-approve + escalation` — is
validated by the research, not replaced by it:

| The universal loop is… | We already have it as… |
|---|---|
| bash + file-edit + read + search + web; linear context; global turn/budget cap | **Claude Code native tools** |
| delegate specialization to the model in natural language; don't hardcode per-language logic | our **autonomy prompt** |
| sub-agent delegation is *cheaper and better* than escalating the whole session (Anthropic's own harness guidance) | **unerr junior / worker + graph nav** |
| capability ceiling → escalate the model | **opus / fable escalation ladder** |
| self-serve missing knowledge (docs, versions, APIs) | **web search / fetch_url / unerr graph** |
| sub-agents must be able to act unattended | **PreToolUse auto-approve hook** (matcher `*`, unconditional) |

We do **not** need per-language/framework harnesses, and we do **not** need an A/B
between two harnesses. The core stays; three universal layers were added and the
SWE-specific gates deleted.

---

## 2. The three universal layers

All three are language-agnostic. They **replaced** the SWE-specific gates.

### Layer 1 — Project onboarding / discovery

The agent learns the environment before touching code (prompt: the **ONBOARD**
paragraph):

- **Detect stack & commands richest-first:** CI workflows (`.github/workflows`,
  `.gitlab-ci.yml` — the richest source; they list the exact commands maintainers
  run) → config/manifests (`Makefile`, `package.json`, `pyproject.toml`,
  `Cargo.toml`, `go.mod`, `pom.xml`, `CMakeLists.txt`) → lockfiles →
  Dockerfile/README.
- **Provision missing runtime on-the-fly** (`uv`/`pip`/`npm`/`apt`/`apk`) — never
  assume the base image is complete. Harness-level, this also means the hook layer
  itself provisions **python3** if the base image lacks it (see §13, the old #32),
  so gates run on every image.
- **Cache the discovered `{build, test, run, lint}` commands** to verify against.

### Layer 2 — Discovery-driven verification

A strict anti-false-green contract (prompt: the **FINISH CONTRACT**), none of it
pytest-specific:

- **Bind the done-signal to the discovered command**, not a regex. "Verified" =
  the chosen check exits `0` after the fix.
- **Reproduce-first** — run the chosen check *before* editing and confirm it fails
  the way the task describes. Highest-ROI technique in the research (reported to
  move correct-code tasks ~24% → ~77%); it stops "fixing" what isn't broken and
  yields a grounded before→after. (Prompt-enforced: a Stop gate can't retroactively
  see the pre-edit state, so the contract puts reproduce-first up front.)
- **Build / typecheck / lint as universal fallback** when a repo has no usable
  test target — every stack compiles, typechecks, or at least runs.
- **External exit-code grounding + never weaken the check.** The agent marks the
  proof command with `# unerr:verify`; it may not rewrite a test/check to pass
  (self-weakening produces false-positives). The old test-read-only rule is
  generalized here (see Rule T, §4).

### Layer 3 — Generalizable robustness

Mechanical, language-agnostic:

- **Anti-premature-stop** — block "done" unless the marked verification actually
  passed (Gate V, §4).
- **Light mechanical stuck detection** — a repeated identical failing command
  (`STUCK_FAIL_THRESHOLD = 4`) routes into the escalation ladder (Gate E rung-1
  trigger). Deliberately conservative: high threshold, no new *hard* block, backstop
  only — the primary net stays the cost ceiling + verification-bound stop.
- **Escalation** on stuck-OR-verification-gap → `unerr-opus`/`unerr-fable`, or
  sub-agent delegation (Anthropic finds delegation cheaper than in-session model
  escalation).
- **Context management** — Claude Code compacts natively; we lean on it.

---

## 3. One profile, not two

The old three SWE-specific pieces were **deleted**:

| Deleted | Because |
|---|---|
| `TEST_CMD_RE` / `is_broad_test` (pytest regex, broad-vs-narrow) | replaced by discovery (Layer 1) + the marked-command ledger (Layer 2) |
| test-read-only *as a hard SWE rule* | generalized to a soft, one-time, override-able nudge (Rule T, §4) — applies everywhere |
| the utcnow-invariant deny rule (Rule C) | python/django-specific; removed outright |

…leaving one behavior on every benchmark:
`discover → reproduce-first → fix → verify-against-marked-command → escalate-if-stuck-or-unverified`.

---

## 4. The gates (the mechanical finish contract)

Enforced by `cc-harness-hooks.py` via three Claude Code hooks wired into
`.claude/settings.local.json`: a **PreToolUse** deny hook, a **PostToolUse**
record hook, and a **Stop** gate hook. State lives in an append-only JSONL ledger
at `/tmp/cc-harness/state.jsonl` (outside the repo — never in the model patch).

**The gates:**

- **Gate Z** — you can't finish if you did nothing (no edits **and** no successful
  command at all).
- **Gate V** — you can't finish until a `# unerr:verify`-marked command has
  succeeded; blocks again if an edit lands after your last green marked run.
- **Gate R** — a marked command that once passed and now fails blocks you (a
  regression you caused). Unmarked failures never count — `grep` exiting 1 is not
  a regression.
- **Gate E** — repeated V/R trouble (or the stuck trigger) forces escalation to the
  stronger models before finishing (see §5).
- **Deny B** — 5+ edits piling onto one file since the last green marked run →
  stop thrash-editing, escalate instead (capped per run).
- **Deny T** — editing a test/check path is denied **once** with a soft nudge
  ("the grader runs its own copy of the checks; fix the real source"); re-issuing
  the *same* edit is allowed (evidence-cited override — e.g. the task genuinely is
  to write tests). Not a hard block, works on any repo.

Caps: Z=1, R=1, V=2, overall Z/R/V=3; E is exempt and capped separately by mode.

### The sensing mechanism (no framework recognition)

1. **Outcome ledger.** Every Bash command is recorded with its exit status. No
   pytest/framework detection at all.
2. **Declared verification.** The prompt tells the agent to *decide the command
   that proves success for THIS task* — a build-and-run, a script it writes, a
   `curl`, a `diff` — *and append `# unerr:verify` to it.* A shell comment is
   harmless in any stack; the hook spots the marker.
3. **The gates read that ledger** — V needs a green marked run since the last edit;
   R catches a marked green→red; B counts edits since the last green marked run; Z
   relaxes to "no edits and no successful command."

### The `# unerr:verify` marker protocol

```bash
# The task asks to build the project and prove it works:
npm run build       # unerr:verify    ← the proof; exit 0 required to finish
npm test            # no marker → tracked, but not the proof
```

The command's **exit status** is what matters (0 = success). Mark only the check
you would stake the task on, never exploratory commands. Reproduce-first: run the
marked check *before* editing to confirm it fails, then again after to confirm it
passes.

---

## 5. Escalation — ladder vs panel

Gate E forces escalation when verification fails repeatedly (a prior R-block, V
capped at 2, **or** the stuck trigger: the same command failed ≥4× with no
progress). Two shapes, chosen by `ESCALATION_PANEL`:

| Mode | Env value | Behavior | When |
|---|---|---|---|
| **Ladder** | unset / `0` (DEFAULT) | Rung 1: spawn `unerr-opus` alone with the evidence brief (hypothesis withheld). If trouble PERSISTS after opus's proposal (a *new* V or R block), Rung 2: spawn `unerr-fable` with opus's proposal + why it failed. Max 2 rounds. | Default everywhere; best when opus/fable are the same family at different effort |
| **Panel** | `1` | Spawn `unerr-opus` AND `unerr-fable` **in parallel** (one message, two Task calls), same brief, independent reads, then reconcile. Agreement = confidence; disagreement = ambiguous evidence, prefer the verdict that explains ALL evidence + a definition-site fix. | When the two tiers are genuinely different model families |

**Per-arm recommendation:**

| Arm | opus / fable tier map | Recommendation |
|---|---|---|
| `claude-open` | opus = deepseek-v4-pro, fable = glm-5.2 | **`ESCALATION_PANEL=1`** — distinct families make agreement a strong signal |
| `claude-gpt` | opus = gpt-5.6-sol, fable = gpt-5.6-sol-high | **Ladder (default)** — same family at different reasoning effort; a panel doubles the most expensive tier for correlated reads |
| `claude-native` | opus = real Anthropic Opus, fable = Fable | Ladder (cheaper default); enable the panel deliberately when run value justifies it |

The ladder avoids self-assessment (gate state decides the rung, not the model's
opinion of task complexity) and avoids redundant expensive reads when tiers are
correlated. The panel's value is model diversity.

---

## 6. Environment toggles (the complete inventory)

| Env var | Values | Meaning |
|---|---|---|
| `HARNESS_HOOKS` | `0`/unset = OFF; any non-empty/non-`0` value = ON | Turns the universal harness (gates + deny rules + escalation) ON. Legacy `1` and `generic` **both** resolve to `universal`. **Defaulted to `1`** for every `claude-*` arm by `run-distributed.sh` (opt out with `HARNESS_HOOKS=0` for a bare-agent baseline). `ARM=econ` is never defaulted. |
| `ESCALATION_PANEL` | `1` = panel; unset/`0` = ladder | Gate E shape (see §5). Orthogonal to everything else. |
| `TERMINAL_STOCK_AGENT` | `1` = bare first-party claude-code agent | The no-harness baseline control (terminal flow). No default. |
| ~~`HARNESS_PROFILE`~~ | — | **RETIRED.** Accepted for env-wiring compat but never read; legacy `swe`/`generic` values are ignored. |

---

## 7. Agent integration — `ClaudeUnerrAgent` (the fixes that make it work)

`ClaudeUnerrAgent` (`tools/harbor_agents.py`) **subclasses** Harbor's first-party
`ClaudeCode` and reuses its `run()` **unchanged** — no auth/model/output-teeing
logic duplicated. It stages the unerr harness and makes a few integration points:

```
ClaudeCode (Harbor, harbor.agents.installed.claude_code)
  └── ClaudeUnerrAgent (tools/harbor_agents.py)
        ├─ build_cli_flags()  → drop --permission-mode, add --dangerously-skip-permissions
        │                        (and shlex.quote the --append-system-prompt value)
        ├─ ENV_VARS           → forward the 4 tier aliases + ANTHROPIC_MODEL
        ├─ __init__()         → inject the universal prompt into --append-system-prompt;
        │                        register unerr MCP; validate HARNESS_HOOKS is a bare token
        └─ install()          → stage unerr (npm tgz + entitlement + index + daemon + agents),
                                 provision python3 if missing, then write
                                 .claude/settings.local.json (PreToolUse auto-approve +
                                 deny + record + Stop gate). Empty --model from harness_terminal.
```

### Gateway routing tier map

| Claude tier | Host env var | `claude-gpt` (GPT-5.6) example |
|---|---|---|
| main loop / sonnet | `ANTHROPIC_MODEL` (= SONNET alias) / `ANTHROPIC_DEFAULT_SONNET_MODEL` | `openai/gpt-5.6-terra` |
| opus (escalation) | `ANTHROPIC_DEFAULT_OPUS_MODEL` | `openai/gpt-5.6-sol` |
| haiku (sub-agents) | `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `openai/gpt-5.6-luna` |
| fable | `ANTHROPIC_DEFAULT_FABLE_MODEL` | `openai/gpt-5.6-sol-high` |

### The root-caused fixes (all still load-bearing)

1. **Permission bypass under root.** `bypassPermissions` is silently downgraded to
   interactive prompt-mode when `claude -p` runs as root (Terminal-Bench containers
   do) → every tool denied → non-zero exit. `--dangerously-skip-permissions` IS
   honored under root **because** `run()` exports `IS_SANDBOX=1`. We drop
   `--permission-mode` at the source (`self._resolved_flags["permission_mode"]=None`),
   never by regex.
2. **Tier flatten.** `run()` sets all model aliases to one model whenever
   `ANTHROPIC_BASE_URL` **and** `ANTHROPIC_MODEL` are both set. Passing **empty
   `--model`** leaves `ANTHROPIC_MODEL` unset at flatten time → flatten skipped →
   the per-tier aliases survive. (Wired in `harness_terminal.py`.)
3. **Tier-alias forwarding.** `run()` builds the container env from a hardcoded key
   list and does not forward arbitrary host vars. Declaring the 4 tier aliases as
   `ENV_VARS` routes them through `_resolved_env_vars` (merged last, after the
   flatten), so opus/haiku/fable keep distinct tiers.
4. **Sub-agent permission gap.** `--dangerously-skip-permissions` applies to the
   **main session only** — it does NOT propagate to Task sub-agents (nor do
   settings `defaultMode` / sub-agent frontmatter). **Fix:** a **PreToolUse
   auto-approve hook** (matcher `*`, unconditional) is inherited by sub-agents and
   fires before the permission resolver on every tool call, emitting
   `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow",…}}`.
   Silence does NOT approve — you must emit `"allow"` explicitly. Never write
   `.claude/settings.json` yourself (unerr owns it); our allow-hook goes in a
   **separate** `.claude/settings.local.json` — Claude Code **unions** hook arrays
   across both. Version-independent (proven identical across CLI 2.1.212 / .215).
5. **Silent hooks-install failure.** A mislanded `settings.local.json` write once
   left every gate inert with zero trace (the write ran through `_lenient_exec`,
   no verification). Fix, three parts: (a) write to **both** the project-relative
   path AND `$HOME/.claude/`; (b) in-command post-write verification — every
   artifact existence-checked, both JSON copies parsed with `python3 -m json.tool`,
   exiting non-zero `FATAL: <path>` on any miss; (c) run the whole step via
   **`exec_as_agent`** (raises on non-zero) instead of `_lenient_exec`. A task
   without gates produces invalid data — worse than loud failure. *(Any terminal
   result recorded before 2026-07-20 should not be read as proof the mechanical
   gates fired; prompt compliance was real, gates may not have been.)*
6. **`--append-system-prompt` shell-quoting.** harbor==0.20.0's `build_cli_flags()`
   renders every flag as bare `f"{cli} {value}"` with no shell escaping, then
   splices the string into `bash -c "<string>"`; the prompt's `(`, `)`, backticks
   and newlines made bash mis-parse (`syntax error near unexpected token '('`),
   dropping all but the first word and exiting non-zero. Fix: pop
   `append_system_prompt` before `super().build_cli_flags()`, then re-append it
   `shlex.quote`'d as a single token (`import shlex`). Plus a fail-loud `__init__`
   guard rejecting a `HARNESS_HOOKS` value that isn't a bare `[A-Za-z0-9_.-]+`
   token (it reaches the hook-command env unquoted inside the settings JSON).

### Reusable decisions (for wiring Claude Code into any other Harbor benchmark)

1. Subclass `ClaudeCode`, reuse `run()`. Inject via `build_cli_flags`, `ENV_VARS`,
   `__init__`, `install()`. Keeps the class arm-agnostic.
2. Empty `--model` for any custom-gateway ensemble.
3. Forward per-tier aliases via `ENV_VARS`, not extra_env.
4. `--dangerously-skip-permissions` + `IS_SANDBOX=1` for root, never `bypassPermissions`.
5. The PreToolUse auto-approve hook is the ONLY thing that unblocks sub-agent tools
   under `-p`/root. Required for *any* Harbor agent that delegates via Task.
6. Version-independence is a finding, not a workaround.

`ClaudeUnerrAgent` is benchmark-agnostic: point a new `harness_<bench>.py` at it and
all six fixes come for free — only the dataset/grade/flow descriptor changes.

---

## 8. Two-flow install map

The harness is staged on two flows; **both drive the single universal profile.**

- **SWE-bench flow — `run-instance.sh`:** COPY'd into `/opt/toolbox` by
  `Dockerfile.toolbox`, grafted onto each per-instance SWE-bench image by
  `Dockerfile.instance`. At runtime it writes `.claude/settings.local.json` into
  `$REPO_DIR/.claude/` and asserts existence + JSON validity.
- **Terminal flow — `harbor_agents.py`'s `_hooks_settings_command()`:**
  `ClaudeUnerrAgent.install()` uploads the harness artifacts into the Harbor task
  container, provisions python3 if missing, then writes the settings file to BOTH
  the project-relative path AND `$HOME/.claude/`, asserting existence + JSON
  validity in-command (via `exec_as_agent`).

The gate script `cc-harness-hooks.py` is the **same file** for both flows (uploaded
by the terminal flow, COPY'd by the SWE flow); editing it once covers both.

---

## 9. The prompt — two byte-identical sites + maintenance rule

The autonomy/finish prompt is authored in **two** places that must stay in sync,
plus the gate messages that must stay consistent with them:

1. **`harbor_agents.py:_build_autonomy_prompt()`** — Python, terminal flow.
2. **`run-instance.sh`'s inline bash heredoc** — SWE flow.
3. **The gate block messages in `cc-harness-hooks.py`** — shown when a gate fires.

**Maintenance rule:** the ON-harness block (TRACK → ONBOARD → FIX DISCIPLINE →
DELEGATION → ESCALATION → FINISH CONTRACT) in (1) and (2) MUST be **byte-identical**;
any wording change is made in both in the SAME change and re-verified identical.
The gate messages (3) must stay semantically consistent with what (1)/(2) tell the
agent to expect. *(The BASE line legitimately differs per flow — terminal "Resolve
the task directly" vs SWE "…by editing the repository's source files directly" — and
the SWE flow's OFF baseline uses that BASE line unchanged.)*

Verify byte-identity with the scratch check that extracts the `TRACK →` block from
each site and diffs it (they were 4595 chars, identical, as of 2026-07-21).

---

## 10. Running & debugging it locally

Local repro against the fly gateway (drives the exact container path):

```bash
export ANTHROPIC_BASE_URL="https://econ-litellm.fly.dev"
export ANTHROPIC_AUTH_TOKEN="$(grep -E '^LITELLM_MASTER_KEY=' infra/litellm/.env.local | cut -d= -f2-)"
export ANTHROPIC_DEFAULT_SONNET_MODEL="openai/gpt-5.6-terra"
export ANTHROPIC_DEFAULT_OPUS_MODEL="openai/gpt-5.6-sol"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="openai/gpt-5.6-luna"
export ANTHROPIC_DEFAULT_FABLE_MODEL="openai/gpt-5.6-sol-high"
export PYTHONPATH="$PWD/e2e/distributed/tools"
unset ANTHROPIC_MODEL                 # NO -m → empty model (fix 2)
export HARNESS_HOOKS=1                 # universal harness ON (unset/0 = bare-agent baseline)

harbor run \
  -d terminal-bench/terminal-bench-2-1 \
  -a harbor_agents:ClaudeUnerrAgent \
  -i terminal-bench/build-pmars \
  -o out/local-claude
```

**Debugging playbook:**

- **Sub-agent permission problem?** Split denials by transcript:
  `agent/sessions/projects/-app/<uuid>.jsonl` (main) vs
  `.../subagents/<agent>.jsonl` (sub). Main=0 denials + sub>0 ⇒ fix 4 regressed.
- **Model routing right?** Check trajectory `model_name` per step — main loop =
  SONNET, sub-agents = HAIKU, escalation = OPUS/FABLE. `claude-opus-4-8` appearing
  on a gateway arm ⇒ fix 3 regressed.
- **Did the CLI get the flag?** The tee'd command is in `trial.log`; confirm
  `--dangerously-skip-permissions` is present.
- **Coexistence with unerr's own hooks:** `unerr install claude-code` writes
  `.claude/settings.json` with unerr's OWN observability hooks (they don't deny).
  Our allow-hook goes in the separate `settings.local.json`; Claude Code unions
  the arrays. `unerr install claude-code` sets up the MCP + active-cognition hooks
  only — NOT tool permissions, which is why the harness owns the bypass.
- **Gates inert?** Confirm `settings.local.json` exists in the container and is
  valid JSON, and that `HARNESS_HOOKS` is a non-empty/non-`0` value.

---

## 11. File map

| File | Role |
|---|---|
| `e2e/reference/claude/local-docker/context/cc-harness-hooks.py` | The gates + sensor + escalation ladder/panel (single universal profile). Ships a `--selftest`. |
| `e2e/reference/claude/local-docker/tests/test_cc_harness_hooks.py` | Unit suite locking gate behavior |
| `e2e/distributed/tools/harbor_agents.py` | `ClaudeUnerrAgent` (fixes 1, 3, 4, 6) + `_build_autonomy_prompt` + `_hooks_settings_command` + python3 provisioning |
| `e2e/distributed/tools/harness_terminal.py` | `_arm_agent_config` (fix 2 + gateway/auth wiring) |
| `e2e/reference/claude/local-docker/context/run-instance.sh` | SWE flow: prompt + hook settings writer (mirrors the terminal flow) |
| `e2e/reference/claude/local-docker/run-benchmark.py` | Forwards env vars into the SWE instance container |
| `e2e/distributed/run-distributed.sh` | Launcher: defaults `HARNESS_HOOKS=1` per claude-* arm; forwards env to workers |

---

## 12. What landed (2026-07-21)

- `cc-harness-hooks.py`: `_profile()` → `None`|`universal`; deleted the swe record/
  gate/deny branches, `TEST_CMD_RE`/`is_broad_test`, and Rule C; softened Rule T to a
  one-time override-able nudge; added `STUCK_FAIL_THRESHOLD=4` stuck trigger to Gate
  E rung-1. `--selftest`: **28/28 PASS**.
- `harbor_agents.py`: dropped the `profile` axis; universal `_build_autonomy_prompt`
  + ONBOARD; python3 provisioning in `install()`; `HARNESS_PROFILE` fully removed.
- `run-instance.sh`: unconditional universal prompt fragments; OFF baseline
  untouched; converged onto the general wording so the ON-harness block is
  **byte-identical** to the harbor copy (4595 chars).
- `run-distributed.sh`: `HARNESS_HOOKS=1` default for every claude-* benchmark (no
  more family-dependent `1`-vs-`generic`); `ARM=econ` never defaulted.
- **econ untouched** — verified: 0 econ files in the diff; the `CLAUDE_ARM_KIND`
  guard keeps econ out of the defaulting; econ has its own `run-instance.sh`.

Landing a change to these files needs a **rebake** (`harbor_agents.py` +
`cc-harness-hooks.py` are in the `Dockerfile.dist` / `Dockerfile.toolbox` COPY set)
and a **re-prepare** (HARNESS_* are runtime env resolved at worker-machine creation).

---

## 13. Open items

- **python3-missing on heterogeneous terminal base images (old #32) — FIXED** by
  Layer 1's harness-level provisioning in `install()`; ~30% of a terminal suite was
  previously lost to this infra gap (hook validator ran `python3 -m json.tool` and
  died when python3 was absent → task never launched, $0, 0-byte trajectory).
- **`status.sh` / `debug-workers.sh` can't enumerate the `claude-gpt × terminal`
  fleet (old #31)** — a separate `fleet-common.sh` tooling bug (resolves the wrong
  app from LABEL alone); tracked independently. Workaround: read the coordinator
  `queue.db` directly over `flyctl ssh console --machine <id>`.

---

## 14. Provenance

Designed + implemented 2026-07-21 from a four-part research sweep (minimal-loop /
environment-onboarding / verification / robustness) plus a code-grounded map of the
prior gates, and consolidating the two retired docs (`HARNESS_PROFILES.md`,
`HARBOR_CLAUDE_CODE.md`). Motivating evidence: the `claude-gpt × terminal`
dogfood-10 (2026-07-20) — 5/10 resolved, $3.24 real GPT-5.6 spend, escalation
proven live on build-cython-ext, 3 losses were the python3 infra gap (#32), not
capability. Related memory notes: `claude-gpt-terminal-dogfood10-result`,
`harness-hooks-python3-missing`, `append-prompt-shell-quoting-bug`.
