/**
 * Track 4 — single-repo host-local A/B types.
 *
 * One open-source repo, set up once on the host (no Docker), a handful of seeded
 * tasks each with a failing-test oracle, run twice or thrice (baseline vs unerr,
 * plus an optional memory-wiped unerr arm). The agent is driven by `claude -p`
 * so no API key is needed and real token/turn counts come from the CLI `usage`.
 *
 * @sem domain=benchmark role=schema
 */
import type { Trajectory } from "../../e2e/common/scoring/swe-effi.js";

/** The three arms. `unerr-nomemory` isolates the memory pillar by wiping notes
 * between tasks while keeping every other unerr surface active. */
export type ArmId = "baseline" | "unerr" | "unerr-nomemory";

export const ALL_ARMS: ArmId[] = ["baseline", "unerr", "unerr-nomemory"];

/** Whether an arm has unerr installed at all (baseline does not). */
export function armUsesUnerr(arm: ArmId): boolean {
  return arm !== "baseline";
}

/** Whether an arm keeps memory warm across the task chain. */
export function armKeepsMemory(arm: ArmId): boolean {
  return arm === "unerr";
}

/** One seeded task: a break to apply, an instruction, and a test that gates it. */
export interface TaskSpec {
  /** Stable id; also used as the SWE-Effi `instanceId`. */
  id: string;
  /** Human title for the report. */
  title: string;
  /** The instruction handed to the agent verbatim (becomes the `claude -p` prompt). */
  prompt: string;
  /** Shell command run in the repo root that introduces the bug (e.g. reverts a
   * real fix so `testCommand` goes red). Omit if the repo already ships red. */
  breakCommand?: string;
  /** Shell command run in the repo root that runs the gate test(s). Exit 0 = the
   * task is resolved; any non-zero exit = unresolved. */
  testCommand: string;
  /** Ids of earlier tasks this one builds on. Non-empty => the task exercises
   * memory carry-over (a fact learned earlier should help here). */
  dependsOn?: string[];
}

/** The whole benchmark definition for one repo. */
export interface TaskManifest {
  /** Local path or git URL of the target repo. */
  repo: string;
  /** Commit to reset to before each task (keeps the oracle deterministic). */
  baseCommit?: string;
  /** One-time per-arm environment setup, run after checkout (e.g. `pip install -e .`). */
  setupCommand?: string;
  /** Which arms to run. Defaults to all three. */
  arms?: ArmId[];
  /** The seeded tasks, run in array order (so a `dependsOn` target precedes it). */
  tasks: TaskSpec[];
}

/** unerr-side telemetry pulled from one arm's `.unerr/metrics.db` for a run window. */
export interface PlatformEvents {
  /** Count of `behavior_events` rows classified as a guardrail firing. */
  guardrailFires: number;
  /** Every `behavior_events.type` seen in the window → count (nothing hidden). */
  eventsByType: Record<string, number>;
  /** Sum of `token_flow_events.tokens_saved` in the window (navigation/compression). */
  navTokensSaved: number;
  /** Mean `compression_events.saved_pct` in the window, or null if no rows. */
  compressSavedPct: number | null;
  /** Total tool calls inferred from `behavior_events` rows in the window. */
  behaviorRows: number;
}

/** One arm's attempt at one task — a SWE-Effi `Trajectory` plus platform pillars. */
export interface RunRecord extends Trajectory {
  arm: ArmId;
  taskId: string;
  dependsOn: string[];
  /** Fresh (uncached) input tokens — 1× input rate. Sub-component of `inputTokens`. */
  freshInputTokens: number;
  /** Cache-write tokens — 1.25× input rate. Sub-component of `inputTokens`. */
  cacheCreateTokens: number;
  /** Cache-read tokens — 0.1× input rate. Sub-component of `inputTokens`. */
  cacheReadTokens: number;
  /** Wall-clock of the agent run in ms. */
  wallMs: number;
  /** Real cost from the `claude -p` JSON `total_cost_usd` (0 on a subscription). */
  costUsd: number;
  /** unerr-side telemetry for this run (all-zero for the baseline arm). */
  platform: PlatformEvents;
}

/** An empty platform record — the baseline arm and dry runs use this. */
export function emptyPlatform(): PlatformEvents {
  return {
    guardrailFires: 0,
    eventsByType: {},
    navTokensSaved: 0,
    compressSavedPct: null,
    behaviorRows: 0,
  };
}
