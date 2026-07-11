/**
 * Multi-provider pricing matrix — BENCHMARK / MARKETING ONLY.
 *
 * This is intentionally separate from the product's `src/proxy/model-pricing.ts`
 * (which is load-bearing in 7 runtime files). This table exists purely to
 * translate measured token deltas into an illustrative dollar figure for the
 * benchmark report.
 *
 * THE HEADLINE METRIC IS A PERCENTAGE, NOT A DOLLAR.
 *   "unerr cuts N% of the tokens your Claude Code / Cursor session sends" → the
 *   same N% off the usage-based portion of that bill, at ANY model price. The
 *   percentage is rate-agnostic and never goes stale. The $ figures below are a
 *   secondary, illustrative "what that means in money" matrix — update the rates
 *   freely; they do not affect the percentage claim.
 *
 * Rates are public list prices, USD per 1M tokens, INPUT side (unerr saves on
 * the context the agent would otherwise consume — that is input/read tokens).
 * Last reviewed: 2026-05. Verify against provider pricing pages before quoting.
 */

export interface ProviderRate {
  id: string;
  /** Human label for the report. */
  label: string;
  /** The coding-agent product this model commonly powers. */
  agent: string;
  /** USD per 1M input tokens. */
  inputPerMillion: number;
  /** USD per 1M output tokens (recorded for completeness; savings are input-side). */
  outputPerMillion: number;
}

/**
 * Representative current rates. The point of the matrix is to show the dollar
 * impact spans an order of magnitude across providers while the PERCENTAGE
 * reduction is identical — which is exactly why we headline the percentage.
 */
export const PROVIDER_RATES: ProviderRate[] = [
  {
    id: "claude-opus-4",
    label: "Claude Opus 4.x",
    agent: "Claude Code (Opus)",
    inputPerMillion: 15,
    outputPerMillion: 75,
  },
  {
    id: "claude-sonnet-4",
    label: "Claude Sonnet 4.x",
    agent: "Claude Code / Cursor (Sonnet)",
    inputPerMillion: 3,
    outputPerMillion: 15,
  },
  {
    id: "claude-haiku-4",
    label: "Claude Haiku 4.5",
    agent: "Claude Code (Haiku)",
    inputPerMillion: 1,
    outputPerMillion: 5,
  },
  {
    id: "gpt-4o",
    label: "GPT-4o",
    agent: "Cursor / Copilot (GPT-4o)",
    inputPerMillion: 2.5,
    outputPerMillion: 10,
  },
  {
    id: "gpt-4.1",
    label: "GPT-4.1",
    agent: "Cursor / Copilot (GPT-4.1)",
    inputPerMillion: 2,
    outputPerMillion: 8,
  },
  {
    id: "gemini-2.5-pro",
    label: "Gemini 2.5 Pro",
    agent: "Gemini CLI (Pro)",
    inputPerMillion: 1.25,
    outputPerMillion: 10,
  },
  {
    id: "gemini-2.5-flash",
    label: "Gemini 2.5 Flash",
    agent: "Gemini CLI (Flash)",
    inputPerMillion: 0.3,
    outputPerMillion: 2.5,
  },
];

export const RATES_REVIEWED = "2026-05";

/** Dollars saved for a given input-token reduction at one provider rate. */
export function dollarsSaved(
  tokensSaved: number,
  rate: ProviderRate
): number {
  return (tokensSaved * rate.inputPerMillion) / 1_000_000;
}

/** Format a dollar amount with sensible precision for small values. */
export function fmtUsd(amount: number): string {
  if (amount >= 1) return `$${amount.toFixed(2)}`;
  if (amount >= 0.01) return `$${amount.toFixed(2)}`;
  if (amount >= 0.0001) return `$${amount.toFixed(4)}`;
  return `$${amount.toExponential(2)}`;
}
