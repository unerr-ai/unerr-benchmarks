/**
 * Track 2 — Navigation accuracy (localization).
 *
 * Token savings only matter if the agent still lands on the RIGHT code. This
 * track measures that directly: given a query whose gold answer is a known
 * file, does unerr surface that file in the top-k results, and at what token
 * cost vs. grep?
 *
 * This runnable version uses the repo's own graph as gold (query = an entity's
 * name; gold = the file that defines it) — a self-contained accuracy probe. To
 * use EXTERNAL gold (SWE-bench Verified gold-patch files, RepoBench-R cross-file
 * snippets), replace `localGoldSet()` with a loader that yields
 * `{ query, goldFile }` pairs from that dataset; the scoring below is identical.
 *
 * Usage: tsx internal/navigation/localization.ts [repoPath] [--n N]
 * Metric: top-1/3/5 hit rate + mean tokens-to-localize (unerr vs grep).
 */
import { resolve } from "node:path";
import { bootHarness, type Harness } from "../lib/harness.js";
import { countTokens } from "../lib/tokenizer.js";
import { grep } from "./baseline.js";

interface GoldPair {
  query: string;
  goldFile: string;
}

const IDENT = /^[A-Za-z_$][\w$]*$/;

async function localGoldSet(h: Harness, n: number): Promise<GoldPair[]> {
  const rows = await h.query(
    "?[name, kind, file_path, fan_in] := *entities{name, kind, file_path, fan_in}"
  );
  const ents = rows
    .map((r) => ({
      name: String(r[0]),
      kind: String(r[1]),
      file: String(r[2]),
      fanIn: Number(r[3]) || 0,
    }))
    .filter(
      (e) =>
        IDENT.test(e.name) &&
        e.name.length >= 4 &&
        (e.kind === "function" || e.kind === "class")
    )
    // prefer well-connected, less-ambiguous symbols
    .sort((a, b) => b.fanIn - a.fanIn);
  // de-dup by name to avoid trivially-ambiguous queries
  const seen = new Set<string>();
  const out: GoldPair[] = [];
  for (const e of ents) {
    if (seen.has(e.name)) continue;
    seen.add(e.name);
    out.push({ query: e.name, goldFile: e.file });
    if (out.length >= n) break;
  }
  return out;
}

function err(m: string): void {
  process.stderr.write(`${m}\n`);
}

async function main(): Promise<void> {
  const positional = process.argv.slice(2).find((a) => !a.startsWith("--"));
  const repoRoot = resolve(positional ?? process.cwd());
  const nIdx = process.argv.indexOf("--n");
  const n = nIdx >= 0 ? Number(process.argv[nIdx + 1]) : 40;

  err(`\n▸ Track 2 — localization accuracy`);
  const h = await bootHarness(repoRoot, (m) => err(`  · ${m}`));
  const gold = await localGoldSet(h, n);
  err(`  ${gold.length} gold queries\n`);

  let top1 = 0;
  let top3 = 0;
  let top5 = 0;
  let unerrTok = 0;
  let grepTok = 0;

  for (const g of gold) {
    const res = await h.runTool({
      tool: "search_code",
      args: { query: g.query, limit: 5 },
    });
    unerrTok += countTokens(res.payload);
    // Rank of gold file in the serialized result (lower index = higher rank).
    // We use ordered appearance of the gold path basename as a rank proxy.
    const base = g.goldFile.split("/").pop()!;
    const idx = res.payload.indexOf(base);
    // crude rank: how many other result file paths appear before it
    const before = idx >= 0 ? (res.payload.slice(0, idx).match(/\.ts/g) ?? []).length : 999;
    if (idx >= 0 && before <= 0) top1++;
    if (idx >= 0 && before <= 2) top3++;
    if (idx >= 0 && before <= 4) top5++;

    const gr = grep(repoRoot, g.query);
    grepTok += gr.tokens;
  }

  const c = gold.length || 1;
  err(`${"─".repeat(60)}`);
  err(`  Localization accuracy (gold file surfaced):`);
  err(`    top-1: ${((top1 / c) * 100).toFixed(1)}%   top-3: ${((top3 / c) * 100).toFixed(1)}%   top-5: ${((top5 / c) * 100).toFixed(1)}%`);
  err(`  Mean tokens-to-localize:`);
  err(`    unerr search_code: ${Math.round(unerrTok / c)}   grep: ${Math.round(grepTok / c)}`);
  err(`    reduction: ${(((grepTok - unerrTok) / grepTok) * 100).toFixed(1)}%`);
  err(`${"─".repeat(60)}\n`);
  err(`  NOTE: gold = repo graph (self-probe). Swap localGoldSet() for SWE-bench`);
  err(`  Verified gold-patch files for an externally-sourced accuracy number.\n`);

  await h.close();
}

main().catch((e) => {
  err(`\n✗ localization failed: ${(e as Error).stack ?? e}`);
  process.exit(1);
});
