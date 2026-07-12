# Full-resolve SWE-bench Verified Mini-50 on fly.io

End-to-end **resolve + grade + cost** for `codex ±unerr` on the official
**SWE-bench Verified Mini-50** (25 django + 25 sphinx), run entirely on a fly.io
machine. Fly machines are real Firecracker microVMs on **native x86_64**, so the
SWE-bench instance images run with **no QEMU emulation** — the slow part of doing
this on an Apple-Silicon laptop disappears.

This reuses the laptop pipeline verbatim (`local-docker/run-benchmark.py` →
`swebench.harness.run_evaluation` → `report-runs.py`); it just runs it inside a
fly VM via **Docker-in-Docker**, with all output on an attached volume.

## What runs where

| Piece | Where | Notes |
|---|---|---|
| `run.sh` | your laptop | deploy + launch + stream + pull bundle + destroy |
| `Dockerfile` | fly remote builder | no local Docker needed |
| `entrypoint.sh` | inside the fly VM | boots dockerd, builds toolbox, resolves, grades, reports |
| instance images | inside the fly VM | `docker pull swebench/sweb.eval.x86_64.<id>` (native amd64) |
| `/data` volume | fly | docker data-root + all results + `bundle.tgz` |

## Run it

```bash
cd e2e/reference/codex/fly-remote/fullresolve

# 1) SMOKE FIRST — validates DinD + toolbox + codex + grade on fly (~minutes, ~cents)
INSTANCES=1 MODELS=gpt-5.4-mini ./run.sh

# 2) Full Mini-50, unerr arm only, both models (bare numbers come from HAL leaderboard)
./run.sh

# 3) Internal apples-to-apples A/B (also resolves bare codex)
MODES=both ./run.sh
```

### Knobs (env vars)

| Var | Default | Meaning |
|---|---|---|
| `MODELS` | `gpt-5.4-mini gpt-5.3-codex` | space-separated codex model ids |
| `MODES` | `on` | `on` (unerr) · `off` (bare) · `both` |
| `INSTANCES` | `0` (all 50) | cap per model — set `1` for smoke |
| `MEM` / `CPUS` | `16384` / `8` | machine size |
| `VOL_GB` | `200` | volume size (env images are tens of GB) |
| `REGION` | `iad` | fly region |
| `HOLD` | `1800` | seconds the VM holds open after finishing, for the SFTP pull |
| `RESUME` | `0` | `1` keeps prior results/reports on the volume (resume); default clears them for a fresh run |
| `MAXWAIT` | `5400` | host-side seconds to wait for `bundle_ready` before giving up |
| `CLEANUP` | `1` | after the run, `docker system prune -af` to reclaim the volume |
| `CLEANUP_VOLUMES` | `0` | also drop docker volumes during cleanup (`prune --volumes`) |
| `IMAGE` | — | reuse a prebuilt `registry.fly.io/...` ref, skip the build |

Auth is automatic: fly token from `~/.fly/config.yml`, `OPENAI_API_KEY` from env
or `../../../unerr-web-service/.env.local`. No key material is ever printed.

## Output (in `out/`)

- `run.log` — full machine log stream (headline resolve counts + cost report are echoed inline)
- `beacons.jsonl` — the `{"ev":...}` progress beacons (`dockerd_up`, `toolbox_built`, `model_start`, `resolve_done`, `grade_done`+resolved, `bundle_ready`, `all_done`)
- `bundle.tgz` + `bundle/` — the full `/data` tree: `results/<model>/preds_*.json` + `meta_*.jsonl` + per-instance `artifacts/`, `reports/<model>_<mode>.json` (swebench grade reports), `cost-report.{txt,md,json}`

## How unerr stays Pro without a login

The grafted toolbox mints a dev-signed offline-Pro entitlement
(`unerr_offline_pro` in `context/lib.sh`) inside each instance container — same as
the laptop runner. No `unerr login`, no network entitlement check.

## Disk cleanup (Docker fills up fast)

The `/data` volume holds the Docker data-root, so without cleanup the SWE-bench
**env images** (one per repo, GBs), the **derived per-instance images**, stopped
**grading containers**, and **build cache** accumulate tens of GB every run.

The entrypoint reclaims it automatically at the end (`CLEANUP=1`, the default):
after grading + bundling it runs `docker system prune -af` + `docker builder
prune -af` and logs the reclaimed space (`docker system df` before/after, plus a
`cleanup_done` beacon). After-grading nothing references those images, but they
are *tagged*, so the `-a` is what actually frees them (plain prune only drops
dangling layers). Set `CLEANUP=0` to keep the env images cached for faster
re-runs; `CLEANUP_VOLUMES=1` to also drop docker volumes.

The whole machine is destroyed after each run anyway — cleanup matters only
because the **named `/data` volume survives** and is reused next run. To wipe it
entirely: `flyctl volumes destroy bench_data -a swebench-agent-codex-fullresolve`.

**On your laptop**, the local-docker A/B leaves the same artifacts in *your*
Docker. Reclaim them manually (run.sh never touches your local Docker):

```bash
docker system df                 # see what's using space
docker system prune -af          # drop stopped containers + unused images + cache
# heaviest items are the swebench env/instance images:
docker images 'swebench/*' -q | xargs -r docker rmi -f
docker images 'unerr-codex-*' -q | xargs -r docker rmi -f
```

## Notes / gotchas

- **Grading is sequential per (model, mode).** The swebench HF cache
  cross-contaminates if two harnesses share a CWD — each grade gets its own
  scratch dir.
- **DinD storage** lives on the `/data` volume (`dockerd --data-root=/data/docker`);
  the VM rootfs is too small for the env images.
- **`--restart no` + hold:** the machine can't be SSH'd once PID1 exits, so the
  entrypoint bundles `/data` and `sleep`s `HOLD`s while `run.sh` SFTPs the bundle,
  then destroys it (ending the sleep).
- **Refresh the toolbox** after changing `../../../unerr-cli`: rerun
  `../../local-docker/build-toolbox.sh` to repack `context/unerr-ai-*.tgz` before
  deploying (the image bakes `context/` as-is).
