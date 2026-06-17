# Compression benchmark — fidelity-gated results

Frozen corpus, n=5 per category. The **headline is the fidelity-gated**
savings: a fixture's byte win counts only when every must-survive pattern
survived compression. A compression that drops the answer earns **zero**
savings, not its bytes — so the gated number never hides a regression.

Reproduce: `node benchmarks/compression-corpus/generate-fixtures.mjs` then
`pnpm run test:run src/__tests__/compression-harness.test.ts`. Same
fixtures in → same numbers out (no clock, no randomness).

## Per-category

| category | n | gross saved % | gated saved % | fidelity pass |
| --- | --- | --- | --- | --- |
| file-read | 5 | 78.1% | 78.1% | 5/5 |
| json | 5 | 94.2% | 94.2% | 5/5 |
| shell | 5 | 90.2% | 76.6% | 4/5 |
| **all** | 15 | 89.8% | 83.2% | 14/15 |

## Per-fixture

| fixture | category | mechanism | ranking | orig→deliv tok | saved % | fidelity |
| --- | --- | --- | --- | --- | --- | --- |
| source-file-0 | file-read | smart_truncate | query | 4365→958 | 78.1% | PASS |
| source-file-1 | file-read | smart_truncate | query | 4365→958 | 78.1% | PASS |
| source-file-2 | file-read | smart_truncate | query | 4365→958 | 78.1% | PASS |
| source-file-3 | file-read | smart_truncate | query | 4365→958 | 78.1% | PASS |
| source-file-4 | file-read | smart_truncate | query | 4365→958 | 78.1% | PASS |
| search-response-0 | json | wire_cap | query | 10394→598 | 94.2% | PASS |
| search-response-1 | json | wire_cap | query | 10386→599 | 94.2% | PASS |
| search-response-2 | json | wire_cap | query | 10380→596 | 94.3% | PASS |
| search-response-3 | json | wire_cap | query | 10396→603 | 94.2% | PASS |
| search-response-4 | json | wire_cap | query | 10377→598 | 94.2% | PASS |
| build-log-eslint | shell | shell_log_text | query | 15203→1349 | 91.1% | PASS |
| build-log-fatal | shell | shell_log_text | query | 20322→1352 | 93.3% | PASS |
| git-diff-large | shell | shell_compressor | positional | 11512→2147 | 81.3% | FAIL |
| kubectl-yaml | shell | shell_compressor | positional | 3306→505 | 84.7% | PASS |
| test-results-fail | shell | shell_log_text | query | 18264→1346 | 92.6% | PASS |

Savings framing: retrieval-slice (what one tool response delivers), not a
whole-session bill. `ranking=query` marks a survivor ordered by the S7
task-conditioned path; `importance` by S3 graph centrality; `positional` by
structure. Understanding-code savings (graph/query ordering) and
compressing-output savings (shell/JSON truncation) both land in this table.
