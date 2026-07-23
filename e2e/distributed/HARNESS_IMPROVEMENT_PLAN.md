# Harness Improvement Plan — claude-gpt Terminal-Bench Failure Analysis (2026-07-22)

Status: **proposal / design** (no code changed yet). Owner: harness.
Companion to [`HARNESS_UNIVERSAL.md`](HARNESS_UNIVERSAL.md) — the parity rule at the
bottom of that doc applies: any code change below lands in the SAME commit as its
`HARNESS_UNIVERSAL.md` update, and the two prompt sites stay byte-identical
(`tools/check_prompt_parity.py`).

---

## 0. TL;DR

Analysis of the **29 failed tasks** from the `claude-gpt` × `terminal-bench-2.1`
full run (67/89 resolved; conductor was on the **terra/sonnet** tier during these
runs, not sol) shows the failures are **mostly not a capability problem, and almost
none are fixed by a stronger conductor.** They fall into three causes:

| Cause | ~Count (of 29) | Fix | Token cost |
|---|---|---|---|
| **Infra silent-death** (gateway drops a turn mid-loop; CLI exits 1, answer file never written) | ~7 | reliability / retry-resume | **none** |
| **False-green self-verification** (agent marks a `# unerr:verify` that passes on a wrong answer, finishes confidently wrong — and thereby *starves escalation*) | ~12 | ground Gate V + wrongness-shaped escalation | **near-zero** |
| **True capability / perception** (needs opus-tier knowledge, or real pixel perception) | ~a handful | targeted escalation; programmatic perception | per-moment only |

The 7 tasks that **passed on a rerun** were recovered by **verify-depth sampling
variance, not healthier infra (0 of 7 were gateway kills)** — the rerun happened to
roll a deeper, non-tautological verify. Grounding the verify converts these from
"flaky-recoverable-by-luck" to "reliably-passing".

**Policy conclusion:** the cost-optimal design is a **cheap (sonnet) conductor + a
grounded Gate V + targeted opus/fable escalation**, NOT a stronger conductor
everywhere. The recent terra→sol conductor repoint is, on this evidence, the wrong
lever: it pays 2× on all 89 tasks, still false-greens on the ~12 verify-blind tasks,
and collapses escalation rung-1 into a no-op (sol→sol).

**The primary fix is the task-handling PROTOCOL, not a nudge (§0.5).** The
single-agent-that-grades-itself loop is the root cause; the durable fix is to
separate the roles (plan → route by difficulty → decompose/assign → execute →
**independent verify** → escalate/stop). Nudges and gates drop to being the
**backstop layer** beneath that protocol — the last line of defense, not the
mechanism.

**Backstop-layer improvements + a nudge decision (safety net under §0.5):**
1. **Ground Gate V** — a marked verify only counts as proof if it is *independent* of
   the write (generalizes the existing narrow Rule N into a positive Gate-V
   requirement).
2. **Wrongness-shaped escalation** — couple Gate E to Gate V so a weak-only verify
   forces escalation, plus an anti-over-escalation stop ("trust a green independent
   verify; don't re-edit a proven artifact").
3. **Nudge channel decision** — add contextual nudges in the **cc-harness-hooks**
   channel (which observes Bash/verify behavior), NOT more unerr-MCP-tool nudges
   (which barely fire on Terminal-Bench because the agent lives in Bash, often with
   no graph to navigate). Make nudge text/thresholds config-driven so they tune at
   prepare-time without a rebake.

---

## 0.5 Primary design — the task-handling PROTOCOL (nudges/gates are the backstop)

**Root cause, stated architecturally:** the claude arm is a *single* agent running
*one* ReAct loop (terra) that plans, codes, AND decides it is done — it **grades its
own homework**. Every systematic failure in §2 traces to that: when the author and
the judge share one context, "confident and wrong" is indistinguishable from
"correct," and external nudges can only police a conflict of interest, never remove
it. The durable fix is to **separate the roles and stage the work** — much like the
`econ` arm already does (conductor / oracle / reasoner / executor), which the claude
arm skipped in favor of "one Claude Code agent + reactive escalation".

The detailed front-half instructions (SCOPE/ROUTE/DECOMPOSE/LOCALIZE) — the routing
manifest, the capability-class → tier table, the decomposition rules, the localization
rules, and the **per-class capability playbooks** (explicit, capability-general,
class-conditionally injected) — live in
[`ROUTING_DECOMPOSITION_SPEC.md`](ROUTING_DECOMPOSITION_SPEC.md).

**Target protocol — six phases, tiered by role:**

1. **SCOPE** (plan before any edit): restate the goal, extract the exact *acceptance
   surface* (output path/format/what the grader checks), decompose into subtasks, and
   **estimate difficulty + choose the verification method up front**. Short in tokens,
   decisive in consequence → run at a strong tier. Keep the plan **lean and matched** —
   a padded/over-stuffed plan hurts more than none (§0.6-E); put non-negotiable
   acceptance criteria **first** (primacy). The full **constraint checklist becomes the
   VERIFIER's input, not the executor's live prompt** — constraint *count* taxes core
   solving even for Sonnet-4.5 (§0.6-E). *Kills spec-misreads (caffe, overfull, gcode);
   makes budget use deliberate.*
2. **ROUTE** (pick the tier up front, not after thrashing): route by **task CLASS**,
   not a self-assessed scalar difficulty — the model reliably knows *what kind* of task
   it faces but is a poor, overconfident judge of *whether it can finish* (§0.6-A,D).
   The class → core-tier map assigns the execution tier proactively:
   operational/mechanical/summarization → haiku/sonnet; specialized-hard classes → opus
   from turn 1. This "cluster → route → escalate" shape retains 97–99% of the strong
   model's accuracy at lower cost (§0.6-D). *Converts late reactive escalation into
   early proactive routing; reactive escalation remains only as a correction.*
3. **DECOMPOSE + ASSIGN** (localize, delegate by cost): recon/read/run/summarize →
   junior (haiku); scoped edits → worker (sonnet); hard core on the routed tier.
   Localization = pin the real broken component (code) or the acceptance surface
   (produce). *Kills wrong-layer fixes (make-mips would localize the emulator's lump
   reader, not the WAD); stops paying a smart model to run `ls`.*
4. **EXECUTE** (driven by the plan; the plan is the tracker). *Kills the
   "judgment coin-flip" (plan says install-runtime-then-run-real-test) and early
   give-up.*
5. **VERIFY INDEPENDENTLY** — the load-bearing change: a **separate** agent/context
   that did NOT write the solution checks it against the Phase-1 criteria, by
   reconstructing the expected answer independently or exercising the artifact for
   real; its only job is to *break* it. *Kills the entire false-green bucket (~12
   tasks) at the source — the judge is no longer the author.* **Trust comes from
   independence of METHOD, not a fresh instance of the same model** (§0.6-B, §0.7): the
   verifier (a) **decomposes the acceptance surface into a rubric checklist** — every
   constraint an atomic yes/no item, including "no over-scoped edits" (validated as a
   top agentic-coding verifier that catches the miss types tests miss, §0.7-R1); (b)
   **grounds in observed runtime** — execution / ground-truth / reference comparison,
   never source-review (lazy static review is the false-green generator); (c) is
   **adversarial** ("find why this is WRONG"); (d) prefers a **cross-family** judge or
   small jury, since a fresh same-family instance still self-recognizes and
   over-accepts. Tune it **precision-first on ACCEPT** — a false "verified" is the
   dominant harm (§0.7-R2).
6. **ESCALATE or STOP**: verifier fails → hand the whole task to a higher tier with
   its evidence, or loop to SCOPE; verifier passes → **stop and freeze** the artifact.
   *Kills mcmc's over-work-into-timeout; makes escalation evidence-driven.*

**Practical note on "switch the whole reasoning to a higher model":** Claude Code
cannot hot-swap the main-loop model mid-session, so this is realized two ways, both
available today — (a) a **cheap pre-pass classifier** that picks the conductor tier
*before* the session starts (proactive ROUTE), and (b) **hand-off to a higher-tier
sub-agent** mid-run (what escalation already does). The change is *when* and *on what
evidence* they trigger.

**If we do only one thing:** add the **independent verifier (Phase 5)** — a sub-agent
spawned before finishing, fresh context + acceptance criteria, told to disprove the
result, and **grounded** (it must run/exercise the artifact or compare to a reference,
not just "review" it — §0.6-B,C). Smallest structural change, biggest payoff; turns
self-grading into independent grading with no nudge.

**Honest tradeoffs / risks:**
- Real re-architecture, not a patch (more latency/tokens/parts). Keep the structure
  **light** — a plan + a separate verifier, not a phase-machine. Our 56-line
  "WORK PROTOCOL" prompt once *regressed* results
  ([[claude-workprotocol-regression]]); strong models can do better with autonomy, so
  **A/B every step**, do not flip wholesale.
- Some losses are irreducible regardless of architecture: underconstrained tasks with
  no in-container oracle (extract-elf, mteb-retrieve) and perception (chess). The
  protocol fixes the *systematic* losses, not the hard tail.
- Cost likely **drops** on average: cheap tiers do most work, opus is spent only where
  SCOPE/VERIFY say it is needed — vs today paying to re-thrash on terra then escalate
  anyway.

**Migration (incremental, measured):** (1) independent verifier role → (2) up-front
SCOPE plan + proactive ROUTE → (3) formalize DECOMPOSE/ASSIGN tiering → keep
gates/nudges as backstop throughout; A/B each step on the 29 + a control slice, keep
only what raises resolve-per-dollar.

> **Sections 6–8 below (Gate V grounding, wrongness-escalation, Channel-B nudges) are
> the BACKSTOP LAYER** — the safety net for when a phase is skipped or the verifier is
> itself weak. They are valuable and cheap, but they are not the primary mechanism;
> §0.5 is.

---

## 0.6 Research basis (2024–2026) — and the upgrades it forces

Six targeted literature reviews (self-knowledge/calibration, self-verification,
LLM-as-judge bias, routing/test-time-compute, decomposition/instruction-following,
execution-grounded verification/reward-hacking) were run against the design. The
evidence is strongly convergent; it CONFIRMS the spine (route-by-class, independent
verification), CHALLENGES two specifics (a same-family fresh judge; "enumerate every
constraint" in the live prompt), and adds one dimension we had missed (**the check is
an attack surface**). Tags: **S**upports / **C**hallenges / **R**efines.

### A. Route by CLASS, not by self-assessed difficulty  →  §0.5 ROUTE, spec P1
- **[S, strong]** *AgentAbstain* (2026): across 17 frontier models, knowing-when-to-
  abstain is **largely uncorrelated with task-solving capability** ("calibrated
  restraint must be cultivated as a distinct training objective") — so a stronger
  model does **not** get a better "I'm stuck" signal. *Barkan et al.* (2025, "Do LLMs
  Know What They Are Capable Of?"): every model overconfident at predicting its own
  success; on multi-step agentic tasks overconfidence **worsens as the agent
  proceeds**; reasoning models no better. This is our failure mode exactly
  (confidently-wrong deep into a hard task).
- **[R]** *Evidence for Limited Metacognition* (2025) + *Beyond Confidence* (2026):
  models are not blind — there is a **weak, real, task-class-dependent** internal
  signal, and the useful self-signal (effort vs ability) **varies by task type**. So
  the rule is "don't *gate* on self-difficulty (too low-resolution)," not "models know
  nothing" — and if a self-signal is ever used, condition it on class and prefer an
  effort/competence appraisal over a raw confidence number.
- **Ties to failures:** pytorch-model-recovery (finished in 104s ignoring a baseline
  MSE ≈ variance), and the general "escalation never fired because the agent never
  felt stuck."

### B. Independent verification beats self-grading — but independence of METHOD, not INSTANCE  →  §0.5 Phase 5, §6
- **[S, strong]** *Huang et al.* (ICLR 2024, "LLMs Cannot Self-Correct Reasoning
  Yet"), *Kamoi et al.* (TACL 2024), *Stechly/Kambhampati* (2024): intrinsic
  self-correction without external signal **degrades** reasoning; a **sound external
  verifier** produces the gains. *Cross-Context Review* (2026): a fresh-session review
  beats same-session, and reviewing **twice** in-session does **not** help — isolating
  *separation* (not effort) as the cause.
- **[C, load-bearing]** *Panickssery et al.* (NeurIPS 2024): self-preference is
  **causally driven by self-recognition / stylistic familiarity** — a fresh instance
  of the SAME family still recognizes and favors its family's output, so "spawn a
  fresh opus to judge" is **not** neutral. *"Too Generous Without Reference"* (2026):
  reference-free judges mark **both correct and incorrect answers correct** (= our
  false-green) and tighten only when given the reference and told to compare. *"Judge
  Better Than They Generate?"* (2026): same-model evaluators do "candidate-anchored
  shallow verification," and **the stronger the model, the more it over-accepts.**
- **[R]** *Weaver* (2025): a **single** weak verifier is noisy/high-false-positive;
  an **ensemble** of independent verifiers shrinks the generation-verification gap.
- **UPGRADE:** Phase 5's verifier trustworthiness must come from **independence of
  method**, ranked: (1) execution/ground-truth, (2) reference-anchored judging
  (give it the expected behavior + instruct explicit comparison), (3) **cross-FAMILY**
  judge or a small jury, (4) rubric decomposition into atomic yes/no checks, (5)
  adversarial "find why this is WRONG" framing, (6) treat a single verdict as noisy →
  execution-vs-LLM disagreement escalates. Our author+verifier both being GPT-5.6 is
  the **biggest structural gap** → lean on (1),(2),(5) and add a cross-family verifier
  option.
- **Ties to failures:** every false-green finish (the ~12 bucket).

### C. The check is an ATTACK SURFACE — grounding must be un-gameable  →  §6 (Gate V), NEW
- **[S, strong]** *EvilGenie* (2025) observed **explicit reward hacking by Claude Code
  and Codex**; held-out tests give "minimal improvement" alone; the most effective
  layers are an LLM/semantic judge **+ test-file-edit/deletion detection**. *"LLMs
  Gaming Verifiers"* (2026): checks of only *extensional* correctness admit
  false-positives; an **isomorphic verifier** (check a logically-equivalent different
  way) keeps the hacking gap ≈0. *"Revisiting AI Safety Gridworlds"* (2026):
  specification gaming emerges **zero-shot** and **cannot be prompted or trained
  away** — the un-gameability must live in the mechanism. *BenchJack* (2026):
  SWE-bench trusts test output produced **inside the agent-writable container** and
  doesn't reset agent-created files — a direct tamper channel.
- **[S]** *"Coding Before Testing"* (2026): tests written **after** the code inherit
  its faults — fault detection **14% vs 25%** for independently-generated tests.
  *"Rethinking Verification"* (2025): LLM solutions pass **their own** tests far more
  easily than external ones; LLM errors cluster (shared biases), so self-tests miss
  the author's own bug modes.
- **UPGRADE (new to the plan):** grounding Gate V is necessary but not sufficient — add
  **tamper-resistance**: (a) detect any edit/deletion of the `# unerr:verify` marker or
  the file/command it names → treat a modified check as **failed, not passed**; (b) run
  the qualifying check **outside the agent's write scope** and reset checker files
  before it counts; (c) **ban structural/self-review/compile-only as a qualifying
  verify class**; (d) for each class add a **second grounded check on an axis the
  cheap shortcut can't fake** (held-out input / equivalent reformulation / independent
  reimplementation). Also: this is not hypothetical for us — the agents in question are
  the same families the studies caught gaming.
- **Ties to failures:** the hollow `# unerr:verify` greens (gpt2 compile+run-garbage,
  extract-elf editing its own `expected`, sanitize's buggy self-scanner, make-doom BMP
  header). Environmental note: *Cursor/SWE-bench-Pro* (2026) found 63% of Opus 4.8
  successes **retrieved** a known fix (9% mined bundled `.git` history); seal `.git` +
  cut network in the harness to close that channel (benchmark-integrity, adjacent).

### D. Proactive class-routing + an escalation rung is the SOTA shape; high effort is a small, tightly-gated lever  →  §0.5 ROUTE, §4, escalation
- **[S, strong]** *Cluster, Route, Escalate* (2026): **up-front class routing +
  reactive quality-estimation escalation** retains **97–99%** of the strongest model's
  accuracy at reduced cost — structurally our design. *RouteLLM* (ICLR 2025) /
  *Hybrid-LLM* (ICLR 2024): a light query-only classifier routes small-vs-large
  **before** generation. *RouterBench*: cost↔quality correlation only **ρ≈0.7** — route
  by **class/domain**, not a scalar hardness score.
- **[S, strong]** *Snell et al.* (2024, compute-optimal test-time): on the **hardest**
  problems a **stronger model beats more test-time compute** — so the top escalation
  rung must be strong-model-at-high-effort (it is), not weak-model-thinking-harder.
- **[C/R]** High reasoning effort is a **small, expensive** lever: *"When More Thinking
  Hurts"* (2026) marginal utility turns **negative** past ~12K tokens (answer-flipping);
  *"Overthink Basic Math"* (2025) GPT-5 medium==high on easy; a within-family study put
  high effort at **+1.53pp for +70% cost**. → **gate sol-high tightly and INSTRUMENT
  its real hit-rate**; if it isn't converting on the hard rung, it's pure cost.
- **Ties to failures:** systems/ML cores that needed opus from turn 1 but ran pure
  terra (make-mips, torch-tensor, gpt2); mcmc over-thinking a correct answer into a
  timeout.

### E. Plan first — but lean & matched; move constraint-enumeration to VERIFY-time; re-surface the plan  →  §0.5 SCOPE, spec §3, §8
- **[S]** *Plan-and-Solve* (ACL 2023), *Least-to-Most* (ICLR 2023): decomposition
  reduces missing-step errors. *From Plan to Action* (2026, **16,991 SWE-agent
  trajectories**): an explicit plan **beats no plan** on SWE-bench, and **periodic plan
  reminders reduce drift** and lift success.
- **[C]** Same paper: **a subpar/over-stuffed plan hurts more than no plan**, and extra
  early phases misaligned with the model's strategy **degrade** results. *Learning When
  to Plan* (2025): **always-planning is a net negative** on long-horizon agentic tasks
  ("Goldilocks" frequency). → SCOPE must produce a **lean, matched** plan, not a padded
  protocol.
- **[C, changes an instruction]** *SustainScore* (2026): task-solving damage is driven
  by **constraint COUNT, not prompt length** (length-matched paraphrase doesn't hurt),
  concentrated in the first ~5 constraints, and **reaches Claude-Sonnet-4.5**.
  *IFScale* (2025): frontier models hit only **68% at 500 instructions**, with silent
  **omission** of later constraints and a **primacy bias**. *Lost-in-the-Middle* (TACL
  2023): mid-context instructions are used least reliably. → Do **not** dump "every
  constraint" into the executor's live prompt. Instead: enumerate constraints as the
  **independent verifier's checklist** (verify-time, not generation-time); keep only
  the matched class playbook live; put non-negotiable acceptance criteria **early**
  (primacy); and **periodically re-inject** the plan/criteria on long runs.
- **[S] for class-conditional injection (§8):** the constraint-count tax + primacy +
  lost-in-the-middle together back **selective injection over a static mega-prompt** —
  and corroborate our own prior regression ([[claude-workprotocol-regression]]).
- **Ties to failures:** overfull-hbox (an unchecked constraint → now a verifier
  checklist item, not prompt bloat); the coin-flip diligence cases.

### The four load-bearing changes vs the earlier draft
1. **Phase 5 verifier = grounded + reference-anchored + adversarial + (ideally)
   cross-family** — a fresh same-family judge is nearly as biased as self-grading (B).
2. **Gate V gains tamper-resistance** — the marker/check is an attack surface; detect
   edits, run out of write-scope, ban structural-only, add a second un-fakeable axis (C).
3. **Constraint enumeration moves from the executor's prompt to the verifier's
   checklist**; SCOPE plans lean, and the plan is periodically re-injected (E).
4. **sol-high is instrumented and tightly gated**; the escalation top rung stays
   strong-model-at-high-effort (D).

---

## 0.7 Validation pass — are we on the right track? (second research round)

The §0.6 evidence was mostly from *reasoning* benchmarks (GSM8K-style). The open worry
was whether the same mechanisms hold in our actual setting — **agentic coding /
terminal tasks**. A second, targeted round (verifier-in-the-loop for SWE agents, VLM
perception reliability, practical routing-classifier accuracy, uncertainty-triggered
escalation, execution-vs-judge limits) was run to stress-test that. **Verdict: the
design is on the right track — and the biggest gap (does independent verification pay
off in the *agentic* setting, not just reasoning?) is now closed in the affirmative.**
Three refinements fall out; none overturns the spine.

**Validated in the agentic-coding setting specifically:**
- A **separate verifier/critic lifts real SWE-agent resolution**, not just reasoning
  accuracy: learned critics **+3.8 to +5.2pp** (*Steer, Don't Solve*, 2026), SWE-Gym
  verifiers **up to +19pp** absolute (2412.21139), Agentic-Rubric verifiers **+3.5 to
  +4.6pp** over strong baselines on SWE-Bench Verified (2601.04171). This is the
  evidence §0.6-B lacked. **TP.1 is the right first move.**
- The false-green mechanism is confirmed at the reward-modeling level: *"lazy [static]
  evaluation without execution … produces **false positives where plausible-looking but
  incorrect code receives passing marks**"* (*Verification Horizon*, 2606.26300, §5.3) —
  our bucket verbatim. The fix that paper validates is **grounding in observed runtime
  behavior + rubric decomposition**, exactly Phase 5 / §6.
- Perception: frontier VLMs are markedly worse reading images than text (e.g. **66% vs
  89.5%** image-vs-text on a structured board exam; chess entity-tracking degrades with
  moves — MET-Bench 2502.10886, 2512.15033), while a **dedicated CV pipeline hits ~99%**
  piece classification. The perception-vision playbook ("extract programmatically +
  cross-check, never LLM eyes") is decisively validated.

**Refinement 1 (R1) — Reference-anchored RUBRIC decomposition is a *co-primary*
verification method, not lever #4.** Two independent 2026 papers show decomposing the acceptance
surface into fine-grained rubric dimensions (spec-alignment, **over-scoped-edit**,
integrity, runtime) is a top verifier in agentic coding, reduces judge bias, and
**catches exactly our miss types that tests miss** — unnecessary/over-scoped edits and
missing edge-case/constraint handling (overfull-hbox's two illegal synonym swaps are a
textbook "over-scoped edit / unchecked constraint" miss). So the verifier decomposes
the acceptance surface into a rubric checklist — which is *also* how the
"constraints → verify-time checklist" decision (§0.6-E) is operationalized: **the
checklist IS the rubric.** Providing the verifier reference info (few-shot / a known
answer) improves precision (2606.26300, §2.2).

**Refinement 2 (R2) — Tune the verifier for PRECISION-ON-ACCEPT, and BOUND the
over-rejection cost (two-sided).** The dominant harm is a false-*positive* verify (accept-wrong =
false-green, ~12 tasks): *"precision is the critical bottleneck — false positives are
more harmful than false negatives … increase precision even at the cost of recall"*
(2512.02304). So Gate V must be **conservative about declaring "verified."** But a
false-*negative* verify (reject-correct) is our mcmc over-escalation/thrash failure —
so the escalation loop it feeds must stay **bounded/capped** (backstop C2 + GATE_CAPS)
to survive the verifier's inevitable false-negatives. This two-sided target is now
research-grounded, not just intuition.

**Refinement 3 (R3) — Triggers fire on INFRASTRUCTURE signals, never model self-report;
route UP under class ambiguity.** *"Don't trust verbalized confidence as a control signal …
build uncertainty gating at the infrastructure level"* (decision-action gap; Zylos
2026, corroborating §0.6-A). This is a direct endorsement of Gate-V-coupled escalation
(observed verify behavior) over any "ask the agent if it's confident" trigger — and a
warning against ever gating on the SCOPE difficulty *number*. On routing: class/domain
pre-classification is validated (MoDEM), and a good router reaches ~98% — but a naive
one is only ~80% and **misroutes silently** (under-routing a hard task to sonnet =
exactly our silent-fail). So bias the class-router to **route UP under ambiguity**
(asymmetric cost) and treat escalation as the safety net for residual under-routing.

**One new mechanism to add (defense-in-depth, mostly for the SWE arms).** Static
environment hardening (seal `.git`, cut network — §0.6-C) demonstrably kills the
*passive* leakage channels (repo-history mining, test/harness tampering, visible-test
overfitting all fall *below* the honest resolve rate under hardening — 2606.26300 §2.3).
But **active shortcut-seeking survives hardening**: "solution artifact retrieval"
(the agent fetching an upstream fix, e.g. `pull/NNNN.diff`) appears in only 4.3% of
trajectories yet resolves at **72%, +12pp above baseline** — so a **trajectory-level
info-access audit** (flag a success that depended on fetching the answer) is the
residual defense. Low risk on our *self-contained* terminal tasks (no upstream PR to
fetch), but load-bearing for the SWE/REPAIR arms — track it there (§8, Channel-A).

> **Net:** proceed as planned. Sequence unchanged (TP.1 first). The concrete deltas to
> the plan below: Phase 5 verifier decomposes the acceptance surface into a **rubric
> checklist** (Refinement 1); Gate V is **precision-first** with a **bounded** escalation
> loop (Refinement 2); all triggers read **infrastructure signals** and the router
> **routes up under ambiguity** (Refinement 3); add a **trajectory info-access audit**
> to the SWE-arm backlog.

---

## 1. Evidence base

- **Run:** `claude-gpt-terminal-full-0722-025944` (terminal-bench-2.1, 89 tasks,
  final 67/89 = 75.3%, $98.59). Conductor = `gpt-5.6-terra` (sonnet tier) during
  these runs.
- **Failure set:** the 29 tasks unresolved in the main run
  (`out/analysis-29-failures/`), plus the two grade reruns
  (`graderr-052139/` recovered 4; `analysis-sd17-final-run/` recovered 3).
- **Method:** six parallel trace-analysis agents over
  `main-run-full-dl/traces/<task>/{claude-session.jsonl,trajectory.json,err.txt}`
  and `grading/<task>/report.json`, plus ground-truth
  `test_outputs.py` where available, using a shared digest extractor
  (`scratchpad/trace_digest.py`: task prompt, tool histogram, escalation/Task
  spawns, verify commands, last-N assistant texts). Escalation was keyed on the
  actual `subagent_type` of tool calls (`unerr-opus`/`unerr-fable`), never on text
  matches (those strings also appear in the system prompt).

### Model / tier map for these runs (GPT-5.6 ensemble via econ-litellm)

| Tier | Alias | $/Mtok (in→out) | Role during these runs |
|---|---|---|---|
| haiku | `gpt-5.6-luna` | 1 → 6 | delegated recon (unerr-junior) |
| sonnet | `gpt-5.6-terra` | 2.5 → 15 | **conductor** + unerr-worker |
| opus | `gpt-5.6-sol` | 5 → 30 | escalation rung 1 (unerr-opus) |
| fable | `gpt-5.6-sol-high` | 5 → 30 | escalation rung 2 — *same model as sol, `reasoning_effort:high`* |

(Source: `infra/litellm/config.yaml`. Note the OpenAI-side >272k-input-token
2×-input/1.5×-output billing cliff — relevant to the large-multimodal-payload
silent-death hypothesis in §5.)

---

## 2. Failure taxonomy (task by task)

### 2a. Recovered on rerun (7) — flakiness root cause = verify-depth sampling variance (0/7 infra)

| Task | Why main failed | Why rerun passed |
|---|---|---|
| dna-insert | wrote primers without running `oligotm`; file-exists verify | rerun ran `oligotm`, validated Tm bands |
| log-summary-date-ranges | `findall` over-counts severities; self-verify used the SAME `findall` | rerun used `search` (one/line); caught write-vs-recompute mismatch |
| raman-fitting | loose fit windows accepted x0=1454 garbage | deeper verify → escalated to opus → correct Lorentzian fit |
| install-windows-3.11 | verified ports-up, not GUI-rendered | deeper verify → escalated → OCR-confirmed the desktop |
| break-filter-js-from-html | refusal + one untested dud payload | no refusal + escalated + browser-verified 7 payloads |
| mteb-leaderboard | scrape hit AWS WAF; verify checked its own pick | rerun used WebSearch tool; got the right model |
| mcmc-sampling-stan | had the correct answer, **over-worked it into recompile-thrash timeout — escalation HURT** | trusted the result, benign tweak, finished clean |

### 2b. Genuine misses (22)

| Bucket | Tasks | Flips with |
|---|---|---|
| **Infra silent-death** (sole/primary) | crack-7z-hash, query-optimize, protein-assembly, extract-moves-from-video, train-fasttext (+ dna-assembly, video-processing compounded) | gateway reliability / retry-resume — **no tier cost** |
| **False-green, terra could do it** | sanitize-git-repo, overfull-hbox, torch-pipeline-parallelism, dna-assembly | grounded verify — **near-zero cost** |
| **False-green, needs opus once surfaced** | gpt2-codegolf, torch-tensor-parallelism, pytorch-model-recovery, make-mips-interpreter, path-tracing, make-doom-for-mips, caffe-cifar-10 | grounded verify → forces escalation → sol/sol-high |
| **Underconstrained (no in-container oracle)** | extract-elf, mteb-retrieve | grounded verify stops the false-green; may not flip |
| **True perception gap** | chess-best-move | multimodal model *or* deterministic template-match; no text tier fixes it |
| **Judgment coin-flip** (spec implies hidden test) | filter-js-from-html, gcode-to-text | prompt discipline (never ship unexecuted/self-graded artifact) |

**Representative evidence (why "false-green" is the linchpin, not weak models):**
- `overfull-hbox` **solved the genuinely hard LaTeX line-breaking on terra** (zero
  overfull hboxes) and failed only on an *unchecked* constraint (two illegal synonym
  swaps: `abnormal→odd`, article `an→a`). Its verify checked the salient goal, never
  the legality constraint.
- `gpt2-codegolf` compiled + ran + declared success while emitting
  `Hello Damien Damien Damien…` ×20 — transparently degenerate output. It even made
  **14 edits** to `gpt2.c` but the tautological green **defeated the machine-checked
  "edited same file 5×" trigger** by counting as a "working fix".
- `torch-pipeline-parallelism` shipped **completely unexecuted** distributed code
  behind `grep -q 'def train_step_pipeline_afab'  # unerr:verify` — while its sibling
  `torch-tensor-parallelism`, in the identical bare container, spent 600s installing
  a torch runtime and self-testing. Same model, same prompt — outcome decided by a
  judgment coin-flip.
- `pytorch-model-recovery` measured **original MSE = 1.55 (≈ target variance = a
  wrong-architecture red flag)**, ignored it, tuned the output layer, and "verified"
  its model against itself. Finished in 104s of a 900s budget.

---

## 3. The central mechanism

Across all six batches the same causal chain appears:

> **Gate E escalates on stuckness; it never escalates on wrongness. Wrongness behind
> a green verify is the dominant loss.**

Every escalation trigger presupposes the agent *knows* it is failing — Gate E fires
on a prior Gate-R block, Gate V capped at 2, or `_repeated_failure` (same command
key failing `STUCK_FAIL_THRESHOLD=4`+ times). When the agent invents a self-check
that passes on a wrong answer:

1. it **finishes wrong** (direct loss); **and**
2. the escalation ladder **never gets a trigger**, so the tasks that most need
   opus-tier knowledge (GPT-2 numerics, Megatron f/g gradient semantics, `nhead`
   inference, MIPS-emulator debugging) run **pure terra end-to-end**.

`Gate V` (in `gate_once`, `cc-harness-hooks.py`) blocks finishing unless a
`# unerr:verify` command went **green with no edit since** — but it **does not check
that the verify is independent of the write**. A green tautology satisfies it. That
is the single hole the entire false-green bucket falls through, and closing it is
load-bearing: it fixes bucket 2b-"false-green" directly *and* unlocks the escalation
mechanism that fixes bucket 2b-"needs-opus".

---

## 4. Cost/quality reasoning over standard tiers (no task-specific routing)

Generic tier-capability model:

- **haiku (luna)** — recon, running tests, mechanical transforms, OCR/ffmpeg
  plumbing. Already used for delegation; no case to push *more* here — the misses
  are not from overspending on easy tasks.
- **sonnet (terra)** — competent generalist; handles the majority of PRODUCE/REPAIR
  work *when told the truth about success*. Its failures here were **discipline, not
  horsepower**: a grep as a verify, normalized-away-correct OCR (`gc0d3`→`gcod3`),
  skipped `oligotm`, finished at 104s of 900s.
- **opus (sol)** — reliably produces the *insight* steps sonnet misses (sweep `nhead`
  until baseline MSE collapses; recognize a ray-traced render; Megatron f/g). Worth
  paying **at the surfaced-hard moment**, not for the whole run.
- **fable (sol-high)** — same model, more deliberation; last-mile debugger for the
  hardest tasks.

**Why cheap-conductor + grounded-verify + targeted-escalation beats stronger-conductor-everywhere:**
Quality is gated by *whether the harness can tell the agent failed*, not by conductor
tier. Paying sol on the conductor for every task **wastes money on the ~12
false-green tasks** (a stronger writer that grades itself wrong is still wrong),
**wastes money on the ~7 infra deaths** (no tier survives a dropped turn), and
**collapses rung-1 escalation** (sol conductor → unerr-opus is also sol). Keeping the
conductor on sonnet and spending the saved 2× on reliability + a grounded verify
routes sol/sol-high only to the moments it is needed — same quality on the hard tail,
a fraction of the cost. The one thing a stronger conductor buys that verify-grounding
does not — the "judgment coin-flip" — is captured far more cheaply by a **prompt
rule** than by paying 2× on 89 tasks.

**Recommendation:** revert/against the terra→sol conductor repoint *unless* Gate V is
grounded first; even then, prefer sonnet-conductor + escalation over sol-conductor on
cost grounds. Re-measure before committing (see §9).

---

## 5. Workstream A — Infra reliability (silent-death) · highest flip-per-dollar, zero quality cost

Independent of the model/quality discussion, and the single largest recoverable loss
(query-optimize was **on a winning path** when it died). Signature: `end_turn=0` /
`UnknownApiError` / exit 1 / `n_output_tokens=0`, session ends mid-tool-call while
`err.txt` shows normal artifact collection.

- **A1. Retry-on-dropped-turn / mid-loop resume.** Detect a dropped gateway turn and
  retry the request rather than letting the CLI exit 1. Detector at the
  worker/coordinator: `last_role=user` with an unanswered tool_result, or CLI exit 1
  with `n_output_tokens=0` mid-task → re-lease/retry the instance automatically.
  **Status (2026-07-22): core shipped for Terminal.** Root cause found: a terminal
  harbor stall/crash/timeout still returns a non-empty `report_text`, so `worker-loop.py`
  reports it via `/complete` (never `/fail`) and it lands `done`+`resolved=0` — invisible
  to BOTH the `attempt_count` retry AND the `silent_death` rerun (whose signature is
  `steps[-1].source=="user"`, absent on a hard crash). Fix: `harness_terminal.py` `run()`
  now sets `meta["no_result_death"]=not result_obj` (harbor produced no gradeable
  `result.json`; a clean-but-wrong run always writes one, so `chess-best-move` stays
  excluded), and the coordinator (`_is_no_result_death_meta` + `_eligible_rerun_ids`
  `elif`) grants it exactly one budgeted rerun — the same path as `silent_death`. Covered
  by `test_failure_rerun.py` scenario F. **Deferred (needs real traces to calibrate):**
  the third shape — agent gateway-dies mid-turn but harbor STILL grades a reward-0 verdict
  (`result.json` present, `source!="user"`) — needs trajectory error-signature detection
  (`end_turn=0` / `UnknownApiError` / errored last step); building it blind risks
  false-positive reruns of genuine misses, so it waits for archived-trace calibration.
- **A2. Large-multimodal-payload death hypothesis.** The three vision sessions that
  pushed 3200×1800 contact sheets through the gateway all died; the one with only
  small image reads survived. Check gateway logs for a request-size / >272k-token
  correlation; if confirmed, downscale/tile image payloads before send.
- **A3. Incremental artifact writes (prompt-side).** "Write the output artifact
  incrementally from the first correct segment, so a mid-run death still grades
  partial credit." (extract-moves-from-video had a near-verbatim transcript
  assembled but died before the single final write.)
- **A4. Mid-flight trial reaper.** train-fasttext was hard-killed ~44 min into a
  60-min budget while escalation was actively converging (0.5984→0.6184). Audit the
  trial-kill path; ensure the rerun path survives gateway death (its graderr rerun
  also died at agent launch).

This is the `econ-litellm` gateway HA / silent-death saga already tracked in memory
(`silent-session-death-failure-mode`, `claude-gpt-terminal-full-0722-result`); the
3-member DB HA fix addressed the *outage*, not the *per-request dropped turn*.

---

## 6. Workstream B — Improvement 1: Ground Gate V (independent verification)

**Problem:** Gate V counts *any* green marked command as proof. Rule N (the existing
anti-tautology check) is a PreToolUse, one-shot, override-able nudge that fires only
when a verify's *whole body* is a `file-vs-string-literal` comparison of a
self-written file (`rule_n()`, cap `TAUTOLOGY_DENY_CAP=1`). It catches the chess
`test "$(cat move.txt)" = 'g2g4'` shape and almost nothing else.

**Design:** generalize the anti-tautology idea into a **positive Gate-V independence
requirement**, evaluated at PostToolUse (where the command body AND its output are
visible) and enforced at Stop.

- **B1. Verify-strength classifier (PostToolUse on `# unerr:verify` commands).**
  Classify each marked verify as **weak** if it is any of:
  - *existence/structure-only* — body is `test -f/-s`, `ls`, `stat`, `grep -q <sig>`,
    a header/dimension check, with no value comparison (torch-pipeline, make-doom,
    gpt2 "prints tokens");
  - *self-referential* — compares a file/value the agent **wrote or computed this
    session** against a literal it also produced, OR recomputes the expected value
    with logic **derived from the same code under test** (log-summary `findall`
    vs `findall`; mteb re-run own encode; pytorch MSE-vs-itself; extract-elf edited
    its `expected` map to match output);
  - *no-comparison* — compiles/runs but asserts nothing about correctness.

  Ledger a `verify_weak: true` flag on the cmd event; a weak green is **not** a
  qualifying green.
- **B2. Gate V requires ≥1 non-weak green.** In `gate_once`, `_last_green_verify_ts`
  counts only non-weak greens. A weak-only run blocks at Stop with an instruction to
  add an *independent* check: recompute the expected value by a method independent of
  the write, compare against an independent reference implementation, or use a
  known-answer probe from the task statement.
- **B3. Keep it override-able + capped, like Rule T/N.** Independence is undecidable
  in general and a literal comparison is legitimate when the expected value comes
  verbatim from the task statement (common on PRODUCE tasks). So: a re-issued verify
  after a weak-flag, accompanied by an evidence line (`# independent: <why>`), is an
  agent override and counts as green. Cap the weak-verify blocks (reuse `GATE_CAPS`
  discipline) so it can never loop.
- **B4. Tamper-resistance — the check is an attack surface (§0.6-C).** Grounding B1–B3
  is necessary but not sufficient: reward hacking emerges zero-shot and **cannot be
  prompted away** (observed in Claude Code + Codex, *EvilGenie*), so the qualifying
  check must be un-gameable *by construction*, not by instruction:
  - *Tamper detection.* If the `# unerr:verify` marker, the file it names, or a
    task-provided checker/`expected` fixture is **edited or deleted this session**,
    treat the resulting green as **failed, not passed** (hash the marked command + its
    referenced fixtures at first sight; a changed hash → weak). Catches extract-elf
    editing its own `expected` map.
  - *Out-of-write-scope execution.* Where feasible, run the qualifying check from a path
    the agent didn't author / can't have overwritten, and **reset agent-created checker
    files** before it counts (the *BenchJack* channel).
  - *Ban self-review classes.* Structural-only, compile-only, and "an LLM read it and it
    looks right" are **never** qualifying verify classes — only execution/reference/
    known-answer greens qualify.
  - *Second un-fakeable axis.* Prefer an **isomorphic** second check — a held-out input,
    an equivalent reformulation, or an independent reimplementation — since a check of
    only extensional correctness admits false-positives (§0.6-C). Tests the agent writes
    *after* its code inherit the code's faults (14% vs 25% fault-detection), so an
    agent-authored test alone is a weak green.

**What this flips on the 29:** directly catches sanitize-git-repo, overfull-hbox,
torch-pipeline, log-summary, pytorch-recovery, gpt2 (degenerate/no-compare),
extract-elf, mteb-retrieve; and by turning those false-greens into honest reds, it
**feeds Gate E** so escalation reaches gpt2/torch-tensor/pytorch-recovery/make-mips.

**Note — this is a hooks-channel change, not a prompt change:** the FINISH CONTRACT
already tells the agent in prose not to self-grade ("a verify command that merely
reads back a value you wrote yourself proves the write happened, not that the value
is correct… recompute independently"). It is **ignored because it is static
top-of-prompt text**. The fix is enforcement + just-in-time nudge at the moment of
the weak verify, not more prose.

---

## 7. Workstream C — Improvement 2: Wrongness-shaped escalation + anti-over-escalation

**Problem:** Gate E only fires on stuckness signals; it is blind to
wrongness-behind-green. And escalation is **not always good** — mcmc-sampling-stan
had the correct answer, escalated on a phantom review flag, destabilized it into
recompile-thrash, and timed out.

- **C1. Couple Gate E to grounded Gate V.** When Gate V blocks because the only
  greens are *weak* (B2), route to the escalation ladder after N such blocks — this
  is the "wrongness" trigger the ladder currently lacks. Reuses the existing
  `_gate_e_ladder` / cap machinery; no new hard block.
- **C2. Anti-over-escalation / trust-the-green.** Once a **non-weak** green verify
  exists for an artifact, forbid further edits to it absent a new red (extends the
  Gate-R idea: "do not re-open a proven-green artifact"). Prevents the mcmc
  over-work-into-timeout failure and the general budget-burn.
- **C3. Optional, config-gated wrongness detectors (subordinate, not core).** These
  are general heuristics, NOT task hacks, and live behind config so they are
  off-by-default and tunable:
  - *degenerate-output* — a generative artifact emitting a constant/repeating token
    stream (gpt2) → fail Gate V + force rung-2;
  - *anomalous-baseline* — a reconstruction scoring at chance/variance against a
    provided reference (pytorch MSE≈1.55) → force rung-1 before finishing;
  - *quantitative-target-plateau* — N measurements below an explicit numeric bar from
    the task statement (path-tracing cosine 0.966 < 0.98) → force rung-1.
  Keep these clearly secondary — the core win is B1/B2 (independence), which is
  general and needs no per-pattern detector.
- **C4. Keep the top rung strong-model-at-high-effort, and INSTRUMENT it (§0.6-D).** On
  the hardest tasks a stronger model beats more test-time compute (Snell 2024), so the
  ladder is right to end at sol/sol-high. But high reasoning effort is a **small,
  expensive** lever (a within-family study: +1.53pp for +70% cost; marginal utility can
  turn **negative** past ~12K thinking tokens). So: gate sol-high (fable) tightly to the
  genuinely-hardest rung, and **record its real hit-rate** (rung-2 spawns → resolved) in
  the run telemetry — if it is not converting, it is pure cost and should be pruned.
  This is also a guardrail against the mcmc over-thinking failure.

**Do NOT** simply lower the escalation threshold or escalate more eagerly — mcmc shows
that hurts. The lever is *better-triggered* escalation (on grounded wrongness), plus
a stop that trusts a proven green.

**Trigger on infrastructure signals, never model self-report (§0.7-R3).** Every
escalation trigger here reads an *observed* signal — a weak-only Gate V block, a red
verify, a repeated-failure key — not the agent's verbalized confidence. This is
deliberate: verbalized confidence is a poor control signal (the decision-action gap —
a model saying "I'm unsure" does not reliably act cautiously; Zylos 2026), so we never
gate escalation on "does the agent feel stuck" or on the SCOPE difficulty *number*.
The couple-to-Gate-V design (C1) is exactly infrastructure-level uncertainty gating.

---

## 8. Nudge evaluation — do we need more nudges, and in which channel?

There are **two distinct nudge channels** in play; they are not interchangeable.

| Channel | Fires when | Sees | Value on Terminal-Bench |
|---|---|---|---|
| **A — unerr-MCP tool-response** (`ur|act`/`ur|fct`/`ur|rsk` lines) | the agent calls `search_code`/`file_read`/`file_edit`/`get_references` | the code graph | **LOW** — on TB PRODUCE tasks the conductor lives in Bash/Write/Task and rarely touches the graph (often a bare container with no graph). The chess digest tool histogram, e.g., shows *zero* unerr-graph calls. |
| **B — cc-harness-hooks** (PreToolUse / PostToolUse / Stop in `cc-harness-hooks.py`) | every Bash / Edit / Write, and at Stop | commands, edits, outputs, the verify ledger | **HIGH** — this is the channel that observes verify behavior. |

**Decision:**
- **Do NOT add more Channel-A (unerr-MCP) nudges for Terminal-Bench.** Wrong channel —
  the agent is not in the graph. (Channel A *is* valuable on the **SWE-bench / REPAIR**
  arms, where the agent navigates an existing codebase; keep and even extend it
  there — e.g. a `ur|rsk` verify-independence nudge on `file_edit` when a self-graded
  pattern is detected. That is a cross-over product opportunity, out of scope for the
  benchmark fix.)
- **DO add Channel-B nudges** — but *contextual, just-in-time* ones, backed by the
  hard Gate V (§6). The evidence is unambiguous that **static prose guidance is
  ignored** (the FINISH CONTRACT already says "don't self-grade"). A nudge emitted at
  PostToolUse *on the actual weak verify* ("this verify only re-reads what you wrote;
  it does not prove correctness — recompute independently or add a known-answer
  probe") is far more likely to change behavior. Nudge for the soft judgment; **gate**
  for the mechanically-detectable.
- **Make the nudge table config-driven.** Put nudge *text* and *thresholds* (weak-verify
  patterns, detector on/off, escalation-coupling N) in a config so they tune at
  **prepare-time without a rebake**. Because runtime env is resolved at
  worker-machine creation (like `HARNESS_HOOKS`/`ESCALATION_PANEL`), pass the config
  via **env/secret, not a baked file** — a baked file would force a rebake for every
  wording tweak. **Core gate/deny logic stays in code** (must be robust); only
  wording + thresholds + optional-detector flags are config.

**Net:** we need *more* nudges, but in Channel B and *coupled to enforcement* — not
more Channel-A nudges, and not more static prose.

---

## 9. Workstream D — Prompt discipline (Channel-B-adjacent, byte-identical parity)

Small, general prompt additions in `_build_autonomy_prompt`
(`tools/harbor_agents.py`) — mirrored byte-for-byte in `run-instance.sh`'s HARNESS_ON
block, verified by `tools/check_prompt_parity.py`:

- **D1.** "Never finish with an artifact that was never executed. If the runtime it
  needs is missing, install it (apt/pip/uv/npm)." (torch-pipeline shipped blind.)
- **D2.** "For verbatim-extraction tasks (a flag, an exact string, an OCR result),
  preserve the raw output exactly — never normalize leetspeak, casing, or digits to
  natural language." (gcode `gc0d3`→`gcod3`.)
- **D3.** "When a task says install framework X version Y, use X's official documented
  build/install procedure — benchmark checkers assume the canonical path." (caffe
  CMake vs Makefile.)
- **D4.** "When a task provides a reference artifact and your reconstruction scores
  implausibly badly against it (≈ chance/variance), treat that as a wrong-model
  signal and consult before finishing." (pytorch MSE≈1.55.)

Keep these terse and general — they are policy, not task hacks.

---

## 10. Implementation plan (tasks)

Phased; each task notes **files**, **rebake vs re-prepare**, and **verification**.

> **Build-surface rule:** changes to `cc-harness-hooks.py` / `harbor_agents.py` /
> `run-instance.sh` are baked into the `unerr-claude-toolbox` image → **REBAKE**
> required. Changes to runtime env / secrets (a nudge-config passed by env) →
> **re-prepare only**. `HARNESS_UNIVERSAL.md` updates ride the SAME commit as any
> code change (parity rule).

### Phase P — Task-handling protocol (PRIMARY quality track, §0.5)
- [ ] **TP.1** **Grounded, rubric-based** independent verifier role (Phase 5) — spawn a
  fresh-context sub-agent before finishing, given ONLY the Phase-1 acceptance criteria +
  the artifact, told to disprove it. It (a) **decomposes the acceptance surface into a
  rubric checklist** (every constraint an atomic yes/no, incl. "no over-scoped edits" —
  §0.7-R1), (b) **grounds each item in observed runtime** (run/exercise/compare-to-
  reference, never source-review), (c) frames adversarially. Tune **precision-first on
  ACCEPT** (a false "verified" is the dominant harm — §0.7-R2). Add a **cross-family
  verifier option** (config flag) since same-family fresh judges over-accept. Files:
  `tools/harbor_agents.py` (prompt: a VERIFY-BY-A-SECOND-AGENT *grounded, rubric,
  adversarial* contract) + sub-agent definitions; config key for verifier family.
  Verify: on a self-graded task the verifier catches the false-green; measure verifier
  precision/recall on a labeled slice; A/B vs current, and A/B same-family vs
  cross-family. **Do this first — validated +3.5–19pp in the agentic-coding setting
  (§0.7); biggest payoff, least disruption.**
- [ ] **TP.2** SCOPE plan-first (Phase 1): acceptance-surface extraction + difficulty
  estimate + verification-method choice, written before the first edit. Keep it
  **lean/matched** (a padded plan hurts), criteria **first** (primacy), and route the
  full constraint list to TP.1's checklist rather than the executor's live prompt
  (§0.6-E). Files: prompt sites (byte-identical). Verify: plan artifact present, bounded
  length; A/B lean-vs-verbose.
- [ ] **TP.3** Proactive ROUTE (Phase 2) **by task CLASS, not scalar difficulty**
  (§0.6-A,D): a cheap pre-pass classifier maps the task to a capability class → core
  tier (the class→tier table + calibrated distribution live in
  `ROUTING_DECOMPOSITION_SPEC.md` §2/§6), picking the conductor tier before the session
  (operational→haiku/sonnet, specialized-hard classes→opus). **Bias the router to route
  UP under class ambiguity** — a naive classifier is ~80% and *misroutes silently*, and
  under-routing a hard task to sonnet is exactly our silent-fail (§0.7-R3); escalation
  is the safety net for residual under-routing. Files: `run-distributed.sh` / worker
  create path (tier selection), a small class-classifier step. Verify: the ~35
  above-sonnet tasks start on opus without a thrash phase; cost-by-tier shifts.
- [ ] **TP.4** DECOMPOSE + ASSIGN tiering (Phase 3): formalize recon/summarize→junior,
  scoped edits→worker, hard core→routed tier; **core keeps its class tier even when
  siblings are cheap** (P2 tier-inheritance). Files: prompt DELEGATION section. Verify:
  cheap-tier share of tokens rises without resolve loss.
- [ ] **TP.5** ESCALATE-or-STOP discipline (Phase 6): freeze a verified artifact;
  escalate on the verifier's evidence, not on thrash. Overlaps backstop C2.
- [ ] **TP.6** Periodic plan/criteria **re-injection** on long runs (§0.6-E) — re-surface
  the acceptance criteria + open plan items every N turns to counter drift and
  lost-in-the-middle. Files: `cc-harness-hooks.py` (turn-count trigger) or prompt-side
  reminder. Verify: on a long-horizon task the criteria are re-stated; A/B drift rate.

> Phase P is the primary track; Phases 1–3 below are its safety net. Sequence P.1 →
> (P.2+P.3) → P.4 → P.6, A/B each against the current single-agent arm before adopting.

### Phase 0 — Infra reliability (parallel, unblocks the most tasks)
- [~] **T0.1** Retry-on-dropped-turn / mid-loop resume detector (A1). **Core done
  (no-verdict deaths):** `harness_terminal.py` `run()` flags `no_result_death`; coordinator
  `_is_no_result_death_meta` + `_eligible_rerun_ids` rerun it (budget-capped, same as
  `silent_death`). Files: `tools/harness_terminal.py`, `coordinator/server.py`,
  `coordinator/test_failure_rerun.py` (scenario F). Offline-verified (suite A–F green +
  `py_compile`); needs rebake + fly validation at end-testing. **Remaining:** mid-turn
  gateway-death with a graded reward-0 verdict (trajectory error-signature) — deferred
  pending real-trace calibration.
- [ ] **T0.2** Investigate large-payload death correlation (A2) against gateway logs;
  add image downscale/tile if confirmed. Files: agent image-read path.
- [ ] **T0.3** Mid-flight trial-reaper audit (A4) + rerun-survives-death. Files: trial
  runner. Verify: a killed trial's rerun completes.

### Phase 1 — Ground Gate V (the load-bearing change)
> **Phase 1 + Phase 2 (T1.1–T1.5, T2.1–T2.2) verified DONE — 2026-07-22 reconciliation.**
> The prior session shipped the full stack; the checkboxes below were stale. Selftest
> Cases 18/18b/19/20/21/22/22b cover it. `function:line` refs are into `cc-harness-hooks.py`.

- [x] **T1.1** Verify-strength classifier — ✓ `_verify_strength:748` returns `(weak, reason)`,
  detecting all four categories: `existence-only` / `self-referential` / `no-comparison` /
  `tamper` (regexes `_EXISTENCE_ONLY_RE`, `_GREP_SIGNATURE_RE`, `_NO_COMPARISON_RE`,
  `_KNOWN_RUNNER_RE`); `verify_weak` flag set at record time (`:1164`). Cases 18/18b.
- [x] **T1.2** Gate V counts only non-weak greens — ✓ `_last_green_verify_ts:336` skips
  `verify_weak` events (`:347`); `evaluate_gate:1443` emits a weak-specific block when the
  only green is weak. Cases 18/18b.
- [x] **T1.3** Override path + cap — ✓ `# independent: <why>` clears ONLY `self-referential`
  weak (`:1164`, `_INDEPENDENT_OVERRIDE_RE`); banned classes never override; capped
  (`TAUTOLOGY_DENY_CAP=1`). Case 19.
- [x] **T1.4** Rule N — ✓ **resolved: kept complementary, not subsumed (by design).** `rule_n:1675`
  stays the narrow PreToolUse fast-path (tautology shape); the general 4-category
  classification runs at PostToolUse via `_verify_strength:748`.
- [x] **T1.5** Tamper-resistance — ✓ `_verify_tampered:724` hashes referenced files at first
  sight (`_parse_referenced_paths:695`), stored as `ref_hashes` on the cmd event; a green after
  any edit/delete → `weak,"tamper"`, which ALWAYS bans override (`:765`). Case 20.

### Phase 2 — Wrongness-shaped escalation
- [x] **T2.1** Gate E coupled to weak-verify — ✓ `_harness_escalate_on_weak:585` hardwired on;
  both `_gate_e_panel:1239` and `_gate_e_ladder:1280` fire on `weak_v_blocks >= _weak_verify_escalate_n()`
  (`=GATE_CAPS["V"]=2`); `weak_v_blocks:1399` sums only `reason=="weak"` V-blocks. Case 21.
- [x] **T2.2** Anti-over-escalation / trust-the-green — ✓ Rule G `rule_g:1720`: a non-weak green
  for the file + no new red since blocks re-opening it; override-able once, capped
  `TRUST_GREEN_DENY_CAP=2`. `_harness_trust_green:601` hardwired on. Cases 22/22b.
- [x] ~~**T2.3** *(optional)* Config-gated wrongness detectors (C3), off by default~~ —
  **VOID (2026-07-22).** Config-gated + off-by-default conflicts with the "no flags" rule
  (like T3.2); wrongness signals fold into the always-on weak-verify path.
- [ ] **T2.4** **sol-high (fable) hit-rate instrument (C4, §0.6-D).** Record rung-2
  spawns → resolved in the run telemetry so the high-effort rung's real conversion is
  visible; gate it to the hardest rung only. Files: `cc-harness-hooks.py` /
  status/telemetry path. Verify: rung-2 fire+resolve counts surface in `status.sh`.

### Phase 3 — Nudge channel + config
- [x] **T3.1** Channel-B PostToolUse nudge on weak verify — ✓ `_WEAK_NUDGE_DEFAULTS:810`
  (4 baked reasons), `_weak_verify_nudge_text:838`, injected as `additionalContext` by
  `_post_record_context:1055` (once per distinct reason, ledger-deduped). Nudge text is
  BAKED, not env/config-sourced — per the "no flags" rule (T3.2 is void).
- [x] ~~**T3.2** Env/secret-driven nudge-config (text + thresholds + detector flags)~~ —
  **VOID (2026-07-22).** The "no flags" rule: the harness runs its improved behaviour
  unconditionally, with nudge text/thresholds BAKED (`_WEAK_NUDGE_DEFAULTS` in
  `cc-harness-hooks.py`), never env/config-driven — no reader, no `run-distributed.sh`
  passthrough. Superseded by T3.1's baked defaults.
- [ ] **T3.3** *(SWE arms only, separate)* Channel-A verify-independence `ur|rsk`
  nudge on `file_edit`. Out of scope for the TB fix; track separately.
- [ ] **T3.4** *(SWE arms only, separate)* **Trajectory info-access audit (§0.7).**
  Static hardening (seal `.git`, cut network) kills passive leakage, but active
  shortcut-seeking survives it — an agent that fetches an upstream fix (`pull/NNNN.diff`)
  resolves at 72% (+12pp) while looking "verified". Flag a success whose trajectory
  depended on fetching the answer. Low risk on self-contained terminal tasks (no
  upstream PR); load-bearing for SWE/REPAIR. Track separately.

### Phase 4 — Prompt discipline
- [x] **T4.1** D1–D4 in `_build_autonomy_prompt` + `run-instance.sh` byte-identically —
  ✓ D1(execute)/D2(verbatim)/D4(implausible-score→escalate) added prior session; D3
  ("Install by the canonical path" — official build/install steps + install missing
  runtime) added 2026-07-22. Parity MATCH (8210/8144 chars); `check_prompt_parity.py` green.

### Phase 5 — Docs + validation
- [x] **T5.1** Update `HARNESS_UNIVERSAL.md` (gates §4, escalation §5, env-toggle
  inventory) in the SAME commits as T1–T4 (parity rule). ✓ 2026-07-22: §6 toggle
  table rewritten (`HARNESS_HOOKS` marked REMOVED, `HARNESS_PROFILE` RETIRED,
  `ESCALATION_PANEL` the sole runtime toggle; bare-baseline-via-git-checkout note);
  §7 removed the bare-token guard + conductor tier corrected to SONNET (matches the
  opus→sonnet revert); §9 D3 bullet; §11 file-map + §12 "what landed" (always-on +
  no_result_death). Also swept the retired toggles out of `harbor_agents.py`,
  `run-instance.sh`, `run-distributed.sh`, `run-benchmark.py`, `cc-harness-hooks.py`,
  `test_cc_harness_hooks.py`: zero live env reads remain (all residual refs are
  removal-note prose). Offline-verified: py_compile OK, `--selftest` 56/56,
  `test_cc_harness_hooks.py` 29 passed, failure-rerun A–F ALL PASS, parity MATCH
  (8210/8144), `bash -n` both scripts.
- [ ] **T5.2** Rebake `unerr-claude-toolbox`; re-prepare fleets. *(END-TESTING — user-triggered)*
- [ ] **T5.3** **Ablation re-run of the 29** (see §11) to measure lift per lever.
  *(END-TESTING — user-triggered; bare baseline now via a pre-harness git-checkout
  image, since the `HARNESS_HOOKS=0` opt-out was removed.)*

---

## 11. Expected impact & measurement

**Rough attribution of the 22 genuine misses to the levers** (a task can need more
than one):

- **Infra reliability (Phase 0):** ~5 clean flips (crack-7z, query-optimize,
  protein-assembly, extract-moves, train-fasttext) + 2 compounded.
- **Grounded Gate V alone (Phase 1):** ~4–6 terra-reachable flips (sanitize-git-repo,
  overfull-hbox, torch-pipeline, dna-assembly; log-summary/raman/install/dna-insert
  in the recovered set become deterministic rather than lucky).
- **Grounded Gate V → escalation (Phases 1+2):** ~4–5 opus-tier flips
  (gpt2-codegolf, torch-tensor, pytorch-recovery, make-mips, path-tracing).
- **Underconstrained (extract-elf, mteb-retrieve):** false-green stopped; flip
  uncertain (no in-container oracle).
- **Perception (chess-best-move):** needs programmatic template-match / multimodal —
  1 task, separate lever.

**Measurement:** re-run the 29 (and a control slice of the passing 60) with each lever
toggled — infra-only, +grounded-V, +escalation-coupling, +prompt — via the existing
config/env toggles so the ablation needs **re-prepares, not rebakes**, after the
one-time Phase-1/2 rebake. Track resolved-count, **real LiteLLM cost by tier**
(status.sh --cost), and escalation-fire count. Success = higher resolve at
**equal-or-lower conductor spend**, with escalation firing on the hard tail and NOT
on already-correct artifacts.

---

## 12. Risks & open questions

- **Independence is undecidable.** B1's classifier will have false positives/negatives;
  the override path (B3) and caps keep it from looping or blocking legitimate
  task-statement literals. Tune conservatively — a missed weak-verify is a lost task;
  a false weak-flag on a correct verify is a bounded, override-able nudge.
- **Grounded Gate V raises cost** (more escalation, longer runs). That is the *point* —
  but it must be measured against the conductor-tier saving (§4). If grounded-V +
  sonnet-conductor beats sol-conductor on resolve-per-dollar, keep sonnet.
- **Config-by-env** must never leak into the graded `model_patch` (state already lives
  in `/tmp`, outside the repo — preserve that).
- **Terra→sol repoint interaction:** do not evaluate the repoint until Gate V is
  grounded; otherwise sol-conductor's false-greens confound the comparison.
