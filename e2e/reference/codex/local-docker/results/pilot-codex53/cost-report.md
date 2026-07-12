# Codex ±unerr Cost/Efficiency Report

## 1. Per-Run Summary

| Label         | Mode | Model         | N | No-Tel | Mean Turns | Total In Tok | Mean In Tok | Total Cached | Mean Cached | Total Out Tok | Mean Out Tok | Total $ | Mean $/inst | Mean Tools | Mean MCP | unerrd_up% | install_ok% | patch>0% |
| ------------- | ---- | ------------- | - | ------ | ---------- | ------------ | ----------- | ------------ | ----------- | ------------- | ------------ | ------- | ----------- | ---------- | -------- | ---------- | ----------- | -------- |
| pilot-codex53 | off  | gpt-5.3-codex | 5 | 0      | 1.00       | 1217396      | 243479      | 1107456      | 221491      | 12944         | 2589         | $0.5674 | $0.1135     | 11.40      | 0.00     | 0.0%       | 0.0%        | 80.0%    |
| pilot-codex53 | on   | gpt-5.3-codex | 5 | 0      | 1.00       | 2453829      | 490766      | 2258816      | 451763      | 15846         | 3169         | $0.9584 | $0.1917     | 19.00      | 16.20    | 100.0%     | 100.0%      | 80.0%    |

## 2. ±unerr Delta (on vs off)

| Label         | Model         | N(on) | N(off) | Δ Turns | Δ Turns% | Δ Mean In Tok | Δ In Tok% | Δ Mean $/inst | Δ $%   | MCP (on) | unerrd_up% (on) |
| ------------- | ------------- | ----- | ------ | ------- | -------- | ------------- | --------- | ------------- | ------ | -------- | --------------- |
| pilot-codex53 | gpt-5.3-codex | 5     | 5      | +0.00   | +0.0%    | +247287       | +101.6%   | $+0.0782      | +68.9% | 16.20    | 100.0%          |

## 3. Cross-Model / Cross-Label Comparison (on-arm)

_Only one label/model present; cross-comparison not applicable._

## 4. unerr Fired? Verdict

- **pilot-codex53 / gpt-5.3-codex**: PASS — mean mcp_tool_calls=16.20, unerrd_up=100%
