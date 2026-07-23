# Routing · Decomposition · Localization — the instructions that drive execution

Companion to [`HARNESS_IMPROVEMENT_PLAN.md`](HARNESS_IMPROVEMENT_PLAN.md) §0.5. This
is the concrete content for the protocol's front half — **SCOPE → ROUTE →
DECOMPOSE/ASSIGN → LOCALIZE** — i.e. "get the whole flow ready before executing, then
drive it." Verification/escalation (the back half) live in the plan doc.

> Class-distribution counts (how often each class appears, how often the core exceeds
> sonnet) are calibrated from the full 89-task TB2.1 set in §6 — headline: the hard
> core exceeds sonnet on **35 of 89 tasks (39%)**. The *rules* below do not depend on
> those counts. Every section is grounded in the 2024–2026 literature reviewed in
> plan-doc §0.6.

---

## 0. Two load-bearing principles

**P1 — Route by CLASS, not by self-assessed difficulty.** The model reliably
identifies *what kind* of task this is (keywords, file types, imports, the grader's
shape); it does NOT reliably know *whether it will find it hard* — the dominant
failure is finishing a hard task **confidently wrong** (pytorch-model-recovery
ignored a baseline MSE ≈ variance and finished in 104s). So the execution tier is a
**deterministic function of the capability class** (§2 table), with difficulty
signals only bumping within a class. Never gate tier on "do I feel stuck" — on the
worst cases the model never feels stuck.

> _Research basis:_ knowing-when-to-abstain is **uncorrelated with task-solving
> capability** across 17 frontier models (*AgentAbstain*, 2026), and self-confidence
> is systematically miscalibrated — worsening **as an agentic run proceeds** (*Barkan
> et al.*, 2025), i.e. exactly on the long hard tasks. Routing correlates with class,
> not a scalar hardness score (RouterBench cost↔quality ρ≈0.7). There *is* a weak,
> real, **task-class-dependent** self-signal (*Beyond Confidence*, 2026), which is why
> we condition on class — a resolution the raw "am I stuck" signal doesn't have. See
> plan-doc §0.6-A,D.
>
> _Second-round validation (plan-doc §0.7):_ class/domain pre-classification for
> routing is empirically confirmed (MoDEM), and a good router reaches ~98% — but a
> naive one is only ~80% and **misroutes silently**. Under-routing a hard task to
> sonnet is exactly our silent-fail, so **route UP under class ambiguity** (asymmetric
> misrouting cost) and lean on escalation as the safety net. And never gate on
> verbalized confidence or the difficulty *number*: the decision-action gap means
> "I'm unsure" doesn't produce cautious behavior — **gate on infrastructure signals**
> (the grounded-verify outcome), not self-report (Zylos 2026).

**P2 — Split the HARD CORE from the OPERATIONAL SCAFFOLDING, then tier each
independently.** Almost every task = a small hard-reasoning core wrapped in cheap
scaffolding (install, build, run, parse, render, file IO). Decomposition must isolate
the core and give it the tier its *own* class demands, even when the surrounding
subtasks are trivial. chess-best-move = `extract-position` (perception) +
`find-best-move` (trivial engine). The whole task is not "one sonnet job"; the
perception subtask is where it died.

---

## 1. SCOPE output — the routing manifest (produced before the first edit)

SCOPE emits a small structured manifest that ROUTE/DECOMPOSE/EXECUTE all read. It is
the plan; it is the working memory; it is what the independent verifier checks
against.

```
manifest:
  shape:            REPAIR | PRODUCE | OPERATE
  class:            <capability class from §2>
  acceptance_surface:                 # what "correct" actually is
    output_path:    <exact path/filename>
    format:         <exact format / schema / field names>
    constraints:    [<every stated rule — enumerate ALL, not just the salient one>]
    tolerances:     [<numeric bands, similarity thresholds, exact strings>]
    grader_shape:   visible-provided | hidden-adversarial | unknown
  subtasks:                           # the decomposition (§3)
    - id: <name>
      kind: CORE | SCAFFOLDING
      class: <class of THIS subtask>  # may differ from the task class
      tier:  haiku | sonnet | opus | opus-high | perception
      verify: <how this subtask is independently checked>
  core_tier:        <max tier over CORE subtasks>   # drives ROUTE
  verification_plan: <the independent check — see per-class §2 and plan-doc §6>
  risks/unknowns:   [<underconstrained? no in-container oracle? perception gap?>]
```

Rules for SCOPE:
- **Extract the acceptance surface from the task statement AND the grader** — exact
  output path, filename, format, field names, value constraints, tolerances. On
  PRODUCE these ARE correctness, not presentation (caffe wanted the canonical
  `.build_release` path; overfull wanted synonym-legality; gcode wanted the exact
  leetspeak flag).
- **Enumerate EVERY constraint — into the `constraints:` list (the VERIFIER's
  checklist), NOT the executor's live prompt.** The recurring miss is an *unchecked*
  constraint, not the main goal — so every constraint must be *checked*. But do not
  keep the full enumeration in the working prompt while solving: constraint **count**
  (independent of prompt length) measurably taxes core task-solving and reaches even
  Sonnet-4.5 (*SustainScore*, 2026; *IFScale*, 2025 — silent omission + primacy bias).
  The executor holds only the current subtask + its matched class playbook; the full
  constraint set is applied by the independent verifier (plan-doc §0.5 Phase 5).
- **Keep the plan LEAN and MATCHED, criteria FIRST.** A padded/over-stuffed plan hurts
  more than no plan, and misaligned early phases degrade results (*From Plan to
  Action*, 2026, 16,991 SWE trajectories; *Learning When to Plan*, 2025). Put the
  non-negotiable acceptance criteria at the TOP of the manifest (primacy), and on long
  runs **re-inject** the plan/criteria periodically to counter drift and
  lost-in-the-middle (plan-doc §0.6-E, TP.6).
- **Classify `grader_shape`**: if the task provides a runnable/visible test, the
  self-check can be faithful; if the grader is hidden/adversarial or the goal is
  universal ("block ALL", "byte-identical", "exact"), any self-authored check is only
  a proxy — flag it so VERIFY uses an empirical/known-robust method. Note (plan-doc
  §0.6-C): even a provided grader run **inside the agent's writable container** is a
  tamper channel — the verifier treats an edited checker/`expected` fixture as failed.

---

## 2. ROUTE — capability-class table (deterministic class → tier + methods)

`core_tier` is the MINIMUM tier that can reliably do the class's hard core (not the
whole task). `perception` means "no text tier — must be extracted programmatically
and cross-checked; escalate to multimodal only if no programmatic path exists."

| Class | Detect signal | Usual shape | Core tier | Decomposition pattern | Real (independent) verification | Localization means | Known trap |
|---|---|---|---|---|---|---|---|
| **perception-vision** | image/video/png/frame/screenshot/OCR/pixels/render-to-match | PRODUCE | **perception** (then downstream tier) | extract-ground-truth-programmatically → **cross-check by 2nd method** → downstream logic | re-derive the perceived fact a 2nd way AND check the downstream answer; never a single visual read | the perceived fact (FEN/frame idx/text) — programmatic, never model-eyes | reads pixels with its eyes → confidently wrong; tautology on wrong perception |
| **numerical-scientific** | fit/optimize/sample/estimate/simulate; tolerances; spectra/MCMC/primers/physics | PRODUCE | sonnet (opus if domain reasoning non-trivial) | parse-data-correctly → model/fit [core] → independent-recompute | recompute with an INDEPENDENT method; assert domain invariants (G≈1580; Tm bands; gamma-off-bound) | the data-parse contract + the invariant that defines "physically right" | loose tolerances; self-referential recompute; decimal/sep parse bug |
| **systems-lowlevel** | ELF/binary/objdump/emulator/syscall/ABI/QEMU/boot/memory/register | PRODUCE/OPERATE | **opus / opus-high** | install/setup [scaffold] → emulation/relocation/ABI core [opus] → exercise-real-behavior | run the artifact vs a REAL reference behavior (frame pixel content, memory image), not a header/structure check | the DEFINING component (emulator's reader, loader's relocation), not a flow site | wrong-layer fix (patch the WAD not the reader); structural verify passes content-wrong |
| **ml-frameworks** | torch/caffe/fasttext/transformer/distributed/train/quantize/state-dict | PRODUCE | opus (sonnet if straightforward train-to-metric) | official-build [scaffold] → correctness core [opus] → run/train → independent metric/reference | compare vs an INDEPENDENT reference impl or a held-out metric you didn't construct; reject degenerate output | the correctness-defining piece (autograd f/g, exact arch, tokenizer/decode) + the canonical build path | "it runs/compiles" false-green; self-test embeds same misconception; non-canonical build |
| **text-data-transform** | parse/aggregate/transform text·CSV·SQL·HTML·LaTeX·logs·regex | PRODUCE | sonnet | enumerate EVERY constraint → transform → check-per-constraint | recompute expected by logic INDEPENDENT of the transform; assert every constraint; diff on real data | the FULL constraint set (acceptance surface) — the miss is an unchecked constraint | verify checks salient goal only; recompute-with-same-logic tautology; format misread |
| **security-adversarial** | crypto/hash/crack/XSS/sanitize/bypass/secret/vuln | PRODUCE | sonnet (opus if universal-robustness vs hidden corpus) | classify EXISTENCE (one payload vs visible check) vs UNIVERSAL (block-all vs hidden corpus) → build → empirical test | empirical: run payload in a real browser / run the cracker / scan reachable git history the grader's way — never a self-authored proxy for a universal claim | for universal: recognize self-check is a proxy → hardened library; for secrets: whole history not working tree | universal claim "verified" on a few self-invented cases; regex where a parser is needed |
| **web-research-retrieval** | find/look-up a fact from a web/live source; "top X as of <date>" | PRODUCE | sonnet + search tool | retrieve-via-search-tool (not raw scrape) → cross-check claim vs complete data → exact-format write | does the chosen answer actually top/satisfy the fetched complete table? cross-check, don't compare to your own pick | the authoritative source + the exact selection criterion (date, "all-reported") | anti-bot/WAF → incomplete data; tautological "matches my pick" |
| **build-compile** | compile/port/build a real project (cython/pmars/pov-ray/compcert/cobol) | REPAIR/PRODUCE | sonnet (opus on real toolchain/lang friction) | onboard OFFICIAL build (CI/Makefile/README) → build → run its real check | the project's own test/build check + the expected artifact; canonical build path | the documented build command + the specific broken step | alt build system the grader doesn't expect; outer timeout on long builds |
| **devops-operate** | serve a port / git ops / grpc·kv store / wal recovery / webserver / ssh | OPERATE | sonnet | probe-state → make-change → **EXERCISE the running thing** (curl/ssh/client) | exercise the live interface end-to-end (grader connects/curls); git-history verified vs reachable history | the exercised interface (endpoint/port/client), NOT the config file | "config looks right" vs exercising; services-up not function-verified |
| **esoteric-codegen** | synthesize a program in an exotic substrate or under a hard size/step cap (regex/logic-gates/polyglot/codegolf/Redcode/self-hosting/≤2KB) | PRODUCE | **opus / opus-high** | *the whole task IS the core — almost no scaffold*; spend the top tier here, escalate by default | run the artifact through its exotic evaluator and diff vs reference (compile+run, apply the regex list, run the sim, pmars battle) AND honor the size cap | which rule/state/token in the target substrate | thinking combinationally where clocked state is needed; the size/step cap forces genuinely dense code |
| **scientific-modeling** | encode a statistical/probabilistic model (Stan/BN/DAG/prior/posterior/ARS) | PRODUCE/REPAIR | opus (sonnet if a standard fit) | correct model SPECIFICATION [core] → sampler/fit/install [scaffold] | posterior means / recovered structure vs reference bands; **distrust a lone stochastic self-test** | which parameter/edge/prior clause encodes the spec | wrong prior parameterization; a lenient stochastic grader hides a wrong model |
| **bio-design** | computational molecular biology with domain ground-truth (primer/Tm/PDB/fpbase/Golden-Gate/SDM) | PRODUCE | **opus / opus-high** | domain-rule DESIGN (sites/Tm/identity/spectral match) [core] → FASTA/file emission [scaffold] | **simulate the biology** (assembly/mutagenesis) and check vs ground-truth tools/APIs (primer3, PDB, fpbase) | which fragment/site/sequence the rule applies to | multi-hop specialized knowledge; Tm-window/cut-site rules easy to get subtly wrong |

**Conductor vs core tier.** The conductor (orchestrator) can stay **sonnet** — it
plans, decomposes, drives, and integrates but does no hard reasoning itself. It
DELEGATES: the CORE subtask to its class's tier (opus/opus-high sub-agent),
scaffolding to haiku/sonnet, and VERIFY to an independent opus sub-agent. "Switch the
whole reasoning to a higher model" is realized as **(a)** a pre-session tier pick when
the whole task's core is opus-class, or **(b)** an opus sub-agent hand-off for the
core subtask — not a mid-session main-loop swap (not possible in Claude Code).

> _Research basis for the "Real (independent) verification" column:_ every entry
> demands an **execution / reference / isomorphic** check rather than a self-authored
> proxy, because a check of only *extensional* correctness admits false-positives and
> reward-hacking (kept ≈0 by an isomorphic second check — plan-doc §0.6-C), tests
> written after the code inherit its faults (14% vs 25% fault-detection), and a
> same-family LLM judge over-accepts (§0.6-B). One cross-cutting fact from the 89-task
> calibration (§6): **on every TB class the grader is itself an independent exact /
> tolerance check the agent can run** — so the false-green losses come from the agent
> *substituting a weaker self-check* (or, for perception, hallucinating the
> extraction), not from a missing oracle. The router rule is therefore to force the
> real grader-shaped verification before "done".
>
> _Second-round validation (plan-doc §0.7):_ in the *agentic-coding* setting
> specifically, a separate verifier lifts real resolution (+3.5–19pp across learned
> critics, SWE-Gym verifiers, and Agentic Rubrics), and the strongest execution-free
> signal is **rubric decomposition** — turning the acceptance surface into fine-grained
> yes/no dimensions (spec-alignment, **over-scoped-edit**, integrity, runtime). It
> catches exactly the miss types tests miss (unnecessary edits, unchecked constraints —
> overfull-hbox's illegal synonym swaps) and reduces judge bias (2601.04171,
> 2606.26300). So the "independent verification" column above is realized as a **grounded
> rubric checklist**, tuned precision-first on ACCEPT (a false "verified" is the
> dominant harm — 2512.02304). This is the same checklist SCOPE builds (§1): **the
> constraint checklist IS the verifier's rubric.**

---

## 3. DECOMPOSE + ASSIGN — instructions

1. **Split CORE from SCAFFOLDING.** Scaffolding = install/build/run/parse/render/IO
   and any step with a deterministic, well-known method. CORE = the step whose
   *correctness is in question* (the fit, the emulation, the distributed-autograd, the
   perception extract, the constraint-satisfying transform).
2. **Type every subtask** with its own `class` and `tier` from §2. A subtask's class
   may differ from the task's (chess is perception-vision as a task, but `find-move`
   is a trivial engine subtask).
3. **Tier-inheritance rule:** a subtask keeps its CLASS's tier even when siblings are
   cheaper. Never average a task down to sonnet because most of it is easy — the one
   opus/perception subtask decides the outcome.
4. **`core_tier` = max tier over CORE subtasks** → drives ROUTE / the conductor's
   delegation.
5. **Assignment map:**
   - SCAFFOLDING (recon, read, run, summarize, install, render) → **junior (haiku)**.
   - SCAFFOLDING (scoped multi-file edits) → **worker (sonnet)**.
   - CORE, opus-class → **opus sub-agent**; CORE, opus-high-class → **fable**.
   - CORE, perception-class → programmatic extract + **cross-check** sub-task (two
     independent extractions must agree); multimodal only if no programmatic path.
   - VERIFY → **independent opus sub-agent** that did not write the solution
     (plan-doc §0.5 Phase 5).
6. **When NOT to split:** a small single-class task whose core is sonnet-tractable
   (most text-data-transform, devops-operate) — decompose into constraints, not
   sub-agents; over-delegation adds latency with no quality gain. Split when there is a
   genuine tier boundary (a perception or opus core) or independent parallel slices.
7. **Parallelism:** independent subtasks (multi-file scaffolding, several
   constraints to check) fan out in parallel; the CORE and its VERIFY are sequential.

---

## 4. LOCALIZE — instructions (per shape, with per-class overrides)

Localization = *pin the exact target before acting* — the analogue of "find the right
file" generalized to every shape.

- **REPAIR** (something broken): find the **definition site** of the wrong behavior —
  the entity whose behavior is wrong where it is DEFINED, not a downstream site where
  the bad value merely flows through. Use the unerr graph (`get_references`,
  `search_code`) when a codebase exists. (make-mips localized to "the WAD" — a flow
  site — instead of "the emulator's lump reader" — the definition; that is the
  wrong-layer trap.)
- **PRODUCE** (create to spec): localization = **pin the acceptance surface** — the
  exact output path/filename/format/field-names/constraints/tolerances from the
  statement AND the grader's real check. This is where caffe (build path), overfull
  (legality constraint), and gcode (exact flag casing) were lost.
- **OPERATE** (make a system work): localization = identify the **exercised
  interface** — the port/endpoint/client the grader will hit — and target that, never
  the config file. Verify by exercising it.

**Per-class localization overrides:**
- perception-vision → the perceived fact, extracted programmatically and
  cross-checked; never the model's visual read.
- numerical-scientific → the data-parse contract + the domain invariant.
- systems-lowlevel / ml-frameworks → the correctness-DEFINING component + the
  canonical build path the grader assumes.
- security-adversarial → for universal-robustness, that the self-check is only a
  proxy (→ hardened library); for secret-removal, the whole reachable history.
- web-research-retrieval → the authoritative source + the exact selection criterion.

---

## 5. Worked examples (from the 29 failures)

- **chess-best-move** → class perception-vision; decompose `extract-FEN` (perception:
  template/font-match the synthetic render; **cross-check** a 2nd extraction) +
  `find-move` (trivial Stockfish). VERIFY re-derives the FEN independently. Old
  failure: single visual read → wrong FEN → engine correct on a fake board → tautology.
- **make-mips-interpreter** → systems-lowlevel; core-tier **opus**. Localize the
  emulator's lump reader (definition), not the WAD (flow). VERIFY = boot to a real
  rendered frame, not a structure check.
- **torch-tensor-parallelism** → ml-frameworks; core-tier **opus** (Megatron f/g
  autograd). VERIFY compares per-rank grads vs an INDEPENDENT `nn.Linear` reference,
  not the impl's own semantics.
- **overfull-hbox** → text-data-transform; core-tier **sonnet**. SCOPE enumerates BOTH
  constraints (no-overfull AND synonym-legality); VERIFY asserts each. Old failure:
  checked only the salient goal.
- **caffe-cifar-10** → build-compile / ml-frameworks; onboard the OFFICIAL Makefile
  build (grader execs `.build_release/tools/caffe.bin`), not CMake.
- **mteb-leaderboard** → web-research-retrieval; retrieve via the search tool (raw
  scrape hit a WAF), cross-check the pick against the complete table.

---

## 6. Class & core-tier distribution (calibration)

_Calibrated from a full read of all 89 TB2.1 `task.toml` instructions + the
`test_outputs.py` graders (read-only recon, 2026-07-22). This is what sets the
router's default tiers and tells us the expected route/escalation rate and its cost._

### 6.1 Core-tier distribution — THE key number

`core_tier` = the minimum tier the *hardest subtask* needs (not the whole task).

| core-tier | n | share |
|---|---|---|
| haiku | 2 | 2% |
| **sonnet** | **52** | **58%** |
| opus | 20 | 22% |
| opus-high (fable) | 9 | 10% |
| perception (no text tier) | 6 | 7% |

**Router implication:** a flat-sonnet conductor can *scaffold* almost every task, but
the **hard core exceeds sonnet on 35 of 89 tasks (39%)** — 20 opus + 9 opus-high + 6
perception. This is the quantified case for §0.5 ROUTE + escalation: ~39% of the set
needs the core routed up, and the 6 perception tasks need a *different tool*
(programmatic pixel extraction + numeric cross-check), not a bigger LLM. It also
bounds cost: proactive class-routing should send roughly a third of tasks' cores to
opus/opus-high, not all 89 (the case against a blanket terra→sol conductor repoint,
plan-doc §4).

### 6.2 Class distribution (89 tasks)

| class | n | | class | n |
|---|---|---|---|---|
| security-adversarial | 11 | | build-compile | 6 |
| text-data-transform | 11 | | numerical-scientific | 5 |
| devops-operate | 11 | | scientific-modeling | 4 |
| esoteric-codegen | 10 | | bio-design | 3 |
| ml-frameworks | 10 | | web-research-retrieval | 2 |
| systems-lowlevel | 7 | | software-general | 2 |
| perception-vision | 6 | | abstract-reasoning | 1 |

### 6.3 Taxonomy reconciliation (9 candidate → 14 calibrated classes)

The calibration refined the original 9 classes into **14**. The router's §2 table +
§7 playbooks cover the 11 highest-volume classes; the deltas:
- **`numerical-scientific` split** → `numerical-scientific` (compute/optimize a number,
  n=5) + **`scientific-modeling`** (encode a statistical/probabilistic model, n=4) —
  different verification (numeric tolerance vs posterior/structure recovery).
- **`esoteric-codegen` added** (n=10) — the single most important addition; these hid
  under "systems/software" but their core is *algorithm-in-an-exotic-substrate*, which
  drives them to opus/opus-high regardless of any systems knowledge. **All 10 are
  opus or opus-high core.** Added to §2 + a §7 playbook.
- **`bio-design` added** (n=3) — computational molecular biology with domain
  ground-truth (primer3/PDB/fpbase). Added to §2 + a §7 playbook.
- **security-adversarial broadened** to fold in secret/forensic recovery and ReLU
  model-extraction (not just crypto/XSS).
- **`web-research-retrieval` shrank to 2** — most "download from HF" tasks are really
  `ml-frameworks` (model/tokenizer usage), not open-web fact-finding.
- **`abstract-reasoning` (n=1, ARC-style rule induction, opus)** and **`software-general`
  (n=2, concurrency / Coq proof)** — small tails; route by the §6.4 rules, no dedicated
  playbook.

### 6.4 Tasks whose core most clearly exceeds sonnet (name them)

These are the router's "route up from turn 1" set — do NOT let a sonnet conductor own
their core:
- **circuit-fibsqrt** — fib∘isqrt as a clocked logic-gate netlist under 32k-line/step
  caps (hardware-design reasoning). *opus-high*
- **regex-chess** — full legal chess move-gen expressed only as ordered regex subs
  (checked exactly vs python-chess). *opus-high*
- **gpt2-codegolf** — complete GPT-2 forward+argmax parsing a TF checkpoint in <5000B
  of C. *opus-high*
- **path-tracing** — reconstruct a path-traced render to 0.99 similarity in <2KB with
  no filesystem (must re-derive the algorithm, can't embed data). *opus-high*
- **feal-differential / feal-linear-cryptanalysis** — real differential/linear
  key-recovery attacks; grader checks recovered key/plaintexts exactly. *opus-high*
- **model-extraction-relu-logits** — recover a ReLU net's weight matrix up to
  permutation/scale from query outputs (Carlini-style). *opus-high*
- **protein-assembly** — FRET fusion design matching a 505/610nm filter cube via
  fpbase/PDB/antibody lookups (multi-hop specialized bio + web/API). *opus-high*
- **fix-ocaml-gc** — root-cause a GC free-list corruption in the OCaml runtime that
  only surfaces during self-bootstrap (silent, non-local heap bug). *opus-high*
- Plus the whole **perception cluster** (chess-best-move, code-from-image,
  gcode-to-text, extract-moves-from-video, video-processing, financial-document-proc):
  scaffolding is trivial, but the "read the pixels" core exceeds *any* text tier —
  route to programmatic extraction + numeric cross-check, never LLM "look and tell me".

### 6.5 Per-class reusable router rules (all 14 classes)

Decompose / real-verification / localize, condensed from the calibration — the §2
table rows in one place, including the classes without a full §7 playbook:

- **perception-vision** — split "extract pixels→structured data" (hard core; MUST be a
  program: OCR/cv2/frame-diff/format-parse) from "compute over that structure" (trivial);
  verify by a 2nd independent programmatic path + diff, never "I see X"; localize which
  pixels/frames/regions carry signal.
- **numerical-scientific** — set up equations/objective [core] vs run solver+IO
  [scaffold]; verify by plugging back into the defining equation (Ax=λx, |KL−10|,
  baseline diff) — the grader IS the check, iterate against it; localize the hard
  term/window/loop.
- **scientific-modeling** — correct model spec (prior/likelihood/DAG) [core] vs
  sampler/fit/install [scaffold]; verify posterior means / recovered structure vs
  reference bands, distrust a lone stochastic self-test; localize which parameter/edge/
  prior clause encodes the spec.
- **bio-design** — domain-rule design (sites/Tm/identity/spectral) [core, opus+] vs
  FASTA emission [scaffold]; verify by simulating the biology vs primer3/PDB/fpbase;
  localize the fragment/site/sequence.
- **systems-lowlevel** — the format/UB/emulation invariant [core] vs build/parse/run
  [scaffold]; verify by running the real thing (boot/valgrind/replay), behavioral not
  source-inspection; localize the byte/section/instruction/heap-invariant at fault.
- **ml-frameworks** — the correctness-critical algorithm (sharding/collectives/arch
  inference) [core] vs data/train/serve plumbing [scaffold]; verify tensors/grads/
  metrics vs a reference model + hit the floor; localize the layer/shard/collective.
- **text-data-transform** — the transform/query logic [core] vs read/write formats
  [scaffold]; verify byte-exact / exact-row-set diff vs golden; localize the columns/
  records/query-clauses. (sparql-university is the opus outlier — criteria + EU domain
  facts are the core.)
- **security-adversarial** — the attack/analysis idea [core, often opus+] vs
  tooling/harness [scaffold]; verify exact recovered key/secret/flag or a real browser
  firing the alert — never "looks exploitable"; localize the sink/round/code-path.
- **web-research-retrieval** — find+filter the authoritative source [core] vs write the
  answer [trivial]; verify exact string/int, re-fetch and re-derive; localize the
  page/row/split under which filter.
- **build-compile** — the compat patch/config [core] vs fetch/extract/install
  [scaffold]; verify the tool's own sanity command / render / test; localize the legacy
  construct or missing flag/dep.
- **devops-operate** — the service wiring (hook/proto/config) [core] vs install/start
  [scaffold]; verify by exercising the running system end-to-end (curl/ssh/rpc/push);
  localize the config directive/hook/port.
- **esoteric-codegen** — the algorithm-in-the-target is the WHOLE core (almost no
  scaffold) — spend the top tier, **escalate by default**; verify by running the
  artifact through its exotic evaluator + diff vs reference and honoring the size cap;
  localize the rule/state/token in the substrate.
- **abstract-reasoning** — infer the generalizing rule [core, opus] vs git/file scaffold;
  verify by applying the rule to HELD-OUT inputs (note: the ARC grader tests only given
  examples → add your own held-out check to avoid a hardcoded pass); localize the
  transformation invariant across examples.
- **software-general** — the correctness edge (cancellation-safety, proof step) vs
  boilerplate; verify by running the exact failure scenario (cancel mid-run; coqc);
  localize the one construct that makes it correct.

---

## 7. Per-class capability playbooks (explicit, capability-general)

These are the category-specific instructions — the "how a competent engineer
approaches THIS class" guidance, one block per capability class. They are
**capability-level, not task-level**: each would apply to *any* task of that class,
including ones not in this benchmark, and none references a specific task id, a
specific grader, or a specific answer. They encode general engineering discipline for
the category, not a recipe for a puzzle. Delivery is class-conditional (§8): once
SCOPE assigns a class, only the matching playbook(s) are injected — so the prompt
stays light (heavy static protocol prose has regressed us before) while the guidance
is precise.

### perception-vision
- Any answer that depends on the content of an image, video, or frame: the pixels are
  DATA to be processed **programmatically**, never read with your own eyes as the
  answer. Looking may form a hypothesis; it is never the basis of the answer.
- Pick the extraction method by content type: text → OCR (e.g. tesseract);
  synthetically rendered content with known fonts/colors/shapes → deterministic
  template / glyph / color matching; motion or geometry → cv2 / numpy frame analysis.
- **Cross-check:** derive the perceived fact by two independent means (or one method
  plus a structural sanity assertion) and require agreement before using it. A single
  extraction that "looks right" is not trusted.
- Decompose as perceive → cross-check → downstream logic. The downstream logic is
  usually deterministic and cheap; the entire risk is in the perception.
- Verify the final answer by re-deriving the perceived fact independently — never by
  re-reading the value you wrote.
- If there is no programmatic extraction path (open-world scene understanding), say so
  and escalate to a genuinely multimodal model; do not fake it with ad-hoc pixel
  heuristics.

### numerical-scientific
- Parse the input EXACTLY first — delimiter, decimal separator (a comma may be a
  decimal point), header, units. A parse bug silently corrupts every number after it.
- Solve with an established library (scipy / numpy / statsmodels / primer3, …) rather
  than a hand-rolled approximation, unless the task forbids it.
- Know the domain invariants and assert them: expected physical ranges, that a fit did
  NOT hit a parameter bound, that a distribution normalizes, that a rate is positive.
  A result violating a known invariant is wrong regardless of what your check returns.
- Verify by recomputing with an INDEPENDENT method or a stricter unconstrained refit
  and asserting the invariants; re-running the same fit only proves determinism.
- If honest attempts keep missing the invariants, the model or the parse is wrong —
  escalate; do not loosen tolerances to pass.

### systems-lowlevel
- Reason about the actual machine contract (ELF/ABI/syscall/relocation/memory
  semantics), not a surface approximation. When behavior is wrong, fix the DEFINING
  component; never patch input data or a downstream site to paper over a mis-modeled
  component — feeding a broken reader different bytes instead of fixing the reader is
  the classic wrong-layer error.
- Reproduce the exact failure and read it literally: a crash names the component that
  failed — trace it to its cause before hypothesizing.
- Decompose setup/install (cheap) from the emulation/relocation/ABI core (hard); spend
  the reasoning on the core.
- Verify by EXERCISING the artifact against real reference behavior (the actual
  rendered output, the actual memory image, the actual program result), never a
  structural/header check that a content-wrong artifact also passes.
- This class routinely exceeds a mid-tier model; if the core is deep
  emulation/relocation debugging, route it to the strongest tier from the start.

### ml-frameworks
- Use the framework's OFFICIAL documented build / install / run path — graders assume
  the canonical layout; an alternative build that "works" often lands artifacts where
  the checker does not look.
- Separate "it runs" from "it's correct." Compiling, loading a state_dict, or printing
  tokens proves nothing about numerical correctness; for anything generative,
  degenerate / constant / repeating output is a failure signal, not success.
- When inferring an architecture or config from a reference artifact, treat an
  implausible baseline (loss ≈ variance, accuracy ≈ chance) as evidence the
  reconstruction is WRONG — sweep candidates until the baseline collapses; never
  proceed on a bad baseline.
- Know the semantics that make distributed/numeric code correct (e.g. which
  collective's backward sums across ranks) and verify against an INDEPENDENT reference
  implementation, not your own re-implementation's assumptions.
- Route the correctness core to the strongest tier; env/build/training runs are cheap
  scaffolding.

### text-data-transform
- Enumerate EVERY constraint the task states — output path, format, field names, and
  every rule ("only X", "byte-identical", "one event per line") — and make each a
  separate check. The usual miss is a constraint you never checked, not the main goal.
- Read the spec's semantics precisely (one-match vs all-matches; inclusive vs
  exclusive ranges; exact vs normalized strings); small choices flip the answer.
- Verify by recomputing the expected output with logic INDEPENDENT of your transform
  (a different parser, a hand-computed small case, a diff against the original on real
  data). Re-running your own transform as the check is a tautology.
- Preserve verbatim outputs exactly (flags, extracted strings, casing, digits); never
  normalize them to natural language.

### security-adversarial
- First decide the goal's shape: EXISTENCE (craft one input that beats a VISIBLE /
  provided check → a faithful self-test is possible) vs UNIVERSAL robustness
  (block/withstand ALL of a hidden adversarial set → any self-test is only a weak
  proxy).
- For universal robustness, prefer a known-hardened library/tool over a hand-rolled
  matcher, state explicitly that a self-authored check cannot prove universality, and
  test the way the grader will (run it in a real browser, run the actual cracker).
- For "remove/neutralize X everywhere," cover the FULL surface — git history, not just
  the working tree; all encodings/vectors, not a few — and verify with the grader's
  own method.
- Trust literal evidence over a self-written scanner: if a plain grep finds the secret,
  it is there, whatever your custom scan reports.

### web-research-retrieval
- Retrieve through the provided search tools, not raw scraping of a site that will
  anti-bot you; if a source is blocked, switch method rather than proceeding on partial
  data.
- Pin the exact selection criterion (as-of date, "all-reported", the exact metric) and
  apply it to the COMPLETE fetched data.
- Verify by cross-checking that the chosen answer actually satisfies the criterion
  against the fetched data — never by comparing the output file to the answer you
  already picked.
- Write the answer in the exact required format/identifier.

### build-compile
- Onboard the project's own build first (CI workflows, Makefile/CMake, README — the
  exact commands maintainers use) and use that path; it is what the grader assumes.
- Install missing toolchain/deps yourself; never assume the environment is complete.
- Long builds can exceed the outer time budget — parallelize (`-j`); if a full build is
  infeasible, target the specific component the task needs.
- Verify with the project's own test/run and by producing the expected artifact.

### devops-operate
- Probe the current state before changing anything.
- Verify by EXERCISING the running system the way a client will — curl the endpoint,
  ssh in, connect the client, hit the port — never by inspecting config and declaring
  it correct. An open port is not a rendered screen; services-up is not
  function-correct.
- Target the exercised interface; make the change at the layer the grader interacts
  with.

### esoteric-codegen
- Recognize when the task is "implement an algorithm in an exotic substrate or under a
  hard size/step budget" (logic gates, ordered regex substitutions, a polyglot source,
  code-golf byte caps, a self-hosting interpreter, a ≤2KB reconstruction). Here the
  *whole task is the hard core* — there is almost no scaffolding to delegate, and it
  routes to the strongest tier by default.
- Model the target's real semantics before writing: what carries STATE between steps
  (a gate's clocked output, a regex pass's captured groups, the interpreter's
  environment), and how the evaluator will run your artifact. The classic error is
  reasoning combinationally where sequential/clocked state is required.
- Treat the size/step cap as a first-class constraint that forces genuinely dense,
  principled code — you cannot embed data or brute-force; you must express the
  algorithm compactly.
- Verify by running the produced artifact through its own exotic evaluator and diffing
  against a reference (compile+run, apply the full regex list, run the simulator, run
  the battle) AND confirming the cap is honored — never by reading the source and
  judging it plausible.

### scientific-modeling
- The core is the MODEL SPECIFICATION — the prior, the likelihood, the DAG structure,
  the parameterization — not the sampler or the install. Get the math of the model
  right first; the fit is mechanical.
- Reproduce reference settings faithfully when porting (seed, sampler config,
  hyperparameters); small drifts move the posterior.
- Verify against reference posterior means / recovered structure within the stated
  bands, and DISTRUST a lone stochastic self-test — a lenient random check passes a
  wrong model. Assert the model's defining property, not just "it sampled".
- If honest fits keep missing the bands, the specification is wrong — fix the model,
  don't widen tolerances.

### bio-design
- The core is domain-rule design (restriction/cut sites, primer melting-temperature
  windows, sequence identity, spectral/filter-cube matching), which needs real
  molecular-biology knowledge — route it to the strongest tier; the FASTA/file emission
  is scaffolding.
- Use the authoritative domain tools and databases (primer3 for Tm, PDB/fpbase for
  sequences/spectra) rather than approximating the rules by hand.
- Verify by SIMULATING the biology the grader simulates (assembly, site-directed
  mutagenesis) and checking the product against ground truth — not by inspecting that
  the sequence "looks reasonable".

> **The "not a hack" test** each playbook must pass: remove every proper noun and every
> reference to a specific task, grader, or expected answer, and the instruction still
> reads as sound general practice for the category. If a line only makes sense for one
> task, it is a hack — delete it. (Contrast: "for chess, run Stockfish on the FEN" is a
> hack; "never treat your visual read of pixels as the answer — extract and cross-check
> programmatically" is capability-general.)

---

## 8. Delivery — class-conditional injection (not a static mega-prompt)

Playbooks are **selected, not all-present**. This is the answer to "do we need more
nudges": we need *targeted, class-conditional context*, not more generic nudges and
not a bigger static prompt (heavy static protocol prose has regressed us —
[[claude-workprotocol-regression]]).

- SCOPE classifies the task (and each subtask) → the router injects ONLY the matching
  class playbook(s) into working context at plan time, plus the class's row from the
  §2 table (core-tier, verification method, localization meaning).
- A task spanning classes (chess = perception + trivial-logic) gets each subtask's
  playbook attached to that subtask, following the tier-inheritance rule (§3).
- Mechanism: the classification + injection is a cheap pre-pass (deterministic
  class→playbook map); the playbook text lives in **config** (env/secret, tune at
  prepare-time without a rebake — same pattern as the nudge config in plan-doc §8),
  keyed by class. The router picks the key; the text is data, not code.
- The base prompt stays light: SHAPE + ONBOARD + FIX-DISCIPLINE + DELEGATION +
  ESCALATION + FINISH-CONTRACT as today; the class playbook is the only
  task-conditional addition, and it is one block, not nine.

This makes the guidance both **explicit** (a real per-category playbook, not a vague
"be careful") and **light** (only the relevant one is present), and keeps it tunable
without rebaking the image.

> _Research basis:_ selective injection over a static mega-prompt is not just an
> aesthetic — instruction-following **degrades with the COUNT of active constraints**
> independent of prompt length (*SustainScore*, 2026, hits Sonnet-4.5), frontier models
> silently **omit** later instructions and show a **primacy bias** (*IFScale*, 2025),
> and mid-context guidance is used least reliably (*Lost-in-the-Middle*, 2023). Keeping
> only the *matched* class playbook live minimizes that active-constraint load, and it
> corroborates our own prior regression from a heavy static protocol
> ([[claude-workprotocol-regression]]). Two corollaries: put the acceptance criteria
> **first** (primacy), and on long runs **periodically re-inject** the plan + matched
> playbook (plan-doc TP.6) to counter drift.
