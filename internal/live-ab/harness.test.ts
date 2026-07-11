/**
 * Pure-logic tests for the Track 4 single-repo A/B harness. Covers the parts that
 * run without a real agent: arm-isolation arg wiring, `claude -p` JSON parsing,
 * the metrics-window reader (against a temp sqlite), and the four-pillar scoring
 * (guardrail saves, memory carry-over, savings comparison). The agent run itself
 * is the operator's paid step and is exercised via `run.ts --dry-run`, not here.
 *
 * Colocated with the harness under benchmarks/ (outside the src-only tsconfig
 * rootDir); vitest's include glob is extended to pick up benchmark test files.
 */
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import Database from "better-sqlite3";
import { afterEach, describe, expect, it } from "vitest";
import {
  type DriverOptions,
  buildClaudeArgs,
  parseClaudeJson,
  totalInputTokens,
} from "./claude-driver.js";
import {
  DEFAULT_GUARDRAIL_TYPES,
  readPlatformEvents,
} from "./metrics-reader.js";
import {
  buildReport,
  findGuardrailSaves,
  renderReport,
  scoreCarryOver,
  sumTokens,
} from "./score.js";
import {
  type ArmId,
  type PlatformEvents,
  type RunRecord,
  emptyPlatform,
} from "./types.js";

const DRIVER: DriverOptions = {
  permissionMode: "acceptEdits",
  emptyMcpConfigPath: "/tmp/empty-mcp.json",
};

const tmpDirs: string[] = [];
afterEach(() => {
  for (const d of tmpDirs.splice(0)) {
    rmSync(d, { recursive: true, force: true });
  }
});

function record(
  arm: ArmId,
  taskId: string,
  over: Partial<RunRecord> = {}
): RunRecord {
  return {
    instanceId: `${taskId}#0`,
    taskId,
    arm,
    dependsOn: [],
    resolved: false,
    inputTokens: 1000,
    freshInputTokens: 200,
    cacheCreateTokens: 300,
    cacheReadTokens: 500,
    outputTokens: 100,
    turns: 5,
    wallMs: 1000,
    costUsd: 0,
    breakages: 0,
    platform: emptyPlatform(),
    ...over,
  };
}

function platform(over: Partial<PlatformEvents>): PlatformEvents {
  return { ...emptyPlatform(), ...over };
}

describe("claude-driver arg wiring", () => {
  it("neutralizes the baseline arm with strict-mcp-config + empty config", () => {
    const args = buildClaudeArgs("do the thing", "baseline", DRIVER);
    expect(args).toContain("--strict-mcp-config");
    const i = args.indexOf("--mcp-config");
    expect(i).toBeGreaterThan(-1);
    expect(args[i + 1]).toBe(DRIVER.emptyMcpConfigPath);
  });

  it("does NOT neutralize the unerr arms (native .mcp.json applies)", () => {
    for (const arm of ["unerr", "unerr-nomemory"] as ArmId[]) {
      const args = buildClaudeArgs("do the thing", arm, DRIVER);
      expect(args).not.toContain("--strict-mcp-config");
      expect(args).not.toContain("--mcp-config");
    }
  });

  it("always asks for JSON output and passes the prompt + permission mode", () => {
    const args = buildClaudeArgs("fix bug", "unerr", DRIVER);
    expect(args.slice(0, 2)).toEqual(["-p", "fix bug"]);
    expect(args).toContain("--output-format");
    expect(args[args.indexOf("--output-format") + 1]).toBe("json");
    expect(args[args.indexOf("--permission-mode") + 1]).toBe("acceptEdits");
  });

  it("maps bypassPermissions to --dangerously-skip-permissions (MCP tools need it)", () => {
    const args = buildClaudeArgs("fix bug", "unerr", {
      ...DRIVER,
      permissionMode: "bypassPermissions",
    });
    expect(args).toContain("--dangerously-skip-permissions");
    // Mutually exclusive with --permission-mode — never emit both.
    expect(args).not.toContain("--permission-mode");
  });

  it("appends the autonomy system prompt for both arms (unattended-safe)", () => {
    for (const arm of ["baseline", "unerr"] as const) {
      const args = buildClaudeArgs("fix bug", arm, DRIVER);
      const i = args.indexOf("--append-system-prompt");
      expect(i).toBeGreaterThanOrEqual(0);
      expect(args[i + 1]).toMatch(/Never ask clarifying questions/);
      expect(args[i + 1]).toMatch(/Never enter plan mode/);
    }
  });

  it("adds optional model + max-turns when set", () => {
    const args = buildClaudeArgs("x", "unerr", {
      ...DRIVER,
      model: "claude-snap",
      maxTurns: 40,
    });
    expect(args[args.indexOf("--model") + 1]).toBe("claude-snap");
    expect(args[args.indexOf("--max-turns") + 1]).toBe("40");
  });
});

describe("claude-driver JSON parsing", () => {
  it("sums every input-token kind", () => {
    expect(
      totalInputTokens({
        input_tokens: 10,
        cache_creation_input_tokens: 5,
        cache_read_input_tokens: 100,
        output_tokens: 999,
      })
    ).toBe(115);
    expect(totalInputTokens(undefined)).toBe(0);
  });

  it("parses the result envelope into a DriverResult", () => {
    const out = parseClaudeJson(
      JSON.stringify({
        is_error: false,
        result: "done",
        total_cost_usd: 0.42,
        num_turns: 7,
        usage: {
          input_tokens: 10,
          cache_read_input_tokens: 90,
          output_tokens: 33,
        },
      })
    );
    expect(out).toEqual({
      inputTokens: 100,
      freshInputTokens: 10,
      cacheCreateTokens: 0,
      cacheReadTokens: 90,
      outputTokens: 33,
      turns: 7,
      costUsd: 0.42,
      isError: false,
      resultText: "done",
    });
  });
});

describe("token breakdown", () => {
  it("sums each bucket and weights units by the rate ratios", () => {
    const runs: RunRecord[] = [
      record("unerr", "t1", {
        freshInputTokens: 100,
        cacheCreateTokens: 200,
        cacheReadTokens: 1000,
        outputTokens: 50,
      }),
      record("unerr", "t2", {
        freshInputTokens: 50,
        cacheCreateTokens: 0,
        cacheReadTokens: 500,
        outputTokens: 10,
      }),
    ];
    const b = sumTokens(runs);
    expect(b.freshInput).toBe(150);
    expect(b.cacheCreate).toBe(200);
    expect(b.cacheRead).toBe(1500);
    expect(b.output).toBe(60);
    // plain count = 150 + 200 + 1500 + 60
    expect(b.total).toBe(1910);
    // weighted = 150·1 + 200·1.25 + 1500·0.1 + 60·5 = 150 + 250 + 150 + 300
    expect(b.weightedUnits).toBe(850);
  });

  it("renders a per-bucket table with a baseline→unerr reduction column", () => {
    const runs: RunRecord[] = [
      record("baseline", "t1", {
        freshInputTokens: 1000,
        cacheCreateTokens: 0,
        cacheReadTokens: 0,
        outputTokens: 100,
      }),
      record("unerr", "t1", {
        freshInputTokens: 400,
        cacheCreateTokens: 0,
        cacheReadTokens: 0,
        outputTokens: 60,
      }),
    ];
    const md = renderReport(buildReport(runs));
    expect(md).toContain("Token usage — full breakdown");
    expect(md).toContain("Fresh input (1×)");
    expect(md).toContain("Cache read (0.1×)");
    expect(md).toContain("**Total tokens**");
    // fresh: 1000 → 400 is a 60% reduction
    expect(md).toContain("60.0%");
  });
});

describe("metrics-reader", () => {
  function makeDb(rows: {
    behavior?: Array<{ ts: number; type: string }>;
    flow?: Array<{ ts: number; tokens_saved: number }>;
    comp?: Array<{ ts: number; saved_pct: number }>;
  }): string {
    const dir = mkdtempSync(join(tmpdir(), "t4-metrics-"));
    tmpDirs.push(dir);
    const db = new Database(join(dir, "metrics.db"));
    db.exec(
      `CREATE TABLE behavior_events (ts INTEGER, type TEXT);
       CREATE TABLE token_flow_events (ts INTEGER, tokens_saved INTEGER);
       CREATE TABLE compression_events (ts INTEGER, saved_pct REAL);`
    );
    const bi = db.prepare(
      "INSERT INTO behavior_events (ts, type) VALUES (?, ?)"
    );
    for (const r of rows.behavior ?? []) bi.run(r.ts, r.type);
    const fi = db.prepare(
      "INSERT INTO token_flow_events (ts, tokens_saved) VALUES (?, ?)"
    );
    for (const r of rows.flow ?? []) fi.run(r.ts, r.tokens_saved);
    const ci = db.prepare(
      "INSERT INTO compression_events (ts, saved_pct) VALUES (?, ?)"
    );
    for (const r of rows.comp ?? []) ci.run(r.ts, r.saved_pct);
    db.close();
    return dir;
  }

  it("returns an empty record when the DB is absent", () => {
    const dir = mkdtempSync(join(tmpdir(), "t4-nodb-"));
    tmpDirs.push(dir);
    expect(existsSync(join(dir, "metrics.db"))).toBe(false);
    expect(readPlatformEvents(dir, { startTs: 0, endTs: 9e15 })).toEqual(
      emptyPlatform()
    );
  });

  it("counts guardrail fires and groups every behavior type in the window", () => {
    const dir = makeDb({
      behavior: [
        { ts: 100, type: "cascade_guard" },
        { ts: 110, type: "cascade_guard" },
        { ts: 120, type: "blast_radius" },
        { ts: 130, type: "search_code" }, // not a guardrail
      ],
    });
    const ev = readPlatformEvents(dir, { startTs: 0, endTs: 1000 });
    expect(ev.guardrailFires).toBe(3); // 2 cascade + 1 blast
    expect(ev.eventsByType.cascade_guard).toBe(2);
    expect(ev.eventsByType.search_code).toBe(1); // present but not a guardrail
    expect(ev.behaviorRows).toBe(4);
  });

  it("excludes rows outside the time window", () => {
    const dir = makeDb({
      behavior: [
        { ts: 50, type: "cascade_guard" }, // before window
        { ts: 500, type: "cascade_guard" }, // in window
        { ts: 5000, type: "cascade_guard" }, // after window
      ],
    });
    const ev = readPlatformEvents(dir, { startTs: 100, endTs: 1000 });
    expect(ev.guardrailFires).toBe(1);
  });

  it("sums token-flow savings and averages compression saved_pct", () => {
    const dir = makeDb({
      flow: [
        { ts: 200, tokens_saved: 300 },
        { ts: 300, tokens_saved: 700 },
      ],
      comp: [
        { ts: 200, saved_pct: 80 },
        { ts: 300, saved_pct: 60 },
      ],
    });
    const ev = readPlatformEvents(dir, { startTs: 0, endTs: 1000 });
    expect(ev.navTokensSaved).toBe(1000);
    expect(ev.compressSavedPct).toBe(70);
  });

  it("ships a default guardrail set that includes the signature + read gates", () => {
    expect(DEFAULT_GUARDRAIL_TYPES.has("signature_edit_denied")).toBe(true);
    expect(DEFAULT_GUARDRAIL_TYPES.has("read_routing_denied")).toBe(true);
  });
});

describe("scoring — guardrail saves", () => {
  it("flags a task where baseline failed, unerr passed, and a guardrail fired", () => {
    const runs: RunRecord[] = [
      record("baseline", "t1", { resolved: false }),
      record("unerr", "t1", {
        resolved: true,
        platform: platform({
          guardrailFires: 2,
          eventsByType: { cascade_guard: 2 },
        }),
      }),
    ];
    const saves = findGuardrailSaves(runs);
    expect(saves).toHaveLength(1);
    expect(saves[0]?.taskId).toBe("t1");
    expect(saves[0]?.firedTypes).toContain("cascade_guard");
  });

  it("does not flag a save when no guardrail fired", () => {
    const runs: RunRecord[] = [
      record("baseline", "t1", { resolved: false }),
      record("unerr", "t1", { resolved: true, platform: emptyPlatform() }),
    ];
    expect(findGuardrailSaves(runs)).toHaveLength(0);
  });

  it("does not flag a save when baseline already passed", () => {
    const runs: RunRecord[] = [
      record("baseline", "t1", { resolved: true }),
      record("unerr", "t1", {
        resolved: true,
        platform: platform({ guardrailFires: 1, eventsByType: { drift: 1 } }),
      }),
    ];
    expect(findGuardrailSaves(runs)).toHaveLength(0);
  });
});

describe("scoring — memory carry-over", () => {
  it("counts a win when warm unerr resolves a dependent task the wiped arm missed", () => {
    const runs: RunRecord[] = [
      record("unerr", "t2", { dependsOn: ["t1"], resolved: true }),
      record("unerr-nomemory", "t2", { dependsOn: ["t1"], resolved: false }),
    ];
    const carry = scoreCarryOver(runs);
    expect(carry).toHaveLength(1);
    expect(carry[0]?.memoryHelped).toBe(true);
  });

  it("ignores tasks with no dependsOn (not a memory probe)", () => {
    const runs: RunRecord[] = [
      record("unerr", "t1", { dependsOn: [], resolved: true }),
      record("unerr-nomemory", "t1", { dependsOn: [], resolved: false }),
    ];
    expect(scoreCarryOver(runs)).toHaveLength(0);
  });
});

describe("scoring — full report", () => {
  it("compares unerr vs baseline and rolls up all pillars", () => {
    const runs: RunRecord[] = [
      record("baseline", "t1", { resolved: false, inputTokens: 5000 }),
      record("baseline", "t2", {
        dependsOn: ["t1"],
        resolved: false,
        inputTokens: 6000,
      }),
      record("unerr", "t1", {
        resolved: true,
        inputTokens: 2000,
        platform: platform({
          guardrailFires: 1,
          eventsByType: { blast_radius: 1 },
        }),
      }),
      record("unerr", "t2", {
        dependsOn: ["t1"],
        resolved: true,
        inputTokens: 2500,
      }),
      record("unerr-nomemory", "t1", { resolved: true, inputTokens: 3000 }),
      record("unerr-nomemory", "t2", {
        dependsOn: ["t1"],
        resolved: false,
        inputTokens: 4000,
      }),
    ];
    const report = buildReport(runs);
    expect(report.unerrVsBaseline).not.toBeNull();
    // baseline 11000 → unerr 4500 input tokens.
    expect(report.unerrVsBaseline?.inputTokenReductionPct).toBeCloseTo(
      ((11000 - 4500) / 11000) * 100,
      1
    );
    expect(report.guardrailFires).toBe(1);
    expect(report.guardrailSaves).toHaveLength(1); // t1
    expect(report.memoryWins).toBe(1); // t2 warm passed, wiped failed
  });

  it("leaves the comparison null when an arm is missing", () => {
    const runs = [record("unerr", "t1", { resolved: true })];
    expect(buildReport(runs).unerrVsBaseline).toBeNull();
  });
});
