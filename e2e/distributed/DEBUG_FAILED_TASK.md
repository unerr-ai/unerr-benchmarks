# Debug a failed distributed-benchmark task

**Purpose:** an instance finished with `resolved=0` (or ended up `dead`/`failed`) —
find out why, either while the fleet is still live or after drain. This is the
verified, hard-won procedure; follow it in order, don't re-derive it.

See also: [`README.md` §3 Monitor](README.md) for fleet-level status, and the
["Benchmark runbooks"](../../CLAUDE.md) list in the repo-root `CLAUDE.md` for
the other operator docs.

## Step 0 — classify the failure first

**First choice:** `tools/pull_traces.sh <LABEL> --failed-only` (or `--ids <iid>`
for one you already know). It stages every artifact column — `report_json` →
`report.json`, `trajectory_json` → `trajectory.json`, etc. — to
`out/dist-<LABEL>/traces-live/<iid>/`, so you read `status`/`resolved`/
`failure_reason` out of the local `report.json` instead of hand-querying the
coordinator over ssh. It works mid-run for the same reason Step 1 below does:
traces are already durable in `queue.db` the moment a worker POSTs `/complete`.

`status` + `failure_reason` distinguish two fundamentally different cases — and, since
2026-07-21, a `done`+`resolved=0` row splits into two further sub-cases the coordinator
itself now tells apart (`meta_json.silent_death`, Terminal only):

| Signal | Meaning | It's a... |
|---|---|---|
| `status='failed'` / non-null `failure_reason` | crash, timeout, empty patch | **execution failure** — infra problem. Auto-retried at drain (failure-rerun, below). |
| `status='done'` AND `resolved=0` AND `meta_json.silent_death=true` | trajectory's LAST step is an unanswered agent nudge (`steps[-1].source=="user"`), Harbor exits clean (rc=0) | **silent session death** — TRANSIENT, not a real miss. Auto-retried at drain (same budget as `failed`, see §"Failure-rerun" in `README.md` §8.1). |
| `status='done'` AND `resolved=0` AND (no `silent_death` flag, or non-Terminal benchmark) | the agent ran to completion, the **grader** scored it below threshold | **capability/harness gap** — not infra, NOT retried (rerunning it would burn real money re-running a genuine miss — e.g. TB2.1's `chess-best-move`, which fails reproducibly with a different wrong answer every run). |

`status.sh`'s `up4retry` counts BOTH the `failed` row count AND any silent-death-eligible
`done` rows (`/status`'s `pending_reruns` field) — it is no longer just `counts.failed`.

**Fallback** (what `pull_traces.sh` does under the hood over ssh, or if you
just want the raw columns without downloading artifacts) — query the
coordinator `queue.db` `tasks` table directly. Works mid-run (coordinator
machine id from `flyctl machines list -a <APP>` or `status.sh`):

```bash
flyctl ssh console -a <APP> --machine <COORD_MID> -C 'python3 -c "
import sqlite3
c=sqlite3.connect(\"/data/queue.db\")
print(c.execute(\"SELECT instance_id,status,resolved,failure_reason FROM tasks WHERE resolved=0 OR status IN (\\\"failed\\\",\\\"dead\\\")\").fetchall())
"'
```

(`DB_PATH` defaults to `/data/queue.db` —
`e2e/distributed/coordinator/coordinator-entrypoint.sh:59`.)

## Step 1 — where the evidence lives (the key map)

Traces become durable in the coordinator `queue.db` the moment the worker
POSTs `/complete` — the worker reads them into memory **before** deleting its
`/tmp/dist-<iid>-*` scratch (`Worker._process`'s `finally` block calls
`_read_artifacts` then `shutil.rmtree` —
`e2e/distributed/worker/worker-loop.py:334-342`; the read/sync itself is
`Worker._read_artifacts`, `worker-loop.py:565-610`, driven by the active
benchmark's `traces` list — `tools/benchmarks.py:172-176` for
Verified/Pro/Live, `tools/benchmarks.py:266-275` for Terminal).

| When | Where |
|---|---|
| **During a run** | Query the `queue.db` columns directly (Step 0's table, extended below). Worker scratch is **already gone** — do not go looking for it on the worker machine. |
| **After drain** | Same data, written to coordinator disk by `coordinator-entrypoint.sh`'s aggregate step (`coordinator-entrypoint.sh:252-365`): artifacts at `/data/results/<label>/artifacts/<iid>/`, per-instance grade at `/data/logs/grade-merged/<iid>/report.json`, merged grade summary at `/data/reports/merged.<label>.json` — and in the downloaded bundle / Tigris archive under the same `results/<label>/...` layout. |

Columns on `tasks` (`Queue.complete`, `e2e/distributed/coordinator/server.py:255-327`)
and what each ACTUALLY contains:

| Column | Contents | Use it for |
|---|---|---|
| `report_json` | Harbor result: `resolved`, `error`, `exceptions`, full `harbor_result` incl. `stats.evals[*].reward_stats` / `exception_stats` | WHY the grade was 0; reward bucketing |
| `trajectory_json` | **THE AGENT TRANSCRIPT** — `{agent, session_id, schema_version, steps[], final_metrics}` | what the agent actually did, per-step `model_name`, tool calls, gate feedback |
| `err_txt` | copy of Harbor `trial.log` = **SETUP/INSTALL log only** (`tools/harness_terminal.py:476` copies `trial_dir/trial.log` → `art_dir/err.txt`), ending with the `claude ... --append-system-prompt '<the whole prompt>'` invocation | install/setup verification ONLY |
| `harbor_run_log` | stdout/stderr of the `harbor run` subprocess — the only place a SETUP-phase RuntimeError (before the agent ever starts) is captured | setup-phase RuntimeErrors |
| `events_jsonl` | tiny synthesized `{type:harness_result,resolved,ts}` summary | quick resolved check |
| `meta_json` | cost + telemetry (agent, arm, cost, cost_usd, n_*_tokens, rc, telemetry) plus, on Terminal, `silent_death` (bool — see Step 0's table and `harness_terminal.py`'s `_is_silent_death`) | cost/turns/tokens; `silent_death` drives the coordinator's failure-rerun eligibility |
| `sessions_cast` | asciinema session (Terminal only) | replay the terminal session |
| `claude_session_jsonl` | Claude Code's OWN session transcript (`claude-*` arms only), synced by `cc-harness-hooks.py`'s `_sync_claude_session` on every PostToolUse call | **the one trace that survives a trial killed mid-run** — see below |
| `claude_sessions_tgz_b64` | base64 of a gzip tar of **every** Claude Code session `.jsonl` for the trial — main session PLUS every Task sub-agent sidechain (`cc-harness-hooks.py`'s additive `_sync_all_claude_sessions`, packaged by `harness_terminal.py`'s `_collect_traces`) | **the only trace of a Task sub-agent's OWN transcript** — `claude_session_jsonl` above only ever holds the main session, so this is what you need when the ladder (`unerr-opus`/`unerr-fable`) or routine delegation misbehaves. Decoded back to a real `claude-sessions.tgz` under `results/<label>/artifacts/<iid>/` at drain — `tar xzf` it, each member is `sessions/<sessionId>.jsonl` |

**Why `claude_session_jsonl` exists — a trial killed mid-run has NO
`trajectory_json`.** Harbor only writes `trajectory.json` when a trial
COMPLETES. On 2026-07-21 the task `caffe-cifar-10` was killed after
exhausting its `[agent] timeout_sec=3600` budget (`rc=None`,
`finished_at=null`), leaving nothing but a synthesized `events.jsonl` and
making the failure permanently undiagnosable. Claude Code, by contrast,
appends to its own session `.jsonl` incrementally as it runs, and the
sync hook copies it into Harbor's persisted `/logs/agent/sessions/` dir
on every tool call — so it survives a kill that `trajectory.json` does
not. If `trajectory_json`/`err_txt` are empty on a `claude-*` instance,
check `claude_session_jsonl` next before writing the instance off as
undiagnosable. It rides the SAME durability chain as every other trace
column (`Worker._read_artifacts` → `/complete` → `queue.db` →
`coordinator-entrypoint.sh`'s aggregate step →
`results/<label>/artifacts/<iid>/claude-session.jsonl`) and the SAME
Tigris archive sweep (`tools/tigris_archive.py` uploads the whole
`artifacts/<iid>/` tree wholesale — no per-filename allow-list to
maintain), so `tools/tigris-archive.sh get <label> --only traces` retrieves
it after the fleet is torn down exactly like `trajectory.json`.

**`claude_session_jsonl` alone cannot show you escalation — that closed
2026-07-21 too.** `_sync_claude_session`'s main-session SELECTION
deliberately filters OUT every session `.jsonl` marked `isSidechain: true`
— that's correct for picking the ONE coherent main-loop transcript, but it
means every Task sub-agent's own session (routine delegation, AND the
`unerr-opus`/`unerr-fable` escalation ladder, which runs entirely inside a
sub-agent — HARNESS_UNIVERSAL.md §5) was silently discarded, leaving
escalation misbehavior unobservable. `claude_sessions_tgz_b64` closes that
gap additively, without touching the main-session selection: `cc-harness-
hooks.py`'s `_sync_all_claude_sessions` copies EVERY candidate (main +
every sidechain) into the same sessions dir under its own
`<sessionId>.jsonl`, and `harness_terminal.py`'s `_collect_traces` tars +
gzips the whole dir, base64-encodes it, and rides it through the SAME
`_read_artifacts`/`/complete`/`queue.db`/aggregate-step/Tigris chain as
every other trace column. At drain it's decoded back to a real
`results/<label>/artifacts/<iid>/claude-sessions.tgz` — `tar xzf` it and
diff `sessions/<sessionId>.jsonl` member timestamps against
`trajectory_json`'s per-step `model_name` (Step 3 below: `sol`/`sol-high`
on a `claude-gpt` arm) to line an escalation rung up with its own
transcript.

**A killed trial used to lose EVERY nested trace — fixed 2026-07-21, and
worth understanding because the symptom is so misleading.** Harbor writes
`result.json` at TWO depths: a JOB-level one at `<jobs_dir>/<task>/`
(always) and a TRIAL-level one at `<jobs_dir>/<task>/<task>__<hash>/`
(only on COMPLETION). `harness_terminal.py:run()` derived
`trial_dir = os.path.dirname(_find_result_json(jobs_dir))`, and
`_find_result_json` picks the DEEPEST match — so for a killed trial, where
the trial-level file was never written, the deepest match *is* the
job-level one and `trial_dir` silently collapsed to the **job dir**. Every
nested glob below it then missed: `trial.log`→`err.txt`,
`agent/**/trajectory.json`, `agent/**/*.cast`, and
`agent/**/claude-session.jsonl`. The artifacts dir came back holding only
`harbor-run.log` + `events.jsonl`.

The tell is **`err_txt` missing**: `trial.log` is written INCREMENTALLY
from trial start, so it always exists on disk. If `err_txt` is empty the
files were not absent, they were not FOUND — a path-resolution bug, not a
Harbor limitation. Anchoring trace collection on a completion-only file
defeated the whole point of collecting an incrementally-written transcript.
Fix: `_find_trial_dir()` anchors on `trial.log` (deepest match, newest
mtime on ties for retry siblings), falling back to
`dirname(result_path)`. Regression tests in
`tools/test_harness_terminal.py` build both the killed- and completed-trial
layouts on disk.

**CRITICAL GOTCHA — cost real time, do not repeat it:** `err_txt` is NOT the
agent transcript. It ends with the full autonomy prompt passed via
`--append-system-prompt`. Grepping `err_txt` for harness strings —
`unerr:verify`, `Gate `, `escalate`, `unerr-opus`, `grader runs its own
copy` — returns **FALSE POSITIVES**: you are matching the PROMPT TEXT, not
events that occurred. Always analyze `trajectory_json` for agent behavior;
use `err_txt` only to verify install/setup (Step 2).

## Step 2 — verify the harness actually installed (from `err_txt`, legitimately)

Grep `err_txt` for:

- `settings.local.json` (expect **2** matches — project + `$HOME`)
- `permissionDecision` (PreToolUse allow-hook)
- `dangerously-skip-permissions`
- `append-system-prompt`
- `cc-harness-hooks.py deny|record|gate` (all three should be wired)
- `unerr --version`
- `UNERR_ENTITLEMENT`

Note: a `FATAL:` match here is usually the **validator's own guard text**
being installed (`[ -f ... ] || { echo "FATAL: ... missing" >&2; exit 1; }`),
not a fired error — read the matched line in context before concluding
anything.

## Step 3 — analyze the trajectory (the real evidence)

From `trajectory_json`:
- count `steps`, tally per-step `model_name` — on a `claude-gpt` arm the tier
  map is injective, so model IS role: `terra`=main loop, `luna`=Task
  sub-agents, `sol`=escalation rung 1, `sol-high`=rung 2.
- count real harness events in the steps blob: `unerr:verify` (did the agent
  declare a proof command?), `unerr-opus`/`unerr-fable` (escalation actually
  spawned), and the gate block messages `grader runs its own copy` / `fakes
  progress` (Rule T deny), `no evidence any work happened` (Gate Z),
  `previously passed now fails` (Gate R).
- **check `steps[-1].source` FIRST, before reading anything else, on a Terminal
  `resolved=0` row.** `"user"` means the session died silently — Claude Code's
  own "[Your previous response had no visible output...]" nudge went
  unanswered, Harbor exits clean (rc=0, 0 exceptions, reward 0.0), and nothing
  else in `report_json`/`err_txt` looks broken. This is the SAME check
  `harness_terminal.py`'s `_is_silent_death` runs right after the trial (its
  verdict rides as `meta_json.silent_death`, Step 0's table) — the coordinator
  auto-retries these at drain, so a fresh pull may already show the rerun's
  outcome instead. `"agent"` means the session finished normally and was
  simply graded wrong — a real capability miss, not requeued (see Step 0).
  Nudge COUNT alone does not predict failure — a task can absorb several and
  still resolve; only the session *ending* on one does.

**Second gotcha to document:** `final_metrics.total_cost_usd` and
`total_cached_tokens` in the trajectory are **Harbor's own accounting** and
are NOT reliable — they use list pricing and have been observed reporting 0
cached tokens where LiteLLM measured >90% cache. Real cost is ALWAYS LiteLLM
spend (`status.sh --cost`, `meta_json.cost`). Never quote the trajectory's
cost figure.

## Step 4 — live-container inspection (only while the task is still running)

If the task is still leased, read the gate ledger directly inside the
worker's task container:

```bash
flyctl ssh console -a <APP> --machine <WORKER> -C 'sh -lc "CID=\$(docker ps -q|head -1); docker exec \$CID sh -c \"tail -20 /tmp/cc-harness/state.jsonl; ls -la .claude/settings.local.json\""'
```

`/tmp/cc-harness/state.jsonl` (default path, `CC_HARNESS_STATE` env
override — `e2e/reference/claude/local-docker/context/cc-harness-hooks.py:69,90`)
is the append-only outcome ledger written by `append_event`
(`cc-harness-hooks.py:241`): one `{ev:cmd, key, ok, verify}` row per Bash
command (`key` = a content hash of the command, `verify` only set when the
agent suffixed it with the literal `# unerr:verify` marker). This is the
**only** way to watch gates live — it dies with the container.

## Step 5 — after drain

`tools/debug_instance.py` operates on a **downloaded bundle** (not a live
fleet) — use it post-run to pull everything for one instance into a clean
folder plus a one-screen summary:

```bash
tools/debug_instance.py <bundle_dir> <instance_id> [--out <dir>]
# default <out>: <bundle_dir>/debug/<instance_id>/
# gathers: artifacts/* (engine.log, err.txt, events.jsonl, opencode.db),
#          report.json, meta.json, model_patch.diff, dead.json (if dead-lettered)
```

**Check `WHY_FAILED.txt` first — it already applies most of Steps 0/1/3 for
you, per instance, with no manual work.** `tools/collect-failed.py` runs
automatically at the end of every `run-distributed.sh` run (writes
`failed-runs/<label>/<iid>/WHY_FAILED.txt` for every `resolved=0` instance;
also runnable standalone against any bundle) and, until 2026-07-21, printed
the SAME SWE-bench-shaped `FAIL_TO_PASS: N passed, M still failing` /
`patch_exists=` report for a Terminal-Bench instance too — structurally
meaningless there (Harbor grades in-container on reward, there is no patch)
and actively misleading: `chess-best-move` printed `FAIL_TO_PASS: 0 passed, 0
still failing` / `patch_exists=None`, which reads like a grading failure when
the task simply produced a wrong answer. It's now benchmark-aware, branching
on the report.json shape itself (a `harbor_result` key — the field only
`harness_terminal.py`'s `run()` ever writes — not on the run label or
`--benchmark` string, which this tool is never even passed). For a
harness_run failure it reports: Harbor reward + `rc`; whether the trial
COMPLETED or was KILLED mid-run (the `finished_at`/`stats.n_running_trials`/
`n_completed_trials` signature from Step 1, applied automatically — a killed
trial is a fundamentally different diagnosis from a completed-but-wrong one);
which trace artifacts actually exist (`trajectory.json`/`err.txt`/
`claude-session.jsonl`/`harbor-run.log`); and, from `trajectory.json`'s
`steps[-1].source` when present, agent (capability miss, matches Step 3's
check) vs user (silent session death — see Step 0's `meta_json.silent_death`
row) — plus the `err_txt`/cost caveats from Steps 1 and 3, restated inline so
you don't have to re-derive them per instance. resolve_then_grade benchmarks
(SWE-bench Verified/Pro/Live) still get the original FAIL_TO_PASS report,
unchanged. Regression tests: `tools/test_collect_failed.py`.

Get the bundle first with one of:

```bash
tools/pull_results.sh LABEL [APP]              # live/torn-down fleet, sftp-get
tools/tigris-archive.sh get <label> [--only traces|grading|submission|bundle]  # archived run
```
