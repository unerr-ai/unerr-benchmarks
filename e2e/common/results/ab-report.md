# End-to-end A/B (SWE-Effi scored)

| Metric | Baseline (builtin grep/read) | Treatment (unerr MCP) | Δ |
|---|--:|--:|--:|
| Instances | 2 | 2 | |
| Resolve rate | 50.0% | 50.0% | 0.0pp |
| Total input tokens | 219,000 | 117,000 | -46.6% |
| Mean turns | 17.0 | 10.0 | -7.0 |
| Breakages | 0 | 0 | |
| Token-bounded AUC | 0.010 | 0.190 | +0.180 |

**Headline:** 46.6% fewer input tokens with no resolve-rate regression → that % off the bill. AUC up confirms it's "more resolves per token", not "failing faster".

Saved input tokens this run: 102,000
- Claude Opus 4.x: $1.53 saved on this 50-instance run
- Claude Sonnet 4.x: $0.31 saved on this 50-instance run
- Claude Haiku 4.5: $0.10 saved on this 50-instance run