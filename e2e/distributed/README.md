# Distributed benchmark runner

A work-stealing fleet that runs a coding-agent benchmark across N fly.io machines in
parallel instead of one machine serially. Full design in [`PLAN.md`](./PLAN.md) — this
is the concise operator doc: launch, monitor, pull, teardown.

Two independent axes: **`ARM`** picks the agent (`econ` | `claude` | `claude-real`), **`BENCHMARK`** picks the
dataset (`verified` | `lite` | `pro` | `terminal` | `live_verified`, default `verified`). §1–§7 below describe a
single fleet (the historical SWE-bench Verified + econ path); **[§8](#8-other-benchmarks--the-matrix-launcher)
is the multi-benchmark + matrix layer** — run any subset of arm×benchmark combos as independent
fleets with `bench.sh`, flip dedicated GPUs with `gpu-flip.sh`, and pull everything with
`download-all.sh`. Read §8 first if you're running anything other than Verified.

## 0. Invariants (do not violate)
- **Never print API keys/tokens** — only their lengths. Keys: `LITELLM_API_KEY`, `EXA_API_KEY`, `FLY_API_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`.
- **Set `FLY_ORG=<your-team-org>` (required)** — never a personal org. `run-distributed.sh` defaults
  `ORG` to the non-functional placeholder `your-fly-org`; an unset `FLY_ORG` fails at app-create with
  `organization your-fly-org not found`. **App is per `ARM × BENCHMARK`**: `swebench-dist-<arm>-<slug>`,
  slug = benchmark key with `_`→`-` and `verified`→`verif` (fly's abuse filter blocks app names
  containing "verified" — a common phishing target), e.g. `live_verified` →
  `swebench-dist-econ-live-verif`, `swebench-dist-claude-real-verif` — each combo gets its own app, auto-created at prepare; runs
  within one app are further scoped by `fleet=<LABEL>` machine metadata, not a separate app per run.
- **Authentication for `claude-real` arm.** Set `CLAUDE_CODE_OAUTH_TOKEN` to your Claude Code subscription token (auto-loaded from repo-root `.env.local` by the launcher if present, else read from the environment). This token is never printed. The `claude` arm (open-weight ensemble) uses `ANTHROPIC_BASE_URL` redirect to the LiteLLM gateway; `claude-real` connects directly to Claude Code without any gateway or environment overrides.
  `APP=` overrides the derived name.
- **Web search: `ARM=econ` = Exa ON by default** (the econ agent ships Exa web search default-on
  across all tiers/personas), so **every econ benchmark run is a web-on result class**. The launcher
  sources `EXA_API_KEY` from `econ-coding-agent/.env.local` (canonical) then `e2e/econ/.env.local`,
  and injects it into workers. Set **`WEBSEARCH=0` to force a clean, baseline-comparable (no-web)**
  econ run (SWE-bench fixes are public on GitHub → web search = answer-lookup, so never compare a
  web-on run 1:1 against a no-web baseline or submit it). `ARM=claude` and `ARM=claude-real` stay STRICT opt-in
  (`WEBSEARCH=1` → Tavily for both arms).
- **econ conductor = minimax-m3** for the econ arm (matches the single-machine baseline config).
- **`LABEL` MUST be unique per run.** It names the fleet metadata (`fleet=<LABEL>`), the coordinator's
  `RUN_ID`, the coordinator volume (`dist_coord_<LABEL>`), and the local out-dir
  (`out/dist-<LABEL>/`). Reusing a LABEL against a still-live fleet double-seeds/mixes results.
- **Workers are volumeless.** DinD's data-root lives on the ephemeral rootfs (no per-worker volume
  to provision/teardown). Set `ROOTFS_GB` to enlarge that rootfs if env-image unpack is slow — see
  the calibration note below.

## 1. Architecture (summary — see `PLAN.md` §1 for the full rationale)
One small **coordinator** machine (SQLite work queue + aiohttp HTTP server on 6PN) plus N **worker**
machines (16 GB / 8 dedicated `performance` CPUs by default, no volume — `CPU_KIND=shared` is the
explicit cheap override). Each worker claims one instance at a time
(`POST /claim`), resolves + grades it by shelling out to the SAME `e2e/<arm>/local-docker/
run-benchmark.py` + swebench harness the single-machine runner uses, heartbeats during the run, and
reports the patch + grade back (`POST /complete`/`/fail`). A worker exits 0 and self-stops
(`restart:no`) once the queue drains. The coordinator aggregates every completed instance into one
merged bundle and holds it for the host to pull. The host (`run-distributed.sh`) builds the image,
seeds the queue, paces out workers, polls `/status`, pulls the bundle, and tears the fleet down by
`fleet=<LABEL>` metadata — never reimplementing resolve/grade itself.

## 2. Launch
```bash
cd e2e/distributed
MACHINES=5 ARM=econ LABEL=mini SUITE=mini ./run-distributed.sh
```
- **Smoke first** (always): `MACHINES=2 ARM=econ LABEL=dist-smoke TASKS="django__django-11880,django__django-11951,django__django-11790" ./run-distributed.sh`
- Task set: `SUITE=mini|lite|full` (full = all 500 SWE-bench_Verified, the default if none of
  `SUITE`/`TASKS`/`TASKS_FILE` is given), or `TASKS="id1,id2,..."`, or `TASKS_FILE=path`.
- `ROOTFS_GB` defaults to **50** (fly's max) for every benchmark since 2026-07-19 — workers pull
  1-4 GB per-task eval images from the private mirror and ENOSPC below it. Values 1-49 are floored
  back to 50 (**fly caps this at 50 GB**; higher values are clamped down to 50 too); set
  `ROOTFS_GB=0` to opt out entirely (fly's 8 GB default rootfs, no `--rootfs-size` flag — the
  old-flyctl fallback) — see the calibration note below.
- **Long tasks** (multi-hour resolves): the harness enforces nothing task-level anymore — no
  per-instance wall-clock ceiling (difficulty tiers deleted), no stall/progress watchdog, no
  timeout wrapper around the resolve call; the coding agent owns its own watchdog/thrash
  detection. Liveness needs no tuning: the worker heartbeats every 30 s and the coordinator
  only requeues a lease after `HEARTBEAT_TIMEOUT` (default `300` s) of silence, so a
  multi-hour resolve stays leased for its whole duration. The remaining backstops are all
  fleet-level, not task-level: `MAXWAIT` (default `864000` = 10 days, the *host's* poll
  ceiling — if it's hit the fleet keeps running and self-stops on drain, you just pull +
  destroy manually, §4/§5) and `NO_PROGRESS_GIVEUP` (the coordinator's wedge detector — gives
  up early only when nothing is leased and nothing completes). A grade-side subprocess cap
  (~24h, from `benchmarks.py`'s `timeout` descriptor field) still bounds the grader itself —
  eval infra, not the agent — and never binds in practice.
- **Completion signal is stream-independent.** The host detects "done" two ways: the `bundle_ready`
  beacon in the streamed `flyctl logs`, AND a durable `/data/BUNDLE_READY` sentinel the coordinator
  writes *after* the Tigris archive (race-safe — same moment as the beacon), polled over the reliable
  `ssh curl /status` channel. So a silently-dropped log stream on a long run (observed live: coordinator
  finished + archived, launcher stuck polling `done=None` past MAXWAIT) can no longer strand a finished
  run — the sentinel breaks the poll and the normal pull + teardown proceeds.
- **Parallel independent triggers (per-combo apps).** Each `ARM × BENCHMARK` combo builds and runs on
  its OWN fly app (`swebench-dist-<arm>-<slug>`, §0) — this replaced one app per `ARM` serving
  every benchmark. Two concurrent `flyctl deploy --build-only` calls against the SAME app contend on
  that app's Depot builder (observed live: a second benchmark's bake stalled the first's at
  context-upload). With per-combo apps, any two combos are fully independent triggers — fire
  `econ:verified` now and `econ:pro` an hour later, or both at once — builds, fleets, monitoring and
  teardown never interact. The image stays benchmark-agnostic (one `Dockerfile.dist`, the worker
  dispatches on `BENCHMARK`), so `IMAGE=` can still reuse one baked ref across sibling apps of the same
  arm; by default each app just bakes its own image on its own builder (no contention).
- **Parallel fleets + slow placement.** Multiple fleets *of the same combo* (same `ARM`+`BENCHMARK`,
  different `LABEL`) run concurrently on that combo's app — each is scoped by `fleet=<LABEL>` metadata,
  so a run only ever seeds/polls/tears down its own LABEL (this is what `bench.sh` relies on to fan out
  labels). Under placement contention (iad tight, or another fleet
  churning bakes in parallel) fly can CREATE a machine but not confirm `started` inside flyctl's
  start-wait window, so `flyctl machine run` exits non-zero even though the machine comes up seconds
  later. `run_machine` no longer treats that as fatal: it scrapes the created machine's id and polls it
  up to `SLOW_START_WAIT` (default `240` s) for `started`, proceeding if it comes up and only
  recreating if it truly never does. Without this a second fleet launched during a first fleet's churn
  would abort at "failed to reach desired start state" and orphan a coordinator that started moments later.
- Prereqs: `flyctl` logged in (token auto-read from `~/.fly/config.yml`); `FLY_ORG` set (§0);
  `LITELLM_API_KEY` exported or in `e2e/econ/.env.local`; `python3` on `PATH` (+ the `datasets`
  package if `SUITE=full/verified/lite`). The launcher rebuilds the econ binary from live source in its
  vendor step each run (single-machine build flow: [`e2e/econ/README.md`](../econ/README.md)); pass
  `SKIP_ECON_BUILD=1` to reuse the existing `packages/opencode/dist/` binary instead.
- **econ web-UI build gotcha.** A fresh `bun run --cwd packages/opencode build` can fail with
  `Could not resolve "../app/dist/assets/*.js"` — the embedded web UI isn't built, and it's dead weight
  for headless benchmark runs. Build with `bun run --cwd packages/opencode build --skip-embed-web-ui`,
  then run with `SKIP_ECON_BUILD=1` (and no `IMAGE=`) to bake a fresh image from that binary.
- `run-distributed.sh` does the build → seed → create coordinator + N workers (paced ≤1/s, 429/
  MANIFEST_UNKNOWN retried) → poll → pull → teardown, all in one run. It is safe to `Ctrl-C` — nothing
  about the fleet depends on the launcher process staying alive; fall back to the out-of-band commands
  in §3–§5 (status curl, `tools/pull_results.sh`, `run-distributed.sh destroy`).
- **Never edit `run-distributed.sh` while a launcher is running it.** Bash re-reads the script from disk
  by byte offset as it executes, so an edit mid-run shifts the offsets and corrupts the live read —
  crashing the running process (observed live: a cosmetic label edit during a run crashed teardown at
  `line 895: syntax error near 'done'`, leaving the coordinator up for a manual `destroy`). `bash -n`
  passing does NOT protect you — the file is valid; the running shell's read position is what breaks.
  Coordinator-baked files (`coordinator-entrypoint.sh`, `harness_*.py`) are safe to edit mid-run (already
  frozen in the image) — only the host-side launcher is read live. Edit it between runs, or check
  `pgrep -fl run-distributed.sh` first.

### Dedicated conductor (opt-in — `DEDICATED_CONDUCTOR=1`)
The conductor (`minimax/minimax-m3`, a 428B MoE) is served from Fireworks **serverless** by
default, whose shared TPM limits throttle us (`429`/`503`) above ~2 parallel resolves. Setting
`DEDICATED_CONDUCTOR=1` brings up an **ephemeral Fireworks dedicated-GPU deployment** (8× B300 288GB
FP4 — Fireworks' recommended default shape for minimax-m3; pinned id `bench-conductor`) for the
duration of the run and flips the shared `econ-litellm`
gateway to it — a dedicated deployment has *no shared rate limits*, so the whole fleet can run in
parallel.

```bash
# one-time: ship the gateway image that can do the flip (adds infra/litellm/econ-entrypoint.sh)
# (gateway infra lives IN THIS REPO at infra/litellm/ since 2026-07-19)
cd ../../infra/litellm && fly deploy -a econ-litellm

# then any distributed run can opt in:
cd e2e/distributed
MACHINES=25 ARM=econ LABEL=full-dedicated DEDICATED_CONDUCTOR=1 ./run-distributed.sh
```

- **Cost:** ~**$96/hr** while up (8× B300 @ $12/GPU-hr), billed per-GPU-second, **$0 when deleted**.
  This is a concurrency/reliability play, not a cost cut — raw conductor spend goes *up* vs serverless.
- **Ephemeral + safe:** the deployment is created after the coordinator (before workers) and deleted
  by a `trap` (`cleanup_dedicated`) on **any** exit — normal end, error, or `Ctrl-C` — so a mid-run
  abort can never orphan the $96/hr GPU. The teardown also unsets the gateway secret, reverting the
  conductor to serverless for the next run.
- **How the flip works (drift-safe):** the committed `infra/litellm/config.yaml` stays serverless
  (the OL-8.B2 drift test is untouched). The runner sets a `CONDUCTOR_DEPLOYMENT_PATH` fly secret;
  the gateway's `econ-entrypoint.sh` rewrites *only the conductor's upstream line*, in the container's
  config copy, at startup. Secret unset → back to serverless. No per-run image rebuild.
- **Exclusive:** flagged runs assume sole use of the shared gateway (they flip it for everyone). Don't
  run a second benchmark against `econ-litellm` while a `DEDICATED_CONDUCTOR=1` run is live.
- **Verification:** after the flip the runner probes `/health/liveliness` and `/v1/model/info`; a
  `WARN: could not confirm conductor routing` means the gateway image lacks the flip wrapper (do the
  one-time deploy above) — otherwise you'd pay for an unused GPU. Manual control:
  `./fireworks-conductor.sh {up|status|print-path|down}`.
- **Stale-flip preflight (prepare-side hard gate):** before creating any fleet machine,
  `run-distributed.sh` checks whether the gateway has ANY `<TIER>_DEPLOYMENT_PATH` secret set
  (leftover from a prior `DEDICATED_CONDUCTOR=1` run or a manual `gpu-flip.sh` flip) and, if so, runs
  `gpu-flip.sh --verify` (§8.4) against it. A non-PASS verdict — the dedicated deployment behind that
  flip is gone — **aborts the prepare** with `ERROR: gateway has a STALE dedicated flip — run
  ./gpu-flip.sh --verify (and --revert if dead) before preparing a fleet`, so a fleet never launches
  into a gateway that will 404 that tier for every worker. No tier secrets set → skipped silently; a
  `flyctl secrets list` failure only warns (never blocks a plain serverless run).
- **Stale-flip warning (teardown-side, non-fatal):** `destroy_fleet` re-checks the same secrets after
  tearing a fleet down and prints a prominent multi-line `WARNING` (never fails teardown) if any tier
  flip is still set — the underlying GPU may already be gone, and a stale flip 404s that tier
  fleet-wide for the *next* run against this shared gateway. Follow-up: `./gpu-flip.sh --verify`, then
  `./gpu-flip.sh --revert --<tier>` if it's dead.

## 3. Monitor
While it runs, `run-distributed.sh` streams the coordinator's `progress:` line
(`done`/`total`/`resolved` + per-instance status) every 30 s — that is the primary monitor when the
launcher is in the foreground. For an out-of-band check — a `bench.sh` matrix (backgrounded per combo),
the launcher died, or you `Ctrl-C`'d it — use the two read-only monitor scripts (both take a `<LABEL>
[APP]`, a `--matrix <id>`, or **no args = the run you most recently launched**, reading the same
`out/bench-*/manifest.tsv` `download-all.sh` uses; neither ever touches the fleet):

```bash
./status.sh                          # newest matrix: one line per fleet — armed?, workers, counts, resolved/total
./status.sh --matrix smk-e --watch   # live monitor (re-print every 15s) for a whole matrix
./status.sh smk-e-econ --instances   # one fleet + its per-instance table (id, status, resolved, which worker)
./status.sh --matrix smk-e --cost    # + total $ and per-tier (conductor/oracle/reasoner/executor) token·turn·cost
./status.sh <LABEL> --json           # raw /status JSON for a fleet

./debug-workers.sh                   # for every worker: what it holds (per /status) + a flagged log tail
./debug-workers.sh smk-e-econ --lines 120           # deeper per-worker tail of one fleet
./debug-workers.sh smk-e-econ --grep worker-loop    # only the claim/resolve lines (skip HF/httpx noise)
./debug-workers.sh smk-e-econ --follow              # live-stream both workers' logs, prefixed by machine id
./debug-workers.sh smk-e-econ --instance django__django-11999   # only the worker holding that instance
```

`status.sh` reads each fleet's coordinator `/status`; the default line now carries the **grade %**
(`resolved/total (NN%)`) and **retries** (`reatt`=re-attempts so far, `up4retry`=failed rows the fleet
reruns at drain, `dead`=permanently failed). `--cost` adds a **cost + per-tier breakdown** — total `$`,
`turns`, tokens, and a conductor/oracle/reasoner/executor split (`$`, %-share, in/out tokens, calls,
instance-count). **Cost source differs by arm:** econ and claude report real LiteLLM gateway spend
(from `litellm_spend_logs`); `claude-real` (Anthropic real models, no gateway) reports **claude-native cost**
(from Claude Code's own `total_cost_usd`) with source tag `"claude-native"` and is displayed as a separate line
("claude-native (Anthropic $, NOT litellm spend)") in the multi-fleet view — never LiteLLM, always Anthropic billing.
A multi-fleet view folds every fleet into one `MATRIX TOTAL` (grade % + cost across all runs). `debug-workers.sh`
additionally pulls each worker machine's `flyctl logs` and flags (`»»`) the boot/work state lines
(dockerd up, toolbox build, `claimed`, `resolve (ceiling=… tier=…)`, `Instances resolved`, `dead`,
`ERROR`). Reach for `status.sh` first ("where is every combo, and what's it costing?"), then
`debug-workers.sh` when one looks stuck / a patch came back empty.

Both are thin wrappers over the underlying mechanism — find the coordinator by metadata and curl its
`/status` on 6PN (the same call the launcher makes internally,
[`run-distributed.sh`](./run-distributed.sh) `poll_status`) — which you can still run by hand:
```bash
flyctl machines list -a swebench-dist-econ-verif    # app = swebench-dist-<arm>-<slug> (§0); pick role=coordinator, fleet=<LABEL>
flyctl ssh console -a swebench-dist-econ-verif --machine <COORD_ID> -C "curl -s localhost:8080/status"
```
`/status` returns `armed`, `workers_seen`, `counts` (`pending`/`leased`/`done`/`dead`/`failed`),
`resolved`/`total`, and a per-instance array (each row's `worker_id` is what `--instances` /
`debug-workers.sh` use to map instance→worker). Per-instance **cost/turns/by-tier aren't in `/status`**
— they live in the coordinator's `/data/queue.db` `tasks` table (completion-meta JSON), which is exactly
what `status.sh --cost` reads over ssh (no rebake, no LiteLLM query). The lookup/token/`/status`/queue.db
plumbing is single-sourced in [`tools/fleet-common.sh`](./tools/fleet-common.sh) (sourced by both scripts).

> The old `tools/bench-ctl.sh distributed-*` control surface was **removed** in the public-release
> reorg. There is no `bench-ctl.sh`; use the §3–§5 commands here.

## 4. Pull results
```bash
tools/pull_results.sh LABEL [APP]     # sftp-get /data/bundle.tgz -> out/dist-<LABEL>/bundle/
```
One-shot pull + extract that does **not** tear the fleet down. `pull_results.sh`'s built-in `APP` default
(`swebench-agent-dist`) predates the per-combo app scheme (§0) — always pass the combo's app explicitly
as the second arg: `swebench-dist-<arm>-<slug>` (e.g. `swebench-dist-econ-verif`,
`swebench-dist-claude-pro`). Overwrite-safe (the local
`bundle.tgz` is `rm -f`'d before the sftp get). `run-distributed.sh` already does this automatically
once it sees the `bundle_ready` beacon in the coordinator's logs — this is the manual/safety-net path
when you want the bundle before teardown (e.g. a `KEEP=1` debug run) or the host process died first.
The bundle only exists after the coordinator aggregates (post-drain), so a mid-run pull before any
instance completes finds no `bundle.tgz` yet.

## 5. Teardown
```bash
DESTROY_ONLY=1 LABEL=<label> ./run-distributed.sh   # or: LABEL=<label> ./run-distributed.sh destroy
```
Destroys every machine tagged `fleet=<LABEL>` (workers AND the coordinator) plus the coordinator
volume `dist_coord_<LABEL>`. `run-distributed.sh` already tears its own fleet down at the end of a
normal run (workers self-stop on drain, `restart:no`, so teardown just frees the stopped machines +
coordinator + volume) — this `destroy` path is the safety net for an aborted run, a killed host
process, or a stuck/never-draining fleet. Pass the same `ARM`/`BENCHMARK`/`APP`/`FLY_ORG` you launched
with if they weren't the defaults, so the derived `APP` and the metadata lookup target the right combo.

## 6. Known calibration note — ephemeral-rootfs IOPS
Workers have no volume; DinD's data-root sits on the worker's ephemeral rootfs, which is capped at
roughly 2000 IOPS / 8 MiB/s (vs ~8000 IOPS for a fly volume). If a smoke run shows env-image unpack
stalling and the default `ROOTFS_GB=50` (§0/§2) isn't enough, the workaround is a per-worker volume
analogous to the coordinator's (not wired by default — this is the flagged escape hatch, not the
steady-state path).

**Disk space (ENOSPC) — per-instance eval images.** The worker-loop prunes images surgically after each instance's container exits: it removes all images except `econ-toolbox` and `alpine`, then runs `docker image prune -f` for dangling layers. This surgical approach (not blanket `docker image prune -a -f`) is necessary because `Dockerfile.instance` does `COPY --from=econ-toolbox`; deleting the toolbox between tasks breaks the next build. The prune reclaims per-task eval+run images (`51jaswanth15/sweap-images:*` for Pro, `swebench/sweb.eval.x86_64.*` for Verified, `starryzhang/*` for Live) while preserving the toolbox, keeping disk to ~one instance's footprint (~7GB peak). `ROOTFS_GB=50` is now the global default (§0/§2) so no per-benchmark opt-in is needed for Pro/Live/live_verified.

**Worker disk housekeeping.** Beyond the per-instance image prune above, the worker (`worker/worker-loop.py`) reclaims per-instance eval/run images after EVERY graded task regardless of pass/fail (keep-list: the arm's toolbox image + `alpine`, `KEEP_IMAGE_REPOS` overrides it), runs a pre-instance disk guard before each `/claim` (soft threshold `DISK_SOFT_PCT`, default 75% — cheap image-only prune; hard threshold `DISK_HARD_PCT`, default 88% — escalates to a deep reclaim: stopped containers + builder cache + dangling volumes on top of the image prune), and fires that SAME deep reclaim as an emergency measure whenever an instance dies with an ENOSPC-signature error (errno 28 / "no space left" / "input/output error") before reporting `/fail`. Together this bounds disk to roughly one instance's footprint (~7GB peak) so the 50GB rootfs floor suffices across the whole SWE-bench family (Verified/Pro/Live) without a per-worker volume.

## 7. Troubleshooting (hard-won — read before re-touching the build)
- **Every resolve returns a 0-byte patch (`n_preds=0`, 0 turns, ~85 s wall).** The toolbox image is
  missing econ's `@opencode-ai/plugin` dependency, so econ's vendored project tools
  (`.opencode/tool/github-*.ts`, which `import "@opencode-ai/plugin"`) fail to load — and opencode
  treats an unloadable project tool as **fatal**, killing the session before the first model turn.
  Two guardrails in this repo keep the dep shipping; do **not** regress either:
  - (a) `e2e/.dockerignore` must re-include `dot-opencode/node_modules` (the `**/node_modules` rule
    would otherwise strip `@opencode-ai/plugin` from the build context). **The vendored dir is
    `dot-opencode`, not `.opencode`** — an un-ignore keyed on `.opencode` is a silent no-op.
  - (b) The vendor step in `run-distributed.sh` (and `fullresolve/run.sh`) prunes
    `dot-opencode/tool/github-*.ts` — SWE-bench never uses the GitHub tools, so belt-and-suspenders
    it out of the image.
  - Confirm inside the built image:
    `docker run --rm --entrypoint sh econ-toolbox -c 'test -d /opt/toolbox/.opencode/node_modules && echo OK || echo MISSING'`.
- **Never edit `run-distributed.sh` (or any live script) while a launch is running.** bash reads a
  script by byte-offset; a mid-run insert shifts every later offset and corrupts the live read
  (`line NNN: ooks: command not found`) and the fleet never comes up. Sequence your edits after the
  run finishes, or kill the launcher first.
- **Read durable logs, not fly stdout.** fly's stdout is a ~40-line rolling buffer. The real sinks
  are the coordinator's `/data/logs/server.log` (on its volume — the `/fail` reason with
  `stderr_tail` lands here) and the launcher's `out/dist-<LABEL>/coord.log` (full coordinator
  fly-log stream). The launcher's own stdout only carries `progress:` lines — don't grep it for
  failure reasons.
- **The pulled bundle's `queue.db` is empty.** The queue is WAL-mode and the bundle copies the `.db`
  without its `-wal` sidecar, so offline queries against the bundled DB see no rows. The live merge is
  unaffected (it reads the coordinator's live DB); for live per-instance cost/turns, query the
  coordinator's `/data/queue.db` directly over ssh, not the bundled copy.
- **Report shows `0 resolved` despite the harness resolving instances.** The merge normalizes both
  the aggregate swebench summary shape (`resolved_ids`/`submitted_ids`) and the per-instance harness
  shape (`{"<iid>": {"resolved": bool, ...}}`) the worker actually posts — if you see 0/0, confirm
  `tools/merge-reports.py` still carries `ids_from_report()`; it corrects both `merged.json` and the
  downstream `cost-report.md`.

## 8. Other benchmarks & the matrix launcher
Everything above runs one fleet. This section adds the **benchmark axis** and the tools that fan a
whole **arm × benchmark matrix** out as independent fleets. The single source of truth for every
benchmark's dataset / images / grade / timeout / traces / flow is
[`tools/benchmarks.py`](./tools/benchmarks.py) — one descriptor per benchmark; `run-distributed.sh`,
`suite.py`, and the worker all read it, so adding a benchmark is one descriptor, not a code sweep.

### 8.1 The benchmark axis (`BENCHMARK=`)
Set `BENCHMARK` on any `run-distributed.sh` invocation (default `verified`). It is orthogonal to
`ARM`. It selects the app suffix, the ids, the grader, the grade-side cap, and the trace set:

| `BENCHMARK` | flow | ids from | per-instance image | grade | grade-side cap | traces (extra) |
|---|---|---|---|---|---|---|
| `verified` (default) | resolve→grade | HF `princeton-nlp/SWE-bench_Verified` | `swebench/sweb.eval.x86_64.<key>` (public harness build/pull) or private mirror | swebench harness | 86400 s (grade subprocess only; resolve is unbounded) | events/err/**engine** |
| `lite` | resolve→grade | HF `princeton-nlp/SWE-bench_Lite` | same as Verified | swebench harness | 86400 s (grade subprocess only; resolve is unbounded) | events/err/engine |
| `pro` | resolve→grade | vendored `swebench-pro/sweap_eval_full_v2.jsonl` (no HF pull) | `51jaswanth15/sweap-images:<tag>` (mirror) | `tools/grade_pro.py` (Scale eval, `--use_local_docker`) | 86400 s (grade subprocess only; resolve is unbounded) | events/err/engine |
| `terminal` | **harness run** (fused, no patch) | vendored `terminal-bench/tasks/` dirs | built per-task from each task's Dockerfile (DinD; no registry) | `tools/harness_terminal.py` (Harbor `harbor run`, pytest end-state) | 86400 s outer-wrapper fallback only — Harbor's own per-task `task.toml` limit is the real ceiling | events/err/**trajectory/sessions** |
| `live_verified` | resolve→grade | HF `SWE-bench-Live/SWE-bench-Live` split `verified` (500 frozen ids) | `starryzhang/sweb.eval.x86_64.<key>` (public, own namespace) | SWE-bench-Live's OWN harness via `tools/grade_live.py` (trusts its `report.json`) | 86400 s (grade subprocess only, Verified defaults) | events/err/engine |

- **App scoping + LABEL fold.** Each `ARM × BENCHMARK` combo gets its own fly app,
  `swebench-dist-<arm>-<slug>` (slug = benchmark key with `_`→`-` and `verified`→`verif` — fly's
  abuse filter blocks app names containing "verified" — e.g. `BENCHMARK=live_verified` →
  `swebench-dist-econ-live-verif`) — so different benchmarks of the same arm never share an app (or
  its remote builder) in the first place. `APP=` overrides the derived name; `fleet=<LABEL>` metadata
  still further scopes runs WITHIN an app. For non-`verified` benchmarks the launcher ALSO folds
  `-<benchmark>` onto your `LABEL` (e.g. `LABEL=run1 BENCHMARK=pro` → fleet `run1-pro`), so a label
  collision within one app's fleet metadata can't happen either. `verified` keeps the raw LABEL
  (back-compat).
- **Two flows.** `resolve_then_grade` (verified/lite/pro/live_verified) runs the arm → `preds.json` → grade, and
  yields a leaderboard-submittable patch. `harness_run` (terminal) runs the agent *inside* the task
  container and grades with pytest — **there is no git patch**, so there is no submission for it.
- **The harness enforces no task-level limits.** There is no per-instance wall-clock ceiling, no
  difficulty-tier timeout, no stall/progress watchdog, and no timeout wrapper around the resolve
  call — the coding agent owns its own watchdog/thrash detection. Each descriptor's `timeout`
  field (default `86400` s, still env-overridable: `PER_INSTANCE_TIMEOUT` for
  verified/lite/live_verified, `PRO_PER_INSTANCE_TIMEOUT` for pro, `TERMINAL_PER_INSTANCE_TIMEOUT`
  for terminal) is a **grade-side subprocess cap only** — it bounds the grader (swebench
  `run_evaluation --timeout`, `grade_pro`/`grade_live`, or terminal's outer-wrapper fallback), never
  the agent's resolve. It never binds in practice. What DOES still protect the fleet: coordinator
  `HEARTBEAT_TIMEOUT` (dead-worker-VM detection, not slow tasks), fleet backstops `MAXWAIT` (10-day
  host poll ceiling) + `NO_PROGRESS_GIVEUP` (wedge detection), and — for terminal only — Harbor's own
  per-task `task.toml` limit, enforced internally as that benchmark's real scoring rule.
- **Failure-rerun (`MAX_FAILURE_RERUN`, default 1).** When the fresh queue drains, the coordinator
  gives each `failed` instance up to this many extra tries; a rerun's success overwrites the earlier
  failure in place (the bundle shows the rerun outcome). Set `0` to disable (exhausted attempts
  dead-letter straight to `dead`), or higher to retry more. Applies to every benchmark. Watch it live
  via `status.sh`'s `reatt`/`up4retry`/`dead` counters (§3). The invariant (rerun outcome persists over
  the initial failure) has a deterministic offline test: `python3 coordinator/test_failure_rerun.py`
  (drives the real `Queue` through fail→failed→rerun→complete-overwrite; no fly/docker).
- **Traces.** Every completed instance's transcript rides `/complete` and lands under
  `results/<label>/artifacts/<iid>/`: `events.jsonl` + `err.txt` for all; `engine.log` + `opencode.db`
  for the econ resolve path; `trajectory.json` + `sessions.cast` (Harbor agent trajectory + asciinema
  recording) for terminal. The set is descriptor-driven — a new trace type needs a descriptor entry
  and a coordinator column only.

### 8.2 Per-benchmark quick how-to
Each is one `run-distributed.sh` (or `bench.sh`) call with `BENCHMARK=` set. **Always smoke first**
with `MACHINES=2 SUITE=smoke` (the descriptor's 5-id set).

**Verified** (default) — nothing extra:
```bash
MACHINES=2 ARM=econ LABEL=v-smoke BENCHMARK=verified SUITE=smoke ./run-distributed.sh run
```
Images come from the public swebench harness. To pull from our **private mirror** instead (Hub
rate-limit insurance), set `SWEBENCH_NAMESPACE=51jaswanth15` — the worker's grade adds `--namespace`
and the harness pulls `51jaswanth15/sweb.eval.x86_64.*`. Populate that mirror with
`e2e/swebench-pro/mirror-sweap-images.sh DATASET=verified` (see that README).

**SWE-bench-Live (verified split)** — same flow/knobs as Verified, own dataset + harness:
```bash
MACHINES=2 ARM=econ LABEL=live-smoke BENCHMARK=live_verified SUITE=smoke ./run-distributed.sh run
```
Same shape as Verified (`resolve_then_grade`, 1 coordinator + N workers, submission-capable) — the
**only** delta is the task list: SWE-bench-Live's **verified split** (500 frozen instances, HF
`SWE-bench-Live/SWE-bench-Live` split `verified`, not the rolling live split) and images from the
public `starryzhang/*` Docker Hub namespace (`starryzhang/sweb.eval.x86_64.<key>`, the same
`__`→`_1776_` key transform as swebench) — front a multi-worker run with `SWEBENCH_REGISTRY_MIRROR` to
dodge Docker Hub rate limits, same knob as Verified; no `SWEBENCH_NAMESPACE` needed (the namespace is
already public). Graded by SWE-bench-Live's **own** vendored harness (`grade_module=grade_live`,
pinned in `Dockerfile.dist` at `/work/swebench-live`), NOT stock swebench — `tools/grade_live.py`
trusts the harness's own `report.json` "resolved" verdict rather than recomputing it. Two
non-obvious bake requirements (both learned from a failed smoke, see `Dockerfile.dist`): the harness
installs into an **isolated `/work/.venv-live`** because SWE-bench-Live's own pip package is
confusingly named `swebench` v1.0.0 and a shared-venv `pip install -e .` would **clobber** the real
`swebench>=4.1` that Verified/Pro grading depends on; and its `launch/` git submodule
(`microsoft/RepoLaunch`) is init'd **recursively** (a plain clone leaves it empty →
`ModuleNotFoundError: launch.core` at grade time). `grade_live.py` shells `/work/.venv-live/bin/python`.
`SUITE=live_verified-mini` is the 5-id smoke set. The arm axis is orthogonal — both `econ` and
`claude` run it.

**Disk:** `live_verified`'s per-instance footprint (the large `starryzhang/*` eval images plus
`grade_live` cloning/extracting into the worker's OUTER rootfs) overflows fly's 8 GB default rootfs
and ENOSPCs every task (proven live: run `seq-live-0718` lost 4/5 tasks to `OSError: [Errno 28] No
space left on device` at 8 GB). `ROOTFS_GB` now defaults to fly's 50 GB max for EVERY benchmark (§0/
§2), so `live_verified` needs no special case here anymore — set `ROOTFS_GB=0` to opt out if you
need the old 8 GB default rootfs behavior.

**Pro** — vendored ids + mirrored images, longer timeout:
```bash
MACHINES=2 ARM=econ LABEL=pro-smoke BENCHMARK=pro SUITE=smoke ./run-distributed.sh run
```
Ids resolve from the vendored `swebench-pro/sweap_eval_full_v2.jsonl` (no HF pull). **Resolve wiring
(econ arm):** `worker-loop.py._resolve` hands the econ runner the Pro descriptor fields —
`--ids-jsonl` (the vendored jsonl, not HF), `--dockerhub-username 51jaswanth15` (build each instance
FROM `51jaswanth15/sweap-images:<tag>`, the *same* image the grader pulls — the row's own `image_name`
is Scale's private ECR URL and is ignored), and `--repo-dir /app` (Pro repos live at `/app`, not
Verified's `/testbed`; the agent must edit + `git diff` there or grade applies an empty patch). The
image tag is computed by the exact `helper_code/image_uri.py` rule the grader uses, so resolve and
grade share one image. Mirror the full image set once with
[`e2e/swebench-pro/`](../swebench-pro/README.md) (`./mirror-sweap-images.sh`, needs a R/W Docker Hub
PAT). Grade is Scale's `swe_bench_pro_eval.py --use_local_docker --dockerhub_username 51jaswanth15`
via `tools/grade_pro.py`, which computes the verdict itself (Scale's `eval_results.json` reads
lowercase keys the jsonl doesn't have).

**Terminal-Bench 2.1** — Harbor registry dataset `terminal-bench/terminal-bench-2-1` (89 tasks; `2.0`/`2.1` are distinct dataset *names*, not `@version` tags), vendored at build via `harbor dataset download`, fused run+grade, no per-instance registry:
```bash
MACHINES=2 ARM=claude LABEL=tb-smoke BENCHMARK=terminal SUITE=smoke ./run-distributed.sh run
MACHINES=2 ARM=claude-real LABEL=tb-real-smoke BENCHMARK=terminal SUITE=smoke ./run-distributed.sh run
```
Ids are the vendored `terminal-bench/tasks/` directory names. Each task's image is built at run time
from its own Dockerfile inside the worker's DinD — nothing to mirror. `tools/harness_terminal.py`
shells `harbor run --model <m> --env docker` and grades on the pytest end-state; the result rides
`/complete` as `{resolved, harbor_result}` (no patch → no `preds.json`, no submission).

**Agent selection.** The `econ` arm shells the opencode agent as before (no change). The `claude`
and `claude-real` arms run a **custom Harbor agent** (`harbor run --agent-import-path harbor_agents:ClaudeUnerrAgent`,
module: `e2e/distributed/tools/harbor_agents.py`, pinned against `harbor==0.20.0`) that subclasses
Harbor's own `claude_code.ClaudeCode` and stages the **FULL unerr harness** inside each task container:
Claude Code install (Harbor's own installer), unerr CLI from vendored tgz (nvm node), `unerr install claude-code`,
unerr MCP server via Harbor's mcp_servers mechanism, shipped `.claude/agents/unerr-*.md` sub-agents
(delegation/escalation ladder), and the appended ON operator prompt (TRACK/FIX-DISCIPLINE/DELEGATION/ESCALATION
always; hook-dependent sections only with hooks on). **Before this change**, both claude arms ran
Harbor's **bare first-party** `claude-code` agent (no unerr/harness) — results from before this change
are bare-Claude baselines.

**Control knobs** (read by `harness_terminal.py`; `run-distributed.sh` forwards only when set):
`TERMINAL_STOCK_AGENT=1` reverts both claude arms to the bare first-party agent (the no-harness baseline
control). `HARNESS_HOOKS=1` opts into the `cc-harness-hooks` finish-gate (default OFF for terminal because
its deny/gate rules are SWE-bench-shaped, not terminal-shaped). **Cost** on terminal for `claude-real`:
`meta.cost` is stamped `source="claude-native"` with the agent's own reported USD (no LiteLLM vk mint),
consistent with the resolve flow; the `claude` (open-weights) arm still uses LiteLLM spend.

### 8.3 Fire a matrix: `bench.sh`
Run any subset of `arm:benchmark` combos as **independent, LABEL-scoped fleets**, in parallel
(default) or `--seq`. Each combo is its own coordinator + workers.
```bash
# explicit combos (3 independent fleets, in parallel):
./bench.sh run econ:verified claude:pro claude-real:verified

# cartesian product (3 arms × 3 benchmarks = 9 fleets):
./bench.sh run --arms econ,claude,claude-real --benches verified,pro,terminal --matrix july16

# preview only — resolves every combo's app/label/dataset, makes NO fly calls:
PLAN_ONLY=1 ./bench.sh run --arms econ,claude,claude-real --benches verified,pro,terminal
```
- **Modes:** `run` = full one-shot per combo (build+seed+create+arm+poll+pull+teardown — this is
  run-distributed.sh's default no-subcommand mode); `prepare` = build + create each fleet WARM
  (coordinator holding `/claim`, workers idle) then exit; `start` = arm each **prepared** fleet + poll
  + pull + teardown (the second half after a `prepare` + GPU flip — maps to run-distributed's `run`
  subcommand); `destroy` = tear every combo's fleet down. No `status` mode.
- Any `run-distributed.sh` env (`MACHINES`, `ROOTFS_GB`, `CPU_KIND`, `DEDICATED_CONDUCTOR`, …) set
  once on the `bench.sh` call is inherited by **every** combo. `--suite <s>` and `--matrix <id>` apply
  to all.
- `bench.sh` writes `out/bench-<matrix>/manifest.tsv` (arm, benchmark, **resolved** label, app — read
  back from each combo's own PLAN output so the fold/app rules stay single-sourced). `download-all.sh`
  reads it. Per-combo logs are `out/bench-<matrix>/<arm>-<bench>.log`.

### 8.4 Dedicated GPUs: `gpu-flip.sh` (you raise them; the flip routes to them)
You raise a dedicated Fireworks deployment per tier **manually** and pass its deployment id here.
`gpu-flip.sh` sets the matching `<TIER>_DEPLOYMENT_PATH` secret on the `econ-litellm` gateway; the
gateway's `econ-entrypoint.sh` rewrites just that tier's upstream to the dedicated `#deployments/…`
form at boot. Unset = serverless again. Runnable any time, any subset of tiers, independent of any run.
Tier → base-model slug mirrors `ECON_TIER_BINDING`: `conductor=minimax-m3 oracle=glm-5p2
reasoner=deepseek-v4-pro executor=gpt-oss-120b`.
```bash
./gpu-flip.sh --conductor <dep-id> [--oracle <id>] [--reasoner <id>] [--executor <id>]
./gpu-flip.sh --status                 # which tiers are currently dedicated vs serverless
./gpu-flip.sh --verify                 # probe all 4 tiers x {chat, responses} for a real tool call
./gpu-flip.sh --serverless             # revert ALL tiers
./gpu-flip.sh --revert --oracle        # revert just one tier (put --revert BEFORE the tier flag)
# prefix any command with --dry-run to print the flyctl call without running it
```
> This flips the **shared** gateway for everyone — don't run a second econ campaign against
> `econ-litellm` while a dedicated flip is live. `DEDICATED_CONDUCTOR=1` (§2) is the automatic,
> ephemeral, conductor-only alternative when you want the runner to own the GPU's lifecycle.

`--verify` is the health check for a flip: for each of conductor/oracle/reasoner/executor it POSTs a
"call the echo tool" prompt to both `/v1/chat/completions` and `/v1/responses` on the gateway (using
`LITELLM_API_KEY`/`LITELLM_MASTER_KEY`, else sourced from `infra/litellm/.env.local`) and classifies the
reply: a real tool call → `PASS`; `UnsupportedParamsError` → `FAIL(param-seam)`; a `NOT_FOUND`/`Model not
found`/`NotFoundError` body → `FAIL(STALE-FLIP)` (the dedicated deployment behind that tier's secret is
gone — printed alongside the exact revert command for that tier); anything else → `FAIL(other: ...)`
with the first 200 body chars. Prints an aligned `tier | flipped | chat | responses` table; exits 0 iff
all 8 probes PASS. `run-distributed.sh` calls this automatically as a prepare-side hard gate and a
teardown-side warning — see §2.

### 8.5 Recommended GPU-backed matrix flow (prepare → flip → start)
Warm the fleets before the GPU meter starts, so you only pay for the GPU while workers are actually
resolving:
```bash
M=july16
./bench.sh prepare --arms econ,claude --benches verified,pro,terminal --matrix $M   # warm, no GPU yet
# ... raise your dedicated GPUs on Fireworks, then route to them:
./gpu-flip.sh --conductor <id> --oracle <id> --reasoner <id>
./bench.sh start   --arms econ,claude --benches verified,pro,terminal --matrix $M   # arm gates → poll → pull → teardown
./download-all.sh --matrix $M --submission          # re-summarize every bundle + traces (+ submissions)
./gpu-flip.sh --serverless                          # revert the gateway
```

### 8.6 Pull everything: `download-all.sh`
One pass over a matrix's manifest — pulls each fleet's `bundle.tgz` (via `tools/pull_results.sh`),
extracts to `out/dist-<label>/bundle/`, and prints a per-combo summary (resolved/total, preds,
artifacts, dead):
```bash
./download-all.sh --matrix july16                       # pull all combos of that matrix
./download-all.sh --matrix july16 --submission --model-name unerr-claude-openmodels
./download-all.sh <label> <app> [<label> <app> ...]     # explicit fleets, no manifest
```
`--submission` also emits the leaderboard `all_preds.jsonl` (via `tools/make_submission.py`) for each
`resolve_then_grade` combo; it's skipped for `terminal` (fused run, no patch). Traces for every
instance are under `out/dist-<label>/bundle/results/<label>/artifacts/<iid>/`.

## 9. Archive to Tigris (opt-in — tear the fleet down, keep the data)
Every run's DATA — execution traces, grading, submission, logs, a generated `overview.json`, and the
`bundle.tgz` — can be pushed to **Tigris** (fly's S3-compatible store) at end-of-run, so the coordinator
and workers can be destroyed and the run is still fully lookup-able. The coordinator uploads *itself*
(after it bundles, before it holds); a failed upload is **non-fatal** (the host SFTP pull still works).
This is code data only (traces/logs/grades/submissions) — never the agent source.

**S3 layout** (sorted by category → date → runid → test):
```
s3://<bucket>/<prefix>/<benchmark>/<arm>/<YYYY-MM-DD>/<label>/
    overview.json          run summary: grade{resolved,total,pct} + cost{usd,by_tier,turns} + counts + per-instance
    bundle.tgz             the full tarball (one-shot restore)
    submission/            preds.json + all_preds.jsonl   (resolve_then_grade: verified/lite/pro/live_verified; skipped for terminal)
    grading/               merged.json + <iid>/report.json
    traces/<iid>/          events.jsonl, err.txt, engine.log, opencode.db (econ), trajectory.json + sessions.cast (terminal)
    results/               preds.json, meta.jsonl, dead.jsonl, cost-report.md
    logs/ ; db/queue.db
```
`overview.json` is the submissable/at-a-glance summary for Verified, Pro, and Terminal-2.1 alike — grade
% + **real-LiteLLM cost** + per-tier token/turn split, computed the same way as `status.sh --cost`.

**One-time provisioning** (billable Tigris bucket; prints S3 keys once → run it yourself):
```bash
FLY_ORG=<your-team-org> ./provision-tigris.sh          # creates bucket swebench-dist-archive,
                                                        # saves the keypair to .env.tigris (gitignored)
```
This creates the Tigris bucket + keypair and writes it to `e2e/distributed/.env.tigris` (gitignored,
0600) for the host lookup tool. **Per-combo auto-staging:** since apps are now per `ARM × BENCHMARK`
combo and created on demand (§0, §8.1), you don't need to attach secrets to every app yourself —
`run-distributed.sh` reads `.env.tigris` and stages `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` as
**fleet-app secrets** onto that combo's app the first time it runs with `ARCHIVE_TIGRIS=1` (idempotent —
skipped once the app already carries them; non-fatal on failure, meaning that run's archive would write
0 objects until backfilled). Run `provision-tigris.sh` once, ahead of any combo's first archived run.

**Enable on a run** (needs a fresh bake — the coordinator image gained the uploader + `boto3`):
```bash
ARCHIVE_TIGRIS=1 TIGRIS_BUCKET=swebench-dist-archive MACHINES=2 ARM=econ SUITE=smoke ./run-distributed.sh
ARCHIVE_TIGRIS=1 TIGRIS_BUCKET=swebench-dist-archive ./bench.sh run econ:verified claude:pro --suite smoke
```
Watch for the coordinator's `archived` beacon (or `archive_failed`, non-fatal). `ARCHIVE_TIGRIS` defaults
to `0` (off) — behaviour is unchanged unless you opt in.

**Look runs up later** (no live fleet — reads `.env.tigris` or env for creds; never prints them):
```bash
./tools/tigris-archive.sh list                          # every archived run: label, grade, cost
./tools/tigris-archive.sh list --benchmark pro --arm econ --date 2026-07-16
./tools/tigris-archive.sh overview <label>              # fast: just that run's overview.json (grade + cost + tiers)
./tools/tigris-archive.sh get <label> [--only traces|grading|submission|bundle] [--dest out/archive/<label>]
```
The uploader (`tools/tigris_archive.py`) also runs standalone against a pulled bundle
(`--data-dir out/dist-<label>/bundle --label <label> --arm <arm> --benchmark <b>`), and `--dry-run` prints
the exact object plan + overview without touching S3.
