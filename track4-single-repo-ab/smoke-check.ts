/**
 * Smoke check — does unerr actually get triggered end-to-end? Builds a tiny
 * throwaway git repo, runs the FULL unerr install flow into it (the same
 * `installUnerrFlow` the A/B runner uses), fires ONE `claude -p` prompt that can
 * only be answered with the unerr graph tools, and inspects the agent's
 * tool-call stream for `mcp__unerr__*` calls. No baseline arm — this verifies
 * the unerr path works, nothing comparative. Exit 0 (PASS) iff the agent made
 * at least one unerr MCP tool call; exit 1 (FAIL) otherwise.
 *
 * Run: `npx tsx benchmarks/track4-single-repo-ab/smoke-check.ts`
 *
 * @sem domain=benchmark role=smoke-check
 */
import { execFileSync, execSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { installUnerrFlow } from "./repo-harness.js";

/** Create a minimal committed TS repo with one exported fn + a caller, so the
 * graph has a real entity AND a real caller edge for the prompt to surface. */
function makeSmokeRepo(): string {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "unerr-smoke-")));
  mkdirSync(join(dir, "src"), { recursive: true });
  writeFileSync(
    join(dir, "src/math.ts"),
    "/** Adds two numbers. */\n" +
      "export function addNumbers(a: number, b: number): number {\n" +
      "  return a + b;\n}\n"
  );
  writeFileSync(
    join(dir, "src/index.ts"),
    'import { addNumbers } from "./math.js";\n' +
      "/** Sums a list by folding addNumbers. */\n" +
      "export function total(xs: number[]): number {\n" +
      "  return xs.reduce((acc, x) => addNumbers(acc, x), 0);\n}\n"
  );
  writeFileSync(
    join(dir, "package.json"),
    `${JSON.stringify({ name: "unerr-smoke", version: "0.0.0", type: "module" }, null, 2)}\n`
  );
  execSync(
    "git init -q && git add -A && " +
      "git -c user.email=smoke@unerr.dev -c user.name=smoke commit -qm init",
    { cwd: dir }
  );
  return dir;
}

interface StreamResult {
  /** Every tool the agent invoked, in order (built-in + MCP). */
  allCalls: string[];
  /** Just the `mcp__unerr__*` calls — the signal that unerr was triggered. */
  unerrCalls: string[];
  /** The agent's final answer text. */
  resultText: string;
}

/** Run one prompt with a streamed tool-call transcript and pull out which tools
 * the agent actually invoked. `stream-json` requires `--verbose` in -p mode. */
function runPromptStream(repoDir: string, prompt: string): StreamResult {
  const out = execFileSync(
    "claude",
    [
      "-p",
      prompt,
      "--output-format",
      "stream-json",
      "--verbose",
      "--dangerously-skip-permissions",
    ],
    { cwd: repoDir, encoding: "utf-8", maxBuffer: 64 * 1024 * 1024 }
  );
  const allCalls: string[] = [];
  let resultText = "";
  for (const line of out.split("\n")) {
    if (!line.trim()) continue;
    let obj: {
      type?: string;
      result?: string;
      message?: { content?: Array<{ type?: string; name?: string }> };
    };
    try {
      obj = JSON.parse(line);
    } catch {
      continue; // non-JSON noise line — skip
    }
    if (obj.type === "assistant" && obj.message?.content) {
      for (const block of obj.message.content) {
        if (block.type === "tool_use" && typeof block.name === "string") {
          allCalls.push(block.name);
        }
      }
    }
    if (obj.type === "result" && typeof obj.result === "string") {
      resultText = obj.result;
    }
  }
  const unerrCalls = allCalls.filter((n) => n.startsWith("mcp__unerr__"));
  return { allCalls, unerrCalls, resultText };
}

async function main(): Promise<void> {
  const repoDir = makeSmokeRepo();
  process.stderr.write(`\nunerr smoke check\n  repo: ${repoDir}\n`);
  let pass = false;
  try {
    process.stderr.write(
      "  installing unerr (dev-pro + pm start + install + config)…\n"
    );
    installUnerrFlow(repoDir);

    const prompt =
      "Using the unerr MCP tools (search_code and get_references), find the " +
      "exported function named addNumbers in this repo. Report its file path " +
      "and list every function that calls it. Do not edit any files.";
    process.stderr.write("  running one prompt…\n");
    const { allCalls, unerrCalls, resultText } = runPromptStream(
      repoDir,
      prompt
    );
    pass = unerrCalls.length > 0;

    const uniqUnerr = [...new Set(unerrCalls)];
    process.stderr.write("\n=== result ===\n");
    process.stderr.write(`  tool calls total : ${allCalls.length}\n`);
    process.stderr.write(
      `  unerr tool calls : ${unerrCalls.length}${uniqUnerr.length ? ` → ${uniqUnerr.join(", ")}` : ""}\n`
    );
    process.stderr.write(`\n  agent answer:\n${resultText.slice(0, 900)}\n`);
    process.stderr.write(
      `\n  ${pass ? "PASS — unerr was triggered" : "FAIL — no unerr MCP tool call observed"}\n`
    );
  } finally {
    // Unregister the throwaway repo from unerrd and delete it.
    try {
      execSync(`unerr pm remove ${repoDir}`, { stdio: "ignore" });
    } catch {
      // not registered / daemon down — nothing to free
    }
    rmSync(repoDir, { recursive: true, force: true });
    process.stderr.write("  cleaned up.\n");
  }
  process.exitCode = pass ? 0 : 1;
}

main().catch((err) => {
  process.stderr.write(`smoke check errored: ${(err as Error).message}\n`);
  process.exitCode = 1;
});
