# e2e/reference/codex/local-docker — Codex (± unerr) on SWE-bench

Paired A/B: the same Codex CLI agent solves the same SWE-bench instances **with**
unerr attached (arm B) and **without** (arm A). Same image, one env flip, so the
cost/turn/resolve delta is attributable to unerr.

This is the headline-credible config: **stock Codex + `unerr install codex`** —
what a real user runs, reproducible by anyone. (Arm "C", a forked Codex with
unerr natively bound, is a separate research arm and is NOT built here.)

## How it fits together

```
official instance image            toolbox (we build)            grader (standard)
swebench/sweb.eval.x86_64.<id>  +  Node + Codex + unerr     ->   swebench.harness
repo @ base_commit, deps, tests    + offline Pro entitlement      .run_evaluation
        │                                  │                            │
        └──────── derived image ───────────┘                            │
                  run Codex -> git diff = model_patch ──> preds.json ───┘
```

- **Environment** — every SWE-bench instance has a prebuilt image on Docker Hub
  with the repo already at `base_commit`, deps installed, tests runnable. We do
  not build repo environments.
- **Toolbox** — a relocatable `/opt/toolbox` (Node + `@openai/codex` + unerr
  built from your `unerr-cli` checkout + the entitlement minter + driver),
  grafted onto each instance image with `COPY --from` (one cached layer).
- **Grader** — the standard `run_evaluation` harness applies each patch and runs
  the tests in its own containers. It is agent-agnostic: it only reads a
  predictions file. Codex is just the agent that produced the patches.

## unerr without login (offline Pro)

See `e2e/README.md` for the full explanation. In short: `unerr_offline_pro()`
(in `e2e/common/lib.sh`) mints a dev-signed Pro entitlement and sets `UNERR_TOKEN`
before starting the daemon. The driver calls it in `run-instance.sh` before
`unerr pm start`.

## Preflight — prove unerr works BEFORE spending tokens

Run the health check first. It builds the instance image and, inside it,
verifies the whole chain with **no API key and no `codex exec` (zero cost)**:

```bash
python run-benchmark.py --instances 1 --preflight
```

It checks, in order, and prints `[PASS]`/`[FAIL]` for each:

1. toolbox binaries present (`node`, `codex`, `unerr`)
2. `unerr doctor` — native cozo/sqlite modules load in the grafted image
3. offline Pro entitlement minted + `dev-entitlement status` shows pro
4. `unerrd` socket is up (started after the entitlement env)
5. `unerr install codex` wrote `.codex/config.toml` (references unerr) + `AGENTS.md`
6. MCP path works: `initialize` → `tools/list` returns the unerr tools (no
   `-32003` cap refusal = login-skip worked) → `tools/call file_read` executes

A non-zero exit means unerr is NOT correctly attached — fix that before any paid
run. The `-32003` check is the empirical proof the offline-Pro path worked end to
end (if the daemon hadn't inherited the entitlement env, it would refuse here).

## Run it

**Quickstart via the tiered launcher** (recommended):

```bash
# From e2e/reference/codex/, the launcher routes to this backend
../run-tier.sh smoke                    # 1 instance
../run-tier.sh pilot                    # 5 instances
../run-tier.sh mini --backend local     # 50 instances, full bill
```

**Direct invocation** (full control over flags):

```bash
# 0. prereqs
pip install datasets swebench
export OPENAI_API_KEY=sk-...           # Codex auth
docker info >/dev/null                 # daemon up; ~30GB free disk for images

# 1. build the toolbox from your unerr-cli checkout (re-run when unerr changes)
UNERR_REPO=/path/to/unerr-cli ./build-toolbox.sh

# 2a. PREFLIGHT — prove unerr runs + MCP tools work in-image. No API key, $0.
python run-benchmark.py --instances 1 --preflight

# 2b. SMOKE TEST — one instance, both arms (~$0.2–0.6). Prove the pipeline.
python run-benchmark.py --instances 1 --mode both

# 3. grade the one instance (commands are printed at the end of step 2)
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified --split test \
  --predictions_path results/preds_on.json --run_id codex_on --max_workers 4

# 4. scale once green: Verified Mini (50). Watch cost — see below.
python run-benchmark.py --slice 0:50 --instances 50 --mode both
```

## Cost & sequencing (keep the first run < $10)

Single-pass Codex on Verified-Mini-class tasks runs ~$0.05–0.28/instance
(published mini-swe-agent range; your Codex cost may differ). So:

| Step | Instances × arms | Rough cost | Purpose |
|---|---|---|---|
| Smoke | 1 × 2 | ~$0.2–0.6 | pipeline works end-to-end |
| Pilot | 5 × 2 | ~$1–3 | shape of the delta, catch flakes |
| Mini | 50 × 2 | ~$5–28 | the number (paired delta is confident at n=50 for a ~50% effect) |

Run the smoke + pilot first; only spend on the full 50 once both arms produce
valid patches. Report cost **only on instances BOTH arms solved** (fidelity
gate) so you compare like-for-like.

## What you get

`results/preds_<mode>.json` (predictions) + `results/meta_<mode>.jsonl`
(per-instance wall time, exit code, patch size, stderr tail). After grading,
`run_evaluation` writes resolve rates per arm. Cost/turn come from the Codex
`--json` event stream captured inside the container (`/tmp/codex-events.jsonl`).

## Open items before a real run

- **Verified Mini dataset id** — defaulted to `princeton-nlp/SWE-bench_Verified`
  sliced to 50. If you have the exact Verified-Mini HF id, pass it via
  `--dataset`. Confirm the 50-instance selection you want is reproducible.
- **`REPO_DIR`** — defaults to `/testbed` (SWE-bench convention). If a pulled
  image checks out elsewhere, set `--repo-dir`.
- **Codex MCP wiring in-image** — `unerr install codex` writes `.codex/config.toml`.
  Verify Codex picks up the unerr MCP server inside the container on the smoke
  run (check `/tmp/unerr-install.log` and that `codex exec` makes unerr tool
  calls). This is the one integration seam to watch.
- **Disk** — instance images are large; prune between batches
  (`docker image prune`) or the 50-run will fill the disk.
