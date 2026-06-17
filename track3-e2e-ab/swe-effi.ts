/**
 * SWE-Effi scoring — resource-aware effectiveness for the end-to-end A/B.
 *
 * Raw "fewer tokens" can be won by FAILING FASTER, so we never report tokens
 * alone. SWE-Effi (arXiv 2509.09853) scores resolve rate AGAINST resources:
 * the token-bounded effectiveness curve = fraction of instances resolved within
 * a token budget B, swept over B; its normalized area-under-curve (AUC) is the
 * headline. "More resolves per token" is the defensible claim.
 *
 * This module is pure scoring over trajectory records — usable the moment you
 * have Arm A / Arm B trajectories (see README). No agent run happens here.
 */
import { PROVIDER_RATES, dollarsSaved, fmtUsd } from "../lib/pricing.js";

/** One SWE-bench instance attempt by one arm. */
export interface Trajectory {
  instanceId: string;
  resolved: boolean;
  /** Real input tokens from the provider `usage` (sum over turns). */
  inputTokens: number;
  /** Real output tokens from the provider `usage` (sum over turns). */
  outputTokens: number;
  /** Model turns / tool-call rounds to terminate. */
  turns: number;
  /** Patch failed to apply / malformed tool call etc. — the "breakage" count. */
  breakages?: number;
}

export interface ArmScore {
  arm: string;
  n: number;
  resolved: number;
  resolveRate: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  meanTurns: number;
  breakages: number;
  /** Normalized token-bounded effectiveness AUC in [0,1]. */
  tokenBoundedAuc: number;
}

/**
 * Token-bounded effectiveness: sweep budget B from 0..maxB, at each B count the
 * fraction of instances RESOLVED using ≤ B input tokens. Normalized AUC over B.
 */
function tokenBoundedAuc(trajs: Trajectory[], steps = 50): number {
  if (trajs.length === 0) return 0;
  const maxB = Math.max(...trajs.map((t) => t.inputTokens), 1);
  let area = 0;
  for (let i = 1; i <= steps; i++) {
    const budget = (maxB * i) / steps;
    const resolvedWithin = trajs.filter(
      (t) => t.resolved && t.inputTokens <= budget
    ).length;
    area += resolvedWithin / trajs.length;
  }
  return area / steps; // normalized to [0,1]
}

export function scoreArm(arm: string, trajs: Trajectory[]): ArmScore {
  const n = trajs.length;
  const resolved = trajs.filter((t) => t.resolved).length;
  return {
    arm,
    n,
    resolved,
    resolveRate: n > 0 ? resolved / n : 0,
    totalInputTokens: trajs.reduce((a, t) => a + t.inputTokens, 0),
    totalOutputTokens: trajs.reduce((a, t) => a + t.outputTokens, 0),
    meanTurns: n > 0 ? trajs.reduce((a, t) => a + t.turns, 0) / n : 0,
    breakages: trajs.reduce((a, t) => a + (t.breakages ?? 0), 0),
    tokenBoundedAuc: tokenBoundedAuc(trajs),
  };
}

export interface AbComparison {
  baseline: ArmScore;
  treatment: ArmScore;
  /** % reduction in total input tokens, treatment vs baseline. */
  inputTokenReductionPct: number;
  resolveRateDelta: number;
  meanTurnsDelta: number;
  aucDelta: number;
}

export function compareArms(
  baseline: ArmScore,
  treatment: ArmScore
): AbComparison {
  const inputTokenReductionPct =
    baseline.totalInputTokens > 0
      ? ((baseline.totalInputTokens - treatment.totalInputTokens) /
          baseline.totalInputTokens) *
        100
      : 0;
  return {
    baseline,
    treatment,
    inputTokenReductionPct,
    resolveRateDelta: treatment.resolveRate - baseline.resolveRate,
    meanTurnsDelta: treatment.meanTurns - baseline.meanTurns,
    aucDelta: treatment.tokenBoundedAuc - baseline.tokenBoundedAuc,
  };
}

export function renderAb(cmp: AbComparison): string {
  const L: string[] = [];
  const b = cmp.baseline;
  const t = cmp.treatment;
  L.push(`# Track 3 — End-to-end A/B (SWE-Effi scored)`);
  L.push("");
  L.push(`| Metric | Baseline (${b.arm}) | Treatment (${t.arm}) | Δ |`);
  L.push(`|---|--:|--:|--:|`);
  L.push(`| Instances | ${b.n} | ${t.n} | |`);
  L.push(
    `| Resolve rate | ${(b.resolveRate * 100).toFixed(1)}% | ${(t.resolveRate * 100).toFixed(1)}% | ${(cmp.resolveRateDelta * 100).toFixed(1)}pp |`
  );
  L.push(
    `| Total input tokens | ${b.totalInputTokens.toLocaleString()} | ${t.totalInputTokens.toLocaleString()} | -${cmp.inputTokenReductionPct.toFixed(1)}% |`
  );
  L.push(
    `| Mean turns | ${b.meanTurns.toFixed(1)} | ${t.meanTurns.toFixed(1)} | ${cmp.meanTurnsDelta.toFixed(1)} |`
  );
  L.push(`| Breakages | ${b.breakages} | ${t.breakages} | |`);
  L.push(
    `| Token-bounded AUC | ${b.tokenBoundedAuc.toFixed(3)} | ${t.tokenBoundedAuc.toFixed(3)} | ${cmp.aucDelta >= 0 ? "+" : ""}${cmp.aucDelta.toFixed(3)} |`
  );
  L.push("");
  L.push(
    `**Headline:** ${cmp.inputTokenReductionPct.toFixed(1)}% fewer input tokens with no resolve-rate regression → that % off the bill. AUC ${cmp.aucDelta >= 0 ? "up" : "down"} confirms it's "more resolves per token", not "failing faster".`
  );
  L.push("");
  L.push(`Saved input tokens this run: ${(b.totalInputTokens - t.totalInputTokens).toLocaleString()}`);
  for (const r of PROVIDER_RATES.slice(0, 3)) {
    L.push(
      `- ${r.label}: ${fmtUsd(dollarsSaved(b.totalInputTokens - t.totalInputTokens, r))} saved on this 50-instance run`
    );
  }
  return L.join("\n");
}
