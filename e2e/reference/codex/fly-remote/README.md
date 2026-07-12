# e2e/reference/codex/fly-remote — run A/B benchmarks on fly.io (not your machine)

Runs the SWE-bench Lite **localization A/B** (Codex `gpt-5.4-mini`, with vs without
unerr) on an ephemeral fly.io machine. The image is built by **fly's remote
builder**, so **no local Docker is required**. unerr runs at full Pro tier
**offline** in-container (entitlement minted by `dev-entitlement.mjs`) — no login,
no cloud.

## Status

- ✅ Image builds on fly remote builder → `registry.fly.io/swebench-agent-codex:<label>` (~517 MB)
- ✅ fly app `swebench-agent-codex` created; auth via the token in `~/.fly/config.yml`
- ✅ Machine launches and boots the entrypoint
- ⏳ End-to-end in-container run (codex auth + unerrd + scoring) **not yet validated**
  — one `LIMIT=1` smoke confirms it. Held per "setup only, don't run."

## Layout

| file | role |
|---|---|
| `Dockerfile` | node24 + git + codex CLI + unerr (from `unerr-ai-unerr-0.3.4.tgz`) + harness |
| `entrypoint.sh` | mints offline-Pro entitlement, starts `unerrd`, logs codex in (api key), sets up two codex homes (±unerr), runs the harness |
| `loc-runner.mjs` | the A/B: clone repo@base → codex names the buggy file(s) → score vs gold-patch files → emit JSONL |
| `swebl.ndjson` | SWE-bench Lite rows (id/repo/base/problem/patch) baked into the image |
| `lib.sh`, `dev-entitlement.mjs` | from `e2e/common/` — offline Pro entitlement |
| `run.sh` | orchestrator: build (remote) → run one-shot machine → scrape results → destroy |
| `out/` | build/run logs + `loc-results.jsonl` (gitignored scratch) |

## Run it (when ready)

**Via the tiered launcher** (recommended for standard tiers):

```bash
# From e2e/reference/codex/, the launcher routes to this backend
../run-tier.sh pilot --backend fly        # 5 instances (requests:3, flask:2)
../run-tier.sh mini                       # 50 instances (django:20, sympy:12, scikit-learn:8, pytest:6, requests:4)
```

**Direct invocation** (custom repo selections):

```bash
# tiny smoke — 1 instance, both arms
SELECT=requests:1 LIMIT=1 ./run.sh

# default slice — 15 instances (requests:6, flask:3, pytest:6)
./run.sh

# reuse the already-built image (skip the rebuild)
IMAGE=registry.fly.io/swebench-agent-codex:run-1782217119 ./run.sh
```

Knobs (env): `SELECT` (`repo:n,...`), `LIMIT`, `ARMS` (`baseline,unerr`),
`REGION`, `MEM`, `CPUS`, `APP`, `IMAGE`.

Secrets: `OPENAI_API_KEY` is read from the env or `../../unerr-web-service/.env.local`;
the fly token is read from `~/.fly/config.yml`. Nothing is committed.

## Notes / next

- **Repository map** — The localization A/B uses all 12 SWE-bench Lite repos. The `mini` tier (50 instances) includes large repos (django:20, sympy:12, scikit-learn:8) to reach the scale. If you hit memory/disk limits on the fly machine, increase `MEM=` or `CPUS=` when calling `run.sh` (see Knobs, above).
- **Codex-only** for now (needs only `OPENAI_API_KEY`). A Claude arm needs an
  `ANTHROPIC_API_KEY` (headless Claude Code) — none present yet.
- Results are scraped from machine **stdout** (`{"ev":"result",...}` lines). For
  larger runs, switch to a fly **volume** mounted at `/work` + `fly ssh sftp get`.
- Docker **resolution** grading (true bill-per-solved-task) is the later upgrade —
  reuse `e2e/reference/codex/local-docker`'s per-instance images.
