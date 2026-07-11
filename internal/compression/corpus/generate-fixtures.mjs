#!/usr/bin/env node
/**
 * Frozen-corpus generator (S0/T0.3).
 *
 * Writes the deterministic fixture files for the compression harness and a
 * hash-pinned manifest.json. Re-running this script reproduces byte-identical
 * fixtures (no clock, no randomness — a seeded LCG drives all "noise"), so the
 * SHA-256 in the manifest stays stable. The harness verifies each fixture's
 * hash before running, so a fixture can never silently drift.
 *
 * Run:  node internal/compression/corpus/generate-fixtures.mjs
 * It prints nothing to stdout that a pipe consumer would choke on; this is a
 * standalone script (not proxy code), so normal console output is fine.
 */
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = dirname(fileURLToPath(import.meta.url));

// Deterministic pseudo-random — seeded LCG. NO Math.random, NO Date.now, so
// fixtures are byte-identical across machines and runs.
function makeRng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0x100000000;
  };
}
const pick = (rng, arr) => arr[Math.floor(rng() * arr.length)];

// ── Category 1: large shell outputs ──────────────────────────────────
function buildLog(rng, lines, fatalAt, fatalText) {
  const levels = ["INFO", "DEBUG", "WARN", "TRACE"];
  const mods = ["server", "router", "graph", "indexer", "proxy", "cache"];
  const out = [];
  for (let i = 0; i < lines; i++) {
    if (i === fatalAt) {
      out.push(fatalText);
      continue;
    }
    const lvl = pick(rng, levels);
    const mod = pick(rng, mods);
    out.push(
      `2026-06-13T10:${String(i % 60).padStart(2, "0")}:00 [${lvl}] ${mod}: step ${i} ok handler=${Math.floor(rng() * 9999)}`
    );
  }
  return out.join("\n");
}

function buildDiff(rng, files) {
  const out = [];
  for (let f = 0; f < files; f++) {
    const path = `src/module_${f}/component_${f}.ts`;
    out.push(`diff --git a/${path} b/${path}`);
    out.push(`index ${Math.floor(rng() * 0xffffff).toString(16)}..${Math.floor(rng() * 0xffffff).toString(16)} 100644`);
    out.push(`--- a/${path}`);
    out.push(`+++ b/${path}`);
    const hunks = 4 + Math.floor(rng() * 6);
    for (let h = 0; h < hunks; h++) {
      const start = 1 + h * 20;
      out.push(`@@ -${start},8 +${start},9 @@ function handler_${f}_${h}() {`);
      for (let l = 0; l < 8; l++) {
        const mark = rng() < 0.3 ? "+" : rng() < 0.3 ? "-" : " ";
        out.push(`${mark}  const value_${l} = compute(${Math.floor(rng() * 100)});`);
      }
    }
  }
  return out.join("\n");
}

function buildKubectlYaml(rng, pods) {
  const out = ["apiVersion: v1", "items:"];
  for (let p = 0; p < pods; p++) {
    out.push("- apiVersion: v1");
    out.push("  kind: Pod");
    out.push("  metadata:");
    out.push(`    name: service-${p}-${Math.floor(rng() * 99999)}`);
    out.push("    namespace: production");
    out.push("    labels:");
    out.push(`      app: service-${p}`);
    out.push(`      tier: ${pick(rng, ["frontend", "backend", "cache"])}`);
    out.push("  spec:");
    out.push("    containers:");
    out.push(`    - name: main`);
    out.push(`      image: registry.example.com/service-${p}:v${Math.floor(rng() * 9)}`);
    out.push("      resources:");
    out.push("        requests:");
    out.push(`          cpu: "${250 + Math.floor(rng() * 750)}m"`);
    out.push(`          memory: "${128 + Math.floor(rng() * 512)}Mi"`);
    out.push("  status:");
    out.push(`    phase: ${pick(rng, ["Running", "Pending"])}`);
    out.push(`    podIP: 10.${Math.floor(rng() * 255)}.${Math.floor(rng() * 255)}.${Math.floor(rng() * 255)}`);
  }
  return out.join("\n");
}

// ── Category 2: large file / entity reads (code) ─────────────────────
function buildSourceFile(rng, fns, targetFn, targetBody) {
  const out = [
    "import { join } from 'node:path';",
    "import { readFileSync } from 'node:fs';",
    "import { estimateTokenCount } from '../intelligence/token-estimator.js';",
    "",
  ];
  for (let i = 0; i < fns; i++) {
    if (i === targetFn) {
      out.push(targetBody);
      out.push("");
      continue;
    }
    out.push(`/** Helper number ${i}. */`);
    out.push(`export function helper_${i}(x: number, y: number): number {`);
    out.push(`  const acc = x * ${Math.floor(rng() * 10) + 1} + y;`);
    out.push(`  const scaled = acc / ${Math.floor(rng() * 5) + 1};`);
    out.push(`  return Math.floor(scaled) + ${Math.floor(rng() * 100)};`);
    out.push("}");
    out.push("");
  }
  return out.join("\n");
}

// ── Category 3: large JSON tool responses ────────────────────────────
function buildSearchJson(rng, n, targetKey, targetName) {
  const results = [];
  for (let i = 0; i < n; i++) {
    const isTarget = i === Math.floor(n / 2);
    results.push({
      key: isTarget ? targetKey : `entity_${i}_${Math.floor(rng() * 99999).toString(16)}`,
      name: isTarget ? targetName : `symbol_${i}`,
      kind: pick(rng, ["function", "class", "interface", "type"]),
      file_path: `src/area_${i % 12}/file_${i}.ts`,
      fan_in: Math.floor(rng() * 40),
      fan_out: Math.floor(rng() * 20),
      risk_level: pick(rng, ["low", "medium", "high"]),
      summary: `Handles ${pick(rng, ["routing", "parsing", "caching", "indexing"])} for ${pick(rng, ["entities", "edges", "tokens", "files"])} in subsystem ${i}.`,
    });
  }
  return JSON.stringify({ results, total: n }, null, 2);
}

// ── Fixture definitions ──────────────────────────────────────────────
const fixtures = [];

function add(category, id, mustSurvive, task, content) {
  const relPath = join(category, `${id}.txt`);
  const abs = join(ROOT, relPath);
  mkdirSync(dirname(abs), { recursive: true });
  writeFileSync(abs, content, "utf8");
  const sha256 = createHash("sha256").update(content, "utf8").digest("hex");
  fixtures.push({
    id,
    category,
    path: relPath,
    bytes: Buffer.byteLength(content, "utf8"),
    sha256,
    mustSurvive,
    // The intended "current task" string an agent would carry when this
    // payload arrives — feeds the S7 query-aware compressors (wire-cap
    // relevance ordering, compressLogText error ranking, chunk ranking). The
    // task names the thing the agent is looking for, never the answer verbatim.
    task,
  });
}

// shell (n=5)
add(
  "shell",
  "build-log-fatal",
  "FATAL: type error TS2345 in src/proxy/wire-cap\\.ts",
  "why did the type check fail",
  buildLog(makeRng(101), 1200, 947, "2026-06-13T10:47:00 [ERROR] tsc: FATAL: type error TS2345 in src/proxy/wire-cap.ts(88,12)")
);
add(
  "shell",
  "build-log-eslint",
  "FATAL: 1 error \\(no-unused-vars\\) in src/intelligence/recon\\.ts",
  "which lint error broke the build",
  buildLog(makeRng(102), 900, 612, "2026-06-13T10:10:00 [ERROR] eslint: FATAL: 1 error (no-unused-vars) in src/intelligence/recon.ts:204")
);
add(
  "shell",
  "git-diff-large",
  "diff --git a/src/module_0/component_0\\.ts",
  "what changed in module_0 component_0",
  buildDiff(makeRng(103), 14)
);
add(
  "shell",
  "kubectl-yaml",
  "kind: Pod",
  "list the pods and their kind",
  buildKubectlYaml(makeRng(104), 30)
);
add(
  "shell",
  "test-results-fail",
  "FAIL src/__tests__/fleet-reporter\\.test\\.ts > reports machine snapshot",
  "which test failed in the fleet reporter suite",
  buildLog(makeRng(105), 700, 488, "2026-06-13T10:08:00 [ERROR] vitest: FAIL src/__tests__/fleet-reporter.test.ts > reports machine snapshot")
);

// file-read (n=5)
for (let i = 0; i < 5; i++) {
  const targetName = `criticalHandler_${i}`;
  const body = [
    `/** The one entity a query for ${targetName} must surface. */`,
    `export async function ${targetName}(input: Request): Promise<Response> {`,
    `  const parsed = parseRequest(input);`,
    `  if (!parsed.ok) throw new Error('bad request ${i}');`,
    `  return dispatch(parsed.value, ${i});`,
    "}",
  ].join("\n");
  add(
    "file-read",
    `source-file-${i}`,
    `export async function ${targetName}\\(`,
    `find the ${targetName} request handler`,
    buildSourceFile(makeRng(200 + i), 80, 40, body)
  );
}

// json (n=5)
for (let i = 0; i < 5; i++) {
  const targetKey = `tgt_${i}_deadbeef`;
  const targetName = `answerEntity_${i}`;
  add(
    "json",
    `search-response-${i}`,
    // Whitespace-tolerant: the answer entity must survive whether the wire
    // renders compact (JSON.stringify) or pretty-printed JSON.
    `"name":\\s*"${targetName}"`,
    `locate the ${targetName} entity in the results`,
    buildSearchJson(makeRng(300 + i), 120, targetKey, targetName)
  );
}

// ── Manifest ─────────────────────────────────────────────────────────
const manifest = {
  schema: 1,
  description:
    "Frozen compression corpus (S0/T0.3-T0.4). Hash-pinned fixtures across 3 categories, n>=5 each. mustSurvive is a JS RegExp source the fidelity probe (T0.4) checks against the compressed output.",
  generator: "generate-fixtures.mjs",
  categories: {
    shell: "large shell outputs (build logs, diffs, kubectl yaml, test results)",
    "file-read": "large code file / entity reads",
    json: "large JSON tool responses (search_code-style result arrays)",
  },
  fixtures,
};
writeFileSync(
  join(ROOT, "manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
  "utf8"
);

console.log(`Wrote ${fixtures.length} fixtures + manifest.json`);
for (const f of fixtures) {
  console.log(`  ${f.category}/${f.id}  ${f.bytes}B  ${f.sha256.slice(0, 12)}`);
}
