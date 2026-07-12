/**
 * econ-output-trim — S2.2 tool output trimming plugin.
 *
 * Registers a tool.execute.after hook that trims large tool outputs (bash,
 * read, grep, glob) to OUTPUT_BUDGET tokens using head+tail truncation.
 * This prevents the model from consuming excessive context tokens on verbose
 * command output or large file reads while still keeping the most relevant
 * slice: the beginning (preamble / imports) and the end (result / exports),
 * with an elision marker in the middle that reports the full token count.
 *
 * Trim policy:
 *   - Budget: 4000 estimated tokens
 *   - Head: 40% of budget (beginning of output)
 *   - Tail: 30% of budget (end of output)
 *   - Middle (match context): 30% of budget when a query is available
 *   - Elision marker: "... [N lines elided — T estimated tokens, budget B] ..."
 *
 * Load mechanism: OpenCode auto-discovers all *.js files under .opencode/plugins/
 * at startup (packages/opencode/src/config/plugin.ts: Glob.scan("{plugin,plugins}/*.{ts,js}")).
 * tool.execute.after is confirmed present in packages/plugin/src/index.ts (line 274).
 *
 * The trim logic (estimateTokens + trimOutput) is an inline JS port of the
 * TypeScript source at packages/code-intelligence/src/trim.ts — kept inline
 * because plugin files are plain .js with no bundler step.
 */

// ---------------------------------------------------------------------------
// Inline port of token-estimator + smart-truncate (pure JS, no imports)
// ---------------------------------------------------------------------------

const CHARS_PER_TOKEN = { code: 3.5, prose: 4.0, json: 3.0, mixed: 3.7 }

function detectContentType(text) {
  if (text.length === 0) return "mixed"
  const sample = text.slice(0, 2000)
  const trimmed = sample.trim()
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  )
    return "json"
  const braceCount = (sample.match(/[{}()[\];=><]/g) ?? []).length
  const wordCount = (sample.match(/\b\w+\b/g) ?? []).length
  const codeIndicators = (
    sample.match(
      /\b(function|const|let|var|class|import|export|return|if|for|while|async|await|type|interface|def|fn)\b/g,
    ) ?? []
  ).length
  if (codeIndicators > 3 || braceCount > wordCount * 0.15) return "code"
  if (braceCount < 5 && sample.split("\n").length < sample.length / 60) return "prose"
  return "mixed"
}

function estimateTokens(text) {
  if (!text || text.length === 0) return 0
  const ratio = CHARS_PER_TOKEN[detectContentType(text)] ?? 3.7
  const ws = (text.match(/\s/g) ?? []).length
  return Math.ceil(ws * 0.25 + (text.length - ws) / ratio)
}

/**
 * Trim text to budget using head+tail strategy. Never cuts mid-line.
 * Returns original string unchanged when it fits within the budget.
 */
function trimOutput(text, budget) {
  const fullTokens = estimateTokens(text)
  if (fullTokens <= budget) return text

  const AVG = 3.7
  const totalChars = budget * AVG
  const ELISION_RESERVE = 100
  const headChars = Math.max(0, Math.floor(totalChars * 0.4) - ELISION_RESERVE)
  const tailChars = Math.max(0, Math.floor(totalChars * 0.3))

  const lines = text.split("\n")

  // Head
  const headLines = []
  let headLen = 0
  for (const line of lines) {
    const len = line.length + 1
    if (headLen + len > headChars && headLines.length > 0) break
    headLines.push(line)
    headLen += len
  }

  // Tail
  const tailLines = []
  let tailLen = 0
  for (let i = lines.length - 1; i >= headLines.length; i--) {
    const len = lines[i].length + 1
    if (tailLen + len > tailChars && tailLines.length > 0) break
    tailLines.unshift(lines[i])
    tailLen += len
  }

  const omitted = lines.length - headLines.length - tailLines.length
  return [
    ...headLines,
    `... [${omitted} lines elided — ${fullTokens} estimated tokens, budget ${budget}] ...`,
    ...tailLines,
  ].join("\n")
}

// ---------------------------------------------------------------------------
// Plugin export
// ---------------------------------------------------------------------------

/** Token budget above which tool output is trimmed. */
const OUTPUT_BUDGET = 4000

/**
 * Tool IDs whose output.output field may be large and should be trimmed.
 * Matches Tool.define first-arg values in packages/opencode/src/tool/.
 *   bash  → ShellTool (shell/id.ts: ToolID = "bash")
 *   read  → ReadTool
 *   grep  → GrepTool
 *   glob  → GlobTool
 */
const TRIMMABLE_TOOLS = new Set(["bash", "read", "grep", "glob"])

/**
 * EconOutputTrimPlugin
 *
 * Registers a tool.execute.after hook that trims large outputs from bash,
 * read, grep, and glob tools to OUTPUT_BUDGET tokens. The elision marker
 * embeds the full estimated token count so the model can assess how much was
 * dropped and issue a targeted follow-up if the missing content matters.
 */
export const EconOutputTrimPlugin = async (_ctx) => {
  console.error("[econ-output-trim] plugin loaded")

  return {
    "tool.execute.after": async (input, output) => {
      if (!TRIMMABLE_TOOLS.has(input.tool)) return
      if (typeof output.output !== "string") return

      const original = output.output
      const trimmed = trimOutput(original, OUTPUT_BUDGET)

      if (trimmed !== original) {
        const before = estimateTokens(original)
        const after = estimateTokens(trimmed)
        console.error(
          `[econ-output-trim] '${input.tool}' output trimmed: ${before} → ${after} tokens`,
        )
        output.output = trimmed
      }
    },
  }
}
