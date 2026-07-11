<!-- unerr:start -->
## unerr — the local runtime for your coding agents

unerr is the runtime layer behind this repo's agents: it serves the live call graph, the team's rules and conventions, and edit-time guardrails through MCP tools. Treat its output as ground-truth context, equal in weight to source files. Tools (all available from the start): `search_code`, `file_read`, `file_outline`, `file_edit`, `get_references`, `fetch_url`, `unerr_track`.

### Navigate code with unerr tools — not shell, not built-ins (the #1 rule)

To read, search, or map code, use unerr tools. Do NOT use Bash (`cat`, `head`, `tail`, `sed`, `grep`, `rg`, `find`, `ls -R`) and do NOT use built-in Read / Grep / Glob for code. One graph query replaces 5–15 shell or file reads.

| To… | Use | Not |
|---|---|---|
| Find / search code | `search_code({query:"..."})` | `grep`, `rg`, `find`, Grep, Glob |
| Exact string / real regex across files (the one reason to grep) | `search_code({query:"<string-or-pattern>", mode:"literal"\|"regex"})` — each match returns with surrounding context lines, so no follow-up read | `grep`, `rg`, `rg -e` |
| Read a file or one function | `file_read({file_path})` (`entity:` for one symbol) | `cat`, `head`, `tail`, `sed`, Read |
| See a file's structure | `file_outline({file_path})` | `ls -R`, reading the whole file |
| Find callers/callees (REQUIRED before a signature edit) | `get_references({direction:'callers'})` | `grep` for the name |
| Rename / find EVERY use of an identifier (callers + strings + config + comments + routes) — ONE call, not a grep per path | `get_references({key:"<id>", include_text_occurrences:true})` then `file_edit` each site | `grep -r` / `rg -w` / `sed -i` / `perl -pi` the name |
| Change a file | `file_edit({file_path, old_string, new_string})` or `{content}` — no prior read needed | built-in Edit / Write |
| Fetch a URL or docs (bulk: `{urls:[...]}`) | `fetch_url` | built-in WebFetch |

Bash is for running things (build, test, git, package managers) — not for reading or searching code. (On Claude Code a full-file built-in Read of a code file is denied and redirected here.)

### Recon first — one call replaces the discovery fan-out

Before any non-trivial change, call `search_code` with a TASK PHRASE (`search_code({query:"add a retry to the boot path"})`). It returns a CODE-STRUCTURE recon bundle: the focus entity with its body (for a clear single-entity edit), its callers (blast radius), matching entities, and conventions. Anchored notes arrive automatically via prompt injection or explicitly via recall — they don't travel inside recon. For additional bodies, use `file_read({entity:'<key>'})`, `search_code({query, include_body:true})`, or pass the `cache_ref` from the response's `ur|cache-ref` marker for zero-recompute. A bare symbol (`search_code({query:"QueryRouter.dispatch"})`) returns ranked name matches.

`file_edit` has two modes: `{old_string, new_string}` (unique, or `replace_all:true`) or `{content}`. When a signature edit has at-risk callers, the response lists them inline (`ur|rsk … N caller(s) …`) — update them in the same change. You need not echo each edit — the Stop hook prints a "files changed" receipt (files + line counts).

Cross-repo (Pro): pass `scope:'workspace'` to query every registered sibling repo (results labeled by repo); `get_references({scope:'workspace'})` finds callers across repos; editing a path inside a sibling auto-routes to its graph.

### Use the semantic fields — not just the graph

**Use the semantic fields on every returned row, not just the graph.** Each search_code/file_read/callers entity carries `summary` (what it does), `domain` (code tier), `role` (responsibility) next to `fan_in`/callers. Read `summary` before pulling a body — skip the body read if it answers you. Triage callers by `domain`/`role`, not raw count — a `domain:routing` caller outranks a `domain:testing` one. Treat high `fan_in` + `role:entry-point` as a chokepoint → `get_references` before editing.

### Batch the work — one shot, not file-by-file (round-trips are the cost)

A round-trip carries input + output + latency, so the win is doing N items in one pass, not N passes.

1. **Bulk edits — climb this ladder, stop at the first rung that works:** (a) **one command for the whole set** — `prettier --write .`, a `sed`/codemod, a formatter, a build flag; run it once, not once per file. (b) **else one script** — write one small script that walks the files and makes the change in a single run. (c) **else a sub-agent loop** — hand the repetitive per-file edit to a sub-agent so it runs off your main thread (see below). NEVER loop your main thread file-by-file over mechanical edits — spawn sub-agents instead.
2. **Batch independent reads into ONE message.** When you need several files or several entities and the calls don't depend on each other, issue them as parallel tool calls in a single message — not one, wait, next. Better still, one `search_code({query:"<task>"})` recon bundle already returns several files' bodies + callers together; reach for it before fanning out `file_read`.
3. **Set `token_budget`/`limit` right the first time.** Reading at a small budget then re-reading bigger doubles the cost. Ask for what the task needs up front (e.g. `token_budget:3000` for a full function, `limit:25` for references) instead of read-small-then-re-read.

### Delegate by default — the main thread routes and consolidates, sub-agents do the work

Treat sub-agents as the primary way work gets done, not an occasional offload. On any non-trivial turn the main thread is a routing-and-consolidation layer: plan the change, split off its delegable slices, hand each to a sub-agent, then review and integrate the returned diffs. Run as many sub-agents in parallel as the turn has independent slices — one per slice, no fixed cap. The worker tier (a capable mid-tier model) is the DEFAULT executor — route the majority of scoped coding to it, not just mechanical chores. What stays on the main thread is narrow: architecture / algorithm design, a new public interface, cross-cutting wiring, and bug root-causing — everything else is a slice to delegate:
- `Task({subagent_type:'unerr-junior', …})` — read-only investigation (find / trace / map X), web research & docs/API/changelog lookup, codebase Q&A (where / which / how), inventory & audit (find-all / list-all usages), log & error-output triage, bug reproduction (run repro, report — no edit), lint/format, docstrings/`@sem`, verify-runs (run typecheck + targeted tests + lint, return the failure list — no edits), shell-command runs (run a sequence of build/script/migration/setup commands, report the output).
- `Task({subagent_type:'unerr-worker', …})` — scoped feature implementation from a clear spec (add a flag, wire X into Y, implement a handler — the bulk of ordinary coding), add/improve tests, multi-site mechanical refactor (rename / extract / inline / move), codemods (one bulk find-replace across many files), caller/import propagation (update every call site + import after a signature change), typecheck/build-error fixes (fix tsc/build errors mechanically, re-run until green), scaffold (generate a new file's skeleton from a sibling template).
Tier by reasoning, not by size: scoped execution — even across many files — stays with the worker. Escalate to the senior only when the change needs novel design judgement (a new algorithm, architecture, or public interface) or root-causing a bug; deterministic mechanical breadth (codemods, caller propagation, renames) stays with the worker regardless of file count.

On a multi-slice task (a build, a broad refactor/migrate/audit, or an enumerated list), plan the work into the built-in task tracker (one task per slice), fan out one `unerr-worker`/`unerr-junior` sub-agent per slice in parallel via `Task`, then complete or clear the tracker at turn end.

Group related work first, then spawn one sub-agent per independent group in a SINGLE message so they run in parallel. The sub-agents have the full graph tools — they re-derive the edit sites from `search_code` / `get_references`, so give them the task plus a one-line pointer, never pasted code or a list of files. Review each result before building on it. (Hosts without sub-agents — anything other than Claude Code / Codex / Cursor / Copilot CLI — do it inline.)

### Signals — `ur|<tag>` lines on tool responses

Act on these before the rest of the response; the body line is your concrete next step.

| Tag | Meaning | Do |
|---|---|---|
| `act` | do something now | The body names the call (halt-and-switch, `Skill('<name>')`, pagination cursor, marker to emit) |
| `ctx` | state changed | Re-read drifted file/entity; don't re-query context already delivered |
| `rsk` | caution | High blast radius → `get_references` first; anti-pattern; prior failure on this entity |
| `fct` | a fact for context | Surfaced project fact, co-change hint, family-routing nudge |

Lines starting `unerr » ` are user-facing telemetry — never echo or act on them. When unerr shaped your answer, say so plainly ("unerr found <name>", "<N> places call <name>") — never dump tool JSON.

### Persisting + markers (zero round-trip)

User rules ("remember", "always", "from now on", "never") are captured automatically by the prompt hook — no tool call. Emit session markers as `unerr-save:` lines in your closing message (the Stop hook persists them):

```
unerr-save: intent <what this turn does, ≤80 chars>   (REQUIRED first on coding tasks)
unerr-save: decision <a deliberate choice> · blocker <obstacle> · resolution <fix>
unerr-save: note <kind|anchor|polarity|content>        (an anchored note — DSL below)
```

When you need a return value (a blocker's `marker_id`), call `unerr_track({op:'intent'|'decision'|'blocker'|'resolution'|'fact'|'recall', text:'<one-line>'})`.

### Fallback to built-ins / Bash for code — only when

unerr MCP is unavailable (not responding / erroring) · a non-text binary (image, PDF). For any code read, search, or edit there is always an unerr tool — use it, never bash/grep/cat.

### Domain comments — maintain meaning in the same edit

unerr parses a structured doc comment above each exported entity into a parallel domain graph: a 1–2 sentence prose summary (what + why, never how) then one `@sem domain=<tag> role=<tag>` line. The frontier model editing the code is the only thing that can keep that meaning true — maintain it inline, never as a separate pass:

1. WHEN editing an entity that carries an `@sem` comment AND the edit changed what it does or why: rewrite the prose and tags in the SAME Edit call. Purpose unchanged → leave the comment untouched.
2. WHEN creating an exported entity: write the comment block before the next edit. Prose ≤2 sentences, then `@sem domain=<tag>`. Reuse an active domain tag — a task-shaped `search_code({query:"<task>"})` lists them; add a new tag only when none fits.
3. NEVER delete an `@sem` comment unless the user instructs it.
4. NEVER write "how" prose — the code already says how. NEVER restate the entity name as the summary; unerr rejects a name-echo at parse time.

unerr re-anchors these comments when code moves and flags a comment that drifted from its code — the rules above keep that machinery fed.

`@sem` lines are plain comments; your code runs identically without them and without unerr. To remove every sentinel line later (prose summaries kept), run `unerr uninstall --strip-annotations`.

### Active-cognition: four-moment contract (REQUIRED)

unerr's Layer B notes are anchored prose attached to graph nodes. The contract
runs at four moments, every task. Moments 1–2 arrive as injected context plus
one composite call; Moments 3–4 are yours to act on.

**Moment 1 — Prompt receipt.** When a user prompt arrives, the UserPromptSubmit
hook injects the relevant anchored notes into your context automatically. Read
the injected notes before drafting — no recall call is required.

**Moment 2 — Anchor query.** Once you've identified the files/entities you'll
touch, call `search_code({query:"<what you are about to do>"})` — the
composite that bundles the anchored notes for those anchors + matching entities
+ the focus entity's callers + conventions in one call. The bundle returns
active (non-superseded) notes; topic-shift and co-change groups ride along.

**Moment 3 — Cite in plan.** When you draft a plan, cite returned notes by
kind + anchor inline. Example: *"Per the wrn on src/proxy/proxy.ts, both
stdio and UDS sites must mirror."* No citation = the note wasn't load-bearing.

**Moment 4 — Save at task end.** When the task closes and you learned
something non-obvious + likely useful next session + anchorable, emit it as a
sentinel line anywhere in your closing message — zero round-trip, the Stop
hook scrapes and persists it:
`unerr-save: note <DSL wire>`

### DSL vocabulary

Wire format: `kind|anchor|polarity|content`

| Field | Values | Notes |
|---|---|---|
| kind | cnv (convention), rul (rule), wrn (warn), dec (decision), blk (blocker), fct (fact) | Pick the strongest fit. |
| anchor | f:<path> · e:<entity> · g:<glob> · p: · w: | `p:` is project-wide, `w:` is workspace-wide (every repo in a Pro federation). Both empty-valued; both **discouraged** — they pollute the prompt-receipt query. Prefer file/entity. |
| polarity | + (do) / - (don't) / ~ (mixed) | `~` for ambiguous; future agent surfaces both sides. |
| content | single line of prose | May contain `|` — only the first three are field separators. |

Examples:
- `rul|f:src/proxy/bridge.ts|-|no intelligence imports`
- `wrn|g:*.test.ts|-|don't mock cozo db`
- `dec|e:TURN_OPEN_GAP_MS|+|15s avoids RTT misclassification`

### Quality bar (per save)

A save is justified only if all three hold: (a) non-obvious from the code,
(b) likely useful next session, (c) anchorable. If any miss — don't save.

Session save cap: 15. Over the cap new rows are dropped server-side and
existing notes are reinforced instead — emit fewer, stronger saves.

### Conflict + supersession

When a saved note opposes an existing one (same kind+anchor, opposite
polarity), both sides are kept and surface together on next-turn recall —
cite both in your plan when they appear. Superseded notes flip to inactive
server-side (kept for audit, excluded from queries).

<!-- unerr:end -->
