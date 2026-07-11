# unerr Benchmark — Track 1.5 (head-to-head)

Repo `commander.js (tj/commander.js)` @ `8247364` · 2026-05-26
2029 entities, 1551 edges · tokenizer `o200k_base`

Every arm answered the **same** corpus of code questions; outputs were scored with the **same** `o200k_base` tokenizer and the **same** fidelity gate (did the output still contain the answer?). Tokens are compared only over tasks the arm answered correctly — dropping tokens by losing the answer is not a win.

## Arms compared

| Arm | Tasks answered | Fidelity | Tokens (valid) | vs naive baseline |
|---|--:|--:|--:|--:|
| naive (grep+read) | 27/34 | 26/33 | 4.4M | — (reference) |
| graphify | 21/34 | 20/33 | 2.9K | −99.9% |
| unerr | 31/34 | 30/33 | 24.0K | −99.6% |
| RTK | 20/34 | 19/33 | 98.6K | −96.6% |

> "vs naive baseline" = token reduction versus raw grep+read **on the same tasks the arm answered correctly**. Fidelity `p/c` = passed/checked against the repo's actual symbol & caller facts. An arm that passed 0 checks shows **no** reduction — returning the wrong answer in few tokens is not a saving.

> ⚠️ **This table is not the bottom line.** Each arm's % is computed over the *different subset of tasks it personally answered*, so a tool can post a higher % by answering fewer, easier questions. For the apples-to-apples number — every category weighted by how often agents actually use it, with misses charged at their real fallback cost — see **[Expected cost per operation](#expected-cost-per-operation-weighted-by-real-usage)** below.

## Tokens by task category

| Category | naive (grep+read) | graphify | unerr | RTK |
|---|--:|--:|--:|--:|
| find-symbol | 1.9M (8/8) | 204 (3/8) | 2.4K (6/8) | 18.2K (1/8) |
| get-entity | 2.1M (8/8) | 629 (8/8) | 7.5K (8/8) | 31.2K (8/8) |
| find-callers | 426.6K (1/8) | 0 (0/8) | 5.8K (8/8) | 2.0K (1/8) |
| understand-file | 26.3K (7/8) | 1.4K (7/8) | 8.0K (7/8) | 26.3K (7/8) |
| imports | 21.0K (2/2) | 650 (2/2) | 258 (1/2) | 21.0K (2/2) |

> Cells show `tokens (fidelity pass/total)` for that category.

## Expected cost per operation (weighted by real usage)

Per-category tokens above treat every category equally — but agents do not use them equally. Weighting each category by its real share of navigation tool-calls (mined from unerr's shadow ledger: understand-file 51%, find-symbol 30%, get-entity 11%, find-callers 7%, imports 1%) gives the **expected token cost of one navigation operation in a realistic session**. Operations a tool *cannot* answer fall back to naive grep+read — the cost the agent actually pays when the tool misses — so this rewards coverage as well as terseness.

| Arm | Expected tokens / op | vs naive baseline |
|---|--:|--:|
| naive (grep+read) | 129.7K | — (reference) |
| graphify | 74.3K | −42.7% |
| unerr | 18.2K | −86.0% |
| RTK | 90.6K | −30.1% |

> Lower is better. This single number is the honest aggregate: it folds in how often each operation is used AND penalises tools for the operations they cannot answer (which revert to the naive cost).

## Build cost — the tokens spent before any query

Query tokens are not the whole bill. A graph has to be built first, and how it is built differs by tool:

| Tool | Graph build | LLM tokens to build |
|---|---|--:|
| unerr | local AST (tree-sitter) + CozoDB, no model calls | **0** |
| graphify (`update`, AST-only) | local AST (tree-sitter), no model calls | **0** |
| graphify (`/graphify .`, full) | local AST **+ semantic LLM pass over every file** | **≈ 157.9K** input |
| RTK | none (stateless command compressor) | **0** |

graphify's headline experience is `/graphify .` invoked **inside your AI assistant**, which runs the semantic extraction **on your assistant's own model session** (per graphify's README: "the model API is provided by your IDE session"). For this repo that semantic pass sends ≈ **157.9K tokens** of source through the model — spent from the very budget this benchmark measures, *before the first question is answered*. unerr's graph is built with **zero** model tokens. (The AST-only `graphify update` mode used for the per-query numbers above skips this pass — so graphify's query columns are measured in its *cheapest-to-build* mode; its richest mode costs the ≈157.9K above.)

## How to read this

- **Same tier (unerr vs graphify):** both are graph-backed code engines. graphify is driven through its own `explain "<node>"` command (node-and-neighbours lookup) — its native interface, not the brittle NL `query` router — so it gets its honest best shot per task.
- **graphify returns node *metadata*, unerr returns the *definition*.** On `get-entity`, graphify's `explain` reports where a symbol's node lives plus its neighbours; unerr (and the baseline) return the actual definition body. Both contain the symbol name, so both pass the gate — but graphify's lower token count there reflects returning *less information*, not a tighter answer. Read its `get-entity` tokens as "located it", not "delivered the code".
- **graphify has no call edges.** Its model is `imports_from` / `contains`, so "who calls X" (`find-callers`) has no answer in its graph — hence its 0 there. The mirror image: `imports` is its home turf, where it matches or beats unerr.
- **Different tier (RTK):** RTK is a command-output compressor, not a code-navigation engine. On this corpus it runs the naive commands compressed, so it shows the *compression-only* ceiling — useful as a tier reference, not a like-for-like rival.
- **Fidelity is the gate.** A low token count with low fidelity means the tool answered a different (smaller) question. Read every token number next to its `pass/total`. An arm that passes **0** checks in a category did not "win on tokens" — it returned an answer this corpus could not confirm.
- **Each arm's headline % is over the tasks THAT arm passed** — different arms pass different subsets, so the % column is not a common-denominator race. The fair cross-arm view is the per-category `tokens (pass/total)` matrix above.

## What this corpus measures — and what it doesn't

This corpus probes the navigation work a coding agent does most: locating a symbol's definition (`find-symbol`), reading its body (`get-entity`), finding its callers (`find-callers`), outlining a file (`understand-file`), and listing a file's direct dependencies (`imports`). The last is deliberately included as a **graph engine's home turf** — graphify's `imports_from` edges are its core competency — so the corpus is not stacked toward unerr's strengths.
It does **not** probe PR-impact, triage, or "the why" (design-rationale / docstring) questions that graphify's full semantic graph also targets. An arm that scores low on a category here may score well on its own question class; this benchmark does not claim otherwise.
Needles are tool-neutral repository facts — exact symbol name, file basename, caller function names, and import-target stems parsed from the file's **own source** (not from any tool's graph) — checked by plain substring containment. Any tool that surfaces the fact in any format passes; an arm scores 0 in a category only when its output never contains the fact, not because of formatting.

## Provenance

- unerr: `in-process (this build)`
- graphify: `graphify 0.8.18`
- rtk: `rtk 0.40.0`

## Fairness notes

- The task corpus (which symbols, which caller sets) is derived from the repo's own AST via unerr's indexer, but the ground-truth needles (symbol names, caller function names, file basenames) are **tool-neutral facts about the repository** — any tool either surfaces them or does not.
- The baseline is deliberately conservative (grep with source-only excludes, read only top-N files), which UNDER-states the naive cost and therefore under-states every arm's reduction.
- unerr is measured at the QueryRouter output, before the stdio wire-cap that only shrinks payloads further — a conservative measurement for unerr.
- graphify is queried against a graph it built itself (`graphify update`, its key-free AST mode) via its own `explain "<node>"` lookup — its native happy path for node-anchored questions. Its full `/graphify .` mode adds a semantic LLM pass (see *Build cost*); that pass runs on the agent's own model session and was not reproducible headlessly here, so the per-query columns reflect graphify's AST-mode output. RTK runs its own filters.
