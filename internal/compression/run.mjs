/**
 * Phase 6 benchmark — bulk vs single fetch_url, measured on a real agent loop.
 *
 * Motivation (FETCH_URL_PIPELINE.md Part B/C): after a web search the agent
 * reads N result pages. Doing it as N separate fetch_url({url}) calls means N
 * tool roundtrips, and EVERY roundtrip re-bills the whole conversation prefix
 * as cache_read. One bulk fetch_url({urls:[...]}) reads all N pages in a single
 * roundtrip → the prefix is re-billed once. That saving only exists inside a
 * real agent loop, so this harness drives `claude -p` headless and reads the
 * actual billed usage from the stream-json transcript — nothing is modeled.
 *
 * Two arms, identical everything except call shape:
 *   - SEQ : N separate fetch_url({url:...}) calls, one per page.
 *   - BULK: one fetch_url({urls:[...N...]}) call.
 * Both run in THIS repo, so the large CLAUDE.md + unerr prefix is the cached
 * block being re-billed — a realistic mid-session prefix, identical per arm.
 *
 * Run:  node internal/compression/run.mjs [--pages 5] [--reps 3] [--model <id>]
 * Env:  BENCH_MODEL overrides the model (default claude-sonnet-4-6).
 */

import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { startFixtureServer } from "./fixture-server.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, "..", "..");

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const PAGES = Number(arg("pages", "5"));
const REPS = Number(arg("reps", "3"));
const MODEL = arg("model", process.env.BENCH_MODEL || "claude-sonnet-4-6");
const RANK_PROMPT = "token compression caching extraction ranking";

function seqPrompt(urls) {
  const lines = urls
    .map((u) => `fetch_url({url:"${u}", prompt:"${RANK_PROMPT}"})`)
    .join("\n");
  return [
    "You are executing a controlled benchmark. Follow these steps EXACTLY and do nothing else.",
    `Call the unerr fetch_url tool ${urls.length} times — ONE call per URL, each with a single \`url\` argument. Do NOT use the \`urls\` array. Do NOT batch. Make the calls one at a time, in this order:`,
    lines,
    'After the last call returns, reply with exactly the word: DONE',
    "Do not call any other tool. Do not summarize the pages.",
  ].join("\n\n");
}

function bulkPrompt(urls) {
  const arr = urls.map((u) => `"${u}"`).join(", ");
  return [
    "You are executing a controlled benchmark. Follow these steps EXACTLY and do nothing else.",
    `Call the unerr fetch_url tool ONE time, passing all ${urls.length} URLs in the \`urls\` array:`,
    `fetch_url({urls:[${arr}], prompt:"${RANK_PROMPT}"})`,
    "Make exactly ONE fetch_url call. Do NOT call fetch_url once per URL.",
    'After it returns, reply with exactly the word: DONE',
    "Do not call any other tool. Do not summarize the pages.",
  ].join("\n\n");
}

/** Run one headless `claude -p` arm and sum the real billed usage from the
 *  stream-json transcript. Never throws on agent error — returns ok:false. */
function runArm(prompt) {
  return new Promise((resolve) => {
    const args = [
      "-p",
      prompt,
      "--output-format",
      "stream-json",
      "--verbose",
      "--permission-mode",
      "bypassPermissions",
      "--model",
      MODEL,
      "--max-turns",
      "25",
    ];
    const child = spawn("claude", args, {
      cwd: REPO_ROOT,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    let err = "";
    child.stdout.on("data", (d) => {
      out += d.toString();
    });
    child.stderr.on("data", (d) => {
      err += d.toString();
    });
    child.on("close", () => {
      resolve(parseStream(out, err));
    });
  });
}

/** Parse stream-json NDJSON: sum per-assistant-message usage (true total billed
 *  tokens across the whole session), count fetch_url single vs bulk tool_use
 *  blocks (compliance), and pull total_cost_usd + num_turns from the result. */
function parseStream(stdout, stderr) {
  const usage = {
    input: 0,
    cacheCreation: 0,
    cacheRead: 0,
    output: 0,
  };
  let assistantMsgs = 0;
  let fetchSingle = 0;
  let fetchBulk = 0;
  let otherTools = 0;
  let costUsd = 0;
  let numTurns = 0;
  let isError = false;
  let resultText = "";

  for (const line of stdout.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    let ev;
    try {
      ev = JSON.parse(t);
    } catch {
      continue;
    }
    if (ev.type === "assistant" && ev.message) {
      assistantMsgs++;
      const u = ev.message.usage ?? {};
      usage.input += u.input_tokens ?? 0;
      usage.cacheCreation += u.cache_creation_input_tokens ?? 0;
      usage.cacheRead += u.cache_read_input_tokens ?? 0;
      usage.output += u.output_tokens ?? 0;
      for (const block of ev.message.content ?? []) {
        if (block.type !== "tool_use") continue;
        const name = block.name ?? "";
        if (name.includes("fetch_url")) {
          const inp = block.input ?? {};
          if (Array.isArray(inp.urls)) fetchBulk++;
          else if (typeof inp.url === "string") fetchSingle++;
          else otherTools++;
        } else {
          otherTools++;
        }
      }
    } else if (ev.type === "result") {
      costUsd = ev.total_cost_usd ?? 0;
      numTurns = ev.num_turns ?? 0;
      isError = ev.is_error === true;
      resultText = ev.result ?? "";
    }
  }

  const billedTokens =
    usage.input + usage.cacheCreation + usage.cacheRead + usage.output;
  return {
    ok: assistantMsgs > 0 && !isError,
    usage,
    billedTokens,
    assistantMsgs,
    fetchSingle,
    fetchBulk,
    otherTools,
    costUsd,
    numTurns,
    resultText: resultText.slice(0, 60),
    stderrTail: stderr.split("\n").filter(Boolean).slice(-3).join(" | "),
  };
}

function median(nums) {
  const s = [...nums].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function pct(seq, bulk) {
  if (seq <= 0) return 0;
  return Math.round(((seq - bulk) / seq) * 1000) / 10;
}

async function main() {
  const { server, urls, origin } = await startFixtureServer(PAGES);
  console.error(`▸ fixture server: ${origin} (${PAGES} pages)`);
  console.error(`▸ model: ${MODEL} · reps: ${REPS}\n`);

  const runs = [];
  for (let r = 0; r < REPS; r++) {
    console.error(`── rep ${r + 1}/${REPS} ─────────────────────────────`);
    console.error("  SEQ  (5 single fetch_url calls) …");
    const seq = await runArm(seqPrompt(urls));
    console.error(
      `    billed=${seq.billedTokens} tok  cost=$${seq.costUsd.toFixed(4)}  turns=${seq.numTurns}  fetch(single=${seq.fetchSingle},bulk=${seq.fetchBulk})  ok=${seq.ok}`
    );
    console.error("  BULK (1 fetch_url urls:[...] call) …");
    const bulk = await runArm(bulkPrompt(urls));
    console.error(
      `    billed=${bulk.billedTokens} tok  cost=$${bulk.costUsd.toFixed(4)}  turns=${bulk.numTurns}  fetch(single=${bulk.fetchSingle},bulk=${bulk.fetchBulk})  ok=${bulk.ok}`
    );
    // Compliance gate: SEQ must be N single calls + 0 bulk; BULK must be 1
    // bulk + 0 single. A non-compliant rep is recorded but excluded from the
    // median so a stray model turn doesn't pollute the headline.
    const compliant =
      seq.ok &&
      bulk.ok &&
      seq.fetchSingle === PAGES &&
      seq.fetchBulk === 0 &&
      bulk.fetchBulk === 1 &&
      bulk.fetchSingle === 0;
    console.error(`    compliant=${compliant}\n`);
    runs.push({ rep: r + 1, compliant, seq, bulk });
  }

  await new Promise((res) => server.close(() => res()));

  const valid = runs.filter((r) => r.compliant);
  const summary = {
    model: MODEL,
    pages: PAGES,
    reps: REPS,
    validReps: valid.length,
    headline: null,
    runs,
  };

  if (valid.length > 0) {
    const seqBilled = median(valid.map((r) => r.seq.billedTokens));
    const bulkBilled = median(valid.map((r) => r.bulk.billedTokens));
    const seqCost = median(valid.map((r) => r.seq.costUsd));
    const bulkCost = median(valid.map((r) => r.bulk.costUsd));
    const seqCacheRead = median(valid.map((r) => r.seq.usage.cacheRead));
    const bulkCacheRead = median(valid.map((r) => r.bulk.usage.cacheRead));
    const seqTurns = median(valid.map((r) => r.seq.numTurns));
    const bulkTurns = median(valid.map((r) => r.bulk.numTurns));
    summary.headline = {
      billedTokens: { seq: seqBilled, bulk: bulkBilled, reductionPct: pct(seqBilled, bulkBilled) },
      costUsd: { seq: seqCost, bulk: bulkCost, reductionPct: pct(seqCost, bulkCost) },
      cacheReadTokens: { seq: seqCacheRead, bulk: bulkCacheRead, reductionPct: pct(seqCacheRead, bulkCacheRead) },
      turns: { seq: seqTurns, bulk: bulkTurns },
    };
  }

  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outPath = join(__dirname, "results", `run-${stamp}.json`);
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(summary, null, 2));

  console.error("════════════════════════════════════════════════════");
  if (summary.headline) {
    const h = summary.headline;
    console.error(`RESULT (median of ${valid.length} compliant rep(s)):`);
    console.error(
      `  total billed tokens : SEQ ${h.billedTokens.seq}  →  BULK ${h.billedTokens.bulk}   = ${h.billedTokens.reductionPct}% fewer`
    );
    console.error(
      `  cache_read tokens   : SEQ ${h.cacheReadTokens.seq}  →  BULK ${h.cacheReadTokens.bulk}   = ${h.cacheReadTokens.reductionPct}% fewer`
    );
    console.error(
      `  billed cost (USD)   : SEQ $${h.costUsd.seq.toFixed(4)}  →  BULK $${h.costUsd.bulk.toFixed(4)}   = ${h.costUsd.reductionPct}% cheaper`
    );
    console.error(
      `  roundtrips (turns)  : SEQ ${h.turns.seq}  →  BULK ${h.turns.bulk}`
    );
  } else {
    console.error("NO COMPLIANT REPS — see runs[] in the results file for tool-call counts.");
  }
  console.error(`\n▸ written: ${outPath}`);
}

main().catch((e) => {
  console.error("benchmark failed:", e);
  process.exit(1);
});
