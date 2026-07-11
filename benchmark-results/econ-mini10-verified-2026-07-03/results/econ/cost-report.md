# econ Benchmark — Performance & Cost Report

## 1. Summary

| Label | N  | Resolve% | Resolved/N | Mean Turns | Total In Tok | Mean In Tok | Total Cached | Mean Cached | Total Out Tok | Mean Out Tok | Total $ | Mean $/inst | $/Resolved | Mean Tool Calls | Mean Graph-Tool Calls | Mean Wall(s) | patch>0% |
| ----- | -- | -------- | ---------- | ---------- | ------------ | ----------- | ------------ | ----------- | ------------- | ------------ | ------- | ----------- | ---------- | --------------- | --------------------- | ------------ | -------- |
| econ  | 10 | 40.0%    | 4/10       | 17.60      | 271959       | 27196       | 4898430      | 489843      | 22168         | 2217         | $0.9380 | $0.0938     | $0.2345    | 17.90           | 7.00                  | 454.01       | 60.0%    |

_Cost basis: econ BYOK $0.9380 (upstream models.dev would be $1.0033, +7.0%)._

## 2. Per-Instance

| Instance             | Resolved | Turns | In Tok | Cached  | Out Tok | $       | Tier $         | Tool Calls | Graph Calls | Wall(s) | rc | patch(bytes) |
| -------------------- | -------- | ----- | ------ | ------- | ------- | ------- | -------------- | ---------- | ----------- | ------- | -- | ------------ |
| django__django-11790 | ✗        | 0     | 0      | 0       | 0       | $0.0000 | n/a            | 0          | 0           | 1811.30 | 0  | 0            |
| django__django-11815 | ✓        | 26    | 21429  | 428736  | 2107    | $0.1033 | oracle $0.1033 | 25         | 9           | 112.80  | 0  | 4244         |
| django__django-11848 | ✗        | 13    | 18363  | 174942  | 2113    | $0.0088 | cond $0.0088   | 12         | 7           | 82.50   | 0  | 615          |
| django__django-11880 | ✓        | 10    | 42986  | 135175  | 1294    | $0.0105 | cond $0.0105   | 12         | 7           | 53.40   | 0  | 460          |
| django__django-11885 | ✗        | 104   | 127955 | 3941688 | 14086   | $0.7991 | oracle $0.7991 | 104        | 31          | 741.30  | 0  | 8483         |
| django__django-11951 | ✓        | 13    | 15177  | 155731  | 1670    | $0.0075 | cond $0.0075   | 16         | 9           | 259.10  | 0  | 876          |
| django__django-11964 | ✗        | 1     | 9940   | 0       | 145     | $0.0015 | cond $0.0015   | 1          | 0           | 639.40  | 0  | 0            |
| django__django-11999 | ✗        | 1     | 9543   | 0       | 34      | $0.0014 | cond $0.0014   | 1          | 0           | 128.20  | 0  | 0            |
| django__django-12039 | ✗        | 3     | 15339  | 21459   | 240     | $0.0030 | cond $0.0030   | 3          | 3           | 680.90  | 0  | 0            |
| django__django-12050 | ✓        | 5     | 11227  | 40699   | 479     | $0.0030 | cond $0.0030   | 5          | 4           | 31.20   | 0  | 500          |

## 4. Per-Tier Cost

| Tier      | Total $ | % of Total $ | Mean $/inst | Total In Tok | Total Cached | Total Out Tok | Total Tokens | Instances Used |
| --------- | ------- | ------------ | ----------- | ------------ | ------------ | ------------- | ------------ | -------------- |
| reasoner  | $0.9024 | 88.9%        | $0.4512     | 149384       | 4370424      | 16193         | 4536001      | 2              |
| explore   | $0.0773 | 7.6%         | $0.0258     | 376860       | 545593       | 18049         | 940502       | 3              |
| conductor | $0.0356 | 3.5%         | $0.0044     | 122575       | 528006       | 5975          | 656556       | 8              |

_Source: econ session DB (includes executor volume). conductor = cheap bulk tier · oracle/reasoner = expensive glm tier (may show as one merged row if they share a model) · executor = self-hosted, billed at $0._

## 5. Notes

- 0/10 instance(s) exited non-zero (rc != 0).
- 4/10 instance(s) produced an empty patch (patch_bytes == 0).
- 3/10 instance(s) recorded zero graph_tool_calls — FLAG: econ's embedded graph tools didn't fire (want >0).
- Mean cache-hit ratio (cached_in / in_tokens): 875.2%
- 1/10 instance(s) have usd_source == "upstream_fallback" — FLAG: that instance's cost is the models.dev catalog price, not econ's BYOK matrix (the econ binary didn't emit a cost_breakdown / is out of date): django__django-11790
