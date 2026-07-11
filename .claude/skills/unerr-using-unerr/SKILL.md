---
name: unerr-using-unerr
description: "Always on. For anything that reads, searches, or edits code, reach for unerr's graph tools first (search_code / get_references / file_read / file_edit), and delegate the work to unerr sub-agents by default — the main thread plans, routes, and consolidates while sub-agents run the slices in parallel (one per independent slice, no fixed cap). Guidance toward the tools and capabilities, not a workflow — there are no fixed steps to run."
---

---
name: unerr-using-unerr
description: "Always on. For anything that reads, searches, or edits code, reach for unerr's graph tools first, and delegate the work to unerr sub-agents by default. This is guidance toward the tools, not a workflow — there are no fixed steps to run."
---

## Use unerr's tools, not bash or built-ins

unerr serves a live code graph plus your team's rules through MCP tools. For anything that reads, searches, or edits code, an unerr tool is the ground-truth path — one graph query replaces 5-15 file reads.

| To… | Use |
|---|---|
| Find code, or pull context for a change | `search_code({query})` — a task phrase returns a CODE-STRUCTURE recon bundle: focus entity (+ body for single-entity edits) + callers (blast radius) + top relevant entities + conventions. For additional bodies, use `file_read({entity})` or `search_code({query, include_body:true})` or `cache_ref` (zero recompute). Anchored notes come via prompt injection or on-demand recall, not inline. A bare symbol returns ranked matches. |
| Exact string / regex across files | `search_code({query, mode:'literal'\|'regex'})` — match + surrounding lines, no follow-up read |
| Who calls it / what it calls (before a risky edit) | `get_references({key, direction:'callers'\|'callees'})` |
| Read a file or one function | `file_read` (`entity:` for one symbol); `file_outline` for structure |
| Change a file | `file_edit` (`{old_string,new_string}` or `{content}`) — no prior read needed |
| Fetch a URL or docs | `fetch_url` (bulk: `{urls:[...]}`) |

Pick the tool that fits the moment. There is no required sequence.

## Use the semantic fields — not just the graph

**Use the semantic fields on every returned row, not just the graph.** Each search_code/file_read/callers entity carries `summary` (what it does), `domain` (code tier), `role` (responsibility) next to `fan_in`/callers. Read `summary` before pulling a body — skip the body read if it answers you. Triage callers by `domain`/`role`, not raw count — a `domain:routing` caller outranks a `domain:testing` one. Treat high `fan_in` + `role:entry-point` as a chokepoint → `get_references` before editing.

## Keep `@sem` doc comments true in the same edit

Domain comment (Layer 8): when you `file_edit` an entity that carries an `@sem` doc comment AND the edit changed what it does or why, rewrite the prose summary and `@sem domain=<tag>` line in the SAME Edit call. NEVER delete an `@sem` comment unless the user instructs it.

## The hooks already do the protective work — don't pay a call to repeat it

Anchored notes (on the prompt), conventions and drift (on read), and the blast-radius gate (on edit) arrive on their own as `ur|<tag>` lines. Read them. Don't spend a `search_code` / `get_references` / `file_read` to re-fetch context you were already handed.

## Delegate by default — the main thread routes and consolidates, sub-agents do the work

Treat sub-agents as the primary way work gets done, not an occasional offload. On any non-trivial turn the main thread is a routing-and-consolidation layer: plan the change, split off its delegable slices, hand each to a sub-agent, then review and integrate the returned diffs. Aim for 2-3 sub-agents running in parallel on a substantive turn. The worker tier is the DEFAULT executor — route the majority of scoped coding to it, not just mechanical chores. What stays on the main thread is narrow — architecture / algorithm design, a new public interface, cross-cutting wiring, and bug root-causing; everything else is a slice to delegate:
On a multi-slice turn (a build, a broad refactor/migrate/audit, or an enumerated list of changes), externalize the plan into the built-in task tracker first — one task per slice — then fan out one sub-agent per task; mark tasks completed and clear the tracker at turn end. The tracker turns an in-head plan into concrete, assignable, closeable slices.
- `Task({subagent_type:'unerr-junior', …})` — read-only investigation (find / trace / map X), lint/format, docstrings/@sem, verify-runs (run typecheck + targeted tests + lint, return the failure list — no edits), shell-command runs (run a sequence of build/script/migration/setup commands, report the output).
- `Task({subagent_type:'unerr-worker', …})` — scoped feature implementation from a clear spec (add a flag, wire X into Y, implement a handler — the bulk of ordinary coding), add/improve tests, multi-site mechanical refactor (rename / extract / inline / move), caller/import propagation (update every call site + import after a signature change), typecheck/build-error fixes (fix tsc/build errors mechanically, re-run until green), scaffold (generate a new file's skeleton from a sibling template).

Consolidate first: group the related work, then spawn ONE sub-agent per independent group, all in a single message so they run in parallel. Give each the task plus a one-line pointer — it re-derives the edit sites from the graph itself; never paste file contents or a list of sites. Review each result before you build on it. (Hosts without sub-agents — anything other than Claude Code / Codex / Cursor / Copilot CLI — do it inline.)

## Batch repetitive work — never a file-by-file loop on the main thread

Same change across many files? One command (`prettier --write .`, a codemod) → else one script → else a sub-agent loop. Independent reads follow the same rule: issue them as parallel calls in ONE message, or pull them together with one `search_code` bundle — never one-read-wait-next. Set `token_budget` / `limit` right the first time.

## Close-out (zero round-trip)

Emit `unerr-save:` lines in your closing message — the Stop hook persists them, no tool call: `intent` (first), then `decision` / `blocker` / `resolution`, and `note <kind|anchor|polarity|content>` for a non-obvious convention. User rules ("remember / always / never") are captured automatically. When unerr shaped your answer, say so plainly ("unerr found <name>", "<N> places call <name>") — never echo `ur|<tag>` lines.

## Output discipline

Lead with the answer; structured summaries over prose; show diffs, not whole files; never repeat context already in the conversation.

