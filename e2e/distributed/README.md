# Distributed SWE-bench runner

A work-stealing fleet that runs SWE-bench resolve+grade across N fly.io machines in
parallel instead of one machine serially. Full design in [`PLAN.md`](./PLAN.md) — this
is the concise operator doc: launch, monitor, pull, teardown.

## 0. Invariants (do not violate)
- **Never print API keys/tokens** — only their lengths. Keys: `LITELLM_API_KEY`, `EXA_API_KEY`, `FLY_API_TOKEN`.
- **Set `FLY_ORG=<your-team-org>` (required)** — never a personal org. `run-distributed.sh` defaults
  `ORG` to the non-functional placeholder `your-fly-org`; an unset `FLY_ORG` fails at app-create with
  `organization your-fly-org not found`. App `swebench-agent-dist` (fixed; every run is scoped by
  `fleet=<LABEL>` machine metadata, not a separate app per run).
- **Web search OFF** (baseline-comparable): the launcher exports `EXA_API_KEY` unset unless
  `WEBSEARCH=1` — same rule as the single-machine econ runbook. SWE-bench fixes are public on
  GitHub → an enabled web search is answer-lookup.
- **econ conductor = minimax-m3** for the econ arm (matches the single-machine baseline config).
- **`LABEL` MUST be unique per run.** It names the fleet metadata (`fleet=<LABEL>`), the coordinator's
  `RUN_ID`, the coordinator volume (`dist_coord_<LABEL>`), and the local out-dir
  (`out/dist-<LABEL>/`). Reusing a LABEL against a still-live fleet double-seeds/mixes results.
- **Workers are volumeless.** DinD's data-root lives on the ephemeral rootfs (no per-worker volume
  to provision/teardown). Set `ROOTFS_GB` to enlarge that rootfs if env-image unpack is slow — see
  the calibration note below.

## 1. Architecture (summary — see `PLAN.md` §1 for the full rationale)
One small **coordinator** machine (SQLite work queue + aiohttp HTTP server on 6PN) plus N **worker**
machines (shared-cpu-8x / 16 GB, no volume). Each worker claims one instance at a time
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
- `ROOTFS_GB=50` enlarges each worker's ephemeral rootfs (**fly caps this at 50 GB**; higher values
  are clamped to 50 by the launcher) — see the calibration note below for when you need this.
- **Long tasks** (multi-hour resolves): `PER_INSTANCE_TIMEOUT` (default `14400` = 4 h) is the per-task
  resolve ceiling the worker enforces — a resolve running past it is killed and the task requeued;
  raise it for tasks that need longer. Liveness needs no tuning: the worker heartbeats every 30 s and
  the coordinator only requeues a lease after `HEARTBEAT_TIMEOUT` (default `300` s) of silence, so a
  multi-hour resolve stays leased for its whole duration. `MAXWAIT` (default `172800` = 48 h) is only
  the *host's* poll ceiling — if it's hit the fleet keeps running and self-stops on drain; you just
  pull + destroy manually (§4, §5).
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
cd ../../../econ-coding-agent/infra/litellm && fly deploy -a econ-litellm

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

## 3. Monitor
While it runs, `run-distributed.sh` streams the coordinator's `progress:` line
(`done`/`total`/`resolved` + per-instance status) every 30 s — that is the primary monitor. For an
out-of-band check (the launcher died or you `Ctrl-C`'d it), find the fleet's coordinator machine and
curl its `/status` on 6PN — the same call the launcher makes internally
([`run-distributed.sh`](./run-distributed.sh) `:233`):
```bash
flyctl machines list -a swebench-agent-dist    # pick the machine with role=coordinator, fleet=<LABEL>
flyctl ssh console -a swebench-agent-dist --machine <COORD_ID> -C "curl -s localhost:8080/status"
```
`/status` returns `counts` (`pending`/`leased`/`done`/`dead`), `resolved`/`total`, and a per-instance
array. Live per-instance **cost/turns/pull_s aren't in `/status`** — they land in the coordinator's
`/data/queue.db` `tasks` table (completion-meta JSON) and in the final bundle.

> The old `tools/bench-ctl.sh distributed-*` control surface was **removed** in the public-release
> reorg. There is no `bench-ctl.sh`; use the §3–§5 commands here.

## 4. Pull results
```bash
tools/pull_results.sh LABEL [APP]     # sftp-get /data/bundle.tgz -> out/dist-<LABEL>/bundle/
```
One-shot pull + extract that does **not** tear the fleet down. `APP` defaults to `swebench-agent-dist`
(the econ/default app; pass `swebench-agent-dist-claude` for the claude arm). Overwrite-safe (the local
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
process, or a stuck/never-draining fleet. Pass the same `ARM`/`APP`/`FLY_ORG` you launched with if
they weren't the defaults, so the metadata lookup targets the right app.

## 6. Known calibration note — ephemeral-rootfs IOPS
Workers have no volume; DinD's data-root sits on the worker's ephemeral rootfs, which is capped at
roughly 2000 IOPS / 8 MiB/s (vs ~8000 IOPS for a fly volume). If a smoke run shows env-image unpack
stalling, flip a worker to a volume instead — pass a larger `ROOTFS_GB` first (cheap, one flag), and
if that isn't enough, the workaround is a per-worker volume analogous to the coordinator's (not
wired by default — this is the flagged escape hatch, not the steady-state path).

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
