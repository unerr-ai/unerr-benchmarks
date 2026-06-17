/**
 * Offline token counter for the benchmark harness.
 *
 * Uses `gpt-tokenizer` (already a project dependency) — pure-JS, no WASM, no
 * network. Default encoding is o200k_base (GPT-4o / GPT-4.1 / o-series), the
 * most modern public BPE and the standard cross-model proxy for token counts.
 *
 * TOKENIZER FIDELITY (read before trusting absolute numbers):
 *   - GPT-4o / GPT-4.1 / o-series: EXACT — o200k_base is their real encoding.
 *   - Claude (Opus / Sonnet / Haiku): APPROXIMATE. Anthropic's tokenizer is not
 *     public; o200k_base lands within ~5–15% on code/prose. Treat Claude token
 *     counts here as estimates with that error bar. For an EXACT Claude number,
 *     the end-to-end A/B (Track 3) reads real `usage` off the Anthropic API
 *     response instead of estimating.
 *   - Why the percentage headline survives this: the primary metric is a RATIO
 *     (tokens_saved / baseline_tokens). Both sides are counted with the SAME
 *     encoder, so a constant tokenizer bias cancels in the ratio. The percent
 *     reduction is robust even where the absolute count drifts a few percent.
 *
 * This module is benchmark/research tooling only. It is NOT shipped (the npm
 * `files` array is `dist/**` and excludes everything under benchmarks/).
 */
import { encode } from "gpt-tokenizer";

/** Count tokens for a plain string with o200k_base. */
export function countTokens(text: string): number {
  if (!text) return 0;
  return encode(text).length;
}

/**
 * Count tokens for a tool payload exactly as an agent receives it. Strings are
 * counted directly; structured values are JSON-serialized first (that is the
 * wire form an MCP tool result carries).
 */
export function countPayloadTokens(value: unknown): number {
  if (value === null || value === undefined) return 0;
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return countTokens(text);
}

export const ENCODING = "o200k_base" as const;
