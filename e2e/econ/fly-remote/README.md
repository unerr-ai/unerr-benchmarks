# econ SMOKE on fly.io

Runs the exact same smoke as `e2e/econ/smoke.sh` — one tiny task through
econ's headless CLI — but ON a fly.io machine. Validates, in the fly
environment, before spending on a real SWE-bench mini run:

- econ runs headless and actually edits the file (agent works)
- LiteLLM-gateway auth works from inside the VM
- the `cost_breakdown` telemetry event is emitted and parsed
- `opencode.db` populates and the SQLite per-tier reader (`econ-tier-cost.py`)
  works, including executor-subagent volume

The image vendors the **LOCAL** econ build (`linux-x64-baseline-musl`), not
npm — the latest econ code is uncommitted locally, so `run.sh` copies the
binary straight out of the sibling `econ-coding-agent` repo's `dist/` before
deploying.

## Run it

```bash
bash run.sh
```

## Prereqs

- `flyctl` logged in (token auto-read from `~/.fly/config.yml`)
- econ built locally: `cd ../../../../econ-coding-agent && bun install && bun run --cwd packages/opencode build`
- `LITELLM_API_KEY` exported, or set in `e2e/econ/.env.local` (gitignored)

## What it is not

This is a proof-of-life smoke — one instance, no Docker-in-Docker, no
SWE-bench images. The full 10-instance SWE-bench mini needs the DinD
instance-image path (mirror `e2e/codex/fly-remote/fullresolve`) as a
follow-up.

## Outputs

`out/build.log`, `out/machine-run.log`, `out/run.log` (full streamed logs,
including the `SMOKE VERDICT` block and `RESULT:` line).
