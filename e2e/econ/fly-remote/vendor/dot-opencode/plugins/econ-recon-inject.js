/**
 * econ-recon-inject — S2.1 push-based recon bundle injection.
 *
 * Registers a `context.assemble.before` hook that:
 *   1. Derives a task phrase from the user message metadata (summary title,
 *      body, or a runtime `.text` field if the host populates it).
 *   2. Calls `graph.recon(task)` against a lazy BunSqliteGraph singleton
 *      (indexed once from process.cwd() in the background on first call).
 *   3. Formats a compact, line-oriented recon bundle (top entities + callers
 *      + references + any anchored notes/domain tags).
 *   4. Pushes the bundle as a `{ role: "user", content }` entry into
 *      `output.messages`, which the core prepends immediately before the
 *      conversation history.
 *
 * Position in the final message array (cache-friendly):
 *   [system prompt(s)] [recon bundle] [conversation history]
 *
 * The stable position (before history) lets the system + recon block be
 * prompt-cached by the provider on every turn, so the graph query cost
 * is paid once per session, not per token.
 *
 * No read fan-out: the model starts each turn with relevant entities,
 * callers, and conventions pre-loaded — no search_code / file_read needed
 * for common routing decisions.
 *
 * Load mechanism: OpenCode auto-discovers .opencode/plugins/*.js at startup
 * (packages/opencode/src/config/plugin.ts → Glob.scan("{plugin,plugins}/*.{ts,js}")).
 * Hook contract: packages/plugin/src/index.ts Hooks["context.assemble.before"].
 * Fire point: packages/opencode/src/session/llm/request.ts ~line 83.
 */

import { fileURLToPath } from "node:url"
import { dirname, resolve } from "node:path"

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

// ---------------------------------------------------------------------------
// Lazy BunSqliteGraph singleton — indexed once against process.cwd()
// ---------------------------------------------------------------------------

/** @type {import("../../packages/code-intelligence/src/index.ts").BunSqliteGraph | null} */
let _graph = null

/**
 * Return the singleton graph, initialising it on first call.
 * Indexing is fire-and-forget; queries return empty results until the index
 * completes, which is acceptable for the first turn of a session.
 *
 * @returns {Promise<import("../../packages/code-intelligence/src/index.ts").BunSqliteGraph | null>}
 */
async function getGraph() {
  if (_graph) return _graph
  try {
    const ciPath = resolve(__dirname, "../../packages/code-intelligence/src/index.ts")
    const { BunSqliteGraph } = await import(ciPath)
    const root = process.cwd()
    _graph = new BunSqliteGraph(":memory:", root)
    // Background index — fire and forget; empty results are fine until done
    _graph.index(root).catch((err) => {
      console.error("[econ-recon-inject] index error:", err?.message ?? String(err))
    })
    return _graph
  } catch (err) {
    console.error("[econ-recon-inject] graph init failed:", err?.message ?? String(err))
    return null
  }
}

// ---------------------------------------------------------------------------
// Task-phrase extraction
// ---------------------------------------------------------------------------

/**
 * Derive a task phrase from the UserMessage metadata.
 *
 * Priority:
 *  1. `message.text`         — populated by some host integrations (SessionMessageUser v2)
 *  2. `message.summary.title` — set after compaction summarises the turn
 *  3. `message.summary.body`  — longer summary prose (sliced to 120 chars)
 *  4. Absolute fallback: "code" (broad enough to hit most entity searches)
 *
 * @param {Record<string, any>} message
 * @returns {string}
 */
function extractTask(message) {
  if (typeof message?.text === "string" && message.text.trim()) {
    return message.text.trim().slice(0, 200)
  }
  const title = message?.summary?.title
  if (typeof title === "string" && title.trim()) return title.trim()
  const body = message?.summary?.body
  if (typeof body === "string" && body.trim()) return body.trim().slice(0, 120)
  return "code"
}

// ---------------------------------------------------------------------------
// Bundle formatter
// ---------------------------------------------------------------------------

/**
 * Format a ReconBundle into a compact, deterministic context block.
 *
 * Format:
 *   === RECON: <task> ===
 *   ENTITIES:
 *     <name> (<kind>) <filePath>:<lineStart>  [domain=<d> role=<r>]
 *       sig: <signature>
 *   REFERENCES:
 *     <fromEntity> → <toEntity> (<kind>)
 *   NOTES:
 *     <note>
 *   === END RECON ===
 *
 * Returns null when the bundle is empty (nothing to inject).
 *
 * @param {string} task
 * @param {{ entities: any[]; references: any[]; notes: string[] }} bundle
 * @returns {string | null}
 */
function formatBundle(task, bundle) {
  if (!bundle) return null
  const hasContent =
    bundle.entities.length > 0 || bundle.notes.length > 0
  if (!hasContent) return null

  const lines = [`=== RECON: ${task} ===`]

  if (bundle.entities.length > 0) {
    lines.push("ENTITIES:")
    const top = bundle.entities.slice(0, 8)
    for (const e of top) {
      const domainTag =
        e.domain
          ? `  [domain=${e.domain}${e.role ? ` role=${e.role}` : ""}]`
          : ""
      lines.push(`  ${e.name} (${e.kind}) ${e.filePath}:${e.lineStart}${domainTag}`)
      if (e.signature) lines.push(`    sig: ${e.signature}`)
    }
  }

  const shown = new Set()
  const topRefs = bundle.references.slice(0, 12)
  if (topRefs.length > 0) {
    lines.push("REFERENCES:")
    for (const r of topRefs) {
      const key = `${r.fromEntity}→${r.toEntity}`
      if (shown.has(key)) continue
      shown.add(key)
      lines.push(`  ${r.fromEntity} → ${r.toEntity} (${r.kind})`)
    }
  }

  if (bundle.notes && bundle.notes.length > 0) {
    lines.push("NOTES:")
    for (const n of bundle.notes) {
      lines.push(`  ${n}`)
    }
  }

  lines.push("=== END RECON ===")
  return lines.join("\n")
}

// ---------------------------------------------------------------------------
// Plugin export
// ---------------------------------------------------------------------------

/**
 * EconReconInjectPlugin
 *
 * Registers a context.assemble.before hook that queries the in-process code
 * graph for the user's task phrase and injects a compact recon bundle as a
 * user-role context message before the conversation history.
 */
export const EconReconInjectPlugin = async (_ctx) => {
  console.error("[econ-recon-inject] plugin loaded")

  return {
    "context.assemble.before": async (input, output) => {
      const task = extractTask(input.message)

      const graph = await getGraph()
      if (!graph) return

      let bundle
      try {
        bundle = await graph.recon(task)
      } catch (err) {
        console.error("[econ-recon-inject] recon failed:", err?.message ?? String(err))
        return
      }

      const formatted = formatBundle(task, bundle)
      if (!formatted) return

      // Inject immediately before conversation history.
      // role "user" is used so the block is clearly separated from the system
      // prompt and sits in the most cache-friendly position.
      output.messages.push({
        role: "user",
        content: formatted,
      })

      console.error(
        `[econ-recon-inject] injected recon for "${task}": ` +
          `${bundle.entities.length} entities, ${bundle.references.length} refs`,
      )
    },
  }
}
