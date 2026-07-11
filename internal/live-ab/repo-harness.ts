/**
 * Sets up an isolated git worktree per arm of the target repo, applies a task's
 * break, and runs its test oracle. Worktree isolation means the three arms never
 * see each other's edits, and resetting to `baseCommit` between tasks keeps the
 * oracle deterministic. The unerr arms get unerr installed into the worktree; the
 * baseline arm does not.
 *
 * @sem domain=benchmark role=harness
 */
import { execSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { type ArmId, type TaskSpec, armUsesUnerr } from "./types.js";

/** Where one arm's worktree + scratch state live. */
export interface ArmWorkspace {
  arm: ArmId;
  /** Absolute path to the arm's git worktree (the agent's cwd). */
  worktreeDir: string;
  /** Absolute path to the arm's `.unerr` dir (where metrics.db lands). */
  unerrDir: string;
}

/** Result of running one task's gate test. */
export interface OracleResult {
  resolved: boolean;
  exitCode: number;
}

const RUN_OPTS = { stdio: "inherit" as const, encoding: "utf-8" as const };

/** Run a shell command in `cwd`, throwing on non-zero exit (setup must succeed). */
function sh(command: string, cwd: string): void {
  execSync(command, { ...RUN_OPTS, cwd });
}

/**
 * Run the gate test for a task. Exit 0 = resolved; any other exit = unresolved.
 * Never throws on a failing test — a red test is data, not an error.
 */
export function runOracle(task: TaskSpec, worktreeDir: string): OracleResult {
  try {
    execSync(task.testCommand, { ...RUN_OPTS, cwd: worktreeDir });
    return { resolved: true, exitCode: 0 };
  } catch (err) {
    const code =
      typeof (err as { status?: number }).status === "number"
        ? (err as { status: number }).status
        : 1;
    return { resolved: false, exitCode: code };
  }
}

/** Apply a task's break command (if any) so the gate test starts red. */
export function applyBreak(task: TaskSpec, worktreeDir: string): void {
  if (task.breakCommand) {
    sh(task.breakCommand, worktreeDir);
  }
}

/**
 * Create a fresh worktree for `arm` under `<workRoot>/<arm>`, checked out at
 * `baseCommit`. Removes any prior worktree at that path first so a re-run starts
 * clean. For the unerr arms, runs `installUnerr` to wire the worktree's `.mcp.json`.
 */
export function setupArmWorkspace(
  repoDir: string,
  workRoot: string,
  arm: ArmId,
  baseCommit: string | undefined,
  installUnerr: (worktreeDir: string) => void
): ArmWorkspace {
  const worktreeDir = resolve(join(workRoot, arm));
  // Tear down a stale worktree from a previous run (git refuses to reuse a path).
  if (existsSync(worktreeDir)) {
    try {
      execSync(`git worktree remove --force ${worktreeDir}`, {
        ...RUN_OPTS,
        cwd: repoDir,
      });
    } catch {
      rmSync(worktreeDir, { recursive: true, force: true });
    }
  }
  mkdirSync(workRoot, { recursive: true });
  // Clear any dangling registration whose dir was deleted out-of-band (e.g. a
  // prior `rm -rf out` after a killed run) — otherwise `worktree add` fails with
  // "missing but already registered worktree".
  execSync("git worktree prune", { ...RUN_OPTS, cwd: repoDir });

  const ref = baseCommit ?? "HEAD";
  sh(`git worktree add --detach ${worktreeDir} ${ref}`, repoDir);

  if (armUsesUnerr(arm)) {
    installUnerr(worktreeDir);
  }

  return { arm, worktreeDir, unerrDir: join(worktreeDir, ".unerr") };
}

/** Install artifacts that `unerr install` writes as UNTRACKED files in the
 * worktree. `git clean` would delete them between tasks (it did in the first
 * run — task 2's unerr arm ran with zero unerr), so every reset must exclude
 * them. node_modules is excluded too so a reset never forces a re-install. */
const INSTALL_ARTIFACTS = [
  ".mcp.json",
  "CLAUDE.md",
  ".claude",
  ".unerr",
  "node_modules",
];

/** Pro dev-context for one worktree so its unerr is never blocked by the
 * free-tier single-active-repo cap. Mirrors the global ~/.unerr/dev.json
 * (apiUrl + tier); honored only in dev builds (__UNERR_DEV_BUILD__). */
export function writeWorktreeDevPro(worktreeDir: string): void {
  const dir = join(worktreeDir, ".unerr");
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, "dev.json"),
    `${JSON.stringify({ apiUrl: "http://localhost:3000", tier: "pro" }, null, 2)}\n`
  );
}

/**
 * Install unerr into a worktree/repo exactly the way a user does by hand —
 * `cd <repo> && unerr install claude-code`. With the daemon already running
 * (it is, in any dev session), install's own step 7 registers the repo in the
 * unerrd registry and spins up the per-repo proxy child, which then writes its
 * own `.unerr/config.json` (an empty `{}`; the repoId lives in the registry).
 *
 * Two things this deliberately does NOT do, because both broke the first run:
 *   - It does NOT pre-spawn with `unerr pm start --detached`. install already
 *     spawns the child; a second pre-spawn bounced off the per-repo PID lock and
 *     the daemon false-reported "ensureRepo failed: Child exited", looping the
 *     agent's bridge forever.
 *   - It does NOT hand-write config.json with a synthesized repoId. That repoId
 *     disagreed with the one install registered, so the child never matched its
 *     registry entry. Letting the child write its own `{}` keeps them in sync.
 *
 * Only the pro dev-context is written first, so multiple worktrees never trip
 * the free-tier single-active-repo cap. Verified by hand 2026-06-17: clone →
 * install → registered + child indexed; uninstall → deregistered.
 */
export function installUnerrFlow(worktreeDir: string): void {
  writeWorktreeDevPro(worktreeDir);
  execSync("unerr install claude-code", { cwd: worktreeDir, stdio: "inherit" });
}

/**
 * Tear unerr back out of a worktree the way a user does — `unerr uninstall
 * claude-code` from inside the repo. This removes the MCP config + skills +
 * hooks AND deregisters the repo from the unerrd registry (and stops its child),
 * so each arm leaves the daemon as clean as it found it. `.unerr/` data is left
 * in place (uninstall preserves it); the worktree teardown deletes it anyway.
 */
export function uninstallUnerrFlow(worktreeDir: string): void {
  try {
    execSync("unerr uninstall claude-code", {
      cwd: worktreeDir,
      stdio: "inherit",
    });
  } catch {
    // Best-effort teardown — a failed uninstall must not abort the benchmark.
  }
}

/**
 * Reset a worktree back to `baseCommit` between tasks — discards the agent's
 * edits and any break so the next task's oracle is independent. Keeps `.unerr`
 * (and thus metrics history) unless `wipeUnerr` is set. The `git clean` excludes
 * the unerr install artifacts so they survive into the next task.
 */
export function resetWorktree(
  ws: ArmWorkspace,
  baseCommit: string | undefined,
  wipeUnerr: boolean
): void {
  const ref = baseCommit ?? "HEAD";
  const cleanExcludes = INSTALL_ARTIFACTS.map((a) => `-e ${a}`).join(" ");
  sh("git reset --hard", ws.worktreeDir);
  sh(`git clean -fd ${cleanExcludes}`, ws.worktreeDir);
  sh(`git checkout --detach ${ref}`, ws.worktreeDir);
  if (wipeUnerr && existsSync(ws.unerrDir)) {
    rmSync(ws.unerrDir, { recursive: true, force: true });
  }
}

/**
 * Wipe only the unerr memory surface (anchored notes + facts) for the
 * `unerr-nomemory` arm, while keeping the graph and metrics.db. Removing the
 * facts DB forces the next task to start with no carried-over memory.
 */
export function wipeUnerrMemory(ws: ArmWorkspace): void {
  for (const rel of ["facts.db", "notes.db"]) {
    const p = join(ws.unerrDir, rel);
    if (existsSync(p)) {
      rmSync(p, { force: true });
    }
  }
}

/** Write an empty MCP config (used to neutralize the baseline arm). */
export function writeEmptyMcpConfig(path: string): string {
  const resolved = resolve(path);
  writeFileSync(resolved, JSON.stringify({ mcpServers: {} }, null, 2));
  return resolved;
}
