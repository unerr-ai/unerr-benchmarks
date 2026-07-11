# e2e — End-to-end bill benchmark

Answers the question: **does unerr lower the API bill for a real coding agent on real tasks?**

The benchmark runs the same coding agent on the same SWE-bench instances twice — once
with unerr's MCP tools attached (treatment) and once without (baseline). A/B delta
reported: **resolve rate**, **total tokens + $**, **mean turns**.

```
SWE-bench instances (Lite / Verified Mini / Verified)
         │
         ├── Arm A  Codex, no unerr  ──┐
         └── Arm B  Codex + unerr    ──┴─► scoring/  (swe-effi.ts) ──► ab-report.md
```

## Directory map

| Path | Role |
|---|---|
| `codex/local-docker/` | Full resolve-rate + cost run: Codex ± unerr inside official SWE-bench Docker images, graded by the standard harness. Needs local Docker + ~30 GB disk. |
| `codex/fly-remote/` | No-local-Docker variant: image built by fly's remote builder, one-shot machine, results scraped back. Localization A/B only (no resolution grading yet). |
| `common/` | Shared offline-Pro entitlement (`dev-entitlement.mjs`, `lib.sh`), preflight health-check (`preflight.sh`, `mcp-healthcheck.mjs`), and SWE-Effi scoring (`scoring/`). |
| `econ/` | Econ-coding-agent arm — scaffolded separately; see its own README. |

## Tiered launcher

Use `codex/run-tier.sh` to route each tier to the right backend. The script enforces the routing policy and prints the resolved command before execution.

| Tier | Size | Default backend | Backend options | What it produces |
|---|--:|---|---|---|
| smoke | 1 | local-docker | local only | full resolve + cost bill (on the laptop) |
| pilot | 5 | local-docker | local OR fly | local = full bill; fly = localization A/B |
| mini | 50 | fly | fly OR local | fly = localization A/B at scale; `--backend local` = full bill |

**Commands:**
```bash
./codex/run-tier.sh smoke                       # 1 instance, full bill, local-docker
./codex/run-tier.sh pilot                       # 5 instances, full bill, local-docker
./codex/run-tier.sh pilot --backend fly         # 5 instances, localization A/B, fly
./codex/run-tier.sh mini                        # 50 instances, localization A/B, fly
./codex/run-tier.sh mini --backend local        # 50 instances, full bill, local-docker
./codex/run-tier.sh <tier> --dry-run            # print the resolved command, run nothing
```

**Critical notes:**

1. **fly is localization-only today** — it runs the SWE-bench *Lite* file-naming A/B (codex names the buggy file, scored vs gold-patch files). It does NOT apply patches or run the grader, so it does NOT produce the resolve-rate/$ bill. The full-bill mini therefore stays on local-docker on purpose (`mini --backend local`).

2. **Different datasets** — local-docker uses SWE-bench **Verified** (full resolve); fly uses SWE-bench **Lite** (localization). They are not directly comparable as absolute numbers.

3. **fly mini may need more memory** — the 50-instance mini clones large repos (django, sympy, scikit-learn) to reach 50 instances. The fly machine may need memory/disk adjustment via `MEM=`/`CPUS=` env on `run.sh`.

## Shared runtime: offline Pro entitlement

Both codex backends use unerr at the Pro tier with **no login and no cloud** via two
env vars injected before any agent run:

- `UNERR_ENTITLEMENT_KID` / `UNERR_ENTITLEMENT_PUBKEY` — minted by
  `common/dev-entitlement.mjs mint pro`; lifts the free-tier 1-repo cap so unerr
  runs in every instance repo.
- `UNERR_TOKEN` — any non-blank value satisfies the login-presence wall
  (`src/cloud/login-gate.ts:hasHeadlessToken`); no cloud call is ever made.

Order matters: **always export these before `unerr pm start`** so the daemon
inherits them and enforces Pro at spawn.

These vars are set by `unerr_offline_pro()` in `common/lib.sh`, sourced by both
backends.

## Shared preflight

Before spending any tokens, verify the entire unerr chain works in-image:

```bash
# local-docker
python e2e/codex/local-docker/run-benchmark.py --instances 1 --preflight

# fly-remote: exit after the image build and a short smoke
SELECT=requests:1 LIMIT=1 e2e/codex/fly-remote/run.sh
```

`common/preflight.sh` checks (in order): toolbox binaries → native modules →
offline Pro minted → unerrd up → `unerr install codex` wrote config → MCP
`initialize → tools/list → file_read` all succeed (no `-32003`/`-32004` error).

## Scoring: SWE-Effi (`common/scoring/`)

Raw "fewer tokens" can be won by *failing faster*. SWE-Effi (arXiv 2509.09853)
scores resolve rate *against* resources: the normalized AUC of the token-bounded
effectiveness curve is the headline. "More resolves per token" is the defensible claim.

Once you have per-instance trajectory JSONL from both arms:

```bash
tsx e2e/common/scoring/run.ts armA-baseline.jsonl armB-unerr.jsonl
# → e2e/common/results/ab-report.md
```

See `common/scoring/README.md` for the trajectory schema and metric definitions.

## Quick-start

```bash
# local-docker full run (smoke first)
cd e2e/codex/local-docker
./build-toolbox.sh                                     # build toolbox image
python run-benchmark.py --instances 1 --preflight      # verify, $0
python run-benchmark.py --instances 1 --mode both      # smoke, ~$0.4

# fly-remote localization A/B (no local Docker)
SELECT=requests:1 LIMIT=1 e2e/codex/fly-remote/run.sh
```
