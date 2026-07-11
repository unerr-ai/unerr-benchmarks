/**
 * Head-to-head arms — each arm answers the SAME task and returns the text it
 * would put into an agent's context. Every arm is scored with the SAME
 * `o200k_base` tokenizer and the SAME fidelity gate (see run.ts), so the only
 * thing that varies between arms is the tool. That is what makes the comparison
 * fair: not "unerr's counter says X" but "for this question, who put fewer
 * tokens in context while still containing the answer".
 *
 * Arms:
 *   baseline  — naive grep + full file read (what an unassisted agent does)
 *   unerr     — the real QueryRouter tool result (graph-backed)
 *   rtk       — the SAME baseline commands, piped through `rtk` (output compression)
 *   graphify  — `graphify query "<question>"` against a prebuilt graph.json
 *
 * Benchmark/research tooling only — not shipped.
 */
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";
import type { Harness, ToolCall } from "../lib/harness.js";
import { countTokens } from "../lib/tokenizer.js";
import { type BaselineRecipe, grep } from "./baseline.js";
import type { TaskSpec } from "./corpus.js";

export interface ArmOutput {
  /** False = the tool isn't installed/usable; the arm is skipped, not failed. */
  available: boolean;
  /** Text the arm would place in the agent's context. */
  payload: string;
  tokens: number;
  detail: string;
  /** Set when the tool ran but errored (still counted — empty payload fails fidelity). */
  error?: string;
}

function readFileText(repoRoot: string, rel: string): string {
  try {
    const abs = rel.startsWith("/") ? rel : join(repoRoot, rel);
    return readFileSync(abs, "utf-8");
  } catch {
    return "";
  }
}

// ── baseline: raw grep + full file read ─────────────────────────────────────
export function baselineArm(repoRoot: string, recipe: BaselineRecipe): ArmOutput {
  const parts: string[] = [];
  let detail = "";
  switch (recipe.kind) {
    case "grep-only": {
      parts.push(grep(repoRoot, recipe.pattern).output);
      detail = `grep "${recipe.pattern}"`;
      break;
    }
    case "grep+read-top": {
      const g = grep(repoRoot, recipe.pattern);
      parts.push(g.output);
      const top = g.files.slice(0, recipe.readTopN);
      for (const f of top) parts.push(readFileText(repoRoot, f));
      detail = `grep + read ${top.length} file(s)`;
      break;
    }
    case "grep+read-files": {
      parts.push(grep(repoRoot, recipe.pattern).output);
      for (const f of recipe.files) parts.push(readFileText(repoRoot, f));
      detail = `grep + read ${recipe.files.length} file(s)`;
      break;
    }
    case "read-file": {
      parts.push(readFileText(repoRoot, recipe.file));
      detail = `read ${recipe.file}`;
      break;
    }
  }
  const payload = parts.join("\n");
  return { available: true, payload, tokens: countTokens(payload), detail };
}

// ── unerr: real QueryRouter tool result ─────────────────────────────────────
export async function unerrArm(h: Harness, call: ToolCall): Promise<ArmOutput> {
  try {
    const res = await h.runTool(call);
    return {
      available: true,
      payload: res.payload,
      tokens: countTokens(res.payload),
      detail: call.tool,
    };
  } catch (e) {
    return {
      available: true,
      payload: "",
      tokens: 0,
      detail: call.tool,
      error: (e as Error).message,
    };
  }
}

// ── rtk: the same baseline commands, compressed ─────────────────────────────
let _rtkChecked = false;
let _rtkOk = false;

export function rtkAvailable(): boolean {
  if (_rtkChecked) return _rtkOk;
  _rtkChecked = true;
  try {
    execFileSync("rtk", ["--version"], { encoding: "utf-8" });
    _rtkOk = true;
  } catch {
    _rtkOk = false;
  }
  return _rtkOk;
}

function rtkRun(repoRoot: string, args: string[]): string {
  try {
    return execFileSync("rtk", args, {
      cwd: repoRoot,
      encoding: "utf-8",
      maxBuffer: 64 * 1024 * 1024,
      timeout: 60_000,
    });
  } catch (e) {
    return (e as { stdout?: string }).stdout ?? "";
  }
}

// `rtk read` defaults to --level none (full content): RTK compresses command
// output, NOT source-file reads. We use defaults — RTK's real out-of-box UX.
const rtkRead = (repoRoot: string, f: string): string =>
  rtkRun(repoRoot, ["read", f]);
// `rtk grep <PATTERN> [PATH]` — ripgrep-backed, strips whitespace, groups by file.
const rtkGrep = (repoRoot: string, p: string): string =>
  rtkRun(repoRoot, ["grep", p, "."]);

export function rtkArm(repoRoot: string, recipe: BaselineRecipe): ArmOutput {
  if (!rtkAvailable())
    return { available: false, payload: "", tokens: 0, detail: "rtk not installed" };
  const parts: string[] = [];
  let detail = "";
  switch (recipe.kind) {
    case "grep-only": {
      parts.push(rtkGrep(repoRoot, recipe.pattern));
      detail = "rtk grep";
      break;
    }
    case "grep+read-top": {
      const g = grep(repoRoot, recipe.pattern); // raw grep only to learn which files
      parts.push(rtkGrep(repoRoot, recipe.pattern));
      const top = g.files.slice(0, recipe.readTopN);
      for (const f of top) parts.push(rtkRead(repoRoot, f));
      detail = `rtk grep + rtk read ${top.length}`;
      break;
    }
    case "grep+read-files": {
      parts.push(rtkGrep(repoRoot, recipe.pattern));
      for (const f of recipe.files) parts.push(rtkRead(repoRoot, f));
      detail = `rtk grep + rtk read ${recipe.files.length}`;
      break;
    }
    case "read-file": {
      parts.push(rtkRead(repoRoot, recipe.file));
      detail = "rtk read";
      break;
    }
  }
  const payload = parts.join("\n");
  return { available: true, payload, tokens: countTokens(payload), detail };
}

// ── graphify: query a prebuilt knowledge graph ──────────────────────────────
let _gfyChecked = false;
let _gfyOk = false;

export function graphifyAvailable(): boolean {
  if (_gfyChecked) return _gfyOk;
  _gfyChecked = true;
  try {
    execFileSync("graphify", ["--version"], { encoding: "utf-8" });
    _gfyOk = true;
  } catch {
    _gfyOk = false;
  }
  return _gfyOk;
}

export interface GraphifyBuild {
  ok: boolean;
  graphPath?: string;
  error?: string;
}

/** Build graphify's graph.json for `repoRoot` once, before the task loop. */
export function graphifyBuild(
  repoRoot: string,
  say: (m: string) => void
): GraphifyBuild {
  if (!graphifyAvailable()) return { ok: false, error: "graphify not installed" };
  try {
    say("graphify: building knowledge graph via `graphify update` (no LLM)");
    // `update <path>` extracts code files and writes graphify-out/graph.json.
    execFileSync("graphify", ["update", repoRoot], {
      cwd: repoRoot,
      encoding: "utf-8",
      maxBuffer: 256 * 1024 * 1024,
      timeout: 900_000,
    });
  } catch (e) {
    // Build may exit non-zero yet still emit graph.json — keep looking.
    say(`graphify build returned: ${(e as Error).message.split("\n")[0]}`);
  }
  const candidates = [
    join(repoRoot, "graphify-out", "graph.json"),
    join(repoRoot, "graph.json"),
    join(repoRoot, ".graphify", "graph.json"),
  ];
  const graphPath = candidates.find((p) => existsSync(p));
  return graphPath
    ? { ok: true, graphPath }
    : { ok: false, error: "graph.json not found after build" };
}

/**
 * graphify's native interface for a task. Every corpus task is anchored on a
 * named node — a symbol (find-symbol/get-entity/find-callers) or a file
 * (understand-file/imports) — so `graphify explain "<node>"` is its proper
 * node-and-neighbors lookup. We use it instead of the NL `query` command
 * because `query`'s keyword router is brittle (e.g. "imports" jumps to files
 * literally named *import*; "files" jumps to package.json's `files` key),
 * which would SANDBAG graphify. `explain` gives graphify its honest best shot.
 */
function graphifyExplainTarget(task: TaskSpec): string | undefined {
  const a = task.unerr.args ?? {};
  if (typeof a.file_path === "string") return basename(a.file_path);
  if (typeof a.key === "string") return a.key;
  if (typeof a.query === "string") return a.query;
  return undefined;
}

export function graphifyArm(
  repoRoot: string,
  task: TaskSpec,
  graphPath?: string
): ArmOutput {
  if (!graphifyAvailable())
    return { available: false, payload: "", tokens: 0, detail: "graphify not installed" };
  const target = graphifyExplainTarget(task);
  // Node-anchored questions → `explain`; only fall back to NL `query` when no
  // node target can be derived (should not happen for the current corpus).
  const sub = target ? "explain" : "query";
  const arg = target ?? task.question;
  const args = graphPath ? [sub, arg, "--graph", graphPath] : [sub, arg];
  try {
    const out = execFileSync("graphify", args, {
      cwd: repoRoot,
      encoding: "utf-8",
      maxBuffer: 64 * 1024 * 1024,
      timeout: 120_000,
    });
    return {
      available: true,
      payload: out,
      tokens: countTokens(out),
      detail: `graphify ${sub} "${arg}"`,
    };
  } catch (e) {
    const out = (e as { stdout?: string }).stdout ?? "";
    return {
      available: true,
      payload: out,
      tokens: countTokens(out),
      detail: `graphify ${sub} (nonzero exit)`,
      error: (e as Error).message.split("\n")[0],
    };
  }
}
