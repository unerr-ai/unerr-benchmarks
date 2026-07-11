# Internal Benchmarks — Micro-harnesses by Pillar

The `internal/` directory contains vertical micro-benchmarks and shared infrastructure, each mapping to one or more pillars in the [BENCHMARK_FRAMEWORK](../BENCHMARK_FRAMEWORK.md). These harnesses are **independent**, **runnable**, and **testable without Docker** — suitable for rapid iteration, CI/CD, and cross-competitor setup.

## Verticals and Pillar Coverage

| Vertical | Pillar(s) | Scope | Shared Harness |
|---|---|---|---|
| **navigation/** | 1 (Navigation/token savings), 1.5 (head-to-head vs competitors), 2 (localization accuracy) | Token delta on code-graph queries + corpus-based tests. Frozen corpus fidelity-gated. | `lib/tokenizer`, `lib/metrics`, `lib/report`, `lib/pricing` |
| **compression/** | 5 (Context cleaning/rot resistance) | Bulk-fetch A/B with output compression. Needle-survival + fidelity gates on must-survive patterns. | `lib/harness`, `lib/pricing`, `lib/metrics`, `lib/report` |
| **live-ab/** | 2 (Cascade guard), 3 (Reasoning), 4 (Memory), + 1 (SWE-Effi scoring) | Single open-source repo, host-local `claude -p` A/B runner. Three arms: baseline, unerr, unerr-nomemory. Real token/turn/cost JSON. Guardrail + memory isolation. | `lib/harness`, `lib/metrics`, `../../e2e/common/scoring/swe-effi.ts` (SWE-Effi scorer) |
| **lib/** | (infrastructure) | Shared tokenizer, metrics reader, pricing, report builder, test harness. Re-used by all verticals. | n/a |

## Reference: BENCHMARK_FRAMEWORK Pillars

1. **Navigation / token savings** — graph query efficiency vs grep + read
2. **Cascade guard / blast-radius** — preventing caller breakage on signature edits
3. **Reasoning improvement** — upstream outcome of navigation + memory
4. **Context management / memory** — warm facts across dependent tasks
5. **Context cleaning / rot resistance** — compression + needle survival under context growth
6. **Drift / re-anchoring** — rule attachment fidelity across code moves

## Scoring Notes

- **Pillar 1 (SWE-Effi):** All arms scored with token-bounded AUC from [`e2e/common/scoring/swe-effi.ts`](../e2e/common/scoring/swe-effi.ts) (formerly track3).
- **Pillar 2–4 (live-ab specific):** Cascade guard, memory, and reasoning measured within Track 4's three-arm design.
- **Pillar 5 (compression specific):** Fidelity-gated savings on a frozen corpus; see `compression/corpus/RESULTS.md` for reproducible numbers.

## Running Tests

Each vertical is independently runnable; see the README in each subdirectory:
- `navigation/` — token-delta corpus tests
- `compression/` — fixture harness + A/B runner
- `live-ab/` — full-platform A/B (requires target repo + claude CLI)
- `lib/` — shared utilities (tested via live-ab and compression runners)
