# unerr-bench — feature-resolved, cross-harness A/B benchmark framework

> Status: **spec; partially built** (spec 2026-06-14, last revised 2026-06-28).
> Supersedes the single-pillar framing of `internal/live-ab/` by generalizing it
> into a multi-pillar, two-harness, multi-competitor suite. The navigation,
> localization, e2e, and live-ab benchmarks are now the *components* of this
> framework, not separate one-offs:
>
> - **Verticals** live under `internal/` (`navigation/`, `compression/`, `live-ab/`,
>   shared `lib/`).
> - **The end-to-end "total bill" run** lives under `e2e/` — `codex/` (unerr + Codex,
>   `local-docker/` + `fly-remote/` backends) and `econ/` (the team's econ-coding-agent
>   arm, scaffolded), sharing offline-Pro entitlement + the SWE-Effi scorer via
>   `e2e/common/`.

## 1. Why this exists

The benchmarks the market publishes (ours included, so far) measure **one
thing**: tokens saved or text compressed. unerr ships six distinct capabilities;
a token-savings number proves only one of them and silently takes credit for the
other five. We need a framework that **measures each feature on its own terms**,
**runs inside more than one agent harness** so a win can't be a harness artifact,
and **puts competitors in the same table** on the pillars where they actually
compete — and shows the empty cell where they have no entry at all.

Two findings from the benchmark literature drive every design choice below:

1. **The harness counts as much as the model.** The same model scores ~4 points
   apart across harnesses, and one CLI burns 3–4× fewer tokens than another
   *natively* on identical tasks. A raw "Harness A vs Harness B" number measures
   the harness, not unerr. → We never report cross-harness absolutes as a
   headline. The unit is **within-harness A/B: same harness + model, unerr ON vs
   OFF**, repeated in *both* harnesses so the claim is "unerr helps whoever is
   driving."
2. **A blended score hides which feature did the work.** Our own first A/B "win"
   (n=1) turned out to be doc-first routing variance, not unerr — 0 MCP calls, 0
   compression events fired. A valid A/B needs **n ≥ 5**, **graph-exercising
   tasks**, and **big-shell-output tasks**. → The framework is **feature-resolved**:
   one pillar per capability, each with its own probe tasks, baseline, and metric.
   A blended "platform index" may appear only as a clearly-labeled secondary
   rollup, never the headline.

## 2. Core principles (the methodology contract)

| Principle | Rule |
|---|---|
| **Within-harness A/B** | Hold harness + model fixed; toggle unerr. Report the *delta*. Mirrors CodeCompass (unaugmented Claude Code vs augmented, dependency-graded tasks). |
| **Two harnesses** | Every pillar runs in **Claude Code** (subscription, `claude -p`, real `usage` JSON) AND **Codex CLI** (ChatGPT API). A capability that only helps one harness is a weaker claim than one that helps both. |
| **Feature-resolved** | One pillar = one capability = one metric + its own probe-task pack + its own baseline. No capability borrows another's credit. |
| **Fidelity-gated** | Every "saving" or "win" passes a correctness check first. Fewer tokens by losing the answer, or a faster turn that ships a regression, is not a win. (Carried from the existing suite.) |
| **Held-out + powered** | n ≥ 5 reps per cell, fixed seeds, report mean + CI. Use private/held-out tasks where contamination matters (the SWE-bench memorization problem). |
| **Conservative baseline** | The OFF arm models a *disciplined* agent and under-counts where in doubt, so the measured delta is a lower bound. |
| **Competitors run native** | Each rival is driven through its own correct interface, pinned version + commit. Running a rival wrong is the fastest way to lose credibility. |
| **No silent caps** | Any dropped task, skipped arm, or uncovered pillar is printed in the report, never omitted. |

## 3. Harness adapters — parity via hooks

Both target harnesses expose the **same lifecycle hook surface**, so unerr's
guardrails and note-injection can run in either. The framework ships one adapter
per harness that maps unerr's hook logic onto that harness's config format and
reads its run telemetry.

| unerr surface | Claude Code hook | Codex CLI hook | Notes |
|---|---|---|---|
| Inject anchored notes on prompt | `UserPromptSubmit` (additionalContext) | `UserPromptSubmit` (`hookSpecificOutput.additionalContext`) | parity |
| Pre-edit cascade-guard deny | `PreToolUse` deny | `PreToolUse` `permissionDecision:"deny"` (intercepts `apply_patch` = Edit/Write, and MCP tools) | parity for edits + MCP |
| Read-routing redirect | `PreToolUse` on Read | `PreToolUse` on MCP/`apply_patch` | parity |
| Session markers / continuation | `Stop` | `Stop` (`decision:"block"` → continuation prompt) | parity |
| Post-edit review signal | `PostToolUse` | `PostToolUse` | parity |
| Run telemetry source | `claude -p` JSON `usage` + `.unerr/metrics.db` | Codex transcript `usage` + `.unerr/metrics.db` | both give real tokens/turns |

**Known Codex caveat to state in the report:** Codex `PreToolUse` does not yet
intercept every shell path (`unified_exec` streaming is incomplete) and skips
`WebSearch`/non-shell-non-MCP tools. It **does** intercept `apply_patch` (edits)
and MCP tool calls, which is what the cascade-guard and read-routing pillars
need. Bash-mediated edits that bypass `apply_patch` are an uncovered edge — note
it, don't hide it.

**Cost asymmetry to respect:** Claude Code subscription runs report
`total_cost_usd = 0`; the savings signal there is **input tokens + turns**, not
dollars. The Codex arm (API key) reports real dollars. The report keeps token,
turn, and dollar columns separate and never sums across the two.

## 4. The six pillars

Each pillar is independently runnable and independently reported. "Competitor
column" names the *category* of rival (kept generic per repo policy — never the
product name).

### Pillar 1 — Navigation / token savings
- **Claim:** one graph query replaces many grep + read cycles.
- **Metric:** tokens-per-navigation-op (fidelity-gated); turns + resolve-rate in E2E.
- **Probe tasks:** the existing head-to-head frozen corpus (`internal/navigation/head-to-head.ts`) + CodeCompass-style tasks
  graded by dependency-discoverability (easy/medium/hard to find the dependency).
- **Baseline (OFF):** disciplined grep + read, top-N files only (lower bound).
- **Competitor arms:** code-graph tools, MCP code-index / retrieval servers,
  output-compression tool (as the compression-only ceiling).
- **Harness coverage:** Claude ✅, Codex ✅.

### Pillar 2 — Cascade guard / blast-radius
- **Claim:** stops the agent breaking callers it never opened.
- **Metric:** *breaking-change-caught rate* = (regressions avoided) / (regressions
  the OFF arm shipped). A "regression" = a seeded signature edit whose callers'
  test goes red.
- **Probe tasks:** edit tasks with a known caller fan-out of N; the gate test
  covers the callers, not just the edited entity.
- **Baseline (OFF):** unguarded agent, same harness.
- **Competitor arms:** general guardrail / policy tools (most have no graph-aware
  caller model → the empty cell *is* the result: the regression they'd ship).
- **Harness coverage:** Claude ✅, Codex ✅ (via `PreToolUse` deny on `apply_patch`).

### Pillar 3 — Reasoning improvement
- **Claim:** the right context up front → fewer wrong turns and dead-ends.
- **Metric:** wrong-edit rate, backtracks, turns-to-resolve, plan quality
  (LLM-as-judge with a fixed rubric, calibrated against a human sample).
- **Probe tasks:** cross-file tasks where the answer is NOT in the first file the
  agent opens (forces real navigation/reasoning).
- **Baseline (OFF):** same harness, no unerr.
- **Competitor arms:** none direct — this is an outcome of pillars 1+4; report it
  as a downstream effect.
- **Harness coverage:** Claude ✅, Codex ✅.

### Pillar 4 — Context management / memory
- **Claim:** a fact learned in an earlier session helps a later task.
- **Metric:** dependent-task accuracy + recall@k, **warm vs cold** (the existing
  `unerr` vs `unerr-nomemory` arm). Adopt LongMemEval's five ability axes:
  information extraction, multi-session reasoning, temporal reasoning, knowledge
  updates, abstention.
- **Probe tasks:** `dependsOn` task chains where task B needs a fact from task A.
- **Baseline (OFF):** memory-wiped unerr arm (isolates memory from the rest).
- **Competitor arms:** memory-server products, MCP-memory tools.
- **Harness coverage:** Claude ✅ (auto-injection), Codex ✅ (auto-injection via
  Codex `UserPromptSubmit` — same as Claude, plus the `unerr_context` recall tool).

### Pillar 5 — Context cleaning / rot resistance
- **Claim:** compression keeps the needle as the working context grows.
- **Metric:** needle-survival rate + a **context-rot degradation curve** (task
  accuracy vs injected-context length), unerr ON vs OFF. Adopt Chroma's
  context-rot method (extended NIAH + semantic + conversational QA).
- **Probe tasks:** long-session / big-shell-output tasks with planted "needle"
  records (UUIDs, the one error line among 200), à la the standard needle design.
- **Baseline (OFF):** raw uncompressed tool output, same harness.
- **Competitor arms:** output-compression tool, head-to-head on its home turf.
  **Cite its median compression, not its hero-table numbers** — its own published
  median is ~5%, not the 60–95% on cherry-favorable content.
- **Harness coverage:** Claude ✅, Codex ✅.

### Pillar 6 — Drift / re-anchoring
- **Claim:** rules stay attached to the right code when the code moves.
- **Metric:** re-anchoring precision/recall — after a refactor that moves/renames
  an entity, does the note still fire on the correct entity and not a namesake?
- **Probe tasks:** move / rename / extract refactors with a note pre-anchored to
  the moved entity.
- **Baseline (OFF):** none — this is a unerr-unique capability; the result is
  pass/fail re-anchoring accuracy, plus the "competitors have no equivalent" cell.
- **Competitor arms:** none.
- **Harness coverage:** Claude ✅, Codex ✅ (hook-driven, harness-agnostic).

## 5. Competitor arm matrix

Rivals appear only on the pillars where they actually compete. An empty cell is a
*reported finding* ("category has no entry"), not a hidden gap.

| Pillar | code-graph | output-compressor | MCP code-index | memory-server | guardrail/policy | context-mgmt |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| 1 Navigation | ✅ | ceiling | ✅ | — | — | — |
| 2 Cascade guard | — | — | — | — | ▵ no caller graph | — |
| 3 Reasoning | downstream | downstream | downstream | downstream | — | — |
| 4 Memory | — | — | — | ✅ | — | ✅ |
| 5 Context cleaning | — | ✅ | — | — | — | ✅ |
| 6 Drift re-anchoring | — | — | — | — | — | — |

▵ = rival exists in the category but lacks the graph-aware mechanism; the cell
reports what it ships instead (the regression).

## 6. Task-pack schema

Generalizes `internal/live-ab/types.ts`. One pack per pillar; a pack is a
list of probe tasks plus the metric extractor that scores them.

```ts
type PillarId =
  | "navigation" | "cascade-guard" | "reasoning"
  | "memory" | "context-cleaning" | "drift";

type HarnessId = "claude-code" | "codex-cli";

interface ProbeTask {
  id: string;                 // stable; also SWE-Effi instanceId
  pillar: PillarId;
  title: string;
  prompt: string;             // handed to the agent verbatim
  setupCommand?: string;      // per-arm one-time env setup
  breakCommand?: string;      // introduce the bug/condition (omit if repo ships it)
  gate: GateSpec;             // how this task is judged (see below)
  dependsOn?: string[];       // memory carry-over chain
  expectedSignal?: string;    // which unerr signal SHOULD fire (e.g. "cascade_guard")
  difficulty?: "easy" | "medium" | "hard"; // dependency-discoverability grade
}

type GateSpec =
  | { kind: "test"; testCommand: string }                  // exit 0 = pass
  | { kind: "regression"; callerTestCommand: string }      // callers stay green
  | { kind: "fidelity"; mustContain: string[] }            // answer survived
  | { kind: "needle"; needles: string[] }                  // planted items survived
  | { kind: "reanchor"; entity: string; afterMove: string }// note fired on right node
  | { kind: "judge"; rubricId: string };                   // LLM-as-judge + rubric

interface Arm {
  id: string;                 // "off" | "unerr" | "unerr-nomemory" | "<rival>"
  harness: HarnessId;
  unerr: boolean;
  memoryWarm: boolean;
  rival?: string;             // generic category id, never a product name
}

interface BenchManifest {
  repo: string; baseCommit?: string; setupCommand?: string;
  pillars: PillarId[];
  arms: Arm[];
  reps: number;               // >= 5
  packs: Record<PillarId, ProbeTask[]>;
}
```

## 7. Scoring & report

- **One `REPORT.md` scorecard.** Rows = pillars. Columns = {OFF, unerr, each rival}
  × {Claude, Codex}. Each cell: metric value + fidelity pass-rate + n + 95% CI.
- **No headline blended score.** A weighted "platform index" may sit at the bottom,
  labeled as a rollup, weighted by real navigation frequency from the shadow
  ledger (understand-file 51% / find-symbol 30% / get-entity 11% / find-callers
  7% / imports 1%), with uncovered ops charged at their fallback cost.
- **Per-pillar provenance appendix.** Versions, commits, seeds, the exact prompts,
  and every task's pass/fail so a reader can replay it. Reproduce-from-one-command,
  per the existing suite.
- **Reuse:** `internal/lib/` tokenizer + fidelity gate, `e2e/common/scoring/swe-effi.ts`,
  `internal/live-ab/metrics-reader.ts` (behavior_events classifier), `internal/live-ab/claude-driver.ts`.
  **New:** `codex-driver.ts`, the six probe-task packs, the hook adapters, the
  per-pillar competitor arms, the unified scorecard renderer.

## 8. Build phases + acceptance criteria

| Phase | Deliverable | Acceptance |
|---|---|---|
| P0 | Harness-adapter layer (Claude + Codex hook writers; telemetry readers) | a no-op task runs end-to-end in both harnesses, real `usage` captured |
| P1 | `codex-driver.ts` + OFF/unerr arms on Pillar 1 (navigation) | navigation delta reproduces in both harnesses, fidelity-gated, n≥5 + CI |
| P2 | Pillar 2 (cascade guard) probe pack + regression gate | a seeded caller-breaking edit is denied in both harnesses; OFF ships the regression |
| P3 | Pillars 4 & 5 (memory chains, needle/rot curve) | warm>cold on memory; needle-survival + rot curve plotted ON vs OFF |
| P4 | Pillars 3 & 6 (reasoning judge, drift re-anchor) | judge rubric calibrated vs human sample; re-anchor precision/recall reported |
| P5 | Competitor arms per §5 + unified `REPORT.md` renderer | every rival runs native + pinned; empty cells printed; one scorecard emitted |

## 9. Honesty / limitations (stated up front)

- **Codex `PreToolUse` shell-interception is partial** — edits via `apply_patch`
  and MCP tools are covered; Bash-mediated edits are an uncovered edge.
- **LLM-as-judge (Pillar 3) needs human calibration** or it measures the judge.
- **Subscription cost = 0** — read tokens + turns there, never dollars.
- **Memorization risk** — prefer held-out/private tasks for the E2E pillars; the
  literature shows frontier models can reproduce public gold patches from the task
  id alone.
- **Per the recalled A/B note:** any pillar's A/B is only valid at n≥5 with
  graph-exercising and big-shell-output tasks; a single run is variance, not a
  result.

## 10. References (methodology grounding)

- CodeCompass / "Navigation Paradox" — graph-structured navigation vs retrieval,
  A/B vs unaugmented Claude Code on dependency-graded tasks.
- SWE-bench Verified / Pro, UTBoost — contamination + test-augmentation rigor.
- SWE-Effi — token-bounded effectiveness (used in `e2e/common/scoring/swe-effi.ts`).
- LongMemEval (+ V2) — five long-term-memory ability axes.
- Chroma "Context Rot" — degradation-vs-length curve, extended NIAH.
- LiveMCPBench / MCP-Bench / MCP-AgentBench — MCP tool-use eval, distractor
  servers for retrieval precision, token-vs-passrate tradeoff.
- SEAL standardized scaffolding — identical tooling + fixed turn limit across arms.

**External score snapshot.** Current published SWE-bench Verified / Pro / Mini
numbers for the frontier coding models (the absolute anchors our paired ±unerr
delta sits on top of), with the scaffold-dependence caveat and sources, are kept
dated in [`e2e/REFERENCE-SCORES.md`](e2e/REFERENCE-SCORES.md). As of 2026-06-28:
GPT-5.5 88.7% and Claude Opus 4.7 87.6% lead full Verified; **GPT-5.3-Codex 85.0%**
is the Codex-scaffold anchor for the `e2e/codex` arm; Opus 4.7 leads SWE-bench Pro
at 64.3%.