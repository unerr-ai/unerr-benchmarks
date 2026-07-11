/**
 * Head-to-head report — one row per arm, scored on the SAME corpus, tokenizer,
 * and fidelity gate. The honest headline is NOT "fewest tokens" — it is "fewest
 * tokens WITH the answer intact", so every token column is paired with a
 * fidelity column. An arm that drops tokens by losing the answer is not winning.
 */

export interface ArmTaskResult {
  arm: string;
  category: string;
  baselineTokens: number;
  armTokens: number;
  fidelity: boolean | null;
  error?: string;
}

export interface ArmRollup {
  arm: string;
  available: boolean;
  /** Tasks where the arm returned the answer (fidelity !== false). */
  validTasks: number;
  totalTasks: number;
  /** Tokens summed over valid tasks. */
  armTokens: number;
  /** Baseline tokens summed over the SAME valid tasks. */
  baselineTokens: number;
  pctReduction: number;
  fidelityChecked: number;
  fidelityPassed: number;
  /** Per-category token totals (valid tasks only). */
  byCategory: Record<
    string,
    { armTokens: number; baselineTokens: number; pass: number; valid: number; total: number }
  >;
}

function k(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(Math.round(n));
}
function pct(n: number): string {
  return `${n.toFixed(1)}%`;
}

export function rollupArm(
  arm: string,
  available: boolean,
  results: ArmTaskResult[]
): ArmRollup {
  const mine = results.filter((r) => r.arm === arm);
  const valid = mine.filter((r) => r.fidelity !== false);
  const armTokens = valid.reduce((a, r) => a + r.armTokens, 0);
  const baselineTokens = valid.reduce((a, r) => a + r.baselineTokens, 0);
  const byCategory: ArmRollup["byCategory"] = {};
  for (const r of mine) {
    const c = (byCategory[r.category] ??= {
      armTokens: 0,
      baselineTokens: 0,
      pass: 0,
      valid: 0,
      total: 0,
    });
    c.total++;
    if (r.fidelity === true) c.pass++;
    if (r.fidelity !== false) {
      c.valid++;
      c.armTokens += r.armTokens;
      c.baselineTokens += r.baselineTokens;
    }
  }
  return {
    arm,
    available,
    validTasks: valid.length,
    totalTasks: mine.length,
    armTokens,
    baselineTokens,
    pctReduction: baselineTokens > 0 ? ((baselineTokens - armTokens) / baselineTokens) * 100 : 0,
    fidelityChecked: mine.filter((r) => r.fidelity !== null).length,
    fidelityPassed: mine.filter((r) => r.fidelity === true).length,
    byCategory,
  };
}

export interface H2HMeta {
  repo: string;
  commit?: string;
  date?: string;
  entities: number;
  edges: number;
  encoding: string;
  language?: string;
  /** Total source tokens (one pass over indexed code files) — graphify's LLM build input. */
  codebaseTokens?: number;
  versions: Record<string, string>;
  unavailable: { arm: string; reason: string; install: string }[];
}

const ARM_LABEL: Record<string, string> = {
  unerr: "unerr",
  graphify: "graphify",
  rtk: "RTK",
  baseline: "naive (grep+read)",
};

/**
 * Real-world usage weights per task category, mined from unerr's own shadow
 * ledger (1,146 navigation tool-calls across this codebase's agent sessions):
 *   file_read+file_outline → understand-file, search_code → find-symbol,
 *   get_entity → get-entity, get_references → find-callers, get_imports → imports.
 * These weight the per-category results into a single "expected cost per
 * navigation operation in a realistic session" number — because treating a 1%
 * operation (imports) and a 51% operation (understand-file) as equal would
 * misrepresent real impact. CAVEAT: this distribution is from sessions that ran
 * unerr on this repo, so it is a proxy, not a universal constant.
 */
const USAGE_WEIGHTS: Record<string, number> = {
  "understand-file": 0.513,
  "find-symbol": 0.299,
  "get-entity": 0.105,
  "find-callers": 0.072,
  imports: 0.01,
};

/**
 * Expected token cost PER navigation operation, weighted by real usage, where an
 * operation the arm cannot answer (0 valid tasks in that category, or the
 * unanswered fraction of a partially-answered category) falls back to the naive
 * grep+read cost — because that is exactly what the agent must do when the tool
 * fails. Rewards terseness AND coverage. Returns null if no baseline reference.
 */
function weightedCostPerOp(
  arm: ArmRollup,
  baseline: ArmRollup,
  categories: string[]
): number | null {
  let wSum = 0;
  let cost = 0;
  for (const cat of categories) {
    const w = USAGE_WEIGHTS[cat];
    const b = baseline.byCategory[cat];
    if (w === undefined || !b || b.valid === 0) continue; // no naive reference
    const naiveAvg = b.armTokens / b.valid;
    const a = arm.byCategory[cat];
    const total = a?.total ?? 0;
    if (total === 0) continue;
    const answeredTokens = a?.armTokens ?? 0;
    const unanswered = total - (a?.valid ?? 0);
    const effPerOp = (answeredTokens + unanswered * naiveAvg) / total;
    cost += w * effPerOp;
    wSum += w;
  }
  return wSum > 0 ? cost / wSum : null;
}

export function renderH2H(
  rollups: ArmRollup[],
  categories: string[],
  meta: H2HMeta
): string {
  const L: string[] = [];
  L.push(`# unerr Benchmark — Track 1.5 (head-to-head)`);
  L.push("");
  L.push(
    `Repo \`${meta.repo}\`${meta.commit ? ` @ \`${meta.commit}\`` : ""}${meta.date ? ` · ${meta.date}` : ""}`
  );
  L.push(
    `${meta.language ? `${meta.language} · ` : ""}${meta.entities} entities, ${meta.edges} edges · tokenizer \`${meta.encoding}\``
  );
  L.push("");
  L.push(
    `Every arm answered the **same** corpus of code questions; outputs were scored with the **same** \`${meta.encoding}\` tokenizer and the **same** fidelity gate (did the output still contain the answer?). Tokens are compared only over tasks the arm answered correctly — dropping tokens by losing the answer is not a win.`
  );
  L.push("");

  // ── Headline comparison ─────────────────────────────────────────────────
  L.push(`## Arms compared`);
  L.push("");
  L.push(
    `| Arm | Tasks answered | Fidelity | Tokens (valid) | vs naive baseline |`
  );
  L.push(`|---|--:|--:|--:|--:|`);
  // Sort: baseline first (reference), then by pctReduction desc among same-tier.
  const ordered = [...rollups].sort((a, b) => {
    if (a.arm === "baseline") return -1;
    if (b.arm === "baseline") return 1;
    return b.pctReduction - a.pctReduction;
  });
  for (const r of ordered) {
    if (!r.available) {
      L.push(`| ${ARM_LABEL[r.arm] ?? r.arm} | _not installed_ | — | — | — |`);
      continue;
    }
    const fid =
      r.fidelityChecked > 0
        ? `${r.fidelityPassed}/${r.fidelityChecked}`
        : "—";
    // A savings % is only meaningful if the arm actually answered something.
    // An arm that passed 0 fidelity checks "saved" tokens by returning the
    // wrong answer — that is not a reduction, so we suppress the number.
    const answeredNothing = r.arm !== "baseline" && r.fidelityPassed === 0;
    const vs = r.arm === "baseline"
      ? "— (reference)"
      : answeredNothing
        ? `n/a — answered 0/${r.fidelityChecked}`
        : `−${pct(r.pctReduction)}`;
    L.push(
      `| ${ARM_LABEL[r.arm] ?? r.arm} | ${r.validTasks}/${r.totalTasks} | ${fid} | ${k(r.armTokens)} | ${vs} |`
    );
  }
  L.push("");
  L.push(
    `> "vs naive baseline" = token reduction versus raw grep+read **on the same tasks the arm answered correctly**. Fidelity \`p/c\` = passed/checked against the repo's actual symbol & caller facts. An arm that passed 0 checks shows **no** reduction — returning the wrong answer in few tokens is not a saving.`
  );
  L.push("");
  L.push(
    `> ⚠️ **This table is not the bottom line.** Each arm's % is computed over the *different subset of tasks it personally answered*, so a tool can post a higher % by answering fewer, easier questions. For the apples-to-apples number — every category weighted by how often agents actually use it, with misses charged at their real fallback cost — see **[Expected cost per operation](#expected-cost-per-operation-weighted-by-real-usage)** below.`
  );
  L.push("");

  // ── Per-category tokens (lower = better, but read alongside fidelity) ─────
  L.push(`## Tokens by task category`);
  L.push("");
  const avail = ordered.filter((r) => r.available);
  L.push(`| Category | ${avail.map((r) => ARM_LABEL[r.arm] ?? r.arm).join(" | ")} |`);
  L.push(`|---|${avail.map(() => "--:").join("|")}|`);
  for (const cat of categories) {
    const cells = avail.map((r) => {
      const c = r.byCategory[cat];
      if (!c || c.total === 0) return "—";
      return `${k(c.armTokens)} (${c.pass}/${c.total})`;
    });
    L.push(`| ${cat} | ${cells.join(" | ")} |`);
  }
  L.push("");
  L.push(`> Cells show \`tokens (fidelity pass/total)\` for that category.`);
  L.push("");

  // ── Usage-weighted expected cost per operation ───────────────────────────
  const baseRollup = rollups.find((r) => r.arm === "baseline");
  if (baseRollup) {
    const baseW = weightedCostPerOp(baseRollup, baseRollup, categories);
    L.push(`## Expected cost per operation (weighted by real usage)`);
    L.push("");
    L.push(
      `Per-category tokens above treat every category equally — but agents do not use them equally. Weighting each category by its real share of navigation tool-calls (mined from unerr's shadow ledger: understand-file 51%, find-symbol 30%, get-entity 11%, find-callers 7%, imports 1%) gives the **expected token cost of one navigation operation in a realistic session**. Operations a tool *cannot* answer fall back to naive grep+read — the cost the agent actually pays when the tool misses — so this rewards coverage as well as terseness.`
    );
    L.push("");
    L.push(`| Arm | Expected tokens / op | vs naive baseline |`);
    L.push(`|---|--:|--:|`);
    for (const r of ordered) {
      if (!r.available) {
        L.push(`| ${ARM_LABEL[r.arm] ?? r.arm} | _not installed_ | — |`);
        continue;
      }
      const w = weightedCostPerOp(r, baseRollup, categories);
      if (w === null) {
        L.push(`| ${ARM_LABEL[r.arm] ?? r.arm} | — | — |`);
        continue;
      }
      const vs =
        r.arm === "baseline"
          ? "— (reference)"
          : baseW && baseW > 0
            ? `−${pct(((baseW - w) / baseW) * 100)}`
            : "—";
      L.push(`| ${ARM_LABEL[r.arm] ?? r.arm} | ${k(w)} | ${vs} |`);
    }
    L.push("");
    L.push(
      `> Lower is better. This single number is the honest aggregate: it folds in how often each operation is used AND penalises tools for the operations they cannot answer (which revert to the naive cost).`
    );
    L.push("");
  }

  // ── Build cost: the tokens a tool spends BEFORE answering anything ────────
  if (meta.codebaseTokens && meta.codebaseTokens > 0) {
    const ct = meta.codebaseTokens;
    L.push(`## Build cost — the tokens spent before any query`);
    L.push("");
    L.push(
      `Query tokens are not the whole bill. A graph has to be built first, and how it is built differs by tool:`
    );
    L.push("");
    L.push(`| Tool | Graph build | LLM tokens to build |`);
    L.push(`|---|---|--:|`);
    L.push(
      `| unerr | local AST (tree-sitter) + CozoDB, no model calls | **0** |`
    );
    L.push(
      `| graphify (\`update\`, AST-only) | local AST (tree-sitter), no model calls | **0** |`
    );
    L.push(
      `| graphify (\`/graphify .\`, full) | local AST **+ semantic LLM pass over every file** | **≈ ${k(ct)}** input |`
    );
    L.push(`| RTK | none (stateless command compressor) | **0** |`);
    L.push("");
    L.push(
      `graphify's headline experience is \`/graphify .\` invoked **inside your AI assistant**, which runs the semantic extraction **on your assistant's own model session** (per graphify's README: "the model API is provided by your IDE session"). For this repo that semantic pass sends ≈ **${k(ct)} tokens** of source through the model — spent from the very budget this benchmark measures, *before the first question is answered*. unerr's graph is built with **zero** model tokens. (The AST-only \`graphify update\` mode used for the per-query numbers above skips this pass — so graphify's query columns are measured in its *cheapest-to-build* mode; its richest mode costs the ≈${k(ct)} above.)`
    );
    L.push("");
  }

  // ── How to read this ─────────────────────────────────────────────────────
  L.push(`## How to read this`);
  L.push("");
  L.push(
    `- **Same tier (unerr vs graphify):** both are graph-backed code engines. graphify is driven through its own \`explain "<node>"\` command (node-and-neighbours lookup) — its native interface, not the brittle NL \`query\` router — so it gets its honest best shot per task.`
  );
  L.push(
    `- **graphify returns node *metadata*, unerr returns the *definition*.** On \`get-entity\`, graphify's \`explain\` reports where a symbol's node lives plus its neighbours; unerr (and the baseline) return the actual definition body. Both contain the symbol name, so both pass the gate — but graphify's lower token count there reflects returning *less information*, not a tighter answer. Read its \`get-entity\` tokens as "located it", not "delivered the code".`
  );
  L.push(
    `- **graphify has no call edges.** Its model is \`imports_from\` / \`contains\`, so "who calls X" (\`find-callers\`) has no answer in its graph — hence its 0 there. The mirror image: \`imports\` is its home turf, where it matches or beats unerr.`
  );
  L.push(
    `- **Different tier (RTK):** RTK is a command-output compressor, not a code-navigation engine. On this corpus it runs the naive commands compressed, so it shows the *compression-only* ceiling — useful as a tier reference, not a like-for-like rival.`
  );
  L.push(
    `- **Fidelity is the gate.** A low token count with low fidelity means the tool answered a different (smaller) question. Read every token number next to its \`pass/total\`. An arm that passes **0** checks in a category did not "win on tokens" — it returned an answer this corpus could not confirm.`
  );
  L.push(
    `- **Each arm's headline % is over the tasks THAT arm passed** — different arms pass different subsets, so the % column is not a common-denominator race. The fair cross-arm view is the per-category \`tokens (pass/total)\` matrix above.`
  );
  L.push("");

  // ── What this corpus measures (scope honesty) ────────────────────────────
  L.push(`## What this corpus measures — and what it doesn't`);
  L.push("");
  L.push(
    `This corpus probes the navigation work a coding agent does most: locating a symbol's definition (\`find-symbol\`), reading its body (\`get-entity\`), finding its callers (\`find-callers\`), outlining a file (\`understand-file\`), and listing a file's direct dependencies (\`imports\`). The last is deliberately included as a **graph engine's home turf** — graphify's \`imports_from\` edges are its core competency — so the corpus is not stacked toward unerr's strengths.`
  );
  L.push(
    `It does **not** probe PR-impact, triage, or "the why" (design-rationale / docstring) questions that graphify's full semantic graph also targets. An arm that scores low on a category here may score well on its own question class; this benchmark does not claim otherwise.`
  );
  L.push(
    `Needles are tool-neutral repository facts — exact symbol name, file basename, caller function names, and import-target stems parsed from the file's **own source** (not from any tool's graph) — checked by plain substring containment. Any tool that surfaces the fact in any format passes; an arm scores 0 in a category only when its output never contains the fact, not because of formatting.`
  );
  L.push("");

  // ── Provenance ───────────────────────────────────────────────────────────
  L.push(`## Provenance`);
  L.push("");
  for (const [tool, v] of Object.entries(meta.versions)) {
    L.push(`- ${tool}: \`${v}\``);
  }
  if (meta.unavailable.length > 0) {
    L.push("");
    L.push(`Arms not run (install to include):`);
    for (const u of meta.unavailable) {
      L.push(`- **${ARM_LABEL[u.arm] ?? u.arm}** — ${u.reason}. Install: \`${u.install}\``);
    }
  }
  L.push("");

  // ── Fairness ─────────────────────────────────────────────────────────────
  L.push(`## Fairness notes`);
  L.push("");
  L.push(
    `- The task corpus (which symbols, which caller sets) is derived from the repo's own AST via unerr's indexer, but the ground-truth needles (symbol names, caller function names, file basenames) are **tool-neutral facts about the repository** — any tool either surfaces them or does not.`
  );
  L.push(
    `- The baseline is deliberately conservative (grep with source-only excludes, read only top-N files), which UNDER-states the naive cost and therefore under-states every arm's reduction.`
  );
  L.push(
    `- unerr is measured at the QueryRouter output, before the stdio wire-cap that only shrinks payloads further — a conservative measurement for unerr.`
  );
  L.push(
    `- graphify is queried against a graph it built itself (\`graphify update\`, its key-free AST mode) via its own \`explain "<node>"\` lookup — its native happy path for node-anchored questions. Its full \`/graphify .\` mode adds a semantic LLM pass (see *Build cost*); that pass runs on the agent's own model session and was not reproducible headlessly here, so the per-query columns reflect graphify's AST-mode output. RTK runs its own filters.`
  );
  L.push("");
  return L.join("\n");
}
