# econ-rerun Benchmark — Performance & Cost Report

## 1. Summary

| Label      | N | Resolve% | Resolved/N | Mean Turns | Total In Tok | Mean In Tok | Total Cached | Mean Cached | Total Out Tok | Mean Out Tok | Total $ | Mean $/inst | $/Resolved | Mean Tool Calls | Mean Graph-Tool Calls | Mean Wall(s) | patch>0% |
| ---------- | - | -------- | ---------- | ---------- | ------------ | ----------- | ------------ | ----------- | ------------- | ------------ | ------- | ----------- | ---------- | --------------- | --------------------- | ------------ | -------- |
| econ-rerun | 4 | 25.0%    | 1/4        | 2.50       | 43117        | 10779       | 74954        | 18738       | 1617          | 404          | $0.0089 | $0.0022     | $0.0089    | 3.25            | 0.75                  | 960.58       | 25.0%    |

_Cost basis: econ BYOK $0.0089 (upstream models.dev would be $0.0056, -36.8%)._

## 2. Per-Instance

| Instance             | Resolved | Turns | In Tok | Cached | Out Tok | $       | Tier $       | Tool Calls | Graph Calls | Wall(s) | rc | patch(bytes) |
| -------------------- | -------- | ----- | ------ | ------ | ------- | ------- | ------------ | ---------- | ----------- | ------- | -- | ------------ |
| django__django-11790 | ✗        | 1     | 9518   | 0      | 115     | $0.0014 | cond $0.0014 | 1          | 0           | 121.30  | 0  | 0            |
| django__django-11964 | ✗        | 1     | 9940   | 0      | 117     | $0.0015 | cond $0.0015 | 1          | 0           | 1810.70 | 0  | 0            |
| django__django-11999 | ✗        | 1     | 9543   | 0      | 128     | $0.0014 | cond $0.0014 | 1          | 0           | 1803.90 | 0  | 0            |
| django__django-12039 | ✓        | 7     | 14116  | 74954  | 1257    | $0.0047 | cond $0.0047 | 10         | 3           | 106.40  | 0  | 1383         |

## 4. Per-Tier Cost

| Tier      | Total $ | % of Total $ | Mean $/inst | Total In Tok | Total Cached | Total Out Tok | Total Tokens | Instances Used |
| --------- | ------- | ------------ | ----------- | ------------ | ------------ | ------------- | ------------ | -------------- |
| explore   | $0.0329 | 78.7%        | $0.0165     | 121236       | 404680       | 7414          | 533330       | 2              |
| conductor | $0.0089 | 21.3%        | $0.0022     | 43117        | 74954        | 1617          | 119688       | 4              |

_Source: econ session DB (includes executor volume). conductor = cheap bulk tier · oracle/reasoner = expensive glm tier (may show as one merged row if they share a model) · executor = self-hosted, billed at $0._

## 5. Notes

- 0/4 instance(s) exited non-zero (rc != 0).
- 3/4 instance(s) produced an empty patch (patch_bytes == 0).
- 3/4 instance(s) recorded zero graph_tool_calls — FLAG: econ's embedded graph tools didn't fire (want >0).
- Mean cache-hit ratio (cached_in / in_tokens): 132.7%
