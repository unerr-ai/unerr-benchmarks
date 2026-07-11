/**
 * In-process unerr harness.
 *
 * Boots a real CozoDB (in-memory), indexes a target repo with the SAME indexer
 * the daemon uses, and exposes `runTool()` which calls the SAME QueryRouter the
 * MCP server dispatches through. So the bytes we measure are the bytes the agent
 * would actually receive from a tool call — post graph query, post enrichment,
 * post output-compression, post budget enforcement.
 *
 * Caveat: the final stdio↔UDS `wire-cap` (proxy.ts) is one layer above the
 * QueryRouter and is not applied here. wire-cap only ever SHRINKS payloads
 * further, so measuring at the router output is a CONSERVATIVE (favours the
 * baseline) estimate of unerr's saving — which is the honest direction to err.
 *
 * Benchmark/research tooling only — not shipped.
 */
import { performance } from "node:perf_hooks";

export interface ToolCall {
  tool: string;
  args: Record<string, unknown>;
}

export interface ToolResult {
  /** The serialized payload an agent receives (JSON string of the tool result). */
  payload: string;
  /** The raw object, for fidelity assertions. */
  raw: unknown;
  latencyMs: number;
}

export interface Harness {
  repoRoot: string;
  entityCount: number;
  edgeCount: number;
  runTool(call: ToolCall): Promise<ToolResult>;
  /** Raw Datalog passthrough to the graph DB — used to auto-derive the corpus. */
  query(datalog: string, params?: Record<string, unknown>): Promise<unknown[][]>;
  close(): Promise<void>;
}

/**
 * Index `repoRoot` and return a harness ready to answer tool calls.
 * `progress` is invoked with coarse phase strings so the CLI can show life.
 */
export async function bootHarness(
  repoRoot: string,
  progress?: (msg: string) => void
): Promise<Harness> {
  const say = progress ?? (() => {});

  say(`opening in-memory CozoDB`);
  // cozo-node is a dynamic import everywhere in this codebase (native addon).
  const cozoMod = (await import("cozo-node")) as unknown as {
    CozoDb: new (engine?: string, path?: string) => unknown;
  };
  const CozoDb = cozoMod.CozoDb ?? (cozoMod as { default: typeof cozoMod.CozoDb }).default;
  const db = new CozoDb(); // no path → in-memory engine

  const { CozoGraphStore } = await import(
    "../../src/intelligence/local-graph.js"
  );
  // create() runs initSchema() internally.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const graph = await CozoGraphStore.create(db as any);

  say(`indexing ${repoRoot}`);
  const { indexLocalProject } = await import(
    "../../src/intelligence/local-indexer.js"
  );
  const repoId = repoRoot.replace(/[^a-zA-Z0-9]/g, "_").slice(-48);
  const indexResult = await indexLocalProject(repoRoot, graph, repoId);

  // Convention + community passes enrich the graph the way the daemon does, so
  // tools like get_conventions / get_cross_boundary_links have data to return.
  try {
    const { runCommunityDetection, runConventionDetection } = await import(
      "../../src/intelligence/local-indexer.js"
    );
    say(`detecting communities + conventions`);
    await runCommunityDetection(graph, repoId);
    await runConventionDetection(graph, repoId);
  } catch (err) {
    say(`enrichment skipped: ${(err as Error).message}`);
  }

  const { QueryRouter } = await import(
    "../../src/intelligence/query-router.js"
  );
  const router = new QueryRouter(graph);
  router.setProjectRoot(repoRoot);

  const entityCount = await graph.getEntityCount();
  const edgeCount =
    (indexResult as { edgeCount?: number }).edgeCount ??
    (indexResult as { edges?: number }).edges ??
    0;

  say(`ready — ${entityCount} entities, ${edgeCount} edges`);

  return {
    repoRoot,
    entityCount,
    edgeCount,
    async runTool({ tool, args }: ToolCall): Promise<ToolResult> {
      const t0 = performance.now();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const raw = await (router as any).execute(tool, args);
      const latencyMs = performance.now() - t0;
      const payload = typeof raw === "string" ? raw : JSON.stringify(raw);
      return { payload, raw, latencyMs };
    },
    async query(datalog: string, params?: Record<string, unknown>) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const res = await (graph as any).db.run(datalog, params ?? {});
      return (res?.rows ?? []) as unknown[][];
    },
    async close() {
      try {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (db as any).close?.();
      } catch {
        /* in-memory db: nothing to flush */
      }
    },
  };
}
