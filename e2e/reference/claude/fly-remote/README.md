# Running the Claude arm on SWE-bench — unerr ON + open-weight ensemble, distributed on fly

**This is the single source of truth for running the Claude Code agent against SWE-bench
with unerr's MCP tools ON and the model tiers overridden to an open-weight ensemble via
the LiteLLM gateway, distributed across a fly fleet.** Written after repeatedly
re-deriving these commands (and burning a run on a wrong VM default). If the run
process or config changes, **update this file** — it is referenced from the repo-root
`CLAUDE.md` for exactly that reason.

The runner code lives in `e2e/distributed/` (shared by the econ arm); this doc is the
claude-arm operator guide for it. The per-instance pipeline lives in
`e2e/reference/claude/local-docker/` (baked into the fleet image).

---

## 0. TL;DR — the command that works

```bash
cd e2e/distributed
MACHINES=2 ARM=claude LABEL=<run-name> SUITE=mini FLY_ORG=vamsee-k-933 \
  CPU_KIND=performance ROOTFS_GB=50 \
  SWEBENCH_REGISTRY_MIRROR=http://swebench-registry.flycast:5000 \
  ./run-distributed.sh
```

- `SWEBENCH_REGISTRY_MIRROR` points every worker's dockerd at the shared pull-through
  registry cache (`e2e/distributed/registry/`, a long-lived fly app `swebench-registry`).
  Its value is **Docker-Hub rate-limit protection** (100 anonymous pulls/6h per IP would
  stall a multi-worker fleet) — NOT pull wall-time (a `docker pull` is
  extraction-bound, not download-bound). Omit the var if the registry app is down; the
  fleet still works, just against Docker Hub directly.
- `ROOTFS_GB=50` is **NOT optional** — see the gotcha in §9. The default outer-container
  rootfs is **8 GB**, and the in-VM toolbox build fills it to 100% (`none 7.8G 7.8G 0
  100% /`); the SWE-bench instance builds then die with DinD `input/output error` →
  **every instance dead, 0 resolved**. `ROOTFS_GB=50` grows the outer rootfs to 49 GB
  (46 GB free after warm). This is the real DinD-failure fix — verified by `df -h /` on a
  worker.
- `CPU_KIND=performance` gives dedicated cores (avoids the daemon event-loop starvation
  that drops Claude's first MCP call → heartbeat stall). Keep it, but note it does **not**
  fix the disk failure on its own — the disk fix is `ROOTFS_GB=50`.
- Both LiteLLM keys and `CLAUDE_OPEN_MODELS=1` are auto-wired for `ARM=claude` — you do
  not pass them (see §5, §6).
- Omit `IMAGE=` to bake fresh from live source (default). To reuse an existing image and
  skip the ~10–15 min bake, see §7.
- Expect **~45–120 min** wall-clock for Mini-10 on 2× performance-8x (§8).

---

## 1. What this actually runs

- **Agent:** Claude Code CLI (`claude -p`, headless) resolving one SWE-bench instance per
  container, with `--dangerously-skip-permissions` and unerr's MCP server wired via
  `--mcp-config .mcp.json --strict-mcp-config`.
- **unerr ON:** `unerr install claude-code` runs inside each instance and writes
  `.mcp.json`, `.claude/settings.json` (hooks), and `CLAUDE.md` (the operator manual with
  the delegate/track/recon policy). The graph is indexed in-container (index + scip).
- **Open-weight models:** Claude Code's tier slots are redirected through
  `ANTHROPIC_BASE_URL=https://econ-litellm.fly.dev` to the LiteLLM gateway. The main
  agent loop runs on the **sonnet slot = the conductor**. See §3.
- **Distributed:** `run-distributed.sh` bakes ONE fleet image, starts a coordinator
  (SQLite work-queue on a small volume) + N workers (each a DinD host that builds and
  runs instance containers), grades each instance live, bundles, and tears down.

---

## 2. One-time prerequisites

| Thing | Where / value |
|---|---|
| fly auth | `flyctl auth whoami` → your fly account |
| fly org | `vamsee-k-933` (team/shared) — pass `FLY_ORG=vamsee-k-933` |
| fly app | `swebench-agent-dist-claude` (auto-created/reused) |
| LiteLLM API key (conductor virtual key) | auto-read from `e2e/econ/.env.local` (`LITELLM_API_KEY`, len 56) |
| LiteLLM MASTER key (mints per-instance keys — claude arm only) | auto-read from `../econ-coding-agent/infra/litellm/.env.local` (`LITELLM_MASTER_KEY`, len 56) |
| unerr-cli checkout (to re-vendor) | `../unerr-cli` (i.e. `/Users/<you>/IdeaProjects/unerr-cli`) |
| gateway health | `curl -s https://econ-litellm.fly.dev/health/liveliness` → `"I'm alive!"` |

**Never disturb the econ arm** or its running distributed jobs. The claude overrides
apply only to the benchmark's Claude instances, never to machine-level `~/.claude`.

---

## 3. The open-weight model map

Set in `e2e/reference/claude/local-docker/run-benchmark.py:280-287`; overridable per-env.

| Claude tier slot | Env var | Default model | Role in the ensemble |
|---|---|---|---|
| sonnet (main loop) | `ANTHROPIC_DEFAULT_SONNET_MODEL` | `minimax/minimax-m3` | **conductor** — runs the whole turn |
| opus | `ANTHROPIC_DEFAULT_OPUS_MODEL` | `deepseek/deepseek-v4-pro` | **reasoner** — escalation (`unerr-opus`) |
| haiku | `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `openai/gpt-oss-120b` | **fast** — cheap sub-tasks |
| fable | `ANTHROPIC_DEFAULT_FABLE_MODEL` | `z-ai/glm-5.2` | **oracle** — escalation (`unerr-fable`) |

- Gateway model ids are **provider-prefixed** (`minimax/minimax-m3`, not `minimax-m3`).
- `claude -p` runs with `--model sonnet` when open-models is on, so the main agent loop =
  the minimax conductor. Escalation (spawning `unerr-opus`/`unerr-fable` subagents) is
  what pulls in deepseek/glm; in practice it rarely fires (conductor solos most turns).
- `minimax-m3` **reasons by default** at the provider level regardless of any client
  thinking param — reasoning is ON without `MAX_THINKING_TOKENS`.

---

## 4. Re-vendoring the latest unerr-cli (do this when unerr source changed)

The fleet image bakes a packed unerr tarball (`context/unerr-ai-unerr-*.tgz`). It ships
the MCP server tools **and** the `CLAUDE.md` manual that `unerr install` writes. To test
the latest unerr code you must re-pack it **before** the bake.

```bash
cd e2e/reference/claude/local-docker
UNERR_PROD_BUILD=0 ./build-toolbox.sh      # UNERR_REPO auto-resolves to ../../../../../unerr-cli
```

- Builds the **working tree as-is** (uncommitted changes included) → that IS "latest".
  `UNERR_PROD_BUILD=0` = dev build (all `__UNERR_DEV_BUILD__` features ON) — the intended
  benchmark binary.
- Produces `context/unerr-ai-unerr-<version>.tgz`, injects `scripts/build-contracts.mjs`
  (postinstall no-op fix), refreshes `dev-entitlement.mjs`, and builds a LOCAL
  `unerr-claude-toolbox` image (a free install-validation; not used by the fleet).
- The tgz filename is **globbed everywhere** (`unerr-ai-unerr-*.tgz`) — a version bump
  needs no Dockerfile edits.
- Validate it packed the latest: `tar xzf context/unerr-ai-unerr-*.tgz` and grep the
  `package/dist` bundle for a string from your newest change.
- Then run with a **fresh bake** (omit `IMAGE=`) so `Dockerfile.dist:55` COPYs the new
  tgz into the fleet image.

---

## 5. The run commands

### 5a. All-in-one (recommended for Mini-10; no dedicated GPU needed)

```bash
cd e2e/distributed
MACHINES=2 ARM=claude LABEL=mini10-run1 SUITE=mini FLY_ORG=vamsee-k-933 \
  CPU_KIND=performance \
  ./run-distributed.sh
```

Bakes → creates coordinator + workers → arms → polls `/status` → pulls bundle → **tears
down the fleet automatically**.

### 5b. Prepare / run / arm split (only if using a dedicated GPU)

Only worth it when `DEDICATED_CONDUCTOR=1` raises the $80/hr Fireworks GPU — the split
warms the workers (toolbox build) BEFORE the GPU comes up. For the open-weight ensemble
on **serverless minimax (default), skip this — there is no GPU to protect.**

```bash
# 1) build image + create fleet + warm workers, HOLD at the armed=0 gate (no GPU up)
MACHINES=2 ARM=claude LABEL=mini10-run1 SUITE=mini FLY_ORG=vamsee-k-933 \
  CPU_KIND=performance ./run-distributed.sh prepare      # ~16 min, prints "PREPARED"

# 2) flip the gate + poll + bundle + teardown
LABEL=mini10-run1 ARM=claude FLY_ORG=vamsee-k-933 ./run-distributed.sh run

# (or just flip the gate and poll later)
LABEL=mini10-run1 ./run-distributed.sh arm
```

### 5c. Suite / instance selection

- `SUITE=mini` → the 10-instance Mini set (django 11790, 11815, 11848, 11880, 11885,
  11951, 11964, 11999, 12039, 12050), hardcoded in `distributed/tools/suite.py`.
- `SUITE=<other>` or `TASKS="id1,id2,..."` / `--ids-file <path>` to override.
- Full Verified = the default (no SUITE) — 500 instances; do NOT run casually.

---

## 6. Environment variables — required vs auto

| Var | Required? | Notes |
|---|---|---|
| `ARM=claude` | **yes** | selects the claude pipeline (default is econ) |
| `LABEL=<name>` | **yes** | names the fleet, the coordinator volume, and `out/dist-<label>/`. Use a fresh name per run. |
| `MACHINES=2` | **yes** (all-in-one/prepare) | worker count |
| `SUITE=mini` | for Mini-10 | else full Verified |
| `FLY_ORG=vamsee-k-933` | **yes** | the team org |
| `CPU_KIND=performance` | **effectively yes** | **default `shared` fails** — see §9 |
| `CLAUDE_OPEN_MODELS=1` | auto | injected for `ARM=claude` (`run-distributed.sh:609-610`) |
| `LITELLM_MASTER_KEY` | auto | read from infra `.env.local`; injected into workers |
| `LITELLM_API_KEY` | auto | read from `e2e/econ/.env.local` |
| `ANTHROPIC_BASE_URL` | auto | defaults to `https://econ-litellm.fly.dev` for claude |
| `IMAGE=` | no | unset = fresh bake (default). Set to reuse — §7 |
| `DEDICATED_CONDUCTOR=1` | no | raises the $80/hr GPU; NOT needed for serverless minimax |
| `ROOTFS_GB` | no | ephemeral rootfs size (max 50); only if a disk-starve recurs on top of `performance` |
| `SWEBENCH_REGISTRY_MIRROR` | recommended | `http://swebench-registry.flycast:5000` — workers' dockerd mirrors docker.io through the shared cache (`lib/boot.sh:74-83`); registry app must be deployed (see `e2e/distributed/registry/README.md`) |
| `PER_INSTANCE_TIMEOUT` | no | default **10800** (3 h) per-task resolve ceiling; claude budget = timeout − 1200 s (`run-benchmark.py:111`). Hard tasks that hang are handled by the stall watchdog, not a longer ceiling. |
| `STALL_KILL_S` | no | default **2700** — the worker kills a resolve whose logs show ZERO progress (captured log not growing AND the `HB events_bytes=` heartbeat value not advancing) for this many seconds; the attempt then re-leases once (`MAX_ATTEMPTS=2`) = automatic stop-&-restart of stuck instances |

---

## 7. Fresh bake vs reusing an image

- **Fresh bake (default, omit `IMAGE=`):** `flyctl deploy --build-only --remote-only`
  on fly's depot builder. ~10–15 min (layers cache well on re-bake). REQUIRED after any
  edit to `e2e/reference/claude/local-docker/**` (prompt, run-instance.sh) or a re-vendor
  (§4) — those are COPYd into the image at `Dockerfile.dist:55`.
- **Reuse (`IMAGE=registry.fly.io/swebench-agent-dist-claude:dist-<ts>`):** skips the
  bake. Safe when source is unchanged since that image was built. Find the exact ref in
  the prior run's `out/dist-<label>/build.log` (grep `registry.fly.io/...`). Destroying
  machines does NOT delete the pushed image.

Example reuse (e.g. re-running only because the VM size was wrong):
```bash
MACHINES=2 ARM=claude LABEL=mini10-run2 SUITE=mini FLY_ORG=vamsee-k-933 \
  CPU_KIND=performance \
  IMAGE=registry.fly.io/swebench-agent-dist-claude:dist-1784089905 \
  ./run-distributed.sh
```

---

## 8. Coordinator, workers, timings

**Coordinator** (`shared-cpu-1x:1024MB`, one 10 GB volume `dist_coord_<label>` in `iad`):
runs the SQLite queue server. Key mechanics:
- `armed` gate: `prepare` starts it `armed=0`; `run`/`arm`/all-in-one POST `/arm` to release.
- `/status`: live JSON — `counts{pending,leased,done,dead}`, `resolved`, per-instance
  `status`/`attempt_count`/`worker_id`. `resolved` is computed LIVE from graded reports.
- Beacons (in `out/dist-<label>/`): `seeded` → `drained` → `aggregated` → `grade_done` →
  `bundle_ready`.

**Workers** (`performance-8x:16384MB`, **no volume** — DinD data-root on ephemeral
rootfs): each is a DinD host. Per instance: pull SWE-bench base image → build instance
image (`COPY --from=unerr-claude-toolbox`) → `run-instance.sh` (`unerr install` → index +
scip → `claude -p`) → grade → POST report. Created with `--restart no` (a crash or a
drained queue stops the machine; it does not restart).

**Typical timings (Mini-10, 2× performance-8x):**

| Phase | Time |
|---|---|
| Fresh bake (depot) | ~10–15 min (faster cached) |
| Worker warm: toolbox build in-VM (once/worker) | ~600–700 s |
| unerr index + scip (per instance) | index ~330 s + scip ~300 s |
| `claude -p` resolve (per instance) | ~9–45 min (minimax; 11885 is the long pole ~43 min) |
| Grade (per instance) | ~1–3 min |
| **Total Mini-10 wall-clock** | **~45–120 min** (2 workers × ~5 instances serial) |

`prepare` alone ≈ 16 min (bake + warm). Per-instance timeout = 10800 s (3 h); MAXWAIT =
48 h; bundle HOLD = 1 h. A stuck resolve doesn't burn the full ceiling: the worker-loop
stall watchdog (`_watch_resolve`) polls every 30 s and kills the resolve subprocess after
`STALL_KILL_S` (default 2700 s) of zero progress — progress being either the captured
log growing or the in-container `HB events_bytes=` heartbeat value advancing — so the
coordinator re-leases the instance instead of the worker hanging on a wedged
claude/MCP call for hours.

---

## 8.5. Mechanical gates (open-models arm only)

Enforcement, not prose: appended WORK PROTOCOL text alone never made the conductor
escalate (§12), so the harness now mechanically gates the same behaviors.

- **Installed via** `.claude/settings.local.json`, written by `run-instance.sh` step
  3.15 — never touches unerr's `settings.json` (unerr owns that file); Claude Code
  UNIONS the `PreToolUse`/`PostToolUse`/`Stop` hook arrays from both files, so this
  only adds hooks, it can never clobber unerr's.
- **PreToolUse `deny`** (`cc-harness-hooks.py deny`):
  - Rule T — test files are read-only (hard deny; the grader runs its own tests, so
    editing them can only fake-green a failure; kills the edit-tests-to-pass class,
    e.g. 11885).
  - Rule B — ≥5 edits to one file with no green verification in between → deny with
    an imperative to spawn `unerr-opus` + `unerr-fable` (max 2 fires) — the mechanical
    escalation forcing function; prose triggers measurably never fire.
  - Rule C — convention divergence: introducing `datetime.now(` into a file that
    already uses `utcnow` denies once with an evidence-cited re-apply path (the exact
    11848 fatal token class).
- **PostToolUse `record`** — silent event recorder → `/tmp/cc-harness/state.jsonl`.
- **Stop `gate`** — Z (no edits) / R (regression) / V (unverified, cap 2) / E
  (escalation trigger hit but never escalated; arms on hot-file ≥3 edits OR an
  R-block OR ≥2 V-blocks). Overall cap 3, fail-open.
- `HARNESS_SUMMARY {...}` is logged to stderr at the end of `run-instance.sh` and
  survives into `meta.jsonl`'s `stderr_tail` (the bundle drops artifacts, so this is
  the per-instance gate/deny evidence).
- Escalation mechanics preflight: the Task→`unerr-opus` (deepseek) / `unerr-fable`
  (glm) spawn+billing path is verified working (isolated local check with
  `CLAUDE_CONFIG_DIR`); the historical $0 escalation was the conductor never
  *choosing* to escalate under conditional prose — hence Rule B.

---

## 9. GOTCHAS / known failure modes

1. **Default 8 GB outer rootfs fills to 100% → total DinD failure (THE big one).** The
   worker's **outer** container rootfs (`none` mounted at `/`) defaults to 8 GB, and the
   in-VM toolbox build (pnpm install/build/pack of the whole unerr-cli monorepo, plus the
   baked image content) fills it: `df -h /` shows `none 7.8G 7.8G 0 100% /`. Every
   subsequent SWE-bench instance build then throws `mkdir /var/lib/docker-dind/...:
   input/output error` (and `Can't close tar writer: io: read/write on closed pipe`) —
   even though the DinD data-root on `/var/lib/docker-dind` (loop0, 42 G) has plenty free.
   Every instance dies (`counts{dead:10}`, `n_preds:0`, resolved 0/10), workers exit
   `exit_code=0` ("queue drained"), and `claude -p` never runs — so it looks like a
   benchmark result but is pure infra. **Always pass `ROOTFS_GB=50`** (grows `/` to 49 G).
   This failed identically on BOTH `shared-cpu-8x` and `performance-8x` before the disk
   fix — `CPU_KIND` is orthogonal (it fixes daemon starvation, not disk). Confirm the fix
   took: after `prepare` warms, `flyctl ssh console --machine <w> -C "df -h /"` must show
   ~49 G total / ~46 G avail, not 7.8 G.
2. **Stale `IMAGE=` ships old code silently.** The script warns "REUSING PREBUILT — NOT
   baking from live source." After ANY `local-docker/**` edit or a re-vendor, omit `IMAGE=`.
3. **Empty patches / `UnknownError` on turn 1** = a gateway/model hiccup, not the runner.
   Check `econ-litellm` health and the minted key's `/spend/logs`.
4. **Leftover `stopped` machines** from prior images linger in `flyctl machine list`
   (harmless; different image tags). The current run's machines carry
   `--metadata fleet=<label>`; teardown targets only those.
5. **The bundle cost report:** the coordinator routes the claude arm to
   `local-docker/cost_report.py` (arm-aware). If a bundle ever shows `$0.00`
   everywhere, regenerate: `python3 e2e/reference/claude/local-docker/cost_report.py
   out/dist-<label>/bundle/results/<label> --mode on --grade <merged.json>`.
6. **"cost" always means the real LiteLLM spend** (gateway `/spend/logs`), never the
   Anthropic-priced `total_cost_usd` (which is renamed `telemetry.usd_anthropic_priced`).
7. **A stuck instance (MCP hang, silent stall) no longer burns its full ceiling** — the
   stall watchdog kills at `STALL_KILL_S` of zero log growth and the attempt re-leases
   once. If an instance dead-letters with `stalled:` in its error, it stalled twice.

---

## 10. Monitoring a live run

```bash
APP=swebench-agent-dist-claude

# coordinator status (authoritative, parsed)
flyctl logs -a "$APP" --no-tail | grep -oE '\[dist-coordinator\] status .*' | tail -1

# worker states (performance-8x + started == healthy)
flyctl machine list -a "$APP" | grep dist-<ts>

# SSH into a worker to see the live instance container (unerr index/scip/claude timeline)
flyctl ssh console -a "$APP" --machine <worker-id> -C "docker ps"
flyctl ssh console -a "$APP" --machine <worker-id> -C "docker logs --timestamps <cid>"
```

The launcher's own poll log lands in the terminal / your redirected logfile; its
`progress:` lines can lag the coordinator by a poll — trust the coordinator `/status`.

---

## 11. Download & process results

The all-in-one / `run` path already pulls the bundle to
`e2e/distributed/out/dist-<label>/bundle/` and grades it. **Use the prebuilt scripts
below for everything else — do not hand-roll one-off parsing each time.** They live in
`e2e/distributed/tools/` (+ the arm-aware cost report in `local-docker/`). All reuse the
run's own parsers (`collect-failed.py::_load_preds/_load_dead`, `cost_report.py::instance_row`)
by file-path import, so their numbers match the bundle exactly.

**Bundle layout** — `out/dist-<label>/bundle/`:
`results/<label>/meta.jsonl` (per-instance `telemetry{turns,tokens,tools}` + `cost{by_tier,by_model}`),
`results/<label>/preds.json` (SWE format), `dead.jsonl`,
`reports/merged.<label>.json` (swebench grade). **Note:** the default distributed bundle
carries **no** per-instance `artifacts/<iid>/` (engine.log/events.jsonl/opencode.db) —
the aggregate summary shows `n_artifacts: 0`. Only `report.json` + `meta` + `model_patch`
survive to the bundle; for turn-by-turn engine logs you must SSH a **live** worker during
the run (see §11d).

### 11a. Re-pull a bundle from a LIVE fleet (the `arm` flow, not `run`)

```bash
# pulls /data/bundle.tgz off the coordinator via sftp -> out/dist-<label>/bundle/
# needs the coordinator STILL RUNNING. If the fleet is torn down but the bundle is
# already local, it no-ops (rc=0). Does NOT tear anything down itself.
e2e/distributed/tools/pull_results.sh <label>          # APP defaults to the claude app
e2e/distributed/tools/pull_results.sh <label> swebench-agent-dist   # econ arm's app
```

> To keep a fleet alive for a live re-pull, arm it with the **`arm`** subcommand (§5b) —
> it arms and exits without polling/bundling/teardown, so the fleet keeps claiming. `run`
> and all-in-one always tear the fleet down at the end. **`KEEP=1` does NOT keep the fleet
> up** — it only preserves the coordinator *volume* (see §11e).

### 11b. Cost + per-task × per-tier breakdown

```bash
# summary (default) — overall + by-tier + by-model, real LiteLLM spend
python3 e2e/reference/claude/local-docker/cost_report.py \
  out/dist-<label>/bundle/results/<label> \
  --grade out/dist-<label>/bundle/reports/merged.<label>.json

# --detailed adds §6 Per-Task×Tier cost matrix + §7 Per-Task×Tier detail
# (turns, $, in/out tokens, cached, cache%, requests, resolved — per task AND per tier)
python3 e2e/reference/claude/local-docker/cost_report.py \
  out/dist-<label>/bundle/results/<label> \
  --grade .../merged.<label>.json --detailed          # add --json for machine-readable
```

Writes `cost-report.md` + `cost-report.json` beside the results. Cost source is always the
gateway `/spend/logs`, never Anthropic-priced.

- **`--mode` is optional** for distributed bundles. It selects `meta_<mode>.jsonl` (default
  `on`), but the distributed bundle's file is the unqualified `meta.jsonl`, so it falls
  back to that regardless of the value — omit `--mode` (do **not** pass the label; that was
  a doc error).
- Caveat: the pre-existing "Turns" columns in the summary/by-tier/by-model sections are
  actually `cost.requests`, mislabeled; the `--detailed` §7 "Turns" column pulls the real
  `telemetry.turns` (e.g. a run may show §2 turns=44 but §7 turns=58 for the same instance).

### 11c. SWE-bench Verified authorized submission format

```bash
# emits results/<label>/submission/all_preds.jsonl + preds.json
# validates coverage (non-empty patch per instance); exits non-zero on any empty patch
python3 e2e/distributed/tools/make_submission.py out/dist-<label>/bundle \
  --model-name unerr-claude-openmodels      # default name; override per submission
```

### 11d. Debug a single failed / dead instance (pull logs local)

```bash
# gathers report.json, meta.json, model_patch.diff (+ artifacts IF present) for ONE
# instance, any status (resolved/failed/dead), into --out. Prints FAIL_TO_PASS /
# PASS_TO_PASS pass-fail counts so you can classify the failure at a glance.
python3 e2e/distributed/tools/debug_instance.py out/dist-<label>/bundle \
  django__django-11885 --out /tmp/dbg-11885
```

The FAIL_TO_PASS / PASS_TO_PASS split is the fastest failure triage: `0/N FAIL_TO_PASS`
= the fix didn't land (under-fix / wrong site); `N/N FAIL_TO_PASS but M PASS_TO_PASS
regressed` = over-edit / collateral damage. **Because the default bundle has
`n_artifacts:0`, engine.log/events.jsonl are usually absent** and the script says so
(`no artifacts/engine.log found`) — it still gathers report/meta/patch. For the turn-level
trace, SSH a live worker mid-run (`docker logs <cid>`).

For a batch archive of **all** failed instances at once, `tools/collect-failed.py` writes
`e2e/distributed/failed-runs/<label>/` with an `INDEX.md`, plus each failed instance's
`report.json`, `model_patch.diff`, and a `WHY_FAILED.txt` (the FAIL_TO_PASS/PASS_TO_PASS
breakdown). The `run` path runs it automatically at bundle time.

### 11e. Cleanup / teardown

Automatic at the end of `run` / all-in-one. Manual:
`DESTROY_ONLY=1 LABEL=<label> FLY_ORG=vamsee-k-933 ./run-distributed.sh`.
**`KEEP=1` keeps only the coordinator *volume*** (`dist_coord_<label>`, so a later
`prepare` can reuse it) — it does **not** keep the fleet machines running. To inspect a
live fleet before teardown, arm it with the **`arm`** subcommand instead of `run` (§5b),
inspect, then `DESTROY_ONLY=1 …` to tear down.

> **Toolkit policy:** these scripts are the standard way to read any run's results. When a
> new question needs data they don't yet surface, **extend the script** (add a flag/section)
> rather than writing a throwaway — keep the toolkit complete and reusable.

---

## 12. Baseline results (for regression comparison)

| Run | Config | Result |
|---|---|---|
| `claude-mini10` | minimax conductor, unerr ON | 7/10 · $1.3475 LiteLLM |
| `claude-mini10-noharness` | + WORK PROTOCOL removed | 7/10 · $1.4019 LiteLLM |
| `claude-mini10-priors3` | + fix-at-definition / native-type / countable-escalation priors, `ROOTFS_GB=50` | **8/10 · $1.3986 LiteLLM · 0 dead** |
| `claude-gates-2inst` | Stop-gate only (V/E), no deny hooks | 0/2 on {11848, 11885} — gate fired (2× V-block) but converted neither; motivated the PreToolUse deny rules |

Escalation still never fires (By-Tier 100% conductor / $0 reasoner+oracle) — the countable
triggers in the appended prompt are advisory text minimax ignores; enforcement needs a
mechanical forcing function, not prose. The two consistent Mini-10 failures (11848, 11885)
are **not** localization gaps — in `priors3` both fixed at the right site:
- `11848` under-fixed (0/2 FAIL_TO_PASS, patch algorithm nearly right) and **bailed at 17
  turns without verifying** → a verification gap.
- `11885` **solved** the target bug (1/1 FAIL_TO_PASS) but **regressed 2 PASS_TO_PASS**
  (`test_large_delete*`) over 106 thrashing turns ($0.75 = 54% of the run) → an
  over-edit / regression-check gap.

See the memory notes `claude-workprotocol-regression` and `claude-openmodels-mini10-result`
for the fuller autopsies.
