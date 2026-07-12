# Results

Headline numbers, each sourced and caveated. See [`METHODOLOGY.md`](METHODOLOGY.md)
for how these are produced.

## econ vs Claude Code — SWE-bench Verified Mini-10 (2026-07-03)

| Arm | Resolved | Cost / instance | Total cost |
|---|--:|--:|--:|
| econ (unerr embedded, OSS-model routing) | 5/10 | $0.094 | $0.941 |
| Claude Code (unerr off) | 10/10 | $0.312 | $3.118 |

**Caveat:** n=10 is a small, noisy snapshot — directional, not a resolve-rate
estimate. econ is ~70% cheaper per instance on this snapshot but resolves fewer;
3 of its 5 misses were transient OSS-gateway stalls (empty patch on a hung
upstream call, no client-side request timeout), not modeling failures. Full
per-instance breakdown lives in `benchmark-results/` (gitignored, regenerated per
run — not committed to this repo).

## Navigation token savings

unerr's code-navigation tools (`search_code`, `get_references`, `file_outline`)
cut the tokens spent reading/searching code by **86–92.6%** vs a disciplined
grep+read baseline, fidelity-gated — a saving only counts if the answer still
survived.

| Source | Reduction | Corpus |
|---|--:|---|
| [`../results/token-delta-unerr-cli.md`](../results/token-delta-unerr-cli.md) | 92.6% | unerr-cli, 32 tasks |
| [`../results/head-to-head-commander.js.md`](../results/head-to-head-commander.js.md) | 86.0% | commander.js, weighted aggregate vs naive grep+read |
| [`../results/head-to-head-click.md`](../results/head-to-head-click.md) | 90.1% | pallets/click, weighted aggregate vs naive grep+read |
| [`results/compression-RESULTS.md`](results/compression-RESULTS.md) | 83.2% gated (89.8% gross) | fidelity-gated compression corpus, n=15 |

**Caveat:** this is the retrieval-slice reduction (code navigation + read/outline),
not a whole-session token discount — a real agent also spends tokens on
generation, reasoning, and conversation history that this number doesn't touch.

## Regenerating these numbers

- Mini-10 and future campaigns regenerate via `e2e/distributed/run-distributed.sh`
  — see the root [`README.md`](../README.md) quickstart.
- The navigation/compression tables above were produced by tooling that lived
  under the now-removed `internal/` directory. The committed `.md` reports linked
  above are the frozen results of that last run, kept as a historical/salvaged
  reference — they are not regenerable from this repo as it stands today.

## Not a leaderboard submission

These numbers back a **reproducible, self-hosted** run, not a SWE-bench Verified
leaderboard entry — the leaderboard requires an academic-affiliated author and a
published technical report. Full gap analysis: [`SUBMISSION.md`](SUBMISSION.md).
