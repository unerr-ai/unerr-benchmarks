/**
 * econ-gate-spike — S0.4 proof-of-concept governance gate plugin.
 *
 * Demonstrates that tool.execute.before can ENFORCE at the edit boundary:
 * any write/edit/apply_patch to a path containing "ECON_GATE_BLOCKED" is refused
 * by throwing synchronously, which surfaces to the model as a tool error.
 *
 * Load mechanism: OpenCode auto-discovers all *.js files under .opencode/plugins/
 * at startup (packages/opencode/src/config/plugin.ts: Glob.scan("{plugin,plugins}/*.{ts,js}")).
 * Each exported function is called with a PluginInput context and must return a Hooks object.
 */

/**
 * The set of tool IDs that perform file writes/edits in this codebase.
 * Matches Tool.define(...) first arg in packages/opencode/src/tool/{write,edit,apply_patch}.ts.
 */
const EDIT_TOOLS = new Set(["write", "edit", "apply_patch"])

/**
 * Files whose path contains this marker are blocked by the gate.
 * Replace with a real rule (e.g. a set of protected paths, or a regex) for production use.
 */
const BLOCKED_MARKER = "ECON_GATE_BLOCKED"

/**
 * EconGateSpikePlugin
 *
 * Registers a tool.execute.before hook that aborts edit-class tool calls
 * targeting paths matched by BLOCKED_MARKER. Throwing inside the hook causes
 * Effect.promise (plugin/index.ts:290) to reject, which propagates up through
 * run.promise and surfaces to the AI SDK as a tool call error the model sees.
 */
export const EconGateSpikePlugin = async (_ctx) => {
  console.error("[econ-gate-spike] plugin loaded")

  return {
    "tool.execute.before": async (input, output) => {
      // Only intercept file-edit class tools
      if (!EDIT_TOOLS.has(input.tool)) return

      // The filePath lives in output.args for both write and edit tools
      const filePath = output.args?.filePath ?? output.args?.file_path ?? ""

      if (filePath.includes(BLOCKED_MARKER)) {
        const msg =
          `[econ-gate] REFUSED: '${input.tool}' on '${filePath}' blocked by governance gate ` +
          `(path matches marker "${BLOCKED_MARKER}"). ` +
          `This edit is not permitted. Choose a different file.`
        console.error(msg)
        throw new Error(msg)
      }
    },
  }
}
