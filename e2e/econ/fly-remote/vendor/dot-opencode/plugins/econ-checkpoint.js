/**
 * econ-checkpoint — S5.1 MiMo-style structured checkpoint trigger (survive 100-200 step runs).
 *
 * Grafts a DETERMINISTIC, proactive context-reconstruction layer ON TOP of
 * OpenCode's existing overflow compaction (packages/core/src/session/compaction.ts).
 * Compaction fires only when the window OVERFLOWS (estimate > context - buffer ~ full)
 * and pays an LLM summary call. This plugin fires structured-state reinjection
 * EARLIER and FOR FREE — at 40/60/80% context fill — so load-bearing state
 * (scratchpad, tool-call ledger, attempts, task-tree) stays fresh in-context and a
 * long run never has to rebuild everything from a single lossy overflow summary.
 * The two layers are complementary: checkpoint is the cheap proactive net,
 * compaction is the lossy backstop at the ceiling.
 *
 * Seam decision (mirrors econ-status.js / econ-recon-inject.js): OpenCode exposes
 * an `event` hook and a `context.assemble.before` hook, but no DB handle to the
 * SessionCanonicalState service from a plain-.js plugin. So this plugin keeps a
 * lightweight in-process snapshot (a tool-call ledger built from tool.execute.after
 * + a fill estimate from session.next.step.ended token usage), runs the pure
 * checkpoint engine (packages/code-intelligence/src/checkpoint.ts) to decide WHEN a
 * threshold fires, and injects the bounded reconstruction at the next
 * context.assemble.before. Wiring the trigger to the durable canonical-state store
 * (SessionCanonicalState.rebuildContext + loadTaskTree, added in S5.1/S5.2) is a
 * backend follow-up that needs core edits outside this sprint's ownership boundary —
 * documented in docs/sprint5/checkpoint.md.
 *
 * Load mechanism: OpenCode auto-discovers *.js under .opencode/plugins/ at startup.
 */

import { fileURLToPath } from "node:url"
import { dirname, resolve } from "node:path"

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

// Default model context window when the event stream carries no limit. Override
// with ECON_CONTEXT_LIMIT. Used only to turn token counts into a fill ratio.
const DEFAULT_CONTEXT_LIMIT = Number(process.env.ECON_CONTEXT_LIMIT) || 200_000

// Tool IDs whose completed calls are worth recording in the reconstruction ledger.
const LEDGER_TOOLS = new Set(["write", "edit", "apply_patch", "bash", "read", "grep", "glob", "webfetch"])

let _checkpoint = null

/** Lazily import the pure checkpoint engine from the code-intelligence package. */
async function loadEngine() {
  if (_checkpoint) return _checkpoint
  try {
    const path = resolve(__dirname, "../../packages/code-intelligence/src/checkpoint.ts")
    _checkpoint = await import(path)
  } catch (err) {
    console.error("[econ-checkpoint] engine load failed (checkpoint disabled):", err?.message ?? String(err))
    _checkpoint = null
  }
  return _checkpoint
}

/** Best-effort model context window from a step.started model ref, else the default. */
function contextLimitOf(model) {
  return model?.limit?.context ?? model?.contextLimit ?? model?.limits?.context ?? DEFAULT_CONTEXT_LIMIT
}

/** Per-session running state: the in-process snapshot, fill estimate, and checkpoint hysteresis. */
function freshSession(engine) {
  return {
    contextLimit: DEFAULT_CONTEXT_LIMIT,
    snapshot: { scratchpad: {}, ledger: [], attempts: [], taskTree: undefined },
    state: engine.createCheckpointState(),
    pendingContext: null, // a built checkpoint block awaiting injection at context.assemble.before
  }
}

export const EconCheckpointPlugin = async (_ctx) => {
  const engine = await loadEngine()
  if (!engine) {
    return {} // load failed → no-op plugin, never blocks the session
  }
  console.error("[econ-checkpoint] plugin loaded (thresholds 40/60/80% context fill)")

  /** @type {Map<string, ReturnType<typeof freshSession>>} */
  const sessions = new Map()
  const get = (id) => {
    let s = sessions.get(id)
    if (!s) sessions.set(id, (s = freshSession(engine)))
    return s
  }

  return {
    // Build the in-process reconstruction ledger from completed tool calls.
    "tool.execute.after": async (input, output) => {
      try {
        if (!input?.sessionID || !LEDGER_TOOLS.has(input.tool)) return
        const s = get(input.sessionID)
        const args = output?.args ?? {}
        s.snapshot.ledger.push({
          tool: input.tool,
          result: typeof output?.title === "string" ? output.title : (args.filePath ?? args.file_path ?? undefined),
          seq: s.snapshot.ledger.length + 1,
        })
      } catch (err) {
        console.error("[econ-checkpoint] ledger capture error:", err?.message ?? String(err))
      }
    },

    // Watch context fill and fire a checkpoint when a threshold is crossed.
    event: async ({ event }) => {
      try {
        if (!event || typeof event.type !== "string") return
        const p = event.properties || {}

        if (event.type === "session.next.step.started" && p.sessionID && p.model) {
          get(p.sessionID).contextLimit = contextLimitOf(p.model)
          return
        }

        if (event.type === "session.next.step.ended" && p.sessionID && p.tokens) {
          const s = get(p.sessionID)
          const t = p.tokens
          const used =
            (t.input || 0) +
            (t.output || 0) +
            (t.reasoning || 0) +
            ((t.cache && t.cache.read) || 0) +
            ((t.cache && t.cache.write) || 0)
          const fillRatio = s.contextLimit > 0 ? used / s.contextLimit : 0

          const result = engine.checkpointIfNeeded(fillRatio, s.snapshot, s.state)
          if (result.fired && result.context) {
            s.pendingContext = result.context
            console.error(
              `[econ-checkpoint] reconstruction fired at ${(result.threshold * 100).toFixed(0)}% fill ` +
                `(used≈${used}tok / ${s.contextLimit}) — ${result.context.length} chars queued for injection`,
            )
          }
        }
      } catch (err) {
        console.error("[econ-checkpoint] event handler error:", err?.message ?? String(err))
      }
    },

    // Inject the queued checkpoint block (if any) before the conversation history.
    "context.assemble.before": async (input, output) => {
      try {
        const sessionID = input?.sessionID ?? input?.message?.sessionID
        if (!sessionID) return
        const s = sessions.get(sessionID)
        if (!s || !s.pendingContext) return
        output.messages.push({ role: "user", content: s.pendingContext })
        console.error(`[econ-checkpoint] injected reconstruction (${s.pendingContext.length} chars)`)
        s.pendingContext = null // consume once
      } catch (err) {
        console.error("[econ-checkpoint] injection error:", err?.message ?? String(err))
      }
    },
  }
}
