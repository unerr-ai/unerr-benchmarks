/**
 * Drives one agent run via `claude -p --output-format json` and parses the real
 * token/turn/cost numbers out of the CLI result. No API key — it uses the host's
 * Claude Code login. The baseline arm gets `--strict-mcp-config` + an empty MCP
 * config so the repo's `.mcp.json` (unerr) is ignored; the unerr arms run native
 * so the project's MCP servers and hooks apply.
 *
 * @sem domain=benchmark role=driver
 */
import { execFileSync } from "node:child_process";
import { type ArmId, armUsesUnerr } from "./types.js";

/**
 * Forces the headless agent to run the turn autonomously. A `claude -p` run has
 * no human to answer, so a task that's even slightly under-specified would make
 * the agent STOP and ask — which hangs an unattended benchmark forever. This is
 * the same guard the in-agent demo tape uses (demo/unerr-cascade-demo.tape):
 * no clarifying questions, no option menus, no plan-mode pause, no confirm step.
 * Applied to BOTH arms so it never becomes a hidden variable between them.
 */
const AUTONOMOUS_SYSTEM_PROMPT =
  "You are running in an unattended benchmark with no human available to " +
  "answer. Execute the task end to end autonomously. Never ask clarifying " +
  "questions. Never present options for the user to choose. Never call " +
  "AskUserQuestion. Never stop to confirm or seek approval. Never enter plan " +
  "mode. Pick the most reasonable interpretation and implement it directly, " +
  "including updating every caller.";

/** Knobs for the headless agent invocation. */
export interface DriverOptions {
  /** Permission posture for `claude -p`. Unattended runs must never prompt.
   * `bypassPermissions` maps to `--dangerously-skip-permissions` (clears EVERY
   * prompt, including MCP tool calls); any other value is passed through as
   * `--permission-mode <value>`. `acceptEdits` is NOT enough for the unerr arm:
   * it auto-approves file edits but still blocks `mcp__unerr__*` tool calls. */
  permissionMode: string;
  /** Optional model snapshot to pin (freezes one variable across arms). */
  model?: string;
  /** Path to an empty MCP-config JSON used to neutralize the baseline arm. */
  emptyMcpConfigPath: string;
  /** Hard turn cap so a runaway agent can't burn the whole budget. */
  maxTurns?: number;
}

/** Parsed result of one headless agent run. */
export interface DriverResult {
  /** Total input tokens billed = fresh + cache-create + cache-read. */
  inputTokens: number;
  /** Fresh (uncached) prompt input tokens — billed at the 1× input rate. */
  freshInputTokens: number;
  /** Cache-write tokens — the prefix written into the cache (1.25× input rate). */
  cacheCreateTokens: number;
  /** Cache-read tokens — the prefix served from cache (0.1× input rate). */
  cacheReadTokens: number;
  outputTokens: number;
  turns: number;
  costUsd: number;
  /** True when the CLI reported `is_error` (the run itself failed, not the task). */
  isError: boolean;
  /** The agent's final text (`result` field), for debugging a trajectory. */
  resultText: string;
}

/** The `usage` block Claude Code's JSON output carries. */
interface ClaudeUsage {
  input_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  output_tokens?: number;
}

interface ClaudeJsonResult {
  is_error?: boolean;
  result?: string;
  total_cost_usd?: number;
  num_turns?: number;
  usage?: ClaudeUsage;
}

/**
 * Build the `claude` argv for one arm. Pure (no process spawn) so the arm-isolation
 * flags are unit-testable. The baseline arm is neutralized with
 * `--strict-mcp-config --mcp-config <empty>`; the unerr arms inherit the worktree's
 * native `.mcp.json` + hooks.
 */
export function buildClaudeArgs(
  prompt: string,
  arm: ArmId,
  opts: DriverOptions
): string[] {
  const args = ["-p", prompt, "--output-format", "json"];
  // Keep the unattended run from stalling on a clarifying question / plan-mode
  // pause. Same autonomy guard the demo tape uses; applied to both arms.
  args.push("--append-system-prompt", AUTONOMOUS_SYSTEM_PROMPT);
  if (opts.permissionMode === "bypassPermissions") {
    // MCP tool calls (mcp__unerr__*) are NOT auto-approved by acceptEdits, so
    // the unerr arm could not call a single unerr tool under it. This flag
    // clears every prompt for the unattended run; both arms use it so unerr's
    // presence stays the only variable.
    args.push("--dangerously-skip-permissions");
  } else {
    args.push("--permission-mode", opts.permissionMode);
  }
  if (opts.model) {
    args.push("--model", opts.model);
  }
  if (typeof opts.maxTurns === "number") {
    args.push("--max-turns", String(opts.maxTurns));
  }
  if (!armUsesUnerr(arm)) {
    // Baseline: ignore the project's .mcp.json entirely. The worktree also has
    // no unerr install, so hooks/CLAUDE.md guidance are absent — this is the
    // belt-and-suspenders that guarantees no unerr leaks into the control arm.
    args.push("--strict-mcp-config", "--mcp-config", opts.emptyMcpConfigPath);
  }
  return args;
}

/** Sum every input-token kind the model was billed for. */
export function totalInputTokens(usage: ClaudeUsage | undefined): number {
  if (!usage) return 0;
  return (
    (usage.input_tokens ?? 0) +
    (usage.cache_creation_input_tokens ?? 0) +
    (usage.cache_read_input_tokens ?? 0)
  );
}

/** Parse the JSON Claude Code prints in `--output-format json` mode. */
export function parseClaudeJson(stdout: string): DriverResult {
  const parsed = JSON.parse(stdout) as ClaudeJsonResult;
  const usage = parsed.usage;
  return {
    inputTokens: totalInputTokens(usage),
    freshInputTokens: usage?.input_tokens ?? 0,
    cacheCreateTokens: usage?.cache_creation_input_tokens ?? 0,
    cacheReadTokens: usage?.cache_read_input_tokens ?? 0,
    outputTokens: usage?.output_tokens ?? 0,
    turns: parsed.num_turns ?? 0,
    costUsd: parsed.total_cost_usd ?? 0,
    isError: parsed.is_error === true,
    resultText: parsed.result ?? "",
  };
}

/**
 * Run `claude -p` for one (task, arm) in `cwd` and return the parsed result.
 * Throws if the CLI is missing or emits unparseable output — the caller records
 * that as a breakage rather than a silent zero.
 */
export function runClaude(
  prompt: string,
  arm: ArmId,
  cwd: string,
  opts: DriverOptions
): DriverResult {
  const args = buildClaudeArgs(prompt, arm, opts);
  const stdout = execFileSync("claude", args, {
    cwd,
    encoding: "utf-8",
    // A large agent transcript can exceed the default 1 MB stdio buffer.
    maxBuffer: 64 * 1024 * 1024,
  });
  return parseClaudeJson(stdout);
}
