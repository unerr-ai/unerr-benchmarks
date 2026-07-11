# econ — SWE-bench Verified Mini-10 (2026-07-03)

Single-arm (unerr compiled into econ). Same 10 django instances as the Claude-off run
(byte-identical MINI_50_IDS → first 10). Ran on fly app `unerr-bench-econ-fullresolve`
(machine 2879091fd49008, org vamsee-k-933), DinD + swebench grading in-VM.

## Result: 4/10 resolved (raw)

| Instance | Grade | Turns | In | Out | Cost | Patch B | Wall |
|---|---|--:|--:|--:|--:|--:|--:|
| 11790 | empty ⚠️stall | 0 | 0 | 0 | $0.00000 | 0 | 1811s |
| 11815 | **RESOLVED** | 26 | 21,429 | 2,107 | $0.10329 | 4,244 | 113s |
| 11848 | fail | 13 | 18,363 | 2,113 | $0.00880 | 615 | 82s |
| 11880 | **RESOLVED** | 10 | 42,986 | 1,294 | $0.01053 | 460 | 53s |
| 11885 | fail | 104 | 127,955 | 14,086 | $0.79911 | 8,483 | 741s |
| 11951 | **RESOLVED** | 13 | 15,177 | 1,670 | $0.00745 | 876 | 259s |
| 11964 | empty ⚠️stall | 1 | 9,940 | 145 | $0.00146 | 0 | 639s |
| 11999 | empty ⚠️stall | 1 | 9,543 | 34 | $0.00137 | 0 | 128s |
| 12039 | empty ⚠️stall | 3 | 15,339 | 240 | $0.00298 | 0 | 681s |
| 12050 | **RESOLVED** | 5 | 11,227 | 479 | $0.00297 | 500 | 31s |
| **TOTAL** | **4/10** | 176 | 271,959 | 22,168 | **$0.9380** | | 4,540s |

- Resolved: 11815, 11880, 11951, 12050
- Empty patch (all 4 = transient OpenRouter stalls, watchdog-killed or near-instant exit): 11790, 11964, 11999, 12039
- Had patch, failed tests: 11848, 11885

## Cost
- Total $0.9380 | $/instance $0.0938 | $/resolved $0.2345
- Per-tier: reasoner (z-ai/glm-5.2) $0.9024 (96%) · explore (deepseek-v4-flash) $0.0773 · conductor (deepseek-v4-flash) $0.0356 · executor (gpt-oss-20b, self-hosted) $0.00
- 86% of total cost is the single hard instance 11885 ($0.80) — same instance that cost Claude $1.80.

## Caveat — stalls
4 of 10 instances hit transient OpenRouter hangs. econ has NO client-side request timeout,
so each needed the external watchdog to recover (kill opencode → run-instance times out → advance).
These 4 owe a fair single-instance re-run before a clean resolve-rate comparison. Of the 6
instances that ran to completion, 4 resolved (67%).

## vs Claude-off (10/10, $3.1181)
| | Claude OFF | econ (raw) |
|---|--:|--:|
| Resolved | 10/10 | 4/10 (6 ran clean → 4) |
| Total cost | $3.1181 | $0.9380 |
| $/instance | $0.3118 | $0.0938 |
| $/resolved | $0.3118 | $0.2345 |
| Wall-sum | 677s | 4,540s |
| Model(s) | claude-opus-4-8 | glm-5.2 + deepseek-v4-flash + gpt-oss-20b |
