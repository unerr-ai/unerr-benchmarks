/**
 * Benchmark metrics — the percentage-first scoring model.
 *
 * Every track produces `TaskMeasurement`s. The aggregation answers the only
 * question that matters for the value claim:
 *
 *   "Of the tokens an agent would have spent WITHOUT unerr to get the same
 *    answer, what fraction did unerr remove?"  →  pct_reduction
 *
 * That fraction is, to first order, the percentage off the usage-based bill for
 * the affected work (Claude Code, Cursor, etc.). It is rate-agnostic.
 */

/** One measured comparison: the same task done the naive way vs. via unerr. */
export interface TaskMeasurement {
  id: string;
  /** Capability bucket: navigation | compression | prevention. */
  bucket: "navigation" | "compression" | "prevention";
  /** Finer category, e.g. "find-symbol", "understand-file". */
  category: string;
  repo: string;
  /** Tokens the naive built-in path (grep + full reads) would put in context. */
  baselineTokens: number;
  /** Tokens the unerr tool result puts in context for the same question. */
  unerrTokens: number;
  /**
   * Did the unerr result actually contain the answer the baseline contained?
   * A compression win is only real if fidelity holds. `null` = not checked.
   */
  fidelity: boolean | null;
  /** Optional human note (what the task asked, which baseline model applied). */
  note?: string;
}

export interface BucketRollup {
  bucket: string;
  /** Total tasks in this group (including fidelity failures). */
  tasks: number;
  /** Tasks counted toward savings (fidelity !== false). */
  validTasks: number;
  baselineTokens: number;
  unerrTokens: number;
  savedTokens: number;
  pctReduction: number;
  fidelityChecked: number;
  fidelityPassed: number;
}

export interface Aggregate {
  /** Tasks counted toward the headline (fidelity-valid). */
  tasks: number;
  /** All tasks run, including fidelity failures. */
  totalTasks: number;
  /** Tasks excluded from savings because unerr did NOT return the answer. */
  failedTasks: number;
  baselineTokens: number;
  unerrTokens: number;
  savedTokens: number;
  /**
   * PRIMARY HEADLINE: savedTokens / baselineTokens over fidelity-VALID tasks
   * only. Fidelity failures (empty/wrong tool output) never count as savings.
   */
  pctReduction: number;
  byBucket: BucketRollup[];
  byCategory: BucketRollup[];
}

/** Savings are summed over `valid`; fidelity stats are reported over `all`. */
function rollup(
  name: string,
  valid: TaskMeasurement[],
  all: TaskMeasurement[]
): BucketRollup {
  const baselineTokens = valid.reduce((a, m) => a + m.baselineTokens, 0);
  const unerrTokens = valid.reduce((a, m) => a + m.unerrTokens, 0);
  const savedTokens = baselineTokens - unerrTokens;
  return {
    bucket: name,
    tasks: all.length,
    validTasks: valid.length,
    baselineTokens,
    unerrTokens,
    savedTokens,
    pctReduction: baselineTokens > 0 ? (savedTokens / baselineTokens) * 100 : 0,
    fidelityChecked: all.filter((m) => m.fidelity !== null).length,
    fidelityPassed: all.filter((m) => m.fidelity === true).length,
  };
}

export function aggregate(all: TaskMeasurement[]): Aggregate {
  // A fidelity failure means unerr did NOT return the answer — that is not a
  // saving, it's a miss. Exclude it from the headline so an empty/error
  // response can never inflate the percentage.
  const valid = all.filter((m) => m.fidelity !== false);
  const baselineTokens = valid.reduce((a, m) => a + m.baselineTokens, 0);
  const unerrTokens = valid.reduce((a, m) => a + m.unerrTokens, 0);
  const savedTokens = baselineTokens - unerrTokens;

  const buckets = [...new Set(all.map((m) => m.bucket))];
  const categories = [...new Set(all.map((m) => m.category))];

  return {
    tasks: valid.length,
    totalTasks: all.length,
    failedTasks: all.filter((m) => m.fidelity === false).length,
    baselineTokens,
    unerrTokens,
    savedTokens,
    pctReduction: baselineTokens > 0 ? (savedTokens / baselineTokens) * 100 : 0,
    byBucket: buckets.map((b) =>
      rollup(
        b,
        valid.filter((m) => m.bucket === b),
        all.filter((m) => m.bucket === b)
      )
    ),
    byCategory: categories.map((c) =>
      rollup(
        c,
        valid.filter((m) => m.category === c),
        all.filter((m) => m.category === c)
      )
    ),
  };
}

/**
 * Extrapolate a per-task token saving to a developer's monthly spend.
 *
 * This is deliberately simple and the assumptions are surfaced in the report so
 * the reader can re-run with their own numbers. We do NOT claim these are
 * universal — they are a worked example of "what the percentage means per seat".
 */
export interface MonthlyAssumptions {
  /** Navigation/read/compression-bearing operations per working day. */
  tasksPerDay: number;
  workingDaysPerMonth: number;
  /** Average baseline tokens per such operation (defaults to measured mean). */
  avgBaselineTokensPerTask?: number;
}

export interface MonthlyProjection {
  assumptions: MonthlyAssumptions & { avgBaselineTokensPerTask: number };
  pctReduction: number;
  baselineTokensPerMonth: number;
  savedTokensPerMonth: number;
}

export function projectMonthly(
  agg: Aggregate,
  assumptions: MonthlyAssumptions
): MonthlyProjection {
  const avgBaseline =
    assumptions.avgBaselineTokensPerTask ??
    (agg.tasks > 0 ? agg.baselineTokens / agg.tasks : 0);
  const tasksPerMonth = assumptions.tasksPerDay * assumptions.workingDaysPerMonth;
  const baselineTokensPerMonth = avgBaseline * tasksPerMonth;
  const savedTokensPerMonth = baselineTokensPerMonth * (agg.pctReduction / 100);
  return {
    assumptions: { ...assumptions, avgBaselineTokensPerTask: avgBaseline },
    pctReduction: agg.pctReduction,
    baselineTokensPerMonth,
    savedTokensPerMonth,
  };
}
