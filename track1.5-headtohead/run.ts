/**
 * Track 1.5 — head-to-head benchmark.
 *
 * Runs the SAME frozen corpus of code questions through every available arm
 * (unerr, graphify, RTK, naive baseline) on an external OSS repo, scoring each
 * with the SAME o200k_base tokenizer and the SAME fidelity gate. Produces a
 * comparison report. Missing arms (tool not installed) are skipped, not faked.
 *
 * Usage:
 *   tsx benchmarks/track1.5-headtohead/run.ts <owner/repo | git-url | local-path> [--per N] [--refresh]
 *
 * Output: benchmarks/results/track1.5-<repo>.md
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { bootHarness } from "../lib/harness.js";
import { countTokens, ENCODING } from "../lib/tokenizer.js";
import { checkFidelity, loadOrFreezeCorpus } from "../track1-deterministic/corpus.js";
import {
  type ArmOutput,
  baselineArm,
  graphifyArm,
  graphifyAvailable,
  graphifyBuild,
  rtkArm,
  rtkAvailable,
  unerrArm,
} from "./arms.js";
import {
  type ArmRollup,
  type ArmTaskResult,
  type H2HMeta,
  renderH2H,
  rollupArm,
} from "./report.js";

function err(msg: string): void {
  process.stderr.write(`${msg}\n`);
}
function argNum(flag: string, dflt: number): number {
  const i = process.argv.indexOf(flag);
  return i >= 0 && process.argv[i + 1] ? Number(process.argv[i + 1]) : dflt;
}
function ver(cmd: string, args: string[]): string {
  try {
    return execFileSync(cmd, args, { encoding: "utf-8" }).trim().split("\n")[0] ?? "?";
  } catch {
    return "n/a";
  }
}
function gitShort(repoRoot: string): string {
  try {
    return execFileSync("git", ["rev-parse", "--short", "HEAD"], {
      cwd: repoRoot,
      encoding: "utf-8",
    }).trim();
  } catch {
    return "n/a";
  }
}

const CODE_EXTS = new Set([
  ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs",
  ".java", ".c", ".cpp", ".h", ".hpp", ".rb", ".cs", ".kt", ".php",
]);

/**
 * Total source tokens over one pass of the repo's code files — the input a
 * full semantic graph build (graphify's `/graphify .`) sends through an LLM.
 * unerr's AST build sends none; this quantifies that gap.
 */
function codebaseTokens(repoRoot: string): number {
  let files: string[];
  try {
    files = execFileSync("git", ["ls-files"], { cwd: repoRoot, encoding: "utf-8" })
      .split("\n")
      .filter((f) => f && CODE_EXTS.has(`.${f.split(".").pop()}`));
  } catch {
    return 0;
  }
  let total = 0;
  for (const f of files) {
    try {
      total += countTokens(readFileSync(resolve(repoRoot, f), "utf-8"));
    } catch {
      /* unreadable file — skip */
    }
  }
  return total;
}

/** Resolve a repo spec to a local checkout, cloning shallowly if needed. */
function resolveRepo(spec: string): string {
  if (existsSync(spec)) return resolve(spec);
  const cache = resolve(homedir(), ".cache", "unerr-h2h");
  mkdirSync(cache, { recursive: true });
  const url = spec.includes("://") || spec.startsWith("git@")
    ? spec
    : `https://github.com/${spec}.git`;
  const name = basename(spec).replace(/\.git$/, "");
  const dest = resolve(cache, name);
  if (!existsSync(dest)) {
    err(`▸ cloning ${url} → ${dest}`);
    execFileSync("git", ["clone", "--depth", "1", url, dest], {
      stdio: "inherit",
    });
  } else {
    err(`▸ using cached checkout ${dest}`);
  }
  return dest;
}

async function main(): Promise<void> {
  const spec = process.argv.slice(2).find((a) => !a.startsWith("--"));
  if (!spec) {
    err("usage: run.ts <owner/repo | git-url | local-path> [--per N] [--refresh]");
    process.exit(2);
  }
  const perCategory = argNum("--per", 8);
  const refresh = process.argv.includes("--refresh");

  const repoRoot = resolveRepo(spec);
  const repoName = basename(repoRoot);
  err(`\n▸ Track 1.5 — head-to-head on ${repoName}`);

  // ── Probe arms ──────────────────────────────────────────────────────────
  const gfyOk = graphifyAvailable();
  const rtkOk = rtkAvailable();
  err(`  arms: unerr ✓ · baseline ✓ · graphify ${gfyOk ? "✓" : "✗"} · rtk ${rtkOk ? "✓" : "✗"}`);

  // ── Boot unerr + shared corpus ────────────────────────────────────────────
  const h = await bootHarness(repoRoot, (m) => err(`  · ${m}`));
  const { tasks, fromFixture } = await loadOrFreezeCorpus(
    h,
    repoName,
    perCategory,
    refresh
  );
  err(`\n▸ corpus: ${tasks.length} tasks (${fromFixture ? "frozen fixture" : "derived + froze"})`);

  // ── Build graphify graph once ─────────────────────────────────────────────
  let graphPath: string | undefined;
  if (gfyOk) {
    const b = graphifyBuild(repoRoot, (m) => err(`  · ${m}`));
    if (b.ok) {
      graphPath = b.graphPath;
      err(`  · graphify graph: ${graphPath}`);
    } else {
      err(`  ! graphify build failed: ${b.error} — graphify arm will report empty`);
    }
  }

  // ── Run every task through every arm ──────────────────────────────────────
  const results: ArmTaskResult[] = [];
  let done = 0;
  for (const t of tasks) {
    const base = baselineArm(repoRoot, t.baseline);
    const arms: Record<string, ArmOutput> = {
      baseline: base,
      unerr: await unerrArm(h, t.unerr),
      rtk: rtkArm(repoRoot, t.baseline),
      graphify: graphifyArm(repoRoot, t, graphPath),
    };
    for (const [arm, out] of Object.entries(arms)) {
      if (!out.available) {
        results.push({
          arm,
          category: t.category,
          baselineTokens: base.tokens,
          armTokens: 0,
          fidelity: null,
        });
        continue;
      }
      let fidelity: boolean | null = null;
      try {
        fidelity = checkFidelity(t, out.payload);
      } catch {
        fidelity = null;
      }
      results.push({
        arm,
        category: t.category,
        baselineTokens: base.tokens,
        armTokens: out.tokens,
        fidelity,
        error: out.error,
      });
    }
    if (++done % 8 === 0) err(`  ... ${done}/${tasks.length}`);
  }

  await h.close();

  // ── Aggregate + render ────────────────────────────────────────────────────
  const armList: Array<{ arm: string; available: boolean }> = [
    { arm: "baseline", available: true },
    { arm: "unerr", available: true },
    { arm: "graphify", available: gfyOk },
    { arm: "rtk", available: rtkOk },
  ];
  const rollups: ArmRollup[] = armList.map(({ arm, available }) =>
    rollupArm(arm, available, results)
  );
  const categories = [...new Set(tasks.map((t) => t.category))];

  const unavailable: H2HMeta["unavailable"] = [];
  if (!gfyOk)
    unavailable.push({
      arm: "graphify",
      reason: "graphify CLI not on PATH",
      install: "uv tool install graphifyy",
    });
  if (!rtkOk)
    unavailable.push({
      arm: "rtk",
      reason: "rtk CLI not on PATH",
      install: "cargo install --git https://github.com/rtk-ai/rtk",
    });

  const meta: H2HMeta = {
    repo: `${repoName} (${spec})`,
    commit: gitShort(repoRoot),
    date: new Date().toISOString().slice(0, 10),
    entities: h.entityCount,
    edges: h.edgeCount,
    encoding: ENCODING,
    codebaseTokens: codebaseTokens(repoRoot),
    versions: {
      unerr: "in-process (this build)",
      graphify: gfyOk ? ver("graphify", ["--version"]) : "n/a",
      rtk: rtkOk ? ver("rtk", ["--version"]) : "n/a",
    },
    unavailable,
  };

  const md = renderH2H(rollups, categories, meta);
  const here = dirname(fileURLToPath(import.meta.url));
  const outDir = resolve(here, "../results");
  mkdirSync(outDir, { recursive: true });
  const stem = `track1.5-${repoName}`;
  writeFileSync(resolve(outDir, `${stem}.md`), md);
  writeFileSync(
    resolve(outDir, `${stem}.json`),
    JSON.stringify({ rollups, results, meta }, null, 2)
  );

  err(`\n${"─".repeat(64)}`);
  for (const r of rollups) {
    if (!r.available) {
      err(`  ${r.arm.padEnd(10)} not installed`);
      continue;
    }
    err(
      `  ${r.arm.padEnd(10)} ${String(r.fidelityPassed).padStart(2)}/${r.fidelityChecked} fidelity · ` +
        `${Math.round(r.armTokens / 1000)}K tok · ${r.arm === "baseline" ? "(reference)" : `−${r.pctReduction.toFixed(1)}% vs naive`}`
    );
  }
  err(`  Report: benchmarks/results/${stem}.md`);
  err(`${"─".repeat(64)}\n`);
}

main().catch((e) => {
  err(`\n✗ head-to-head failed: ${(e as Error).stack ?? e}`);
  process.exit(1);
});
