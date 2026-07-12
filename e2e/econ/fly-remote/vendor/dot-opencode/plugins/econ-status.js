/**
 * econ-status — S7.2 status-line surface plugin.
 *
 * econ's proof surface needs three live numbers on the status line: context
 * fill, cache-hit ratio, and the tier currently in use. This fork's TUI is
 * TS/Solid (not Go) and already renders context-fill in its sidebar
 * (packages/tui/src/feature-plugins/sidebar/context.tsx) by reading per-message
 * tokens. Rather than fork the TUI, this plugin subscribes to the same event
 * stream the TUI consumes and surfaces econ's status (tier + cumulative
 * cache-hit) alongside it — the seam that needs no TUI change.
 *
 * Seam decision: OpenCode has no dedicated "statusline" plugin hook (the Hooks
 * interface in packages/plugin/src/index.ts exposes `event`, `chat.message`,
 * `tool.execute.*`, etc., but nothing status-shaped). The cleanest econ-owned
 * surface is therefore the `event` hook: we watch `session.next.step.started`
 * for the model in use and `session.next.step.ended` for per-turn tokens/cost.
 * A first-class status surface (a `message.usage` hook + a server
 * `/session/:id/status` route the TUI footer reads) is documented as a deferred
 * follow-up in docs/sprint7/status-line.md — it requires editing core/server
 * files outside Sprint 7's ownership boundary.
 *
 * Tier inference here is a name heuristic mirroring inferTier() in
 * packages/code-intelligence/src/telemetry.ts (kept inline because plugin files
 * are plain .js with no bundler). The authoritative tier is the router's S3.2
 * decision; this is the best-effort fallback until the router tags turns.
 *
 * Load mechanism: OpenCode auto-discovers all *.js under .opencode/plugins/ at
 * startup (packages/opencode/src/config/plugin.ts: Glob.scan).
 */

// ---------------------------------------------------------------------------
// Tier inference — inline mirror of telemetry.ts::inferTier
// ---------------------------------------------------------------------------

const ORACLE_HINTS = ["opus", "gpt-4", "gpt4", "gpt-5", "o1", "o3", "sonnet-4", "ultra"]
const CHEAP_HINTS = ["haiku", "mini", "nano", "flash", "qwen", "8b", "7b", "small", "lite"]
const REASONER_HINTS = ["32b", "reason", "r1", "sonnet", "medium", "70b", "deepseek"]

function inferTier(model) {
  const m = (model || "").toLowerCase()
  if (ORACLE_HINTS.some((h) => m.includes(h))) return "oracle"
  if (REASONER_HINTS.some((h) => m.includes(h))) return "reasoner"
  if (CHEAP_HINTS.some((h) => m.includes(h))) return "cheap"
  return "unknown"
}

function pct(n) {
  return `${(n * 100).toFixed(1)}%`
}

// ---------------------------------------------------------------------------
// EconStatusPlugin
// ---------------------------------------------------------------------------

/**
 * Per-session running status. cacheRead / (cacheRead + input) is the cumulative
 * cache-hit ratio; cache-write is a cost, not a hit, so it is excluded.
 */
function freshState() {
  return { model: undefined, tier: "unknown", cumRead: 0, cumInput: 0, latestTokens: 0, cumCost: 0, turns: 0 }
}

export const EconStatusPlugin = async (_ctx) => {
  console.error("[econ-status] plugin loaded")

  /** @type {Map<string, ReturnType<typeof freshState>>} */
  const sessions = new Map()
  const get = (id) => {
    let s = sessions.get(id)
    if (!s) sessions.set(id, (s = freshState()))
    return s
  }

  return {
    event: async ({ event }) => {
      if (!event || typeof event.type !== "string") return
      const p = event.properties || {}

      // Track the model in use — only Step.Started carries the model ref.
      if (event.type === "session.next.step.started") {
        if (!p.sessionID || !p.model) return
        const s = get(p.sessionID)
        s.model = `${p.model.providerID}/${p.model.id}`
        s.tier = inferTier(s.model)
        return
      }

      // Roll up per-turn usage and surface the status line.
      if (event.type === "session.next.step.ended") {
        if (!p.sessionID || !p.tokens) return
        const s = get(p.sessionID)
        const t = p.tokens
        const read = (t.cache && t.cache.read) || 0
        s.cumRead += read
        s.cumInput += t.input || 0
        s.latestTokens = (t.input || 0) + (t.output || 0) + (t.reasoning || 0) + read + ((t.cache && t.cache.write) || 0)
        s.cumCost += p.cost || 0
        s.turns += 1

        const denom = s.cumRead + s.cumInput
        const cacheHit = denom > 0 ? s.cumRead / denom : 0
        // Status line: tier · context tokens · cache-hit · spend.
        // (Context-fill % needs the model's context-window limit, which lives in
        // the provider config the TUI sidebar already reads — see status-line.md.)
        console.error(
          `[econ-status] tier=${s.tier} model=${s.model || "?"} ctx≈${s.latestTokens}tok cache-hit=${pct(cacheHit)} spent=$${s.cumCost.toFixed(4)} turns=${s.turns}`,
        )
        return
      }
    },
  }
}
