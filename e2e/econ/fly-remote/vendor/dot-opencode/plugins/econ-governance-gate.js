/**
 * econ-governance-gate — S6.3 in-loop governance gate (the safety moat).
 *
 * Registers a tool.execute.before hook that evaluates every write/edit/apply_patch
 * against the guardrail engine (packages/code-intelligence/src/guardrail.ts) BEFORE
 * it lands. A breaking signature change whose callers are not being updated is
 * REFUSED (thrown → surfaces to the model as a tool error); architecture-boundary
 * crossings and convention/drift advisories are logged but allowed; a
 * retry-loop-breaker releases the block if the model re-proposes the identical
 * edit too many times, so the turn is never deadlocked.
 *
 * Load mechanism: OpenCode auto-discovers *.js under .opencode/plugins/ at startup.
 * The graph is a lazily-indexed in-memory BunSqliteGraph over process.cwd() — on
 * the first turn it may be empty (no callers → no block), which is acceptable.
 *
 * NOTE: guardrail.ts is imported directly (not via index.ts) — its exports are
 * pending index wiring under the sprint's file-ownership boundary.
 */

import { fileURLToPath } from "node:url"
import { dirname, resolve, relative, isAbsolute } from "node:path"
import { readFile } from "node:fs/promises"

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

/** Tool IDs that perform file writes/edits (matches Tool.define names). */
const EDIT_TOOLS = new Set(["write", "edit", "apply_patch"])

let _graph = null
let _guardrail = null
let _state = null

/** Lazily import the graph + guardrail engine and create per-session state. */
async function load() {
  if (_graph && _guardrail) return
  try {
    const ciPath = resolve(__dirname, "../../packages/code-intelligence/src/index.ts")
    const guardrailPath = resolve(__dirname, "../../packages/code-intelligence/src/guardrail.ts")
    const { BunSqliteGraph } = await import(ciPath)
    _guardrail = await import(guardrailPath)
    _state = _guardrail.createGuardrailState()
    _graph = new BunSqliteGraph(":memory:", process.cwd())
    // Background index — fire and forget; empty results are fine until done.
    _graph.index(process.cwd()).catch((err) => {
      console.error("[econ-governance-gate] index error:", err?.message ?? String(err))
    })
  } catch (err) {
    console.error("[econ-governance-gate] load failed (gate disabled):", err?.message ?? String(err))
    _graph = null
  }
}

/** Repo-relative path so graph lookups + boundary rules match indexed keys. */
function toRel(filePath) {
  const root = process.cwd()
  return isAbsolute(filePath) ? relative(root, filePath) : filePath
}

/**
 * Derive the (oldContent, newContent) the guardrail evaluates from the tool args.
 * write: whole-file (old from disk, new from args). edit: the replaced hunk
 * fragments (the signature detector tolerates partial hunks). apply_patch: only
 * the old file content (no reliable reconstructed new content).
 */
async function resolveContents(tool, args, filePath) {
  if (tool === "write") {
    const newContent = typeof args.content === "string" ? args.content : null
    const oldContent = await readFile(filePath, "utf8").catch(() => null)
    return { oldContent, newContent }
  }
  if (tool === "edit") {
    const oldContent = typeof args.oldString === "string" ? args.oldString : (args.old_string ?? null)
    const newContent = typeof args.newString === "string" ? args.newString : (args.new_string ?? null)
    return { oldContent, newContent }
  }
  // apply_patch and anything else: best-effort old content from disk.
  const oldContent = await readFile(filePath, "utf8").catch(() => null)
  return { oldContent, newContent: null }
}

/**
 * EconGovernanceGatePlugin — the in-loop gate. Throwing inside the hook rejects
 * the tool call, refusing the edit; advisories are logged without blocking.
 */
export const EconGovernanceGatePlugin = async (_ctx) => {
  console.error("[econ-governance-gate] plugin loaded")

  return {
    "tool.execute.before": async (input, output) => {
      if (!EDIT_TOOLS.has(input.tool)) return

      let decision
      try {
        await load()
        if (!_graph) return // gate disabled / load failed → fail open

        const args = output.args ?? {}
        const rawPath = args.filePath ?? args.file_path ?? ""
        if (!rawPath) return

        const { oldContent, newContent } = await resolveContents(input.tool, args, rawPath)
        decision = await _guardrail.evaluateEdit(
          _graph,
          { tool: input.tool, filePath: toRel(rawPath), oldContent, newContent },
          _state,
        )
      } catch (err) {
        // Fail open on internal gate errors — never block a legitimate edit on a graph hiccup.
        console.error("[econ-governance-gate] internal error (failing open):", err?.message ?? String(err))
        return
      }

      if (!decision) return
      if (decision.verdict === "block") {
        console.error(decision.reason)
        throw new Error(decision.reason) // refuses the edit; the model sees the reason
      }
      if (decision.verdict === "warn" && decision.reason) {
        console.error(decision.reason) // advisory — the edit proceeds
      }
    },
  }
}
