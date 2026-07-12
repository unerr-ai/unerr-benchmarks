#!/usr/bin/env node
/**
 * score.mjs — Claude local-docker scoring adapter
 *
 * Joins per-instance meta JSONL (produced by the Claude benchmark runner) with
 * per-instance resolution verdicts (produced by the SWE-bench harness grader),
 * then emits two JSONL files in the exact Trajectory shape consumed by the
 * shared SWE-Effi scorer (e2e/common/scoring/run.ts).
 *
 * NOTE ON LOCALIZATION F1:
 *   The local-docker full-resolve backend does NOT produce file-localization
 *   predictions. Localization F1 is the fly-remote loc-runner metric and is
 *   out of scope here. This backend measures resolve-rate + SWE-Effi
 *   (resolves-per-token/usd).
 *
 * Usage:
 *   node score.mjs <results-dir> <grader-on.json> <grader-off.json> [out-dir]
 *
 *   results-dir    Directory containing meta_on.jsonl and meta_off.jsonl
 *   grader-on.json SWE-bench grader output for the "on" arm
 *                  (e.g. claude-on.claude_on.json written by run_evaluation)
 *   grader-off.json SWE-bench grader output for the "off" arm
 *                  (e.g. claude-off.claude_off.json)
 *   out-dir        Directory to write arm-on.jsonl and arm-off.jsonl into.
 *                  Defaults to <results-dir>.
 *
 * Output Trajectory row shape (matches e2e/common/scoring/swe-effi.ts):
 *   {
 *     "instanceId": string,     // SWE-bench instance id
 *     "resolved": boolean,      // from grader resolved_ids
 *     "inputTokens": number,    // meta.telemetry.in_tokens  (real provider usage)
 *     "outputTokens": number,   // meta.telemetry.out_tokens (real provider usage)
 *     "turns": number,          // meta.telemetry.turns
 *     "breakages": number       // 1 when meta.rc !== 0, else 0
 *   }
 *
 * Fidelity gate:
 *   Only instances attempted by BOTH arms are included in the output. Instances
 *   present in only one arm are skipped (logged to stderr). This prevents
 *   spurious token-savings from asymmetric coverage.
 *
 * Zero dependencies — node builtins only.
 */

import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, join } from "node:path";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function die(msg) {
  process.stderr.write(`ERROR: ${msg}\n`);
  process.exit(1);
}

function log(msg) {
  process.stderr.write(`${msg}\n`);
}

/**
 * Load a JSONL file and return an array of parsed objects.
 * Each non-blank line is parsed as JSON independently.
 */
function loadJsonl(path) {
  if (!existsSync(path)) die(`file not found: ${path}`);
  return readFileSync(path, "utf-8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l, i) => {
      try {
        return JSON.parse(l);
      } catch (e) {
        die(`JSON parse error in ${path} line ${i + 1}: ${e.message}`);
      }
    });
}

/**
 * Load a SWE-bench grader result JSON and return a Set of resolved instance ids.
 *
 * The grader writes <model_name_or_path>.<run_id>.json with shape:
 *   { resolved_ids: string[], submitted_ids: string[], ... }
 * (schema_version 2 as of swebench ≥ 1.1)
 */
function loadGraderResolved(path) {
  if (!existsSync(path)) die(`grader result not found: ${path}`);
  let data;
  try {
    data = JSON.parse(readFileSync(path, "utf-8"));
  } catch (e) {
    die(`JSON parse error in ${path}: ${e.message}`);
  }
  if (!Array.isArray(data.resolved_ids)) {
    die(
      `grader file ${path} missing 'resolved_ids' array — ` +
        `expected SWE-bench schema_version 2 format`
    );
  }
  return new Set(data.resolved_ids);
}

/**
 * Index a JSONL row array by instance_id.
 * Warns and keeps the LAST row if duplicates exist.
 */
function indexByInstanceId(rows, label) {
  const idx = new Map();
  for (const row of rows) {
    if (!row.instance_id) {
      log(`WARN [${label}]: row missing instance_id — skipping`);
      continue;
    }
    if (idx.has(row.instance_id)) {
      log(
        `WARN [${label}]: duplicate instance_id ${row.instance_id} — keeping last`
      );
    }
    idx.set(row.instance_id, row);
  }
  return idx;
}

/**
 * Convert a single meta row + resolved verdict into a Trajectory object.
 *
 * Mapping (see Trajectory in e2e/common/scoring/swe-effi.ts):
 *   instanceId  <- meta.instance_id
 *   resolved    <- graderResolved.has(meta.instance_id)
 *   inputTokens <- meta.telemetry.in_tokens
 *   outputTokens<- meta.telemetry.out_tokens
 *   turns       <- meta.telemetry.turns
 *   breakages   <- 1 if meta.rc !== 0 else 0
 *
 * Note: we use in_tokens (total input tokens including cache hits) as the
 * token cost metric — this is what the provider charged for and matches the
 * SWE-Effi paper's "tokens used" definition. Cached input still consumes
 * cache-read credits (cheaper, but non-zero) and inflates per-turn context.
 * If you want cache-adjusted tokens, subtract meta.telemetry.cached_in.
 */
function toTrajectory(meta, graderResolved, arm) {
  const t = meta.telemetry;
  if (!t) {
    log(
      `WARN [${arm}]: ${meta.instance_id} has no telemetry — using 0 tokens/turns`
    );
  }
  return {
    instanceId: meta.instance_id,
    resolved: graderResolved.has(meta.instance_id),
    inputTokens: t ? (t.in_tokens ?? 0) : 0,
    outputTokens: t ? (t.out_tokens ?? 0) : 0,
    turns: t ? (t.turns ?? 0) : 0,
    breakages: meta.rc !== 0 ? 1 : 0,
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const [, , resultsDir, graderOnPath, graderOffPath, outDirArg] = process.argv;

if (!resultsDir || !graderOnPath || !graderOffPath) {
  process.stderr.write(
    `usage: node score.mjs <results-dir> <grader-on.json> <grader-off.json> [out-dir]\n`
  );
  process.exit(2);
}

const absResultsDir = resolve(resultsDir);
const absGraderOn = resolve(graderOnPath);
const absGraderOff = resolve(graderOffPath);
const outDir = resolve(outDirArg ?? resultsDir);

// Create output directory if needed
mkdirSync(outDir, { recursive: true });

log(`[score.mjs] results-dir : ${absResultsDir}`);
log(`[score.mjs] grader-on   : ${absGraderOn}`);
log(`[score.mjs] grader-off  : ${absGraderOff}`);
log(`[score.mjs] out-dir     : ${outDir}`);

// Load inputs
const metaOnRows = loadJsonl(join(absResultsDir, "meta_on.jsonl"));
const metaOffRows = loadJsonl(join(absResultsDir, "meta_off.jsonl"));
const resolvedOn = loadGraderResolved(absGraderOn);
const resolvedOff = loadGraderResolved(absGraderOff);

const onIdx = indexByInstanceId(metaOnRows, "on");
const offIdx = indexByInstanceId(metaOffRows, "off");

// Fidelity gate: only instances attempted by both arms
const onIds = new Set(onIdx.keys());
const offIds = new Set(offIdx.keys());
const bothIds = [...onIds].filter((id) => offIds.has(id));

const onOnly = [...onIds].filter((id) => !offIds.has(id));
const offOnly = [...offIds].filter((id) => !onIds.has(id));

if (onOnly.length > 0) {
  log(
    `[score.mjs] FIDELITY GATE: ${onOnly.length} instance(s) in ON only — excluded:`
  );
  for (const id of onOnly) log(`  - ${id}`);
}
if (offOnly.length > 0) {
  log(
    `[score.mjs] FIDELITY GATE: ${offOnly.length} instance(s) in OFF only — excluded:`
  );
  for (const id of offOnly) log(`  - ${id}`);
}

log(
  `[score.mjs] fidelity gate: ${bothIds.length} instances in both arms (excluded ${onOnly.length + offOnly.length})`
);

if (bothIds.length === 0) {
  die("No instances passed the fidelity gate — both arms have no overlap.");
}

// Build trajectory rows for each arm
const trajOn = bothIds.map((id) =>
  toTrajectory(onIdx.get(id), resolvedOn, "on")
);
const trajOff = bothIds.map((id) =>
  toTrajectory(offIdx.get(id), resolvedOff, "off")
);

// Write JSONL output files
const outOnPath = join(outDir, "arm-on.jsonl");
const outOffPath = join(outDir, "arm-off.jsonl");

writeFileSync(outOnPath, trajOn.map((r) => JSON.stringify(r)).join("\n") + "\n");
writeFileSync(outOffPath, trajOff.map((r) => JSON.stringify(r)).join("\n") + "\n");

log(`[score.mjs] wrote ${trajOn.length} rows -> ${outOnPath}`);
log(`[score.mjs] wrote ${trajOff.length} rows -> ${outOffPath}`);

// Print a summary of resolved counts for quick sanity check
const resolvedOnCount = trajOn.filter((r) => r.resolved).length;
const resolvedOffCount = trajOff.filter((r) => r.resolved).length;
log(
  `[score.mjs] resolved: on=${resolvedOnCount}/${trajOn.length}  off=${resolvedOffCount}/${trajOff.length}`
);
log(`[score.mjs] done — pass arm-on.jsonl and arm-off.jsonl to tsx e2e/common/scoring/run.ts`);
