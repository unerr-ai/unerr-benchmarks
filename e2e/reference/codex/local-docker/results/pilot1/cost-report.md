# Codex ±unerr Cost/Efficiency Report

## 1. Per-Run Summary

| Label         | Mode | Model         | N | No-Tel | Mean Turns | Total In Tok | Mean In Tok | Total Cached | Mean Cached | Total Out Tok | Mean Out Tok | Total $ | Mean $/inst | Mean Tools | Mean MCP | unerrd_up% | install_ok% | patch>0% |
| ------------- | ---- | ------------- | - | ------ | ---------- | ------------ | ----------- | ------------ | ----------- | ------------- | ------------ | ------- | ----------- | ---------- | -------- | ---------- | ----------- | -------- |
| pilot-codex53 | off  | gpt-5.3-codex | 5 | 0      | 1.00       | 1217396      | 243479      | 1107456      | 221491      | 12944         | 2589         | $0.4053 | $0.0811     | 11.40      | 0.00     | 0.0%       | 0.0%        | 80.0%    |
| pilot-codex53 | on   | gpt-5.3-codex | 5 | 0      | 1.00       | 2453829      | 490766      | 2258816      | 451763      | 15846         | 3169         | $0.6846 | $0.1369     | 19.00      | 16.20    | 100.0%     | 100.0%      | 80.0%    |
| pilot1        | off  | gpt-5.4-mini  | 5 | 0      | 1.00       | 1924169      | 384834      | 1756800      | 351360      | 21062         | 4212         | $0.1279 | $0.0256     | 17.80      | 0.00     | 0.0%       | 0.0%        | 60.0%    |
| pilot1        | on   | gpt-5.4-mini  | 5 | 0      | 1.00       | 3797063      | 759413      | 3484416      | 696883      | 25903         | 5181         | $0.2171 | $0.0434     | 20.60      | 12.40    | 100.0%     | 100.0%      | 60.0%    |

## 2. ±unerr Delta (on vs off)

| Label         | Model         | N(on) | N(off) | Δ Turns | Δ Turns% | Δ Mean In Tok | Δ In Tok% | Δ Mean $/inst | Δ $%   | MCP (on) | unerrd_up% (on) |
| ------------- | ------------- | ----- | ------ | ------- | -------- | ------------- | --------- | ------------- | ------ | -------- | --------------- |
| pilot-codex53 | gpt-5.3-codex | 5     | 5      | +0.00   | +0.0%    | +247287       | +101.6%   | $+0.0559      | +68.9% | 16.20    | 100.0%          |
| pilot1        | gpt-5.4-mini  | 5     | 5      | +0.00   | +0.0%    | +374579       | +97.3%    | $+0.0178      | +69.7% | 12.40    | 100.0%          |

## 3. Cross-Model / Cross-Label Comparison (on-arm)

| Label         | Model         | N | Mean $/inst | Mean Turns | Mean MCP |
| ------------- | ------------- | - | ----------- | ---------- | -------- |
| pilot-codex53 | gpt-5.3-codex | 5 | $0.1369     | 1.00       | 16.20    |
| pilot1        | gpt-5.4-mini  | 5 | $0.0434     | 1.00       | 12.40    |

## 4. unerr Fired? Verdict

- **pilot-codex53 / gpt-5.3-codex**: PASS — mean mcp_tool_calls=16.20, unerrd_up=100%
- **pilot1 / gpt-5.4-mini**: PASS — mean mcp_tool_calls=12.40, unerrd_up=100%
