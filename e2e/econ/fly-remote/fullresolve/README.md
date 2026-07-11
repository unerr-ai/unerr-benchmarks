# econ full-resolve SWE-bench Verified Mini on fly.io

End-to-end **resolve + grade + cost** for the **econ** agent on the official
**SWE-bench Verified Mini** (first N of the pinned Mini-50), run entirely on a
fly.io machine via **Docker-in-Docker**. Fly machines are real Firecracker
microVMs on **native x86_64**, so the SWE-bench instance images run with **no
QEMU emulation** — the slow part of doing this on an Apple-Silicon laptop
disappears.

**SINGLE ARM.** econ has unerr compiled in (`packages/code-intelligence`), so it
runs **once** per instance — there is no on/off, no MODES, no `unerr install`, no
unerrd daemon, no MCP wiring. econ itself IS the (only) arm.

## Run it

```bash
cd e2e/econ/fly-remote/fullresolve

# 1) SMOKE FIRST — validates DinD + toolbox + econ + grade on fly (1 instance)
INSTANCES=1 ./run.sh

# 2) Verified Mini-10
INSTANCES=10 ./run.sh
```

`run.sh` **vendors the LOCAL musl econ build** (`linux-x64-baseline-musl` under
`$ECON_REPO/packages/opencode/dist`) into `local-docker/context/vendor/` before
the fly build — NOT npm, since the latest econ code is uncommitted locally.
Build econ first: `cd <econ-coding-agent> && bun install && bun run --cwd
packages/opencode build`.

Auth: fly token from `~/.fly/config.yml`; **`LITELLM_API_KEY`** from the env
or `e2e/econ/.env.local` (never printed). econ routes its model tiers via the
self-hosted LiteLLM gateway per `opencode.json` — no `--model` flag.

## Knobs (env vars)

| Var | Default | Meaning |
|---|---|---|
| `INSTANCES` | `1` | cap to the first N Mini-50 ids (`1` = smoke, `10` = Mini-10) |
| `MEM` / `CPUS` | `8192` / `4` | machine size (DinD needs room) |
| `VOL_GB` | `200` | volume size (env images are tens of GB) |
| `REGION` | `iad` | fly region |
| `HOLD` | `1800` | seconds the VM holds open after finishing, for the SFTP pull |
| `MAXWAIT` | `5400` | host-side seconds to wait for `bundle_ready` before giving up |
| `KEEP` | `1` | keep the machine after the run (`0` to destroy it) |
| `ECON_REPO` | `../../../../../econ-coding-agent` | sibling econ checkout to vendor from |

## Output (in `out/`)

- `run.log` — full machine log stream (resolve count + cost report echoed inline)
- `beacons.jsonl` — `{"ev":...}` progress beacons (`dockerd_up`, `toolbox_built`,
  `resolve_done`, `grade_done`+resolved, `resolve_summary`, `bundle_ready`)
- `bundle.tgz` + `bundle/` — the full `/data` tree: `results/econ/{preds.json,
  meta.jsonl, cost-report.{md,json}, artifacts/}`, `reports/econ.econ.json`
  (swebench grade report), `logs/`

## App / infra

- App `unerr-bench-econ-fullresolve`, org `vamsee-k-933` (team), region `iad`.
- Named volume `bench_data_econ` (200GB) holds the Docker data-root + all output;
  it survives across runs. Wipe it with
  `flyctl volumes destroy bench_data_econ -a unerr-bench-econ-fullresolve`.
