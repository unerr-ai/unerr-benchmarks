/**
 * Baseline model — what a naive agent WITHOUT unerr puts in its context to
 * answer the same question, using only built-in grep + Read.
 *
 * FAIRNESS IS THE WHOLE GAME. A strawman baseline produces a fake win. So this
 * model is deliberately CONSERVATIVE — it under-counts the naive path wherever
 * there is doubt:
 *   - grep is run with the same source-only excludes a careful agent would use
 *     (no node_modules/.git/dist/.unerr), so we don't inflate grep output.
 *   - "read top N" reads only the N most-likely files, not every match — a real
 *     agent often reads more, but we assume the disciplined minimum.
 *   - We count the grep OUTPUT the agent sees (file:line:text), not the cost of
 *     issuing the command.
 * Erring toward a cheap baseline means the measured saving is a LOWER BOUND.
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { countTokens } from "../lib/tokenizer.js";

const EXCLUDE_DIRS = [
  "node_modules",
  ".git",
  "dist",
  ".unerr",
  "coverage",
  ".next",
  "build",
];

export interface GrepResult {
  output: string;
  tokens: number;
  matchCount: number;
  /** Distinct files that matched, in match order. */
  files: string[];
}

/** Run `grep -rn` for `pattern` under `repoRoot`, source-only, capped output. */
export function grep(repoRoot: string, pattern: string): GrepResult {
  const args = [
    "-rnI", // recursive, line numbers, skip binary
    ...EXCLUDE_DIRS.flatMap((d) => [`--exclude-dir=${d}`]),
    "-e",
    pattern,
    ".",
  ];
  let output = "";
  try {
    output = execFileSync("grep", args, {
      cwd: repoRoot,
      encoding: "utf-8",
      maxBuffer: 64 * 1024 * 1024,
    });
  } catch (err) {
    // grep exits 1 when there are no matches — that's not an error for us.
    const e = err as { status?: number; stdout?: string };
    if (e.status === 1) output = e.stdout ?? "";
    else if (e.stdout) output = e.stdout;
    else throw err;
  }
  const lines = output.split("\n").filter(Boolean);
  const files: string[] = [];
  for (const line of lines) {
    const file = line.split(":", 1)[0]!;
    if (!files.includes(file)) files.push(file);
  }
  return {
    output,
    tokens: countTokens(output),
    matchCount: lines.length,
    files,
  };
}

/** Token cost of reading a file in full (what built-in Read returns by default). */
export function readFileTokens(repoRoot: string, relPath: string): number {
  try {
    const abs = relPath.startsWith("/") ? relPath : join(repoRoot, relPath);
    return countTokens(readFileSync(abs, "utf-8"));
  } catch {
    return 0;
  }
}

export type BaselineRecipe =
  /** Find where a symbol lives: grep the name, read the top-N matched files. */
  | { kind: "grep+read-top"; pattern: string; readTopN: number }
  /** Understand a file: read the whole thing. */
  | { kind: "read-file"; file: string }
  /** Pure retrieval question grep alone answers (e.g. "list all X"). */
  | { kind: "grep-only"; pattern: string }
  /** Blast radius: grep usages, read a fixed set of files to verify. */
  | { kind: "grep+read-files"; pattern: string; files: string[] };

export interface BaselineResult {
  tokens: number;
  detail: string;
}

/** Execute a baseline recipe against the real repo and count tokens. */
export function runBaseline(
  repoRoot: string,
  recipe: BaselineRecipe
): BaselineResult {
  switch (recipe.kind) {
    case "grep-only": {
      const g = grep(repoRoot, recipe.pattern);
      return {
        tokens: g.tokens,
        detail: `grep "${recipe.pattern}" → ${g.matchCount} lines`,
      };
    }
    case "grep+read-top": {
      const g = grep(repoRoot, recipe.pattern);
      const topFiles = g.files.slice(0, recipe.readTopN);
      const readTok = topFiles.reduce(
        (a, f) => a + readFileTokens(repoRoot, f),
        0
      );
      return {
        tokens: g.tokens + readTok,
        detail: `grep "${recipe.pattern}" (${g.matchCount} lines) + read ${topFiles.length} file(s)`,
      };
    }
    case "grep+read-files": {
      const g = grep(repoRoot, recipe.pattern);
      const readTok = recipe.files.reduce(
        (a, f) => a + readFileTokens(repoRoot, f),
        0
      );
      return {
        tokens: g.tokens + readTok,
        detail: `grep "${recipe.pattern}" (${g.matchCount} lines) + read ${recipe.files.length} verify file(s)`,
      };
    }
    case "read-file": {
      const t = readFileTokens(repoRoot, recipe.file);
      return { tokens: t, detail: `Read ${recipe.file} in full` };
    }
  }
}
