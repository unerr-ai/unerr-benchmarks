/**
 * econ-executor-validate — S4.1 tool-call argument schema validator.
 *
 * Catches malformed tool calls from cheap executor models (~9B) before they
 * reach dispatch. Required fields are checked for presence and correct
 * primitive type; missing or wrong-typed fields cause an immediate refusal.
 *
 * Load mechanism: OpenCode auto-discovers all *.js files under .opencode/plugins/
 * at startup (packages/opencode/src/config/plugin.ts: Glob.scan("{plugin,plugins}/*.{ts,js}")).
 * Each exported function is called with a PluginInput context and must return a Hooks object.
 *
 * Enforcement mechanism: throwing synchronously inside "tool.execute.before" causes
 * Effect.promise (plugin/index.ts:290) to reject, which propagates up through
 * run.promise and surfaces to the AI SDK as a tool call error the model sees —
 * so the model can self-correct and retry with valid arguments.
 *
 * Hook contract (packages/plugin/src/index.ts:266):
 *   "tool.execute.before": (
 *     input:  { tool: string, sessionID: string, callID: string },
 *     output: { args: any }
 *   ) => Promise<void>
 */

/**
 * Per-tool required-field schema table.
 *
 * Each entry is an array of field specs: { aliases: string[], type: string }.
 * aliases: the accepted camelCase / snake_case names for the same logical field.
 * type:    the expected typeof value (always 'string' here).
 *
 * Tools NOT listed here pass through untouched.
 */
const TOOL_SCHEMAS = {
  read: [
    { aliases: ["filePath", "file_path"], type: "string" },
  ],
  edit: [
    { aliases: ["filePath", "file_path"],   type: "string" },
    { aliases: ["oldString", "old_string"], type: "string" },
    { aliases: ["newString", "new_string"], type: "string" },
  ],
  write: [
    { aliases: ["filePath", "file_path"], type: "string" },
    { aliases: ["content"],               type: "string" },
  ],
  bash: [
    { aliases: ["command"], type: "string" },
  ],
  grep: [
    { aliases: ["pattern"], type: "string" },
  ],
  glob: [
    { aliases: ["pattern"], type: "string" },
  ],
  task: [
    { aliases: ["description"],    type: "string" },
    { aliases: ["prompt"],         type: "string" },
    { aliases: ["subagent_type"],  type: "string" },
  ],
}

/**
 * Resolve the value for a field spec from args, checking all aliases.
 * Returns { found: true, value } when any alias is present,
 * or { found: false } when none are.
 */
function resolveField(args, fieldSpec) {
  for (const alias of fieldSpec.aliases) {
    if (Object.prototype.hasOwnProperty.call(args, alias)) {
      return { found: true, value: args[alias] }
    }
  }
  return { found: false }
}

/**
 * Validate args against a schema (array of field specs).
 * Returns null on success, or an error reason string on failure.
 */
function validateArgs(tool, args, schema) {
  // Guard: args must be a non-null object
  if (args === null || args === undefined || typeof args !== "object" || Array.isArray(args)) {
    return `args is ${args === null ? "null" : typeof args === "undefined" ? "undefined" : Array.isArray(args) ? "an array" : typeof args} — must be a plain object`
  }

  const failures = []

  for (const fieldSpec of schema) {
    const primary = fieldSpec.aliases[0]
    const { found, value } = resolveField(args, fieldSpec)

    if (!found) {
      const aliasDisplay = fieldSpec.aliases.length > 1
        ? `${fieldSpec.aliases.join(" | ")}`
        : primary
      failures.push(`'${aliasDisplay}' is missing`)
      continue
    }

    // Must be the expected primitive type and non-empty string
    if (typeof value !== fieldSpec.type) {
      failures.push(`'${primary}' must be a ${fieldSpec.type}, got ${typeof value}`)
      continue
    }

    if (fieldSpec.type === "string" && value.trim() === "") {
      failures.push(`'${primary}' must be a non-empty string`)
    }
  }

  if (failures.length === 0) return null

  // Build a human-readable required-fields list for the error message
  const required = schema.map((f) =>
    f.aliases.length > 1 ? f.aliases.join("|") : f.aliases[0]
  )

  return `${failures.join("; ")}. Required: ${required.join(", ")}`
}

/**
 * EconExecutorValidatePlugin
 *
 * Registers a tool.execute.before hook that validates tool-call argument schemas
 * for a fixed set of tools before dispatch. Tools not in the schema table pass
 * through untouched. Throwing inside the hook surfaces the error to the model
 * so it can self-correct and retry with valid arguments.
 */
export const EconExecutorValidatePlugin = async (_ctx) => {
  console.error("[econ-executor-validate] plugin loaded")

  return {
    "tool.execute.before": async (input, output) => {
      const schema = TOOL_SCHEMAS[input.tool]

      // Tool not in our schema table — pass through
      if (!schema) return

      const reason = validateArgs(input.tool, output.args, schema)
      if (reason === null) return

      const msg =
        `[econ-executor-validate] REFUSED: '${input.tool}' call has a malformed schema: ` +
        `${reason}. Fix the arguments and retry.`

      console.error(msg)
      throw new Error(msg)
    },
  }
}
