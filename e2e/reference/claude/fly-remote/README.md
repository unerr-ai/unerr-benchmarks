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
| `PER_INSTANCE_TIMEOUT` | no | the benchmark descriptor's grade-side cap only (default **86400** s, `benchmarks.py`) — bounds the swebench grade subprocess, not the resolve. There is no per-task resolve ceiling and no stall/progress watchdog; the coding agent owns its own watchdog/thrash detection. |
| `WEBSEARCH=1` | no — **changes the result class** | **`ARM=claude` only** (STRICT opt-in): enables `TAVILY_API_KEY` (read from `e2e/econ/.env.local`, injected into workers) → `run-instance.sh` merges **Tavily's hosted MCP server** (`mcp.tavily.com`, HTTP transport, no npm dep) into the instance's `.mcp.json`, and the ON-arm prompt points the model at `tavily_search`/`tavily_extract` (underscores!). Unset → ambient keys IGNORED, zero search tools. **`ARM=econ` is DIFFERENT: Exa web search is DEFAULT-ON (see below) — set `WEBSEARCH=0` to force it off.** A web-on run can look up the actual upstream fix — label it (e.g. `-web` suffix) and never compare it 1:1 against no-web baselines or submit it. |
| `WEBSEARCH=0` | no | **econ arm only** — force Exa web search OFF for a clean, baseline-comparable (no-web) econ run. No effect on claude (already opt-in). |
| `EXA_API_KEY` | auto (econ arm) | econ-arm Exa search key — **default-on** for econ, sourced from `econ-coding-agent/.env.local` (canonical) then `e2e/econ/.env.local`; injected into workers unless `WEBSEARCH=0`. |
| `TAVILY_API_KEY` | auto (only when `WEBSEARCH=1`) | claude-arm search key (opt-in). econ arm uses `EXA_API_KEY` (default-on, above). |

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

`prepare` alone ≈ 16 min (bake + warm). The harness enforces no per-task resolve
ceiling and no stall/progress watchdog — the coding agent owns its own watchdog/thrash
detection, and a resolve may legitimately run for hours. Liveness is still protected at
the fleet level: the coordinator reaps a lease only after `HEARTBEAT_TIMEOUT` (dead
worker/VM, not a slow task), and `MAXWAIT` + `NO_PROGRESS_GIVEUP` are the host/coordinator
wedge backstops (§2 of the distributed README); bundle HOLD = 1 h.

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
    e.g. 11885). Test = a `tests`/`testing` path segment or a `test_*`/`*_test.py`
    basename — deliberately NOT a bare `test` segment: `django/test/` is product
    source (TestCase/Client), and denying it would leave a gold fix touching it
    with no legal path.
  - Rule B — ≥5 edits to one file with no green verification **AND** a prior V- or
    R-block already fired → deny with an imperative to spawn `unerr-opus` + `unerr-fable`
    (max 2 fires). The mechanical escalation forcing function, now **throttled to a
    verification-revealed trigger**: raw edit-count alone no longer forces escalation
    (Phase A showed count-triggered escalation billed the reasoner/oracle tiers for zero
    conversions — see §12).
  - Rule C — convention divergence: introducing `datetime.now(` into a file that
    already uses `utcnow` denies once with an evidence-cited re-apply path (the exact
    11848 fatal token class).
- **PostToolUse `record`** — silent event recorder → `/tmp/cc-harness/state.jsonl`.
- **Stop `gate`** — Z (no edits) / R (regression) / V (unverified, cap 2) / E
  (escalation). Two behaviors landed after Phase A:
  - **V requires a BROAD green run** — a recognized suite runner (`pytest`,
    `runtests.py`, `-m unittest`, `manage.py test`, `tox`/`nox`, sympy's `bin/test`)
    over a whole module/file/class. NARROW (does not satisfy V): a `::test_...`
    method node id, a `-k` filter, or a dotted `Class.test_method` path — but a
    django dotted whole-module (`runtests.py app.test_file`) and a `::TestClass`
    whole-class run count as BROAD (case-sensitive, structural regex). A bare repro
    script (`python repro_issue.py`) is NOT a suite run and never satisfies V. This
    forces the edited module's suite to run so **Gate R can catch a PASS_TO_PASS
    regression** — the failure class that sank django-11885 & pylint-7277 (solved
    target, regressed a sibling test they never ran).
  - **E is throttled** — the raw hot-file (≥3 edits) arm was removed; E now escalates
    only on an R-block or ≥2 V-blocks (verification proved the agent is stuck), never on
    edit count.
  - **Caps:** overall cap 3 gates only the Z/R/V nudges; **Gate E is exempt** (else
    `cap(3) == V_cap(2) + E_cap(1)` would let an early Z/V spend the budget and starve
    the escalation — verified deadlock, now selftest-guarded). E is capped at 1, so the
    hard ceiling is 4 blocks per run, then unconditional allow. Fail-open. Flow:
    narrow-verify → V → V(cap) → E escalate → allow; broad-verify red → R rework;
    broad-verify green → finish.
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
   Anthropic-priced `total_cost_usd` (renamed `telemetry.usd_anthropic_priced`). Claude Code
   prices open-weight tokens at sonnet/opus rates and is 7–12× too high — **see §11b′ for the
   mechanism, the gateway spot-check, and the numeric proof.**
7. **The harness no longer auto-kills a wedged resolve** — there is no stall/progress
   watchdog and no per-task timeout; a hung `claude`/MCP call runs until the agent itself
   recovers, or until the coordinator's `HEARTBEAT_TIMEOUT` reaps the lease (worker/VM
   silence, not agent silence). If an instance never completes, check the agent's own
   logs first — the harness has no visibility into task-level progress anymore.
8. **Web search: the native `WebSearch` tool NEVER works on the open-models arm** — it is
   an Anthropic *server-side* tool (`web_search_20250305`); through the gateway fireworks
   rejects it with `400: does not support parameters: ['web_search_options']` (verified
   live). `run-instance.sh` passes `--disallowedTools WebSearch` when `OPEN_MODELS=1` so
   the model can't burn turns on it. Real search = `WEBSEARCH=1` → Tavily hosted MCP (§6).
   Three smoke-verified quirks: the tool names use **underscores** (`tavily_search`,
   `tavily_extract`); MCP servers connect **asynchronously** (`status: pending` in the init
   event) so a model that needs search on turn 1 must call `WaitForMcpServers` first — the
   ON-arm prompt hint says exactly that; `WebFetch` works on both arms regardless (client-
   side fetch; its summarize call routes through the haiku mapping).

---

## 10. Monitoring a live run

Fastest path — the two read-only monitor scripts in `e2e/distributed/` (they resolve the coordinator
and workers by fleet metadata, so pass the claude app as arg 2 or just let the label's `claude` fold
infer it; see distributed [README §3](../../../distributed/README.md)):

```bash
cd e2e/distributed
./status.sh <LABEL> swebench-agent-dist-claude --instances   # armed?, workers, counts, resolved/total + per-instance
./status.sh <LABEL> swebench-agent-dist-claude --cost        # + total $ and per-tier (conductor/reasoner/oracle/fast) token·turn·cost, from LiteLLM spend
./status.sh --matrix <id> --watch                            # live monitor a whole matrix every 15s
./debug-workers.sh <LABEL> swebench-agent-dist-claude --grep 'worker-loop|index|scip|claude'   # what each worker is doing
./debug-workers.sh <LABEL> swebench-agent-dist-claude --follow                                  # live-stream both workers
```

Or drop to the raw fly commands:

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
`reports/merged.<label>.json` (swebench grade). **Per-instance traces** ride `/complete` into
`results/<label>/artifacts/<iid>/` **when the arm/harness writes them** (descriptor-driven — see the
distributed [README §8.1](../../../distributed/README.md#81-the-benchmark-axis-benchmark)):
`terminal` always carries `trajectory.json` + `sessions.cast`; the econ resolve path carries
`engine.log` + `events.jsonl` + `opencode.db`. **The Claude-arm verified/pro path historically wrote
none** of those files into its artifact dir, so its bundle still shows `n_artifacts: 0` and
`report.json` + `meta` + `model_patch` are the only per-instance survivors — for turn-by-turn engine
logs there you SSH a **live** worker during the run (see §11d).

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

### 11b′. Why Claude Code's `usd` is NOT the cost on this arm — never trust it

**This is the single most common cost mistake — read it before quoting any dollar figure.**

Claude Code computes its own spend by applying **Anthropic list prices** to the token
counts of the model *name* it thinks it called (`sonnet` / `opus` / `haiku`). On this arm
those names are re-routed by LiteLLM to open-weight models (`minimax-m3` / `deepseek-v4-pro`
/ `gpt-oss-120b` / `glm-5p2`), which are **~10× cheaper**. Claude Code never learns the real
model or its price, so its figure — surfaced as `telemetry.usd_anthropic_priced` (and the
raw `total_cost_usd` in the stream-json) — is **Anthropic-priced fiction, typically 7–12×
the truth**. Do not put it in a report, a table, or a comparison.

**The only real cost is the LiteLLM gateway's per-model spend.** `cost_report.py` already
reconciles it into `cost.by_tier` / `cost.by_model` in the bundle. To spot-check a live or
finished run straight from the gateway:

```bash
MK=$(grep -E '^LITELLM_MASTER_KEY=' ../econ-coding-agent/infra/litellm/.env.local | sed 's/^[^=]*=//; s/^"//; s/"$//')
D=$(date -u +%F); T=$(date -u -v+1d +%F 2>/dev/null || date -u -d tomorrow +%F)
# per-model spend for the day (window is a daily rollup; econ arm shares the gateway, so
# run this only when econ is idle, or filter by the run's minted api_key hash).
curl -s "https://econ-litellm.fly.dev/spend/logs?start_date=$D&end_date=$T" \
  -H "Authorization: Bearer $MK" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);r=d[0] if isinstance(d,list) else d;print(json.dumps(r.get("models",{}),indent=1))'
```

**Two proofs it's LiteLLM, not Claude Code:**
1. **The breakdown is by open-weight model names** (`minimax-m3`, `deepseek-v4-pro`,
   `glm-5p2`, `gpt-oss-120b`). Claude Code has no concept of these — it believes every call
   was sonnet/opus/haiku. A per-`deepseek`/`glm` spend table can *only* come from the gateway.
2. **Numeric reconciliation.** e.g. `django-11848` conductor used in=40 837, cache_read=952 333,
   out=15 954. At LiteLLM minimax rates ($0.30/$1.20 per M, cache $0.06/M) that is **~$0.09**;
   at Anthropic sonnet rates ($3/$15 per M) it is ~$0.65–1.08 — and Claude Code reported
   **$1.0792**. The reported number tracks Anthropic, not the gateway. Real cost ≈ $0.09.

LiteLLM per-model rates are the ground truth (`GET /model/info` → `input_cost_per_token` /
`output_cost_per_token` / `cache_read_input_token_cost`). Lead with the LiteLLM number, always.

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

### 11f. Pull a whole matrix at once (`download-all.sh`)

When you launched several arm×benchmark fleets with `bench.sh` (distributed
[README §8.3](../../../distributed/README.md#83-fire-a-matrix-benchsh)), pull every one's
results + traces in a single pass instead of running `pull_results.sh` per fleet:
```bash
e2e/distributed/download-all.sh --matrix <matrix-id>                 # reads out/bench-<id>/manifest.tsv
e2e/distributed/download-all.sh --matrix <matrix-id> --submission    # + all_preds.jsonl per resolve_then_grade combo
e2e/distributed/download-all.sh <label> <app> [<label> <app> ...]    # explicit fleets, no manifest
```
It calls `pull_results.sh` for each `(label, app)` in the matrix manifest, extracts to
`out/dist-<label>/bundle/`, and prints a per-combo table (resolved/total, preds, artifacts, dead).
`--submission` is skipped for `terminal` (fused harness run, no patch). `pull_results.sh` now tolerates
an offline re-pull (a flyctl/token error no longer aborts it — it falls back to an already-local
bundle), so re-running `download-all.sh` after teardown just re-summarizes what's on disk.

> **Toolkit policy:** these scripts are the standard way to read any run's results. When a
> new question needs data they don't yet surface, **extend the script** (add a flag/section)
> rather than writing a throwaway — keep the toolkit complete and reusable.

### 11g. Archive to Tigris (destroy the fleet, keep the data)

Opt-in: the coordinator pushes each run's DATA (traces, grading, submission, logs, a generated
`overview.json`, `bundle.tgz` — never agent source) to a Tigris bucket at end-of-run, so the fleet can be
torn down and the run stays lookup-able. Full doc + taxonomy: distributed
[README §9](../../../distributed/README.md). One-time: `FLY_ORG=<team-org> e2e/distributed/provision-tigris.sh`
(billable, prints S3 keys once — attaches AWS_* secrets to the claude fleet app too). Enable per-run:
```bash
ARCHIVE_TIGRIS=1 TIGRIS_BUCKET=swebench-dist-archive ROOTFS_GB=50 CPU_KIND=performance ARM=claude ... ./run-distributed.sh
```
`overview.json` carries grade% + real-LiteLLM cost-by-tier (conductor/reasoner/oracle/fast). Look up later
with no live fleet: `e2e/distributed/tools/tigris-archive.sh list | overview <label> | get <label>`.

---

## 12. Baseline results (for regression comparison)

| Run | Config | Result |
|---|---|---|
| `claude-mini10` | minimax conductor, unerr ON | 7/10 · $1.3475 LiteLLM |
| `claude-mini10-noharness` | + WORK PROTOCOL removed | 7/10 · $1.4019 LiteLLM |
| `claude-mini10-priors3` | + fix-at-definition / native-type / countable-escalation priors, `ROOTFS_GB=50` | **8/10 · $1.3986 LiteLLM · 0 dead** |
| `claude-gates-2inst` | Stop-gate only (V/E), no deny hooks | 0/2 on {11848, 11885} — gate fired (2× V-block) but converted neither; motivated the PreToolUse deny rules |
| `claude-fixval-5` | + deny hooks (T/B/C), escalation now bills | **1/5 · $2.48 LiteLLM · 0 dead** on the 5 hardest fails {11848, 11885, matplotlib-23476, pylint-7277, sympy-15017} |

**`claude-fixval-5` (Phase A validation, 2026-07-16)** proved the mechanisms fire and
**fixed the $0-escalation bug** — By-Tier is no longer 100% conductor: reasoner (deepseek)
$0.92 + oracle-tier (glm) $0.37 billed, i.e. Rule B now makes the conductor actually spawn
`unerr-opus`/`unerr-fable`. `11848` converted (✅) — the **V-gate forced verification** (ran
tests, correct `utcnow` patch), conductor-only, $0.09. But the 4 fails exposed the next levers:
- **2 of 4 (11885, pylint-7277) are the "solved-target-but-regressed-a-sibling-test" class** —
  escalation fired and billed but converted neither → motivated the **broad-verify V gate + R**
  (§8.5): force the module suite to run so the regression is caught pre-finish.
- escalation fired on **all 4** fails and converted **0** (added $1.29, 52% of cost) →
  motivated the **escalation throttle** (E drops the edit-count arm; Rule B needs a
  verification block). The 2 remaining fails (matplotlib, sympy) are wrong-fix / localization
  (F2P 0/1) — model-capability, not a harness gap.
- **Cost is LiteLLM-reconciled, not Anthropic** — the gateway *daily* rollup ($10+) is
  contaminated by shared traffic; the per-instance `by_tier` ($2.48) is the real number (§11b′).

The earlier consistent Mini-10 failures (11848, 11885) were **not** localization gaps — in
`priors3` both fixed at the right site: `11848` under-verified (a verification gap, now closed
by the V-gate); `11885` regressed 2 PASS_TO_PASS (`test_large_delete*`) — the regression-check
gap the broad-verify V gate now targets.

See the memory notes `claude-workprotocol-regression`, `claude-openmodels-mini10-result`, and
`claude-fixval5-phaseA-result` for the fuller autopsies.
