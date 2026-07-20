# Harness profiles — what `generic` is, what `swe` still buys, and the research behind it

A plain-language explainer for the profile-driven Claude Code harness (2026-07-20).
The terse operator reference is [HARBOR_CLAUDE_CODE.md §3.5–3.6](HARBOR_CLAUDE_CODE.md);
this doc is the *why* — written so someone new to the repo can follow the whole
story. Everything here was verified against the code and the TB2.1 dataset on
2026-07-20; file pointers are given so you can re-check any claim.

---

## 1. The problem, in one story

Our Claude Code benchmark arms carry a "harness": a set of hooks that watch the
agent work and mechanically block bad finishes. It exists because polite prompt
instructions ("verify before you finish", "escalate when stuck") measurably
under-fire — the header of `cc-harness-hooks.py` says exactly that. So we made
the rules machine-checked:

- **Gate Z** — you can't finish if you did nothing.
- **Gate V** — you can't finish if you never verified your change.
- **Gate R** — you can't finish if something that passed before now fails.
- **Gate E** — if verification keeps failing, you must escalate to the
  stronger models (`unerr-opus` + `unerr-fable`) before finishing.
- **Deny B** — stop thrash-editing the same file; escalate instead.

Machine-checking needs *sensing*: the harness must recognize, from raw Bash
commands, that "a verification ran and passed/failed". The original sensor was
a regex for the world the harness grew up in — SWE-bench, i.e. Python repos:
`pytest`, `manage.py test`, `tox`, `bin/test`, `repro*.py`
(`cc-harness-hooks.py`, `TEST_CMD_RE`). Over time more world-knowledge accreted:
`tests/` directories are read-only, `-k`/`::test_x` means a "narrow" run,
`django/test/` is product code not tests, don't introduce `datetime.now()` into
a `utcnow` file.

That works great — **on SWE-bench**. But in production (and on Terminal-Bench)
you don't know the language or framework in advance. A harness that recognizes
verification only when it looks like pytest is blind everywhere else.

## 2. What the research found (2026-07-20)

Four parallel investigations (harness internals, the TB2.1 dataset on disk,
TB2.1 primary sources, and the arm×benchmark wiring) produced four load-bearing
findings:

**Finding 1 — the single-sensor collapse.** The gate *skeletons* are generic
(counters, state machines), but every gate transitively depends on the one
SWE-shaped sensor: V and R read it directly; E fires only off V/R block
counters; B requires a prior V/R block. Only Z is independent — and Z only
counts tool-mediated edits, so an agent that builds its solution through Bash
(`make`, `sed`, redirects) looks idle to it. Kill the sensor's input and the
whole stack goes dark. That is precisely what Terminal-Bench does.

**Finding 2 — TB2.1's tests are invisible to the agent.** All 89 TB2.1 tasks
are *graded* by pytest (`tests/test_outputs.py` → `/logs/verifier/reward.txt`),
but the verifier mounts `/tests` **after the agent finishes**. Only **1 of 89**
tasks (`break-filter-js-from-html`) bakes the real test file into the agent's
container; leaderboard rules forbid peeking. So "re-run the existing test
module" — Gate V's demand — is not merely miscalibrated on terminal, it is
**impossible**: there is no test module in the agent's world. Verified
directly against `out/tb21-tasks` (89 `environment/Dockerfile`s, 4 reference
test assets, 1 real exposure).

**Finding 3 — the suite is far wider than SWE-bench.** TB2.1 spans 16
categories (software engineering is the largest at 26/89 but not a majority;
the rest is sysadmin, security, scientific computing, data science, ML,
games, video processing…) and many stacks (C ×13, C++, Rust, R, COBOL,
JS/TS…). Every fixture is build/operate-from-scratch — zero git-repo
checkouts. Nothing about the SWE world-model transfers.

**Finding 4 — the consequence was binary-bad.** With hooks off (the old
terminal default) the gates are inert — which is why escalation *never* fired
on any terminal run (opus/fable spend $0 on every run to date). With hooks
naively on, Gate V blocks every finish (no test module exists), hits its cap,
and Gate E then forces escalation on **every** task — decoupled from need in
the opposite direction. Neither mode correlates with difficulty.

**Bonus finding — the fix only has to land once.** The arm wiring is uniform:
`claude-gpt`, `claude-open`, and `claude-native` stage the exact same harness
components on each flow. Arms differ only in auth/model/cost. So the harness
profile is a **per-benchmark** choice that applies to **all claude arms
identically** — there is no per-arm work, ever.

## 3. The fix: two profiles, one gate stack

The gates stay exactly as they are. What changes is the *sensor* feeding them,
selected by `HARNESS_PROFILE`:

### `generic` — sense the agent's own verification (works anywhere)

The insight comes from TB2.1's own grader: it doesn't know languages either —
it just runs *an arbitrary command* and reads the exit status. The generic
profile applies the same idea agent-side:

1. **Outcome ledger.** Every Bash command the agent runs is recorded with its
   exit status. No framework recognition at all.
2. **Declared verification.** The prompt tells the agent: *decide the command
   that proves success for THIS task* — a build-and-run, a script you write, a
   `curl`, a `diff` — *and append the marker comment `# unerr:verify` to it.*
   A shell comment is harmless in any stack; the hook spots the marker.
3. **The same gates, re-fed:**
   - **V** — you can't finish until a marked check has succeeded, and again if
     you edit after your last green check (2 blocks → E).
   - **R** — a marked check that once passed and now fails blocks you.
     (Unmarked failures never count — `grep` exiting 1 is not a regression.)
   - **E** — unchanged: repeated V/R trouble forces the opus+fable escalation.
     On terminal this now *actually fires*, and only under genuine struggle.
   - **B** — thrash counting keyed to "since your last green marked check".
   - **Z** — relaxed to "no edits AND no successful command at all", so
     Bash-built solutions aren't punished.
4. **What generic deliberately drops:** the test-file read-only rule (on
   unknown tasks the agent *should* write its own checks — denying
   `test_*.py` writes would sabotage the correct strategy) and the Python
   `datetime` convention rule (meaningless outside Python). These aren't
   "missing"; they're SWE-world rules that don't belong in a world-agnostic
   core.

The honest trade-off: generic depends on the model following one prompt
instruction (mark your check). But unlike the old advisory prose,
non-compliance is *visible and enforced* — Gate V blocks the finish and its
block message teaches the marker protocol — the same forcing-function
principle that fixed under-firing on SWE-bench.

### `swe` — the specialization, kept byte-identical (what it still buys)

When we KNOW the world is a Python repo with a test suite (SWE-bench
verified/lite/pro/live_verified), the swe profile is strictly stronger,
because it senses without needing the agent's cooperation:

- **Zero-cooperation sensing** — it recognizes `pytest`/`tox`/`manage.py test`
  runs automatically; the agent can't forget to mark anything.
- **Broad-vs-narrow discrimination** — it knows `pytest -k one_test` is not
  real verification and demands the full module; generic can't tell a strong
  marked check from a weak one.
- **Anti-cheat** — test files are mechanically read-only, so a model can't
  "fix" a task by editing its tests. Generic has no equivalent (and must not:
  see above).
- **Convention guards** — e.g. the `datetime.now()`-into-`utcnow`-file deny,
  learned from real SWE-bench regressions.
- **Continuity** — the swe path is byte-identical to the pre-profile harness
  (proven against `git show HEAD` output comparison + the 41-assertion
  selftest), so every existing SWE-bench baseline stays comparable.

Think of it as: **generic is the default core for unknown worlds; swe is an
overlay of extra knowledge we apply only where that knowledge is true.** The
production posture is inverted from before: framework assumptions are now the
opt-in exception, not the foundation.

## 4. Which profile, where (policy)

| Situation | Profile | Why |
|---|---|---|
| Terminal-Bench 2.1 — all claude arms | `generic` (`HARNESS_HOOKS=generic`) | Tests are hidden from the agent in 88/89 tasks; swe sensing reads nothing |
| SWE-bench family — all claude arms | `swe` (flow hard-enables it; default) | Known Python-repo world; stronger sensing + anti-cheat + baseline comparability |
| Any NEW benchmark / unknown stack | `generic` first | Never assume the world; specialize only with evidence |
| A/B measuring what swe knowledge buys | `generic` on SWE-bench, labeled run | Allowed, but never comparable to `swe` baselines — label it |

Arms never change the answer: profile is per-benchmark, identical across
`claude-gpt` / `claude-open` / `claude-native`.

**Current defaults (nothing flipped yet):** SWE-bench flow = hooks hard-on,
profile `swe`. Terminal = hooks OFF until you pass `HARNESS_HOOKS=generic` at
the launcher. The terminal default flips to `generic` only after the smoke
sequence below proves it live.

## 5. How to run it

```bash
# terminal combo with the generic gates (any claude arm):
HARNESS_HOOKS=generic ARM=claude-gpt BENCHMARK=terminal ./bench.sh ...

# kill switch — all gates off:
unset HARNESS_HOOKS        # (or HARNESS_HOOKS=0)

# SWE-bench: nothing to do — the flow hard-enables profile swe.
```

Contract recap: `HARNESS_HOOKS` unset/`0` = off · `1` = on with profile from
`HARNESS_PROFILE` (default `swe`) · `generic` = on with the generic profile.
The profile value is inlined into the hook commands in
`.claude/settings.local.json` by both writers, so hook processes see it
deterministically.

## 6. Escalation: ladder (default) vs panel (opt-in)

Gate E forces escalation when verification keeps failing. The **escalation mode**
controls HOW it escalates — a crucial detail when the second opinion can cost
2–5× the main agent's tier.

### The two modes

| Mode | Env value | Behavior | When to use |
|---|---|---|---|
| **Ladder** | unset / `"0"` (DEFAULT) | Rung 1: spawn `unerr-opus` alone with the evidence brief (hypothesis withheld). If trouble PERSISTS after opus's proposal is implemented (a new V or R block), Rung 2: spawn `unerr-fable` with opus's proposal and exactly why it failed. Max 2 escalation rounds. | Default for all arms; explicitly recommended on `claude-gpt` where opus/fable are the same family at different reasoning effort |
| **Panel** | `"1"` | Spawn `unerr-opus` AND `unerr-fable` **in parallel** (one message, two Task calls), same brief to each, hypothesis withheld for independent reads, then reconcile. Agreement = confidence signal; disagreement = evidence is ambiguous, prefer the verdict explaining ALL evidence + a definition-site fix. | `claude-open` where opus (deepseek-pro) and fable (glm) are distinct families; cost is justified by model diversity |

### Per-arm recommendation

The panel's value depends on whether the two tiers are genuinely independent readers:

| Arm | Tier map | Recommendation |
|---|---|---|
| `claude-open` | opus = deepseek-pro, fable = glm | **`ESCALATION_PANEL=1`** — distinct families make agreement a strong signal |
| `claude-gpt` | opus = gpt-5.6-sol, fable = gpt-5.6-sol-high | **Ladder (default)** — same family at different reasoning effort; panel doubles the most expensive tier for little diversity |
| `claude-native` | opus = real Anthropic Opus, fable = Fable | Ladder is cheaper default; enable the panel deliberately when run value justifies the cost |

### Why the ladder is gate-driven, not model-chosen

Choosing a tier by self-judged complexity re-introduces the model's unreliable
self-assessment, which is the exact failure that made Gate E machine-checked in
the first place. The ladder's rung is decided by gate state (a V/R block did not
resolve after the first escalation), not by the model's opinion of the task.

### How to enable

Panel mode is set at the launcher via the `ESCALATION_PANEL` env var:

```bash
# claude-open arm with panel mode (independent reads, reconcile):
ESCALATION_PANEL=1 ARM=claude-open BENCHMARK=verified ./bench.sh ...

# claude-gpt arm — ladder is the default (no env needed):
ARM=claude-gpt BENCHMARK=terminal ./bench.sh ...

# explicit ladder (equivalent to unsetting the var):
ESCALATION_PANEL=0 ARM=claude-native BENCHMARK=live_verified ./bench.sh ...
```

The value is inlined into the gate-hook commands in `.claude/settings.local.json`
by `run-distributed.sh` and `run-benchmark.py` (only when set), and read by the
gate hook on escalation.

### Live evidence (gentb-0720a smoke, claude-gpt × terminal, 2026-07-20)

The generic profile's escalation fired for the first time on a terminal run
(prior 7 terminal runs showed $0 opus/fable spend). With ladder mode, opus
billed $0.39 → $1.05 (the cost of the first escalation rung), while fable
stayed at $0.00 — evidence that the model was already honoring the "inspect
gate state to decide rung" logic, not the panel's "spawn both in one message"
instruction. The single-rung behavior was de-facto even when intended as panel.

---

## 7. Validation status

- **Done:** 41/41 legacy selftest (swe untouched); 13/13 new pytest cases
  (`e2e/reference/claude/local-docker/tests/test_cc_harness_hooks.py`);
  swe prompt byte-identity proven; 7-point cross-file integration pass.
- **Pending:** fresh image bake (the hooks/scripts are baked into the
  toolbox/dist images); local `harbor run` smoke on non-Python TB2.1 tasks
  (e.g. `write-compressor`, `overfull-hbox`); a `terminal-mini` fleet smoke —
  success metric: nonzero opus/fable (sol/sol-high) spend correlated with the
  hard task, vs. $0 on every terminal run before this change; then the
  terminal default-flip decision.

## 8. File map

| File | Role |
|---|---|
| `e2e/reference/claude/local-docker/context/cc-harness-hooks.py` | The gates + both profile sensors (`_profile()` resolves the mode) |
| `e2e/reference/claude/local-docker/tests/test_cc_harness_hooks.py` | Unit suite locking both profiles |
| `e2e/distributed/tools/harbor_agents.py` | Terminal agent: profile-aware prompt + hook settings writer |
| `e2e/reference/claude/local-docker/context/run-instance.sh` | SWE flow: profile resolution + prompt + hook settings writer |
| `e2e/reference/claude/local-docker/run-benchmark.py` | Forwards the two env vars into the SWE instance container |
| `e2e/distributed/run-distributed.sh` | Launcher: forwards both vars to workers (only when set) |
| [HARBOR_CLAUDE_CODE.md](HARBOR_CLAUDE_CODE.md) §3.5–3.6 | Terse operator reference (gate table, marker protocol, escalation mode) |

> Maintenance: this doc explains rationale and policy. If you change gate
> semantics, profile contract, or escalation mode, update HARBOR_CLAUDE_CODE.md
> §3.5–3.6 AND this file in the same change.
