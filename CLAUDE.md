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

Delegation is the default behavior, not something to ask permission for: spawn sub-agents immediately when a turn has delegable work — never ask, never announce intent to ask; the user never needs to say "use unerr sub agents." On any non-trivial turn the main thread is a routing-and-consolidation layer: plan the change, split off its delegable slices, hand each to a sub-agent, then review and integrate the returned diffs. Run as many sub-agents in parallel as the turn has independent slices — one per slice, no fixed cap. The worker tier (a capable mid-tier model) is the DEFAULT executor — route the majority of scoped coding to it, not just mechanical chores. What stays on the main thread is narrow: architecture / algorithm design, a new public interface, cross-cutting wiring, and bug root-causing — everything else is a slice to delegate:
- `Task({subagent_type:'unerr-junior', …})` — read-only investigation (find / trace / map X), web research & docs/API/changelog lookup, codebase Q&A (where / which / how), inventory & audit (find-all / list-all usages), log & error-output triage, bug reproduction (run repro, report — no edit), lint/format, docstrings/`@sem`, post-edit code review when unerr-reviewer is unavailable, security audits, git operations (branch/PR prep), benchmark/profiling runs, verify-runs (run typecheck + targeted tests + lint, return the failure list — no edits), shell-command runs (run a sequence of build/script/migration/setup commands, report the output).
- `Task({subagent_type:'unerr-worker', …})` — scoped feature implementation from a clear spec (add a flag, wire X into Y, implement a handler — the bulk of ordinary coding), add/improve tests, multi-site mechanical refactor (rename / extract / inline / move), codemods (one bulk find-replace across many files), caller/import propagation (update every call site + import after a signature change), typecheck/build-error fixes (fix tsc/build errors mechanically, re-run until green), scaffold (generate a new file's skeleton from a sibling template), dependency upgrades, migration scripts.
Tier by reasoning, not by size: scoped execution — even across many files — stays with the worker. Escalate to the senior only when the change needs novel design judgement (a new algorithm, architecture, or public interface) or root-causing a bug; deterministic mechanical breadth (codemods, caller propagation, renames) stays with the worker regardless of file count.

On any task with 2+ independent slices, call `TaskCreate` for each slice before the first edit — unprompted, never wait to be asked — then call `TaskUpdate` to mark each slice completed as it lands. Fan out one `unerr-worker`/`unerr-junior` sub-agent per slice in parallel via `Task`.

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

<!-- benchmark-runbooks: kept OUTSIDE the unerr-managed block so `unerr install` won't wipe it -->
## Benchmark runbooks (keep these updated on any change to the run/results flow)

- **Multi-benchmark + matrix orchestration hub (any arm × any benchmark, all at once):**
  [`e2e/distributed/README.md` §8](e2e/distributed/README.md) — the operator doc for the
  `BENCHMARK=verified|lite|pro|terminal|live_verified` axis and the matrix tooling: `bench.sh` (fire any subset of
  `arm:benchmark` combos as independent LABEL-scoped fleets, parallel or `--seq`, modes
  run/prepare/start/destroy — `run` = full one-shot per combo, `prepare`+`start` bracket a GPU
  window — writes `out/bench-<matrix>/manifest.tsv`), `gpu-flip.sh` (you raise
  dedicated Fireworks GPUs per tier and pass ids; it flips the `econ-litellm` gateway secrets —
  conductor/oracle/reasoner/executor — never launches GPUs itself), and `download-all.sh` (pull every
  fleet's results+traces via the matrix manifest, `--submission` per resolve_then_grade combo).
  **Monitor a live run** with the two read-only scripts (both take `<LABEL> [APP]`, `--matrix <id>`, or
  no-args = newest matrix, single-sourcing fleet lookup via `tools/fleet-common.sh`): `status.sh`
  (per-fleet `/status` one-liner — armed?, workers_seen, pending/leased/done/dead/failed, resolved/total +
  grade % + retries/up-for-retry/dead; `--watch` = live monitor, `--instances` = per-instance→worker table,
  `--cost` = total $ + per-tier conductor/oracle/reasoner/executor token·turn·cost read live from the
  coordinator queue.db meta — econ telemetry/tier_cost_db, claude litellm_spend_logs, always real LiteLLM
  spend) and `debug-workers.sh` (per worker:
  what it holds + a `»»`-flagged `flyctl logs` tail; `--follow` streams, `--grep`/`--lines`/`--instance`).
  See distributed README §3. Per-benchmark contract lives in `tools/benchmarks.py` (dataset/images/grade/grade-cap/traces/flow).
- **Claude arm on SWE-bench (unerr ON + open-weight ensemble, distributed on fly):**
  [`e2e/reference/claude/fly-remote/README.md`](e2e/reference/claude/fly-remote/README.md)
  — the authoritative runbook: exact `run-distributed.sh` command (incl. the mandatory
  `ROOTFS_GB=50` + `CPU_KIND=performance` knobs), model map, re-vendoring, prepare/run/arm
  split, env vars, gotchas, monitoring, and **§11 Download & process results** (the reusable
  `tools/` scripts: `pull_results.sh`, `cost_report.py --detailed`, `make_submission.py`,
  `debug_instance.py`).
- **SWE-bench Pro image mirror (own private copy of Scale AI's instance images):**
  [`e2e/swebench-pro/README.md`](e2e/swebench-pro/README.md) — on-demand, idempotent,
  resumable `crane` mirror of `jefzda/sweap-images` (~1002 tags) into
  `51jaswanth15/sweap-images`, so the Pro eval runs against an account we control
  (`swe_bench_pro_eval.py --dockerhub_username=51jaswanth15`). Run it whenever:
  `./mirror-sweap-images.sh` (needs a **Read & Write** Docker Hub PAT). This is a
  Docker-Hub→Docker-Hub image mirror, distinct from the fly Tigris pull-through cache
  in `e2e/distributed/registry/`.
- **SWE-bench-Live (verified split) benchmark:** `BENCHMARK=live_verified` — same
  `resolve_then_grade` flow as Verified/Pro, but against HF `SWE-bench-Live/SWE-bench-Live` split
  `verified` (500 frozen ids, not the rolling live split). Descriptor `_LIVE_VERIFIED` in
  `e2e/distributed/tools/benchmarks.py`. Per-instance images are the public
  `starryzhang/sweb.eval.x86_64.<key>` namespace (Live's harness hard-codes it — no private mirror),
  fronted by the `SWEBENCH_REGISTRY_MIRROR` pull-through cache like Verified. Graded by Live's OWN
  vendored harness via `e2e/distributed/tools/grade_live.py` (`grade_module=grade_live`; harness
  pinned in `Dockerfile.dist` at `/work/swebench-live`), not stock swebench. Two non-obvious bake
  requirements there (both learned from a failed smoke): the Live harness installs into an ISOLATED
  `/work/.venv-live` (its own package is confusingly named `swebench` v1.0.0 and would clobber the
  real `swebench>=4.1` the Verified/Pro grade path needs), and its `launch/` git submodule
  (`microsoft/RepoLaunch`) must be init'd recursively (an un-init'd clone → `ModuleNotFoundError:
  launch.core`). Smoke suite:
  `live_verified-mini` (5 ids). See distributed [README §8.2](e2e/distributed/README.md).
- **Archive a run's data to Tigris (relieve the fleet, keep the traces/grades/submission):**
  [`e2e/distributed/README.md` §9](e2e/distributed/README.md) — opt-in coordinator-side upload of every
  run's DATA (execution traces, grading, submission, logs, a generated `overview.json`, `bundle.tgz` — never
  agent source) to a Tigris bucket at end-of-run, sorted `<prefix>/<benchmark>/<arm>/<date>/<label>/<category>/`,
  so coordinator+workers can be destroyed and the run stays lookup-able. Uploader:
  `tools/tigris_archive.py` (runs on the coordinator, also standalone against a pulled bundle; `boto3`;
  builds `overview.json` = grade% + real-LiteLLM cost-by-tier the same way as `status.sh --cost`). Enable
  per-run: `ARCHIVE_TIGRIS=1 TIGRIS_BUCKET=swebench-dist-archive` on `run-distributed.sh`/`bench.sh` (default
  off, non-fatal on failure, `terminal`→`--no-submission`). One-time provisioning (billable, prints S3 keys
  once → run it yourself): `FLY_ORG=<team-org> ./provision-tigris.sh` (creates the bucket, writes gitignored
  `.env.tigris`). Apps are now per `ARM × BENCHMARK` (`swebench-dist-<arm>-<slug>`, slug = benchmark
  key with `_`→`-` and `verified`→`verif` — fly's abuse filter blocks app names containing "verified"
  — created on demand —
  see the distributed README §0/§8.1), so `run-distributed.sh` auto-stages the AWS_* secrets from
  `.env.tigris` onto each combo's app itself at prepare time (idempotent, non-fatal). Look runs up with no live fleet:
  `./tools/tigris-archive.sh list|overview <label>|get <label> [--only traces|grading|submission|bundle]`
  (creds from env or `.env.tigris`; never prints secrets). Wiring lives in `coordinator-entrypoint.sh`
  §6.9+§8, `Dockerfile.dist` (`boto3`), `run-distributed.sh` (env passthrough — never AWS_*).
- **Harbor + Claude Code custom agents (the `claude-<mix>` / `claude-native` terminal arms):**
  [`e2e/distributed/HARBOR_CLAUDE_CODE.md`](e2e/distributed/HARBOR_CLAUDE_CODE.md) — the
  agent-integration deep-dive for driving Claude Code as a *custom Harbor agent* (`ClaudeUnerrAgent`,
  subclassing Harbor's `ClaudeCode` and reusing its `run()`). Covers the FOUR root-caused fixes
  (root permission bypass → `--dangerously-skip-permissions` + `IS_SANDBOX=1`; tier flatten →
  **empty `--model`**; missing alias forwarding → `ENV_VARS` → the `claude-opus-4-8` 400; and the
  **sub-agent permission gap** → a **PreToolUse auto-approve hook** in `.claude/settings.local.json`,
  since `--dangerously-skip-permissions` does NOT reach Task sub-agents — proven version-independent),
  the GPT-5.6 gateway tier map, the local `harbor run` repro loop, and the main-vs-subagent
  denial-split debug playbook. `unerr install claude-code` sets up the unerr MCP + active-cognition
  hooks only — NOT tool permissions, which is why the harness owns the bypass. Reusable for wiring
  Claude Code into ANY other Harbor benchmark. Update it in the SAME change as any
  agent-integration/fix edit.
- **Arm naming scheme (`claude-<mix>`, 2026-07-20):** `ARM` = `econ` | `claude-<mix>` (gateway
  ensemble via the econ-litellm gateway; `<mix>` names the models — `claude-gpt` = GPT-5.6,
  `claude-open` = open-weight) | `claude-native` (real Anthropic, OAuth, no gateway). Legacy
  `claude`/`claude-real` are auto-normalized to `claude-open`/`claude-native` by
  `run-distributed.sh` + `bench.sh` + `fleet-common.sh`. The per-mix MODEL MAP is a single bash
  `case` in `run-distributed.sh` (the source of truth; `ANTHROPIC_DEFAULT_*_MODEL` env overrides;
  unknown mix w/o override = fail-loud). **Adding a mix = one `case` arm there + document it** — the
  runner/worker/coordinator code treats every `claude-<mix>` uniformly as a gateway arm (predicates,
  not literal names) and reads the map from env, so no other code changes. All `claude-*` arms share
  one toolbox image (`unerr-claude-toolbox`); each gets its own fly app + Tigris path. Detail:
  distributed README §0 "Arm naming scheme".
- **Rule:** when you change the distributed run flow, the model map, or the result scripts,
  update that README in the SAME change. The result scripts live in
  `e2e/distributed/tools/` (+ `e2e/reference/claude/local-docker/cost_report.py`) — extend
  them for new data needs rather than writing throwaway parsers.
