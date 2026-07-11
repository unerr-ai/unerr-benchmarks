# Codex ±unerr Cost/Efficiency Report

## 1. Per-Run Summary

| Label  | Mode | Model        | N | No-Tel | Mean Turns | Total In Tok | Mean In Tok | Total Cached | Mean Cached | Total Out Tok | Mean Out Tok | Total $ | Mean $/inst | Mean Tools | Mean MCP | unerrd_up% | install_ok% | patch>0% |
| ------ | ---- | ------------ | - | ------ | ---------- | ------------ | ----------- | ------------ | ----------- | ------------- | ------------ | ------- | ----------- | ---------- | -------- | ---------- | ----------- | -------- |
| smoke4 | off  | gpt-5.4-mini | 1 | 0      | 1.00       | 558735       | 558735      | 526720       | 526720      | 6258          | 6258         | $0.0337 | $0.0337     | 22.00      | 0.00     | 0.0%       | 0.0%        | 100.0%   |
| smoke4 | on   | gpt-5.4-mini | 1 | 0      | 1.00       | 35933        | 35933       | 0            | 0           | 2505          | 2505         | $0.0140 | $0.0140     | 0.00       | 0.00     | 100.0%     | 100.0%      | 0.0%     |

## 2. ±unerr Delta (on vs off)

| Label  | Model        | N(on) | N(off) | Δ Turns | Δ Turns% | Δ Mean In Tok | Δ In Tok% | Δ Mean $/inst | Δ $%   | MCP (on) | unerrd_up% (on) |
| ------ | ------------ | ----- | ------ | ------- | -------- | ------------- | --------- | ------------- | ------ | -------- | --------------- |
| smoke4 | gpt-5.4-mini | 1     | 1      | +0.00   | +0.0%    | -522802       | -93.6%    | $-0.0197      | -58.5% | 0.00     | 100.0%          |

## 3. Cross-Model / Cross-Label Comparison (on-arm)

_Only one label/model present; cross-comparison not applicable._

## 4. unerr Fired? Verdict

- **smoke4 / gpt-5.4-mini**: FLAG — mean mcp_tool_calls=0.00 (want >0)
