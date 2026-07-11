/**
 * Task corpus — a FROZEN, serializable task set for reproducible results.
 *
 * The first run on a repo auto-derives tasks from the indexed graph and writes
 * a fixture (`fixtures/corpus-<repo>.json`). Every subsequent run loads that
 * fixture, so the published numbers are reproducible even though the indexer is
 * not bit-for-bit deterministic across runs. Tasks reference entities by stable
 * NAME (never by content-hash keys), passed in the `key` arg — which the
 * QueryRouter resolves to the current content-hash key via exact-name match
 * (`resolveKeyArg` accepts a name or a hex key in that field, and get_references
 * reads ONLY `key`) — so the calls survive re-indexing. Entity-resolving
 * families (get-entity,
 * find-callers) only pick entities whose name is GLOBALLY UNIQUE in the graph,
 * so name→key resolution is unambiguous and lands on exactly the entity whose
 * ground truth was frozen.
 *
 * Four task families map to the capability buckets from the tool audit:
 *   navigation : find-symbol, get-entity, find-callers   (the "~80%")
 *   compression: understand-file                          (the "~20%")
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Harness, ToolCall } from "../lib/harness.js";
import type { BaselineRecipe } from "./baseline.js";

export type Fidelity =
  | { kind: "all"; needles: string[] } // payload must contain every needle
  | { kind: "any"; needles: string[] } // payload must contain at least one
  | { kind: "majority"; needles: string[] }; // payload must contain ≥ half

/** Fully serializable — the whole corpus round-trips through JSON. */
export interface TaskSpec {
  id: string;
  bucket: "navigation" | "compression" | "prevention";
  category: string;
  question: string;
  unerr: ToolCall;
  baseline: BaselineRecipe;
  fidelity: Fidelity;
}

/** Apply a spec's fidelity check to an unerr payload. */
export function checkFidelity(spec: TaskSpec, payload: string): boolean | null {
  const { kind, needles } = spec.fidelity;
  if (needles.length === 0) return null;
  const hits = needles.filter((n) => payload.includes(n)).length;
  if (kind === "all") return hits === needles.length;
  if (kind === "any") return hits > 0;
  return hits >= Math.ceil(needles.length / 2);
}

interface Ent {
  key: string;
  name: string;
  kind: string;
  file_path: string;
  fan_in: number;
  start_line: number;
}

const IDENT = /^[A-Za-z_$][\w$]*$/;

// Kinds getCallersOf excludes (only callable kinds can be callers). The freeze
// must mirror this so the ground-truth caller set matches what the tool returns.
const CALLER_KINDS_EXCLUDED = new Set([
  "variable",
  "interface",
  "type",
  "enum",
  "namespace",
]);

async function loadEntities(h: Harness): Promise<Ent[]> {
  const rows = await h.query(
    "?[key, name, kind, file_path, fan_in, start_line] := *entities{key, name, kind, file_path, fan_in, start_line}"
  );
  return rows.map((r) => ({
    key: String(r[0]),
    name: String(r[1]),
    kind: String(r[2]),
    file_path: String(r[3]),
    fan_in: Number(r[4]) || 0,
    start_line: Number(r[5]) || 0,
  }));
}

async function callerKeysOf(h: Harness, key: string): Promise<string[]> {
  const rows = await h.query(
    '?[from_key] := *edges{from_key, to_key: $k, type: "calls"}',
    { k: key }
  );
  return rows.map((r) => String(r[0]));
}

/** Pick a spread across distinct files to avoid over-sampling one hot file. */
function spread<T extends { file_path: string }>(items: T[], n: number): T[] {
  const out: T[] = [];
  const seen = new Set<string>();
  for (const it of items) {
    if (seen.has(it.file_path)) continue;
    seen.add(it.file_path);
    out.push(it);
    if (out.length >= n) break;
  }
  for (const it of items) {
    if (out.length >= n) break;
    if (!out.includes(it)) out.push(it);
  }
  return out;
}

/** Auto-derive a corpus from the graph (deterministic ordering throughout). */
async function deriveCorpus(h: Harness, perCategory: number): Promise<TaskSpec[]> {
  const ents = await loadEntities(h);
  const byKey = new Map(ents.map((e) => [e.key, e]));
  // resolveKeyArg matches on name across ALL entities, so a name shared by more
  // than one entity is ambiguous: the tool may resolve to a different entity
  // than the one we froze ground truth for. Count names globally.
  const nameCounts = new Map<string, number>();
  for (const e of ents) {
    nameCounts.set(e.name, (nameCounts.get(e.name) ?? 0) + 1);
  }
  const uniqueName = (e: Ent): boolean => nameCounts.get(e.name) === 1;
  const named = ents.filter(
    (e) =>
      IDENT.test(e.name) &&
      e.name.length >= 3 &&
      (e.kind === "function" || e.kind === "class" || e.kind === "type")
  );
  const tasks: TaskSpec[] = [];

  // find-symbol — deterministic alphabetical selection, spread across files.
  const byName = [...named].sort((a, b) => a.name.localeCompare(b.name));
  for (const e of spread(byName.slice(0, perCategory * 3), perCategory)) {
    tasks.push({
      id: `find-symbol:${e.name}`,
      bucket: "navigation",
      category: "find-symbol",
      question: `Where is "${e.name}" defined?`,
      unerr: { tool: "search_code", args: { query: e.name } },
      baseline: { kind: "grep+read-top", pattern: e.name, readTopN: 1 },
      fidelity: { kind: "all", needles: [e.name, e.file_path.split("/").pop()!] },
    });
  }

  // get-entity — highest fan_in (key tiebreaker for stability), unique names
  // only so `name` resolves to exactly this entity.
  const byFanIn = [...named].sort(
    (a, b) => b.fan_in - a.fan_in || a.key.localeCompare(b.key)
  );
  const byFanInUnique = byFanIn.filter(uniqueName);
  for (const e of spread(byFanInUnique.slice(0, perCategory * 2), perCategory)) {
    tasks.push({
      id: `get-entity:${e.name}`,
      bucket: "navigation",
      category: "get-entity",
      question: `Show the definition of "${e.name}".`,
      unerr: { tool: "get_entity", args: { key: e.name } },
      baseline: { kind: "grep+read-top", pattern: e.name, readTopN: 1 },
      fidelity: { kind: "any", needles: [e.name] },
    });
  }

  // find-callers — ground-truth caller set + files pulled from `calls` edges.
  // Unique names only: a shared name would resolve to a different entity than
  // the one whose caller set we froze, breaking the fidelity check.
  const highFanIn = byFanInUnique.filter((e) => e.fan_in >= 2);
  for (const e of spread(highFanIn.slice(0, perCategory * 3), perCategory)) {
    const callers = await callerKeysOf(h, e.key);
    if (callers.length === 0) continue;
    // Mirror getCallersOf's kind filter so the ground-truth set matches exactly
    // the callers get_references can return — and keep ALL of them (including
    // test functions like "it: ...", which are real callers the tool returns
    // first in its key-sorted, capped view). An earlier IDENT-only filter
    // dropped every test caller, leaving names that the tool truncated away.
    const callerEnts = callers
      .map((kk) => byKey.get(kk))
      .filter((c): c is Ent => Boolean(c) && !CALLER_KINDS_EXCLUDED.has(c.kind));
    const callerFiles = [...new Set(callerEnts.map((c) => c.file_path))]
      .sort()
      .slice(0, 5); // conservative: agent reads at most 5 files to verify
    const callerNames = [
      ...new Set(
        callerEnts
          .map((c) => c.name)
          // Drop names that can't be substring-matched in the columnar payload
          // (the `|` column separator / newlines would break includes()).
          .filter((n) => Boolean(n) && !n.includes("|") && !n.includes("\n"))
      ),
    ].sort();
    tasks.push({
      id: `find-callers:${e.name}`,
      bucket: "navigation",
      category: "find-callers",
      question: `Who calls "${e.name}"? (${callers.length} known callers)`,
      unerr: {
        tool: "get_references",
        args: { key: e.name, direction: "callers" },
      },
      baseline: { kind: "grep+read-files", pattern: e.name, files: callerFiles },
      fidelity: { kind: "any", needles: callerNames },
    });
  }

  // understand-file — files with a moderate entity count (size tiebreaker).
  const fileEntities = new Map<string, Ent[]>();
  for (const e of ents) {
    const arr = fileEntities.get(e.file_path) ?? [];
    arr.push(e);
    fileEntities.set(e.file_path, arr);
  }
  const candidateFiles = [...fileEntities.entries()]
    .filter(([, es]) => es.length >= 4 && es.length <= 60)
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  for (const [file, es] of candidateFiles.slice(0, perCategory)) {
    // file_outline emits entities in START-LINE order and caps the view, so the
    // needles must be the EARLIEST symbols by line — otherwise the check demands
    // names the tool legitimately truncated. Take the first 8 distinct names by
    // line; these are always inside the returned (capped) view.
    const seen = new Set<string>();
    const topNames: string[] = [];
    for (const e of [...es]
      .filter((e) => IDENT.test(e.name))
      .sort((a, b) => a.start_line - b.start_line || a.name.localeCompare(b.name))) {
      if (seen.has(e.name)) continue;
      seen.add(e.name);
      topNames.push(e.name);
      if (topNames.length >= 8) break;
    }
    tasks.push({
      id: `understand-file:${file}`,
      bucket: "compression",
      category: "understand-file",
      question: `What are the main functions/classes in ${file}? (${es.length} entities)`,
      unerr: { tool: "file_outline", args: { file_path: file } },
      baseline: { kind: "read-file", file },
      fidelity: { kind: "majority", needles: topNames },
    });
  }

  // imports — direct-dependency questions. This is a GRAPH tool's home turf
  // (graphify's `imports_from` edges, unerr's file→file `imports` edges), added
  // so the corpus is not exclusively symbol-retrieval (unerr-favourable).
  //
  // The ground truth is parsed from the FILE'S OWN SOURCE — the stems of its
  // RELATIVE import specifiers — NOT from any tool's graph. (Edge-derived needles
  // were both circular — unerr graded against its own edges — and noisy: unerr's
  // file→file edges over-attribute through barrel re-exports, so a test file that
  // only `require('../index.js')` was credited with importing every lib module.)
  // A relative-specifier stem is a tool-neutral fact: it appears verbatim in F's
  // source (`require('./argument.js')`, `from .command import X`), in unerr's
  // resolved import rows, and in a graph engine's import edges alike. We exclude
  // the bare `index` barrel stem (its re-export fan-out is the very ambiguity that
  // makes resolution tool-specific) and require ≥2 distinct named-file imports.
  const stemFromSpec = (spec: string): string | null => {
    const seg = spec.includes("/")
      ? (spec.split("/").pop() ?? "")
      : (spec.replace(/^\.+/, "").split(".").pop() ?? ""); // python ".command" → "command"
    const stem = seg.replace(/\.[A-Za-z0-9]+$/, "");
    return stem.length >= 4 && stem !== "index" ? stem : null;
  };
  const REL_JS = /(?:from|require\s*\(\s*)["'](\.[^"']+)["']/g; // JS/TS relative
  const REL_PY = /^\s*from\s+(\.[.\w]*)\s+import\b/gm; // Python relative
  const localImportStems = (file: string): string[] => {
    const abs = file.startsWith("/") ? file : resolve(h.repoRoot, file);
    let src = "";
    try {
      src = readFileSync(abs, "utf-8");
    } catch {
      return [];
    }
    const stems = new Set<string>();
    for (const m of src.matchAll(REL_JS)) {
      const s = stemFromSpec(m[1]);
      if (s) stems.add(s);
    }
    for (const m of src.matchAll(REL_PY)) {
      const s = stemFromSpec(m[1]);
      if (s) stems.add(s);
    }
    return [...stems].sort();
  };
  const importCandidates = [...fileEntities.keys()]
    .map((file) => ({ file, stems: localImportStems(file) }))
    .filter((c) => c.stems.length >= 2)
    .sort((a, b) => b.stems.length - a.stems.length || a.file.localeCompare(b.file));
  for (const { file, stems } of importCandidates.slice(0, perCategory)) {
    tasks.push({
      id: `imports:${file}`,
      bucket: "navigation",
      category: "imports",
      question: `What files does ${file} import? (direct dependencies)`,
      unerr: { tool: "get_imports", args: { file_path: file } },
      baseline: { kind: "read-file", file },
      fidelity: { kind: "majority", needles: stems.slice(0, 8) },
    });
  }

  return tasks;
}

function fixturePathFor(repoBasename: string): string {
  const here = dirname(fileURLToPath(import.meta.url));
  return resolve(here, "fixtures", `corpus-${repoBasename}.json`);
}

/**
 * Load the frozen corpus for a repo, or derive + freeze it on first run.
 * Pass `refresh: true` to regenerate the fixture.
 */
export async function loadOrFreezeCorpus(
  h: Harness,
  repoBasename: string,
  perCategory: number,
  refresh = false
): Promise<{ tasks: TaskSpec[]; fromFixture: boolean; fixturePath: string }> {
  const fixturePath = fixturePathFor(repoBasename);
  if (!refresh && existsSync(fixturePath)) {
    const tasks = JSON.parse(readFileSync(fixturePath, "utf-8")) as TaskSpec[];
    return { tasks, fromFixture: true, fixturePath };
  }
  const tasks = await deriveCorpus(h, perCategory);
  mkdirSync(dirname(fixturePath), { recursive: true });
  writeFileSync(fixturePath, JSON.stringify(tasks, null, 2));
  return { tasks, fromFixture: false, fixturePath };
}
