# Distributed SWE-bench runner

A work-stealing fleet that runs SWE-bench resolve+grade across N fly.io machines in
parallel instead of one machine serially. Full design in [`PLAN.md`](./PLAN.md) — this
is the concise operator doc: launch, monitor, pull, teardown.

## 0. Invariants (do not violate)
- **Never print API keys/tokens** — only their lengths. Keys: `LITELLM_API_KEY`, `EXA_API_KEY`, `FLY_API_TOKEN`.
- **Fly org = `your-fly-org` (team)** — never the personal org. App `swebench-agent-dist` (fixed; every
  run is scoped by `fleet=<LABEL>` machine metadata, not a separate app per run).
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
- Prereqs: `flyctl` logged in (token auto-read from `~/.fly/config.yml`); econ built locally (see
  the single-machine `RUNBOOK.md` §1) with `LITELLM_API_KEY` exported or in `e2e/econ/.env.local`;
  `python3` on `PATH` (+ the `datasets` package if `SUITE=full/verified/lite`).
- `run-distributed.sh` does the build → seed → create coordinator + N workers (paced ≤1/s, 429/
  MANIFEST_UNKNOWN retried) → poll → pull → teardown, all in one run. It is safe to `Ctrl-C` and
  fall back to the `bench-ctl` commands below — nothing about the fleet depends on the launcher
  process staying alive.

## 3. Monitor
```bash
tools/bench-ctl.sh distributed-status LABEL   # fleet machines (role+state) + coordinator progress
```
Lists every machine tagged `fleet=<LABEL>` with its role (`coordinator`/`worker`) and state, then
`flyctl ssh`s into the coordinator and curls its `/status` endpoint for `done`/`total`/`resolved`
plus a per-instance status line. Prints "coordinator not up yet" / "not responding yet" instead of
erroring if the fleet is still booting — safe to poll early.

(`tools/bench-ctl.sh` lives at `e2e/econ/fly-remote/fullresolve/tools/bench-ctl.sh` — same control
surface as the single-machine runbook, extended with a `distributed-*` command group so there is
still only ONE bench-ctl.)

## 4. Pull results
```bash
tools/bench-ctl.sh distributed-pull LABEL     # sftp-get /data/bundle.tgz -> out/dist-LABEL/bundle/
```
One-shot pull + extract, independent of whether `run-distributed.sh` is still running or already
exited. Overwrite-safe (the local `bundle.tgz` is `rm -f`'d before the sftp get, same gotcha as the
single-machine `pull` command). `run-distributed.sh` also does this automatically once it sees the
`bundle_ready` beacon in the coordinator's logs — this is the manual/safety-net path if you need
results before that, or the host process died first.

## 5. Teardown
```bash
tools/bench-ctl.sh distributed-destroy LABEL [-y]   # or FORCE=1
```
Destroys every machine tagged `fleet=<LABEL>` (workers AND the coordinator) plus the coordinator
volume `dist_coord_<LABEL>`. Prompts for confirmation unless `-y` is passed or `FORCE=1` is set.
`run-distributed.sh` already tears its own fleet down at the end of a normal run (workers self-stop
on drain, `restart:no`, so teardown just frees the stopped machines + coordinator + volume) — this
is the safety net for an aborted run, a killed host process, or a stuck/never-draining fleet.
`DESTROY_ONLY=1 LABEL=<label> ./run-distributed.sh` does the same fleet-by-metadata teardown via the
launcher itself, if you'd rather not reach for `bench-ctl`.

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
    `docker run --rm --entrypoint sh unerr-econ-toolbox -c 'test -d /opt/toolbox/.opencode/node_modules && echo OK || echo MISSING'`.
- **Never edit `run-distributed.sh` (or any live script) while a launch is running.** bash reads a
  script by byte-offset; a mid-run insert shifts every later offset and corrupts the live read
  (`line NNN: ooks: command not found`) and the fleet never comes up. Sequence your edits after the
  run finishes, or kill the launcher first.
- **Read durable logs, not fly stdout.** fly's stdout is a ~40-line rolling buffer. The real sinks
  are the coordinator's `/data/logs/server.log` (on its volume — the `/fail` reason with
  `stderr_tail` lands here) and the launcher's `out/dist-<LABEL>/coord.log` (full coordinator
  fly-log stream). The launcher's own stdout only carries `progress:` lines — don't grep it for
  failure reasons.
- **The pulled bundle's `queue.db` is empty.** The queue is WAL-mode and `distributed-pull` copies
  the `.db` without its `-wal` sidecar, so offline queries against the bundled DB see no rows. The
  live merge is unaffected (it reads the coordinator's live DB); re-analyze against the live
  coordinator, not the bundle.
- **Report shows `0 resolved` despite the harness resolving instances.** The merge normalizes both
  the aggregate swebench summary shape (`resolved_ids`/`submitted_ids`) and the per-instance harness
  shape (`{"<iid>": {"resolved": bool, ...}}`) the worker actually posts — if you see 0/0, confirm
  `tools/merge-reports.py` still carries `ids_from_report()`; it corrects both `merged.json` and the
  downstream `cost-report.md`.
