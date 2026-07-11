# unerr benchmark suite

> **Relationship to `unerr-cli`.** This repo holds the benchmark and A/B
> harnesses only. It was split out of the main [`unerr-cli`](https://github.com/unerr-ai/unerr-cli)
> repo so that benchmark output and large cloned fixtures never bloat the
> product tree. Some harnesses (e.g. `internal/lib/harness.ts`, the
> `internal/navigation/` deterministic runs) load unerr's intelligence modules from a built
> `unerr-cli` checkout — clone `unerr-cli` as a sibling directory and
> `pnpm run build` it first. Run everything with [`tsx`](https://github.com/privatenumber/tsx).
> Generated output (`**/out/`, cloned target repos, run logs) is git-ignored.

**The headline benchmark is end-to-end:** `e2e/` runs the same coding agent ± unerr on
standard [SWE-bench](https://www.swebench.com/) instances and measures **resolve rate +
total $ + turns** — the complete cost picture. Per-capability verticals (navigation token
savings, localization accuracy, context compression) live in `internal/` and are
deterministic and CI-friendly; they feed into the headline `e2e/` claim.

**Does unerr actually cut the tokens an AI agent spends reading code — and how does
it compare to the other tools that claim the same?** This suite answers both with
reproducible, ground-truth measurement: real token counts from a real tokenizer, a
conservative grep+read baseline, head-to-head runs against other tools on the *same*
questions, and a fidelity gate that discards any "saving" that lost the answer.

Every number below regenerates from one command — nothing here is hand-entered. The
suite is dev/research tooling and is **not** shipped in the npm package (the `files`
array is `dist/**`, so `benchmarks/` never reaches the registry). Run everything with
[`tsx`](https://github.com/privatenumber/tsx).

---

## Results at a glance

Two external open-source repos — one JavaScript, one Python — each questioned
identically through unerr, [graphify](https://github.com/safishamsi/graphify),
[RTK](https://github.com/rtk-ai/rtk), and a naive grep+read baseline. Same corpus,
same tokenizer, same fidelity gate.

**Expected tokens per navigation operation** — every question category weighted by how
often agents actually use it (from unerr's own telemetry), with any operation a tool
*cannot* answer charged at the naive grep+read cost it forces the agent back to. This is
the honest aggregate: it rewards coverage and terseness together, never one at the
other's expense. **Lower is better.**

| Tool | commander.js · JS/TS | pallets/click · Python |
|---|--:|--:|
| naive grep + read | 129.7K — *(reference)* | 252.7K — *(reference)* |
| **unerr** | **18.2K · −86.0%** | **25.0K · −90.1%** |
| graphify | 74.3K · −42.7% | 49.1K · −80.6% |
| RTK | 90.6K · −30.1% | 129.4K · −48.8% |

Two structural results sit behind that aggregate:

- **unerr answers "who calls this?" — graphify cannot.** graphify's graph has import and
  containment edges but no call edges, so `find-callers` returns nothing (0/8); unerr
  answers it from the graph (8/8). That's the single most common navigation an agent does.
- **unerr's graph costs zero model tokens to build.** graphify's headline `/graphify .`
  flow runs a semantic LLM pass over every file *on your own agent's session* — ≈158K
  (commander.js) / ≈201K (click) input tokens spent before the first question. unerr
  indexes locally with tree-sitter; no model is called.

Where unerr is **not** ahead: listing a file's direct imports is graphify's home turf
(its `imports_from` edges), and unerr's Python extraction is currently thinner than its
JS/TS extraction — see [Limitations](#limitations--what-this-does-not-claim). Both are
stated plainly in the per-repo reports
([`head-to-head-commander.js.md`](results/head-to-head-commander.js.md),
[`head-to-head-click.md`](results/head-to-head-click.md)), because a comparison you can't lose is
a comparison no one believes.

---

## Reproduce it yourself

Every number above comes from one command per repo. Run it on *your* repo — that's the
number that matters to you.

```bash
# head-to-head on any repo: clones it shallowly, builds each tool's graph, runs the corpus
tsx internal/navigation/head-to-head.ts tj/commander.js
tsx internal/navigation/head-to-head.ts pallets/click
# arms whose CLI isn't installed are skipped, never faked:
#   graphify → uv tool install graphifyy      rtk → cargo install --git https://github.com/rtk-ai/rtk
```

Output → `results/head-to-head-<repo>.{md,json}`. The `.md` is the human report
(committed to this repo); the `.json` is the raw rollup (regenerable, git-ignored).

**Versions pinned in the runs above:** unerr (this build) · graphify 0.8.18 · RTK 0.40.0
· commander.js @ `8247364` · click @ `6a141c3`. Pinned versions and commits keep the run
exactly reproducible — [the property that separates a benchmark from a screenshot](https://dl.acm.org/doi/fullHtml/10.1145/3624062.3624133).

---

## How we measure — and why you can trust the number

Any in-product "tokens saved" counter has to *estimate* the counterfactual (what the
agent "would have" done without the tool), which can't be observed at runtime. This suite
removes that guess with four independent, reproducible checks:

| Property | How |
|---|---|
| **Real token counts** | [`gpt-tokenizer`](https://github.com/niieani/gpt-tokenizer) (o200k_base) — pure-JS, offline, deterministic. Both sides counted with the same encoder. |
| **Real baseline** | actual `grep` output + actual file reads for the same question — conservative (top-N files only), so the measured saving is a **lower bound** |
| **Real tool output** | the actual `QueryRouter` result an agent receives, post-enrichment and compression — never a mock |
| **Fidelity-gated** | a saving counts **only** if the output still contains the answer, checked by plain substring match against tool-neutral repository facts (symbol name, caller names, file basename, import stems). Fewer tokens by losing the answer is not a win. |

unerr's tools group into three theses, each tested separately:

| Bucket | Representative tools | Thesis under test |
|---|---|---|
| **Navigation** | `search_code`, `get_entity`, `get_references`, `file_connections` | one graph query replaces many grep + read cycles |
| **Compression** | `file_outline`, `file_read`, `fetch_url` | the same answer in far fewer tokens, fidelity intact |
| **Prevention** | notes, facts, markers, blast-radius signals | a signal that fires prevents a wasted edit/turn (e2e A/B) |

---

## Fairness — how each tool was run

The fastest way to discredit a vendor benchmark is to run the rivals wrong. Each choice
below is the one that gives the *other* tool its best honest shot:

- **Rivals run through their native interface.** graphify is queried with its own
  `explain "<node>"` command (node-and-neighbours lookup) against a graph it built itself
  — not its brittle natural-language `query` router, which mis-routes and would have
  sandbagged it. RTK runs its own filters.
- **Two languages, not one.** JS/TS *and* Python — a single-language corpus invites
  overfitting to a tool's strong language.
  [Diversifying data sources is a core fair-benchmarking principle.](https://arxiv.org/pdf/1709.08242)
- **A category on the rival's home turf.** `imports` is included *specifically* because
  it's graphify's core competency, so the corpus is not stacked toward unerr's strengths.
  graphify wins it.
- **No cherry-picking.** Every task the corpus generates is scored and reported; an arm
  that scores 0 in a category shows 0 — including unerr.

The per-repo reports carry the full fairness notes, provenance, and the per-category
token matrix.

---

## The benchmarks

`internal/navigation/` benchmarks are deterministic — no LLM, no network, no daemon — and
CI-friendly. `e2e/` is the full end-to-end run.

### Token-delta benchmark — deterministic navigation savings (`internal/navigation/token-delta.ts`)

Boots an in-memory graph, indexes the target repo with the production indexer,
auto-derives a task corpus from the repo's own graph, and compares **real baseline
tokens vs. real unerr tokens** per task with fidelity checks.

```bash
tsx internal/navigation/token-delta.ts [repoPath] [--per N] [--tasks-day N]
# defaults: current repo · 8 tasks/category · 40 ops/day for the projection
```

Output → `results/token-delta-<repo>.{md,json}`.

**Baseline model (fairness is the whole point).** The baseline models a *disciplined*
grep+Read agent and under-counts wherever there is doubt, so the measured saving is a
lower bound:

| Task | unerr does | Naive baseline does |
|---|---|---|
| find-symbol | `search_code(name)` | `grep name` + read the **top 1** matched file |
| get-entity | `get_entity(key)` | `grep name` + read the top 1 file |
| find-callers | `get_references(key, callers)` | `grep name` + read the **≤5** real caller files (caller set from the graph's `calls` edges) |
| understand-file | `file_outline(file)` | read the **whole file** |

`grep` runs source-only (excludes `node_modules`, `.git`, `dist`, …) and we count the
grep *output* the agent sees. A real agent typically reads more files, which would only
widen the gap.

### Head-to-head benchmark — unerr vs graphify vs RTK vs naive (`internal/navigation/head-to-head.ts`)

The [Results at a glance](#results-at-a-glance) table. Every arm answers the **same**
frozen corpus, scored with the **same** o200k_base tokenizer and the **same** fidelity
gate. Run command and reproduction are under [Reproduce it yourself](#reproduce-it-yourself).

**Why the weighted aggregate, not a raw "% saved."** A raw percentage favours whichever
tool answers the fewest, easiest questions — each tool's % is over a *different*
denominator (the subset it personally passed). On click, graphify answers more raw tasks
than unerr (26/32 vs 23/32). The aggregate fixes this: it weights every category by its
real share of agent navigation calls (from unerr's shadow ledger — understand-file 51%,
find-symbol 30%, get-entity 11%, find-callers 7%, imports 1%) and charges each operation a
tool can't answer at its real grep+read fallback cost. Coverage counts, not just terseness.

**Build cost — tokens spent before the first query.** A graph has to be built first, and
how differs by tool:

| Tool | Graph build | LLM tokens to build |
|---|---|--:|
| unerr | local AST (tree-sitter) + CozoDB | **0** |
| graphify (`update`, AST-only) | local AST | **0** |
| graphify (`/graphify .`, full) | local AST + semantic LLM pass over every file | **≈158K (commander.js) / ≈201K (click)** |
| RTK | none (stateless command compressor) | **0** |

graphify's headline `/graphify .` runs that semantic pass on your assistant's own model
session — spent from the same budget this benchmark measures, before a single question is
answered. The per-query columns measure graphify in its *cheapest-to-build* AST mode
(`update`); its richest mode adds the cost above.

Full per-repo breakdowns (per-category token matrix, fidelity, provenance):
[`head-to-head-commander.js.md`](results/head-to-head-commander.js.md) ·
[`head-to-head-click.md`](results/head-to-head-click.md).

### Navigation-accuracy probe — localization (`internal/navigation/localization.ts`)

Token savings only matter if the agent still lands on the right code. Given a query whose
gold answer is a known file, this measures whether unerr surfaces it in the top-k and at
what token cost vs. grep.

```bash
tsx internal/navigation/localization.ts [repoPath] [--n N]
```

Reports top-1/3/5 hit rate and mean tokens-to-localize. The runnable version uses the
repo's own graph as gold; swap `localGoldSet()` for SWE-bench Verified gold-patch files
(or RepoBench-R) to source gold externally — scoring is identical.

### End-to-end total-bill benchmark (`e2e/`) — requires Docker + API budget

The strongest claim: the **same agent + model** run twice on
[SWE-bench Verified Mini (50)](https://www.swebench.com/) — once with built-in grep/read
only, once with unerr's MCP tools — holding everything else fixed. Scored on resolve rate,
**turns**, **real tokens** (from the provider's `usage`), patch-apply failures, and
token-bounded effectiveness ([SWE-Effi](https://arxiv.org/abs/2509.09853)) so a win can't
come from "failing faster." The agent runs happen outside this repo; the scoring and
report are ready to consume the resulting trajectories.

```bash
tsx e2e/common/scoring/swe-effi.ts <baseline.jsonl> <treatment.jsonl>
```

See `e2e/codex/local-docker/` for the full protocol and Docker runner, and `e2e/codex/fly-remote/`
for the Fly.io remote variant. The `e2e/econ/` arm covers the team's econ-coding-agent.
Both arms share scoring via `e2e/common/`.

**External anchor.** This benchmark reports a *paired ±unerr delta*, not an absolute
leaderboard rank — compare the Codex arm against the published **Codex-scaffold** score
(GPT-5.3-Codex ≈ 85% on full Verified, June 2026). A dated snapshot of current
SWE-bench Verified / Pro / Mini scores for the frontier models, with the
scaffold-matters caveat, is kept in [`e2e/REFERENCE-SCORES.md`](e2e/REFERENCE-SCORES.md).

---

## Limitations — what this does not claim

A single benchmark is never the whole story —
[ripgrep's author makes exactly this point](https://github.com/BurntSushi/ripgrep/blob/master/README.md).
Where this suite is bounded:

- **Scope is navigation + read, not everything.** It does not measure PR-impact, triage,
  or "the why" (design-rationale) questions that graphify's full semantic graph also
  targets. A tool that scores low on a category here may score well on its own question class.
- **unerr's Python extraction is thinner than its JS/TS extraction today** — 35 graph
  edges built on click vs 1,551 on commander.js, which is why unerr's Python `imports`
  answers are weak and graphify answers more raw click tasks. The weighted aggregate
  already prices this in; it is not hidden.
- **graphify returns node *metadata*; unerr returns the *definition body*.** Both contain
  the symbol name, so both pass the gate — but graphify's lower `get-entity` token count
  reflects returning *less information*, not a tighter answer.
- **The tokenizer is exact for GPT-4o / 4.1 / o-series and a ~±5–10% approximation for
  Claude** (Anthropic's is proprietary). The *percentage* is robust to this because both
  sides use the same encoder, so a constant bias cancels in the ratio. The `e2e/` benchmark
  sidesteps it by reading real `usage` from the provider API.
- **RTK is a different tier** — a command-output compressor, not a code-navigation engine.
  It shows the compression-only ceiling: a useful tier reference, not a like-for-like rival.

Found a way it's unfair? That's a bug — open an issue or PR. The methodology is meant to
be argued with.

---

## Benchmarking principles we follow

This suite is built around the published guidance for fair, reproducible benchmarking:
real tooling over heuristics, conservative baselines, diversified data sources to avoid
overfitting, running rivals through their correct native interface, pinned versions, and
stated limitations. Primary references:
[Best practices for comparing methods](https://arxiv.org/pdf/1709.08242) ·
[Principles for Automated and Reproducible Benchmarking](https://dl.acm.org/doi/fullHtml/10.1145/3624062.3624133) ·
[ClickBench](https://github.com/ClickHouse/ClickBench) (the reproduce-in-minutes model).

## Open-source benchmarks referenced

[SWE-bench Verified Mini](https://www.swebench.com/) (cheap end-to-end),
[SWE-Effi](https://arxiv.org/abs/2509.09853) (token-bounded effectiveness),
[ContextBench](https://arxiv.org/abs/2602.05892) (context precision/recall/F1),
Agentless/Moatless (localization accuracy),
[RepoBench](https://arxiv.org/abs/2306.03091) /
[CrossCodeEval](https://arxiv.org/abs/2310.11248) (retrieval quality, no LLM).

Current published scores for the frontier coding models (SWE-bench Verified / Pro /
Mini), kept as a dated cross-reference for the `e2e/` runs, live in
[`e2e/REFERENCE-SCORES.md`](e2e/REFERENCE-SCORES.md).

## Layout

```
internal/
  lib/                   tokenizer · pricing (appendix only) · metrics · report · in-process harness
  navigation/
    token-delta.ts       token-delta benchmark + baseline model + corpus
    head-to-head.ts      same-corpus comparison vs graphify · RTK · naive
    localization.ts      navigation-accuracy probe
  compression/           fetch-bulk A/B + corpus (corpus/ nested inside)
  live-ab/               single-repo live A/B (claude-driver, metrics-reader)
e2e/                     end-to-end total-bill benchmark (unerr + coding agent on SWE-bench)
  common/
    scoring/             SWE-Effi scorer + A/B protocol
  codex/
    local-docker/        Codex + local Docker runner
    fly-remote/          Codex + Fly.io remote runner
  econ/                  team's econ-coding-agent arm
results/                 generated reports (.md committed · .json git-ignored)
```

## Appendix: illustrative cost translation

The product itself speaks only in **tokens and turns**. For communication, the benchmark
can translate a measured token reduction into an illustrative dollar figure across
provider rates (`lib/pricing.ts`) and a per-developer/month projection. These figures are
illustrative only — list prices change, and the percentage above is the durable claim.
They live exclusively in the benchmark, never in the product.
