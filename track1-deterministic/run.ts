/**
 * Track 1 — Deterministic token-delta benchmark.
 *
 * Measures, with a real tokenizer and a conservative grep+read baseline, how
 * many tokens unerr's tools save versus the naive path for a FROZEN corpus of
 * real code questions. No LLM, no network, no daemon — reproducible and
 * CI-friendly. The corpus is frozen to a fixture on first run (see corpus.ts),
 * so published numbers are stable across runs.
 *
 * Usage:
 *   tsx benchmarks/track1-deterministic/run.ts [repoPath] [--per N] [--tasks-day N] [--refresh]
 *
 * Output: benchmarks/results/track1-<repo>.{md,json}
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { bootHarness } from "../lib/harness.js";
import {
  aggregate,
  projectMonthly,
  type TaskMeasurement,
} from "../lib/metrics.js";
import { renderMarkdown } from "../lib/report.js";
import { ENCODING, countTokens } from "../lib/tokenizer.js";
import { checkFidelity, loadOrFreezeCorpus } from "./corpus.js";
import { runBaseline } from "./baseline.js";

function arg(flag: string, dflt: number): number {
  const i = process.argv.indexOf(flag);
  if (i >= 0 && process.argv[i + 1]) return Number(process.argv[i + 1]);
  return dflt;
}

function err(msg: string): void {
  process.stderr.write(`${msg}\n`);
}

function gitProvenance(repoRoot: string): string {
  try {
    const sha = execFileSync("git", ["rev-parse", "--short", "HEAD"], {
      cwd: repoRoot,
      encoding: "utf-8",
    }).trim();
    let dirty = "";
    try {
      const status = execFileSync("git", ["status", "--porcelain"], {
        cwd: repoRoot,
        encoding: "utf-8",
      }).trim();
      if (status) dirty = "+dirty";
    } catch {
      /* ignore */
    }
    return `${sha}${dirty}`;
  } catch {
    return "n/a";
  }
}

async function main(): Promise<void> {
  const positional = process.argv.slice(2).find((a) => !a.startsWith("--"));
  const repoRoot = resolve(positional ?? process.cwd());
  const perCategory = arg("--per", 8);
  const tasksPerDay = arg("--tasks-day", 40);
  const refresh = process.argv.includes("--refresh");

  err(`\n▸ Track 1 — deterministic token-delta benchmark`);
  err(`  repo: ${repoRoot}`);

  const h = await bootHarness(repoRoot, (m) => err(`  · ${m}`));

  const { tasks, fromFixture, fixturePath } = await loadOrFreezeCorpus(
    h,
    basename(repoRoot),
    perCategory,
    refresh
  );
  err(
    `\n▸ corpus: ${tasks.length} tasks (${fromFixture ? "loaded frozen fixture" : "derived + froze fixture"})`
  );
  err(`  ${fixturePath}`);

  const measurements: TaskMeasurement[] = [];
  let done = 0;
  for (const t of tasks) {
    let unerrTokens = 0;
    let payload = "";
    try {
      const res = await h.runTool(t.unerr);
      unerrTokens = countTokens(res.payload);
      payload = res.payload;
    } catch (e) {
      err(`  ! ${t.id} unerr error: ${(e as Error).message}`);
    }
    const base = runBaseline(repoRoot, t.baseline);
    let fidelity: boolean | null = null;
    try {
      fidelity = checkFidelity(t, payload);
    } catch {
      fidelity = null;
    }
    measurements.push({
      id: t.id,
      bucket: t.bucket,
      category: t.category,
      repo: basename(repoRoot),
      baselineTokens: base.tokens,
      unerrTokens,
      fidelity,
      note: `${t.question} | baseline: ${base.detail}`,
    });
    done++;
    if (done % 10 === 0) err(`  ... ${done}/${tasks.length}`);
  }

  await h.close();

  const agg = aggregate(measurements);
  const monthly = projectMonthly(agg, {
    tasksPerDay,
    workingDaysPerMonth: 22,
  });

  const md = renderMarkdown(
    "unerr Benchmark — Track 1 (deterministic token deltas)",
    agg,
    measurements,
    monthly,
    {
      repo: repoRoot,
      entities: h.entityCount,
      edges: h.edgeCount,
      encoding: ENCODING,
      commit: gitProvenance(repoRoot),
      date: new Date().toISOString().slice(0, 10),
    }
  );

  const here = dirname(fileURLToPath(import.meta.url));
  const outDir = resolve(here, "../results");
  mkdirSync(outDir, { recursive: true });
  const stem = `track1-${basename(repoRoot)}`;
  writeFileSync(resolve(outDir, `${stem}.md`), md);
  writeFileSync(
    resolve(outDir, `${stem}.json`),
    JSON.stringify({ agg, monthly, measurements }, null, 2)
  );

  err(`\n${"─".repeat(60)}`);
  err(
    `  HEADLINE: unerr cut ${agg.pctReduction.toFixed(1)}% of tokens ` +
      `(${Math.round(agg.baselineTokens / 1000)}K → ${Math.round(agg.unerrTokens / 1000)}K) across ${agg.tasks} tasks`
  );
  const fidChecked = measurements.filter((m) => m.fidelity !== null);
  const fidPass = fidChecked.filter((m) => m.fidelity === true);
  err(`  Fidelity: ${fidPass.length}/${fidChecked.length} checked tasks returned the answer`);
  err(`  Report:   benchmarks/results/${stem}.md`);
  err(`${"─".repeat(60)}\n`);
}

main().catch((e) => {
  err(`\n✗ benchmark failed: ${(e as Error).stack ?? e}`);
  process.exit(1);
});
