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

Replacing the hardcoding with **discovery + reproduce-first** collapses SWE-bench
cleanly: SWE-bench is just "a repo where discovery finds pytest + FAIL_TO_PASS."
It does **not**, on its own, collapse Terminal-Bench 2.1. A code-grounded audit
(2026-07-21) of all 89 real TB2.1 tasks found discovery finds *nothing* to onboard
on ~85% of them — they are "produce this artifact" tasks (write a file to an exact
path, render an image, emit a report) with no project, no CI, no pre-existing
failing check. The missing abstraction there isn't discovery, it's **task shape**
(§2, Layer 0): REPAIR tasks *are* "a repo/task where discovery finds a Makefile /
build script / run command," but PRODUCE and OPERATE tasks are not repos at all.
One harness stays true — the shape branch lives inside the same prompt and gate
set, not a second profile — but "discovery absorbs the SWE-vs-terminal difference"
was the wrong load-bearing claim; task-shape classification is what actually does
it. The hidden grader stays **external** (Harbor / SWE-bench run it after the
agent exits) — the agent never needs to know which benchmark it's in, only which
task shape it's facing.

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
between two harnesses. The core stays; four universal layers were added and the
SWE-specific gates deleted.

---

## 2. The four universal layers

All four are language-agnostic. They **replaced** the SWE-specific gates — Layer 0
additionally replaces the incorrect assumption that discovery (Layer 1) alone
absorbs the SWE-vs-terminal difference (see §0).

### Layer 0 — Task-shape classification (new, 2026-07-21 audit)

The agent's FIRST step, **before** onboarding: classify the task into one of three
shapes. This is what actually generalizes the harness across SWE-bench and
Terminal-Bench 2.1 — discovery (Layer 1) does not, on its own (see §0).

| Shape | What it looks like | What replaces the REPAIR-shaped default |
|---|---|---|
| **REPAIR** | Something exists and is broken (a repo, a failing test) | Nothing — the current spine is correct: discover the project's own check, reproduce-first, fix, re-verify. |
| **PRODUCE** | Create an artifact to an exact spec (write a file, render an image, emit a report) — no project to onboard, nothing failing at t=0. Majority of TB2.1 (~85% by the 2026-07-21 audit). | **Spec extraction** replaces reproduce-first: read the task statement and write down explicit acceptance criteria — exact output path, filename, format, field names, value constraints, tolerances. Those criteria become the verify target. |
| **OPERATE** | Make a system actually work (boot a VM, serve on a port, make ssh reachable) | Verify by **exercising** the running thing — curl the endpoint, ssh in, connect the client — never by inspecting config. |

Two mandatory rules apply across shapes (both new, 2026-07-21 audit):

- **Media processing.** Any non-text input — image, video, audio, binary — MUST be
  processed programmatically (PIL / cv2 / numpy / ffmpeg / objdump; install the
  tool if it's absent). Visual inspection may inform a hypothesis but may NEVER be
  the basis of an answer. Motivating case: `chess-best-move` scores 0/3 across our
  runs because the agent reads the board PNG with multimodal vision and reasons
  visually; the task's own reference solution generates piece templates with PIL,
  identifies pieces by pixel MSE, reconstructs FEN, and calls `stockfish` —
  already installed in that container and never invoked. ~8/89 TB2.1 tasks
  involve image/video; zero are solvable by looking.
- **Artifact discipline.** Exact output path, filename, and format are part of
  correctness, not presentation — a right answer at the wrong path scores zero
  (~15/89 TB2.1 tasks grade on exact path/filename/format). Re-read the task
  statement for these before finishing and verify the artifact actually exists
  where specified.

### Layer 1 — Project onboarding / discovery

The agent learns the environment before touching code (prompt: the **ONBOARD**
paragraph). Primarily exercised on REPAIR-shaped tasks — PRODUCE/OPERATE tasks
typically have no project to onboard:

- **Detect stack & commands richest-first:** CI workflows (`.github/workflows`,
  `.gitlab-ci.yml` — the richest source; they list the exact commands maintainers
  run) → config/manifests (`Makefile`, `package.json`, `pyproject.toml`,
  `Cargo.toml`, `go.mod`, `pom.xml`, `CMakeLists.txt`) → lockfiles →
  Dockerfile/README.
- **Provision the task's missing runtime on-the-fly** (`uv`/`pip`/`npm`/`apt`/`apk`)
  when the task's own build needs it — never assume the base image is complete for
  the TASK's stack.
- **Environment-footprint principle (2026-07-21).** The harness itself must not
  mutate the benchmark's environment. The hook layer no longer provisions
  **python3** via `apt-get install` (the old §13 fix); it ships its own
  interpreter under the unerr remote dir instead. It also no longer installs
  `git` or runs `git init` in the task workdir. Consequence, stated plainly:
  `unerr index` degrades on bare-fixture tasks with no repo — by design, not a
  regression (landed 2026-07-21 in `ClaudeUnerrAgent._resolve_pybin` / `install()`;
  see §13 for the mechanism).
- **Cache the discovered `{build, test, run, lint}` commands** to verify against.

### Layer 2 — Discovery-driven verification

A strict anti-false-green contract (prompt: the **FINISH CONTRACT**), none of it
pytest-specific:

- **Bind the done-signal to a command that exercises the actual deliverable**, not
  a regex and not a proxy: for REPAIR, the discovered project check; for PRODUCE,
  a command that reads the produced artifact and validates it against the
  extracted spec (§2, Layer 0); for OPERATE, a command that hits the live system
  (curl the endpoint, ssh in, run the built binary). "Verified" = that command
  exits `0` after the fix — never a restatement of intent.
- **Reproduce-first (REPAIR shape)** — run the chosen check *before* editing and
  confirm it fails the way the task describes. Highest-ROI technique in the
  research (reported to move correct-code tasks ~24% → ~77%); it stops "fixing"
  what isn't broken and yields a grounded before→after. On PRODUCE tasks this step
  is replaced by spec extraction, and on OPERATE tasks by an exercise-first probe
  of the current (broken/absent) state — see Layer 0. (Prompt-enforced: a Stop
  gate can't retroactively see the pre-edit state, so the contract puts this step
  up front regardless of shape.)
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

…leaving one behavior on every benchmark, branching only on task shape (§2, Layer
0 — not a second profile, a branch inside the same prompt and gate set):

`classify-shape → [REPAIR: reproduce-first | PRODUCE: extract-spec | OPERATE:
probe-current-state] → fix/produce/operate → verify-against-marked-command
(exercising the actual deliverable) → escalate-if-stuck-or-unverified`.

Task shape does not fork `cc-harness-hooks.py`: the gates (§4), caps, escalation
(§5), and env toggles (§6) are identical across REPAIR/PRODUCE/OPERATE. Only the
ONBOARD step and what the verify command must exercise change.

---

## 4. The gates (the mechanical finish contract)

Enforced by `cc-harness-hooks.py` via three Claude Code hooks wired into
`.claude/settings.local.json`: a **PreToolUse** deny hook, a **PostToolUse**
record hook, and a **Stop** gate hook. State lives in an append-only JSONL ledger
at `/tmp/cc-harness/state.jsonl` (outside the repo — never in the model patch).

The PostToolUse record hook also best-effort-syncs Claude Code's OWN session
`.jsonl` ($CLAUDE_CONFIG_DIR/projects/**/*.jsonl, default ~/.claude/...) into
`/logs/agent/sessions/claude-session.jsonl` on every call
(`_sync_claude_session`) — Harbor's persisted per-trial log directory. WHY:
Harbor only writes `trajectory.json` when a trial COMPLETES; a task killed
mid-run after exhausting its timeout budget has no `trajectory.json` at all
(root cause: `caffe-cifar-10`, 2026-07-21, killed at
`timeout_sec=3600`). Claude Code appends to its session `.jsonl`
incrementally, so it survives the kill. This piggybacks entirely on the
already-wired PostToolUse hook — no new hook, no new settings.local.json
entry, no `harbor_agents.py` change. See `harness_terminal.py`'s
`_collect_traces` (host-side copy into the artifact bundle),
`tools/benchmarks.py`'s `_TERMINAL["traces"]` (the `claude-session.jsonl` /
`claude_session_jsonl` filename↔column pair), and
[`DEBUG_FAILED_TASK.md`](DEBUG_FAILED_TASK.md) for the full evidence chain.

**Which session, and why it matters (2026-07-21).** Claude Code writes a
SEPARATE session `.jsonl` per Task sub-agent alongside the main agent loop's own
file. `_sync_claude_session` originally took the most-recently-MODIFIED one,
which made the destination FLIP between the main session and whichever sub-agent
last wrote — a transcript that is not a coherent record of any single session
(observed live: synced tool-call count went 41 → 38 while total records rose
112 → 182). It now selects the MAIN session deterministically: filter out
candidates whose records carry `"isSidechain": true`, then take the earliest
first-record `timestamp`. **Not** filesystem `ctime` — that is inode CHANGE time,
and Claude Code appends to the main session all run long, so every append bumps
it forward and a ctime-min tie-break picks whichever session was written LEAST
recently, reproducing the very flip-flop being fixed.

**Sub-agent transcripts (`claude-sessions.tgz`).** Because ESCALATION runs in Task
sub-agents (§5's `unerr-opus`/`unerr-fable` ladder, and all routine delegation),
filtering sidechains out of the main-session file meant escalation was
UNOBSERVABLE — a headline feature with no evidence trail. So every candidate
session is ALSO synced under `<sessionId>.jsonl` (skipped when size+mtime are
unchanged, since this runs on every PostToolUse call), and `_collect_traces` tars
the whole sessions dir into a second artifact. It is carried as
`claude-sessions.tgz.b64` — base64 **deliberately**, because `worker-loop.py`'s
artifact pipeline is text-only and a raw binary `.tgz` would be corrupted by its
`utf-8`/`replace` decode; `coordinator-entrypoint.sh` decodes it back to a real
`.tgz` at drain, the same idiom it already uses for `db_b64`→`opencode.db`.
Session JSONL compresses ~170x in practice, so this stays far under the 5MB
artifact cap.

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
- **Deny N** (anti-tautology, landed 2026-07-21) — a `# unerr:verify`-marked Bash
  command whose ENTIRE body only compares a file the agent wrote *this session*
  against a string literal it chose (`test "$(cat X)" = 'lit'`, `[ "$(cat X)" =
  "lit" ]`, `grep -q 'lit' X`) is denied **once** with a soft nudge — that proves
  the write happened, not that the value is correct; re-issuing the *same*
  command is allowed (evidence-cited override — legitimate whenever the literal
  comes from the task statement, common on PRODUCE-shaped tasks). Soft, one-time,
  capped at 1 denial per run, fail-open — same shape as Deny T, deliberately
  narrow because deciding tautology in general is undecidable. Motivating case:
  TB2.1 `chess-best-move` — the agent wrote its move to a file, then "verified"
  by reading that file back and comparing it to the literal it had just chosen;
  Gate V was satisfied, the move was wrong.

Caps: Z=1, R=1, V=2, overall Z/R/V=3; E is exempt and capped separately by mode.

### Gate efficacy on TB2.1 (code-grounded audit, 2026-07-21)

Five parallel reviews read all 89 real TB2.1 `task.toml` + task files
(`e2e/distributed/out/tb21-tasks/`).

**Infrastructure: sound.** The timeout ceiling absorbs the full measured budget
distribution: 48 tasks @900s, 17 @1800s, 13 @3600s, 5 @1200s, 2 @2400s, one each
@600/750/7200/12000s across all 89 `task.toml`; `_bump_agent_timeout` raises
`[agent]` to a 14400s ceiling and leaves `[verifier]` untouched. Disk guard,
escalation, and trace collection all correct.

**Gates: correct but half-idle on this benchmark.** No gate misfires; all
fail-open; nothing breaks a run. But:

| Gate | Fires on | Why |
|---|---|---|
| Gate R | 17/89 | Needs a marked command that PASSED then FAILED (`cc-harness-hooks.py:565`, `was_ok and not ok`). 72/89 tasks are create-from-scratch with no pre-existing passing check — R is inert there, not broken. |
| Gate V | 89/89, but hollow on most | Each task's real `tests/` are grader-only and HIDDEN from the agent on 83/89. On 76/89 the agent invents its own success criterion, marks it, and passes — ritual, not grounded proof (the exact false-green mode §0 warns about). |
| Deny T | effectively never | Tests hidden on 83/89 — nothing to edit-deny against. |
| Gate caps | as designed | Verified accurate: `OVERALL_CAP=3`, `GATE_CAPS={"Z":1,"R":1,"V":2,"E":1}`, `STUCK_FAIL_THRESHOLD=4`. |

None of this is a bug — the gates do exactly what they're specified to do. The gap
is upstream: without task-shape-aware verification (Layer 0 / Layer 2), the agent
has nothing grounded to mark on the majority of PRODUCE-shaped tasks. Fixing the
prompt (§9) is what fixes Gate V's hollowness, not a gate-logic change.

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

The harness itself is **always on** for every `claude-*` arm — there is no
runtime opt-out. Gating happens upstream only: `CLAUDE_ARM_KIND`
(`run-distributed.sh`, which arm/mix runs at all) and `HARNESS_ON`
(`run-instance.sh`, the SWE flow's own on/off switch). `cc-harness-hooks.py`
itself now reads exactly one runtime toggle:

| Env var | Values | Meaning |
|---|---|---|
| `ESCALATION_PANEL` | `1` = panel; unset/`0` = ladder | Gate E shape (see §5). The ONLY runtime toggle the harness itself reads. |
| `TERMINAL_STOCK_AGENT` | `1` = bare first-party claude-code agent | **The only true no-harness baseline control** (terminal flow): uses Harbor's stock agent and never calls our `install()` — no unerr install, no index, no daemon, no MCP, no autonomy prompt. No default. |
| ~~`HARNESS_HOOKS`~~ | — | **REMOVED (2026-07-22).** Used to gate the mechanical gates + deny rules + escalation ledger on/off; the hooks now run unconditionally and the var is no longer read anywhere, including the hook-command `env` prefix (which forwards only `ESCALATION_PANEL` now — see §7/§9). |
| ~~`HARNESS_PROFILE`~~ | — | **RETIRED.** Accepted for env-wiring compat but never read; legacy `swe`/`generic` values are ignored. |

> **Bare-agent baseline (2026-07-22).** With `HARNESS_HOOKS` gone there is no
> env-flag way to get a bare baseline anymore. Build the toolbox image from a
> git checkout taken BEFORE the harness landed instead — that gets you a
> container with no unerr install, no hooks, no autonomy prompt, full stop.
> `TERMINAL_STOCK_AGENT=1` remains the terminal-flow control that bypasses our
> `install()` entirely on the SAME (current) checkout. *(Historical note:
> `HARNESS_HOOKS=0` was once mistakenly documented here as a "bare-agent
> baseline" — it only skipped writing `.claude/settings.local.json`; the
> container still got the full unerr install, index, daemon, and autonomy
> prompt. The variable no longer exists, so that mislabeling can't recur.)*

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
        │                        register unerr MCP
        └─ install()          → stage unerr (npm tgz + entitlement + index + daemon + agents,
                                 bundled python3 interpreter — no apt mutation, §2 Layer 1),
                                 then write .claude/settings.local.json (PreToolUse auto-approve +
                                 deny + record + Stop gate). Empty --model from harness_terminal.
```

### Gateway routing tier map

| Claude tier | Host env var | `claude-gpt` (GPT-5.6) example |
|---|---|---|
| main loop (conductor) | `ANTHROPIC_MODEL` — pinned to the **SONNET** alias (`harbor_agents.py` `unerr_main_model` `env_fallback="ANTHROPIC_DEFAULT_SONNET_MODEL"`) | `openai/gpt-5.6-terra` |
| sonnet (mid-tier sub-agents, e.g. `unerr-worker`) | `ANTHROPIC_DEFAULT_SONNET_MODEL` (same tier as the conductor) | `openai/gpt-5.6-terra` |
| opus (escalation rung 1) | `ANTHROPIC_DEFAULT_OPUS_MODEL` | `openai/gpt-5.6-sol` |
| haiku (cheap sub-agents, e.g. `unerr-junior`) | `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `openai/gpt-5.6-luna` |
| fable (escalation rung 2) | `ANTHROPIC_DEFAULT_FABLE_MODEL` | `openai/gpt-5.6-sol-high` |

> **Conductor model (2026-07-22):** the main loop rides the **sonnet** tier
> (gpt-5.6-terra) — a valid non-empty model, vs the missing `claude-opus-4-8`
> default. It was briefly repointed to the OPUS alias (sol) earlier the same day
> and then **reverted to sonnet** before running, so the OPUS tier (sol) stays
> reserved for opus-tier escalation (rung 1) and the `unerr-opus` sub-agent.
> Changing it is a one-line `env_fallback` flip in `harbor_agents.py` + a rebake.
> `--model` is still empty — a concrete `--model` would trigger Harbor's
> tier-flatten (see fix #2 below) and collapse the ensemble.

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
   `shlex.quote`'d as a single token (`import shlex`).

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
  container — including its own bundled python3 interpreter, never `apt-get
  install` (environment-footprint principle, §2 Layer 1) — then writes the
  settings file to BOTH the project-relative path AND `$HOME/.claude/`, asserting
  existence + JSON validity in-command (via `exec_as_agent`).

The gate script `cc-harness-hooks.py` is the **same file** for both flows (uploaded
by the terminal flow, COPY'd by the SWE flow); editing it once covers both.

---

## 9. The prompt — two byte-identical sites + maintenance rule

The autonomy/finish prompt is authored in **two** places that must stay in sync,
plus the gate messages that must stay consistent with them:

1. **`harbor_agents.py:_build_autonomy_prompt()`** — Python, terminal flow.
2. **`run-instance.sh`'s inline prompt-assembly block** (plain bash variable
   interpolation, not a `cat <<EOF` heredoc) — SWE flow.
3. **The gate block messages in `cc-harness-hooks.py`** — shown when a gate fires.

**Maintenance rule:** the ON-harness block (TRACK → ONBOARD → FIX DISCIPLINE →
DELEGATION → ESCALATION → FINISH CONTRACT) in (1) and (2) MUST be **byte-identical**;
any wording change is made in both in the SAME change and re-verified identical.
The gate messages (3) must stay semantically consistent with what (1)/(2) tell the
agent to expect. *(The BASE line legitimately differs per flow — terminal
`harbor_agents.py:120` "Resolve the task directly" vs SWE `run-instance.sh:351`
"…by editing the repository's source files directly" — and the SWE flow's OFF
baseline uses that BASE line unchanged.)*

Verify byte-identity with `python3 e2e/distributed/tools/check_prompt_parity.py`
— a real, runnable checker (this used to say "the scratch check", an
ad-hoc, unversioned, never-actually-run step; see the drift below for why
that failed). It renders both sites for real, not by regex-slicing rendered
text: for (1) it imports `harbor_agents.py` and calls
`_build_autonomy_prompt()` directly; for (2) it locates the prompt-assembly
fragment inside `run-instance.sh`'s `HARNESS_ON` branch by unique content
anchor (that block is plain bash variable interpolation, not a `cat <<EOF`
heredoc, despite earlier wording here) and hands the literal extracted
source to a real `bash -c` subprocess so bash itself does the
interpolation — then slices both at the shared `TRACK —` marker and diffs.
It checks BOTH escalation shapes — ladder (`ESCALATION_PANEL` unset/0) and
panel (`ESCALATION_PANEL=1`) — since checking only the default is exactly
how the drift below survived undetected. Exit 0 with a per-variant char
count on match, non-zero with a unified diff on mismatch; no fly/docker/
network dependency. Also wired into `pytest
e2e/reference/claude/local-docker/tests/` via `test_prompt_parity.py` (this
repo has no other CI/pre-commit/lint hook to wire it into instead).

**Panel drift found + fixed (2026-07-21).** The `ESCALATION_PANEL=1`
paragraph had silently drifted ~5 words between the two sites despite
`harbor_agents.py`'s own docstring calling it a "frozen contract ...
byte-identical — never re-word it": `run-instance.sh` said
"issue"/"sites"/"patch proposal"/"problem" where `harbor_agents.py` said
"task"/"approaches"/"proposal"/"task". Reconciled onto `harbor_agents.py`'s
wording: the terminal flow it serves often has no repository at all, so the
generic "task"/"approaches"/"proposal" phrasing is correct for BOTH flows,
whereas `run-instance.sh`'s repo-specific "issue"/"sites"/"patch" words are
wrong for a no-repo TB2.1 task. The ladder (default) variant was already
identical at 6207 chars and stays so; the panel variant now matches too, at
6141 chars. The BASE lines (this section's own opening) were left
untouched, per the standing rule above.

**Landed (2026-07-21) — task-shape branch + anti-tautology nudge.** The ONBOARD
paragraph (Layer 0, §2) now leads with a SHAPE classification step (REPAIR /
PRODUCE / OPERATE) plus the media-processing and artifact-discipline rules — new
SHARED content inside the byte-identical block, not a per-flow BASE-line fix; the
two BASE lines were deliberately left DIFFERENT per the standing rule above (the
SWE phrasing "…by editing the repository's source files directly" is correct for
SWE-bench and WRONG for most TB2.1 tasks, which have no repository to edit).
Landed together in `harbor_agents.py:_build_autonomy_prompt()` AND
`run-instance.sh`'s heredoc AND the gate block messages in `cc-harness-hooks.py`
(§4) — the gate messages previously assumed a REPAIR-shaped task ("a
previously-passing test was regressed", "issue text") and were reworded to make
sense for PRODUCE/OPERATE too. A new Deny N (anti-tautology, §4) plus a matching
FINISH CONTRACT sentence landed in the same change — see §12 for the full list.

**Landed (2026-07-22) — D3 discipline bullet.** The FIX DISCIPLINE block gained
a fourth bullet, **"Install by the canonical path"**: when a task pins a
framework or version, follow that project's own documented build/install steps
(the apt/pip/uv/npm path it prescribes, whichever the project uses) rather than
improvising — a missing runtime means install it, never ship blind. Landed
byte-identical in both `harbor_agents.py:_build_autonomy_prompt()` and
`run-instance.sh`'s prompt-assembly block, parity-verified.

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
                                        # gates + escalation ledger run unconditionally now —
                                        # no env flag to set (§6)

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
  valid JSON — the gates run unconditionally now, so a missing/invalid file is
  the only way they go dark.

---

## 11. File map

| File | Role |
|---|---|
| `e2e/reference/claude/local-docker/context/cc-harness-hooks.py` | The gates + sensor + escalation ladder/panel (single universal profile) + `_sync_claude_session` (session-transcript sync). Ships a `--selftest`. |
| `e2e/reference/claude/local-docker/tests/test_cc_harness_hooks.py` | Unit suite locking gate behavior + session-sync behavior |
| `e2e/distributed/tools/harbor_agents.py` | `ClaudeUnerrAgent` (fixes 1, 3, 4, 6) + `_build_autonomy_prompt` + `_hooks_settings_command` + bundled-interpreter staging (env-footprint principle, §2 Layer 1) |
| `e2e/distributed/tools/harness_terminal.py` | `_arm_agent_config` (fix 2 + gateway/auth wiring); `_collect_traces` (host-side artifact staging, incl. `claude-session.jsonl`) |
| `e2e/reference/claude/local-docker/context/run-instance.sh` | SWE flow: prompt + hook settings writer (mirrors the terminal flow) |
| `e2e/reference/claude/local-docker/run-benchmark.py` | Forwards env vars into the SWE instance container |
| `e2e/distributed/run-distributed.sh` | Launcher: resolves `CLAUDE_ARM_KIND` (which arm/mix gets the always-on harness); forwards `ESCALATION_PANEL` + tier env to workers |

---

## 12. What landed (2026-07-21 → 2026-07-22)

- `cc-harness-hooks.py`: `_profile()` → `None`|`universal`; deleted the swe record/
  gate/deny branches, `TEST_CMD_RE`/`is_broad_test`, and Rule C; softened Rule T to a
  one-time override-able nudge; added `STUCK_FAIL_THRESHOLD=4` stuck trigger to Gate
  E rung-1. `--selftest`: **28/28 PASS**.
- `harbor_agents.py`: dropped the `profile` axis; universal `_build_autonomy_prompt`
  + ONBOARD; python3 provisioning in `install()` (superseded 2026-07-21 by the
  environment-footprint principle, §2 Layer 1 / §13); `HARNESS_PROFILE` fully
  removed.
- `run-instance.sh`: unconditional universal prompt fragments; OFF baseline
  untouched; converged onto the general wording so the ON-harness block is
  **byte-identical** to the harbor copy (4595 chars).
- `run-distributed.sh`: `HARNESS_HOOKS=1` default for every claude-* benchmark (no
  more family-dependent `1`-vs-`generic`); `ARM=econ` never defaulted.
  *(Superseded 2026-07-22 — `HARNESS_HOOKS` itself is retired; see the entry
  below.)*
- **econ untouched** — verified: 0 econ files in the diff; the `CLAUDE_ARM_KIND`
  guard keeps econ out of the defaulting; econ has its own `run-instance.sh`.
- **Claude Code session-transcript collection** (2026-07-21, closes the
  `caffe-cifar-10` blind spot): `cc-harness-hooks.py`'s PostToolUse `record`
  now also syncs Claude Code's own session `.jsonl` into
  `/logs/agent/sessions/claude-session.jsonl`; `harness_terminal.py`'s
  `_collect_traces` stages it into the artifact bundle; `benchmarks.py`'s
  `_TERMINAL["traces"]` declares the `claude-session.jsonl` /
  `claude_session_jsonl` filename↔column pair; `coordinator/server.py` and
  `coordinator-entrypoint.sh` carry the new column end-to-end
  (`/complete` → `queue.db` → drain-time artifact write-out). No new hook,
  no `harbor_agents.py` change — see §4 and `DEBUG_FAILED_TASK.md`.
- **Code-grounded audit against real TB2.1 tasks (2026-07-21).** Five parallel
  reviews read all 89 `task.toml` + task files at
  `e2e/distributed/out/tb21-tasks/`. Findings: infra sound (timeout ceiling
  absorbs the full budget distribution); gates correct but half-idle on this
  benchmark (Gate R fires on 17/89, Gate V's grounding is hollow on 76/89, Deny T
  effectively never fires — see §4); the real gap is the PROMPT, which assumes a
  REPAIR-shaped task that only ~15% of TB2.1 matches. Produced the task-shape
  classification (§2 Layer 0), the media-processing and artifact-discipline
  rules, the bare-baseline correction (§6, later superseded 2026-07-22 when
  `HARNESS_HOOKS` itself was retired), and the environment-footprint principle
  (§2 Layer 1). The prompt/gate-message changes themselves were landed in a
  follow-up change — see the next entry and §9.
- **Task-shape branch + anti-tautology nudge landed (2026-07-21, closes §9's
  pending item).** `harbor_agents.py:_build_autonomy_prompt()` and
  `run-instance.sh`'s heredoc both gained a SHAPE step (REPAIR/PRODUCE/OPERATE
  classification, spec-extraction guidance for PRODUCE, exercise-first guidance
  for OPERATE, the media-processing rule, and the artifact-discipline rule) ahead
  of ONBOARD, plus a FINISH CONTRACT sentence stating the anti-tautology
  principle — landed byte-identical in both sites (re-verified: 6207 chars,
  ladder/PANEL=0 shape). `cc-harness-hooks.py` gained **Deny N** (§4): a soft,
  one-time, capped (1/run), fail-open nudge on a `# unerr:verify`-marked Bash
  command whose whole body only compares a file the agent wrote *this session*
  against a string literal it chose — the exact false-green mode behind TB2.1
  `chess-best-move` (the agent verified its own written answer against a literal
  it picked itself, wrong move, Gate V satisfied anyway). Deliberately narrow and
  override-able (re-issuing the SAME command is allowed) since a literal
  comparison is legitimate whenever the expected value comes from the task
  statement (PRODUCE-shaped tasks) — deciding tautology in general is
  undecidable. The PreToolUse deny hook's matcher gained `Bash` (was
  Edit/Write/MultiEdit/mcp__unerr__file_edit only) in both
  `harbor_agents.py:_hooks_settings_command` and `run-instance.sh` so Deny N can
  actually fire; Rules T/B stay no-ops on a Bash call's empty `file_path`. The
  existing Gate Z/R/V/E and Deny T/B messages were also reworded off
  REPAIR-only phrasing ("a previously-passing test was regressed" → "a
  previously-green verification regressed"; "issue text" → "task text"; "no
  fixed test layout" → "no fixed check layout"; etc.) so they read correctly on
  PRODUCE/OPERATE tasks. Tests: `test_cc_harness_hooks.py` 28/28 PASS (6 new
  Rule N cases); `--selftest` 32/32 PASS (4 new Rule N cases).
- **`HARNESS_HOOKS`/`HARNESS_PROFILE` fully removed — harness always-on
  (2026-07-22).** The mechanical gates now run unconditionally on every
  `claude-*` arm; there is no runtime opt-out anymore. Gating is upstream-only:
  `CLAUDE_ARM_KIND` (`run-distributed.sh`, which arm/mix gets the harness at
  all) and `HARNESS_ON` (`run-instance.sh`, the SWE flow's own on/off switch)
  — never an env value `cc-harness-hooks.py` itself reads. The hook-command
  `env` prefix now forwards only `ESCALATION_PANEL` (§6/§7) — `HARNESS_HOOKS`
  is no longer spliced into it, and the `__init__` bare-token guard against it
  is gone with the var. A true bare-agent baseline is now obtained by building
  the toolbox image from a git checkout taken BEFORE the harness landed, not
  by an env flag — see §6. `run-distributed.sh`'s prior per-arm
  `HARNESS_HOOKS=1` default (the 2026-07-21 entry above) is retired along with
  the variable.
- **No-result-death auto-retry (2026-07-22).** A terminal Harbor trial that
  produces NO gradeable `result.json` — an idle-watchdog/timeout kill or a
  crash before grading — is now flagged by `harness_terminal.py`'s `run()`
  (`meta["no_result_death"] = True`) and picked up by the coordinator's
  existing failure-rerun path (`server.py`'s `_is_no_result_death_meta` +
  `_eligible_rerun_ids`), auto-retried once at the SAME budget as the existing
  `silent_death` rerun (§4). A clean run — even a wrong one — ALWAYS writes
  `result.json`, so a genuine capability miss (e.g. `chess-best-move`, §2)
  is never rerun by this path.

Landing a change to these files needs a **rebake** (`harbor_agents.py`,
`cc-harness-hooks.py`, `harness_terminal.py`, `benchmarks.py`,
`coordinator/server.py`, `coordinator-entrypoint.sh` are all in the
`Dockerfile.dist` / `Dockerfile.toolbox` COPY set — a runtime-env change
alone does not pick up an edit to any of them) and a **re-prepare**
(HARNESS_* are runtime env resolved at worker-machine creation).

---

## 13. Open items

- **python3-missing on heterogeneous terminal base images (old #32) — superseded,
  LANDED 2026-07-21.** The original fix (`apt-get install python3` in `install()`)
  is replaced by the environment-footprint principle (§2, Layer 1): the harness
  ships its own interpreter instead of mutating the task image via `apt-get`.
  Same effect (gates run on every image; ~30% of a terminal suite was previously
  lost to this gap — hook validator ran `python3 -m json.tool` and died when
  python3 was absent → task never launched, $0, 0-byte trajectory) without
  touching the benchmark's environment. Mechanism:
  `ClaudeUnerrAgent._resolve_pybin()` is the single decision point — it prefers an
  already-present system `python3`/`python` (cheap, no upload) and only when the
  image has neither does it upload + extract the vendored
  `python-build-standalone` CPython into `{UNERR_REMOTE_DIR}/py/`, yielding
  `…/py/python/bin/python3`. No apt/apk/yum, no network from inside the task
  container, no writes outside `UNERR_REMOTE_DIR`. It is a HARD gate (raises like
  the unerr-tgz check) — a task with no working interpreter produces invalid gate
  data, which is worse than a loud failure. The resolved path is baked into the
  hook `command` strings and the `json.tool` validator as a LITERAL by
  `_hooks_settings_command(…, pybin)` rather than re-resolved with `command -v` at
  hook time, since a Claude Code hook subprocess is not guaranteed to inherit this
  session's PATH. The tarball is vendored into `context/` by `build-toolbox.sh`
  (idempotent fetch, pinned via `PY_STANDALONE_RELEASE`/`PY_STANDALONE_PYVER`,
  gitignored) — it is a build-context artifact, so a bump needs a **rebake**.
- **The vendored interpreter is a gitignored build artifact — a clean checkout
  bakes a BROKEN image. Guarded 2026-07-21 after it killed a live run.** Because
  `_resolve_pybin()` treats a missing interpreter as a HARD gate (correctly), and
  because the tarball is gitignored, a bake from a checkout where nobody has run
  `build-toolbox.sh` produces an image that hard-fails EVERY task whose base image
  lacks python3 (~30% of a terminal suite). It fails at SETUP, minutes into a paid
  fly run — `Trial <id> failed: no system python3/python in the task image AND no
  vendored python-build-standalone tarball found` — not at preflight. This is
  exactly what happened to `caffe-cifar-10` on run `cgpt-caffe-jsonl2-terminal`
  (dead at 7m11s); the run before it survived only because it reused a cached
  toolbox layer that still held the tarball.
  Two fixes, both landed: **(a)** `build-toolbox.sh` gained `--vendor-only` /
  `VENDOR_ONLY=1`, which runs ONLY the artifact vendoring and skips the unerr
  rebuild + `docker build`. Use it — a full `build-toolbox.sh` run would repack the
  vendored unerr tgz from whatever is currently in the sibling `unerr-cli`
  checkout, silently changing which unerr build the benchmark measures.
  **(b)** `run-distributed.sh` gained a preflight that hard-fails BEFORE the bake
  when the tarball is missing, printing the remediation command. Gated on a
  `claude-*` arm with no `IMAGE=` reuse and `TERMINAL_STOCK_AGENT != 1`, so `econ`,
  pinned-image runs, and the bare-baseline control are unaffected. On success it
  prints `==> vendored python-build-standalone: <file>`.
- **`unerr index` degrades on bare-fixture (no-repo) tasks — by design, not a
  regression.** The same environment-footprint principle drops the harness's own
  `git` install / `git init` in the task workdir. On the ~85% of TB2.1 that are
  bare fixtures with no pre-existing repo, `unerr index` now has nothing to index.
  Accepted trade-off rather than mutating the task environment to work around it.
- **Coverage gap: only 10/89 TB2.1 tasks have ever been run — suite now exists,
  still needs to be RUN.** `terminal-mini` is the first 5 ids alphabetically
  (`_MINI_SMOKE_N=5`, `benchmarks.py:345`) and contains ZERO image/media tasks —
  every smoke to date has been blind to that family (~8/89 tasks; see §2 Layer
  0's media rule). `terminal-mini` is kept as-is (existing runs/docs reference
  it); a second, DELIBERATE 10-id suite —
  `SUITE=terminal-coverage`/`benchmarks._TERMINAL_COVERAGE_SAMPLE`
  (`benchmarks.py`) — was added 2026-07-21, hand-picked FROM the real vendored
  task files (`out/tb21-tasks/`, task.toml + instruction.md read per id, not
  guessed from names) to span all three shapes (>=1 OPERATE — `qemu-alpine-ssh`,
  `install-windows-3.11`; a REPAIR/PRODUCE spread — `fix-git`,
  `fix-code-vulnerability` vs `cobol-modernization`, `code-from-image`, etc.), the
  media rule (4 image/video tasks — `code-from-image`, `financial-document-processor`,
  `sam-cell-seg`, `video-processing`), and the timeout-budget distribution
  including both rare outliers (`sam-cell-seg` @7200s, `build-pov-ray` @12000s —
  the single longest budget in the set). See distributed README §8.2. Still
  open: it has not yet been RUN — don't trust aggregate TB2.1 numbers until it
  has.
- **`status.sh` / `debug-workers.sh` can't enumerate the `claude-gpt × terminal`
  fleet (old #31) — FIXED.** `fleet-common.sh` now exposes `fc_resolve_arm`
  (explicit `$ARM` env > label inference, both scripts wired) which tags every
  resolution `env`/`label`/`guess`; a fall-through guess that finds no
  coordinator/workers now prints a `hint: arm inferred as '<arm>' from LABEL ...`
  line to stderr instead of silently asserting a live fleet is torn down. See
  distributed README §3.

---

## 14. Provenance

Designed + implemented 2026-07-21 from a four-part research sweep (minimal-loop /
environment-onboarding / verification / robustness) plus a code-grounded map of the
prior gates, and consolidating the two retired docs (`HARNESS_PROFILES.md`,
`HARBOR_CLAUDE_CODE.md`). Motivating evidence: the `claude-gpt × terminal`
dogfood-10 (2026-07-20) — 5/10 resolved, $3.24 real GPT-5.6 spend, escalation
proven live on build-cython-ext, 3 losses were the python3 infra gap (#32), not
capability.

A second, deeper code-grounded audit (2026-07-21, five parallel reviews against
all 89 real TB2.1 task files at `e2e/distributed/out/tb21-tasks/`, see §12's dated
entry) found the same-day design above was still SWE-shaped in one respect: it
assumed discovery collapses SWE-vs-terminal, which holds for REPAIR-shaped tasks
but not the ~85% of TB2.1 that are PRODUCE/OPERATE-shaped (§0, §2 Layer 0). That
audit is the source of the task-shape classification, the media and
artifact-discipline rules, the gate-efficacy table (§4), and the bare-baseline
correction (§6, superseded 2026-07-22 — see §12).

Related memory notes: `claude-gpt-terminal-dogfood10-result`,
`harness-hooks-python3-missing`, `append-prompt-shell-quoting-bug`.
