# Running the ECON arm on SWE-bench — open-weight ensemble via opencode, unerr COMPILED IN, distributed on fly

**This is the single source of truth for running the ECON agent (opencode CLI, unerr's
code-graph engine compiled directly into the binary) against SWE-bench on a fly work-
stealing fleet.** Modeled on the claude-arm guide (`e2e/reference/claude/fly-remote/README.md`)
— read that first if you haven't; this mirrors its structure but every command/path here
is verified against the econ arm's own source, not copied from claude. If the run
process or config changes, **update this file**.

The runner code lives in `e2e/distributed/` (**shared** with the claude arm — `ARM=econ`
vs `ARM=claude` selects the pipeline). The per-instance driver lives in
`e2e/econ/local-docker/context/run-instance.sh` (baked into the fleet image).

---

## 0. TL;DR — the command that works

```bash
cd e2e/distributed
MACHINES=2 LABEL=<run-name> SUITE=mini FLY_ORG=vamsee-k-933 ./run-distributed.sh
```

- `ARM=econ` is the **default** (`run-distributed.sh:44`) — no need to pass it.
- No `ROOTFS_GB=` / `CPU_KIND=performance` are required by default (unlike claude — §9.1:
  econ's per-worker toolbox build is a single ~168 MB binary graft, not claude's pnpm
  monorepo build). **Not yet empirically re-validated on a full fleet run** — if every
  instance dead-letters (`n_preds:0`), check `df -h /` on a worker before assuming a
  model/gateway issue.
- No LiteLLM MASTER key, no `unerr install`, no MCP config — econ needs only
  `LITELLM_API_KEY` (auto-read, §2).
- Omit `IMAGE=` to bake fresh (default) — for econ this ALSO rebuilds the `opencode`
  binary from `../econ-coding-agent` every run (§4). `SKIP_ECON_BUILD=1` to skip that.
- Fleet app is **`swebench-agent-dist`** — the plain default (NOT
  `swebench-agent-dist-claude`, claude's separate app) — `run-distributed.sh:48-50`.

---

## 1. What this actually runs

- **Agent:** the `opencode` CLI (econ), one SWE-bench instance per container:
  `opencode run --format json --print-logs --dir "$REPO_DIR" --dangerously-skip-permissions "$PROBLEM"`
  (`run-instance.sh:71-77`). No `--model` — routing is `opencode.json`-driven (§3).
- **unerr COMPILED IN — no install, no MCP.** `packages/code-intelligence` (the graph
  engine) is linked straight into the `opencode` binary. No `.mcp.json`, no daemon, no
  entitlement minter — the arm binary is the only thing the toolbox ships
  (`Dockerfile.toolbox:6-12`).
- **Graph init — econ's equivalent of claude's `unerr index`.** Before the resolve,
  `run-instance.sh` runs `timeout 600 "$TOOLBOX/opencode" init "$REPO_DIR" --force --json`
  (logs to `/tmp/opencode-init.log`), so the first turn starts on a built graph instead
  of cold-indexing mid-request (`run-instance.sh:54-60`). Recently added — confirm it's
  present if diffing against an older image.
- **Open-weight models via a self-hosted LiteLLM gateway:** conductor / oracle /
  reasoner / executor / explorer tiers are pinned in `opencode.json` to models served
  through `https://econ-litellm.fly.dev/v1` (baked into the config's `options.baseURL`
  — not an env override). See §3.
- **Distributed:** the same `run-distributed.sh` claude uses bakes ONE fleet image,
  starts a coordinator (SQLite queue on a small volume) + N workers (DinD hosts),
  grades each instance live, bundles, tears down.

---

## 2. One-time prerequisites

| Thing | Where / value |
|---|---|
| fly auth | `flyctl auth whoami` |
| fly org | `vamsee-k-933` — pass `FLY_ORG=vamsee-k-933` |
| fly app | `swebench-agent-dist` (auto-created/reused; `run-distributed.sh:48-50`) |
| LiteLLM API key | auto-read from `e2e/econ/.env.local` (`LITELLM_API_KEY`) — `run-distributed.sh:315-320` |
| econ checkout (sibling repo, for fresh bake) | `../econ-coding-agent` — override `ECON_REPO=` |
| econ build toolchain | `bun` on PATH — the vendor step runs `bun install && bun run --cwd packages/opencode build` for you (§4) |
| gateway health | `curl -s https://econ-litellm.fly.dev/health/liveliness` → `"I'm alive!"` |

No LiteLLM **MASTER** key needed — that's claude-only (mints per-instance virtual keys).

---

## 3. The open-weight model map

Set in `e2e/econ/local-docker/context/vendor/opencode.json` (vendored verbatim off
`../econ-coding-agent/opencode.json` every bake — §4); the BYOK price matrix mirroring
it lives in `e2e/econ/econ-tier-cost.py:59-74`.

| Tier | Gateway model id | Mode | Role |
|---|---|---|---|
| conductor | `minimax/minimax-m3` | primary | main loop — plans, decides, holds context every turn |
| oracle | `z-ai/glm-5.2` | primary | highest-scoring escalation, called rarely (high-risk turns) |
| reasoner | `deepseek/deepseek-v4-pro` | primary, hidden | mid escalation — hard steps below full-oracle threshold |
| executor | `openai/gpt-oss-120b` | subagent | delegable chores, spawned via the task tool |
| explorer (+5 personas) | `openai/gpt-oss-120b` | subagent, hidden | read-only recon; `runner`/`applier`/`author`/`researcher`/`procedure` share the model, differ in tool permissions |

All models route through `https://econ-litellm.fly.dev/v1`, baked into `opencode.json`'s
litellm provider block (`options.baseURL`, `env:["LITELLM_API_KEY"]`) — no
`ANTHROPIC_BASE_URL`-style env override exists for econ. Escalation rarely fires
(conductor solos most turns), same pattern as claude's minimax conductor.

---

## 4. Re-vendoring the latest opencode build (do this when econ source changed)

Unlike claude's separate `build-toolbox.sh` + tarball, econ has **no separate re-vendor
script** — vendoring is inline in `run-distributed.sh`'s `ARM=econ` block
(`run-distributed.sh:357-408`) and runs automatically on every bake:

```bash
cd e2e/distributed
MACHINES=2 LABEL=<run-name> SUITE=mini FLY_ORG=vamsee-k-933 ./run-distributed.sh
# omitting IMAGE= and SKIP_ECON_BUILD= re-vendors + fresh-bakes every time
```

Steps (`run-distributed.sh:357-405`):
1. `cd ../econ-coding-agent && bun install && bun run --cwd packages/opencode build` —
   rebuilds from **live source, including uncommitted changes**; logged to
   `$OUTDIR/econ-build.log`.
2. Copies the **glibc** `linux-x64-baseline` binary (`packages/opencode/dist/opencode-linux-x64-baseline/bin/opencode`,
   NOT musl — the SWE-bench images are Debian; musl fails ENOENT there) to
   `e2e/econ/local-docker/context/vendor/opencode`.
3. Copies `opencode.json` and `.opencode/` → `vendor/`, pruning
   `dot-opencode/tool/github-*.ts` (unresolvable offline; can abort a session at 0 turns).
4. Copies `packages/code-intelligence/src` → `vendor/dot-opencode/vendor/code-intelligence/src`
   — the embedded graph engine.

A fresh bake then `COPY`s that `vendor/` tree into the fleet image
(`Dockerfile.dist:44-47` → `/work/local-docker/context`); each worker builds the actual
toolbox image from it at boot (§8) — the fleet image ships the *context*, not a
pre-built toolbox.

- **Two independent staleness knobs:** `IMAGE=` (skips the whole fly image bake) and
  `SKIP_ECON_BUILD=1` (skips step 1, reuses local `dist/`, still fresh-bakes the fly
  image). Both must be unset for guaranteed-fresh econ code.
- Validate: `$OUTDIR/econ-build.log`, and `$OUTDIR/run-info.json`'s `econ_commit` /
  `econ_dirty` fields.

---

## 5. The run commands

### 5a. All-in-one (recommended for Mini-10; no dedicated GPU needed)

```bash
cd e2e/distributed
MACHINES=2 LABEL=mini10-run1 SUITE=mini FLY_ORG=vamsee-k-933 ./run-distributed.sh
```

Bakes (rebuilds econ + fresh flyctl image) → creates coordinator + workers → arms →
polls `/status` → pulls bundle → **tears down the fleet automatically**.

### 5b. Prepare / run / arm split (only if using a dedicated GPU)

Same shared mechanism as claude — only worth it when `DEDICATED_CONDUCTOR=1` raises the
$80/hr Fireworks GPU. Skip for serverless minimax (default).

```bash
# 1) build + create fleet + warm workers, HOLD at armed=0 (no GPU up)
MACHINES=2 ARM=econ LABEL=mini10-run1 SUITE=mini FLY_ORG=vamsee-k-933 \
  ./run-distributed.sh prepare      # prints "PREPARED" once workers report warm

# 2) flip the gate + poll + bundle + teardown
LABEL=mini10-run1 ARM=econ FLY_ORG=vamsee-k-933 ./run-distributed.sh run

# (or just flip the gate and poll later)
LABEL=mini10-run1 ./run-distributed.sh arm
```

### 5c. Suite / instance selection

- `SUITE=mini` → the 10-instance Mini set (`e2e/distributed/tools/suite.py:33-39`):
  `django__django-11790, 11815, 11848, 11880, 11885, 11951, 11964, 11999, 12039, 12050`.
- `SUITE=<other>` (`full`/`verified`/`lite`) or `TASKS="id1,id2,..."` / `TASKS_FILE=<path>`
  to override (explicit tasks win, then a file, then the suite name — `suite.py:71-97`).
- Full Verified = the default (no `SUITE`) — 500 instances; do NOT run casually.

---

## 6. Environment variables — required vs auto

| Var | Required? | Notes |
|---|---|---|
| `ARM=econ` | no (default) | `run-distributed.sh:44` |
| `LABEL=<name>` | **yes** | names the fleet, coordinator volume, `out/dist-<label>/` |
| `MACHINES=2` | **yes** (all-in-one/prepare) | worker count |
| `SUITE=mini` | for Mini-10 | else full Verified |
| `FLY_ORG=vamsee-k-933` | **yes** | the team org |
| `LITELLM_API_KEY` | auto | from `e2e/econ/.env.local` |
| `IMAGE=` | no | unset = fresh bake + fresh econ rebuild (default) — §7 |
| `SKIP_ECON_BUILD=1` | no | skip the `bun` rebuild, reuse local `dist/` (still fresh-bakes the image) |
| `ECON_REPO=` | no | override the sibling checkout (default `../../../econ-coding-agent`) |
| `DEDICATED_CONDUCTOR=1` | no | raises the $80/hr GPU; NOT needed for serverless minimax |
| `CPU_KIND` | no | default `shared`; econ has no MCP-negotiation stall to work around — `performance` is unverified-unnecessary here (§9.1) |
| `ROOTFS_GB` | no | default rootfs (8 GB); likely unnecessary for econ's tiny toolbox — verify, don't assume (§9.1) |
| `WEBSEARCH=1` / `EXA_API_KEY` | no | off by default (baseline-comparable); ambient key ignored unless `WEBSEARCH=1` |

---

## 7. Fresh bake vs reusing an image

- **Fresh bake (default, omit `IMAGE=`):** rebuilds `opencode` from live
  `../econ-coding-agent` (§4) **and** runs `flyctl deploy --build-only --remote-only` on
  fly's depot builder. Slower than claude's (adds a `bun` compile) but layers cache well.
  REQUIRED after any edit to `../econ-coding-agent` or `e2e/econ/local-docker/**`
  (`Dockerfile.dist:40-52` `COPY`s them in).
- **Reuse (`IMAGE=registry.fly.io/swebench-agent-dist:dist-<ts>`):** skips both the
  `bun` rebuild and the flyctl bake. Safe only when econ source AND the runner scripts
  are unchanged since that image was built. Find the ref in the prior run's
  `out/dist-<label>/build.log`. Destroying machines does NOT delete the pushed image.

```bash
MACHINES=2 LABEL=mini10-run2 SUITE=mini FLY_ORG=vamsee-k-933 \
  IMAGE=registry.fly.io/swebench-agent-dist:dist-1784089905 \
  ./run-distributed.sh
```

---

## 8. Coordinator, workers, timings

**Coordinator** (`shared-cpu-1x:1024MB`, one 10 GB volume `dist_coord_<label>` in
`iad`): identical mechanics to claude — `armed` gate, `/status` JSON, beacons
(`seeded` → `drained` → `aggregated` → `grade_done` → `bundle_ready`). One arm-specific
branch: `coordinator-entrypoint.sh:308-325` routes `ARM=claude` to
`local-docker/cost_report.py`; **every other arm goes to `REPORT_PY`, default
`/work/report.py`**.

**Workers** (8 cpu / 16 GB, `CPU_KIND` default `shared`, **no volume** — DinD data-root
on ephemeral rootfs, `run-distributed.sh:605-636`): each is a DinD host. Per worker boot
(`worker-entrypoint.sh:63-76`) builds the toolbox image **once**: `docker build -f
Dockerfile.toolbox context -t econ-toolbox` (tag drops the `unerr-` prefix — nothing is
installed). `Dockerfile.toolbox` has **no `RUN` build step beyond `mkdir`/`chmod`**
(`local-docker/Dockerfile.toolbox:20-34`) — a straight `COPY` of the vendored binary +
config into a `scratch` carrier. Then per instance: graft toolbox onto the SWE-bench
base image → `opencode init` (graph warm-up) → `opencode run` → grade → `POST` report.

**Timings — largely unmeasured for this pipeline; the numbers below are ceilings/config
defaults, not observed wall-clock:**

| Phase | Value |
|---|---|
| Graph init (`opencode init`) | 600 s timeout (`run-instance.sh:56`) |
| Resolve (`opencode run`) | `PER_INSTANCE_TIMEOUT` default 14400 s / 4 h |
| Grade (per instance) | same swebench harness as claude — historically ~1–3 min |
| Coordinator hold after bundle | `HOLD` default 3600 s / 1 h |
| Host poll ceiling | `MAXWAIT` default 172800 s / 48 h |

Worker toolbox-build warm time is expected well under claude's ~600–700 s (single
binary graft vs. a full `pnpm` monorepo build) — no measured number exists yet; record
one on your next run and update this table.

---

## 9. GOTCHAS / known failure modes

1. **Claude's 8 GB-outer-rootfs / DinD-disk failure is UNVERIFIED for econ — likely
   doesn't reproduce, but confirm before trusting it.** Claude's failure comes from its
   in-VM `pnpm install/build/pack` ballooning the sparse DinD-backing loopback image
   (`e2e/distributed/lib/boot.sh:36-52`) faster than the outer 8 GB rootfs it sits on can
   absorb. Econ's toolbox build has no `RUN` step at all (§8) — orders of magnitude less
   data. If instances still dead-letter (`n_preds:0`) with `input/output error` in a
   worker's docker logs, check `flyctl ssh console --machine <w> -C "df -h /"` and fall
   back to `ROOTFS_GB=50` — cheap insurance either way.
2. **Two independent staleness knobs** — see §4. Both `IMAGE=` and `SKIP_ECON_BUILD=1`
   must be unset for guaranteed-fresh econ code.
3. **A session that aborts with 0 turns on turn 1** can be a gateway hiccup (check
   `econ-litellm` health + the key's `/spend/logs`) **or** an econ-specific plugin-load
   failure: an unresolvable `.opencode/tool/*.ts` aborts the whole session immediately.
   The vendor step already prunes `github-*.ts` (§4.3); verify any new upstream plugin
   doesn't reach for something unavailable offline before it ships in a bake.
4. **Leftover `stopped` machines** from prior images are harmless (different image
   tags); teardown targets only `--metadata fleet=<label>`.
5. **`$0.00` everywhere in the cost report** means `REPORT_PY` didn't run or errored —
   check the bundle's `logs/report-<label>.log`, then regenerate manually (§11b).
6. **Unpriced-model drift silently reads a tier's cost as $0.** `report.py`'s Per-Tier
   section flags this with `⚠️` (`report.py:420-427`) when a tier used a model absent
   from `ECON_COST_MATRIX` (`econ-tier-cost.py:59-74`). Add a new tier model's rate
   there in the same change (mirrors `econ-cost.ts::ECON_COST_MATRIX` in
   `econ-coding-agent`) or that tier's USD goes invisible.
7. **"cost" always means the recomputed Fireworks-BYOK price**, not the DB's stored
   upstream catalog price. `econ_step_cost` (`econ-tier-cost.py:94-110`) recomputes per
   message from `ECON_COST_MATRIX`; the stored `usd_upstream` is kept for comparison only.

---

## 10. Monitoring a live run

```bash
APP=swebench-agent-dist

# coordinator status (authoritative, parsed)
flyctl logs -a "$APP" --no-tail | grep -oE '\[dist-coordinator\] status .*' | tail -1

# worker states
flyctl machine list -a "$APP" | grep dist-<ts>

# SSH into a worker to see the live instance container (graph-init/run timeline)
flyctl ssh console -a "$APP" --machine <worker-id> -C "docker ps"
flyctl ssh console -a "$APP" --machine <worker-id> -C "docker logs --timestamps <cid>"
```

`docker logs` on a live container shows `[run-instance]` lines: `graph init: ok (...)`
(or `FAILED/timeout`), then `econ run starting`, then `econ exit=<rc>`. The
coordinator's `/status` `resolved` is computed LIVE from graded reports — trust it over
the launcher's own `progress:` lines, which can lag a poll cycle.

---

## 11. Download & process results

The all-in-one / `run` path already pulls the bundle to
`e2e/distributed/out/dist-<label>/bundle/` and grades it. **Use the prebuilt scripts
below — do not hand-roll one-off parsing.** They live in `e2e/distributed/tools/` (+
the arm-aware cost report at `e2e/econ/report.py`); most are the SAME scripts claude
uses (shared/arm-agnostic except where noted).

**Bundle layout** — `out/dist-<label>/bundle/`: `results/<label>/meta.jsonl`
(per-instance `telemetry{turns,tokens,tools}` + `cost{by_tier,by_model}`),
`results/<label>/preds.json` (SWE format), `dead.jsonl`,
`results/<label>/artifacts/<iid>/` (`engine.log`, `err.txt`, `events.jsonl`,
`opencode.db`), `reports/merged.<label>.json` (swebench grade).

**11a. Re-pull a live fleet's bundle** (APP defaults to the CLAUDE app —
`pull_results.sh:22-23` — pass the econ app explicitly):
```bash
e2e/distributed/tools/pull_results.sh <label> swebench-agent-dist
```

**11b. Cost + per-tier breakdown** (SHARED; the coordinator already runs this at end of
run — `coordinator-entrypoint.sh:318-322` — re-run to regenerate or redirect `--out`):
```bash
python3 e2e/econ/report.py \
  --meta out/dist-<label>/bundle/results/<label>/meta.jsonl \
  --grade-report out/dist-<label>/bundle/reports/merged.<label>.json \
  --label <label> --out <dir>
```
Writes `<dir>/cost-report.md` + `.json`. The Per-Tier table (`report.py:370-428`) has
one row per tier (conductor/oracle/reasoner/executor+personas) with Total $, % of
Total, Mean $/inst, In/Cached/Out/Reasoning tokens, Cache Write, Total Tokens,
Cache-Hit %, Instances Used; an unpriced-model tier gets a `⚠️` suffix.

**11c. SWE-bench Verified submission format** (SHARED — override the claude-flavored
default name):
```bash
python3 e2e/distributed/tools/make_submission.py out/dist-<label>/bundle \
  --model-name unerr-econ-openweight
```
Emits `results/<label>/submission/all_preds.jsonl` + `preds.json`; validates coverage
and exits non-zero on any empty patch.

**11d. Debug a single failed / dead instance** (SHARED, arm-agnostic):
```bash
python3 e2e/distributed/tools/debug_instance.py out/dist-<label>/bundle \
  django__django-11885 --out /tmp/dbg-11885
```
Gathers `report.json`, `meta.json`, `model_patch.diff` + the full artifact set for ONE
instance into `--out`.

**11e. Failed-run triage archive** (SHARED; runs automatically at the end of
`run-distributed.sh:739-743` — re-run standalone or to redirect `--dest`):
```bash
python3 e2e/distributed/tools/collect-failed.py --bundle out/dist-<label>/bundle \
  --dest failed-runs/<label>
```
Copies every `resolved=False` and dead-lettered instance's artifacts (+
`DEAD_REASON.txt` for dead ones) into `--dest`, with an `INDEX.md` summary.

**11f. Cleanup / teardown:** automatic at the end of `run`/all-in-one. Manual:
`DESTROY_ONLY=1 LABEL=<label> FLY_ORG=vamsee-k-933 ./run-distributed.sh` (add `KEEP=1`
to a run to skip auto-teardown so §11a/§11d can inspect the live fleet first).

> **Toolkit policy:** `tools/` is the standard, shared way to read any run's results.
> When a new question needs data these don't yet surface, **extend the script** rather
> than writing a throwaway.

---

## 12. Baseline results (for regression comparison)

No Mini-10 run of **this exact pipeline** — the shared `e2e/distributed` fleet runner
with the `opencode init` graph-warm-up step (§1) — has a recorded result yet. The most
recent econ Mini-10 number in memory (`econ-v9-mini10-result`, a prior
iteration/harness, likely NOT this shared distributed runner) is **7/10, $2.2331**
(minimax conductor), recovering `11790`/`11815`/`11885`, losing `11848`/`11964`/`12039`
to cheap bailouts / an empty patch — **directional only**, not a validated baseline for
the config documented here.

**TBD — run Mini-10 end-to-end on this pipeline and record the result here** (resolve
rate, LiteLLM $ spend, wall-clock, per-tier By-Tier split).
