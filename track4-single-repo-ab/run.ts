/**
 * Track 4 orchestrator CLI: loads a task manifest, sets up one worktree per arm,
 * and for each (task, arm) applies the break, times the `claude -p` run, runs the
 * gate test, reads the unerr metrics window, and records a RunRecord. At the end
 * it scores all runs and writes a markdown report. Run with `--dry-run` to validate
 * the manifest and arm wiring without spending any agent budget.
 *
 * @sem domain=benchmark role=orchestrator
 */
import { execSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import {
  type DriverOptions,
  type DriverResult,
  buildClaudeArgs,
  runClaude,
} from "./claude-driver.js";
import { readPlatformEvents } from "./metrics-reader.js";
import {
  type ArmWorkspace,
  applyBreak,
  installUnerrFlow,
  resetWorktree,
  runOracle,
  setupArmWorkspace,
  uninstallUnerrFlow,
  wipeUnerrMemory,
  writeEmptyMcpConfig,
} from "./repo-harness.js";
import { buildReport, renderReport } from "./score.js";
import {
  ALL_ARMS,
  type ArmId,
  type RunRecord,
  type TaskManifest,
  armKeepsMemory,
  armUsesUnerr,
  emptyPlatform,
} from "./types.js";

/** Parsed CLI flags. */
interface CliOpts {
  manifestPath: string;
  reps: number;
  arms: ArmId[];
  dryRun: boolean;
  scoreOnly: boolean;
  /** Explicit --out override; when unset, main() derives a path OUTSIDE the
   * target repo's tree (see the parent-conflict note in main). */
  outDir?: string;
  permissionMode: string;
  model?: string;
  maxTurns?: number;
}

function parseArgs(argv: string[]): CliOpts {
  const [manifestPath] = argv;
  if (!manifestPath) {
    throw new Error(
      "usage: tsx run.ts <tasks.json> [--reps N] [--arms a,b] [--dry-run] [--score-only] [--out DIR] [--permission-mode MODE] [--model M] [--max-turns N]"
    );
  }
  const flag = (name: string): string | undefined => {
    const i = argv.indexOf(`--${name}`);
    return i >= 0 ? argv[i + 1] : undefined;
  };
  const has = (name: string): boolean => argv.includes(`--${name}`);

  const armsRaw = flag("arms");
  const arms = armsRaw
    ? (armsRaw.split(",").map((s) => s.trim()) as ArmId[])
    : ALL_ARMS;
  for (const a of arms) {
    if (!ALL_ARMS.includes(a)) {
      throw new Error(`unknown arm "${a}" — valid: ${ALL_ARMS.join(", ")}`);
    }
  }

  return {
    manifestPath: resolve(manifestPath),
    reps: Number(flag("reps") ?? 1),
    arms,
    dryRun: has("dry-run"),
    scoreOnly: has("score-only"),
    outDir: flag("out") ? resolve(flag("out") as string) : undefined,
    // Default to bypassPermissions → --dangerously-skip-permissions so the
    // unerr arm's mcp__unerr__* tool calls are never blocked on a permission
    // prompt the unattended run can't answer (acceptEdits blocked them).
    permissionMode: flag("permission-mode") ?? "bypassPermissions",
    model: flag("model"),
    maxTurns: flag("max-turns") ? Number(flag("max-turns")) : undefined,
  };
}

function loadManifest(path: string): TaskManifest {
  const raw = JSON.parse(readFileSync(path, "utf-8")) as TaskManifest;
  if (!raw.repo || !Array.isArray(raw.tasks) || raw.tasks.length === 0) {
    throw new Error(`manifest ${path} needs a "repo" and a non-empty "tasks"`);
  }
  return raw;
}

/** Coarse epoch-ms clock for run windows. */
function now(): number {
  return Number(execSync("date +%s%3N", { encoding: "utf-8" }).trim());
}

async function main(): Promise<void> {
  const opts = parseArgs(process.argv.slice(2));
  const manifest = loadManifest(opts.manifestPath);
  const arms = manifest.arms ?? opts.arms;
  const repoDir = resolve(manifest.repo);

  // The worktrees MUST live OUTSIDE any registered unerr repo. detectParentConflict
  // (src/daemon/registry.ts) rejects registering a repo nested under a registered
  // parent, so a worktree placed inside this checkout (e.g. benchmarks/.../out)
  // never gets its config.json mirrored and the per-repo child crash-loops with
  // "No .unerr/config.json". Defaulting beside the target repo (e.g.
  // ~/bench-repos/track4-ab-out) keeps every worktree clear of this repo's tree.
  // Override with --out, but point it somewhere not under a registered repo.
  const outDir = opts.outDir ?? join(dirname(repoDir), "track4-ab-out");
  mkdirSync(outDir, { recursive: true });
  process.stderr.write(`out dir → ${outDir}\n`);

  const runsPath = join(outDir, "runs.jsonl");

  // --score-only: re-score an existing runs.jsonl without touching the agent.
  if (opts.scoreOnly) {
    const runs = readFileSync(runsPath, "utf-8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l) as RunRecord);
    writeReport(runs, outDir);
    return;
  }

  const emptyMcp = writeEmptyMcpConfig(join(outDir, "empty-mcp.json"));
  const driverOpts: DriverOptions = {
    permissionMode: opts.permissionMode,
    model: opts.model,
    emptyMcpConfigPath: emptyMcp,
    maxTurns: opts.maxTurns,
  };

  const workRoot = join(outDir, "worktrees");

  // Install unerr into a worktree by writing its project-level .mcp.json. The
  // unerr binary is expected on PATH (the dev links it globally during testing).
  const installUnerr = (worktreeDir: string): void => {
    if (opts.dryRun) return;
    installUnerrFlow(worktreeDir);
  };

  const records: RunRecord[] = [];
  const recordLines: string[] = [];

  for (const arm of arms) {
    const ws: ArmWorkspace = setupArmWorkspace(
      repoDir,
      workRoot,
      arm,
      manifest.baseCommit,
      installUnerr
    );
    if (manifest.setupCommand && !opts.dryRun) {
      execSync(manifest.setupCommand, {
        cwd: ws.worktreeDir,
        stdio: "inherit",
      });
    }

    for (let rep = 0; rep < opts.reps; rep++) {
      for (const task of manifest.tasks) {
        applyBreak(task, ws.worktreeDir);

        const startTs = now();
        let record: RunRecord;

        if (opts.dryRun) {
          // Validate arg wiring without spending budget.
          const args = buildClaudeArgs(task.prompt, arm, driverOpts);
          process.stderr.write(
            `[dry-run] ${arm} :: ${task.id} :: claude ${args.map((a) => (a.includes(" ") ? `"${a}"` : a)).join(" ")}\n`
          );
          record = {
            instanceId: `${task.id}#${rep}`,
            taskId: task.id,
            arm,
            dependsOn: task.dependsOn ?? [],
            resolved: false,
            inputTokens: 0,
            freshInputTokens: 0,
            cacheCreateTokens: 0,
            cacheReadTokens: 0,
            outputTokens: 0,
            turns: 0,
            wallMs: 0,
            costUsd: 0,
            breakages: 0,
            platform: emptyPlatform(),
          };
        } else {
          let breakages = 0;
          let driver: DriverResult = {
            inputTokens: 0,
            freshInputTokens: 0,
            cacheCreateTokens: 0,
            cacheReadTokens: 0,
            outputTokens: 0,
            turns: 0,
            costUsd: 0,
            isError: false,
            resultText: "",
          };
          try {
            driver = runClaude(task.prompt, arm, ws.worktreeDir, driverOpts);
            if (driver.isError) breakages = 1;
          } catch (err) {
            // A CLI crash is a breakage, not a thrown run — record and continue.
            process.stderr.write(
              `[error] ${arm} :: ${task.id} :: ${(err as Error).message}\n`
            );
            breakages = 1;
          }
          const oracle = runOracle(task, ws.worktreeDir);
          const endTs = now();
          const platform = armUsesUnerr(arm)
            ? readPlatformEvents(ws.unerrDir, { startTs, endTs })
            : emptyPlatform();

          record = {
            instanceId: `${task.id}#${rep}`,
            taskId: task.id,
            arm,
            dependsOn: task.dependsOn ?? [],
            resolved: oracle.resolved,
            inputTokens: driver.inputTokens,
            freshInputTokens: driver.freshInputTokens,
            cacheCreateTokens: driver.cacheCreateTokens,
            cacheReadTokens: driver.cacheReadTokens,
            outputTokens: driver.outputTokens,
            turns: driver.turns,
            wallMs: endTs - startTs,
            costUsd: driver.costUsd,
            breakages,
            platform,
          };
        }

        records.push(record);
        recordLines.push(JSON.stringify(record));

        // Reset between tasks so each oracle is independent. The nomemory arm
        // also has its memory wiped so a later dependent task starts cold.
        if (!opts.dryRun) {
          resetWorktree(ws, manifest.baseCommit, false);
          if (armUsesUnerr(arm) && !armKeepsMemory(arm)) {
            wipeUnerrMemory(ws);
          }
        }
      }
    }

    // Deregister unerr from the daemon for this arm, mirroring the manual
    // `unerr uninstall claude-code` teardown so the next arm (and the next run)
    // starts from a clean registry. Baseline never installed unerr, so skip it.
    if (!opts.dryRun && armUsesUnerr(arm)) {
      uninstallUnerrFlow(ws.worktreeDir);
    }
  }

  writeFileSync(runsPath, `${recordLines.join("\n")}\n`);
  process.stderr.write(`\nwrote ${records.length} runs → ${runsPath}\n`);
  if (!opts.dryRun) {
    writeReport(records, outDir);
  }
}

function writeReport(runs: RunRecord[], outDir: string): void {
  const report = buildReport(runs);
  const md = renderReport(report);
  const reportPath = join(outDir, "REPORT.md");
  writeFileSync(reportPath, md);
  process.stderr.write(`wrote report → ${reportPath}\n`);
}

main().catch((err) => {
  process.stderr.write(`${(err as Error).stack ?? err}\n`);
  process.exit(1);
});
