# econ — Verified Mini-10 MERGED FINAL (2026-07-03)

Original 10-run (label `econ`) + targeted re-run of the 4 stalled instances
(label `econ-rerun`, `--ids`), merged: re-run rows supersede the original for the
4 re-run ids; original rows kept for the other 6.

## Merged result: 5/10 resolved

| Instance | Grade | Turns | In | Out | Cost | Patch B | Wall | Source |
|---|---|--:|--:|--:|--:|--:|--:|---|
| 11790 | no ⚠️stall×2 | 1 | 9,518 | 115 | $0.00138 | 0 | 121s | rerun |
| 11815 | **RESOLVED** | 26 | 21,429 | 2,107 | $0.10329 | 4,244 | 113s | orig |
| 11848 | no (failed tests) | 13 | 18,363 | 2,113 | $0.00880 | 615 | 82s | orig |
| 11880 | **RESOLVED** | 10 | 42,986 | 1,294 | $0.01053 | 460 | 53s | orig |
| 11885 | no (failed tests) | 104 | 127,955 | 14,086 | $0.79911 | 8,483 | 741s | orig |
| 11951 | **RESOLVED** | 13 | 15,177 | 1,670 | $0.00745 | 876 | 259s | orig |
| 11964 | no ⚠️stall×2 | 1 | 9,940 | 117 | $0.00149 | 0 | 1811s | rerun |
| 11999 | no ⚠️stall×2 | 1 | 9,543 | 128 | $0.00138 | 0 | 1804s | rerun |
| 12039 | **RESOLVED** (recovered) | 7 | 14,116 | 1,257 | $0.00468 | 1,383 | 106s | rerun |
| 12050 | **RESOLVED** | 5 | 11,227 | 479 | $0.00297 | 500 | 31s | orig |
| **TOTAL** | **5/10** | 181 | 280,254 | 23,366 | **$0.9411** | | 5,123s | |

- Resolved (5): 11815, 11880, 11951, **12039** (recovered on re-run), 12050
- Failed tests, had patch (2): 11848, 11885
- Empty patch, stalled twice (3): 11790, 11964, 11999 — persistent stallers

## Cost
- Total $0.9411 | $/instance $0.0941 | **$/resolved $0.1882**
- Per-tier: reasoner (glm-5.2) $0.9024 (96%) · conductor (deepseek) $0.0387 · explore (deepseek) $0.0329 · executor (gpt-oss) $0.00

## Re-run findings
- The re-run recovered 1 of 4 (12039). The other 3 stalled AGAIN.
- **11790, 11964, 11999 are persistent stallers** — they hang on an OpenRouter call every attempt. econ has no client-side request timeout, so each fell back to the 1800s hard timeout → empty patch. The watchdog only speeds up the failure (fast empty patch); it does not make a hung call succeed, so it cannot raise the resolve rate for these.
- Watchdog bug found + noted: deploying on `dockerd_up` is too early (before `run-benchmark.py` starts), so `while pgrep -f run-benchmark.py` exits its loop immediately. Deploy AFTER run-benchmark is confirmed running, or bake a wait-for-start into the watchdog.

## vs Claude-off (10/10, $3.1181)
| | Claude OFF | econ (merged) |
|---|--:|--:|
| Resolved | 10/10 | 5/10 |
| Total cost | $3.1181 | $0.9411 |
| $/instance | $0.3118 | $0.0941 |
| $/resolved | $0.3118 | $0.1882 |
| Wall-sum | 677s | 5,123s |
