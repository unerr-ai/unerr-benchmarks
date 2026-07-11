/**
 * Report renderer — turns measurements into a markdown report whose HEADLINE is
 * the percentage token reduction, with the dollar matrix and monthly projection
 * as illustrative secondary detail.
 */
import {
  type Aggregate,
  type MonthlyProjection,
  type TaskMeasurement,
} from "./metrics.js";
import {
  PROVIDER_RATES,
  RATES_REVIEWED,
  dollarsSaved,
  fmtUsd,
} from "./pricing.js";

function pct(n: number): string {
  return `${n.toFixed(1)}%`;
}

function k(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(Math.round(n));
}

/** Context windows the saving is measured against. May exceed 100% (won't fit). */
const CONTEXT_WINDOWS: Array<{ label: string; size: number }> = [
  { label: "200K — standard Claude / GPT-4o context", size: 200_000 },
  { label: "1M — Claude Code auto-compaction window", size: 1_000_000 },
];

function winPct(n: number, w: number): string {
  return `${((n / w) * 100).toFixed(1)}%`;
}

export function renderMarkdown(
  title: string,
  agg: Aggregate,
  measurements: TaskMeasurement[],
  monthly: MonthlyProjection,
  meta: {
    repo: string;
    entities: number;
    edges: number;
    encoding: string;
    commit?: string;
    date?: string;
  }
): string {
  const L: string[] = [];
  L.push(`# ${title}`);
  L.push("");
  L.push(
    `Repo \`${meta.repo}\`${meta.commit ? ` @ \`${meta.commit}\`` : ""}${meta.date ? ` · ${meta.date}` : ""}`
  );
  L.push(
    `${meta.entities} entities, ${meta.edges} edges · tokenizer \`${meta.encoding}\` · ${agg.tasks} tasks`
  );
  L.push("");

  // ── Headline ─────────────────────────────────────────────────────────────
  L.push(`## Headline`);
  L.push("");
  L.push(
    `**On the code-navigation and file-reading work unerr handles, it removed ${pct(agg.pctReduction)} of the tokens** the naive grep+read path would have put into the agent's context to answer the same ${agg.tasks} questions.`
  );
  L.push("");
  L.push(
    `- Baseline tokens (without unerr): **${k(agg.baselineTokens)}**`
  );
  L.push(`- unerr tokens: **${k(agg.unerrTokens)}**`);
  L.push(`- Saved: **${k(agg.savedTokens)}** (${pct(agg.pctReduction)})`);
  L.push("");

  // ── Scope (what the % is and is NOT) ───────────────────────────────────────
  L.push(`### What this percentage covers`);
  L.push("");
  L.push(
    `This is ${pct(agg.pctReduction)} of the tokens spent on **code navigation, file reading, and outlining** — the slice of an agent's work unerr intercepts and answers from its graph. It is **not** ${pct(agg.pctReduction)} off a coding agent's *total* bill.`
  );
  L.push("");
  L.push(
    `A real session also spends tokens on code generation and file edits, model reasoning, sub-agent context, system prompts, and conversation history — none of which this track touches. So treat this as the per-operation gain on the retrieval slice, not a whole-session discount. Track 3 (end-to-end A/B) measures the whole-session effect; this track isolates the retrieval slice so the per-operation reduction is measured cleanly. Because it is a ratio of tokens, it holds at any model price.`
  );
  L.push("");

  // ── Context-window impact (cumulative, not per-turn) ───────────────────────
  L.push(`### Context-window impact`);
  L.push("");
  L.push(
    `Tokens in an agent are cumulative. Anything pulled into context stays there, is re-sent on every following turn, and counts against the window until the agent compacts — Claude Code auto-compacts at its 1M-token window. So the reduction above is not a one-time, per-turn saving: it is context the agent never has to carry for the rest of the session.`
  );
  L.push("");
  L.push(
    `A session that ran all ${agg.tasks} of these operations would accumulate **${k(agg.baselineTokens)}** tokens of navigation/read context the naive way, versus **${k(agg.unerrTokens)}** via unerr. Measured against the context window itself:`
  );
  L.push("");
  L.push(`| Context window | Naive footprint | unerr footprint | Window reclaimed |`);
  L.push(`|---|--:|--:|--:|`);
  for (const w of CONTEXT_WINDOWS) {
    L.push(
      `| ${w.label} | ${k(agg.baselineTokens)} (${winPct(agg.baselineTokens, w.size)}) | ${k(agg.unerrTokens)} (${winPct(agg.unerrTokens, w.size)}) | ${winPct(agg.savedTokens, w.size)} |`
    );
  }
  L.push("");
  L.push(
    `A footprint over 100% does not fit — the agent must compact or drop context mid-task, losing earlier reasoning. Prompt caching discounts the *dollar* cost of re-sent context but does **not** return window space, so reclaimed headroom is the durable, model-agnostic benefit: more of the window stays free for the actual code and reasoning, and auto-compaction triggers far later.`
  );
  L.push("");

  // ── By bucket ──────────────────────────────────────────────────────────────
  L.push(`## By capability bucket`);
  L.push("");
  L.push(`| Bucket | Tasks | Baseline | unerr | Saved | Reduction | Fidelity |`);
  L.push(`|---|--:|--:|--:|--:|--:|--:|`);
  for (const b of agg.byBucket) {
    const fid =
      b.fidelityChecked > 0
        ? `${b.fidelityPassed}/${b.fidelityChecked}`
        : "—";
    L.push(
      `| ${b.bucket} | ${b.validTasks} | ${k(b.baselineTokens)} | ${k(b.unerrTokens)} | ${k(b.savedTokens)} | ${pct(b.pctReduction)} | ${fid} |`
    );
  }
  L.push("");

  // ── By category ────────────────────────────────────────────────────────────
  L.push(`## By task category`);
  L.push("");
  L.push(`| Category | Tasks | Baseline | unerr | Reduction | Fidelity |`);
  L.push(`|---|--:|--:|--:|--:|--:|`);
  for (const c of agg.byCategory) {
    const fid =
      c.fidelityChecked > 0
        ? `${c.fidelityPassed}/${c.fidelityChecked}`
        : "—";
    L.push(
      `| ${c.bucket} | ${c.validTasks} | ${k(c.baselineTokens)} | ${k(c.unerrTokens)} | ${pct(c.pctReduction)} | ${fid} |`
    );
  }
  L.push("");

  // ── Money matrix (illustrative appendix) ───────────────────────────────────
  L.push(`## Appendix — illustrative cost translation`);
  L.push("");
  L.push(
    `The product reports only tokens and turns. The figures below exist solely for communication: an illustrative scaling of the measured token reduction by current list rates (per 1M input tokens, reviewed ${RATES_REVIEWED}). The percentage above is the durable claim; list prices change.`
  );
  L.push("");
  L.push(
    `Per-month projection assumes **${monthly.assumptions.tasksPerDay} nav/read operations/day × ${monthly.assumptions.workingDaysPerMonth} days**, avg **${k(monthly.assumptions.avgBaselineTokensPerTask)}** baseline tokens/op → **${k(monthly.savedTokensPerMonth)}** input tokens saved/developer/month.`
  );
  L.push("");
  L.push(`| Provider (agent) | $/1M in | Saved $/dev/month |`);
  L.push(`|---|--:|--:|`);
  for (const r of PROVIDER_RATES) {
    L.push(
      `| ${r.label} — ${r.agent} | $${r.inputPerMillion} | ${fmtUsd(dollarsSaved(monthly.savedTokensPerMonth, r))} |`
    );
  }
  L.push("");

  // ── Fidelity caveat ────────────────────────────────────────────────────────
  const checked = measurements.filter((m) => m.fidelity !== null);
  const failed = checked.filter((m) => m.fidelity === false);
  L.push(`## Fidelity`);
  L.push("");
  L.push(
    `A token saving only counts if unerr actually returned the answer. Of ${agg.totalTasks} tasks, ${checked.length} were fidelity-checked against the graph's ground truth and **${checked.length - failed.length} passed**.`
  );
  if (agg.failedTasks > 0) {
    L.push("");
    L.push(
      `${agg.failedTasks} task(s) where unerr did **not** return the answer are EXCLUDED from the headline percentage — an empty or wrong response never counts as a saving:`
    );
    for (const f of failed.slice(0, 20)) {
      L.push(`- \`${f.id}\` — baseline ${k(f.baselineTokens)} → unerr ${k(f.unerrTokens)}`);
    }
  } else {
    L.push("");
    L.push(`No fidelity failures — every measured saving returned the answer.`);
  }
  L.push("");

  L.push(`## Method`);
  L.push("");
  L.push(
    `Baseline = real \`grep\` output + real file reads for the same question (conservative: top-N files only). unerr = the real QueryRouter tool result (post enrichment + compression). Both counted with the same \`${meta.encoding}\` tokenizer, so the percentage is robust to tokenizer bias. See \`benchmarks/README.md\` for the full baseline model and its assumptions.`
  );
  L.push("");
  return L.join("\n");
}
