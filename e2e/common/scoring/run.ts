/**
 * e2e/common/scoring runner — scores two arms' trajectories. The agent runs happen OUTSIDE
 * this script (Docker + SWE-bench + API budget — see README.md). This consumes
 * the resulting trajectory JSONL and produces the SWE-Effi A/B report.
 *
 * Each line of a trajectory file is one `Trajectory` JSON object (see
 * swe-effi.ts). Produce these from your agent harness (mini-SWE-agent emits
 * per-instance token/turn/resolved data; map it to the Trajectory shape).
 *
 * Usage:
 *   tsx e2e/common/scoring/run.ts <baseline.jsonl> <treatment.jsonl>
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  type Trajectory,
  compareArms,
  renderAb,
  scoreArm,
} from "./swe-effi.js";

function err(m: string): void {
  process.stderr.write(`${m}\n`);
}

function loadTrajectories(path: string): Trajectory[] {
  if (!existsSync(path)) {
    err(`✗ trajectory file not found: ${path}`);
    err(`  Run the agent arms first — see e2e/common/scoring/README.md`);
    process.exit(1);
  }
  return readFileSync(path, "utf-8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Trajectory);
}

function main(): void {
  const [baselinePath, treatmentPath] = process.argv.slice(2);
  if (!baselinePath || !treatmentPath) {
    err(`usage: tsx e2e/common/scoring/run.ts <baseline.jsonl> <treatment.jsonl>`);
    process.exit(2);
  }
  const baseline = scoreArm("builtin grep/read", loadTrajectories(resolve(baselinePath)));
  const treatment = scoreArm("unerr MCP", loadTrajectories(resolve(treatmentPath)));
  const cmp = compareArms(baseline, treatment);
  const md = renderAb(cmp);

  const here = dirname(fileURLToPath(import.meta.url));
  const outDir = resolve(here, "../results");
  writeFileSync(resolve(outDir, "ab-report.md"), md);
  process.stdout.write(`${md}\n`);
  err(`\n▸ report → e2e/common/results/ab-report.md`);
}

main();
